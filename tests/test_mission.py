"""Tests for the Mission Planner translation layer (Stage 2).

Two layers of testing:
  1. Unit tests for app.models.mission — pure mapping/scoring functions, no
     network, fed with hand-built fixtures of the engine's output shape.
  2. HTTP smoke tests for POST /api/v3/mission/assess — goes through the
     FastAPI stack, exercising the engine but tolerating live-data variance
     (asserts shape, not specific values).
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app
from app.models import mission as M


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _fake_route_decision(
    action: str = "GO",
    waypoints: list[dict] | None = None,
    confidence_label: str = "HIGH",
    confidence_score: float = 0.85,
    stale: bool = False,
    feeds_unavailable: list[str] | None = None,
) -> dict:
    """Build a route-decision response shaped like POST /api/v2/route-decision."""
    return {
        "action": action,
        "action_sentence": f"{action} — engine fixture",
        "decision_type": "ROUTE_RISK",
        "alternatives": [],
        "recommended_actions": [],
        "confidence": {
            "score": confidence_score,
            "label": confidence_label,
            "stale_data": stale,
            "stale_penalty_applied": stale,
            "data_completeness": 1.0,
            "drivers": [],
        },
        "impacts": [],
        "provenance": {
            "model_version": "1.0.0",
            "input_hash": "sha256:dead",
            "computed_at": "2026-05-18T00:00:00Z",
            "observations_used": ["kp_index"],
            "forecasts_used": [],
            "feeds_unavailable": feeds_unavailable or [],
        },
        "waypoints": waypoints or [],
        "valid_until": None,
    }


def _wp(gps_error_m: float = 1.0, hf_viable: bool = True, pca_active: bool = False) -> dict:
    return {
        "name": "WP",
        "lat": 0.0,
        "lon": 0.0,
        "risk_level": "NOMINAL",
        "risk_score": 10.0,
        "gps_error_m": gps_error_m,
        "hf_viable": hf_viable,
        "hf_best_freq_mhz": 12.0,
        "hf_best_reliability_pct": 90.0,
        "hf_absorption_db": 1.0,
        "satcom_fade_db": 0.5,
        "s4_index": 0.1,
        "pca_active": pca_active,
        "watch_notes": [],
    }


# ── map_to_platform_kwargs ──────────────────────────────────────────────────


def test_platform_mapping_rtk_uses_l1l5_and_high_crit_for_defense():
    req = M.MissionRequest(mission_type="defense-patrol", gnss_dependence="rtk", risk_tolerance="low")
    kw = M.map_to_platform_kwargs(req)
    assert kw["asset_type"] == "GPS_L1L5"
    assert kw["criticality"] == 5  # base 4 + low-tolerance +1


def test_platform_mapping_low_gnss_uses_gps_ins():
    req = M.MissionRequest(mission_type="uav", gnss_dependence="low")
    kw = M.map_to_platform_kwargs(req)
    assert kw["asset_type"] == "GPS_INS"


def test_platform_mapping_high_tolerance_lowers_crit():
    req = M.MissionRequest(mission_type="bvlos", gnss_dependence="medium", risk_tolerance="high")
    kw = M.map_to_platform_kwargs(req)
    # bvlos base = 4, high tolerance = -1 → 3
    assert kw["criticality"] == 3


def test_platform_mapping_criticality_clamped_to_1_5():
    # Force out-of-range to confirm clamp
    req = M.MissionRequest(mission_type="precision-ag", gnss_dependence="rtk", risk_tolerance="high")
    kw = M.map_to_platform_kwargs(req)
    assert 1 <= kw["criticality"] <= 5


# ── GNSS reliability scoring ────────────────────────────────────────────────


def test_gnss_reliability_perfect_when_zero_error():
    g = M.gnss_reliability_from_waypoints([_wp(0.0), _wp(0.0)], "medium", "GPS_L1L2")
    assert g.score == 100.0
    assert g.label == "GOOD"
    assert g.affected_legs == 0


def test_gnss_reliability_rtk_treats_half_metre_as_degraded():
    # Half-metre error vs RTK tolerance of 0.5m → at edge of tolerance,
    # well into the falloff zone (score ~67)
    g = M.gnss_reliability_from_waypoints([_wp(0.5)], "rtk", "GPS_L1L5")
    assert g.tolerance_m == 0.5
    assert g.affected_legs == 0  # 0.5 is at tolerance, not above
    assert g.score < 80  # but score reflects we're close to limit
    # Same error for a defense patrol (high dep, 5m tolerance) is excellent
    g2 = M.gnss_reliability_from_waypoints([_wp(0.5)], "high", "GPS_L1L5")
    assert g2.score > 95


def test_gnss_reliability_unreliable_when_above_3x_tolerance():
    # medium dep has 10m tolerance; 35m is above 3× tolerance
    g = M.gnss_reliability_from_waypoints([_wp(35.0)], "medium", "GPS_L1L2")
    assert g.label == "UNRELIABLE"
    assert g.score == 0.0
    assert g.affected_legs == 1


def test_gnss_reliability_empty_waypoints_returns_unreliable():
    g = M.gnss_reliability_from_waypoints([], "medium", "GPS_L1L2")
    assert g.label == "UNRELIABLE"
    assert g.total_legs == 0


# ── Comms risk scoring ──────────────────────────────────────────────────────


def test_comms_risk_low_when_all_legs_viable():
    c = M.comms_risk_from_waypoints([_wp(), _wp(), _wp()], "high")
    assert c.score == 0.0
    assert c.label == "LOW"
    assert c.pca_active is False


def test_comms_risk_high_dep_escalates_on_any_degraded_leg():
    # 1 leg degraded out of 4 = 25% fraction. For 'low' dep that's barely
    # MODERATE; for 'high' dep, the floor forces it to at least 35.
    c_high = M.comms_risk_from_waypoints([_wp(), _wp(hf_viable=False), _wp(), _wp()], "high")
    c_low = M.comms_risk_from_waypoints([_wp(), _wp(hf_viable=False), _wp(), _wp()], "low")
    assert c_high.score >= 35.0
    assert c_low.score < c_high.score


def test_comms_risk_pca_active_adds_25_points():
    c = M.comms_risk_from_waypoints([_wp(pca_active=True)], "low")
    # 0% HF degraded, but PCA adds 25 → MODERATE band
    assert c.pca_active is True
    assert c.score >= 25.0
    assert c.label in {"MODERATE", "HIGH"}


def test_comms_risk_capped_at_100():
    # All legs degraded + PCA + high dep → score should not exceed 100
    wps = [_wp(hf_viable=False, pca_active=True) for _ in range(3)]
    c = M.comms_risk_from_waypoints(wps, "high")
    assert c.score <= 100.0
    assert c.label == "CRITICAL"


def test_comms_risk_hint_text_changes_with_severity():
    c_clear = M.comms_risk_from_waypoints([_wp()], "low")
    c_pca = M.comms_risk_from_waypoints([_wp(pca_active=True)], "high")
    assert "Standard" in c_clear.fallback_hint
    assert "PCA" in c_pca.fallback_hint


# ── Mission verdict derivation ──────────────────────────────────────────────


def test_mission_verdict_clear_for_go_with_good_scores():
    g = M.GnssReliability(
        score=95,
        label="GOOD",
        worst_error_m=0.5,
        tolerance_m=10,
        asset_type="GPS_L1L2",
        affected_legs=0,
        total_legs=1,
    )
    c = M.CommsRisk(
        score=0,
        label="LOW",
        hf_viable_legs=1,
        total_legs=1,
        pca_active=False,
        fallback_hint="ok",
    )
    level, summary = M.derive_mission_risk("GO", g, c, "medium", "medium")
    assert level == M.MISSION_RISK_CLEAR
    assert summary.startswith("CLEAR")


def test_mission_verdict_escalates_when_rtk_unreliable_even_if_engine_says_go():
    """Engine says GO for civilian thresholds, but RTK mission needs
    cm-grade — should escalate to DELAY."""
    g = M.GnssReliability(
        score=0,
        label="UNRELIABLE",
        worst_error_m=5,
        tolerance_m=0.5,
        asset_type="GPS_L1L5",
        affected_legs=1,
        total_legs=1,
    )
    c = M.CommsRisk(
        score=0,
        label="LOW",
        hf_viable_legs=1,
        total_legs=1,
        pca_active=False,
        fallback_hint="",
    )
    level, _ = M.derive_mission_risk("GO", g, c, "rtk", "low")
    assert level == M.MISSION_RISK_DELAY


def test_mission_verdict_escalates_for_high_comms_dep_on_critical_comms():
    g = M.GnssReliability(
        score=95,
        label="GOOD",
        worst_error_m=0.5,
        tolerance_m=10,
        asset_type="GPS_L1L2",
        affected_legs=0,
        total_legs=2,
    )
    c = M.CommsRisk(
        score=85,
        label="CRITICAL",
        hf_viable_legs=0,
        total_legs=2,
        pca_active=True,
        fallback_hint="",
    )
    level, _ = M.derive_mission_risk("GO", g, c, "medium", "high")
    assert level == M.MISSION_RISK_DELAY


def test_mission_verdict_no_go_stays_no_go():
    g = M.GnssReliability(95, "GOOD", 0.5, 10, "GPS_L1L2", 0, 1)
    c = M.CommsRisk(0, "LOW", 1, 1, False, "")
    level, _ = M.derive_mission_risk("NO_GO", g, c, "medium", "medium")
    assert level == M.MISSION_RISK_DELAY


# ── Data quality ────────────────────────────────────────────────────────────


def test_data_quality_high_when_engine_confidence_high():
    d = M.derive_data_quality(_fake_route_decision(confidence_label="HIGH", confidence_score=0.9))
    assert d.label == "HIGH"
    assert d.score == 0.9
    assert d.notes == []


def test_data_quality_low_when_stale_with_feeds_missing():
    rd = _fake_route_decision(
        confidence_label="LOW",
        confidence_score=0.3,
        stale=True,
        feeds_unavailable=["proton", "wind"],
    )
    d = M.derive_data_quality(rd)
    assert d.label == "LOW"
    assert any("Stale" in n for n in d.notes)
    assert any("proton" in n for n in d.notes)


# ── End-to-end assess_mission ───────────────────────────────────────────────


def test_assess_mission_returns_full_dict_shape():
    req = M.MissionRequest(
        mission_type="uav",
        gnss_dependence="medium",
        comms_dependence="medium",
        risk_tolerance="medium",
        waypoints=[M.MissionWaypoint("WP01", 38.8, -104.5)],
    )
    rd = _fake_route_decision(action="ADVISORY", waypoints=[_wp(gps_error_m=4.2)])
    result = M.assess_mission(req, rd)
    d = result.to_dict()
    # Required keys for the UI
    for k in (
        "mission_risk_level",
        "mission_risk_summary",
        "plain_explanation",
        "recommended_actions",
        "gnss",
        "comms",
        "data_quality",
        "inputs_echo",
        "source_labels",
        "raw_decision",
        "generated_at",
    ):
        assert k in d, f"missing key: {k}"
    assert d["gnss"]["score"] > 0
    assert d["mission_risk_level"] in {
        M.MISSION_RISK_CLEAR,
        M.MISSION_RISK_CAUTION,
        M.MISSION_RISK_HIGH,
        M.MISSION_RISK_DELAY,
    }


def test_assess_mission_echoes_inputs():
    req = M.MissionRequest(
        mission_type="defense-patrol",
        gnss_dependence="high",
        callsign="Patrol-Bravo",
    )
    rd = _fake_route_decision(waypoints=[_wp()])
    result = M.assess_mission(req, rd)
    echo = result.inputs_echo
    assert echo["mission_type"] == "defense-patrol"
    assert echo["callsign"] == "Patrol-Bravo"
    assert echo["platform_kwargs"]["asset_type"] == "GPS_L1L5"


def test_source_labels_cover_known_sources():
    labels = M.build_source_labels()
    # Must categorise each source as measured / modeled / heuristic
    assert all(v in {"measured", "modeled", "heuristic"} for v in labels.values())
    # And include the headline sources the page surfaces
    for key in ("noaa_swpc", "klobuchar_gps_model", "route_risk_engine"):
        assert key in labels


# ── HTTP endpoint ───────────────────────────────────────────────────────────


def test_http_mission_assess_returns_200_and_shape():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={
                "mission_type": "uav",
                "gnss_dependence": "medium",
                "comms_dependence": "medium",
                "risk_tolerance": "medium",
                "waypoints": [{"name": "WP01", "lat": 38.8, "lon": -104.5}],
                "time_window": "now",
                "callsign": "Test-1",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mission_risk_level"] in {"CLEAR", "CAUTION", "HIGH_RISK", "DELAY"}
    assert "score" in body["gnss"]
    assert "score" in body["comms"]
    assert "label" in body["data_quality"]
    assert body["inputs_echo"]["callsign"] == "Test-1"
    assert "measured" in body["source_labels"].values()


def test_http_mission_assess_rtk_ag_scenario_uses_rtk_tolerance():
    """An RTK mission with the engine returning GO should still flag the
    tight tolerance via the GNSS subcard."""
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={
                "mission_type": "precision-ag",
                "gnss_dependence": "rtk",
                "comms_dependence": "low",
                "risk_tolerance": "medium",
                "waypoints": [{"name": "Field-1", "lat": 45.7, "lon": -100.1}],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["gnss"]["tolerance_m"] == 0.5
    assert body["gnss"]["asset_type"] == "GPS_L1L5"


def test_http_mission_assess_rejects_missing_waypoints():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={"mission_type": "uav", "waypoints": []},
        )
    # Pydantic min_length=1 → 422
    assert r.status_code == 422


def test_http_mission_assess_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    assert "/api/v3/mission/assess" in schema["paths"]


# ── WarHacker P0-2: military mission types + equipment readout ───────────────


def test_military_mission_types_registered():
    from app.models.mission import BASE_CRITICALITY_BY_MISSION_TYPE, MISSION_TYPES

    for mt in ("fires-support", "sof-comms", "cas-coordination", "ground-maneuver"):
        assert mt in MISSION_TYPES
        assert mt in BASE_CRITICALITY_BY_MISSION_TYPE
    # Coordinate error → rounds off target: fires missions carry top criticality
    assert BASE_CRITICALITY_BY_MISSION_TYPE["fires-support"] == 5


def test_default_equipment_references_real_catalog_ids():
    from app.models.equipment import EQUIPMENT
    from app.models.mission import DEFAULT_EQUIPMENT_BY_MISSION_TYPE

    for mt, eq_ids in DEFAULT_EQUIPMENT_BY_MISSION_TYPE.items():
        for eq in eq_ids:
            assert eq in EQUIPMENT, f"{mt} default {eq} not in catalog"


def test_http_equipment_catalog():
    with TestClient(app) as client:
        r = client.get("/api/v3/equipment")
    assert r.status_code == 200
    body = r.json()
    ids = {e["id"] for e in body["equipment"]}
    assert "gps_single_freq" in ids
    assert "sincgars_fm" in ids
    assert "fires-support" in body["defaults_by_mission_type"]


def test_http_mission_assess_with_equipment_returns_findings():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={
                "mission_type": "fires-support",
                "gnss_dependence": "high",
                "comms_dependence": "high",
                "risk_tolerance": "low",
                "waypoints": [{"name": "FB-1", "lat": 35.0, "lon": -117.0}],
                "equipment": ["gps_single_freq", "counter_battery_radar", "sincgars_fm"],
            },
        )
    assert r.status_code == 200
    body = r.json()
    eq = body["equipment"]
    assert eq is not None
    assert eq["weather_state"] in ("QUIET", "MODERATE", "SEVERE")
    assert "basis: observed Kp" in eq["likelihood"]
    found_ids = {f["equipment_id"] for f in eq["findings"]}
    assert found_ids == {"gps_single_freq", "counter_battery_radar"}
    assert eq["unaffected"][0]["equipment_id"] == "sincgars_fm"
    for f in eq["findings"]:
        assert f["citation"].strip()
    assert body["inputs_echo"]["equipment"] == ["gps_single_freq", "counter_battery_radar", "sincgars_fm"]


def test_http_mission_assess_without_equipment_omits_block():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={
                "mission_type": "uav",
                "waypoints": [{"name": "WP1", "lat": 40.0, "lon": -75.0}],
            },
        )
    assert r.status_code == 200
    assert r.json()["equipment"] is None


def test_http_mission_assess_unknown_equipment_422():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/mission/assess",
            json={
                "mission_type": "uav",
                "waypoints": [{"name": "WP1", "lat": 40.0, "lon": -75.0}],
                "equipment": ["flux_capacitor"],
            },
        )
    assert r.status_code == 422
    assert "flux_capacitor" in r.json()["detail"]


def test_http_recommend_alias_matches_mission_assess():
    payload = {
        "mission_type": "cas-coordination",
        "gnss_dependence": "high",
        "waypoints": [{"name": "IP-1", "lat": 36.2, "lon": -115.0}],
        "equipment": ["gps_single_freq", "uhf_satcom"],
    }
    with TestClient(app) as client:
        r1 = client.post("/api/v3/mission/assess", json=payload)
        r2 = client.post("/api/v3/recommend", json=payload)
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["mission_risk_level"] == b2["mission_risk_level"]
    assert b1["equipment"]["weather_state"] == b2["equipment"]["weather_state"]
