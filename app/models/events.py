"""
Event detection — rule-based + ML-stub classification of space-weather events.

The detector consumes a *window* of recent observations (the most recent N
samples from noaa_snapshots) plus the latest fused snapshot, and emits
`Event` records with a lifecycle:

  ONSET  — driver crossed a threshold; this is a new event
  PEAK   — driver still above threshold; record current peak value
  ENDED  — driver fell back below the deactivation threshold

The detector is *stateful via the database*: it looks up the open events for
each (event_type, region_id) pair to decide whether the current sample
extends an existing event or opens a new one. There is no in-process state,
so multiple workers or restarts behave correctly.

Severity strings follow NOAA scales:
  Geomagnetic (Kp): G1=Kp5, G2=Kp6, G3=Kp7, G4=Kp8, G5=Kp9
  SEP    (proton): S1=10pfu S2=100 S3=1000 S4=10000 S5=100000
  Flare  (X-ray): R1=M1, R2=M5, R3=X1, R4=X10, R5=X20

The ML stub provides a clean interface for a future trained classifier —
right now it returns the rule-based decision with a `classifier="ml-stub"`
marker so the contract can be exercised end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .ontology import Driver, EventType, FusedObservation


class EventState(str, Enum):
    ONSET = "ONSET"
    PEAK = "PEAK"
    ENDED = "ENDED"


@dataclass
class Event:
    event_type: EventType
    state: EventState
    severity: str
    region_id: str
    t_onset: datetime
    t_peak: datetime | None
    t_end: datetime | None
    driver: Driver
    peak_value: float | None
    trigger_value: float
    threshold_value: float
    rationale: str = ""
    classifier: str = "rule"
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type.value,
            "state": self.state.value,
            "severity": self.severity,
            "region_id": self.region_id,
            "t_onset": self.t_onset.astimezone(timezone.utc).isoformat(),
            "t_peak": self.t_peak.astimezone(timezone.utc).isoformat() if self.t_peak else None,
            "t_end": self.t_end.astimezone(timezone.utc).isoformat() if self.t_end else None,
            "driver": self.driver.value,
            "peak_value": self.peak_value,
            "trigger_value": self.trigger_value,
            "threshold_value": self.threshold_value,
            "rationale": self.rationale,
            "classifier": self.classifier,
            "confidence": self.confidence,
        }


# ── Severity helpers ─────────────────────────────────────────────────────────


def kp_severity(kp: float) -> str:
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
    return "NA"


def proton_severity(pfu: float) -> str:
    if pfu >= 100_000:
        return "S5"
    if pfu >= 10_000:
        return "S4"
    if pfu >= 1_000:
        return "S3"
    if pfu >= 100:
        return "S2"
    if pfu >= 10:
        return "S1"
    return "NA"


def xray_severity(flux: float) -> str:
    """NOAA R-scale derived from peak X-ray flux."""
    if flux >= 2e-3:
        return "R5"
    if flux >= 1e-3:
        return "R4"
    if flux >= 1e-4:
        return "R3"
    if flux >= 5e-5:
        return "R2"
    if flux >= 1e-5:
        return "R1"
    return "NA"


# ── Rule-based detection ─────────────────────────────────────────────────────


@dataclass
class Rule:
    """A single rule mapping a driver crossing → event."""

    event_type: EventType
    driver: Driver
    on_threshold: float  # crosses this → ONSET
    off_threshold: float  # falls below this → ENDED (hysteresis)
    severity_fn: callable
    rationale: str = ""

    def trigger_value(self, obs: FusedObservation) -> float:
        return _driver_value(obs, self.driver)


def _driver_value(obs: FusedObservation, driver: Driver) -> float:
    return {
        Driver.KP: obs.kp_index,
        Driver.BZ: obs.bz_nt,
        Driver.WIND_SPEED: obs.wind_speed_km_s,
        Driver.XRAY_FLUX: obs.xray_flux_wm2,
        Driver.PROTON_FLUX: obs.proton_flux_10mev_pfu,
        Driver.F107: obs.f107_sfu,
        Driver.TEC: obs.tec_tecu,
        Driver.TEC_ANOMALY: obs.tec_anomaly_tecu,
    }[driver]


RULES: tuple[Rule, ...] = (
    Rule(
        event_type=EventType.GEOMAG_MAIN,
        driver=Driver.KP,
        on_threshold=5.0,
        off_threshold=4.0,
        severity_fn=kp_severity,
        rationale="Kp ≥ 5 (G1) — geomagnetic storm main phase",
    ),
    Rule(
        event_type=EventType.SEP_EVENT,
        driver=Driver.PROTON_FLUX,
        on_threshold=10.0,
        off_threshold=5.0,
        severity_fn=proton_severity,
        rationale="≥10 MeV proton flux ≥ 10 pfu — SEP event (S1+)",
    ),
    Rule(
        event_type=EventType.FLARE_M,
        driver=Driver.XRAY_FLUX,
        on_threshold=1e-5,
        off_threshold=5e-6,
        severity_fn=xray_severity,
        rationale="GOES X-ray ≥ 1e-5 W/m² (M-class flare, R1+)",
    ),
    Rule(
        event_type=EventType.FLARE_X,
        driver=Driver.XRAY_FLUX,
        on_threshold=1e-4,
        off_threshold=5e-5,
        severity_fn=xray_severity,
        rationale="GOES X-ray ≥ 1e-4 W/m² (X-class flare, R3+)",
    ),
)


# Solar wind shock detection works on a windowed delta, so it's separate.
SHOCK_RULE = {
    "delta_speed_km_s": 100.0,
    "window_minutes": 30,
    "rationale": "Wind speed jump ≥ 100 km/s in 30 min — interplanetary shock",
}


def detect_shock(window: list[FusedObservation]) -> bool:
    """
    Detect a fast solar-wind speed jump across a recent window.

    `window` should be sorted oldest → newest. Returns True if max(speed) -
    min(speed) ≥ delta_speed_km_s within the window AND the latest sample
    is on the high side.
    """
    if len(window) < 2:
        return False
    speeds = [o.wind_speed_km_s for o in window]
    if max(speeds) - min(speeds) < SHOCK_RULE["delta_speed_km_s"]:
        return False
    return window[-1].wind_speed_km_s == max(speeds)


# ── Detector ─────────────────────────────────────────────────────────────────


@dataclass
class DetectionResult:
    """What the detector decided for one rule on one observation."""

    rule: Rule
    fired: bool
    transition: EventState | None  # None = stay in current state, else move to this
    new_event: Event | None
    update_existing: dict | None  # diff to apply to the existing open Event


def evaluate_rule(
    rule: Rule,
    obs: FusedObservation,
    open_event: Event | None,
) -> DetectionResult:
    """
    Decide what should happen for this rule given the latest obs + any open
    event of the same type.

    No I/O — caller persists the result.
    """
    v = rule.trigger_value(obs)
    above_on = v >= rule.on_threshold
    below_off = v < rule.off_threshold

    if open_event is None:
        if above_on:
            ev = Event(
                event_type=rule.event_type,
                state=EventState.ONSET,
                severity=rule.severity_fn(v),
                region_id="GLOBAL",
                t_onset=obs.when,
                t_peak=obs.when,
                t_end=None,
                driver=rule.driver,
                peak_value=v,
                trigger_value=v,
                threshold_value=rule.on_threshold,
                rationale=rule.rationale,
            )
            return DetectionResult(rule, True, EventState.ONSET, ev, None)
        return DetectionResult(rule, False, None, None, None)

    # Have an open event of this type
    if open_event.state == EventState.ENDED:
        # Stale row that already ended; treat as no-op (don't reopen on same row)
        if above_on:
            ev = Event(
                event_type=rule.event_type,
                state=EventState.ONSET,
                severity=rule.severity_fn(v),
                region_id="GLOBAL",
                t_onset=obs.when,
                t_peak=obs.when,
                t_end=None,
                driver=rule.driver,
                peak_value=v,
                trigger_value=v,
                threshold_value=rule.on_threshold,
                rationale=rule.rationale,
            )
            return DetectionResult(rule, True, EventState.ONSET, ev, None)
        return DetectionResult(rule, False, None, None, None)

    # Open and not ended: extend or end it
    if below_off:
        return DetectionResult(
            rule,
            True,
            EventState.ENDED,
            None,
            {"state": EventState.ENDED.value, "t_end": obs.when},
        )

    # Still active — track new peak if applicable
    new_peak = open_event.peak_value or v
    update: dict = {"state": EventState.PEAK.value}
    if v > new_peak:
        update["peak_value"] = v
        update["t_peak"] = obs.when
        update["severity"] = rule.severity_fn(v)
    return DetectionResult(rule, True, EventState.PEAK, None, update)


# ── ML classifier stub ────────────────────────────────────────────────────────


class MLClassifierStub:
    """
    Placeholder for a future trained classifier.

    The interface is designed so swap-in is mechanical: provide a `classify`
    method that takes a window of FusedObservations and returns
    (EventType, confidence). For now we mirror the rule-based decision.

    Replace with sklearn/torch model + feature extractor in a later iteration;
    the rest of the pipeline doesn't care.
    """

    name = "ml-stub"

    def classify(
        self,
        window: list[FusedObservation],
    ) -> tuple[EventType, float] | None:
        if not window:
            return None
        latest = window[-1]
        # Walk rules from highest-severity downward; first match wins
        if latest.xray_flux_wm2 >= 1e-4:
            return EventType.FLARE_X, 0.5
        if latest.proton_flux_10mev_pfu >= 100:
            return EventType.SEP_EVENT, 0.5
        if latest.kp_index >= 7:
            return EventType.GEOMAG_MAIN, 0.6
        if latest.xray_flux_wm2 >= 1e-5:
            return EventType.FLARE_M, 0.5
        if detect_shock(window):
            return EventType.SUBSTORM, 0.4
        return EventType.BACKGROUND, 0.6
