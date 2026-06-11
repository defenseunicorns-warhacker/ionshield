"""
Tests for the equipment-effect rule library (WarHacker P0-1).

The briefing-book definition of done: 5 equipment types × 3 weather states
= 15 rules, validated against 5 test scenarios. Those 5 scenarios are the
last test class in this file.
"""

from app.models.equipment import (
    AFFECTED_EQUIPMENT_IDS,
    EQUIPMENT,
    RISK_AMBER,
    RISK_GREEN,
    RISK_RED,
    RULES,
    UNAFFECTED_EQUIPMENT_IDS,
    WEATHER_STATES,
    WX_MODERATE,
    WX_QUIET,
    WX_SEVERE,
    build_likelihood,
    classify_weather_state,
    evaluate_equipment,
)

ALL_AFFECTED = list(AFFECTED_EQUIPMENT_IDS)
ALL_EQUIPMENT = list(EQUIPMENT)


# ── Rule library completeness ─────────────────────────────────────────────────


def test_rule_library_is_5x3():
    assert len(AFFECTED_EQUIPMENT_IDS) == 5
    assert len(WEATHER_STATES) == 3
    assert len(RULES) == 15


def test_every_affected_equipment_has_a_rule_for_every_state():
    for eq in AFFECTED_EQUIPMENT_IDS:
        for state in WEATHER_STATES:
            assert (eq, state) in RULES, f"missing rule for {eq}/{state}"


def test_every_rule_has_citation_effect_and_action():
    for rule in RULES.values():
        assert rule.citation.strip(), f"{rule.equipment_id}/{rule.weather_state} lacks citation"
        assert rule.effect.strip()
        assert rule.action.strip()


def test_quiet_rules_are_green_severe_rules_are_red():
    for eq in AFFECTED_EQUIPMENT_IDS:
        assert RULES[(eq, WX_QUIET)].risk == RISK_GREEN
        assert RULES[(eq, WX_MODERATE)].risk == RISK_AMBER
        assert RULES[(eq, WX_SEVERE)].risk == RISK_RED


def test_unaffected_set_documents_why():
    assert set(UNAFFECTED_EQUIPMENT_IDS) == {"sincgars_fm", "ehf_satcom"}
    for eq_id in UNAFFECTED_EQUIPMENT_IDS:
        assert EQUIPMENT[eq_id].why_unaffected.strip()


# ── Weather state classification ─────────────────────────────────────────────


def test_classify_quiet():
    assert classify_weather_state(kp=2.0) == WX_QUIET
    assert classify_weather_state(kp=4.9, xray_flux_wm2=9e-6, proton_flux_10mev_pfu=9.0) == WX_QUIET


def test_classify_moderate_thresholds():
    assert classify_weather_state(kp=5.0) == WX_MODERATE  # G1 storm
    assert classify_weather_state(kp=2.0, xray_flux_wm2=1e-5) == WX_MODERATE  # M-class
    assert classify_weather_state(kp=2.0, proton_flux_10mev_pfu=10.0) == WX_MODERATE  # S1


def test_classify_severe_thresholds():
    assert classify_weather_state(kp=7.0) == WX_SEVERE  # G3 storm
    assert classify_weather_state(kp=2.0, xray_flux_wm2=1e-4) == WX_SEVERE  # X-class
    assert classify_weather_state(kp=2.0, proton_flux_10mev_pfu=100.0) == WX_SEVERE  # S2 / PCA


def test_any_single_severe_driver_wins():
    # Quiet Kp but X-class flare → still SEVERE (hazards are independent)
    assert classify_weather_state(kp=1.0, xray_flux_wm2=2e-4) == WX_SEVERE


# ── Evaluation behavior ──────────────────────────────────────────────────────


def test_unknown_equipment_ignored():
    out = evaluate_equipment(["not_a_radio", "gps_single_freq"], kp=2.0)
    assert len(out.findings) == 1


def test_unaffected_equipment_reported_separately_even_in_severe():
    out = evaluate_equipment(["sincgars_fm", "ehf_satcom", "hf_radio"], kp=8.0)
    assert len(out.unaffected) == 2
    assert all(u["status"] == "UNAFFECTED" for u in out.unaffected)
    assert len(out.findings) == 1
    assert out.findings[0].risk == RISK_RED


def test_findings_sorted_worst_first_and_worst_risk():
    out = evaluate_equipment(ALL_AFFECTED, kp=7.5)
    assert out.worst_risk == RISK_RED
    risks = [f.risk for f in out.findings]
    assert risks == sorted(risks, key=lambda r: {"GREEN": 0, "AMBER": 1, "RED": 2}[r], reverse=True)


def test_likelihood_states_observed_basis_no_invented_numbers():
    # Without the NOAA scales feed, likelihood is the measured basis only —
    # the observed Kp appears; no fabricated percentage does.
    out = evaluate_equipment(["gps_single_freq"], kp=5.5)
    assert "Kp 5.5" in out.likelihood
    assert "%" not in out.likelihood


def test_likelihood_includes_real_noaa_forecast_probs_when_available():
    # Mirror of the live noaa-scales.json shape after get_noaa_scales()
    # normalization — forecaster-issued numbers, not ours.
    scales = {
        "observed": {"r_scale": 0, "s_scale": 0, "g_scale": 0},
        "forecast": [
            {"day": 1, "r_minor_prob": 40.0, "r_major_prob": 10.0, "s1_prob": 5.0, "g_predicted": 1},
        ],
    }
    text = build_likelihood(WX_MODERATE, kp=5.5, noaa_scales=scales)
    assert "R1–R2 radio blackout 40%" in text
    assert "R3+ 10%" in text
    assert "S1+ radiation storm 5%" in text
    assert "predicted G1" in text


def test_likelihood_names_flare_and_proton_drivers():
    text = build_likelihood(WX_SEVERE, kp=2.0, xray_flux_wm2=2e-4, proton_flux_10mev_pfu=150.0)
    assert "X-class" in text
    assert "S2+" in text


def test_to_dict_round_trip():
    out = evaluate_equipment(ALL_EQUIPMENT, kp=6.0).to_dict()
    assert out["weather_state"] == WX_MODERATE
    assert isinstance(out["findings"], list)
    assert isinstance(out["unaffected"], list)


# ── NOAA scales feed parsing (real product shape) ────────────────────────────


def test_get_noaa_scales_parses_live_product_shape():
    from app.data import noaa

    # Exact shape of products/noaa-scales.json (sampled live 2026-06-10)
    noaa._cache["scales"] = {
        "0": {
            "DateStamp": "2026-06-11",
            "TimeStamp": "01:58:00",
            "R": {"Scale": "0", "Text": "none", "MinorProb": None, "MajorProb": None},
            "S": {"Scale": "0", "Text": "none", "Prob": None},
            "G": {"Scale": "0", "Text": "none"},
        },
        "1": {
            "DateStamp": "2026-06-11",
            "TimeStamp": "01:58:00",
            "R": {"Scale": None, "Text": None, "MinorProb": "40", "MajorProb": "10"},
            "S": {"Scale": None, "Text": None, "Prob": "5"},
            "G": {"Scale": "1", "Text": "minor"},
        },
    }
    try:
        out = noaa.get_noaa_scales()
        assert out is not None
        assert out["observed"]["g_scale"] == 0
        day1 = out["forecast"][0]
        assert day1["r_minor_prob"] == 40.0
        assert day1["r_major_prob"] == 10.0
        assert day1["s1_prob"] == 5.0
        assert day1["g_predicted"] == 1
    finally:
        noaa._cache["scales"] = None


def test_get_noaa_scales_returns_none_when_feed_absent():
    from app.data import noaa

    noaa._cache["scales"] = None
    assert noaa.get_noaa_scales() is None


# ── The 5 briefing-book test scenarios (definition of done) ──────────────────


def test_scenario_1_quiet_day_all_green():
    """Quiet conditions: everything GREEN, fallbacks still listed."""
    out = evaluate_equipment(ALL_EQUIPMENT, kp=2.0, xray_flux_wm2=3e-7, proton_flux_10mev_pfu=0.1)
    assert out.weather_state == WX_QUIET
    assert out.worst_risk == RISK_GREEN
    assert len(out.findings) == 5
    assert len(out.unaffected) == 2


def test_scenario_2_demo_storm_kp7_uas_isr():
    """The 90-second demo: Kp 7, Group 1 UAS ISR mission. GPS + UAS RED."""
    out = evaluate_equipment(["gps_single_freq", "uas_group1", "sincgars_fm"], kp=7.0)
    assert out.weather_state == WX_SEVERE
    by_id = {f.equipment_id: f for f in out.findings}
    assert by_id["gps_single_freq"].risk == RISK_RED
    assert "100 m" in by_id["gps_single_freq"].effect
    assert by_id["uas_group1"].risk == RISK_RED
    assert "manual control" in by_id["uas_group1"].action
    # The fallback story: SINCGARS confirmed unaffected
    assert out.unaffected[0]["equipment_id"] == "sincgars_fm"


def test_scenario_3_xclass_flare_hf_blackout():
    """X-class flare, quiet Kp: HF RED, action says shift to SINCGARS/EHF."""
    out = evaluate_equipment(["hf_radio", "uhf_satcom"], kp=2.0, xray_flux_wm2=2e-4)
    assert out.weather_state == WX_SEVERE
    by_id = {f.equipment_id: f for f in out.findings}
    assert by_id["hf_radio"].risk == RISK_RED
    assert "SINCGARS" in by_id["hf_radio"].action


def test_scenario_4_moderate_storm_fires_support():
    """Kp 5 (G1): counter-battery radar + GPS AMBER, with doctrine citations."""
    out = evaluate_equipment(["counter_battery_radar", "gps_single_freq"], kp=5.0)
    assert out.weather_state == WX_MODERATE
    assert out.worst_risk == RISK_AMBER
    for f in out.findings:
        assert f.risk == RISK_AMBER
        assert f.citation.strip()


def test_scenario_5_proton_event_polar_pca():
    """S2 proton event: SEVERE via proton channel alone — HF blackout risk."""
    out = evaluate_equipment(["hf_radio"], kp=3.0, proton_flux_10mev_pfu=150.0)
    assert out.weather_state == WX_SEVERE
    assert out.findings[0].risk == RISK_RED
    assert "polar" in out.findings[0].effect.lower()
