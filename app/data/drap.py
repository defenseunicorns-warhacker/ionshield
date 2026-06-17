"""
D-RAP — NOAA SWPC D-Region Absorption Predictions (authoritative HF feed).

Replaces IonShield's modeled HF-absorption proxy (derived from GOES X-ray
flux via CCIR-888) with NOAA's authoritative D-RAP product where available.

Source (text product, goes through SWPC_BASE_URL so a relay/diode/offline
mirror works automatically):
    {SWPC_BASE}/text/drap_global_frequencies.txt

The product is a global grid of the **highest HF frequency (MHz) absorbed**
at each lat/lon — i.e. the top of the band knocked out by D-region
absorption. Higher value = worse HF blackout. We sample the grid at a
mission waypoint to get a location-specific HF degradation indicator,
which drives HF comms risk, frequency guidance, and SATCOM/VHF fallback.

Follows the existing feed pattern (app/data/ustec.py): module _cache,
async fetch(), cache_snapshot(). Registered as a DataSource so it inherits
the circuit breaker, timeout, and /health exposure. Persisted by
state_cache for cache-and-carry offline operation.

Honesty contract: a real fetch is source-labeled "NOAA SWPC D-RAP"; a demo
injection is labeled "DEMO"; when neither is present the status is
"unavailable" and the mission layer falls back to the modeled HF estimate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SWPC = settings.swpc_base_url.rstrip("/")
DRAP_URL = f"{_SWPC}/text/drap_global_frequencies.txt"

# HF-risk thresholds on the highest-absorbed-frequency (MHz) at a location.
# Quiet background sits ~0–2 MHz; an X-class flare / PCA pushes the whole HF
# band (3–30 MHz) into absorption.
HF_MINIMAL_MAX = 5.0  # < 5 MHz absorbed → only the lowest HF affected
HF_MODERATE_MAX = 15.0  # 5–15 MHz → lower/mid HF degraded
# > 15 MHz → most of HF blacked out

_cache: dict = {
    "grid": None,  # {"lats": [...], "lons": [...], "values": [[...]]}
    "valid_at": None,  # ISO timestamp from the product header
    "xray_message": None,
    "proton_message": None,
    "source": None,  # "NOAA SWPC D-RAP" | "DEMO" | None
    "last_fetch": None,  # ISO of last successful real fetch
    "fetch_status": {},  # {"drap": "ok"|"timeout"|"http_NNN"|"error"}
}


# ── Parser ────────────────────────────────────────────────────────────────────


def _parse_drap_text(text: str) -> dict:
    """Parse the SWPC drap_global_frequencies.txt tabular product."""
    lats: list[float] = []
    lons: list[float] = []
    values: list[list[float]] = []
    valid_at = None
    xray_message = None
    proton_message = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            low = line.lower()
            if "product valid at" in low:
                valid_at = line.split(":", 1)[1].strip() if ":" in line else None
            elif "x-ray message" in low:
                xray_message = line.split(":", 1)[1].strip()
            elif "proton message" in low:
                proton_message = line.split(":", 1)[1].strip()
            continue
        if set(line.strip()) <= {"-"}:  # divider row
            continue
        if "|" in line:  # grid data row:  LAT |  v v v ...
            try:
                lat_part, vals_part = line.split("|", 1)
                lat = float(lat_part.strip())
                row = [float(v) for v in vals_part.split()]
                lats.append(lat)
                values.append(row)
            except (ValueError, IndexError):
                continue
        else:  # the longitude axis row
            try:
                cand = [float(v) for v in line.split()]
                if len(cand) > 10 and not lons:
                    lons = cand
            except ValueError:
                continue

    if not values or not lons:
        raise ValueError("D-RAP grid not found in product")
    return {
        "grid": {"lats": lats, "lons": lons, "values": values},
        "valid_at": valid_at,
        "xray_message": xray_message,
        "proton_message": proton_message,
    }


# ── Fetcher ─────────────────────────────────────────────────────────────────


async def fetch_drap(timeout: float = 10.0) -> None:
    """Fetch + parse the D-RAP global product. Fails cleanly (status only)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(DRAP_URL)
            r.raise_for_status()
            parsed = _parse_drap_text(r.text)
        _cache.update(parsed)
        _cache["source"] = "NOAA SWPC D-RAP"
        _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
        _cache["fetch_status"]["drap"] = "ok"
        logger.debug(
            "D-RAP: %d×%d grid @ %s", len(parsed["grid"]["lats"]), len(parsed["grid"]["lons"]), parsed["valid_at"]
        )
    except httpx.TimeoutException:
        _cache["fetch_status"]["drap"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["drap"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("D-RAP fetch error: %s", exc)
        _cache["fetch_status"]["drap"] = "error"


# ── Accessors ─────────────────────────────────────────────────────────────────


def _nearest_index(axis: list[float], val: float) -> int:
    return min(range(len(axis)), key=lambda i: abs(axis[i] - val))


def absorption_freq_at(lat: float, lon: float) -> float | None:
    """Highest absorbed HF frequency (MHz) at a location, or None if no data."""
    g = _cache.get("grid")
    if not g or not g.get("values"):
        return None
    try:
        i = _nearest_index(g["lats"], lat)
        j = _nearest_index(g["lons"], lon)
        return float(g["values"][i][j])
    except (IndexError, ValueError, TypeError):
        return None


def hf_risk_at(lat: float, lon: float) -> dict | None:
    """Operator-facing HF risk at a location from D-RAP. None if no data."""
    f = absorption_freq_at(lat, lon)
    if f is None:
        return None
    if f >= HF_MODERATE_MAX:
        level, blackout = "SEVERE", True
    elif f >= HF_MINIMAL_MAX:
        level, blackout = "MODERATE", False
    else:
        level, blackout = "MINIMAL", False
    # Frequencies at/below the absorbed top are unreliable; above it are usable.
    return {
        "absorbed_to_mhz": round(f, 1),
        "level": level,
        "blackout": blackout,
        "guidance": (
            f"HF below ~{f:.0f} MHz absorbed; prefer higher frequencies or shift off HF"
            if f >= HF_MINIMAL_MAX
            else "HF usable across the band"
        ),
    }


def route_hf_risk(waypoints: list[dict]) -> dict | None:
    """Worst-case D-RAP HF risk across a route's waypoints. None if no data."""
    worst = None
    for w in waypoints or []:
        r = hf_risk_at(w.get("lat"), w.get("lon"))
        if r and (worst is None or r["absorbed_to_mhz"] > worst["absorbed_to_mhz"]):
            worst = {**r, "at": w.get("name")}
    return worst


def global_max_mhz() -> float | None:
    g = _cache.get("grid")
    if not g or not g.get("values"):
        return None
    return round(max(max(row) for row in g["values"] if row), 1)


def available() -> bool:
    return _cache.get("grid") is not None


def cache_snapshot() -> dict:
    """Read-only status snapshot for /health and provenance."""
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "source": _cache["source"],
        "valid_at": _cache["valid_at"],
        "xray_message": _cache["xray_message"],
        "proton_message": _cache["proton_message"],
        "global_max_mhz": global_max_mhz(),
        "available": available(),
    }


# ── Demo injection (WarHacker fixtures — clearly labeled DEMO) ────────────────


def set_demo_blackout() -> None:
    """Populate a synthetic, clearly-labeled DEMO grid: a polar/high-lat HF
    blackout (whole HF band absorbed poleward of ~50°). NOT live data."""
    lats = [float(x) for x in range(89, -90, -2)]
    lons = [float(x) for x in range(-178, 179, 4)]
    values = []
    for la in lats:
        amag = abs(la)
        # severe absorption at high latitude, tapering toward the equator
        v = 30.0 if amag >= 60 else 18.0 if amag >= 50 else 8.0 if amag >= 40 else 1.5
        values.append([v] * len(lons))
    _cache.update(
        {
            "grid": {"lats": lats, "lons": lons, "values": values},
            "valid_at": datetime.now(timezone.utc).isoformat(),
            "xray_message": "DEMO: X-class flare in progress",
            "proton_message": "DEMO: S2 proton event — polar cap absorption",
            "source": "DEMO",
        }
    )


def clear() -> None:
    _cache.update({"grid": None, "valid_at": None, "xray_message": None, "proton_message": None, "source": None})
