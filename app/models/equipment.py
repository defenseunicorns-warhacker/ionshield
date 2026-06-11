"""
Equipment-effect rule library — doctrine-cited space-weather impact rules
for named military equipment.

WarHacker P0-1. The briefing-book MVP definition of done is explicit:
**5 equipment types × 3 weather states = 15 rules**, every rule auditable
and citable. This module is deliberately rule-based, NOT model-driven:
explainability is a DoD procurement criterion (DoD AI Ethical Principles),
and "a commander can read the rule that produced this recommendation" is
the defended answer to "why don't you use AI for this?".

Relationship to the physics engine (app/models/impact, app/models/decision):
the physics engine computes continuous quantities (GPS error in metres, HF
viability, PCA state) from measured drivers. This library answers a
different question — "what does that mean for an AN/TPQ-53 crew?" — by
mapping a coarse weather state onto named equipment with doctrine-cited
effects and actions. The two layers agree by construction (both key off the
same Kp / X-ray / proton drivers); the rule library leads the operator
conversation, the physics backs it under questioning.

Doctrine basis: ALSSA Center, "True Impacts of Space Weather on a Ground
Force" (multi-service tactics pamphlet), which documents GPS errors up to
100 m horizontal / 200 m vertical, multi-hour HF blackouts, "broken and
unreadable" UHF transmissions, and AN/TPQ-53 detection failures in the
July 23, 2012 storm training scenario. NOAA SWPC R/S/G scale descriptions
supply the flare / proton / geomagnetic thresholds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# ── Weather states ───────────────────────────────────────────────────────────
# Three coarse states, per the rule-library design. Thresholds follow NOAA
# scales: G1 storm begins at Kp 5, G3 at Kp 7; M-class flare = R1-R2 radio
# blackout, X-class = R3+; S2 proton event at 100 pfu (PCA-relevant).

WX_QUIET = "QUIET"
WX_MODERATE = "MODERATE"
WX_SEVERE = "SEVERE"

WEATHER_STATES: tuple[str, ...] = (WX_QUIET, WX_MODERATE, WX_SEVERE)

# Risk levels per equipment per state (traffic-light, operator-standard).
RISK_GREEN = "GREEN"
RISK_AMBER = "AMBER"
RISK_RED = "RED"

# NOTE on probabilities: outputs stay probabilistic rather than binary
# (briefing Q13), but every number must be real. The likelihood text is
# built from (a) the observed drivers that produced the classification and
# (b) NOAA SWPC forecaster-issued scale probabilities (noaa-scales.json)
# when available. No invented percentages, ever — when the forecast feed
# is absent, the basis statement stands alone.


def classify_weather_state(
    kp: float,
    xray_flux_wm2: float | None = None,
    proton_flux_10mev_pfu: float | None = None,
) -> str:
    """Bucket current drivers into QUIET / MODERATE / SEVERE.

    Any single driver crossing its severe threshold makes the state SEVERE
    (effects are independent hazards, not an average): Kp ≥ 7 (G3), X-class
    flare (≥ 1e-4 W/m², R3), or S2+ proton event (≥ 100 pfu, PCA risk).
    Moderate: Kp ≥ 5 (G1), M-class flare (≥ 1e-5 W/m²), or S1 protons
    (≥ 10 pfu).
    """
    xray = xray_flux_wm2 or 0.0
    proton = proton_flux_10mev_pfu or 0.0
    if kp >= 7.0 or xray >= 1e-4 or proton >= 100.0:
        return WX_SEVERE
    if kp >= 5.0 or xray >= 1e-5 or proton >= 10.0:
        return WX_MODERATE
    return WX_QUIET


# ── Equipment catalog ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EquipmentProfile:
    """A named equipment class an operator can put in a mission profile."""

    id: str
    display_name: str
    nomenclature: str  # representative fielded systems
    affected: bool  # False → in the always-unaffected set
    why_unaffected: str = ""  # only for affected=False


EQUIPMENT: dict[str, EquipmentProfile] = {
    "gps_single_freq": EquipmentProfile(
        id="gps_single_freq",
        display_name="GPS — single-frequency receiver",
        nomenclature="DAGR, Group 1 UAS nav, commercial L1 receivers",
        affected=True,
    ),
    "hf_radio": EquipmentProfile(
        id="hf_radio",
        display_name="HF radio (3–30 MHz, skywave)",
        nomenclature="AN/PRC-150, AN/PRC-160",
        affected=True,
    ),
    "uhf_satcom": EquipmentProfile(
        id="uhf_satcom",
        display_name="UHF SATCOM",
        nomenclature="AN/PRC-117G, AN/PRC-152 (SATCOM mode)",
        affected=True,
    ),
    "counter_battery_radar": EquipmentProfile(
        id="counter_battery_radar",
        display_name="Counter-battery radar",
        nomenclature="AN/TPQ-53, AN/TPQ-50",
        affected=True,
    ),
    "uas_group1": EquipmentProfile(
        id="uas_group1",
        display_name="Group 1 UAS (GPS-dependent flight)",
        nomenclature="RQ-11 Raven, Skydio X2D, sUAS quadcopters",
        affected=True,
    ),
    # ── Always-unaffected set ───────────────────────────────────────────────
    # Knowing what is NOT affected is as valuable as knowing what is: it
    # gives the operator an immediate fallback instead of troubleshooting
    # working equipment.
    "sincgars_fm": EquipmentProfile(
        id="sincgars_fm",
        display_name="SINCGARS FM (VHF line-of-sight)",
        nomenclature="AN/PRC-119, RT-1523",
        affected=False,
        why_unaffected=(
            "30–88 MHz line-of-sight propagation never transits the "
            "ionosphere (60–1000 km altitude) — geomagnetic and ionospheric "
            "disturbances cannot reach it. Range-limited, but reliable."
        ),
    ),
    "ehf_satcom": EquipmentProfile(
        id="ehf_satcom",
        display_name="EHF SATCOM",
        nomenclature="SMART-T (AN/TSC-154), AEHF terminals",
        affected=False,
        why_unaffected=(
            "EHF (30+ GHz) passes through the ionosphere at a frequency "
            "high enough that disturbance-induced refraction and "
            "scintillation effects are negligible."
        ),
    ),
}

AFFECTED_EQUIPMENT_IDS: tuple[str, ...] = tuple(e.id for e in EQUIPMENT.values() if e.affected)
UNAFFECTED_EQUIPMENT_IDS: tuple[str, ...] = tuple(e.id for e in EQUIPMENT.values() if not e.affected)


# ── Rule library: 5 equipment × 3 weather states = 15 rules ──────────────────

_ALSSA = 'ALSSA, "True Impacts of Space Weather on a Ground Force"'
_NOAA_SCALES = "NOAA SWPC R/S/G space weather scales"


@dataclass(frozen=True)
class EquipmentRule:
    """One auditable rule: (equipment, weather state) → effect + action."""

    equipment_id: str
    weather_state: str
    risk: str  # GREEN | AMBER | RED
    effect: str  # what the operator will experience
    action: str  # what the operator should do
    citation: str  # doctrine / standard the rule derives from


RULES: dict[tuple[str, str], EquipmentRule] = {
    (r.equipment_id, r.weather_state): r
    for r in [
        # ── GPS single-frequency ───────────────────────────────────────────
        EquipmentRule(
            "gps_single_freq",
            WX_QUIET,
            RISK_GREEN,
            "Normal accuracy — typical position error ≤ 5 m.",
            "No mitigation required.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "gps_single_freq",
            WX_MODERATE,
            RISK_AMBER,
            "Uncorrected ionospheric delay on single-frequency receivers — position errors of 10–50 m possible, worst near dawn/dusk and at high latitude.",
            "Cross-check coordinates against a second source before precision use; widen waypoint/target tolerance.",
            _ALSSA,
        ),
        EquipmentRule(
            "gps_single_freq",
            WX_SEVERE,
            RISK_RED,
            "Position errors up to 100 m horizontal / 200 m vertical documented under severe geomagnetic storming.",
            "Do not use single-frequency GPS coordinates for precision fires or landing zones; verify by map/terrain or survey-grade receiver.",
            _ALSSA,
        ),
        # ── HF radio ───────────────────────────────────────────────────────
        EquipmentRule(
            "hf_radio",
            WX_QUIET,
            RISK_GREEN,
            "Normal skywave propagation on published frequency plans.",
            "No mitigation required.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "hf_radio",
            WX_MODERATE,
            RISK_AMBER,
            "Degraded propagation and short fadeouts (10–30 min) on sunlit paths following M-class flares; usable windows between events.",
            "Schedule HF traffic in windows; brief PACE alternates; expect retransmissions.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "hf_radio",
            WX_SEVERE,
            RISK_RED,
            "HF blackout from minutes to hours (X-class flare dayside; polar cap absorption can blank polar routes for days).",
            "Shift traffic to SINCGARS FM (line-of-sight) or EHF SATCOM now; do not plan missions dependent on HF until conditions subside.",
            _ALSSA,
        ),
        # ── UHF SATCOM ─────────────────────────────────────────────────────
        EquipmentRule(
            "uhf_satcom",
            WX_QUIET,
            RISK_GREEN,
            "Normal link quality.",
            "No mitigation required.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "uhf_satcom",
            WX_MODERATE,
            RISK_AMBER,
            "Scintillation-induced dropouts, strongest in equatorial and auroral zones around local evening.",
            "Expect intermittent links; keep messages short; pre-stage critical traffic outside scintillation hours.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "uhf_satcom",
            WX_SEVERE,
            RISK_RED,
            'Transmissions "broken and unreadable" per the July 23, 2012 storm training scenario.',
            "Treat UHF SATCOM as unavailable for C2; move to EHF (SMART-T) or line-of-sight FM relay.",
            _ALSSA,
        ),
        # ── Counter-battery radar ──────────────────────────────────────────
        EquipmentRule(
            "counter_battery_radar",
            WX_QUIET,
            RISK_GREEN,
            "Normal probability of detection.",
            "No mitigation required.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "counter_battery_radar",
            WX_MODERATE,
            RISK_AMBER,
            "Reduced probability of detection and elevated clutter/false tracks during ionospheric disturbance and solar radio noise.",
            "Increase crew scrutiny of track quality; corroborate acquisitions with a second sensor where possible.",
            _ALSSA,
        ),
        EquipmentRule(
            "counter_battery_radar",
            WX_SEVERE,
            RISK_RED,
            'May fail to detect incoming rounds — the documented scenario records the AN/TPQ-53 "not picking up any artillery rounds even though the guns were firing."',
            "Do not rely on counter-battery radar as sole acquisition; brief maneuver units that counterfire response may be degraded.",
            _ALSSA,
        ),
        # ── Group 1 UAS ────────────────────────────────────────────────────
        EquipmentRule(
            "uas_group1",
            WX_QUIET,
            RISK_GREEN,
            "Normal GPS position hold and waypoint navigation.",
            "No mitigation required.",
            _NOAA_SCALES,
        ),
        EquipmentRule(
            "uas_group1",
            WX_MODERATE,
            RISK_AMBER,
            "Degraded position hold; waypoint navigation may wander; autonomous return-to-home accuracy reduced.",
            "Widen waypoint tolerance; keep the aircraft in visual line of sight where mission allows; monitor RTH behavior.",
            _ALSSA,
        ),
        EquipmentRule(
            "uas_group1",
            WX_SEVERE,
            RISK_RED,
            "Hover and loiter operations high risk — potential loss of position hold and flyaway under GPS errors of 50–100 m.",
            "Advance or delay the flight window to quieter conditions; if the window cannot move, fly manual control throughout and disable autonomous RTH.",
            _ALSSA,
        ),
    ]
}

assert len(RULES) == len(AFFECTED_EQUIPMENT_IDS) * len(WEATHER_STATES), "rule library must stay complete"


# ── Likelihood (real data only) ──────────────────────────────────────────────

# NOAA scale names for the basis statement: Kp → G-scale per NOAA definition.
_KP_TO_G = [(9.0, "G5"), (8.0, "G4"), (7.0, "G3"), (6.0, "G2"), (5.0, "G1")]


def _g_label(kp: float) -> str:
    for threshold, label in _KP_TO_G:
        if kp >= threshold:
            return label
    return "below storm level"


def build_likelihood(
    state: str,
    kp: float,
    xray_flux_wm2: float | None = None,
    proton_flux_10mev_pfu: float | None = None,
    noaa_scales: dict | None = None,
) -> str:
    """Factual likelihood statement: observed basis + NOAA forecast probabilities.

    Every number is measured (drivers) or forecaster-issued (noaa-scales.json
    via app.data.noaa.get_noaa_scales()). When the scales feed is unavailable,
    the basis statement stands alone — nothing is invented.
    """
    basis = [f"observed Kp {kp:.1f} ({_g_label(kp)})"]
    if xray_flux_wm2 is not None and xray_flux_wm2 >= 1e-5:
        cls = "X" if xray_flux_wm2 >= 1e-4 else "M"
        basis.append(f"GOES X-ray flux {xray_flux_wm2:.1e} W/m² ({cls}-class)")
    if proton_flux_10mev_pfu is not None and proton_flux_10mev_pfu >= 10.0:
        s = "S2+" if proton_flux_10mev_pfu >= 100.0 else "S1"
        basis.append(f"proton flux {proton_flux_10mev_pfu:.0f} pfu ({s})")
    text = f"{state} — basis: " + ", ".join(basis) + "."

    day1 = None
    if noaa_scales and noaa_scales.get("forecast"):
        day1 = next((d for d in noaa_scales["forecast"] if d.get("day") == 1), None)
    if day1:
        probs = []
        if day1.get("r_minor_prob") is not None:
            probs.append(f"R1–R2 radio blackout {day1['r_minor_prob']:.0f}%")
        if day1.get("r_major_prob") is not None:
            probs.append(f"R3+ {day1['r_major_prob']:.0f}%")
        if day1.get("s1_prob") is not None:
            probs.append(f"S1+ radiation storm {day1['s1_prob']:.0f}%")
        if day1.get("g_predicted") is not None:
            probs.append(f"predicted G{day1['g_predicted']:d}")
        if probs:
            text += " NOAA day-1 forecast: " + ", ".join(probs) + "."
    return text


# ── Evaluation ───────────────────────────────────────────────────────────────

_RISK_ORDER = {RISK_GREEN: 0, RISK_AMBER: 1, RISK_RED: 2}


@dataclass
class EquipmentFinding:
    """Evaluated rule for one piece of equipment in the current state."""

    equipment_id: str
    display_name: str
    nomenclature: str
    risk: str
    effect: str
    action: str
    citation: str


@dataclass
class EquipmentAssessment:
    """Full equipment-level readout for a mission profile."""

    weather_state: str
    likelihood: str
    findings: list[EquipmentFinding]  # affected equipment, worst first
    unaffected: list[dict[str, str]]  # always-available fallbacks + why
    worst_risk: str  # GREEN | AMBER | RED across findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "weather_state": self.weather_state,
            "likelihood": self.likelihood,
            "findings": [asdict(f) for f in self.findings],
            "unaffected": self.unaffected,
            "worst_risk": self.worst_risk,
        }


def evaluate_equipment(
    equipment_ids: list[str],
    kp: float,
    xray_flux_wm2: float | None = None,
    proton_flux_10mev_pfu: float | None = None,
    noaa_scales: dict | None = None,
) -> EquipmentAssessment:
    """Run the rule library for the given equipment under current drivers.

    Unknown equipment ids are ignored (the API layer validates; this keeps
    the engine total). Equipment from the unaffected set is reported in
    `unaffected` with the physical reason, regardless of weather state —
    surfacing the fallback is the point.
    """
    state = classify_weather_state(kp, xray_flux_wm2, proton_flux_10mev_pfu)

    findings: list[EquipmentFinding] = []
    unaffected: list[dict[str, str]] = []
    for eq_id in equipment_ids:
        profile = EQUIPMENT.get(eq_id)
        if profile is None:
            continue
        if not profile.affected:
            unaffected.append(
                {
                    "equipment_id": profile.id,
                    "display_name": profile.display_name,
                    "nomenclature": profile.nomenclature,
                    "status": "UNAFFECTED",
                    "why": profile.why_unaffected,
                }
            )
            continue
        rule = RULES[(eq_id, state)]
        findings.append(
            EquipmentFinding(
                equipment_id=eq_id,
                display_name=profile.display_name,
                nomenclature=profile.nomenclature,
                risk=rule.risk,
                effect=rule.effect,
                action=rule.action,
                citation=rule.citation,
            )
        )

    findings.sort(key=lambda f: _RISK_ORDER[f.risk], reverse=True)
    worst = max((f.risk for f in findings), key=lambda r: _RISK_ORDER[r], default=RISK_GREEN)

    return EquipmentAssessment(
        weather_state=state,
        likelihood=build_likelihood(state, kp, xray_flux_wm2, proton_flux_10mev_pfu, noaa_scales),
        findings=findings,
        unaffected=unaffected,
        worst_risk=worst,
    )
