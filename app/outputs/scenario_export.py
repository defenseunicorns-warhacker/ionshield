"""
B1 — Scenario data export.

Replays a window of historical noaa_snapshots, fuses each onto the global
Region grid (using the same fusion code that the live pipeline uses), runs
the impact models, and emits the result as either:

  - GeoJSON FeatureCollection with per-feature time properties (for
    time-aware import into mapping tools and Earth Studio's
    `time` keyframe driver)
  - CSV with one row per (timestamp, region, system) tuple — the format
    most analytics + Earth Studio scripts read natively

GloTEC FeatureCollections are not stored historically (their listings churn
and we'd inflate the DB by 50–100×), so the replay deliberately uses the
storm-aware **climatology** path for TEC during a historical scenario.
This is a known approximation: it gives operators the right *shape* of
GPS / HF / radar impact across the storm but not the localized
post-sunset bubbles that real GloTEC captures. Live mode still has full
GloTEC fidelity.

Outputs from this module become the inputs to B2 (KML conversion for
Earth Studio) and B5 (Simulation Mode video player).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select

from app.data.db import get_engine, noaa_snapshots
from app.data.fusion import fuse_snapshot
from app.models.impact import assess_grid
from app.models.ontology import Region

logger = logging.getLogger(__name__)


def _utc_iso(t) -> str:
    """
    Convert any datetime / string timestamp into a UTC ISO 8601 string.

    SQLite returns stored DateTime columns as **naive** Python datetimes
    even when they were originally tz-aware UTC (the `Z` is dropped at
    serialize time). Calling `.astimezone(utc)` on a naive datetime would
    misinterpret it as local time and shift it by the host's offset —
    that's the bug we hit during scenario replay where CDT-host outputs
    were 5 hours later than the stored UTC values.

    This helper attaches UTC explicitly when tzinfo is missing.
    """
    if hasattr(t, "astimezone"):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).isoformat()
    return str(t)


# ── Snapshot replay ──────────────────────────────────────────────────────────


async def fetch_snapshots_in_range(
    start: datetime, end: datetime, max_rows: int = 5000,
) -> list[dict]:
    """Pull noaa_snapshots in [start, end] ordered by fetched_at ascending."""
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(noaa_snapshots)
            .where(noaa_snapshots.c.fetched_at >= start)
            .where(noaa_snapshots.c.fetched_at <= end)
            .order_by(noaa_snapshots.c.fetched_at.asc())
            .limit(max_rows)
        )).mappings().all()
    return [dict(r) for r in rows]


def _downsample(snapshots: list[dict], step_seconds: int) -> list[dict]:
    """Keep one snapshot per `step_seconds` slot. step_seconds=0 → keep all."""
    if step_seconds <= 0 or len(snapshots) <= 1:
        return snapshots
    out: list[dict] = []
    last_t: datetime | None = None
    for s in snapshots:
        t = s["fetched_at"]
        if last_t is None or (t - last_t).total_seconds() >= step_seconds:
            out.append(s)
            last_t = t
    return out


def _grid_for_snapshot(snapshot: dict) -> tuple[list, list]:
    """Run fuse + impact on a single snapshot row. Returns (fused, impacts)."""
    fused = fuse_snapshot(
        when=snapshot["fetched_at"],
        kp=float(snapshot["kp"]),
        bz_nt=float(snapshot["bz_nt"]),
        wind_speed_km_s=float(snapshot["wind_speed_km_s"]),
        xray_flux_wm2=float(snapshot["xray_flux"]),
        proton_flux_10mev_pfu=float(snapshot["proton_flux_10mev"]),
        f107_sfu=70.0,           # not stored in noaa_snapshots; use solar-min default
        glotec_fc=None,          # historical replay uses storm-aware climatology
    )
    return fused, assess_grid(fused)


# ── GeoJSON ──────────────────────────────────────────────────────────────────


def _region_polygon(r: Region) -> list[list[list[float]]]:
    """Build the rectangular polygon coordinates for a Region cell.

    GeoJSON Polygons are [ring][vertex][lon, lat]. Closing vertex repeats.
    """
    half_lat = r.lat_size_deg / 2
    half_lon = r.lon_size_deg / 2
    lat0 = r.lat_deg - half_lat
    lat1 = r.lat_deg + half_lat
    lon0 = r.lon_deg - half_lon
    lon1 = r.lon_deg + half_lon
    return [[
        [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0],
    ]]


def build_scenario_geojson(
    snapshots: list[dict],
    *,
    region_filter: Iterable[str] | None = None,
    geometry: str = "polygon",   # "polygon" | "point"
) -> dict[str, Any]:
    """
    Emit one FeatureCollection covering the full time range.

    Each feature has `properties.time_tag` (ISO UTC) so time-aware mapping
    tools can animate. Each region appears once per snapshot — total
    feature count = len(snapshots) × len(grid) (or × |region_filter|).
    """
    feature_set = set(region_filter) if region_filter else None
    features: list[dict] = []
    for snap in snapshots:
        fused, impacts = _grid_for_snapshot(snap)
        time_tag = _utc_iso(snap["fetched_at"])
        for obs, ia in zip(fused, impacts):
            if feature_set and obs.region.region_id not in feature_set:
                continue
            props = {
                "time_tag": time_tag,
                "region_id": obs.region.region_id,
                "lat_deg": obs.region.lat_deg,
                "lon_deg": obs.region.lon_deg,
                "geomag_lat_deg": obs.region.geomag_lat_deg,
                "kp": obs.kp_index,
                "bz_nt": obs.bz_nt,
                "tec_tecu": obs.tec_tecu,
                "gps_l1_error_m": ia.gps["GPS_L1"].error_m,
                "hf_absorption_db": ia.hf.absorption_total_db,
                "hf_blackout_probability": ia.hf.blackout_probability,
                "satcom_l_fade_db": ia.satcom["L"].fade_db,
                "radar_l_range_bias_m": ia.radar["L"].range_bias_m,
            }
            if geometry == "point":
                geom = {"type": "Point",
                        "coordinates": [obs.region.lon_deg, obs.region.lat_deg]}
            else:
                geom = {"type": "Polygon",
                        "coordinates": _region_polygon(obs.region)}
            features.append({"type": "Feature", "geometry": geom, "properties": props})

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_count": len(snapshots),
            "time_start": (_utc_iso(snapshots[0]["fetched_at"])
                           if snapshots else None),
            "time_end": (_utc_iso(snapshots[-1]["fetched_at"])
                         if snapshots else None),
            "regions": (sorted(feature_set) if feature_set else "all"),
        },
        "features": features,
    }


# ── CSV ──────────────────────────────────────────────────────────────────────


CSV_HEADERS = (
    "time_tag", "region_id", "lat_deg", "lon_deg", "geomag_lat_deg",
    "kp", "bz_nt", "tec_tecu",
    "gps_l1_error_m", "gps_l1l2_error_m",
    "hf_absorption_db", "hf_blackout_probability",
    "satcom_l_fade_db",
    "radar_l_range_bias_m", "radar_x_range_bias_m",
)


def build_scenario_csv(
    snapshots: list[dict],
    *,
    region_filter: Iterable[str] | None = None,
) -> str:
    """One row per (timestamp, region). Pure stdlib CSV — no pandas dep."""
    feature_set = set(region_filter) if region_filter else None
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for snap in snapshots:
        fused, impacts = _grid_for_snapshot(snap)
        time_tag = _utc_iso(snap["fetched_at"])
        for obs, ia in zip(fused, impacts):
            if feature_set and obs.region.region_id not in feature_set:
                continue
            writer.writerow((
                time_tag,
                obs.region.region_id, obs.region.lat_deg, obs.region.lon_deg,
                round(obs.region.geomag_lat_deg, 2),
                obs.kp_index, obs.bz_nt, round(obs.tec_tecu, 3),
                ia.gps["GPS_L1"].error_m, ia.gps["GPS_L1L2"].error_m,
                ia.hf.absorption_total_db, ia.hf.blackout_probability,
                ia.satcom["L"].fade_db,
                ia.radar["L"].range_bias_m, ia.radar["X"].range_bias_m,
            ))
    return buf.getvalue()


# ── High-level entry point ───────────────────────────────────────────────────


async def export_scenario(
    *,
    start: datetime,
    end: datetime,
    fmt: str = "geojson",
    step_seconds: int = 0,
    region_filter: Iterable[str] | None = None,
    max_snapshots: int = 500,
    geometry: str = "polygon",
) -> tuple[dict | str, dict[str, Any]]:
    """
    High-level entry point used by the API endpoint.

    Returns (payload, metadata). `payload` is a dict for GeoJSON or a
    string for CSV. `metadata` describes the replay window and downsample.
    """
    raw = await fetch_snapshots_in_range(start, end, max_rows=max_snapshots * 4)
    snapshots = _downsample(raw, step_seconds)[:max_snapshots]
    meta = {
        "raw_snapshot_count": len(raw),
        "downsampled_count": len(snapshots),
        "step_seconds": step_seconds,
        "fmt": fmt,
    }

    if fmt == "geojson":
        return build_scenario_geojson(
            snapshots, region_filter=region_filter, geometry=geometry,
        ), meta
    if fmt == "csv":
        return build_scenario_csv(
            snapshots, region_filter=region_filter,
        ), meta
    raise ValueError(f"Unknown export format: {fmt}")
