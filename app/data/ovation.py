"""
OVATION — NOAA SWPC Aurora (OVATION Prime) precipitation model.

OVATION gives the probability (%) of visible aurora on a global lat/lon grid.
For IonShield it is a high-latitude GNSS/comms degradation indicator: active
auroral precipitation co-locates with ionospheric scintillation and irregular
electron density that scrambles GNSS carrier phase (RTK/PPP) and HF/SATCOM
links. A mission whose route crosses an active auroral oval at high latitude
gets a location-specific elevated-risk flag the mid-latitude feeds would miss.

Source (real, machine-readable JSON; goes through SWPC_BASE_URL so a
relay/diode/offline mirror works automatically):
    {SWPC_BASE}/json/ovation_aurora_latest.json
    → {"Observation Time": ..., "Forecast Time": ...,
       "coordinates": [[lon(0..360), lat, aurora_prob_pct], ...]}

Same feed pattern as ustec/drap: module _cache, async fetch(),
cache_snapshot(); registered as a DataSource; persisted by state_cache.

Honesty contract: a real fetch is "NOAA SWPC OVATION"; demo is "DEMO";
absent either, status is "unavailable" and the mission layer omits the
aurora block (it never invents an oval).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SWPC = settings.swpc_base_url.rstrip("/")
OVATION_URL = f"{_SWPC}/json/ovation_aurora_latest.json"

# Aurora-probability thresholds (%) for GNSS/comms scintillation concern.
AURORA_ELEVATED = 20.0  # measurable auroral activity overhead
AURORA_HIGH = 50.0  # strong precipitation → likely scintillation

_cache: dict = {
    "grid": {},  # {(lat_int, lon_int): prob} sparse lookup
    "max_prob": 0.0,
    "observation_time": None,
    "forecast_time": None,
    "source": None,  # "NOAA SWPC OVATION" | "DEMO" | None
    "last_fetch": None,
    "fetch_status": {},  # {"ovation": "ok"|...}
}


# ── Parser ────────────────────────────────────────────────────────────────────


def _norm_lon(lon: float) -> int:
    """OVATION longitudes are 0..360; fold to nearest int in -180..180."""
    lon = ((lon + 180) % 360) - 180
    return int(round(lon))


def _build_grid(coords: list) -> tuple[dict, float]:
    grid: dict = {}
    mx = 0.0
    for row in coords:
        try:
            lon, lat, prob = float(row[0]), float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        grid[(int(round(lat)), _norm_lon(lon))] = prob
        if prob > mx:
            mx = prob
    return grid, mx


# ── Fetcher ─────────────────────────────────────────────────────────────────


async def fetch_ovation(timeout: float = 12.0) -> None:
    """Fetch + index the OVATION aurora grid. Fails cleanly (status only)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(OVATION_URL)
            r.raise_for_status()
            data = r.json()
        coords = data.get("coordinates") if isinstance(data, dict) else None
        if not coords:
            raise ValueError("Unexpected OVATION payload shape")
        grid, mx = _build_grid(coords)
        if not grid:
            raise ValueError("OVATION grid empty after parse")
        _cache.update(
            {
                "grid": grid,
                "max_prob": round(mx, 1),
                "observation_time": data.get("Observation Time"),
                "forecast_time": data.get("Forecast Time"),
                "source": "NOAA SWPC OVATION",
                "last_fetch": datetime.now(timezone.utc).isoformat(),
            }
        )
        _cache["fetch_status"]["ovation"] = "ok"
        logger.debug("OVATION: %d grid points, max %.0f%%", len(grid), mx)
    except httpx.TimeoutException:
        _cache["fetch_status"]["ovation"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["ovation"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("OVATION fetch error: %s", exc)
        _cache["fetch_status"]["ovation"] = "error"


# ── Accessors ─────────────────────────────────────────────────────────────────


def aurora_prob_at(lat: float, lon: float) -> float | None:
    """Aurora probability (%) nearest a location, or None if no data."""
    grid = _cache.get("grid")
    if not grid:
        return None
    la, lo = int(round(lat)), _norm_lon(lon)
    # exact, then small neighborhood (the grid is ~1°, occasional gaps)
    if (la, lo) in grid:
        return grid[(la, lo)]
    best = None
    for dla in (-1, 0, 1):
        for dlo in (-1, 0, 1):
            v = grid.get((la + dla, lo + dlo))
            if v is not None and (best is None or v > best):
                best = v
    return best


def aurora_risk_at(lat: float, lon: float) -> dict | None:
    """Operator-facing auroral GNSS/comms risk at a location. None if no data."""
    p = aurora_prob_at(lat, lon)
    if p is None:
        return None
    if p >= AURORA_HIGH:
        level = "HIGH"
    elif p >= AURORA_ELEVATED:
        level = "ELEVATED"
    else:
        level = "MINIMAL"
    return {"prob_pct": round(p, 0), "level": level}


def route_aurora_risk(waypoints: list[dict]) -> dict | None:
    """Worst-case auroral risk across a route's waypoints. None if no data."""
    worst = None
    for w in waypoints or []:
        r = aurora_risk_at(w.get("lat"), w.get("lon"))
        if r and (worst is None or r["prob_pct"] > worst["prob_pct"]):
            worst = {**r, "at": w.get("name")}
    return worst


def available() -> bool:
    return bool(_cache.get("grid"))


def cache_snapshot() -> dict:
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "source": _cache["source"],
        "max_prob": _cache.get("max_prob"),
        "observation_time": _cache.get("observation_time"),
        "available": available(),
    }


# ── Demo injection (WarHacker fixture — clearly labeled DEMO) ─────────────────


def set_demo_aurora() -> None:
    """A clearly-labeled DEMO active auroral oval (strong high-latitude
    precipitation poleward of ~55°). NOT live data."""
    grid: dict = {}
    mx = 0.0
    for la in range(40, 81):
        for lo in range(-180, 181, 2):
            if la >= 65:
                p = 80.0
            elif la >= 58:
                p = 55.0
            elif la >= 52:
                p = 25.0
            else:
                p = 3.0
            grid[(la, lo)] = p
            mx = max(mx, p)
    _cache.update(
        {
            "grid": grid,
            "max_prob": round(mx, 1),
            "observation_time": datetime.now(timezone.utc).isoformat(),
            "forecast_time": datetime.now(timezone.utc).isoformat(),
            "source": "DEMO",
            "last_fetch": datetime.now(timezone.utc).isoformat(),
        }
    )
    _cache["fetch_status"]["ovation"] = "demo"


def clear() -> None:
    _cache.update({"grid": {}, "max_prob": 0.0, "observation_time": None, "forecast_time": None, "source": None})
    _cache["fetch_status"].pop("ovation", None)
