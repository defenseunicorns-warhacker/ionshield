"""
IonShield API routes.

Security controls applied here:
  - Rate limiting via slowapi (limit string from config)
  - Optional API key authentication (X-API-Key header; enabled by setting API_KEY env var)
  - Input validation via Pydantic (lat/lon bounds, max waypoints, asset_type allowlist)
  - Max waypoints cap prevents oversized route requests
  - No bare exceptions — all errors logged and surfaced as structured HTTP responses
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.data.noaa import (
    cache_snapshot,
    get_bz,
    get_kp,
    get_wind_speed,
    get_xray_class,
    get_proton_flux_10mev,
)
from app.models.risk import compute_risk, compute_hf_link
from app.models.schemas import RouteRequest
from app.outputs.geojson import generate_geojson
from app.outputs.kml import generate_kml
from app.outputs.cot import build_cot_feed

logger = logging.getLogger(__name__)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


# ── Auth dependency ──────────────────────────────────────────────────────────


def verify_api_key(request: Request) -> None:
    """Reject requests missing a valid X-API-Key header when auth is enabled."""
    if not settings.auth_enabled:
        return
    provided = request.headers.get("X-API-Key", "")
    if provided != settings.api_key:
        logger.warning("Unauthorized request from %s", get_remote_address(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


_auth = Depends(verify_api_key)


# ── Data freshness helper ────────────────────────────────────────────────────


def _confidence(age_seconds: int) -> float:
    """
    Data confidence score based on cache age.

    1.0 — fresh  (< 5 min)   live NOAA data, fully trustworthy
    0.7 — recent (< 15 min)  one refresh cycle missed
    0.4 — stale  (< 1 hour)  multiple missed refreshes; use with caution
    0.2 — old    (≥ 1 hour)  significantly degraded; treat as indicative only
    """
    if age_seconds < 300:
        return 1.0
    if age_seconds < 900:
        return 0.7
    if age_seconds < 3600:
        return 0.4
    return 0.2


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@router.get("/health", include_in_schema=False)
async def health():
    """Lightweight liveness probe — no rate limit, no auth, no NOAA I/O."""
    return {"status": "ok"}


@router.get("/api/status")
@limiter.limit(settings.rate_limit)
async def api_status(request: Request, _: None = _auth):
    """System health and current solar-terrestrial conditions."""
    kp = get_kp()
    if kp < 4:
        global_risk = "NOMINAL"
    elif kp < 5:
        global_risk = "ELEVATED"
    elif kp < 7:
        global_risk = "DEGRADED"
    else:
        global_risk = "SEVERE"

    snap = cache_snapshot()
    age = snap["data_age_seconds"]
    return {
        "ionshield_version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "solar_drivers": {
            "kp_current": round(kp, 1),
            "xray_class": get_xray_class(),
            "bz_nt": round(get_bz(), 1),
            "solar_wind_km_s": round(get_wind_speed()),
            "proton_flux_10mev_pfu": round(get_proton_flux_10mev(), 2),
        },
        "global_risk_level": global_risk,
        "data_source": "NOAA SWPC",
        "last_fetch": snap["last_fetch"],
        "data_age_seconds": age,
        "confidence": _confidence(age),
        "fetch_source": snap["fetch_source"],
        "feed_status": snap["fetch_status"],
    }


@router.get("/api/risk/location")
@limiter.limit(settings.rate_limit)
async def risk_location(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, description="Latitude  (decimal degrees)"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude (decimal degrees)"),
    asset_type: str = Query(
        default="GPS_L1",
        description="GPS asset type: GPS_L1, GPS_L1L2, GPS_L1L5, GPS_INS, SBAS",
    ),
    _: None = _auth,
):
    """Full operational risk assessment for a single geographic point."""
    result = compute_risk(lat, lon, asset_type=asset_type)
    result["confidence"] = _confidence(result["data_age_seconds"])
    return result


@router.post("/api/risk/route")
@limiter.limit(settings.rate_limit)
async def risk_route(request: Request, req: RouteRequest, _: None = _auth):
    """Per-waypoint risk assessment for a multi-leg route."""
    if len(req.waypoints) > settings.max_route_waypoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Route exceeds maximum of {settings.max_route_waypoints} waypoints.",
        )

    kp = get_kp()
    results = []
    worst_score = -1
    worst_idx = 0

    _RISK_FILL = {
        "SEVERE": "#EF4444",
        "DEGRADED": "#F97316",
        "ELEVATED": "#F59E0B",
        "NOMINAL": "#10B981",
    }

    for i, wp in enumerate(req.waypoints):
        risk = compute_risk(wp.lat, wp.lon, kp, asset_type=req.asset_type)
        a = risk["assessment"]
        entry = {
            "index": i,
            "name": wp.name or f"WP{i:03d}",
            "lat": wp.lat,
            "lon": wp.lon,
            "zone": risk["zone"],
            "risk_level": a["risk_level"],
            "risk_score": a["risk_score"],
            "risk_color": _RISK_FILL.get(a["risk_level"], "#10B981"),
            "gps_error_m": a["gps_error_m"],
            "hf_absorption_db": a["hf_absorption_db"],
            "hf_blackout_prob": a["hf_blackout_probability"],
            "satcom_fade_db": a["satcom_fade_db"],
            "s4_index": a["s4_index"],
            "pca_active": a["pca_active"],
            "watch_notes": a["watch_notes"],
        }
        results.append(entry)
        if a["risk_score"] > worst_score:
            worst_score = a["risk_score"]
            worst_idx = i

    worst = results[worst_idx] if results else None
    w_name = worst["name"] if worst else "—"
    w_err = worst["gps_error_m"] if worst else 0
    w_level = worst["risk_level"] if worst else "NOMINAL"

    if worst_score >= 60:
        route_rec = (
            f"NO-GO — Waypoint {worst_idx} ({w_name}) at {w_level}. "
            f"GPS error {w_err} m. Postpone or re-route."
        )
    elif worst_score >= 40:
        route_rec = (
            f"CAUTION — Waypoint {worst_idx} ({w_name}) shows degraded conditions. "
            f"GPS error {w_err} m. Consider delay or backup nav."
        )
    elif worst_score >= 20:
        route_rec = (
            f"ADVISORY — Elevated risk at waypoint {worst_idx} ({w_name}). "
            f"GPS error {w_err} m. Monitor conditions."
        )
    else:
        route_rec = "GO — All waypoints nominal. Standard operations."

    snap = cache_snapshot()
    age = snap["data_age_seconds"]
    return {
        "route_summary": {
            "total_waypoints": len(results),
            "worst_waypoint_index": worst_idx,
            "worst_gps_error_m": w_err,
            "max_risk_level": w_level,
            "max_risk_score": worst_score,
            "route_recommendation": route_rec,
            "asset_type": req.asset_type,
        },
        "waypoints": results,
        "kp_current": round(kp, 1),
        "bz_current_nt": round(get_bz(), 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_age_seconds": age,
        "confidence": _confidence(age),
    }


@router.get("/api/forecast")
@limiter.limit(settings.rate_limit)
async def api_forecast(request: Request, _: None = _auth):
    """
    72-hour Kp forecast with per-window operational risk outlook.

    Returns:
      summary   — peak Kp, storm watch/warning flag, plain-English outlook text
      windows   — 7 time windows: 1h trend, 0-3h, 3-6h, 6-12h, 12-24h, 24-48h, 48-72h
      timeline  — full time series (past 24h observed + next 72h forecast) for chart rendering

    Data sources:
      - NOAA SWPC noaa-planetary-k-index-forecast.json (official, 3h resolution)
      - IonShield 1-min Kp trend extrapolation (estimated, 1h, clearly labelled)
    """
    from app.models.forecast import build_forecast

    try:
        result = build_forecast()
        snap = cache_snapshot()
        age = snap["data_age_seconds"]
        result["data_age_seconds"] = age
        result["confidence"] = _confidence(age)
        return result
    except Exception as exc:
        logger.error("Forecast generation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Forecast generation failed. Check server logs.",
        )


@router.get("/api/hf-link")
@limiter.limit(settings.rate_limit)
async def hf_link(
    request: Request,
    lat: float = Query(
        ..., ge=-90, le=90, description="Observer latitude (decimal degrees)"
    ),
    lon: float = Query(
        ..., ge=-180, le=180, description="Observer longitude (decimal degrees)"
    ),
    dest_lat: float | None = Query(
        default=None,
        ge=-90,
        le=90,
        description="Link destination latitude (defaults to lat)",
    ),
    dest_lon: float | None = Query(
        default=None,
        ge=-180,
        le=180,
        description="Link destination longitude (defaults to lon)",
    ),
    _: None = _auth,
):
    """
    HF radio link reliability by frequency band.

    Returns D-layer absorption and reliability estimates for 8 HF bands
    (4–28 MHz), ranked best to worst. Accounts for solar X-ray flare
    enhancement, dayside/nightside D-layer asymmetry, and Polar Cap
    Absorption (PCA) for high-latitude paths (|lat| > 65°).

    Use case: an aviation dispatcher enters the aircraft's current position
    and next waypoint — the endpoint returns a ranked frequency table and
    a plain-English recommendation ("Use 14 MHz. Avoid 5–10 MHz.").
    """
    kp = get_kp()
    logger.info(
        "HF link assessed: origin=(%.3f, %.3f) dest=(%.3f, %.3f) Kp=%.1f",
        lat,
        lon,
        dest_lat if dest_lat is not None else lat,
        dest_lon if dest_lon is not None else lon,
        kp,
    )
    result = compute_hf_link(lat, lon, dest_lat, dest_lon)
    result["confidence"] = _confidence(result["data_age_seconds"])
    return result


@router.get("/overlay/risk.kml")
@limiter.limit(settings.rate_limit)
async def overlay_kml(request: Request, _: None = _auth):
    """ATAK-compatible KML overlay with risk zones and installation placemarks."""
    try:
        kml = generate_kml()
    except Exception as exc:
        logger.error("KML generation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="KML generation failed. Check server logs.",
        )
    return Response(
        content=kml,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/api/locations")
@limiter.limit(settings.rate_limit)
async def api_locations(request: Request, _: None = _auth):
    """All configured locations with their latest risk assessment and alert state."""
    from app.data.locations import get_all, location_count

    locations = get_all()
    return {
        "count": location_count(),
        "locations": locations,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/locations/{loc_id}")
@limiter.limit(settings.rate_limit)
async def api_location_by_id(request: Request, loc_id: str, _: None = _auth):
    """Single configured location with full risk assessment."""
    from app.data.locations import get_by_id

    item = get_by_id(loc_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location '{loc_id}' not found.",
        )
    return item


@router.get("/api/alerts")
@limiter.limit(settings.rate_limit)
async def api_alerts(request: Request, _: None = _auth):
    """Active alerts across all configured locations."""
    from app.data.locations import get_active_alerts, location_count

    alerts = get_active_alerts()
    return {
        "active_count": len(alerts),
        "total_locations": location_count(),
        "alerts": alerts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/overlay/ionshield.cot")
@limiter.limit(settings.rate_limit)
async def overlay_cot(request: Request, _: None = _auth):
    """ATAK/WinTAK CoT XML feed of all configured IonShield locations."""
    from app.data.locations import get_all

    locations = get_all()
    if not locations:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No locations configured. Add a locations.json file.",
        )
    try:
        cot = build_cot_feed(locations, stale_minutes=settings.cot_stale_minutes)
    except Exception as exc:
        logger.error("CoT feed generation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CoT feed generation failed.",
        )
    return Response(
        content=cot,
        media_type="application/xml",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/overlay/risk.geojson")
@limiter.limit(settings.rate_limit)
async def overlay_geojson(request: Request, _: None = _auth):
    """GeoJSON FeatureCollection with risk zones and installation data."""
    try:
        data = generate_geojson()
    except Exception as exc:
        logger.error("GeoJSON generation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GeoJSON generation failed. Check server logs.",
        )
    return data
