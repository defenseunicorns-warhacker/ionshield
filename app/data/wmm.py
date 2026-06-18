"""
WMM — World Magnetic Model (magnetic declination & compass reliability).

The WMM is the standard geomagnetic reference behind every magnetic compass,
DAGR/PLGR magnetic-heading display, and INS/magnetometer alignment. It gives:

  * declination — the angle between true north and magnetic north at a point.
    A dismounted team navigating by lensatic compass must apply this G-M angle;
    in CONUS it's a few degrees, but it swings widely and is essential at high
    latitude or for long-leg azimuths.
  * blackout / caution zones — near the magnetic poles the horizontal field is
    too weak for a compass to be trusted at all (blackout) or degraded
    (caution). That's a real, location-specific navigation limitation.

UNLIKE the SWPC/NASA feeds, the WMM is a *bundled coefficient model*, computed
locally with `pygeomag` — no network, no live fetch. That makes it perfectly
air-gap-native: it always works on the UDS enclave with zero connectivity.
Because there is nothing to poll, it is NOT registered as a DataSource; it is
an always-available on-demand calculator surfaced in the operational-feeds
block as the navigation-reference layer.

Honesty contract: this is a real, authoritative model evaluated for the
mission's coordinates and date — labeled "World Magnetic Model (local)". There
is no demo mode because there is nothing to fake; the math is the math.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_geomag = None  # lazily-constructed pygeomag.GeoMag (loads coefficients once)


def _model():
    global _geomag
    if _geomag is None:
        from pygeomag import GeoMag

        _geomag = GeoMag()
    return _geomag


def _decimal_year(when: datetime | None = None) -> float:
    d = when or datetime.now(timezone.utc)
    start = datetime(d.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(d.year + 1, 1, 1, tzinfo=timezone.utc)
    return d.year + (d - start).total_seconds() / (end - start).total_seconds()


def available() -> bool:
    """The model is bundled; available unless pygeomag fails to import."""
    try:
        _model()
        return True
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("WMM model unavailable: %s", exc)
        return False


def declination_at(lat: float, lon: float, alt_km: float = 0.0, when: datetime | None = None) -> dict | None:
    """Magnetic declination + compass-reliability at a point. None on failure.

    Positive declination is east (magnetic north east of true north); the
    grid-magnetic angle a navigator applies. Includes pole-proximity flags.
    """
    try:
        res = _model().calculate(glat=float(lat), glon=float(lon), alt=float(alt_km), time=_decimal_year(when))
        decl = round(float(res.d), 1)
        if getattr(res, "in_blackout_zone", False):
            reliability = "BLACKOUT"
        elif getattr(res, "in_caution_zone", False):
            reliability = "CAUTION"
        else:
            reliability = "RELIABLE"
        return {
            "declination_deg": decl,
            "direction": "E" if decl >= 0 else "W",
            "compass_reliability": reliability,
            "guidance": _guidance(decl, reliability),
        }
    except Exception as exc:
        logger.warning("WMM calculation error at (%s,%s): %s", lat, lon, exc)
        return None


def _guidance(decl: float, reliability: str) -> str:
    if reliability == "BLACKOUT":
        return "Magnetic compass unreliable (polar blackout zone) — use GPS/celestial/grid"
    if reliability == "CAUTION":
        return "Magnetic compass degraded near pole — cross-check heading with GPS"
    sign = "E" if decl >= 0 else "W"
    return f"Apply G-M angle {abs(decl):.1f}° {sign} when converting grid/magnetic azimuth"


def route_declination(waypoints: list[dict], when: datetime | None = None) -> dict | None:
    """Declination at the first waypoint plus the route's worst compass
    reliability. None if no usable waypoint."""
    pts = [w for w in (waypoints or []) if w.get("lat") is not None and w.get("lon") is not None]
    if not pts:
        return None
    primary = declination_at(pts[0]["lat"], pts[0]["lon"], when=when)
    if primary is None:
        return None
    order = {"RELIABLE": 0, "CAUTION": 1, "BLACKOUT": 2}
    worst = primary["compass_reliability"]
    for w in pts[1:]:
        r = declination_at(w["lat"], w["lon"], when=when)
        if r and order[r["compass_reliability"]] > order[worst]:
            worst = r["compass_reliability"]
    return {**primary, "route_compass_reliability": worst, "at": pts[0].get("name")}


def cache_snapshot() -> dict:
    """Provenance for /health. The WMM has no fetch state — it's local."""
    return {
        "source": "World Magnetic Model (local)",
        "available": available(),
        "network_required": False,
    }
