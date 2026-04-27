"""
Ionospheric driver ingestion: F10.7 cm solar radio flux and GloTEC.

F10.7 cm radio flux (Ottawa flux) is the standard solar activity proxy used by
ionospheric models (IRI, NeQuick) to parameterize daytime ionospheric density
when actual TEC measurements aren't available.

GloTEC is NOAA SWPC's near-real-time global TEC product. Each snapshot is a
~5,000-point GeoJSON FeatureCollection on a global grid with per-point TEC
(TECu), anomaly (TECu vs. quiet baseline), peak height hmF2 (km), peak density
NmF2, and a quality flag. Updated roughly every 10 minutes.

We fetch the listing, pull the latest file, and reduce to scalar summary
statistics (median, p95, max) plus retain the raw FeatureCollection for any
downstream code that needs the full grid (e.g. Earth Studio overlays in B1+).

Per-feed resilience and conservative fallbacks mirror the NOAA module.

Verified endpoints (2026-04-26):
  F10.7    — https://services.swpc.noaa.gov/json/f107_cm_flux.json
  GloTEC   — https://services.swpc.noaa.gov/products/glotec/geojson_2d_urt.json (listing)
             https://services.swpc.noaa.gov{listing_url} (latest snapshot)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SWPC_BASE = "https://services.swpc.noaa.gov"

USTEC_ENDPOINTS: dict[str, str] = {
    "f107": f"{SWPC_BASE}/json/f107_cm_flux.json",
    "glotec_listing": f"{SWPC_BASE}/products/glotec/geojson_2d_urt.json",
}

# Quiet-time ionospheric defaults — solar minimum, mid-latitude midday
FALLBACK: dict[str, float] = {
    "f107_sfu": 70.0,            # solar minimum baseline
    "glotec_median_tecu": 15.0,  # quiet daytime mid-lat TEC
    "glotec_p95_tecu": 30.0,
    "glotec_max_tecu": 50.0,
}

_cache: dict = {
    "f107": None,
    "glotec": None,            # latest FeatureCollection
    "glotec_time_tag": None,   # ISO time of latest snapshot
    "glotec_last_good_fetch": None,  # UTC iso of last successful fetch (stale-cache TTL)
    "last_fetch": None,
    "fetch_status": {},
}

# How long a stale GloTEC FC remains usable when listing returns empty.
# NOAA's listing has gaps of a few minutes during product updates; one hour
# of staleness is well within useful range for ionospheric nowcasting.
GLOTEC_STALE_TOLERANCE_SECONDS = 60 * 60


async def fetch_ionosphere(timeout: float = 10.0) -> None:
    """Fetch ionospheric driver feeds. Each feed fails independently."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # F10.7
        try:
            r = await client.get(USTEC_ENDPOINTS["f107"])
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                raise ValueError("Unexpected f107 payload shape")
            _cache["f107"] = data
            _cache["fetch_status"]["f107"] = "ok"
            logger.debug("Ionosphere f107: %d records", len(data))
        except httpx.TimeoutException:
            _cache["fetch_status"]["f107"] = "timeout"
        except httpx.HTTPStatusError as exc:
            _cache["fetch_status"]["f107"] = f"http_{exc.response.status_code}"
        except Exception as exc:
            logger.warning("Ionosphere f107: %s", exc)
            _cache["fetch_status"]["f107"] = "error"

        # GloTEC: listing → latest snapshot
        try:
            r = await client.get(USTEC_ENDPOINTS["glotec_listing"])
            r.raise_for_status()
            listing = r.json()
            if not isinstance(listing, list) or not listing:
                raise ValueError("Empty GloTEC listing")
            latest = listing[-1]
            url = latest.get("url")
            time_tag = latest.get("time_tag")
            if not url:
                raise ValueError("Listing entry missing url")
            r2 = await client.get(SWPC_BASE + url if url.startswith("/") else url)
            r2.raise_for_status()
            fc = r2.json()
            if fc.get("type") != "FeatureCollection" or "features" not in fc:
                raise ValueError("GloTEC payload not a FeatureCollection")
            _cache["glotec"] = fc
            _cache["glotec_time_tag"] = time_tag
            _cache["glotec_last_good_fetch"] = datetime.now(timezone.utc).isoformat()
            _cache["fetch_status"]["glotec"] = "ok"
            logger.debug(
                "Ionosphere glotec: %d features @ %s",
                len(fc.get("features", [])), time_tag,
            )
        except httpx.TimeoutException:
            _cache["fetch_status"]["glotec"] = _glotec_status_with_stale("timeout")
        except httpx.HTTPStatusError as exc:
            _cache["fetch_status"]["glotec"] = _glotec_status_with_stale(
                f"http_{exc.response.status_code}"
            )
        except Exception as exc:
            logger.warning("Ionosphere glotec: %s", exc)
            _cache["fetch_status"]["glotec"] = _glotec_status_with_stale("error")

    _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()


def _glotec_status_with_stale(failure: str) -> str:
    """
    Decide the visible glotec status when the live fetch failed.

    If we have a recent successful fetch, mark the status as "stale" and keep
    serving the cached FeatureCollection — that's strictly better for
    operators than collapsing to climatology over a transient gap.
    """
    last = _cache.get("glotec_last_good_fetch")
    if not last:
        return failure
    try:
        last_dt = datetime.fromisoformat(last)
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except Exception:
        return failure
    if age <= GLOTEC_STALE_TOLERANCE_SECONDS and _cache.get("glotec"):
        logger.info("Ionosphere glotec: %s — using stale cache (%.0fs old)", failure, age)
        return "stale"
    return failure


def get_f107_flux() -> float:
    """Daily F10.7 cm solar radio flux in sfu. Fallback: 70 (solar min)."""
    try:
        data = _cache["f107"]
        if data:
            entry = data[-1]
            for field in ("flux", "f10_7", "value"):
                v = entry.get(field)
                if v is not None and float(v) > 0:
                    return float(v)
    except Exception:
        logger.debug("get_f107_flux: parse error, using fallback")
    return FALLBACK["f107_sfu"]


def _glotec_tec_values() -> list[float]:
    fc = _cache.get("glotec")
    if not fc:
        return []
    out: list[float] = []
    for feat in fc.get("features", []):
        props = feat.get("properties") or {}
        tec = props.get("tec")
        qf = props.get("quality_flag", 0)
        if tec is None:
            continue
        # quality_flag == 0 means good in NOAA's convention
        if qf not in (0, None):
            continue
        try:
            tec_f = float(tec)
            if tec_f >= 0:
                out.append(tec_f)
        except (TypeError, ValueError):
            continue
    return out


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def get_glotec_summary() -> dict[str, Any]:
    """
    Scalar summary of the latest GloTEC snapshot.

    Returns median/p95/max TEC over good-quality features. Falls back to
    quiet-time defaults when no snapshot is cached.
    """
    vals = _glotec_tec_values()
    if not vals:
        return {
            "median_tecu": FALLBACK["glotec_median_tecu"],
            "p95_tecu": FALLBACK["glotec_p95_tecu"],
            "max_tecu": FALLBACK["glotec_max_tecu"],
            "n_features": 0,
            "time_tag": None,
        }
    vals_sorted = sorted(vals)
    return {
        "median_tecu": vals_sorted[len(vals_sorted) // 2],
        "p95_tecu": _percentile(vals_sorted, 95),
        "max_tecu": vals_sorted[-1],
        "n_features": len(vals_sorted),
        "time_tag": _cache.get("glotec_time_tag"),
    }


def get_glotec_featurecollection() -> dict[str, Any] | None:
    """Return the raw GloTEC GeoJSON FeatureCollection (for overlay export)."""
    return _cache.get("glotec")


def cache_snapshot() -> dict:
    """Read-only snapshot for status / archiving."""
    glotec = get_glotec_summary()
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "f107_sfu": get_f107_flux(),
        "glotec_median_tecu": glotec["median_tecu"],
        "glotec_p95_tecu": glotec["p95_tecu"],
        "glotec_max_tecu": glotec["max_tecu"],
        "glotec_time_tag": glotec["time_tag"],
        "glotec_n_features": glotec["n_features"],
    }
