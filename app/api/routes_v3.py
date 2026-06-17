"""
IonShield API v3 — output layer (A5).

Exposes the data fusion + impact pipeline through clean external endpoints:

  GET /api/v3/health                  — pipeline status (feed availability, age)
  GET /api/v3/risk-map                — fused Region × Time grid (drivers + TEC)
  GET /api/v3/forecast                — Kp 24-hour forecast + storm probability
  GET /api/v3/events                  — recent events (paginated, optional filter)
  GET /api/v3/events/active           — currently OPEN events only
  GET /api/v3/impact                  — per-region impact rows (filterable by system/band)
  GET /api/v3/regions/{region_id}     — single region full impact assessment

Every response is a Pydantic model so the OpenAPI schema is fully typed at
/openapi.json. Endpoints are read-only, idempotent, cacheable, and don't
touch the DB unless required (events, snapshots).

Auth + rate-limit pattern matches routes_v2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.auth import verify_api_key
from app.api.metrics import render as render_metrics
from app.data import feedback_store
from app.models import retrain as retrain_module
from app.models.ml_classifier import CLASSES as ML_CLASSES
from app.data.event_store import list_events
from app.data.fusion import fuse_snapshot
from app.data.instrumentation import snapshot as instr_snapshot
from app.outputs.scenario_export import export_scenario
from app.data.registry import health_snapshot as registry_health
from app.data.noaa import (
    cache_snapshot as noaa_cache_snapshot,
    get_bz,
    get_kp,
    get_proton_flux_10mev,
    get_wind_speed,
    get_xray_flux,
    _cache as _noaa_cache,
)
from app.data.ustec import (
    cache_snapshot as iono_cache_snapshot,
    get_f107_flux,
    get_glotec_featurecollection,
)
from app.models.impact import assess_grid, assess_region
from app.models.ontology import EventType, FusedObservation

logger = logging.getLogger(__name__)

router_v3 = APIRouter(prefix="/api/v3")
_limiter = Limiter(key_func=get_remote_address)


# ── Response schemas ─────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    ok: bool
    fetched_at: str | None
    fetch_source: str | None
    data_age_seconds: int
    noaa_feeds: dict[str, str]
    iono_feeds: dict[str, str]
    glotec_n_features: int
    glotec_time_tag: str | None
    # A6 additions: source registry + latency instrumentation
    sources: dict[str, Any] = {}
    latency: dict[str, Any] = {}


class DriverSnapshot(BaseModel):
    kp_index: float
    bz_nt: float
    wind_speed_km_s: float
    xray_flux_wm2: float
    proton_flux_10mev_pfu: float
    f107_sfu: float
    glotec_median_tecu: float
    glotec_p95_tecu: float
    glotec_max_tecu: float
    fetched_at: str | None


class RegionRiskRow(BaseModel):
    region_id: str
    lat_deg: float
    lon_deg: float
    geomag_lat_deg: float
    is_polar: bool
    is_auroral: bool
    is_equatorial: bool
    tec_tecu: float
    tec_anomaly_tecu: float
    gps_l1_error_m: float
    hf_absorption_db: float
    hf_blackout_probability: float
    satcom_l_fade_db: float
    radar_l_range_bias_m: float


class RiskMapResponse(BaseModel):
    computed_at: str
    n_regions: int
    drivers: DriverSnapshot
    regions: list[RegionRiskRow]


class ForecastEntry(BaseModel):
    time_tag: str
    kp_predicted: float
    severity: str  # G0..G5


class ForecastResponse(BaseModel):
    computed_at: str
    current_kp: float
    peak_kp_24h: float | None
    storm_probability_24h: float
    entries: list[ForecastEntry]


class EventRow(BaseModel):
    id: int
    event_type: str
    state: str
    severity: str
    region_id: str
    t_onset: str
    t_peak: str | None
    t_end: str | None
    driver: str
    peak_value: float | None
    trigger_value: float
    threshold_value: float
    rationale: str
    classifier: str
    confidence: float


class EventsResponse(BaseModel):
    computed_at: str
    total: int
    events: list[EventRow]


class ImpactRow(BaseModel):
    region_id: str
    lat_deg: float
    lon_deg: float
    geomag_lat_deg: float
    when_utc: str
    system: str
    subsystem: str
    metric: str
    value: float


class ImpactResponse(BaseModel):
    computed_at: str
    n_rows: int
    rows: list[ImpactRow]


class RegionDetailResponse(BaseModel):
    region: dict[str, Any]
    when_utc: str
    drivers: DriverSnapshot
    gps: dict[str, dict[str, Any]]
    hf: dict[str, Any]
    satcom: dict[str, dict[str, Any]]
    radar: dict[str, dict[str, Any]]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _kp_to_severity(kp: float) -> str:
    if kp >= 9:
        return "G5"
    if kp >= 8:
        return "G4"
    if kp >= 7:
        return "G3"
    if kp >= 6:
        return "G2"
    if kp >= 5:
        return "G1"
    return "G0"


def _build_drivers() -> DriverSnapshot:
    iono = iono_cache_snapshot()
    noaa = noaa_cache_snapshot()
    return DriverSnapshot(
        kp_index=get_kp(),
        bz_nt=get_bz(),
        wind_speed_km_s=get_wind_speed(),
        xray_flux_wm2=get_xray_flux(),
        proton_flux_10mev_pfu=get_proton_flux_10mev(),
        f107_sfu=get_f107_flux(),
        glotec_median_tecu=iono.get("glotec_median_tecu", 0.0),
        glotec_p95_tecu=iono.get("glotec_p95_tecu", 0.0),
        glotec_max_tecu=iono.get("glotec_max_tecu", 0.0),
        fetched_at=noaa.get("last_fetch"),
    )


def _build_fused_grid() -> list[FusedObservation]:
    iono = iono_cache_snapshot()
    return fuse_snapshot(
        when=None,
        kp=get_kp(),
        bz_nt=get_bz(),
        wind_speed_km_s=get_wind_speed(),
        xray_flux_wm2=get_xray_flux(),
        proton_flux_10mev_pfu=get_proton_flux_10mev(),
        f107_sfu=iono.get("f107_sfu", 70.0),
        glotec_fc=get_glotec_featurecollection(),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router_v3.get("/health", response_model=HealthResponse)
async def health(request: Request, _: None = Depends(verify_api_key)) -> HealthResponse:
    """Pipeline status: which feeds are live, data age, last fetch time."""
    noaa = noaa_cache_snapshot()
    iono = iono_cache_snapshot()
    return HealthResponse(
        ok=noaa.get("data_age_seconds", 9999) < 900,  # < 15 min stale
        fetched_at=noaa.get("last_fetch"),
        fetch_source=noaa.get("fetch_source"),
        data_age_seconds=int(noaa.get("data_age_seconds", 9999)),
        noaa_feeds=noaa.get("fetch_status", {}),
        iono_feeds=iono.get("fetch_status", {}),
        glotec_n_features=int(iono.get("glotec_n_features", 0)),
        glotec_time_tag=iono.get("glotec_time_tag"),
        sources=registry_health(),
        latency=instr_snapshot(),
    )


@router_v3.get("/risk-map", response_model=RiskMapResponse)
async def risk_map(
    request: Request,
    bbox: str | None = Query(
        None,
        description='Optional "min_lat,min_lon,max_lat,max_lon" filter',
        pattern=r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$",
    ),
    _: None = Depends(verify_api_key),
) -> RiskMapResponse:
    """Current fused Region × Time grid with one row per cell.

    Each row carries the global drivers + local TEC + summary impact metrics
    (GPS L1 error, HF total absorption, SATCOM L fade, radar L range bias).
    Use bbox to clip the grid to a geographic region.
    """
    fused = _build_fused_grid()
    impacts = assess_grid(fused)

    bbox_filter = None
    if bbox:
        a, b, c, d = (float(x) for x in bbox.split(","))
        bbox_filter = (min(a, c), min(b, d), max(a, c), max(b, d))

    rows: list[RegionRiskRow] = []
    for ia in impacts:
        r = ia.region
        if bbox_filter:
            if not (bbox_filter[0] <= r.lat_deg <= bbox_filter[2] and bbox_filter[1] <= r.lon_deg <= bbox_filter[3]):
                continue
        obs = next(o for o in fused if o.region.region_id == r.region_id)
        rows.append(
            RegionRiskRow(
                region_id=r.region_id,
                lat_deg=r.lat_deg,
                lon_deg=r.lon_deg,
                geomag_lat_deg=r.geomag_lat_deg,
                is_polar=r.is_polar,
                is_auroral=r.is_auroral,
                is_equatorial=r.is_equatorial,
                tec_tecu=obs.tec_tecu,
                tec_anomaly_tecu=obs.tec_anomaly_tecu,
                gps_l1_error_m=ia.gps["GPS_L1"].error_m,
                hf_absorption_db=ia.hf.absorption_total_db,
                hf_blackout_probability=ia.hf.blackout_probability,
                satcom_l_fade_db=ia.satcom["L"].fade_db,
                radar_l_range_bias_m=ia.radar["L"].range_bias_m,
            )
        )

    return RiskMapResponse(
        computed_at=datetime.now(timezone.utc).isoformat(),
        n_regions=len(rows),
        drivers=_build_drivers(),
        regions=rows,
    )


@router_v3.get("/forecast", response_model=ForecastResponse)
async def forecast(
    request: Request,
    _: None = Depends(verify_api_key),
) -> ForecastResponse:
    """Kp 24-hour forecast from NOAA SWPC, with peak severity + storm probability."""
    forecast_rows = _noaa_cache.get("kp_forecast") or []
    if len(forecast_rows) < 2:
        return ForecastResponse(
            computed_at=datetime.now(timezone.utc).isoformat(),
            current_kp=get_kp(),
            peak_kp_24h=None,
            storm_probability_24h=0.0,
            entries=[],
        )

    rows = forecast_rows[1:]
    now = datetime.now(timezone.utc)
    entries: list[ForecastEntry] = []
    future_kps: list[float] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        obs_pred = str(row[2]).strip().lower()
        if obs_pred not in ("predicted", "estimated"):
            continue
        try:
            t = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            kp_pred = float(row[1])
        except (ValueError, TypeError):
            continue
        delta_h = (t - now).total_seconds() / 3600
        if 0 <= delta_h <= 24:
            future_kps.append(kp_pred)
            entries.append(
                ForecastEntry(
                    time_tag=t.isoformat(),
                    kp_predicted=kp_pred,
                    severity=_kp_to_severity(kp_pred),
                )
            )

    peak = max(future_kps) if future_kps else None
    # Rough storm-probability proxy: fraction of forecast windows ≥ G1
    storm_p = sum(1 for k in future_kps if k >= 5) / len(future_kps) if future_kps else 0.0

    return ForecastResponse(
        computed_at=now.isoformat(),
        current_kp=get_kp(),
        peak_kp_24h=peak,
        storm_probability_24h=round(storm_p, 2),
        entries=entries,
    )


@router_v3.get("/events", response_model=EventsResponse)
async def events_recent(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    only_open: bool = Query(False, description="Filter to non-ENDED events"),
    event_type: str | None = Query(None, description="Filter by EventType value"),
    _: None = Depends(verify_api_key),
) -> EventsResponse:
    """Recent space-weather events from the detector. Newest first."""
    if event_type is not None:
        try:
            EventType(event_type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown event_type: {event_type}",
            )

    rows = await list_events(limit=limit, only_open=only_open)
    if event_type:
        rows = [r for r in rows if r["event_type"] == event_type]

    events = [
        EventRow(
            id=r["id"],
            event_type=r["event_type"],
            state=r["state"],
            severity=r["severity"],
            region_id=r["region_id"],
            t_onset=(r["t_onset"].isoformat() if hasattr(r["t_onset"], "isoformat") else str(r["t_onset"])),
            t_peak=(
                r["t_peak"].isoformat()
                if r["t_peak"] and hasattr(r["t_peak"], "isoformat")
                else (str(r["t_peak"]) if r["t_peak"] else None)
            ),
            t_end=(
                r["t_end"].isoformat()
                if r["t_end"] and hasattr(r["t_end"], "isoformat")
                else (str(r["t_end"]) if r["t_end"] else None)
            ),
            driver=r["driver"],
            peak_value=r["peak_value"],
            trigger_value=r["trigger_value"],
            threshold_value=r["threshold_value"],
            rationale=r["rationale"],
            classifier=r["classifier"],
            confidence=r["confidence"],
        )
        for r in rows
    ]
    return EventsResponse(
        computed_at=datetime.now(timezone.utc).isoformat(),
        total=len(events),
        events=events,
    )


@router_v3.get("/events/active", response_model=EventsResponse)
async def events_active(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    _: None = Depends(verify_api_key),
) -> EventsResponse:
    """Convenience alias: only events whose state is not ENDED."""
    return await events_recent(
        request=request,
        limit=limit,
        only_open=True,
        event_type=None,
    )


@router_v3.get("/impact", response_model=ImpactResponse)
async def impact_grid(
    request: Request,
    system: str | None = Query(
        None,
        description="Filter by system (GPS, HF, SATCOM, RADAR)",
    ),
    subsystem: str | None = Query(
        None,
        description="Filter by subsystem (e.g. GPS_L1, L, Ku, X)",
    ),
    bbox: str | None = Query(
        None,
        pattern=r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$",
    ),
    _: None = Depends(verify_api_key),
) -> ImpactResponse:
    """Per-region per-system impact rows. Defaults to all systems globally."""
    if system and system.upper() not in {"GPS", "HF", "SATCOM", "RADAR"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown system: {system}",
        )

    bbox_filter = None
    if bbox:
        a, b, c, d = (float(x) for x in bbox.split(","))
        bbox_filter = (min(a, c), min(b, d), max(a, c), max(b, d))

    fused = _build_fused_grid()
    impacts = assess_grid(fused)
    out: list[ImpactRow] = []
    for ia in impacts:
        r = ia.region
        if bbox_filter and not (
            bbox_filter[0] <= r.lat_deg <= bbox_filter[2] and bbox_filter[1] <= r.lon_deg <= bbox_filter[3]
        ):
            continue
        for row in ia.to_rows():
            if system and row["system"] != system.upper():
                continue
            if subsystem and row["subsystem"] != subsystem:
                continue
            out.append(
                ImpactRow(
                    region_id=row["region_id"],
                    lat_deg=row["lat_deg"],
                    lon_deg=row["lon_deg"],
                    geomag_lat_deg=row["geomag_lat_deg"],
                    when_utc=row["when_utc"],
                    system=row["system"],
                    subsystem=row["subsystem"],
                    metric=row["metric"],
                    value=float(row["value"]),
                )
            )

    return ImpactResponse(
        computed_at=datetime.now(timezone.utc).isoformat(),
        n_rows=len(out),
        rows=out,
    )


@router_v3.get("/regions/{region_id}", response_model=RegionDetailResponse)
async def region_detail(
    request: Request,
    region_id: str,
    _: None = Depends(verify_api_key),
) -> RegionDetailResponse:
    """Full impact assessment for a single region by its stable region_id."""
    fused = _build_fused_grid()
    obs = next((o for o in fused if o.region.region_id == region_id), None)
    if obs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"region_id not found: {region_id}",
        )
    ia = assess_region(obs)
    return RegionDetailResponse(
        region={
            "region_id": obs.region.region_id,
            "lat_deg": obs.region.lat_deg,
            "lon_deg": obs.region.lon_deg,
            "geomag_lat_deg": obs.region.geomag_lat_deg,
            "is_polar": obs.region.is_polar,
            "is_auroral": obs.region.is_auroral,
            "is_equatorial": obs.region.is_equatorial,
        },
        when_utc=obs.when.isoformat(),
        drivers=_build_drivers(),
        gps={k: v.__dict__ for k, v in ia.gps.items()},
        hf=ia.hf.__dict__,
        satcom={k: v.__dict__ for k, v in ia.satcom.items()},
        radar={k: v.__dict__ for k, v in ia.radar.items()},
    )


@router_v3.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    """
    Prometheus exposition for IonShield internals.

    Returns text/plain version 0.0.4 (the de-facto standard format).
    Intentionally NOT auth-gated by default — this is a scrape target. If
    your deployment is internet-facing, lock it down at the network edge
    (Render private network, Cloudflare access, etc.) rather than on the
    application.
    """
    return PlainTextResponse(
        render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ── A7 — Feedback loop endpoints ─────────────────────────────────────────────


class FeedbackRequest(BaseModel):
    user_feedback: str = Field(
        ...,
        description=(
            "Operator label correction. Must match an EventType value or " "'NOT_AN_EVENT' for false-positive flagging."
        ),
        max_length=64,
    )


class FeedbackResponse(BaseModel):
    sample_id: int
    user_feedback: str
    accepted: bool


class OutcomeRequest(BaseModel):
    system: str = Field(..., max_length=32)
    subsystem: str = Field(..., max_length=64)
    metric: str = Field(..., max_length=64)
    observed_value: float
    observed_at: str = Field(..., description="ISO 8601 UTC timestamp")
    region_id: str | None = Field(None, max_length=32)
    source: str = Field("user", max_length=64)
    notes: str = Field("", max_length=512)


class OutcomeResponse(BaseModel):
    id: int
    accepted: bool


class DriftResponse(BaseModel):
    n: int
    agreement: float | None
    mean_confidence: float | None
    by_class: dict[str, dict[str, int]]


class ModelVersionRow(BaseModel):
    id: int
    version: str
    trained_at: str
    n_train: int
    n_real_samples: int
    train_accuracy: float | None
    artifact_path: str
    notes: str
    active: bool


class RetrainResponse(BaseModel):
    status: str
    version: str | None = None
    n_train: int | None = None
    n_real_samples: int | None = None
    n_user_corrected: int | None = None
    train_accuracy: float | None = None
    validation_accuracy: float | None = None
    min_validation: float | None = None
    artifact_path: str | None = None
    reason: str | None = None


_VALID_FEEDBACK = set(c for c in ML_CLASSES) | {"NOT_AN_EVENT"}


@router_v3.post("/training/samples/{sample_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    sample_id: int,
    body: FeedbackRequest,
    request: Request,
    _: None = Depends(verify_api_key),
) -> FeedbackResponse:
    """
    Attach an operator label correction to a training sample.

    Valid `user_feedback` values: any EventType class name plus the literal
    `NOT_AN_EVENT` for false-positive flagging.
    """
    if body.user_feedback not in _VALID_FEEDBACK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"user_feedback must be one of {sorted(_VALID_FEEDBACK)}",
        )
    ok = await feedback_store.attach_feedback(sample_id, body.user_feedback)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sample_id {sample_id} not found",
        )
    return FeedbackResponse(
        sample_id=sample_id,
        user_feedback=body.user_feedback,
        accepted=True,
    )


@router_v3.post("/outcomes", status_code=201, response_model=OutcomeResponse)
async def submit_outcome(
    body: OutcomeRequest,
    request: Request,
    _: None = Depends(verify_api_key),
) -> OutcomeResponse:
    """
    Submit observed ground truth (real GPS error from a receiver, observed
    HF link availability, etc.) — the feedback loop's primary input.
    """
    try:
        observed_at = datetime.fromisoformat(body.observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bad observed_at: {exc}",
        )
    rid = await feedback_store.record_outcome(
        system=body.system,
        subsystem=body.subsystem,
        metric=body.metric,
        observed_value=body.observed_value,
        observed_at=observed_at,
        region_id=body.region_id,
        source=body.source,
        notes=body.notes,
    )
    if rid is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist outcome",
        )
    return OutcomeResponse(id=rid, accepted=True)


@router_v3.get("/training/samples")
async def list_training_samples(
    limit: int = Query(100, ge=1, le=1000),
    only_with_feedback: bool = Query(False),
    request: Request = None,
    _: None = Depends(verify_api_key),
) -> dict[str, Any]:
    rows = await feedback_store.list_samples(
        limit=limit,
        only_with_feedback=only_with_feedback,
    )
    total = await feedback_store.count_samples(only_with_feedback=only_with_feedback)
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "n": len(rows),
        "samples": [
            {
                "id": r["id"],
                "created_at": (
                    r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"])
                ),
                "region_id": r["region_id"],
                "rule_label": r["rule_label"],
                "ml_label": r["ml_label"],
                "ml_confidence": r["ml_confidence"],
                "user_feedback": r["user_feedback"],
                "outcome_label": r["outcome_label"],
                "event_id": r["event_id"],
            }
            for r in rows
        ],
    }


@router_v3.get("/training/drift", response_model=DriftResponse)
async def training_drift(
    window: int = Query(500, ge=10, le=5000),
    request: Request = None,
    _: None = Depends(verify_api_key),
) -> DriftResponse:
    """
    Predicted-vs-rule divergence over the latest `window` samples.

    `agreement` is the fraction where ml_label == rule_label. Persistent
    drops below ~0.85 typically indicate that the ML classifier is going
    stale relative to the rules — time to retrain.
    """
    return DriftResponse(**await feedback_store.drift_metrics(window=window))


@router_v3.get("/training/models", response_model=list[ModelVersionRow])
async def training_models(
    limit: int = Query(20, ge=1, le=100),
    request: Request = None,
    _: None = Depends(verify_api_key),
) -> list[ModelVersionRow]:
    rows = await feedback_store.list_model_versions(limit=limit)
    return [
        ModelVersionRow(
            id=r["id"],
            version=r["version"],
            trained_at=(r["trained_at"].isoformat() if hasattr(r["trained_at"], "isoformat") else str(r["trained_at"])),
            n_train=r["n_train"],
            n_real_samples=r["n_real_samples"],
            train_accuracy=r["train_accuracy"],
            artifact_path=r["artifact_path"],
            notes=r["notes"],
            active=bool(r["active"]),
        )
        for r in rows
    ]


@router_v3.post("/training/retrain", response_model=RetrainResponse)
async def trigger_retrain(
    notes: str = Query("", max_length=200),
    request: Request = None,
    _: None = Depends(verify_api_key),
) -> RetrainResponse:
    """
    Run the retrain pipeline and atomically swap the live classifier on
    success. Rejects the swap if the validation accuracy on held-out real
    samples falls below the safety threshold.
    """
    result = await retrain_module.retrain_and_maybe_swap(notes=notes)
    return RetrainResponse(**result)


# ── Champion / challenger + auto-pilot ───────────────────────────────────────


class ShadowMetricsResponse(BaseModel):
    n: int
    champion_agreement: float | None
    challenger_agreement: float | None
    advantage: float | None


class PromoteResponse(BaseModel):
    promoted: bool
    version: str | None = None
    reason: str | None = None
    advantage: float | None = None
    n: int | None = None


@router_v3.get("/training/shadow", response_model=ShadowMetricsResponse)
async def shadow_metrics(
    window: int = Query(200, ge=10, le=5000),
    request: Request = None,
    _: None = Depends(verify_api_key),
) -> ShadowMetricsResponse:
    """Champion vs challenger agreement on the latest `window` samples."""
    return ShadowMetricsResponse(**await feedback_store.shadow_metrics(window=window))


@router_v3.post("/training/promote", response_model=PromoteResponse)
async def promote_challenger(
    request: Request,
    _: None = Depends(verify_api_key),
) -> PromoteResponse:
    """Force-promote the current challenger to active. No safety checks."""
    chal = await feedback_store.challenger_model_version()
    if chal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No challenger registered",
        )
    from pathlib import Path
    from app.models import ml_classifier as mlc

    p = Path(chal["artifact_path"])
    if not p.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Challenger artifact missing",
        )
    mlc.ARTIFACT_PATH.write_text(p.read_text())
    mlc.invalidate_classifier()
    ok = await feedback_store.promote_challenger(chal["version"])
    return PromoteResponse(
        promoted=ok,
        version=chal["version"],
        reason="ok" if ok else "db_update_failed",
    )


@router_v3.post("/training/retire-challenger", response_model=PromoteResponse)
async def retire_challenger(
    request: Request,
    _: None = Depends(verify_api_key),
) -> PromoteResponse:
    """Drop the current challenger flag. The active model is unchanged."""
    chal = await feedback_store.challenger_model_version()
    ok = await feedback_store.retire_challenger()
    return PromoteResponse(
        promoted=False,
        version=(chal["version"] if chal else None),
        reason="retired" if ok else "no_challenger",
    )


@router_v3.post("/training/auto-pilot/run-once")
async def autopilot_run_once(
    request: Request,
    _: None = Depends(verify_api_key),
) -> dict[str, Any]:
    """
    Execute one auto-pilot pass on demand: drift-retrain check, auto-promote
    check, sample-archive check. Useful for manual smoke tests + scripts.
    """
    from app.models import auto_pilot

    return {
        "retrain": await auto_pilot.auto_retrain_tick(),
        "promote": await auto_pilot.auto_promote_tick(),
        "archive": await auto_pilot.archive_tick(),
    }


# ── B1: Scenario export ──────────────────────────────────────────────────────


@router_v3.get("/scenarios/export")
async def scenario_export(
    request: Request,
    start: str = Query(..., description="ISO 8601 UTC start time"),
    end: str = Query(..., description="ISO 8601 UTC end time"),
    fmt: str = Query(
        "geojson",
        pattern=r"^(geojson|csv|kml|kmz|keyframes)$",
        description="Output format: geojson | csv | kml | kmz | keyframes (Earth Studio CSV)",
    ),
    layer: str = Query(
        "hf",
        pattern=r"^(hf|gps|sat)$",
        description="KML coloring driver — hf | gps | sat (KML format only)",
    ),
    keyframe_region: str | None = Query(
        None,
        description="Restrict keyframe CSV to a single region_id (camera-POI export)",
    ),
    step_seconds: int = Query(
        0,
        ge=0,
        le=86400,
        description="Downsample to one snapshot every N seconds. 0 = keep all.",
    ),
    region_filter: str | None = Query(
        None,
        description='Comma-separated region_id filter, e.g. "R+035-090,R+035-070"',
    ),
    max_snapshots: int = Query(500, ge=1, le=2000),
    geometry: str = Query(
        "polygon",
        pattern=r"^(polygon|point)$",
        description="GeoJSON geometry type per feature",
    ),
    _: None = Depends(verify_api_key),
):
    """
    Export a historical scenario as a time-indexed GeoJSON FeatureCollection
    or CSV. Replays noaa_snapshots in [start, end], fuses each tick onto the
    324-cell grid, runs impact models, and emits per-region per-time rows.

    Output is the input to B2 (KML conversion) and B5 (Simulation Mode UI).
    """
    try:
        t_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bad start/end: {exc}",
        )
    if t_end < t_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must be >= start",
        )

    regions = [r.strip() for r in region_filter.split(",") if r.strip()] if region_filter else None

    # KML / KMZ / keyframes always need a GeoJSON intermediate; the converters
    # in app.outputs.earth_studio operate on the same FeatureCollection that
    # fmt=geojson would emit, then translate to the wire format.
    if fmt in ("kml", "kmz", "keyframes"):
        gj_payload, meta = await export_scenario(
            start=t_start,
            end=t_end,
            fmt="geojson",
            step_seconds=step_seconds,
            region_filter=regions,
            max_snapshots=max_snapshots,
            geometry="polygon",
        )
    else:
        payload, meta = await export_scenario(
            start=t_start,
            end=t_end,
            fmt=fmt,
            step_seconds=step_seconds,
            region_filter=regions,
            max_snapshots=max_snapshots,
            geometry=geometry,
        )

    snap_header = {"X-IonShield-Snapshots": str(meta["downsampled_count"])}

    if fmt == "csv":
        return Response(content=payload, media_type="text/csv", headers=snap_header)

    if fmt == "kml":
        from app.outputs.earth_studio import geojson_to_kml

        return Response(
            content=geojson_to_kml(gj_payload, layer_by=layer),
            media_type="application/vnd.google-earth.kml+xml",
            headers={**snap_header, "Content-Disposition": 'attachment; filename="ionshield-scenario.kml"'},
        )

    if fmt == "kmz":
        from app.outputs.earth_studio import geojson_to_kmz

        return Response(
            content=geojson_to_kmz(gj_payload),
            media_type="application/vnd.google-earth.kmz",
            headers={**snap_header, "Content-Disposition": 'attachment; filename="ionshield-scenario.kmz"'},
        )

    if fmt == "keyframes":
        from app.outputs.earth_studio import geojson_to_keyframes_csv

        return Response(
            content=geojson_to_keyframes_csv(gj_payload, region_id=keyframe_region),
            media_type="text/csv",
            headers={**snap_header, "Content-Disposition": 'attachment; filename="ionshield-keyframes.csv"'},
        )

    return JSONResponse(content=payload, headers=snap_header)


# ── B5: Simulation Mode catalog ──────────────────────────────────────────────


_PRECOMPUTED_FILE_KEYS = {
    "geojson_url": "scenario.geojson",
    "kmz_url": "scenario.kmz",
    "keyframes_url": "keyframes.csv",
}


def _attach_cache_bust(scenarios: list[dict]) -> None:
    """
    Mutate each scenario's `precomputed.*_url` to include `?v=<hash>` from
    the per-scenario manifest. Browsers cached on stale assets see the new
    query suffix and re-fetch — caveat 3 fix.
    """
    from app.data import scenario_precompute as sp

    for sc in scenarios:
        pc = sc.get("precomputed") or {}
        sid = sc.get("id")
        if not pc or not sid:
            continue
        manifest = sp.load_manifest(sid)
        if not manifest:
            continue
        files = manifest.get("files", {})
        for url_key, file_key in _PRECOMPUTED_FILE_KEYS.items():
            entry = files.get(file_key) or {}
            h = entry.get("hash")
            if h and pc.get(url_key):
                pc[url_key] = f"{pc[url_key]}?v={h}"
        # expose the manifest for advanced clients (Earth Studio operators
        # like to verify they're working from the same vintage as the demo)
        sc["precomputed"] = pc
        sc["precomputed_manifest"] = {
            "computed_at": manifest.get("computed_at"),
            "n_features": manifest.get("n_features"),
            "n_snapshots": manifest.get("n_snapshots"),
        }


@router_v3.get("/scenarios", include_in_schema=True)
async def scenarios_catalog(request: Request) -> dict:
    """
    Return the pre-defined scenario catalog backing the Simulation Mode UI.

    Reads from app/static/scenarios.json so non-engineers can edit the
    catalog without a code change. Falls back to an empty list if the file
    is missing. Each precomputed-URL is automatically suffixed with `?v=<hash>`
    when a per-scenario manifest exists, so browsers re-fetch after a
    precompute regeneration without manual cache-clearing.
    """
    import json
    from pathlib import Path

    p = Path(__file__).parent.parent / "static" / "scenarios.json"
    if not p.exists():
        return {"scenarios": []}
    try:
        catalog = json.loads(p.read_text())
    except Exception as exc:
        logger.warning("scenarios catalog parse failed: %s", exc)
        return {"scenarios": []}
    _attach_cache_bust(catalog.get("scenarios", []))
    await _attach_video_registrations(catalog.get("scenarios", []))
    return catalog


# ── B5 caveat fix: historical-storm backfill ─────────────────────────────────


@router_v3.post("/scenarios/backfill")
async def scenarios_backfill(
    request: Request,
    profile_id: str | None = Query(
        None,
        description=(
            "Backfill only this scenario (e.g. 'may-2024-g5'). "
            "Omit to backfill every predefined storm with a concrete window."
        ),
    ),
    _: None = Depends(verify_api_key),
) -> dict[str, Any]:
    """
    Pull historical Kp from GFZ Potsdam and seed `noaa_snapshots` so the
    Simulation-Mode storm cards have data. Idempotent — re-running is a
    no-op for already-backfilled windows.
    """
    from app.data import historical_backfill as hb

    if profile_id is None:
        results = await hb.backfill_all_predefined()
    else:
        if profile_id not in hb.STORM_PROFILES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown profile_id: {profile_id}. " f"Known: {sorted(hb.STORM_PROFILES.keys())}",
            )
        # Pull window from the catalog
        from pathlib import Path
        import json

        catalog = json.loads((Path(__file__).parent.parent / "static" / "scenarios.json").read_text())
        match = next(
            (s for s in catalog["scenarios"] if s["id"] == profile_id),
            None,
        )
        if not match:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"profile_id {profile_id} not in scenarios.json",
            )
        t0 = datetime.fromisoformat(match["start"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(match["end"].replace("Z", "+00:00"))
        results = [await hb.backfill_storm(profile_id, t0, t1)]

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


# ── B3: Scenario precompute ──────────────────────────────────────────────────


@router_v3.post("/scenarios/precompute")
async def scenarios_precompute(
    request: Request,
    only_id: str | None = Query(
        None,
        description="Limit precompute to a single scenario id",
    ),
    _: None = Depends(verify_api_key),
) -> dict[str, Any]:
    """
    Generate the GeoJSON + KMZ + keyframe CSV artifacts for every concrete
    scenario in the catalog and write them under `app/static/scenarios/<id>/`.

    Idempotent — overwrites existing files. Run after a backfill to refresh
    the served assets, or after changing scenarios.json windows.
    """
    from app.data import scenario_precompute as sp

    results = await sp.precompute_all(only_id=only_id)
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "scenarios_processed": len(results),
        "results": results,
    }


# ── B4: Earth Studio export workflow ─────────────────────────────────────────


_VIDEO_SIDECAR_NAME = "video.json"


class VideoRegistration(BaseModel):
    video_url: str = Field(..., description="Public URL or /static/... path to the rendered mp4")
    duration_seconds: float | None = None
    rendered_at: str | None = None
    notes: str = Field("", max_length=512)


def _video_sidecar_path(scenario_id: str):
    from app.data import scenario_precompute as sp

    return sp.OUTPUT_ROOT / scenario_id / _VIDEO_SIDECAR_NAME


def _load_video_sidecar(scenario_id: str) -> dict | None:
    """Read the per-scenario video.json sidecar if it exists."""
    import json

    p = _video_sidecar_path(scenario_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


async def _attach_video_registrations(scenarios: list[dict]) -> None:
    """
    Merge video registrations into catalog rows in-place. SQL is the
    canonical store (survives ephemeral disks); the sidecar JSON files
    are read as a read-through cache for instances that have lost DB
    access. SQL wins on conflict.
    """
    db_rows: dict[str, dict] = {}
    try:
        from app.data import scenario_video_store as svs

        db_rows = await svs.lookup_all()
    except Exception as exc:
        logger.debug("video_store lookup_all failed: %s", exc)

    for sc in scenarios:
        sid = sc.get("id")
        if not sid:
            continue
        row = db_rows.get(sid) or _load_video_sidecar(sid)
        if row and row.get("video_url"):
            sc["video_url"] = row["video_url"]
            sc["video_meta"] = {k: row[k] for k in ("duration_seconds", "rendered_at", "notes") if k in row}


@router_v3.get("/scenarios/{scenario_id}/recipe")
async def scenario_recipe(
    scenario_id: str,
    request: Request,
    lint: bool = Query(
        False,
        description="If true, also return any structural issues with the recipe",
    ),
    _: None = Depends(verify_api_key),
) -> dict:
    """
    Return the per-scenario Earth Studio recipe — camera path, duration,
    frame rate, render settings — that the operator runbook references.

    Pass `lint=1` to also receive a `recipe_issues: list[str]` of
    structural problems (out-of-bounds lat/lon, non-monotonic timestamps,
    etc.) so the operator can fix the catalog before rendering.
    """
    import json
    from pathlib import Path

    p = Path(__file__).parent.parent / "static" / "scenarios.json"
    catalog = json.loads(p.read_text())
    sc = next((s for s in catalog["scenarios"] if s["id"] == scenario_id), None)
    if sc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scenario_id {scenario_id} not in catalog",
        )
    if "recipe" not in sc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scenario {scenario_id} has no recipe (live-only?)",
        )
    out: dict = {
        "scenario_id": scenario_id,
        "title": sc.get("title"),
        "recipe": sc["recipe"],
        "downloads": sc.get("precomputed") or {},
    }
    if lint:
        from app.outputs.earth_studio_recipe import validate_recipe

        out["recipe_issues"] = validate_recipe(sc["recipe"])
    return out


@router_v3.post("/scenarios/{scenario_id}/video")
async def register_scenario_video(
    scenario_id: str,
    body: VideoRegistration,
    request: Request,
    _: None = Depends(verify_api_key),
) -> dict:
    """
    Register a rendered Earth Studio mp4 with a scenario.

    Persists to SQL (survives ephemeral filesystems on free-tier deploys)
    AND writes a `video.json` sidecar next to the precomputed artifacts
    (so static-asset consumers still see the registration). URL is
    validated against the optional IONSHIELD_VIDEO_DOMAIN_ALLOWLIST.

    Source-controlled scenarios.json is never mutated.
    """
    import json
    from datetime import datetime as _dt
    from app.data import scenario_video_store as svs

    # Confirm the scenario exists in the catalog
    from pathlib import Path

    catalog_path = Path(__file__).parent.parent / "static" / "scenarios.json"
    catalog = json.loads(catalog_path.read_text())
    if not any(s["id"] == scenario_id for s in catalog["scenarios"]):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scenario_id {scenario_id} not in catalog",
        )

    rendered_at = None
    if body.rendered_at:
        try:
            rendered_at = _dt.fromisoformat(body.rendered_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"bad rendered_at: {exc}",
            )

    try:
        row = await svs.register(
            scenario_id,
            video_url=body.video_url,
            duration_seconds=body.duration_seconds,
            rendered_at=rendered_at,
            notes=body.notes,
        )
    except svs.InvalidVideoURL as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Best-effort write-through to the sidecar file (lets static-asset
    # consumers without DB access still see the registration). Failures
    # are logged but don't abort the registration.
    sidecar_path = _video_sidecar_path(scenario_id)
    try:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(row, indent=2))
    except Exception as exc:
        logger.warning("Sidecar write failed for %s: %s", scenario_id, exc)

    return {
        "scenario_id": scenario_id,
        "registered": True,
        "sidecar_path": str(sidecar_path),
        "video": row,
    }


@router_v3.delete("/scenarios/{scenario_id}/video")
async def unregister_scenario_video(
    scenario_id: str,
    request: Request,
    _: None = Depends(verify_api_key),
) -> dict:
    """Remove a previously-registered video. 404 if none was set."""
    from app.data import scenario_video_store as svs

    removed = await svs.unregister(scenario_id)
    sidecar = _video_sidecar_path(scenario_id)
    if sidecar.exists():
        try:
            sidecar.unlink()
            removed = True
        except Exception as exc:
            logger.warning("Sidecar delete failed for %s: %s", scenario_id, exc)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no video registered for this scenario",
        )
    return {"scenario_id": scenario_id, "removed": True}


# ── B6: Customer profiles + per-customer scenarios ───────────────────────────


@router_v3.get("/customers")
async def customers_catalog(request: Request) -> dict:
    """List the available customer profiles (defense / aerospace / commercial)."""
    from app.data import customer_profile as cp

    return {"customers": cp.list_profiles()}


@router_v3.get("/customers/{customer_id}")
async def customer_detail(
    customer_id: str,
    request: Request,
) -> dict:
    """Single customer profile + the derived scenarios it would produce."""
    from app.data import customer_profile as cp

    profile = cp.get_profile(customer_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"customer_id {customer_id} not found",
        )
    import json
    from pathlib import Path

    catalog = json.loads((Path(__file__).parent.parent / "static" / "scenarios.json").read_text())
    derived = cp.derive_scenarios(catalog["scenarios"], customer_id)
    return {"profile": profile, "derived_scenarios": derived}


@router_v3.get("/scenarios/customer/{customer_id}")
async def scenarios_for_customer(
    customer_id: str,
    request: Request,
) -> dict:
    """
    Catalog response with every concrete scenario passed through the
    customer profile. The simulation page uses this when a customer is
    selected so the user sees branded titles, region filters, and
    correctly cache-busted precomputed URLs (with the customer suffix).
    """
    from app.data import customer_profile as cp

    profile = cp.get_profile(customer_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"customer_id {customer_id} not found",
        )
    import json
    from pathlib import Path

    catalog = json.loads((Path(__file__).parent.parent / "static" / "scenarios.json").read_text())
    derived = cp.derive_scenarios(catalog["scenarios"], customer_id)
    # Apply existing cache-bust + video sidecars so the customer-scoped
    # response gets the same niceties as the base catalog.
    _attach_cache_bust(derived)
    await _attach_video_registrations(derived)
    return {"profile": profile, "scenarios": derived}


# ── Foundry admin: force schema (re)apply ────────────────────────────────────


@router_v3.post("/foundry/reset")
async def foundry_reset(request: Request) -> dict:
    """
    Force the next push to each Foundry dataset to be a SNAPSHOT instead of
    APPEND. Used when migrating away from legacy JSONL files: clears the
    `_SNAPSHOTTED` memo so the next sync_snapshot / sync_rows call wipes the
    dataset and writes a single fresh Parquet file. Subsequent pushes append.

    Useful one-shot after deploying the JSONL→Parquet switchover.
    """
    from app.data import foundry_sync

    before = list(foundry_sync._SNAPSHOTTED)
    foundry_sync._SNAPSHOTTED.clear()
    return {
        "ok": True,
        "cleared": before,
        "note": "Next push to each dataset will be a SNAPSHOT (replaces all files).",
    }


@router_v3.post("/foundry/apply-schema")
async def foundry_apply_schema(request: Request) -> dict:
    """
    Force-apply Foundry schemas to all configured datasets *now*.

    Useful after the very first deploy: the metadata-service endpoint shape
    varies across Foundry versions, so the sync-time attempt may have logged
    "all endpoints failed". Hit this admin route to retry on-demand and see
    which endpoint shape worked. Returns one row per configured dataset.
    """
    from app.config import settings
    from app.data import foundry_sync
    from app.data.noaa import cache_snapshot as noaa_cache_snapshot
    from app.data.ustec import cache_snapshot as iono_cache_snapshot

    if not settings.foundry_sync_enabled:
        return {"applied": [], "skipped_reason": "foundry_sync_disabled"}
    token = settings.foundry_token.get_secret_value() if settings.foundry_token else ""
    if not token or not settings.foundry_stack_url:
        return {"applied": [], "skipped_reason": "missing_foundry_config"}

    sample_snapshot = foundry_sync.build_snapshot_payload(
        noaa_cache_snapshot(),
        iono_cache_snapshot(),
    )

    targets = [
        ("space_weather_raw", settings.foundry_space_weather_raw_rid, sample_snapshot),
        ("location_risk", settings.foundry_location_risk_rid, sample_snapshot),
        ("events", settings.foundry_events_rid, sample_snapshot),
        ("impact", settings.foundry_impact_rid, sample_snapshot),
    ]

    import httpx

    results = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for label, rid, sample in targets:
            if not rid:
                results.append({"dataset": label, "rid": None, "ok": False, "reason": "no_rid"})
                continue
            ok = await foundry_sync._apply_schema(
                client,
                settings.foundry_stack_url,
                rid,
                token,
                sample,
            )
            if ok:
                foundry_sync._SCHEMA_APPLIED.add(rid)
            results.append({"dataset": label, "rid": rid, "ok": ok})
    return {"applied": results}


# ── Phase 1: API key admin (bootstrap via IONSHIELD_ADMIN_BEARER) ────────────


def _require_admin(request: Request) -> None:
    """Bootstrap admin guard. Checks Authorization: Bearer <admin_bearer>."""
    from app.config import settings

    if not settings.admin_bearer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin endpoint disabled. Set IONSHIELD_ADMIN_BEARER to enable.",
        )
    raw = request.headers.get("Authorization", "")
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if token != settings.admin_bearer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin bearer required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router_v3.post("/admin/keys", status_code=201)
async def admin_mint_key(request: Request, body: dict) -> dict:
    """
    Mint a new per-tenant API key. Requires Authorization: Bearer <admin_bearer>.

    Body: {"tenant_id": "acme-corp", "label": "ops dashboard", "scopes": "read"}
    Response includes plaintext exactly once — store it securely; we cannot
    show it again.
    """
    _require_admin(request)
    from app.data import api_keys

    tenant_id = (body or {}).get("tenant_id", "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    return await api_keys.mint_key(
        tenant_id=tenant_id,
        label=(body or {}).get("label", ""),
        scopes=(body or {}).get("scopes", "read"),
    )


@router_v3.get("/admin/keys")
async def admin_list_keys(request: Request, tenant_id: str | None = None) -> dict:
    _require_admin(request)
    from app.data import api_keys

    return {"keys": await api_keys.list_keys(tenant_id=tenant_id)}


@router_v3.delete("/admin/keys/{key_id}")
async def admin_revoke_key(request: Request, key_id: int) -> dict:
    _require_admin(request)
    from app.data import api_keys

    revoked = await api_keys.revoke_key(key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="key not found or already revoked")
    return {"revoked": True, "id": key_id}


@router_v3.get("/admin/audit")
async def admin_audit(request: Request, limit: int = 100, tenant_id: str | None = None) -> dict:
    _require_admin(request)
    from app.data import audit_log

    return {"entries": await audit_log.recent(limit=limit, tenant_id=tenant_id)}


# ── Phase 2: 24h Kp forecaster ──────────────────────────────────────────────


class KpForecastEntry(BaseModel):
    horizon_h: int
    valid_at: str
    kp_predicted: float
    severity: str  # G0..G5


class KpForecastResponse(BaseModel):
    computed_at: str
    current_kp: float
    horizons_h: list[int]
    entries: list[KpForecastEntry]
    model_version: int
    trained_at: str | None
    n_train_samples: int
    training_source: str
    rmse_per_horizon: list[float]
    note: str | None = None


@router_v3.get("/forecast/kp", response_model=KpForecastResponse)
async def forecast_kp(request: Request, _: None = Depends(verify_api_key)) -> KpForecastResponse:
    """
    Phase 2 — 24-hour Kp forecast from a multi-horizon ridge model trained on
    NASA OMNI historical archive + live noaa_snapshots. Returns predictions
    at +1h, +3h, +6h, +12h, +24h with severity buckets.
    """
    from app.models import kp_forecaster as kpf

    artifact = kpf.load()
    note: str | None = None
    if artifact is None:
        # Cold-start: train on whatever we have right now (synth fallback if empty)
        artifact = await kpf.train_from_db()
        note = "Model just bootstrapped; will improve with more data over time."

    feats = await kpf.build_live_features()
    if feats is None:
        # No history yet — degrade gracefully to "current Kp persists"
        kp_now = float(get_kp())
        feats = [kp_now, 0.0, 400.0] * len(kpf.LAG_OFFSETS_H) + [kp_now, kp_now, 400.0, 0.0]
        note = (note or "") + " Insufficient history; using persistence fallback."

    preds = kpf.predict(feats, artifact)
    now = datetime.now(timezone.utc)
    entries = []
    for h in artifact["horizons_h"]:
        kp = preds[f"h{h}"]
        valid = now + timedelta(hours=h)
        entries.append(
            KpForecastEntry(
                horizon_h=h,
                valid_at=valid.isoformat(),
                kp_predicted=kp,
                severity=kpf.kp_to_severity(kp),
            )
        )

    return KpForecastResponse(
        computed_at=now.isoformat(),
        current_kp=float(get_kp()),
        horizons_h=artifact["horizons_h"],
        entries=entries,
        model_version=artifact.get("version", 1),
        trained_at=artifact.get("trained_at"),
        n_train_samples=artifact.get("n_train_real", 0),
        training_source=artifact.get("training_source", "unknown"),
        rmse_per_horizon=artifact.get("metrics", {}).get("rmse_per_horizon", []),
        note=note,
    )


@router_v3.post("/forecast/kp/retrain")
async def retrain_kp_forecaster(request: Request) -> dict:
    """Force a retrain (admin-guarded). Returns new artifact metadata."""
    _require_admin(request)
    from app.models import kp_forecaster as kpf

    artifact = await kpf.train_from_db()
    return {
        "trained_at": artifact["trained_at"],
        "n_train_real": artifact["n_train_real"],
        "n_train_total": artifact["n_train_total"],
        "training_source": artifact["training_source"],
        "rmse_per_horizon": artifact["metrics"]["rmse_per_horizon"],
        "mae_per_horizon": artifact["metrics"]["mae_per_horizon"],
    }


# ── Phase 3b: Foundry Workshop pack ─────────────────────────────────────────


@router_v3.get("/foundry/pack")
async def foundry_pack(request: Request) -> dict:
    """
    Returns the Foundry-readiness pack as JSON: ontology object types,
    sample SQL queries, and a Workshop module layout. A Foundry admin
    imports these via Ontology Manager + Workshop UI to stand up an
    IonShield app inside their tenant.
    """
    from app.outputs import foundry_pack as fp

    return fp.build_pack()


@router_v3.get("/foundry/ontology")
async def foundry_ontology(request: Request) -> dict:
    """Just the ontology object definitions (subset of /foundry/pack)."""
    from app.outputs import foundry_pack as fp

    return {"objects": fp.ontology_objects()}


@router_v3.get("/foundry/sql")
async def foundry_sql_samples(request: Request) -> dict:
    """Sample SQL queries to paste into Foundry's SQL console."""
    from app.outputs import foundry_pack as fp

    return {"queries": fp.sample_sql_queries()}


# ── Mission Planner (Stage 2) ────────────────────────────────────────────────


from pydantic import BaseModel as _MissionBase, Field as _MissionField  # noqa: E402


class _MissionWaypointReq(_MissionBase):
    name: str = "WP"
    lat: float = _MissionField(..., ge=-90, le=90)
    lon: float = _MissionField(..., ge=-180, le=180)


class _MissionRequestModel(_MissionBase):
    mission_type: str = _MissionField(
        "uav",
        description=(
            "uav | bvlos | precision-ag | maritime | defense-patrol | surveying | "
            "autonomous-ground | fires-support | sof-comms | cas-coordination | ground-maneuver"
        ),
    )
    gnss_dependence: str = _MissionField("medium", description="low | medium | high | rtk")
    comms_dependence: str = _MissionField("medium", description="low | medium | high")
    risk_tolerance: str = _MissionField("medium", description="low | medium | high")
    waypoints: list[_MissionWaypointReq] = _MissionField(default_factory=list, min_length=1)
    time_window: str = _MissionField("now", description="now | next-1h | next-6h | next-24h")
    callsign: str = ""
    equipment: list[str] = _MissionField(
        default_factory=list,
        description="Equipment ids from GET /api/v3/equipment (e.g. gps_single_freq, hf_radio)",
    )
    scenario: str = _MissionField(
        "",
        description=(
            "Empty = live data. Or a replay scenario id (e.g. gannon-2024) to run "
            "the assessment against real recorded storm conditions, labeled REPLAY."
        ),
    )
    feeds_demo: list[str] = _MissionField(
        default_factory=list,
        description=(
            "Demo fixtures (labeled DEMO): drap_blackout, nanu_outage, "
            "donki_events, aurora_storm. WarHacker use only."
        ),
    )


@router_v3.get("/equipment")
async def equipment_catalog(request: Request) -> dict:
    """Equipment catalog for mission profiles.

    Returns every equipment id the rule library knows, with display names,
    representative nomenclature, whether space weather affects it, and the
    per-mission-type default selections the Mission Planner pre-checks.
    """
    from app.models.equipment import EQUIPMENT
    from app.models.mission import DEFAULT_EQUIPMENT_BY_MISSION_TYPE

    return {
        "equipment": [
            {
                "id": e.id,
                "display_name": e.display_name,
                "nomenclature": e.nomenclature,
                "affected": e.affected,
                "why_unaffected": e.why_unaffected or None,
            }
            for e in EQUIPMENT.values()
        ],
        "defaults_by_mission_type": {k: list(v) for k, v in DEFAULT_EQUIPMENT_BY_MISSION_TYPE.items()},
    }


@router_v3.post("/mission/assess")
async def mission_assess(request: Request, req: _MissionRequestModel) -> dict:
    """
    Operator-language mission assessment.

    Takes a mission profile (mission_type, GNSS/comms dependence, risk
    tolerance, waypoints) and returns the full operator-facing
    MissionAssessment: mission risk level (CLEAR/CAUTION/HIGH_RISK/DELAY),
    GNSS Reliability score, Comms Risk score, plain-English explanation,
    recommended actions, data quality, and source-labelled provenance.

    Internally maps to the existing route-risk engine (same as
    POST /api/v2/route-decision) but adds mission-type-aware scoring:
    a 0.5 m GPS error reads as DEGRADED for an RTK ag mission and GOOD
    for a defense patrol — the engine itself doesn't know which mission
    it's serving.
    """
    from fastapi import HTTPException

    from app.api.routes_v2 import _build_env, _engine, _platform_from_request, PlatformRequest
    from app.models.decision import WaypointInput
    from app.models.equipment import EQUIPMENT, evaluate_equipment
    from app.models.mission import (
        MissionRequest,
        MissionWaypoint,
        assess_mission,
        map_to_platform_kwargs,
    )

    unknown = [e for e in req.equipment if e not in EQUIPMENT]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown equipment id(s): {unknown}. See GET /api/v3/equipment for the catalog.",
        )

    from app.models.replay_scenarios import REPLAY_SCENARIOS

    replay = None
    if req.scenario:
        replay = REPLAY_SCENARIOS.get(req.scenario)
        if replay is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown scenario '{req.scenario}'. Available: {sorted(REPLAY_SCENARIOS)}.",
            )

    mission = MissionRequest(
        mission_type=req.mission_type,
        gnss_dependence=req.gnss_dependence,
        comms_dependence=req.comms_dependence,
        risk_tolerance=req.risk_tolerance,
        waypoints=[MissionWaypoint(w.name, w.lat, w.lon) for w in req.waypoints],
        time_window=req.time_window,
        callsign=req.callsign,
        equipment=list(req.equipment),
    )

    # Build the engine inputs from the mission profile, run the engine, then
    # hand the engine's output to the mission scorer for the operator card.
    plat_kwargs = map_to_platform_kwargs(mission)
    platform = _platform_from_request(
        PlatformRequest(asset_type=plat_kwargs["asset_type"], criticality=plat_kwargs["criticality"])
    )
    if replay is not None:
        # Replay env: the documented measured values of a real storm, with
        # observations source-labeled REPLAY so provenance can't be mistaken
        # for live telemetry.
        from datetime import datetime, timezone

        from app.models.decision import EnvironmentSnapshot, ObservationInput

        now_iso = datetime.now(timezone.utc).isoformat()
        src = f"REPLAY:{replay.id}"
        env = EnvironmentSnapshot(
            kp=replay.kp,
            bz_nt=replay.bz_nt,
            xray_flux=replay.xray_flux_wm2,
            proton_flux_10mev=replay.proton_flux_10mev_pfu,
            wind_speed_km_s=replay.wind_speed_km_s,
            data_age_seconds=0,
            feeds_available=[src],
            feeds_unavailable=[],
            observations=[
                ObservationInput(src, "kp_index", replay.kp, "index", now_iso, 0),
                ObservationInput(src, "bz_gsm_nt", replay.bz_nt, "nT", now_iso, 0),
                ObservationInput(src, "xray_flux_wm2", replay.xray_flux_wm2, "W/m²", now_iso, 0),
                ObservationInput(src, "proton_flux_10mev_pfu", replay.proton_flux_10mev_pfu, "pfu", now_iso, 0),
                ObservationInput(src, "solar_wind_km_s", replay.wind_speed_km_s, "km/s", now_iso, 0),
            ],
            kp_forecast_24h=None,
        )
    else:
        env = _build_env()

        # Manual observation override (disconnected ops, last resort): an
        # operator-entered Kp (+ optional flare class / proton flux) replaces
        # those drivers, with observations relabeled OPERATOR_ENTRY. The
        # remaining drivers stay on feed/cached values. Replay wins over
        # manual when both are present (scenario is an explicit request).
        from app.data import manual_obs as _manual_obs

        manual = _manual_obs.get_observation()
        if manual is not None:
            from dataclasses import replace as _dc_replace
            from datetime import datetime, timezone

            from app.models.decision import ObservationInput

            now_iso = datetime.now(timezone.utc).isoformat()
            src = "OPERATOR_ENTRY"
            overrides = {"kp": manual.kp}
            obs_extra = [ObservationInput(src, "kp_index", manual.kp, "index", now_iso, 0)]
            if manual.xray_flux_wm2() is not None:
                overrides["xray_flux"] = manual.xray_flux_wm2()
                obs_extra.append(ObservationInput(src, "xray_flux_wm2", manual.xray_flux_wm2(), "W/m²", now_iso, 0))
            if manual.proton_flux_10mev_pfu is not None:
                overrides["proton_flux_10mev"] = manual.proton_flux_10mev_pfu
                obs_extra.append(
                    ObservationInput(src, "proton_flux_10mev_pfu", manual.proton_flux_10mev_pfu, "pfu", now_iso, 0)
                )
            replaced = {o.phenomenon for o in obs_extra}
            env = _dc_replace(
                env,
                **overrides,
                feeds_available=list(env.feeds_available) + [src],
                observations=[o for o in env.observations if o.phenomenon not in replaced] + obs_extra,
            )
    wp_inputs = [WaypointInput(w.lat, w.lon, w.name) for w in mission.waypoints]

    rec, wp_decisions = _engine.route_risk(env, wp_inputs, platform)

    # Reconstruct the same dict shape POST /api/v2/route-decision returns,
    # so the mission scorer reads from a single canonical format.
    route_decision = {
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

    # Equipment-level readout (WarHacker P0-2): run the doctrine rule library
    # against the same live drivers the engine used, with NOAA's forecaster
    # probabilities attached. Only when the request named equipment.
    equipment_assessment = None
    if mission.equipment:
        if replay is not None:
            # Same recorded drivers as the engine env. Live NOAA forecast
            # probabilities don't apply to a historical replay — omitted.
            equipment_assessment = evaluate_equipment(
                mission.equipment,
                kp=replay.kp,
                xray_flux_wm2=replay.xray_flux_wm2,
                proton_flux_10mev_pfu=replay.proton_flux_10mev_pfu,
            ).to_dict()
        else:
            # env already carries any manual override — evaluate from env so
            # the rule library and the physics engine agree on drivers.
            from app.data.noaa import get_noaa_scales

            equipment_assessment = evaluate_equipment(
                mission.equipment,
                kp=env.kp,
                xray_flux_wm2=env.xray_flux,
                proton_flux_10mev_pfu=env.proton_flux_10mev,
                noaa_scales=get_noaa_scales(),
            ).to_dict()

    assessment = assess_mission(mission, route_decision, equipment_assessment)
    out = assessment.to_dict()

    if replay is not None:
        from app.models.replay_scenarios import replay_note

        # Replay banner: surfaced in data quality notes + inputs echo so the
        # UI and any downstream consumer can't mistake this for live data.
        out["inputs_echo"]["scenario"] = replay.id
        out["inputs_echo"]["scenario_title"] = replay.title
        out["data_quality"]["notes"] = [replay_note(replay), replay.citation] + (out["data_quality"].get("notes") or [])
    else:
        # Disconnected-ops labels (honesty first): ADVISORY when running on
        # carried (cache-and-carry) state, MANUAL when an operator-entered
        # observation overrode drivers.
        from app.data import manual_obs as _mo
        from app.data import state_cache as _sc

        notes = []
        manual_active = _mo.get_observation()
        if manual_active is not None:
            notes.append(_mo.manual_note(manual_active))
            out["inputs_echo"]["manual_observation"] = manual_active.to_dict()
        advisory = _sc.advisory_note()
        if advisory:
            notes.append(advisory)
            out["inputs_echo"]["advisory_mode"] = True
        if notes:
            out["data_quality"]["notes"] = notes + (out["data_quality"].get("notes") or [])

    # ── Operational feeds layer (additive — D-RAP HF, NANU GPS) ───────────────
    # Authoritative feeds that refine HF/PNT guidance. Never downgrades the
    # base verdict; only escalates (monotonic) and adds consequences/recs.
    out["operational_feeds"] = _apply_operational_feeds(out, mission, list(req.feeds_demo or []))

    return out


_RISK_ORDER = ["CLEAR", "CAUTION", "HIGH_RISK", "DELAY"]


def _max_risk(a: str | None, b: str | None) -> str | None:
    """Return the higher of two mission_risk_level values (monotonic floor)."""
    vals = [x for x in (a, b) if x in _RISK_ORDER]
    if not vals:
        return a or b
    return max(vals, key=_RISK_ORDER.index)


def _apply_operational_feeds(out: dict, mission, feeds_demo: list[str]) -> dict:
    """Layer the operational feeds onto a completed assessment (additive).

    Covers D-RAP (HF absorption), NANU/CelesTrak (GPS availability), DONKI
    (event cause-of-risk), OVATION (auroral GNSS/comms), and WMM (magnetic
    declination / compass reliability). Returns the operational_feeds status
    block; mutates out in place to add feed-driven recommendations,
    consequences, and a monotonic verdict floor.
    """
    from app.data import donki as _donki
    from app.data import drap as _drap
    from app.data import nanu as _nanu
    from app.data import ovation as _ovation
    from app.data import wmm as _wmm

    # Demo fixtures (clearly labeled DEMO; honest — never a fake live claim).
    if "drap_blackout" in feeds_demo:
        _drap.set_demo_blackout()
    if "nanu_outage" in feeds_demo:
        _nanu.set_demo_outage()
    if "donki_events" in feeds_demo:
        _donki.set_demo_events()
    if "aurora_storm" in feeds_demo:
        _ovation.set_demo_aurora()

    wps = [{"name": w.name, "lat": w.lat, "lon": w.lon} for w in mission.waypoints]
    feed_recs: list[str] = []
    feed_cons: list[dict] = []
    floor: str | None = None

    def _label(snap: dict, live_srcs) -> str:
        s = snap.get("source")
        if s in (live_srcs if isinstance(live_srcs, (set, tuple, list)) else (live_srcs,)):
            return "authoritative"
        if s == "DEMO":
            return "demo"
        return "unavailable"

    # ── D-RAP: authoritative HF absorption ────────────────────────────────────
    drap_snap = _drap.cache_snapshot()
    drap_hf = _drap.route_hf_risk(wps) if _drap.available() else None
    drap_used = drap_hf is not None
    if drap_hf and drap_hf["level"] in ("MODERATE", "SEVERE"):
        sev = drap_hf["level"] == "SEVERE"
        feed_cons.append(
            {
                "risk": "RED" if sev else "AMBER",
                "fn": "HF reachback / BLOS comms",
                "fail": f"D-RAP: HF absorbed below ~{drap_hf['absorbed_to_mhz']:.0f} MHz near "
                f"{drap_hf.get('at') or 'the AO'} — HF reachback may be unreliable.",
            }
        )
        feed_recs.append(
            "HF risk elevated (authoritative D-RAP absorption feed): use SATCOM/VHF backup "
            "or shift to higher HF frequencies / adjust the comms plan."
        )
        if mission.comms_dependence == "high":
            floor = _max_risk(floor, "HIGH_RISK" if sev else "CAUTION")
    drap_block = {
        **drap_snap,
        "used_in_assessment": drap_used,
        "route_hf_risk": drap_hf,
        "feed_label": _label(drap_snap, "NOAA SWPC D-RAP"),
    }

    # ── NANU / CelesTrak: GPS availability ────────────────────────────────────
    nanu_snap = _nanu.cache_snapshot()
    nanu_used = _nanu.has_active_outage()
    if nanu_used:
        const = _nanu.constellation_status()
        advs = _nanu.active_advisories()
        if const and const["degraded"]:
            # Live operational-constellation signal from CelesTrak GPS-ops.
            fail = (
                f"CelesTrak GPS-ops: {const['operational_count']} operational SVs vs nominal "
                f"{const['nominal']} — reduced GPS constellation availability degrades "
                "navigation confidence and DOP."
            )
        else:
            fail = (
                f"NANU: {len(advs)} GPS outage advisory(ies) active — reduced constellation "
                "availability may affect navigation confidence."
            )
        feed_cons.append({"risk": "AMBER", "fn": "GPS constellation availability", "fail": fail})
        rec = "Verify receiver constellation availability; use multi-GNSS / backup navigation."
        if mission.mission_type == "precision-ag" or mission.gnss_dependence == "rtk":
            rec = "RTK/autosteer readiness impact — " + rec + " Delay precision operations if tolerance is low."
        feed_recs.append(rec)
        if mission.gnss_dependence in ("high", "rtk"):
            floor = _max_risk(floor, "CAUTION")
    nanu_block = {
        **nanu_snap,
        "used_in_assessment": nanu_used,
        "feed_label": _label(nanu_snap, ("NANU feed", "CelesTrak GPS-ops")),
    }

    # ── DONKI: space-weather event log (cause-of-risk / timeline) ─────────────
    donki_snap = _donki.cache_snapshot()
    donki_drivers = _donki.drivers_summary() if _donki.available() else []
    donki_used = bool(donki_drivers)
    if donki_used:
        # Explanatory only — DONKI never escalates the verdict by itself; it
        # tells the operator WHY the measured conditions look the way they do.
        feed_recs.append("Space-weather drivers (NASA DONKI): " + "; ".join(donki_drivers) + ".")
        out["event_drivers"] = donki_drivers
    donki_block = {
        **donki_snap,
        "used_in_assessment": donki_used,
        "drivers": donki_drivers,
        "feed_label": _label(donki_snap, "NASA DONKI"),
    }

    # ── OVATION: auroral GNSS/comms scintillation (high latitude) ─────────────
    ovation_snap = _ovation.cache_snapshot()
    aurora = _ovation.route_aurora_risk(wps) if _ovation.available() else None
    ovation_used = bool(aurora and aurora["level"] in ("ELEVATED", "HIGH"))
    if ovation_used:
        high = aurora["level"] == "HIGH"
        feed_cons.append(
            {
                "risk": "RED" if high else "AMBER",
                "fn": "GNSS carrier-phase / HF & SATCOM links",
                "fail": f"OVATION: {aurora['prob_pct']:.0f}% aurora probability near "
                f"{aurora.get('at') or 'the route'} — auroral scintillation can scramble GNSS "
                "carrier phase (RTK/PPP) and degrade HF/SATCOM at high latitude.",
            }
        )
        feed_recs.append(
            "Auroral activity over the route (NOAA OVATION): expect GNSS scintillation and "
            "high-latitude comms fades — plan redundant PNT and comms windows."
        )
        if mission.gnss_dependence in ("high", "rtk") or mission.comms_dependence == "high":
            floor = _max_risk(floor, "HIGH_RISK" if high else "CAUTION")
    ovation_block = {
        **ovation_snap,
        "used_in_assessment": ovation_used,
        "route_aurora_risk": aurora,
        "feed_label": _label(ovation_snap, "NOAA SWPC OVATION"),
    }

    # ── WMM: magnetic declination / compass reliability (local model) ─────────
    wmm_decl = _wmm.route_declination(wps) if _wmm.available() else None
    wmm_used = wmm_decl is not None
    if wmm_decl:
        feed_recs.append("Navigation reference (World Magnetic Model): " + wmm_decl["guidance"] + ".")
        worst = wmm_decl.get("route_compass_reliability")
        if worst in ("CAUTION", "BLACKOUT"):
            feed_cons.append(
                {
                    "risk": "RED" if worst == "BLACKOUT" else "AMBER",
                    "fn": "Magnetic compass / magnetic heading",
                    "fail": "WMM: magnetic compass "
                    + ("unreliable (polar blackout zone)" if worst == "BLACKOUT" else "degraded near the pole")
                    + " along the route — rely on GPS/grid/celestial for heading.",
                }
            )
            if worst == "BLACKOUT":
                floor = _max_risk(floor, "CAUTION")
    wmm_block = {
        "source": "World Magnetic Model (local)",
        "available": _wmm.available(),
        "used_in_assessment": wmm_used,
        "declination": wmm_decl,
        "feed_label": "authoritative" if wmm_used else "unavailable",
    }

    if feed_recs:
        out["recommended_actions"] = (out.get("recommended_actions") or []) + feed_recs
    if feed_cons:
        out["feed_consequences"] = feed_cons
    if floor:
        out["mission_risk_level"] = _max_risk(out["mission_risk_level"], floor)

    return {
        "drap": drap_block,
        "nanu": nanu_block,
        "donki": donki_block,
        "ovation": ovation_block,
        "wmm": wmm_block,
    }


@router_v3.post("/recommend")
async def recommend(request: Request, req: _MissionRequestModel) -> dict:
    """Alias for POST /mission/assess in briefing-book vocabulary.

    Same request and response shape: mission profile in, equipment-specific,
    time-bounded operational recommendation out.
    """
    return await mission_assess(request, req)


@router_v3.get("/mission/overlay.kml")
async def mission_overlay_kml(
    request: Request,
    lat: float,
    lon: float,
    radius_km: float = 25.0,
    scenario: str = "",
) -> Response:
    """Time-windowed ATAK risk overlay (KML with TimeSpan zones).

    Load this URL in ATAK/WinTAK as a network KML layer — no plugin needed.
    Zones over the AO are colored by weather state per 3-hour window; the
    ATAK time slider drives which window is visible.

    Live mode (default): windows from NOAA's 3-day Kp forecast product.
    Replay mode (?scenario=gannon-2024): windows from the storm's recorded
    GFZ Kp timeline mapped onto today, labeled REPLAY.
    """
    from app.models.replay_scenarios import REPLAY_SCENARIOS
    from app.outputs.mission_overlay import build_live_overlay_kml, build_replay_overlay_kml

    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0.5 <= radius_km <= 500):
        raise HTTPException(status_code=422, detail="lat/lon out of range or radius_km not in [0.5, 500]")

    if scenario:
        scn = REPLAY_SCENARIOS.get(scenario)
        if scn is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown scenario '{scenario}'. Available: {sorted(REPLAY_SCENARIOS)}.",
            )
        kml = build_replay_overlay_kml(lat, lon, radius_km, scn)
    else:
        # Raw forecast rows live in the feed cache itself; cache_snapshot()
        # only carries metadata (timestamps/status), so read _cache directly.
        from app.data.noaa import _cache as _noaa_cache
        from app.models.forecast import parse_kp_forecast

        entries = parse_kp_forecast(_noaa_cache.get("kp_forecast") or [])
        kml = build_live_overlay_kml(lat, lon, radius_km, entries)

    return Response(
        content=kml,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'inline; filename="ionshield-mission-overlay.kml"'},
    )


# ── Manual observation entry (disconnected ops) ──────────────────────────────


class _ManualObsRequest(_MissionBase):
    kp: float = _MissionField(..., ge=0, le=9, description="Planetary Kp from the operator's brief (0-9)")
    source_note: str = _MissionField(
        ..., min_length=3, description="Where the value came from (e.g. 'S2 weather brief 0600Z')"
    )
    proton_flux_10mev_pfu: float | None = _MissionField(None, ge=0)
    xray_class: str | None = _MissionField(None, description="Flare class letter: A | B | C | M | X")


@router_v3.post("/manual-observation")
async def set_manual_observation(request: Request, req: _ManualObsRequest) -> dict:
    """Enter an operator-supplied observation (disconnected ops, last resort).

    When no live feed and no carried cache is available, the operator enters
    Kp from an authoritative channel (S2 weather brief, military space
    weather officer report). Mission assessments then run the same doctrine
    rules against it, labeled MANUAL/operator-entered in every output.
    Entries expire after 3 hours (one Kp bin).
    """
    from app.data import manual_obs

    if req.xray_class is not None and req.xray_class.strip().upper() not in ("A", "B", "C", "M", "X"):
        raise HTTPException(status_code=422, detail="xray_class must be one of A, B, C, M, X")

    obs = manual_obs.set_observation(
        kp=req.kp,
        source_note=req.source_note,
        proton_flux_10mev_pfu=req.proton_flux_10mev_pfu,
        xray_class=req.xray_class,
    )
    return {"status": "active", "observation": obs.to_dict()}


@router_v3.get("/manual-observation")
async def get_manual_observation(request: Request) -> dict:
    """The active manual observation, if any (expired entries read as none)."""
    from app.data import manual_obs

    obs = manual_obs.get_observation()
    return {"status": "active" if obs else "none", "observation": obs.to_dict() if obs else None}


@router_v3.delete("/manual-observation")
async def clear_manual_observation(request: Request) -> dict:
    """Clear the manual observation — assessments return to feed data."""
    from app.data import manual_obs

    manual_obs.clear_observation()
    return {"status": "cleared"}
