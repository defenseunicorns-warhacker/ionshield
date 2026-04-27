"""Tests for app.models.events — rules, severity, ML stub, lifecycle evaluator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.events import (
    Event,
    EventState,
    MLClassifierStub,
    RULES,
    detect_shock,
    evaluate_rule,
    kp_severity,
    proton_severity,
    xray_severity,
)
from app.models.ontology import Driver, EventType, FusedObservation, Region


def _obs(
    when: datetime | None = None,
    *, kp=2.0, bz=0.0, wind=400.0, xray=1e-7,
    proton=0.1, f107=70.0, tec=15.0, anomaly=0.0,
) -> FusedObservation:
    return FusedObservation(
        region=Region.from_center(0, 0),
        when=when or datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp_index=kp, bz_nt=bz, wind_speed_km_s=wind,
        xray_flux_wm2=xray, proton_flux_10mev_pfu=proton, f107_sfu=f107,
        tec_tecu=tec, tec_anomaly_tecu=anomaly, hmf2_km=300.0, nmf2=1.5e11,
    )


def test_kp_severity_levels():
    assert kp_severity(4.5) == "NA"
    assert kp_severity(5.0) == "G1"
    assert kp_severity(7.0) == "G3"
    assert kp_severity(9.5) == "G5"


def test_proton_severity_levels():
    assert proton_severity(5) == "NA"
    assert proton_severity(50) == "S1"
    assert proton_severity(500) == "S2"
    assert proton_severity(50_000) == "S4"


def test_xray_severity_levels():
    assert xray_severity(1e-6) == "NA"
    assert xray_severity(2e-5) == "R1"
    assert xray_severity(1e-4) == "R3"
    assert xray_severity(3e-3) == "R5"


def test_geomag_rule_onset_and_end():
    geomag = next(r for r in RULES if r.event_type == EventType.GEOMAG_MAIN)

    # Quiet → no event opens
    res = evaluate_rule(geomag, _obs(kp=3.0), open_event=None)
    assert not res.fired

    # Crosses 5 → ONSET
    res = evaluate_rule(geomag, _obs(kp=5.5), open_event=None)
    assert res.fired
    assert res.new_event is not None
    assert res.new_event.event_type == EventType.GEOMAG_MAIN
    assert res.new_event.severity == "G1"

    # Open event, kp climbs higher → PEAK update
    open_ev = res.new_event
    res2 = evaluate_rule(geomag, _obs(kp=7.5), open_event=open_ev)
    assert res2.update_existing is not None
    assert res2.update_existing["state"] == EventState.PEAK.value
    assert res2.update_existing["peak_value"] == 7.5
    assert res2.update_existing["severity"] == "G3"

    # Open event, kp drops below off threshold (4.0) → ENDED
    res3 = evaluate_rule(geomag, _obs(kp=3.5), open_event=open_ev)
    assert res3.update_existing is not None
    assert res3.update_existing["state"] == EventState.ENDED.value
    assert "t_end" in res3.update_existing


def test_hysteresis_holds_event_open():
    geomag = next(r for r in RULES if r.event_type == EventType.GEOMAG_MAIN)
    open_ev = evaluate_rule(geomag, _obs(kp=5.5), open_event=None).new_event
    # Drop to 4.5: above off (4.0) → still open, no end
    res = evaluate_rule(geomag, _obs(kp=4.5), open_event=open_ev)
    assert res.update_existing is not None
    assert res.update_existing["state"] == EventState.PEAK.value


def test_sep_rule_fires_at_10_pfu():
    sep = next(r for r in RULES if r.event_type == EventType.SEP_EVENT)
    res = evaluate_rule(sep, _obs(proton=15.0), open_event=None)
    assert res.fired
    assert res.new_event.severity == "S1"


def test_x_class_flare_distinct_from_m_class():
    m_rule = next(r for r in RULES if r.event_type == EventType.FLARE_M)
    x_rule = next(r for r in RULES if r.event_type == EventType.FLARE_X)
    obs = _obs(xray=2e-4)
    m_res = evaluate_rule(m_rule, obs, open_event=None)
    x_res = evaluate_rule(x_rule, obs, open_event=None)
    assert m_res.fired and x_res.fired
    assert x_res.new_event.severity == "R3"


def test_ended_event_can_reopen_on_new_crossing():
    geomag = next(r for r in RULES if r.event_type == EventType.GEOMAG_MAIN)
    closed = Event(
        event_type=EventType.GEOMAG_MAIN, state=EventState.ENDED, severity="G1",
        region_id="GLOBAL", t_onset=datetime(2026, 1, 1, tzinfo=timezone.utc),
        t_peak=None, t_end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        driver=Driver.KP, peak_value=5.5, trigger_value=5.5, threshold_value=5.0,
    )
    res = evaluate_rule(geomag, _obs(kp=5.5), open_event=closed)
    assert res.fired and res.new_event is not None and res.new_event.state == EventState.ONSET


def test_shock_detection_window():
    base = datetime(2026, 4, 26, tzinfo=timezone.utc)
    quiet = [_obs(when=base + timedelta(minutes=i*5), wind=400) for i in range(6)]
    assert not detect_shock(quiet)

    shock = quiet[:-1] + [_obs(when=base + timedelta(minutes=30), wind=550)]
    assert detect_shock(shock)


def test_ml_stub_returns_classification():
    clf = MLClassifierStub()
    assert clf.classify([_obs(kp=2.0)])[0] == EventType.BACKGROUND
    assert clf.classify([_obs(kp=8.0)])[0] == EventType.GEOMAG_MAIN
    assert clf.classify([_obs(xray=2e-4)])[0] == EventType.FLARE_X
    assert clf.classify([_obs(proton=200)])[0] == EventType.SEP_EVENT


def test_event_to_dict_round_trips_iso_times():
    ev = Event(
        event_type=EventType.GEOMAG_MAIN, state=EventState.ONSET, severity="G2",
        region_id="GLOBAL", t_onset=datetime(2026, 4, 26, tzinfo=timezone.utc),
        t_peak=None, t_end=None,
        driver=Driver.KP, peak_value=6.0, trigger_value=6.0, threshold_value=5.0,
    )
    d = ev.to_dict()
    assert d["event_type"] == "GEOMAG_MAIN"
    assert d["t_onset"].endswith("+00:00")
    assert d["t_end"] is None
