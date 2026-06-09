"""
NOAA SWPC data ingestion layer.

Fetches five real-time feeds from services.swpc.noaa.gov with per-feed resilience:
  kp      — planetary K-index (1-minute cadence)
  xray    — GOES X-ray flux 1–8 Å (6-hour, primary satellite)
  wind    — solar wind plasma: speed, density, temperature (2-hour)
  mag     — solar wind IMF including Bz GSM component (2-hour)  ← NEW
  proton  — GOES integral proton flux ≥10 MeV (1-hour)         ← NOW USED

Bz is the most critical missing input in v2: sustained southward Bz (< −10 nT)
is the primary geomagnetic storm driver, more predictive than speed alone.

Proton flux drives Polar Cap Absorption (PCA), which blankets HF comms
poleward of ~65° during solar energetic particle (SEP) events.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── NOAA endpoints ────────────────────────────────────────────────────────────
# Base URL is configurable (SWPC_BASE_URL) so disconnected enclaves can point
# at an internal mirror/relay instead of the public internet.

_SWPC = settings.swpc_base_url.rstrip("/")

NOAA_ENDPOINTS: dict[str, str] = {
    "kp": f"{_SWPC}/json/planetary_k_index_1m.json",
    "xray": f"{_SWPC}/json/goes/primary/xrays-6-hour.json",
    # Solar wind feeds moved from /json/ to /products/ in 2025+ and switched
    # from array-of-dicts to header-row + array-of-arrays format.
    "wind": f"{_SWPC}/products/solar-wind/plasma-2-hour.json",
    "mag": f"{_SWPC}/products/solar-wind/mag-2-hour.json",
    # Integral proton 1-hour file was retired; use 3-day file (multi-channel).
    "proton": f"{_SWPC}/json/goes/primary/integral-protons-3-day.json",
    # 3-day Kp forecast: header row + [time_tag, kp, observed|predicted, noaa_scale] rows
    "kp_forecast": f"{_SWPC}/products/noaa-planetary-k-index-forecast.json",
}

# Conservative quiet-time fallback values used when feeds are unavailable.
# These represent roughly median solar-minimum conditions — not worst-case,
# not best-case. Operators should treat fallback data as less reliable.
FALLBACK: dict[str, float] = {
    "kp": 2.0,  # K-index (0–9 scale)
    "xray_flux": 3e-7,  # W/m² (high-B / low-C boundary)
    "wind_speed": 400.0,  # km/s (typical slow solar wind)
    "wind_density": 5.0,  # cm⁻³
    "bz": 0.0,  # nT (neutral — neither geoeffective nor protective)
    "proton_flux_10mev": 0.1,  # pfu — background below S1 threshold (10 pfu)
}

# ── In-memory cache ──────────────────────────────────────────────────────────

_cache: dict = {
    "kp": None,
    "xray": None,
    "wind": None,
    "mag": None,
    "proton": None,
    "kp_forecast": None,  # NOAA 3-day Kp forecast (list with header row)
    "last_fetch": None,
    "fetch_status": {},  # key → "ok" | "timeout" | "http_NNN" | "error"
    "fetch_source": "startup",  # "live" | "fallback" | "startup"
}


# ── Fetcher ──────────────────────────────────────────────────────────────────


async def fetch_noaa(timeout: float = 10.0) -> None:
    """Fetch all NOAA endpoints. Each feed fails independently."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for key, url in NOAA_ENDPOINTS.items():
            try:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
                # wind, mag, and kp_forecast use a header row + tuple rows
                # (ASCII-style products); others are arrays of dicts.
                header_row_feeds = {"kp_forecast", "wind", "mag"}
                min_len = 2 if key in header_row_feeds else 1
                if not isinstance(data, list) or len(data) < min_len:
                    raise ValueError(f"Unexpected payload shape for {key}")
                _cache[key] = data
                _cache["fetch_status"][key] = "ok"
                logger.debug("NOAA %s: %d records", key, len(data))
            except httpx.TimeoutException:
                logger.warning("NOAA %s: request timed out", key)
                _cache["fetch_status"][key] = "timeout"
            except httpx.HTTPStatusError as exc:
                logger.warning("NOAA %s: HTTP %d", key, exc.response.status_code)
                _cache["fetch_status"][key] = f"http_{exc.response.status_code}"
            except Exception as exc:
                logger.warning("NOAA %s: fetch error: %s", key, exc)
                _cache["fetch_status"][key] = "error"

    _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
    ok_count = sum(1 for s in _cache["fetch_status"].values() if s == "ok")
    _cache["fetch_source"] = "live" if ok_count > 0 else "fallback"
    logger.info(
        "NOAA fetch complete: %d/%d feeds live (status: %s)",
        ok_count,
        len(NOAA_ENDPOINTS),
        _cache["fetch_status"],
    )


# ── Derived value accessors ──────────────────────────────────────────────────


def get_kp() -> float:
    """Current planetary K-index (0–9)."""
    try:
        data = _cache["kp"]
        if data:
            entry = data[-1]
            val = entry.get("kp_index") or entry.get("kp")
            if val is not None:
                return float(val)
    except Exception:
        logger.debug("get_kp: parse error, using fallback")
    return FALLBACK["kp"]


def get_xray_flux() -> float:
    """Current GOES X-ray flux in W/m² (1–8 Å long channel)."""
    try:
        data = _cache["xray"]
        if data:
            # Prefer 0.1–0.8 nm channel entries when labelled
            long = [e for e in data if "0.8" in str(e.get("energy", ""))]
            entries = long if long else data
            flux = float(entries[-1].get("flux", FALLBACK["xray_flux"]))
            return flux if flux > 0 else FALLBACK["xray_flux"]
    except Exception:
        logger.debug("get_xray_flux: parse error, using fallback")
    return FALLBACK["xray_flux"]


def get_xray_class() -> str:
    """GOES X-ray flare class: A / B / C / M / X."""
    flux = get_xray_flux()
    if flux >= 1e-4:
        return "X"
    if flux >= 1e-5:
        return "M"
    if flux >= 1e-6:
        return "C"
    if flux >= 1e-7:
        return "B"
    return "A"


def _row_value(rows: list, column: str) -> float | None:
    """
    Pull the latest non-null value from a header-row-style NOAA product.

    rows[0] is the column-name header; rows[1:] are tuples in matching order.
    Walks newest-first and returns the first parseable float for `column`.
    """
    if not rows or len(rows) < 2:
        return None
    header = rows[0]
    if column not in header:
        return None
    idx = header.index(column)
    for row in reversed(rows[1:]):
        if not isinstance(row, list) or len(row) <= idx:
            continue
        v = row[idx]
        if v is None or v == "" or str(v).lower() == "null":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def get_wind_speed() -> float:
    """Solar wind bulk flow speed in km/s."""
    try:
        v = _row_value(_cache["wind"] or [], "speed")
        if v is not None and v > 0:
            return v
    except Exception:
        logger.debug("get_wind_speed: parse error, using fallback")
    return FALLBACK["wind_speed"]


def get_wind_density() -> float:
    """Solar wind proton number density in cm⁻³."""
    try:
        v = _row_value(_cache["wind"] or [], "density")
        if v is not None and v > 0:
            return v
    except Exception:
        logger.debug("get_wind_density: parse error, using fallback")
    return FALLBACK["wind_density"]


def get_bz() -> float:
    """
    IMF Bz GSM component in nT.

    Sign convention: negative = southward = geoeffective.
    Sustained Bz < −10 nT is the primary indicator of impending/active
    geomagnetic storm. Bz > 0 (northward) is partially protective.
    """
    try:
        v = _row_value(_cache["mag"] or [], "bz_gsm")
        if v is not None:
            return v
    except Exception:
        logger.debug("get_bz: parse error, using fallback")
    return FALLBACK["bz"]


def get_proton_flux_10mev() -> float:
    """
    GOES integral proton flux at ≥10 MeV in pfu (p cm⁻² sr⁻¹ s⁻¹).

    NOAA S-scale thresholds:
      S1: 10 pfu   S2: 100   S3: 1 000   S4: 10 000   S5: 100 000
    Polar Cap Absorption (PCA) begins near S1 (~10 pfu).
    """
    try:
        data = _cache["proton"]
        if data:
            # NOAA integral proton file has multiple energy channels;
            # find the ≥10 MeV channel first, fall back to last entry.
            for entry in reversed(data):
                energy = str(entry.get("energy", ""))
                if "10" in energy and "mev" in energy.lower():
                    flux = entry.get("flux")
                    if flux is not None and float(flux) >= 0:
                        return float(flux)
            # Fallback: last entry regardless of channel label
            flux = data[-1].get("flux")
            if flux is not None and float(flux) >= 0:
                return float(flux)
    except Exception:
        logger.debug("get_proton_flux_10mev: parse error, using fallback")
    return FALLBACK["proton_flux_10mev"]


def data_age_seconds() -> int:
    """Seconds since last successful NOAA fetch. Returns 9999 if never fetched."""
    if _cache["last_fetch"]:
        try:
            dt = datetime.fromisoformat(_cache["last_fetch"])
            return int((datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            pass
    return 9999


def cache_snapshot() -> dict:
    """Return a read-only snapshot of cache metadata for the status endpoint."""
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_source": _cache["fetch_source"],
        "fetch_status": dict(_cache["fetch_status"]),
        "data_age_seconds": data_age_seconds(),
    }
