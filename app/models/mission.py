"""
Mission Planner — operator-language → decision-engine translation layer.

Stage 2 of the Mission Planner work. The Mission Planner page accepts inputs
in *operator language* (mission_type, gnss_dependence, comms_dependence,
risk_tolerance) and emits *operator answers* (CLEAR / CAUTION / HIGH_RISK /
DELAY, GNSS reliability score 0-100, comms risk score 0-100, plain-English
explanation, recommended actions, data quality indicator).

This module owns that translation. The downstream `DecisionEngine` in
`app.models.decision` is left untouched — it speaks `PlatformInput` /
`SystemDependencyInput` / `EnvironmentSnapshot` and we adapt to it here.

Why a separate module:
  • Keeps mission-aware scoring decoupled from the physics-grounded engine
  • The same engine still serves /api/v2/route-decision unchanged
  • Tested in isolation (no live network needed for the mapping logic)
  • Easy to add a new mission type / dependence level without touching
    physics code

Scoring rules (mission-aware, not just engine-aware):
  • RTK-critical missions get a much tighter GPS-error tolerance (0.5 m)
  • Solo-GNSS (sole-source nav) missions escalate on any leg with GPS
    error > 5 m, even if the engine itself says GO for a civilian receiver
  • High comms-dependence missions escalate on any leg with HF degradation,
    not just when ALL legs degrade
  • Polar Cap Absorption (PCA) active always pushes comms risk to at least
    MODERATE regardless of HF leg status

Output:
  MissionAssessment dataclass / dict with the full operator-readable result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Mission inputs ───────────────────────────────────────────────────────────

# The mission types the planner exposes. Keep in sync with
# app/pages/mission.html's <select id="m-type"> options.
# The last four are military mission profiles (WarHacker P0-2) — they speak
# the briefing-book vocabulary (AFATDS fires, SOF comms, CAS, maneuver).
MISSION_TYPES: tuple[str, ...] = (
    "uav",
    "bvlos",
    "precision-ag",
    "maritime",
    "defense-patrol",
    "surveying",
    "autonomous-ground",
    "fires-support",
    "sof-comms",
    "cas-coordination",
    "ground-maneuver",
)

# Default equipment profile per military mission type (equipment ids from
# app.models.equipment.EQUIPMENT). The UI pre-selects these; operators can
# adjust. Civilian mission types default to empty (equipment readout is
# opt-in there).
DEFAULT_EQUIPMENT_BY_MISSION_TYPE: dict[str, tuple[str, ...]] = {
    "fires-support": ("gps_single_freq", "counter_battery_radar", "hf_radio", "sincgars_fm"),
    "sof-comms": ("hf_radio", "uhf_satcom", "sincgars_fm", "ehf_satcom"),
    "cas-coordination": ("gps_single_freq", "uhf_satcom", "sincgars_fm"),
    "ground-maneuver": ("gps_single_freq", "sincgars_fm"),
    "defense-patrol": ("gps_single_freq", "uhf_satcom", "sincgars_fm"),
    "uav": ("uas_group1", "gps_single_freq"),
}

GNSS_DEPENDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high", "rtk")
COMMS_DEPENDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
RISK_TOLERANCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
TIME_WINDOWS: tuple[str, ...] = ("now", "next-1h", "next-6h", "next-24h")


@dataclass
class MissionWaypoint:
    """A single waypoint in the mission. Multiple = route assessment."""

    name: str
    lat: float
    lon: float


@dataclass
class MissionRequest:
    """Operator-language mission profile. The Mission Planner sends this."""

    mission_type: str = "uav"
    gnss_dependence: str = "medium"  # low | medium | high | rtk
    comms_dependence: str = "medium"  # low | medium | high
    risk_tolerance: str = "medium"  # low | medium | high
    waypoints: list[MissionWaypoint] = field(default_factory=list)
    time_window: str = "now"  # now | next-1h | next-6h | next-24h
    callsign: str = ""
    # Equipment ids from app.models.equipment.EQUIPMENT. Empty = no
    # equipment-level readout (civilian missions may not need one).
    equipment: list[str] = field(default_factory=list)


# ── Mapping tables ───────────────────────────────────────────────────────────
# Single source of truth for how each operator-language input projects onto
# the decision engine. Edit here when adding a new mission type.

# Asset type to feed into the engine, by GNSS dependence level.
ASSET_BY_GNSS_DEPENDENCE: dict[str, str] = {
    "low": "GPS_INS",  # INS available — receiver hardware least limiting
    "medium": "GPS_L1L2",  # primary nav, dual-frequency civilian
    "high": "GPS_L1L5",  # sole-source, modern survey-grade
    "rtk": "GPS_L1L5",  # same hardware; tightness lives in the score, not engine
}

# Base criticality (1=lowest, 5=highest) by mission type. Risk-tolerance
# adjusts up/down from here.
BASE_CRITICALITY_BY_MISSION_TYPE: dict[str, int] = {
    "uav": 3,
    "bvlos": 4,  # higher — autonomous, no human pilot in loop
    "precision-ag": 3,
    "maritime": 4,  # higher — SAR / long-haul HF dependence
    "defense-patrol": 4,
    "surveying": 3,
    "autonomous-ground": 3,
    "fires-support": 5,  # coordinate error → rounds off target
    "sof-comms": 5,  # comms loss is mission failure
    "cas-coordination": 5,  # danger-close coordination
    "ground-maneuver": 3,
}

# How risk tolerance adjusts the criticality (clamped 1..5).
RISK_TOLERANCE_CRIT_DELTA: dict[str, int] = {
    "low": +1,  # zero-tolerance for surprises → bump threshold up
    "medium": 0,
    "high": -1,
}

# Per-mission GPS-error tolerance (metres). The reliability score interpolates
# from 100 (zero error) to 0 (at GPS_ERROR_TOLERANCE_M[gnss_dep] × 3).
# RTK is tight by design.
GPS_ERROR_TOLERANCE_M: dict[str, float] = {
    "low": 25.0,  # INS-backed — coarse position OK
    "medium": 10.0,  # primary nav — most civilian needs
    "high": 5.0,  # sole-source — sub-5m matters
    "rtk": 0.5,  # cm-grade RTK — half-metre is already degraded
}


# ── Output dataclasses ───────────────────────────────────────────────────────

# Mission-level verdicts. Distinct from the engine's RouteAction so the
# operator language stays stable even if the engine vocabulary changes.
MISSION_RISK_CLEAR = "CLEAR"
MISSION_RISK_CAUTION = "CAUTION"
MISSION_RISK_HIGH = "HIGH_RISK"
MISSION_RISK_DELAY = "DELAY"


@dataclass
class GnssReliability:
    """GNSS reliability summary, mission-aware."""

    score: float  # 0..100, higher = better
    label: str  # GOOD | DEGRADED | POOR | UNRELIABLE
    worst_error_m: float
    tolerance_m: float
    asset_type: str
    affected_legs: int  # number of legs above tolerance
    total_legs: int


@dataclass
class CommsRisk:
    """Comms risk summary, mission-aware."""

    score: float  # 0..100, higher = riskier
    label: str  # LOW | MODERATE | HIGH | CRITICAL
    hf_viable_legs: int
    total_legs: int
    pca_active: bool
    fallback_hint: str  # operator-readable suggestion


@dataclass
class DataQuality:
    """Confidence / freshness indicator surfaced to the operator."""

    label: str  # HIGH | MEDIUM | LOW
    score: float  # 0..1 (from engine confidence)
    notes: list[str]  # human-readable concerns (stale, missing feed, …)
    completeness: float | None  # 0..1


@dataclass
class MissionAssessment:
    """The full operator-facing result. Renders 1-1 to the UI cards."""

    mission_risk_level: str  # CLEAR | CAUTION | HIGH_RISK | DELAY
    mission_risk_summary: str  # e.g. "CLEAR · Proceed"
    plain_explanation: str
    recommended_actions: list[str]
    gnss: GnssReliability
    comms: CommsRisk
    data_quality: DataQuality
    inputs_echo: dict[str, Any]
    source_labels: dict[str, str]  # which sources are measured/modeled/heuristic
    raw_decision: dict[str, Any]  # underlying decision-engine response
    generated_at: str
    # Equipment-level readout (app.models.equipment.EquipmentAssessment
    # .to_dict()), present when the request named equipment.
    equipment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_risk_level": self.mission_risk_level,
            "mission_risk_summary": self.mission_risk_summary,
            "plain_explanation": self.plain_explanation,
            "recommended_actions": self.recommended_actions,
            "gnss": asdict(self.gnss),
            "comms": asdict(self.comms),
            "data_quality": asdict(self.data_quality),
            "equipment": self.equipment,
            "inputs_echo": self.inputs_echo,
            "source_labels": self.source_labels,
            "raw_decision": self.raw_decision,
            "generated_at": self.generated_at,
        }


# ── Mapping ──────────────────────────────────────────────────────────────────


def map_to_platform_kwargs(req: MissionRequest) -> dict[str, Any]:
    """Translate the mission profile into PlatformInput kwargs.

    Returns a plain dict (not the dataclass) so callers can layer on
    system_dependencies if they want; routes_v3 layer does the dataclass
    construction.
    """
    asset_type = ASSET_BY_GNSS_DEPENDENCE.get(req.gnss_dependence, "GPS_L1L2")
    crit_base = BASE_CRITICALITY_BY_MISSION_TYPE.get(req.mission_type, 3)
    delta = RISK_TOLERANCE_CRIT_DELTA.get(req.risk_tolerance, 0)
    criticality = max(1, min(5, crit_base + delta))
    return {"asset_type": asset_type, "criticality": criticality}


# ── Scoring ──────────────────────────────────────────────────────────────────


def gnss_reliability_from_waypoints(
    waypoints: list[dict],
    gnss_dependence: str,
    asset_type: str,
) -> GnssReliability:
    """Compute the GNSS reliability score, mission-aware.

    Score is 100 at zero error, falls linearly to 0 at 3× tolerance.
    The tolerance comes from the *mission's* GNSS dependence, not from the
    engine — that's how a 0.5m error reads as DEGRADED for an RTK ag mission
    but GOOD for a defense patrol.
    """
    tolerance = GPS_ERROR_TOLERANCE_M.get(gnss_dependence, 10.0)
    if not waypoints:
        # No waypoints → nothing to score; report unreliable.
        return GnssReliability(
            score=0.0,
            label="UNRELIABLE",
            worst_error_m=0.0,
            tolerance_m=tolerance,
            asset_type=asset_type,
            affected_legs=0,
            total_legs=0,
        )
    errors = [float(w.get("gps_error_m") or 0.0) for w in waypoints]
    worst = max(errors)
    score = max(0.0, min(100.0, 100.0 - (worst / (tolerance * 3.0)) * 100.0))
    label = "GOOD" if score >= 80 else "DEGRADED" if score >= 55 else "POOR" if score >= 30 else "UNRELIABLE"
    affected = sum(1 for e in errors if e > tolerance)
    return GnssReliability(
        score=round(score, 1),
        label=label,
        worst_error_m=round(worst, 2),
        tolerance_m=tolerance,
        asset_type=asset_type,
        affected_legs=affected,
        total_legs=len(waypoints),
    )


def comms_risk_from_waypoints(
    waypoints: list[dict],
    comms_dependence: str,
) -> CommsRisk:
    """Compute the comms risk score, mission-aware.

    Score is 0 (no risk) to 100 (critical risk). High-dependence missions
    escalate even when only one leg is degraded; low-dependence missions
    only escalate when most legs are degraded.
    """
    n = len(waypoints) or 1
    hf_bad = sum(1 for w in waypoints if w.get("hf_viable") is False)
    pca_any = any(w.get("pca_active") for w in waypoints)
    frac_bad = hf_bad / n

    # Base score scales with fraction degraded; multiplier depends on
    # mission's reliance on continuous comms.
    if comms_dependence == "high":
        base = frac_bad * 100.0
        # Even one degraded leg matters when comms must be continuous
        if hf_bad >= 1:
            base = max(base, 35.0)
    elif comms_dependence == "medium":
        base = frac_bad * 80.0
    else:  # low
        base = frac_bad * 50.0

    if pca_any:
        # Polar cap absorption: structural HF outage, can last hours
        base += 25.0

    score = round(min(100.0, base), 1)
    label = "CRITICAL" if score >= 70 else "HIGH" if score >= 40 else "MODERATE" if score >= 15 else "LOW"

    hint = "Standard HF or SATCOM operations."
    if pca_any:
        hint = "PCA active — HF will be unreliable in high-lat regions. Switch to SATCOM-Ka or UHF."
    elif hf_bad and comms_dependence == "high":
        hint = "HF degraded on at least one leg — pre-authorise SATCOM fallback before launch."
    elif hf_bad:
        hint = "HF degradation on some legs — verify SATCOM link before relying on long-haul HF."

    return CommsRisk(
        score=score,
        label=label,
        hf_viable_legs=n - hf_bad,
        total_legs=n,
        pca_active=pca_any,
        fallback_hint=hint,
    )


def derive_mission_risk(
    engine_action: str,
    gnss: GnssReliability,
    comms: CommsRisk,
    gnss_dependence: str,
    comms_dependence: str,
) -> tuple[str, str]:
    """Mission-aware verdict.

    Starts from the engine's RouteAction and escalates when the mission's
    dependence levels make the engine's threshold too lenient.
    Returns (mission_risk_level, mission_risk_summary).
    """
    # Base mapping from engine action
    base = {
        "GO": MISSION_RISK_CLEAR,
        "ADVISORY": MISSION_RISK_CAUTION,
        "CAUTION": MISSION_RISK_HIGH,
        "NO_GO": MISSION_RISK_DELAY,
    }.get((engine_action or "GO").upper(), MISSION_RISK_CLEAR)

    # Rank order so we can take a max
    rank = {
        MISSION_RISK_CLEAR: 0,
        MISSION_RISK_CAUTION: 1,
        MISSION_RISK_HIGH: 2,
        MISSION_RISK_DELAY: 3,
    }
    level = base

    def escalate(to: str) -> None:
        nonlocal level
        if rank[to] > rank[level]:
            level = to

    # GNSS-driven escalations
    if gnss.label == "UNRELIABLE":
        escalate(MISSION_RISK_DELAY if gnss_dependence in {"high", "rtk"} else MISSION_RISK_HIGH)
    elif gnss.label == "POOR":
        escalate(MISSION_RISK_HIGH if gnss_dependence in {"high", "rtk"} else MISSION_RISK_CAUTION)
    elif gnss.label == "DEGRADED":
        escalate(MISSION_RISK_CAUTION)

    # Comms-driven escalations
    if comms.label == "CRITICAL":
        escalate(MISSION_RISK_DELAY if comms_dependence == "high" else MISSION_RISK_HIGH)
    elif comms.label == "HIGH":
        escalate(MISSION_RISK_HIGH if comms_dependence == "high" else MISSION_RISK_CAUTION)
    elif comms.label == "MODERATE":
        escalate(MISSION_RISK_CAUTION if comms_dependence in {"medium", "high"} else MISSION_RISK_CLEAR)

    summary = {
        MISSION_RISK_CLEAR: "CLEAR · Proceed",
        MISSION_RISK_CAUTION: "CAUTION · Monitor and adapt",
        MISSION_RISK_HIGH: "HIGH RISK · Mitigate before launch",
        MISSION_RISK_DELAY: "DELAY · Do not proceed",
    }[level]
    return level, summary


def derive_data_quality(decision: dict[str, Any]) -> DataQuality:
    """Map the engine's confidence object into operator-facing data quality."""
    conf = decision.get("confidence") or {}
    raw_label = (conf.get("label") or "MEDIUM").upper()
    score = float(conf.get("score") or 0.5)
    completeness = conf.get("data_completeness")
    notes: list[str] = []
    if conf.get("stale_data"):
        notes.append("Stale observation (NOAA data > 10 minutes old) — verify before acting.")
    feeds_unavail = (decision.get("provenance") or {}).get("feeds_unavailable") or []
    if feeds_unavail:
        notes.append(f"Feeds unavailable: {', '.join(feeds_unavail)}.")
    if completeness is not None and completeness < 0.9:
        notes.append(f"Data completeness {round(completeness * 100)}%.")

    label = (
        "HIGH"
        if raw_label.startswith("HIGH")
        else "LOW"
        if raw_label.startswith("LOW") or raw_label.startswith("VERY")
        else "MEDIUM"
    )
    return DataQuality(
        label=label,
        score=round(score, 3),
        notes=notes,
        completeness=round(completeness, 3) if completeness is not None else None,
    )


def derive_recommended_actions(
    decision: dict[str, Any],
    gnss: GnssReliability,
    comms: CommsRisk,
    gnss_dependence: str,
    comms_dependence: str,
) -> list[str]:
    """Mission-aware recommended actions. Layers mission context on top of
    whatever the engine already returned."""
    recs: list[str] = list(decision.get("recommended_actions") or [])

    if gnss.label in {"POOR", "UNRELIABLE"} and gnss_dependence in {"high", "rtk"}:
        if gnss_dependence == "rtk":
            recs.append(
                f"RTK fix integrity at risk — worst-leg GPS error {gnss.worst_error_m} m "
                f"vs tolerance {gnss.tolerance_m} m. Consider rescheduling RTK-critical passes."
            )
        else:
            recs.append(
                "GPS sole-source nav recommended only with INS backup — " "verify backup nav before proceeding."
            )

    if comms.fallback_hint and comms.fallback_hint not in recs:
        recs.append(comms.fallback_hint)

    # If the engine returned an action_sentence and nothing else, expose it
    if not recs and decision.get("action_sentence"):
        recs.append(decision["action_sentence"])

    return recs


def build_source_labels() -> dict[str, str]:
    """Per-source provenance tagging. Used by the UI to show measured /
    modeled / heuristic chips on every input."""
    return {
        "noaa_swpc": "measured",  # live NOAA observation
        "nasa_omni": "measured",  # live NASA OMNI
        "glotec_tec": "measured",  # GloTEC TEC grid
        "kp_forecast_24h": "modeled",  # NOAA 3-day forecast
        "klobuchar_gps_model": "modeled",  # ITU model
        "ccir_888_hf": "modeled",  # CCIR-888 HF absorption
        "bailey_pca": "modeled",  # Bailey polar cap absorption
        "nakagami_satcom": "modeled",  # Nakagami-m fading
        "route_risk_engine": "heuristic",  # rule-based composition
        "ml_kp_forecaster": "modeled",  # ridge regression
    }


# ── End-to-end assess ────────────────────────────────────────────────────────


def assess_mission(
    req: MissionRequest,
    route_decision: dict[str, Any],
    equipment_assessment: dict[str, Any] | None = None,
) -> MissionAssessment:
    """Glue: take the engine's route-decision output + the mission profile,
    return the operator-facing MissionAssessment.

    The route-decision dict is what the engine already produces (the same
    shape POST /api/v2/route-decision returns). The HTTP layer (routes_v3)
    is responsible for actually calling the engine and passing its dict here,
    and likewise for running the equipment rule library against live drivers
    and passing its to_dict() as equipment_assessment. This signature keeps
    the mission module testable with pure fixtures — no network needed.
    """
    waypoints = route_decision.get("waypoints") or []
    asset_type = ASSET_BY_GNSS_DEPENDENCE.get(req.gnss_dependence, "GPS_L1L2")

    gnss = gnss_reliability_from_waypoints(waypoints, req.gnss_dependence, asset_type)
    comms = comms_risk_from_waypoints(waypoints, req.comms_dependence)
    quality = derive_data_quality(route_decision)
    level, summary = derive_mission_risk(
        route_decision.get("action", "GO"),
        gnss,
        comms,
        req.gnss_dependence,
        req.comms_dependence,
    )
    recs = derive_recommended_actions(route_decision, gnss, comms, req.gnss_dependence, req.comms_dependence)

    inputs_echo = {
        "mission_type": req.mission_type,
        "gnss_dependence": req.gnss_dependence,
        "comms_dependence": req.comms_dependence,
        "risk_tolerance": req.risk_tolerance,
        "time_window": req.time_window,
        "callsign": req.callsign,
        "waypoint_count": len(req.waypoints),
        "equipment": list(req.equipment),
        "platform_kwargs": map_to_platform_kwargs(req),
    }

    explanation = route_decision.get("action_sentence") or "No explanation provided by engine."

    return MissionAssessment(
        mission_risk_level=level,
        mission_risk_summary=summary,
        plain_explanation=explanation,
        recommended_actions=recs,
        gnss=gnss,
        comms=comms,
        data_quality=quality,
        inputs_echo=inputs_echo,
        source_labels=build_source_labels(),
        raw_decision=route_decision,
        generated_at=datetime.now(timezone.utc).isoformat(),
        equipment=equipment_assessment,
    )
