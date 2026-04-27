"""
Region × Time data fusion.

Aligns the NOAA scalar observations (kp, Bz, wind, X-ray, proton, F10.7) with
the GloTEC grid (lat/lon × TEC, anomaly, hmF2, NmF2) onto a global
Region grid, producing one FusedObservation per cell.

Inputs come from the live caches in app.data.noaa and app.data.ustec; this
module is pure with respect to those caches — the caller passes the cached
dicts so the fusion is deterministic and unit-testable.

Method:
  - For each Region in the global grid:
    - Look up the GloTEC FeatureCollection point nearest the region center.
      GloTEC is a quasi-uniform global grid so nearest-neighbor is adequate.
    - Broadcast the scalar NOAA / F10.7 values to the region.
  - When GloTEC is unavailable, fall back to climatology (median TEC, hmF2 ≈
    300 km, anomaly = 0). Fusion never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.ontology import (
    FusedObservation,
    Region,
    TimeWindow,
    global_grid,
)


# ── Index helpers for GloTEC nearest-neighbor lookup ─────────────────────────


def _index_glotec(fc: dict | None) -> dict[tuple[int, int], dict]:
    """
    Bucket GloTEC points into 5°×5° cells keyed by (lat_bucket, lon_bucket).

    We bucket once and then pick the bucket nearest each Region centroid;
    this avoids an O(N×F) scan over ~5,000 features × ~162 regions on each
    fusion call.
    """
    out: dict[tuple[int, int], dict] = {}
    if not fc:
        return out
    for feat in fc.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        # Wrap longitude into [-180, 180)
        if lon >= 180:
            lon -= 360
        bucket = (int(round(lat / 5.0)), int(round(lon / 5.0)))
        # Keep first feature per bucket; collisions are acceptable
        out.setdefault(bucket, feat)
    return out


def _climatology_tec(lat_deg: float, kp: float) -> float:
    """
    Storm-aware quiet-VTEC climatology used when GloTEC is unavailable.

    Quiet baseline by latitude (Bilitza IRI-2016 solar-min mid-day values):
      |lat| < 20° (equatorial): 15 TECu
      |lat| < 55° (mid-lat):    10 TECu
      else (polar):              6 TECu

    Storm-time enhancement (Mannucci et al. 2005, empirical, Kp > 4):
      VTEC = baseline · (1 + 0.40 · max(0, Kp - 4))

    Without this, an outage of GloTEC during a storm would silently make the
    impact layer report quiet-time GPS errors at high Kp — the opposite of
    what an operator needs.
    """
    a = abs(lat_deg)
    if a < 20:
        baseline = 15.0
    elif a < 55:
        baseline = 10.0
    else:
        baseline = 6.0
    storm_mult = 1.0 + max(0.0, (kp - 4.0) * 0.40)
    return baseline * storm_mult


def _glotec_at(
    index: dict[tuple[int, int], dict],
    lat: float,
    lon: float,
    kp: float = 0.0,
) -> dict[str, float]:
    """Nearest-bucket lookup. Storm-aware climatology when no point is found."""
    if not index:
        return {"tec": _climatology_tec(lat, kp), "anomaly": 0.0,
                "hmf2": 300.0, "nmf2": 1.5e11}
    target = (int(round(lat / 5.0)), int(round(lon / 5.0)))
    # Spiral outward in 5° steps until we hit a bucket; bounded radius keeps
    # this cheap on sparse caches.
    for r in range(0, 18):  # up to 90°
        for dlat in range(-r, r + 1):
            for dlon in range(-r, r + 1):
                if max(abs(dlat), abs(dlon)) != r:
                    continue
                key = (target[0] + dlat, target[1] + dlon)
                feat = index.get(key)
                if feat is None:
                    continue
                props = feat.get("properties") or {}
                qf = props.get("quality_flag", 0)
                if qf not in (0, None):
                    continue
                return {
                    "tec": float(props.get("tec", 15.0)),
                    "anomaly": float(props.get("anomaly", 0.0)),
                    "hmf2": float(props.get("hmF2", 300.0)),
                    "nmf2": float(props.get("NmF2", 1.5e11)),
                }
    # Spiral exhausted — fall back to storm-aware climatology
    return {"tec": _climatology_tec(lat, kp), "anomaly": 0.0,
            "hmf2": 300.0, "nmf2": 1.5e11}


# ── Public fusion API ────────────────────────────────────────────────────────


def fuse_snapshot(
    *,
    when: datetime | None,
    kp: float,
    bz_nt: float,
    wind_speed_km_s: float,
    xray_flux_wm2: float,
    proton_flux_10mev_pfu: float,
    f107_sfu: float,
    glotec_fc: dict | None,
    feed_quality: dict[str, str] | None = None,
    data_age_seconds: int = 0,
    grid: list[Region] | None = None,
) -> list[FusedObservation]:
    """
    Build a Region × FusedObservation list at a single timestamp.

    `glotec_fc` is the raw GloTEC FeatureCollection (or None); everything else
    is a scalar broadcast. Returns one FusedObservation per region in `grid`
    (default: 10°×20° global grid, 162 cells).
    """
    when = (when or datetime.now(timezone.utc))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    grid = grid or global_grid()
    index = _index_glotec(glotec_fc)
    fq = dict(feed_quality or {})

    fused: list[FusedObservation] = []
    for region in grid:
        local = _glotec_at(index, region.lat_deg, region.lon_deg, kp=kp)
        fused.append(
            FusedObservation(
                region=region,
                when=when,
                kp_index=kp,
                bz_nt=bz_nt,
                wind_speed_km_s=wind_speed_km_s,
                xray_flux_wm2=xray_flux_wm2,
                proton_flux_10mev_pfu=proton_flux_10mev_pfu,
                f107_sfu=f107_sfu,
                tec_tecu=local["tec"],
                tec_anomaly_tecu=local["anomaly"],
                hmf2_km=local["hmf2"],
                nmf2=local["nmf2"],
                data_age_seconds=data_age_seconds,
                feed_quality=fq,
            )
        )
    return fused


def fused_grid_payload(
    fused: list[FusedObservation],
    *,
    window: TimeWindow | None = None,
) -> dict[str, Any]:
    """
    Build a Foundry-row-shaped payload carrying the full fused grid.

    Foundry's auto-schema handles `rows` as an array column; alternatively
    callers can iterate `fused` and push one row per region for a relational
    layout (preferred for the location_risk_model dataset).
    """
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "window_start": window.start.isoformat() if window else None,
        "window_end": window.end.isoformat() if window else None,
        "n_regions": len(fused),
        "rows": [obs.to_dict() for obs in fused],
    }
