"""
IonShield API v2 routes — Decision Engine endpoints.

New in v2:
  GET  /api/v2/comms-decision  — typed HF comms recommendation for a link
  POST /api/v2/route-decision  — typed route risk recommendation with per-waypoint detail

Both endpoints build an EnvironmentSnapshot from the live NOAA cache and pass it
into the stateless DecisionEngine. The engine performs no I/O; all NOAA state is
resolved here before the call.

Auth, rate limiting, and confidence scoring follow the same patterns as routes.py.

Forecast wiring note:
  kp_forecast_24h is populated from the NOAA kp_forecast feed when available.
  When the feed is unavailable, kp_forecast_24h=None is passed and the engine
  uses the current Kp as a conservative fallback. This is labelled clearly in
  the response provenance.feeds_unavailable list.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.auth import _auth
from app.config import settings
from app.data.archiver import (
    count_snapshots,
    get_snapshot_at_or_before,
    get_snapshot_by_id,
    list_snapshots,
    snapshot_row_to_env,
)
from app.data.noaa import (
    cache_snapshot,
    get_bz,
    get_kp,
    get_proton_flux_10mev,
    get_wind_speed,
    get_xray_flux,
)
from app.data.noaa import _cache as _noaa_cache
from app.models.decision import (
    DecisionEngine,
    EnvironmentSnapshot,
    ObservationInput,
    PlatformInput,
    SystemDependencyInput,
    WaypointInput,
)

logger = logging.getLogger(__name__)

router_v2 = APIRouter(prefix="/api/v2")
_limiter = Limiter(key_func=get_remote_address)
_engine = DecisionEngine()


# ── Pydantic request schemas ──────────────────────────────────────────────────


class WaypointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude (decimal degrees)")
    lon: float = Field(..., ge=-180, le=180, description="Longitude (decimal degrees)")
    name: str = Field(default="", description="Optional waypoint label")


class SystemDependencyRequest(BaseModel):
    system_type: str = Field(..., description="HF | SATCOM | UHF | GPS")
    primary_freqs_mhz: list[float] = Field(default_factory=list)
    fallback_modes: list[str] = Field(default_factory=list)
    degradation_tolerance: int = Field(default=3, ge=1, le=5)


class PlatformRequest(BaseModel):
    asset_type: str = Field(
        default="GPS_L1",
        description="GPS_L1 | GPS_L1L2 | GPS_L1L5 | GPS_INS | SBAS",
    )
    system_dependencies: list[SystemDependencyRequest] = Field(default_factory=list)
    criticality: int = Field(
        default=3,
        ge=1,
        le=5,
        description="1=lowest … 5=highest; raises NO-GO threshold for critical platforms",
    )


class RouteDecisionRequest(BaseModel):
    waypoints: list[WaypointRequest] = Field(
        ..., min_length=1, description="Ordered list of route waypoints"
    )
    platform: PlatformRequest = Field(default_factory=PlatformRequest)


class RouteReplayRequest(BaseModel):
    """Body for POST /api/v2/replay/route — same as RouteDecisionRequest + snapshot locator."""

    waypoints: list[WaypointRequest] = Field(
        ..., min_length=1, description="Ordered list of route waypoints"
    )
    platform: PlatformRequest = Field(default_factory=PlatformRequest)
    snapshot_id: int | None = Field(
        default=None,
        description="Replay from this specific snapshot ID",
    )
    at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp — replay from the nearest snapshot at or before "
            "this time. Ignored if snapshot_id is set."
        ),
    )


# ── Environment builder ───────────────────────────────────────────────────────


def _build_env() -> EnvironmentSnapshot:
    """
    Build an EnvironmentSnapshot from the live NOAA cache.

    All NOAA accessors are called here (not inside the decision engine)
    so the engine remains pure and testable.

    kp_forecast_24h: extracted from the kp_forecast cache when available.
    When unavailable, None is passed; the engine treats it as a conservative
    fallback and labels the feed as unavailable in provenance.
    """
    snap = cache_snapshot()
    feed_status: dict[str, str] = snap["fetch_status"]

    feeds_available = [k for k, v in feed_status.items() if v == "ok"]
    feeds_unavailable = [k for k, v in feed_status.items() if v != "ok"]

    kp_val = get_kp()
    bz_val = get_bz()
    xray_val = get_xray_flux()
    proton_val = get_proton_flux_10mev()
    wind_val = get_wind_speed()
    age = snap["data_age_seconds"]
    now_iso = datetime.now(timezone.utc).isoformat()

    observations = [
        ObservationInput("NOAA_SWPC", "kp_index", kp_val, "index", now_iso, age),
        ObservationInput("NOAA_SWPC", "bz_gsm_nt", bz_val, "nT", now_iso, age),
        ObservationInput("NOAA_SWPC", "xray_flux_wm2", xray_val, "W/m²", now_iso, age),
        ObservationInput(
            "NOAA_SWPC", "proton_flux_10mev_pfu", proton_val, "pfu", now_iso, age
        ),
        ObservationInput(
            "NOAA_SWPC", "solar_wind_km_s", wind_val, "km/s", now_iso, age
        ),
    ]

    # Extract 24-hour peak Kp from the forecast feed (conservative: take the max
    # over the next 8 three-hour slots = 24 hours).
    kp_forecast_24h: float | None = None
    kp_forecast_issued_at: str | None = None
    kp_forecast_lead_hours: float | None = None

    forecast_data = _noaa_cache.get("kp_forecast")
    if forecast_data and len(forecast_data) >= 2:
        try:
            # Row 0 is the header; rows 1+ are [time_tag, kp, observed|predicted, noaa_scale]
            rows = forecast_data[1:]
            # Only look at predicted rows within the next 24h
            now_dt = datetime.now(timezone.utc)
            future_kps: list[float] = []
            for row in rows:
                # row is a list: [time_tag, kp, obs_pred, noaa_scale]
                if len(row) < 3:
                    continue
                obs_pred = str(row[2]).strip().lower()
                if obs_pred not in ("predicted", "estimated"):
                    continue
                try:
                    row_dt = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                    delta_h = (row_dt - now_dt).total_seconds() / 3600
                    if 0 <= delta_h <= 24:
                        future_kps.append(float(row[1]))
                        if kp_forecast_issued_at is None:
                            kp_forecast_issued_at = row_dt.isoformat()
                except (ValueError, TypeError):
                    continue
            if future_kps:
                kp_forecast_24h = max(future_kps)
                kp_forecast_lead_hours = 24.0
        except Exception as exc:
            logger.debug("kp_forecast parse failed: %s", exc)

    if kp_forecast_24h is None and "kp_forecast" not in feeds_unavailable:
        feeds_unavailable.append("kp_forecast")

    return EnvironmentSnapshot(
        kp=kp_val,
        bz_nt=bz_val,
        xray_flux=xray_val,
        proton_flux_10mev=proton_val,
        wind_speed_km_s=wind_val,
        data_age_seconds=age,
        feeds_available=feeds_available,
        feeds_unavailable=feeds_unavailable,
        observations=observations,
        kp_forecast_24h=kp_forecast_24h,
        kp_forecast_issued_at=kp_forecast_issued_at,
        kp_forecast_lead_hours=kp_forecast_lead_hours,
    )


def _platform_from_request(req: PlatformRequest) -> PlatformInput:
    deps = [
        SystemDependencyInput(
            system_type=d.system_type,
            primary_freqs_mhz=d.primary_freqs_mhz,
            fallback_modes=d.fallback_modes,
            degradation_tolerance=d.degradation_tolerance,
        )
        for d in req.system_dependencies
    ]
    return PlatformInput(
        asset_type=req.asset_type,
        system_dependencies=deps,
        criticality=req.criticality,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router_v2.get("/comms-decision")
@_limiter.limit(settings.rate_limit)
async def comms_decision(
    request: Request,
    lat: float = Query(
        ..., ge=-90, le=90, description="Observer latitude (decimal degrees)"
    ),
    lon: float = Query(
        ..., ge=-180, le=180, description="Observer longitude (decimal degrees)"
    ),
    dest_lat: float | None = Query(
        default=None, ge=-90, le=90, description="Link destination latitude"
    ),
    dest_lon: float | None = Query(
        default=None, ge=-180, le=180, description="Link destination longitude"
    ),
    _: None = _auth,
):
    """
    Typed HF communications fallback recommendation for a single link.

    Returns a RecommendationObject with:
      - action: one of USE_PRIMARY_HF | USE_ALTERNATE_HF | SWITCH_TO_SATCOM |
                SWITCH_TO_UHF | DEGRADED_MODE | HF_NOT_VIABLE
      - action_sentence: plain-English rationale
      - confidence: score + penalty drivers
      - provenance: input hash for replay verification

    Suitable for: aviation dispatch, ship radio operators, expeditionary comms planners.
    """
    try:
        env = _build_env()
        rec = _engine.comms_fallback(env, lat, lon, dest_lat, dest_lon)
        logger.info(
            "comms-decision: (%.3f, %.3f) → %s (confidence %.2f)",
            lat,
            lon,
            rec.action,
            rec.confidence.score,
        )
        return rec.to_dict()
    except Exception as exc:
        logger.error("comms-decision failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decision engine error. Check server logs.",
        )


@router_v2.post("/route-decision")
@_limiter.limit(settings.rate_limit)
async def route_decision(
    request: Request,
    req: RouteDecisionRequest,
    _: None = _auth,
):
    """
    Typed route risk recommendation with per-waypoint detail.

    Returns a RecommendationObject (route-level GO/ADVISORY/CAUTION/NO-GO)
    plus a waypoints array with per-point GPS error, HF viability, and risk scores.

    Suitable for: mission planners, flight operations, convoy route approval.
    """
    if len(req.waypoints) > settings.max_route_waypoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Route exceeds maximum of {settings.max_route_waypoints} waypoints.",
        )

    try:
        env = _build_env()
        platform = _platform_from_request(req.platform)
        wp_inputs = [WaypointInput(w.lat, w.lon, w.name) for w in req.waypoints]

        rec, wp_decisions = _engine.route_risk(env, wp_inputs, platform)

        logger.info(
            "route-decision: %d waypoints → %s (score %.0f, confidence %.2f)",
            len(wp_inputs),
            rec.action,
            max((w.risk_score for w in wp_decisions), default=0.0),
            rec.confidence.score,
        )

        return {
            **rec.to_dict(),
            "waypoints": [
                {
                    "name": w.name,
                    "lat": w.lat,
                    "lon": w.lon,
                    "risk_level": w.risk_level,
                    "risk_score": w.risk_score,
                    "gps_error_m": w.gps_error_m,
                    "hf_viable": w.hf_viable,
                    "hf_best_freq_mhz": w.hf_best_freq_mhz,
                    "hf_best_reliability_pct": w.hf_best_reliability_pct,
                    "hf_absorption_db": w.hf_absorption_db,
                    "satcom_fade_db": w.satcom_fade_db,
                    "s4_index": w.s4_index,
                    "pca_active": w.pca_active,
                    "watch_notes": w.watch_notes,
                }
                for w in wp_decisions
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("route-decision failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decision engine error. Check server logs.",
        )


# ── Snapshot archive endpoints ────────────────────────────────────────────────


@router_v2.get("/snapshots")
@_limiter.limit(settings.rate_limit)
async def snapshots_list(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
    _: None = _auth,
):
    """
    Paginated list of archived NOAA observation snapshots, most-recent first.

    Each row corresponds to one completed fetch cycle (~5 min cadence).
    Use snapshot IDs with the replay endpoints to reconstruct past decisions.
    """
    try:
        rows, total = (
            await list_snapshots(limit=limit, offset=offset),
            await count_snapshots(),
        )
        return {"count": total, "limit": limit, "offset": offset, "snapshots": rows}
    except Exception as exc:
        logger.error("snapshots list failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error. Check server logs.",
        )


@router_v2.get("/snapshots/{snapshot_id}")
@_limiter.limit(settings.rate_limit)
async def snapshot_detail(
    request: Request,
    snapshot_id: int,
    _: None = _auth,
):
    """Single archived observation snapshot by ID."""
    try:
        row = await get_snapshot_by_id(snapshot_id)
    except Exception as exc:
        logger.error("snapshot fetch failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error. Check server logs.",
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )
    import json

    return {
        "id": row["id"],
        "fetched_at": (
            row["fetched_at"].isoformat()
            if hasattr(row["fetched_at"], "isoformat")
            else str(row["fetched_at"])
        ),
        "fetch_source": row["fetch_source"],
        "kp": row["kp"],
        "bz_nt": row["bz_nt"],
        "xray_flux": row["xray_flux"],
        "proton_flux_10mev": row["proton_flux_10mev"],
        "wind_speed_km_s": row["wind_speed_km_s"],
        "kp_forecast_24h": row["kp_forecast_24h"],
        "feeds_available": json.loads(row["feeds_available"] or "[]"),
        "feeds_unavailable": json.loads(row["feeds_unavailable"] or "[]"),
        "data_age_seconds": row["data_age_seconds"],
    }


# ── Replay helpers ────────────────────────────────────────────────────────────


async def _locate_snapshot(snapshot_id: int | None, at: str | None):
    """
    Resolve a snapshot locator to a DB row.

    Priority:
      1. snapshot_id — exact lookup by primary key
      2. at — nearest snapshot at or before the ISO timestamp
      3. neither — most recent snapshot (equivalent to at=now)

    Raises HTTP 404 if no matching row exists.
    Raises HTTP 422 if `at` is not a valid ISO-8601 string.
    """
    if snapshot_id is not None:
        row = await get_snapshot_by_id(snapshot_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Snapshot {snapshot_id} not found.",
            )
        return row

    if at is not None:
        try:
            at_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid ISO-8601 timestamp: {at!r}",
            )
    else:
        at_dt = datetime.now(timezone.utc)

    row = await get_snapshot_at_or_before(at_dt)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No snapshot found at or before the requested time. "
            "Has the archiver run at least once? Check /api/v2/snapshots.",
        )
    return row


def _replay_meta(row) -> dict:
    """Build the replay metadata block included in replay responses."""
    fetched_at = row["fetched_at"]
    fetched_at_iso = (
        fetched_at.isoformat() if hasattr(fetched_at, "isoformat") else str(fetched_at)
    )
    return {
        "snapshot_id": row["id"],
        "fetched_at": fetched_at_iso,
        "fetch_source": row["fetch_source"],
        "kp_at_snapshot": row["kp"],
        "replay_note": (
            f"Decision reconstructed from archived snapshot id={row['id']} "
            f"(captured {fetched_at_iso}). "
            "The provenance input_hash is deterministic — it matches the hash "
            "of any decision computed with these same geophysical inputs."
        ),
    }


# ── Replay endpoints ──────────────────────────────────────────────────────────


@router_v2.get("/replay")
@_limiter.limit(settings.rate_limit)
async def replay_comms(
    request: Request,
    lat: float = Query(
        ..., ge=-90, le=90, description="Observer latitude (decimal degrees)"
    ),
    lon: float = Query(
        ..., ge=-180, le=180, description="Observer longitude (decimal degrees)"
    ),
    dest_lat: float | None = Query(
        default=None, ge=-90, le=90, description="Link destination latitude"
    ),
    dest_lon: float | None = Query(
        default=None, ge=-180, le=180, description="Link destination longitude"
    ),
    snapshot_id: int | None = Query(
        default=None, description="Replay from this snapshot ID"
    ),
    at: str | None = Query(
        default=None,
        description=(
            "ISO-8601 UTC timestamp — replay from nearest snapshot at or before this time. "
            "Ignored if snapshot_id is set. Defaults to latest snapshot."
        ),
    ),
    _: None = _auth,
):
    """
    Replay a comms-decision using an archived observation snapshot.

    The decision engine is re-run with the stored geophysical state (kp, bz,
    xray, proton, wind). The provenance input_hash is deterministic — it matches
    the hash of any live decision made with identical inputs at the time of
    the original snapshot.

    Use this to:
      - Verify past decisions were made correctly
      - Audit confidence degradation (stale data at replay time shows higher
        data_age_seconds than the original)
      - Run what-if analysis with historical storm data
    """
    try:
        row = await _locate_snapshot(snapshot_id, at)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("replay snapshot lookup failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error during snapshot lookup.",
        )

    try:
        env = snapshot_row_to_env(row)
        rec = _engine.comms_fallback(env, lat, lon, dest_lat, dest_lon)
        logger.info(
            "replay/comms: snapshot=%s → %s (confidence %.2f)",
            row["id"],
            rec.action,
            rec.confidence.score,
        )
        return {**rec.to_dict(), "replay": _replay_meta(row)}
    except Exception as exc:
        logger.error("replay comms-decision failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decision engine error during replay.",
        )


@router_v2.post("/replay/route")
@_limiter.limit(settings.rate_limit)
async def replay_route(
    request: Request,
    req: RouteReplayRequest,
    _: None = _auth,
):
    """
    Replay a route-decision using an archived observation snapshot.

    Accepts the same waypoints/platform body as /api/v2/route-decision, plus
    a snapshot locator (snapshot_id or at). Returns per-waypoint detail plus
    replay provenance metadata.
    """
    if len(req.waypoints) > settings.max_route_waypoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Route exceeds maximum of {settings.max_route_waypoints} waypoints.",
        )

    try:
        row = await _locate_snapshot(req.snapshot_id, req.at)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("replay/route snapshot lookup failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error during snapshot lookup.",
        )

    try:
        env = snapshot_row_to_env(row)
        platform = _platform_from_request(req.platform)
        wp_inputs = [WaypointInput(w.lat, w.lon, w.name) for w in req.waypoints]

        rec, wp_decisions = _engine.route_risk(env, wp_inputs, platform)
        logger.info(
            "replay/route: snapshot=%s %d waypoints → %s (confidence %.2f)",
            row["id"],
            len(wp_inputs),
            rec.action,
            rec.confidence.score,
        )

        return {
            **rec.to_dict(),
            "waypoints": [
                {
                    "name": w.name,
                    "lat": w.lat,
                    "lon": w.lon,
                    "risk_level": w.risk_level,
                    "risk_score": w.risk_score,
                    "gps_error_m": w.gps_error_m,
                    "hf_viable": w.hf_viable,
                    "hf_best_freq_mhz": w.hf_best_freq_mhz,
                    "hf_best_reliability_pct": w.hf_best_reliability_pct,
                    "hf_absorption_db": w.hf_absorption_db,
                    "satcom_fade_db": w.satcom_fade_db,
                    "s4_index": w.s4_index,
                    "pca_active": w.pca_active,
                    "watch_notes": w.watch_notes,
                }
                for w in wp_decisions
            ],
            "replay": _replay_meta(row),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("replay route-decision failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decision engine error during replay.",
        )


# ── Contact / pilot inquiry form ──────────────────────────────────────────────


class PilotInquiryRequest(BaseModel):
    org: str = Field(
        ..., min_length=1, max_length=500, description="Organization or agency name"
    )
    email: str = Field(
        ..., min_length=5, max_length=254, description="Work email address"
    )
    sector: str = Field(
        default="Other", max_length=100, description="Primary operating sector"
    )
    interest: str = Field(
        default="", max_length=4000, description="Mission profile / use case"
    )
    # Honeypot: real users never see or fill this field (hidden via CSS).
    # If it's non-empty a bot filled the form — silently accept, mark spam, skip email.
    website: str = Field(default="", max_length=200, description="Leave blank")

    @field_validator("email")
    @classmethod
    def validate_email_fmt(cls, v: str) -> str:
        import re

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email address")
        return v.lower().strip()


@router_v2.post("/contact", status_code=201)
@_limiter.limit(settings.contact_rate_limit)
async def submit_contact(
    request: Request,
    req: PilotInquiryRequest,
):
    """
    Submit a pilot program inquiry.

    Submissions are persisted to the database regardless of email configuration.
    Email notification is sent via SMTP when `SMTP_HOST` / `SMTP_USERNAME` are set.
    Returns HTTP 201 on success. Honeypot-triggered submissions return 201 silently.
    """
    from datetime import datetime, timezone

    from app.data.contact import (
        mark_email_sent,
        save_inquiry,
        send_inquiry_email,
    )

    client_ip = request.client.host if request.client else "unknown"
    is_spam = bool(req.website)  # honeypot triggered

    try:
        row_id = await save_inquiry(
            org=req.org,
            email=req.email,
            sector=req.sector,
            interest=req.interest,
            client_ip=client_ip,
            status="spam" if is_spam else "new",
        )
        logger.info(
            "pilot inquiry saved: id=%s org=%r spam=%s", row_id, req.org[:40], is_spam
        )

        if not is_spam:
            sent = await send_inquiry_email(
                org=req.org,
                email=req.email,
                sector=req.sector,
                interest=req.interest,
                submitted_at=datetime.now(timezone.utc),
            )
            if sent:
                await mark_email_sent(row_id)
            else:
                logger.info(
                    "SMTP not configured or unavailable — email skipped for id=%s",
                    row_id,
                )

        return {
            "status": "submitted",
            "message": "Thank you for your inquiry. We will be in touch within 1 business day.",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("contact form error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Submission failed. Please try again or email pilots@ionshield.io directly.",
        )


@router_v2.get("/submissions")
@_limiter.limit(settings.rate_limit)
async def list_submissions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: None = _auth,  # requires API key when AUTH is enabled
):
    """
    List pilot inquiry submissions (admin endpoint).

    Requires X-API-Key header when API_KEY is configured.
    ip_hash is excluded from responses; raw IPs are never stored.
    """
    from app.data.contact import count_inquiries, list_inquiries

    try:
        rows = await list_inquiries(limit=limit, offset=offset)
        total = await count_inquiries()
        return {"count": total, "limit": limit, "offset": offset, "submissions": rows}
    except Exception as exc:
        logger.error("list submissions failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error.",
        )
