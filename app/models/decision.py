"""
IonShield Decision Engine — v1.

Wraps the geophysical risk engine (models/risk.py) and NOAA data layer
(data/noaa.py) in a typed, deterministic decision layer.

Design principles:
  - Pure functions: no I/O inside the engine; all NOAA state is passed in via
    EnvironmentSnapshot so the same inputs always produce the same outputs.
  - Typed objects: ConfidenceObject, ProvenanceObject, and RecommendationObject
    carry enough metadata to reconstruct *why* a decision was made.
  - Replay-safe: `now` is passed explicitly to avoid wall-clock non-determinism.
    Only `id` (UUID) and `created_at` differ between replays of the same inputs.
  - Stale-data transparency: confidence penalties are applied per the same
    thresholds used in the v1 status endpoint (_confidence helper in routes.py).

Not included in v1:
  - Operator override persistence (ack / note fields are stored on the object
    but not written to any database)
  - Probabilistic ensemble forecasting
  - Full IRI / WBMOD physics (approximations are used throughout)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from app.models.risk import compute_hf_link, compute_risk

# ── Model versioning ──────────────────────────────────────────────────────────

MODEL_VERSION = "1.0.0"

# ── Enums ────────────────────────────────────────────────────────────────────


class RiskLevel(str, Enum):
    NOMINAL = "NOMINAL"
    ELEVATED = "ELEVATED"
    DEGRADED = "DEGRADED"
    SEVERE = "SEVERE"


class DecisionType(str, Enum):
    COMMS_FALLBACK = "COMMS_FALLBACK"
    ROUTE_RISK = "ROUTE_RISK"


class CommsFallbackAction(str, Enum):
    USE_PRIMARY_HF = "USE_PRIMARY_HF"
    USE_ALTERNATE_HF = "USE_ALTERNATE_HF"
    SWITCH_TO_SATCOM = "SWITCH_TO_SATCOM"
    SWITCH_TO_UHF = "SWITCH_TO_UHF"
    DEGRADED_MODE = "DEGRADED_MODE"
    HF_NOT_VIABLE = "HF_NOT_VIABLE"


class RouteAction(str, Enum):
    GO = "GO"
    ADVISORY = "ADVISORY"
    CAUTION = "CAUTION"
    NO_GO = "NO_GO"


# ── Input dataclasses ────────────────────────────────────────────────────────


@dataclass
class ObservationInput:
    """Single geophysical observation passed into the decision engine."""

    source: str  # e.g. "NOAA_SWPC"
    phenomenon: str  # e.g. "kp_index"
    value: float
    unit: str
    observed_at: str  # ISO-8601
    age_seconds: int


@dataclass
class EnvironmentSnapshot:
    """
    Complete geophysical state at decision time.

    All NOAA I/O must be resolved before constructing this object.
    The decision engine performs no network calls.
    """

    kp: float
    bz_nt: float
    xray_flux: float  # W/m²
    proton_flux_10mev: float  # pfu
    wind_speed_km_s: float
    data_age_seconds: int
    feeds_available: list[str]
    feeds_unavailable: list[str]
    observations: list[ObservationInput]
    # Forecast (optional — None when kp_forecast feed is unavailable)
    kp_forecast_24h: float | None = None
    kp_forecast_issued_at: str | None = None
    kp_forecast_lead_hours: float | None = None


@dataclass
class WaypointInput:
    lat: float
    lon: float
    name: str = ""


@dataclass
class SystemDependencyInput:
    """Describes one comms system the platform depends on."""

    system_type: str  # "HF" | "SATCOM" | "UHF" | "GPS"
    primary_freqs_mhz: list[float] = field(default_factory=list)
    fallback_modes: list[str] = field(default_factory=list)
    degradation_tolerance: int = 3  # 1=intolerant … 5=highly tolerant


@dataclass
class PlatformInput:
    """Describes the requesting platform's capabilities and criticality."""

    asset_type: str = "GPS_L1"
    system_dependencies: list[SystemDependencyInput] = field(default_factory=list)
    criticality: int = 3  # 1=lowest … 5=highest (affects NO-GO thresholds)


# ── Output dataclasses ────────────────────────────────────────────────────────


@dataclass
class ConfidenceFactor:
    factor: str
    effect: float  # negative = penalty, positive = bonus
    detail: str


@dataclass
class ConfidenceObject:
    """
    How much to trust this recommendation.

    score: 0.0–1.0 (1.0 = fully trustworthy live data + short forecast horizon)
    label: human-readable tier (HIGH / MEDIUM / LOW / VERY_LOW)
    drivers: list of named penalties / bonuses that built the score
    stale_data: True when data_age_seconds > 600
    """

    score: float
    label: str
    drivers: list[ConfidenceFactor]
    stale_data: bool
    stale_penalty_applied: bool
    data_completeness: float  # fraction of feeds available (0.0–1.0)
    computed_at: str  # ISO-8601

    @classmethod
    def compute(
        cls,
        env: EnvironmentSnapshot,
        forecast_lead_hours: float = 0.0,
    ) -> "ConfidenceObject":
        """
        Build a ConfidenceObject from an EnvironmentSnapshot.

        Penalty schedule (mirrors _confidence() in routes.py for data freshness):
          data_freshness   < 5 min  → 0.0 penalty
          data_freshness   < 15 min → −0.15
          data_freshness   < 60 min → −0.40
          data_freshness   ≥ 60 min → −0.60
          stale (> 10 min) also sets stale_data=True

          data_completeness fraction < 1.0 → penalty proportional to missing feeds
          forecast_lead_hours ≥ 24 → −0.10 (near-real-time not applicable)
          forecast_lead_hours ≥ 48 → −0.20
          bz_variability: |bz| > 20 nT → −0.05 (rapid storm onset possible)
        """
        score = 1.0
        drivers: list[ConfidenceFactor] = []
        age = env.data_age_seconds

        # Data freshness
        if age < 300:
            freshness_penalty = 0.0
            freshness_detail = f"Data {age}s old — fully fresh"
        elif age < 900:
            freshness_penalty = -0.15
            freshness_detail = f"Data {age}s old — one refresh cycle missed"
        elif age < 3600:
            freshness_penalty = -0.40
            freshness_detail = f"Data {age}s old — multiple missed refreshes"
        else:
            freshness_penalty = -0.60
            freshness_detail = f"Data {age}s old — significantly degraded"

        if freshness_penalty != 0.0:
            drivers.append(
                ConfidenceFactor("data_freshness", freshness_penalty, freshness_detail)
            )
        score += freshness_penalty
        stale_penalty_applied = freshness_penalty < 0.0

        # Data completeness
        total_feeds = len(env.feeds_available) + len(env.feeds_unavailable)
        completeness = (
            len(env.feeds_available) / total_feeds if total_feeds > 0 else 1.0
        )
        if completeness < 1.0:
            missing = len(env.feeds_unavailable)
            completeness_penalty = -0.10 * missing
            drivers.append(
                ConfidenceFactor(
                    "data_completeness",
                    completeness_penalty,
                    f"{missing} feed(s) unavailable: {', '.join(env.feeds_unavailable)}",
                )
            )
            score += completeness_penalty

        # Forecast lead time
        if forecast_lead_hours >= 48:
            drivers.append(
                ConfidenceFactor(
                    "forecast_lead",
                    -0.20,
                    f"Forecast horizon {forecast_lead_hours:.0f}h — high uncertainty",
                )
            )
            score -= 0.20
        elif forecast_lead_hours >= 24:
            drivers.append(
                ConfidenceFactor(
                    "forecast_lead",
                    -0.10,
                    f"Forecast horizon {forecast_lead_hours:.0f}h — moderate uncertainty",
                )
            )
            score -= 0.10

        # Bz variability flag
        if abs(env.bz_nt) > 20.0:
            drivers.append(
                ConfidenceFactor(
                    "bz_variability",
                    -0.05,
                    f"Bz = {env.bz_nt:.1f} nT — rapid storm onset possible",
                )
            )
            score -= 0.05

        score = max(0.0, min(1.0, round(score, 3)))

        if score >= 0.85:
            label = "HIGH"
        elif score >= 0.65:
            label = "MEDIUM"
        elif score >= 0.40:
            label = "LOW"
        else:
            label = "VERY_LOW"

        return cls(
            score=score,
            label=label,
            drivers=drivers,
            stale_data=age > 600,
            stale_penalty_applied=stale_penalty_applied,
            data_completeness=round(completeness, 3),
            computed_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass
class ProvenanceObject:
    """
    Cryptographic provenance of a recommendation.

    input_hash: SHA-256 of the canonical JSON of all inputs that drove the
                decision. Same inputs → same hash, enabling replay verification.
    model_version: semver string of the decision engine.
    """

    model_version: str
    input_hash: str
    computed_at: str  # ISO-8601
    observations_used: list[str]  # phenomenon names
    forecasts_used: list[str]
    feeds_unavailable: list[str]
    operator_overrides: list[str]

    @classmethod
    def build(
        cls,
        env: EnvironmentSnapshot,
        model_version: str = MODEL_VERSION,
        extra_inputs: dict[str, Any] | None = None,
        operator_overrides: list[str] | None = None,
    ) -> "ProvenanceObject":
        """
        Build a ProvenanceObject.

        The hash is computed over a canonical dict that includes all env fields
        that affect the decision output. Fields that vary per-call (timestamps,
        UUIDs) are excluded so the hash is deterministic under replay.
        """
        canonical: dict[str, Any] = {
            "model_version": model_version,
            "kp": env.kp,
            "bz_nt": env.bz_nt,
            "xray_flux": env.xray_flux,
            "proton_flux_10mev": env.proton_flux_10mev,
            "wind_speed_km_s": env.wind_speed_km_s,
            "kp_forecast_24h": env.kp_forecast_24h,
        }
        if extra_inputs:
            canonical.update(extra_inputs)
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        input_hash = "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest()

        phenomena = [o.phenomenon for o in env.observations]
        forecasts = (
            [f"kp_forecast_24h={env.kp_forecast_24h}"]
            if env.kp_forecast_24h is not None
            else []
        )

        return cls(
            model_version=model_version,
            input_hash=input_hash,
            computed_at=datetime.now(timezone.utc).isoformat(),
            observations_used=phenomena,
            forecasts_used=forecasts,
            feeds_unavailable=list(env.feeds_unavailable),
            operator_overrides=operator_overrides or [],
        )


@dataclass
class WaypointDecision:
    """Per-waypoint assessment produced by route_risk()."""

    name: str
    lat: float
    lon: float
    risk_level: str
    risk_score: float
    gps_error_m: float
    hf_viable: bool
    hf_best_freq_mhz: float | None
    hf_best_reliability_pct: float | None
    hf_absorption_db: float
    satcom_fade_db: float
    s4_index: float
    pca_active: bool
    watch_notes: list[str]


@dataclass
class RecommendationObject:
    """
    A fully typed, provenance-bearing decision recommendation.

    id: random UUID — differs between calls even for identical inputs.
        This is intentional: recommendations are events, not idempotent entities.
    valid_until: derived from `now` + horizon; deterministic under replay when
                 `now` is passed explicitly.
    """

    id: str
    decision_type: str
    action: str
    action_sentence: str
    valid_until: str  # ISO-8601
    alternatives: list[str]
    impacts: list[dict]
    recommended_actions: list[str]
    confidence: ConfidenceObject
    provenance: ProvenanceObject
    operator_ack: bool = False
    operator_note: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-serializable dict."""
        return {
            "id": self.id,
            "decision_type": self.decision_type,
            "action": self.action,
            "action_sentence": self.action_sentence,
            "valid_until": self.valid_until,
            "alternatives": self.alternatives,
            "impacts": self.impacts,
            "recommended_actions": self.recommended_actions,
            "confidence": {
                "score": self.confidence.score,
                "label": self.confidence.label,
                "stale_data": self.confidence.stale_data,
                "stale_penalty_applied": self.confidence.stale_penalty_applied,
                "data_completeness": self.confidence.data_completeness,
                "drivers": [
                    {
                        "factor": d.factor,
                        "effect": d.effect,
                        "detail": d.detail,
                    }
                    for d in self.confidence.drivers
                ],
                "computed_at": self.confidence.computed_at,
            },
            "provenance": {
                "model_version": self.provenance.model_version,
                "input_hash": self.provenance.input_hash,
                "computed_at": self.provenance.computed_at,
                "observations_used": self.provenance.observations_used,
                "forecasts_used": self.provenance.forecasts_used,
                "feeds_unavailable": self.provenance.feeds_unavailable,
                "operator_overrides": self.provenance.operator_overrides,
            },
            "operator_ack": self.operator_ack,
            "operator_note": self.operator_note,
            "created_at": self.created_at,
        }


# ── Decision engine ───────────────────────────────────────────────────────────

# Minimum HF reliability threshold to consider a band "viable"
_HF_VIABLE_THRESHOLD_PCT = 25.0

# Risk score thresholds used consistently with routes.py narrative
_SCORE_NO_GO = 60
_SCORE_CAUTION = 40
_SCORE_ADVISORY = 20

# Horizon for comms decision validity (shorter = more conservative)
_COMMS_VALID_HOURS = 1
_ROUTE_VALID_HOURS = 3


class DecisionEngine:
    """
    Stateless decision engine.

    All methods are pure functions over their arguments — no module-level
    globals are read here. NOAA state is fully encapsulated in EnvironmentSnapshot.
    """

    # ── Public interface ──────────────────────────────────────────────────────

    def comms_fallback(
        self,
        env: EnvironmentSnapshot,
        lat: float,
        lon: float,
        dest_lat: float | None,
        dest_lon: float | None,
        platform: PlatformInput | None = None,
        now: datetime | None = None,
    ) -> RecommendationObject:
        """
        Recommend a comms configuration for a single link.

        Calls compute_hf_link() from the risk engine with explicit env values
        (not live NOAA lookups) to stay deterministic.
        """
        now = now or datetime.now(timezone.utc)
        platform = platform or PlatformInput()

        hf = compute_hf_link(
            lat,
            lon,
            dest_lat,
            dest_lon,
            kp=env.kp,
            bz=env.bz_nt,
            xray_flux=env.xray_flux,
            proton_flux=env.proton_flux_10mev,
        )
        summary = hf["link_summary"]
        conditions = hf["conditions"]

        fallback_modes = self._platform_fallback_modes(platform)
        action, sentence, alternatives = self._resolve_comms_action(
            summary, conditions, env, lat, fallback_modes
        )

        impacts = self._build_comms_impacts(hf, env)
        recs = self._build_comms_recommendations(summary, conditions, env)

        confidence = ConfidenceObject.compute(env, forecast_lead_hours=0.0)
        provenance = ProvenanceObject.build(
            env,
            extra_inputs={
                "lat": lat,
                "lon": lon,
                "dest_lat": dest_lat,
                "dest_lon": dest_lon,
            },
        )

        valid_until = (now + timedelta(hours=_COMMS_VALID_HOURS)).isoformat()

        return RecommendationObject(
            id=str(uuid.uuid4()),
            decision_type=DecisionType.COMMS_FALLBACK.value,
            action=action,
            action_sentence=sentence,
            valid_until=valid_until,
            alternatives=alternatives,
            impacts=impacts,
            recommended_actions=recs,
            confidence=confidence,
            provenance=provenance,
            created_at=now.isoformat(),
        )

    def route_risk(
        self,
        env: EnvironmentSnapshot,
        waypoints: list[WaypointInput],
        platform: PlatformInput | None = None,
        now: datetime | None = None,
    ) -> tuple[RecommendationObject, list[WaypointDecision]]:
        """
        Assess risk for each waypoint and produce a route-level recommendation.

        Returns (recommendation, waypoint_decisions) so callers have access to
        the per-waypoint detail without a second compute pass.
        """
        now = now or datetime.now(timezone.utc)
        platform = platform or PlatformInput()

        wp_decisions: list[WaypointDecision] = []
        for wp in waypoints:
            wp_decisions.append(self._assess_waypoint(wp, env, platform))

        if not wp_decisions:
            worst = None
            overall_level = RiskLevel.NOMINAL
        else:
            worst = max(wp_decisions, key=lambda w: w.risk_score)
            overall_level = RiskLevel(worst.risk_level)

        action, sentence, alternatives = self._resolve_route_action(
            overall_level, worst, wp_decisions, env, platform
        )

        impacts = self._build_system_impacts(wp_decisions, platform)
        recs = self._build_route_recommendations(overall_level, worst, env)

        confidence = ConfidenceObject.compute(env, forecast_lead_hours=0.0)
        provenance = ProvenanceObject.build(
            env,
            extra_inputs={
                "waypoints": [
                    {"lat": w.lat, "lon": w.lon, "name": w.name} for w in waypoints
                ],
                "asset_type": platform.asset_type,
                "criticality": platform.criticality,
            },
        )

        valid_until = (now + timedelta(hours=_ROUTE_VALID_HOURS)).isoformat()

        rec = RecommendationObject(
            id=str(uuid.uuid4()),
            decision_type=DecisionType.ROUTE_RISK.value,
            action=action,
            action_sentence=sentence,
            valid_until=valid_until,
            alternatives=alternatives,
            impacts=impacts,
            recommended_actions=recs,
            confidence=confidence,
            provenance=provenance,
            created_at=now.isoformat(),
        )
        return rec, wp_decisions

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _assess_waypoint(
        self,
        wp: WaypointInput,
        env: EnvironmentSnapshot,
        platform: PlatformInput,
    ) -> WaypointDecision:
        """Run the full risk engine for one waypoint against the frozen env."""
        risk = compute_risk(wp.lat, wp.lon, kp=env.kp, asset_type=platform.asset_type)
        a = risk["assessment"]

        hf = compute_hf_link(
            wp.lat,
            wp.lon,
            kp=env.kp,
            bz=env.bz_nt,
            xray_flux=env.xray_flux,
            proton_flux=env.proton_flux_10mev,
        )
        link = hf["link_summary"]
        viable = link["viable_count"] > 0

        return WaypointDecision(
            name=wp.name or f"WP({wp.lat:.2f},{wp.lon:.2f})",
            lat=wp.lat,
            lon=wp.lon,
            risk_level=a["risk_level"],
            risk_score=a["risk_score"],
            gps_error_m=a["gps_error_m"],
            hf_viable=viable,
            hf_best_freq_mhz=link["best_frequency_mhz"],
            hf_best_reliability_pct=link["best_reliability_pct"],
            hf_absorption_db=a["hf_absorption_db"],
            satcom_fade_db=a["satcom_fade_db"],
            s4_index=a["s4_index"],
            pca_active=a["pca_active"],
            watch_notes=a.get("watch_notes", []),
        )

    def _platform_fallback_modes(self, platform: PlatformInput) -> list[str]:
        modes: list[str] = []
        for dep in platform.system_dependencies:
            if dep.system_type == "HF":
                modes.extend(dep.fallback_modes)
        return modes or ["SATCOM", "UHF"]

    def _resolve_comms_action(
        self,
        summary: dict,
        conditions: dict,
        env: EnvironmentSnapshot,
        lat: float,
        fallback_modes: list[str],
    ) -> tuple[str, str, list[str]]:
        """Map HF link summary to a CommsFallbackAction + human sentence."""
        best_freq = summary.get("best_frequency_mhz")
        best_rel = summary.get("best_reliability_pct", 0)
        viable_count = summary.get("viable_count", 0)
        # pca_active lives in link_summary, not in conditions
        pca_active = summary.get("pca_active", False)

        alternatives: list[str] = []

        if pca_active:
            action = CommsFallbackAction.HF_NOT_VIABLE.value
            sentence = (
                f"HF NOT VIABLE — Polar Cap Absorption active (|lat|={abs(lat):.1f}°, "
                f"proton flux={env.proton_flux_10mev:.1f} pfu). "
                "Switch to SATCOM or UHF."
            )
            if "SATCOM" in fallback_modes:
                alternatives.append("SWITCH_TO_SATCOM")
            if "UHF" in fallback_modes:
                alternatives.append("SWITCH_TO_UHF")

        elif viable_count == 0:
            action = CommsFallbackAction.HF_NOT_VIABLE.value
            sentence = (
                "HF NOT VIABLE — No bands with ≥25% reliability. "
                "D-layer absorption too high. Switch to SATCOM or UHF."
            )
            if "SATCOM" in fallback_modes:
                alternatives.append("SWITCH_TO_SATCOM")
            if "UHF" in fallback_modes:
                alternatives.append("SWITCH_TO_UHF")

        elif best_rel is not None and best_rel >= 75:
            action = CommsFallbackAction.USE_PRIMARY_HF.value
            sentence = (
                f"USE PRIMARY HF — {best_freq} MHz at {best_rel:.0f}% reliability. "
                "Conditions favorable."
            )
            alternatives = ["USE_ALTERNATE_HF"]

        elif best_rel is not None and best_rel >= 50:
            action = CommsFallbackAction.USE_ALTERNATE_HF.value
            sentence = (
                f"USE ALTERNATE HF — Best available {best_freq} MHz at "
                f"{best_rel:.0f}% reliability. Conditions degraded; "
                "monitor and prepare fallback."
            )
            alternatives = ["SWITCH_TO_SATCOM", "SWITCH_TO_UHF"]

        else:
            action = CommsFallbackAction.DEGRADED_MODE.value
            sentence = (
                f"DEGRADED MODE — Best available {best_freq} MHz at only "
                f"{best_rel:.0f}% reliability. "
                "Operate in degraded mode; prefer SATCOM if available."
            )
            if "SATCOM" in fallback_modes:
                alternatives.append("SWITCH_TO_SATCOM")

        return action, sentence, alternatives

    def _resolve_route_action(
        self,
        overall_level: RiskLevel,
        worst: WaypointDecision | None,
        all_wps: list[WaypointDecision],
        env: EnvironmentSnapshot,
        platform: PlatformInput,
    ) -> tuple[str, str, list[str]]:
        """Map worst waypoint risk to a RouteAction + human sentence."""
        if worst is None:
            return RouteAction.GO.value, "GO — No waypoints to assess.", []

        score = worst.risk_score
        # Raise threshold one tier for high-criticality platforms
        effective_no_go = _SCORE_NO_GO - (5 * max(0, platform.criticality - 3))
        effective_caution = _SCORE_CAUTION - (5 * max(0, platform.criticality - 3))

        if score >= effective_no_go:
            action = RouteAction.NO_GO.value
            sentence = (
                f"NO-GO — {worst.name} at {worst.risk_level} "
                f"(score {worst.risk_score:.0f}/100, GPS error {worst.gps_error_m:.1f} m). "
                "Postpone or re-route."
            )
            alternatives = ["DELAY_OPERATION", "ALTERNATE_ROUTE"]

        elif score >= effective_caution:
            action = RouteAction.CAUTION.value
            sentence = (
                f"CAUTION — {worst.name} shows degraded conditions "
                f"(score {worst.risk_score:.0f}/100, GPS error {worst.gps_error_m:.1f} m). "
                "Consider delay or backup nav."
            )
            alternatives = ["DELAY_OPERATION", "BACKUP_NAV_REQUIRED"]

        elif score >= _SCORE_ADVISORY:
            action = RouteAction.ADVISORY.value
            sentence = (
                f"ADVISORY — Elevated risk at {worst.name} "
                f"(score {worst.risk_score:.0f}/100, GPS error {worst.gps_error_m:.1f} m). "
                "Monitor conditions."
            )
            alternatives = ["MONITOR_CONDITIONS"]

        else:
            action = RouteAction.GO.value
            sentence = "GO — All waypoints nominal. Standard operations."
            alternatives = []

        return action, sentence, alternatives

    def _build_comms_impacts(self, hf: dict, env: EnvironmentSnapshot) -> list[dict]:
        # Best-band absorption from the sorted frequencies list (index 0 = best)
        freqs = hf.get("frequencies", [])
        best_absorption = freqs[0]["absorption_db"] if freqs else 0
        return [
            {
                "system": "HF",
                "metric": "absorption_db",
                "value": best_absorption,
                "detail": "D-layer absorption estimate (CCIR-888 approximation)",
            },
            {
                "system": "HF",
                "metric": "viable_bands",
                "value": hf["link_summary"]["viable_count"],
                "detail": "Bands with ≥25% reliability",
            },
            {
                "system": "GPS",
                "metric": "kp_index",
                "value": env.kp,
                "detail": "Current planetary K-index driving ionospheric disturbance",
            },
        ]

    def _build_comms_recommendations(
        self, summary: dict, conditions: dict, env: EnvironmentSnapshot
    ) -> list[str]:
        recs = []
        if conditions.get("pca_active"):
            recs.append("PCA event detected — avoid HF poleward of 65° latitude.")
        if env.bz_nt < -10:
            recs.append(
                f"Southward Bz ({env.bz_nt:.1f} nT) — storm activity likely increasing."
            )
        if summary.get("viable_count", 0) > 0:
            recs.append(
                f"Best HF option: {summary['best_frequency_mhz']} MHz "
                f"at {summary['best_reliability_pct']:.0f}% reliability."
            )
        if not recs:
            recs.append("Conditions nominal. Primary HF recommended.")
        return recs

    def _build_system_impacts(
        self, wp_decisions: list[WaypointDecision], platform: PlatformInput
    ) -> list[dict]:
        if not wp_decisions:
            return []
        max_gps_err = max(w.gps_error_m for w in wp_decisions)
        hf_viable_count = sum(1 for w in wp_decisions if w.hf_viable)
        pca_count = sum(1 for w in wp_decisions if w.pca_active)

        return [
            {
                "system": "GPS",
                "metric": "max_gps_error_m",
                "value": round(max_gps_err, 1),
                "detail": f"Worst-case GPS error across {len(wp_decisions)} waypoint(s)",
            },
            {
                "system": "HF",
                "metric": "viable_waypoints",
                "value": hf_viable_count,
                "detail": f"{hf_viable_count}/{len(wp_decisions)} waypoints have viable HF",
            },
            {
                "system": "HF",
                "metric": "pca_waypoints",
                "value": pca_count,
                "detail": f"{pca_count} waypoint(s) under Polar Cap Absorption",
            },
        ]

    def _build_route_recommendations(
        self,
        level: RiskLevel,
        worst: WaypointDecision | None,
        env: EnvironmentSnapshot,
    ) -> list[str]:
        recs = []
        if env.kp >= 7:
            recs.append(
                f"Kp={env.kp:.1f} — G{int(env.kp) - 4} geomagnetic storm active."
            )
        if env.bz_nt < -10:
            recs.append(
                f"Southward Bz ({env.bz_nt:.1f} nT) — conditions may intensify."
            )
        if worst and worst.pca_active:
            recs.append(
                f"PCA active at {worst.name} — HF blackout risk poleward of 65°."
            )
        if level in (RiskLevel.DEGRADED, RiskLevel.SEVERE):
            recs.append(
                "Activate backup navigation. Verify SATCOM link before departure."
            )
        if not recs:
            recs.append(
                "No significant space weather alerts. Standard pre-mission checks."
            )
        return recs
