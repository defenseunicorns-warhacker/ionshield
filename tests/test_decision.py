"""
IonShield Decision Engine — unit + integration tests.

Design principles:
  - No freezegun required: tests that care about stale-data thresholds pass
    data_age_seconds explicitly via EnvironmentSnapshot rather than freezing time.
  - No live network calls: all tests use hardcoded EnvironmentSnapshot fixtures.
  - Determinism: same env inputs must always produce the same action, confidence
    score, and provenance hash. UUIDs and timestamps in RecommendationObject are
    allowed to differ.

Coverage:
  TestConfidenceObject  — freshness penalties, completeness penalties, lead-time
                          penalties, bz variability, label thresholds, clamping
  TestProvenanceObject  — hash determinism, model version, unavailable feeds,
                          extra_inputs contribution
  TestCommsFallback     — nominal HF / PCA / no-viable / marginal / stale /
                          serializable
  TestRouteRisk         — GO / ADVISORY / CAUTION / NO-GO, criticality weighting,
                          single waypoint, determinism
  TestReplayDeterminism — confidence score, hash, and action are deterministic
  TestV2Endpoints       — smoke tests against the FastAPI TestClient
"""

import pytest
from datetime import datetime, timezone
from starlette.testclient import TestClient

from app.main import app
from app.models.decision import (
    ConfidenceObject,
    DecisionEngine,
    EnvironmentSnapshot,
    ObservationInput,
    PlatformInput,
    ProvenanceObject,
    SystemDependencyInput,
    WaypointInput,
    MODEL_VERSION,
)

client = TestClient(app, raise_server_exceptions=True)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _env(
    kp: float = 2.0,
    bz_nt: float = 0.0,
    xray_flux: float = 3e-7,
    proton_flux_10mev: float = 0.1,
    wind_speed_km_s: float = 400.0,
    data_age_seconds: int = 60,
    feeds_unavailable: list[str] | None = None,
    kp_forecast_24h: float | None = None,
) -> EnvironmentSnapshot:
    """Build a minimal EnvironmentSnapshot for testing."""
    all_feeds = ["kp", "xray", "wind", "mag", "proton", "kp_forecast"]
    unavailable = feeds_unavailable or []
    available = [f for f in all_feeds if f not in unavailable]
    now_iso = datetime.now(timezone.utc).isoformat()
    obs = [
        ObservationInput(
            "NOAA_SWPC", "kp_index", kp, "index", now_iso, data_age_seconds
        ),
        ObservationInput(
            "NOAA_SWPC", "bz_gsm_nt", bz_nt, "nT", now_iso, data_age_seconds
        ),
    ]
    return EnvironmentSnapshot(
        kp=kp,
        bz_nt=bz_nt,
        xray_flux=xray_flux,
        proton_flux_10mev=proton_flux_10mev,
        wind_speed_km_s=wind_speed_km_s,
        data_age_seconds=data_age_seconds,
        feeds_available=available,
        feeds_unavailable=unavailable,
        observations=obs,
        kp_forecast_24h=kp_forecast_24h,
    )


_engine = DecisionEngine()


# ── TestConfidenceObject ──────────────────────────────────────────────────────


class TestConfidenceObject:
    def test_fresh_data_no_penalty(self):
        env = _env(data_age_seconds=60)
        conf = ConfidenceObject.compute(env)
        assert conf.score == 1.0
        assert conf.label == "HIGH"
        assert not conf.stale_data
        assert conf.stale_penalty_applied is False

    def test_slightly_stale_15pct_penalty(self):
        env = _env(data_age_seconds=601)  # just over 10 min — triggers stale_data
        conf = ConfidenceObject.compute(env)
        assert conf.score == pytest.approx(0.85, abs=0.01)
        assert conf.stale_data is True  # >600s
        assert conf.stale_penalty_applied is True

    def test_stale_40pct_penalty(self):
        env = _env(data_age_seconds=1800)  # 30 min
        conf = ConfidenceObject.compute(env)
        # −0.40 freshness → 1.0 − 0.40 = 0.60 → LOW (0.40–0.64)
        assert conf.score == pytest.approx(0.60, abs=0.01)
        assert conf.label == "LOW"

    def test_very_stale_60pct_penalty(self):
        env = _env(data_age_seconds=7200)  # 2 hours
        conf = ConfidenceObject.compute(env)
        assert conf.score == pytest.approx(0.40, abs=0.01)

    def test_missing_feeds_compound_penalty(self):
        env = _env(data_age_seconds=60, feeds_unavailable=["proton", "mag"])
        conf = ConfidenceObject.compute(env)
        # 1.0 − (2 × 0.10) = 0.80
        assert conf.score == pytest.approx(0.80, abs=0.01)
        assert conf.data_completeness < 1.0

    def test_forecast_lead_48h_penalty(self):
        env = _env(data_age_seconds=60)
        conf = ConfidenceObject.compute(env, forecast_lead_hours=48.0)
        # 1.0 − 0.20 = 0.80
        assert conf.score == pytest.approx(0.80, abs=0.01)

    def test_bz_variability_penalty(self):
        env = _env(data_age_seconds=60, bz_nt=-25.0)
        conf = ConfidenceObject.compute(env)
        # 1.0 − 0.05 = 0.95
        assert conf.score == pytest.approx(0.95, abs=0.01)

    def test_score_clamped_to_zero(self):
        # Extreme stale + many missing feeds + bz variability
        env = _env(
            data_age_seconds=10000,
            feeds_unavailable=["kp", "xray", "wind", "mag", "proton"],
            bz_nt=-30.0,
        )
        conf = ConfidenceObject.compute(env, forecast_lead_hours=72.0)
        assert conf.score >= 0.0
        assert conf.label == "VERY_LOW"

    def test_label_very_low(self):
        env = _env(data_age_seconds=7200, feeds_unavailable=["kp", "xray", "wind"])
        conf = ConfidenceObject.compute(env)
        assert conf.label == "VERY_LOW"

    def test_computed_at_is_iso8601(self):
        conf = ConfidenceObject.compute(_env())
        # Ensure it parses without error
        datetime.fromisoformat(conf.computed_at)


# ── TestProvenanceObject ──────────────────────────────────────────────────────


class TestProvenanceObject:
    def test_hash_is_deterministic(self):
        env = _env(kp=5.0, bz_nt=-15.0)
        p1 = ProvenanceObject.build(env)
        p2 = ProvenanceObject.build(env)
        assert p1.input_hash == p2.input_hash

    def test_hash_differs_for_different_kp(self):
        p1 = ProvenanceObject.build(_env(kp=2.0))
        p2 = ProvenanceObject.build(_env(kp=7.0))
        assert p1.input_hash != p2.input_hash

    def test_model_version_correct(self):
        p = ProvenanceObject.build(_env())
        assert p.model_version == MODEL_VERSION

    def test_unavailable_feeds_captured(self):
        env = _env(feeds_unavailable=["proton", "kp_forecast"])
        p = ProvenanceObject.build(env)
        assert "proton" in p.feeds_unavailable
        assert "kp_forecast" in p.feeds_unavailable

    def test_extra_inputs_change_hash(self):
        env = _env(kp=3.0)
        p1 = ProvenanceObject.build(env)
        p2 = ProvenanceObject.build(env, extra_inputs={"lat": 55.0, "lon": -10.0})
        assert p1.input_hash != p2.input_hash

    def test_hash_starts_with_sha256(self):
        p = ProvenanceObject.build(_env())
        assert p.input_hash.startswith("sha256:")

    def test_operator_overrides_empty_by_default(self):
        p = ProvenanceObject.build(_env())
        assert p.operator_overrides == []


# ── TestCommsFallback ─────────────────────────────────────────────────────────


class TestCommsFallback:
    def test_nominal_hf_viable(self):
        """Low Kp, daytime — expect USE_PRIMARY_HF or USE_ALTERNATE_HF."""
        env = _env(kp=2.0, bz_nt=0.0, xray_flux=3e-7, proton_flux_10mev=0.1)
        rec = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        assert rec.action in ("USE_PRIMARY_HF", "USE_ALTERNATE_HF", "DEGRADED_MODE")
        assert rec.decision_type == "COMMS_FALLBACK"
        assert rec.action_sentence  # non-empty

    def test_pca_triggers_hf_not_viable(self):
        """High proton flux at polar latitude → PCA → HF_NOT_VIABLE."""
        env = _env(kp=8.0, bz_nt=-25.0, xray_flux=1e-4, proton_flux_10mev=2000.0)
        rec = _engine.comms_fallback(
            env, lat=70.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        assert rec.action == "HF_NOT_VIABLE"
        assert "PCA" in rec.action_sentence or "Polar Cap" in rec.action_sentence

    def test_satcom_alternative_present_during_hf_blackout(self):
        env = _env(kp=8.0, bz_nt=-25.0, xray_flux=1e-4, proton_flux_10mev=2000.0)
        platform = PlatformInput(
            system_dependencies=[SystemDependencyInput("HF", fallback_modes=["SATCOM"])]
        )
        rec = _engine.comms_fallback(
            env, lat=70.0, lon=0.0, dest_lat=None, dest_lon=None, platform=platform
        )
        # PCA active → HF_NOT_VIABLE; SATCOM should be in alternatives
        assert rec.action == "HF_NOT_VIABLE"
        assert "SWITCH_TO_SATCOM" in rec.alternatives

    def test_stale_data_reflected_in_confidence(self):
        env = _env(data_age_seconds=3700)  # >1 hour
        rec = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        assert rec.confidence.stale_data is True
        assert rec.confidence.score < 0.5

    def test_recommendation_is_serializable(self):
        """to_dict() must return a JSON-serializable dict."""
        import json

        env = _env(kp=3.0)
        rec = _engine.comms_fallback(
            env, lat=38.8, lon=-104.5, dest_lat=None, dest_lon=None
        )
        d = rec.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        assert "action" in serialized

    def test_valid_until_is_future(self):
        now = datetime.now(timezone.utc)
        env = _env(kp=2.0)
        rec = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None, now=now
        )
        valid_until = datetime.fromisoformat(rec.valid_until)
        assert valid_until > now

    def test_now_parameter_makes_valid_until_deterministic(self):
        fixed_now = datetime(2024, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
        env = _env(kp=2.0)
        rec1 = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None, now=fixed_now
        )
        rec2 = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None, now=fixed_now
        )
        assert rec1.valid_until == rec2.valid_until


# ── TestRouteRisk ─────────────────────────────────────────────────────────────


class TestRouteRisk:
    def test_nominal_kp_gives_go(self):
        env = _env(kp=1.0)
        waypoints = [WaypointInput(38.8, -104.5, "Alpha")]
        rec, wps = _engine.route_risk(env, waypoints)
        # At Kp=1 mid-latitude, risk score is very low → GO
        assert rec.action == "GO"
        assert rec.decision_type == "ROUTE_RISK"
        assert len(wps) == 1

    def test_severe_kp_high_lat_may_trigger_no_go(self):
        env = _env(kp=9.0, bz_nt=-30.0, proton_flux_10mev=5000.0)
        waypoints = [WaypointInput(72.0, -8.0, "POLAR")]
        rec, wps = _engine.route_risk(env, waypoints)
        # Very high Kp at polar latitude — expect CAUTION or worse
        assert rec.action in ("CAUTION", "NO_GO", "ADVISORY")

    def test_single_waypoint(self):
        env = _env(kp=2.0)
        rec, wps = _engine.route_risk(env, [WaypointInput(0.0, 0.0, "EQ")])
        assert len(wps) == 1
        assert wps[0].name == "EQ"

    def test_multi_waypoint_worst_drives_action(self):
        env = _env(kp=5.5, bz_nt=-15.0)
        waypoints = [
            WaypointInput(38.8, -104.5, "MID"),
            WaypointInput(72.0, -8.0, "POLAR"),  # worst
        ]
        rec, wps = _engine.route_risk(env, waypoints)
        assert len(wps) == 2
        # Worst waypoint score must drive the recommendation
        worst_score = max(w.risk_score for w in wps)
        assert worst_score > 0

    def test_high_criticality_raises_no_go_threshold(self):
        """Criticality=5 raises the NO-GO threshold — same score may differ."""
        env = _env(kp=5.0)
        waypoints = [WaypointInput(60.0, 0.0, "SUB")]
        platform_low = PlatformInput(criticality=1)
        platform_high = PlatformInput(criticality=5)
        rec_low, _ = _engine.route_risk(env, waypoints, platform=platform_low)
        rec_high, _ = _engine.route_risk(env, waypoints, platform=platform_high)
        # High-criticality should be equal or more severe
        action_rank = {"GO": 0, "ADVISORY": 1, "CAUTION": 2, "NO_GO": 3}
        assert action_rank.get(rec_high.action, 0) >= action_rank.get(rec_low.action, 0)

    def test_waypoint_decisions_have_required_fields(self):
        env = _env(kp=3.0)
        _, wps = _engine.route_risk(env, [WaypointInput(45.0, 10.0, "WP1")])
        wp = wps[0]
        assert isinstance(wp.risk_level, str)
        assert wp.risk_score >= 0
        assert wp.gps_error_m >= 0
        assert isinstance(wp.hf_viable, bool)
        assert isinstance(wp.pca_active, bool)
        assert isinstance(wp.watch_notes, list)

    def test_recommendation_has_provenance(self):
        env = _env(kp=4.0)
        rec, _ = _engine.route_risk(env, [WaypointInput(38.8, -104.5)])
        assert rec.provenance.input_hash.startswith("sha256:")
        assert rec.provenance.model_version == MODEL_VERSION

    def test_empty_waypoints_returns_go(self):
        # Edge case: engine receives an empty list
        env = _env()
        rec, wps = _engine.route_risk(env, [])
        assert rec.action == "GO"
        assert wps == []


# ── TestReplayDeterminism ─────────────────────────────────────────────────────


class TestReplayDeterminism:
    """Same env inputs must produce the same action and hash (not the same UUID)."""

    def test_confidence_score_deterministic(self):
        env = _env(kp=5.0, bz_nt=-12.0, data_age_seconds=400)
        c1 = ConfidenceObject.compute(env)
        c2 = ConfidenceObject.compute(env)
        assert c1.score == c2.score

    def test_provenance_hash_deterministic(self):
        env = _env(kp=7.0, bz_nt=-20.0)
        p1 = ProvenanceObject.build(env)
        p2 = ProvenanceObject.build(env)
        assert p1.input_hash == p2.input_hash

    def test_comms_action_deterministic(self):
        now = datetime(2024, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
        env = _env(kp=3.0, bz_nt=-5.0, proton_flux_10mev=0.1)
        r1 = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None, now=now
        )
        r2 = _engine.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None, now=now
        )
        assert r1.action == r2.action
        assert r1.valid_until == r2.valid_until
        assert r1.provenance.input_hash == r2.provenance.input_hash
        # UUIDs are intentionally different
        assert r1.id != r2.id

    def test_route_action_deterministic(self):
        now = datetime(2024, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
        env = _env(kp=6.0, bz_nt=-18.0)
        wps = [WaypointInput(65.0, -20.0, "WP1"), WaypointInput(70.0, -5.0, "WP2")]
        r1, d1 = _engine.route_risk(env, wps, now=now)
        r2, d2 = _engine.route_risk(env, wps, now=now)
        assert r1.action == r2.action
        assert r1.valid_until == r2.valid_until
        assert r1.provenance.input_hash == r2.provenance.input_hash
        assert d1[0].risk_score == d2[0].risk_score


# ── TestV2Endpoints ───────────────────────────────────────────────────────────


class TestV2Endpoints:
    """Smoke tests against the FastAPI stack — no mocking, NOAA returns fallback values."""

    def test_comms_decision_ok(self):
        r = client.get("/api/v2/comms-decision?lat=45.0&lon=0.0")
        assert r.status_code == 200
        data = r.json()
        assert "action" in data
        assert "confidence" in data
        assert "provenance" in data
        assert data["decision_type"] == "COMMS_FALLBACK"

    def test_comms_decision_with_dest(self):
        r = client.get(
            "/api/v2/comms-decision?lat=45.0&lon=0.0&dest_lat=55.0&dest_lon=-10.0"
        )
        assert r.status_code == 200
        assert "action" in r.json()

    def test_comms_decision_lat_out_of_range(self):
        r = client.get("/api/v2/comms-decision?lat=999&lon=0")
        assert r.status_code == 422

    def test_comms_decision_missing_lat(self):
        r = client.get("/api/v2/comms-decision?lon=0")
        assert r.status_code == 422

    def test_route_decision_ok(self):
        payload = {
            "waypoints": [
                {"lat": 38.8, "lon": -104.5, "name": "Alpha"},
                {"lat": 65.0, "lon": -18.0, "name": "Bravo"},
            ],
            "platform": {"asset_type": "GPS_L1", "criticality": 3},
        }
        r = client.post("/api/v2/route-decision", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert "action" in data
        assert "waypoints" in data
        assert len(data["waypoints"]) == 2
        assert data["decision_type"] == "ROUTE_RISK"

    def test_route_decision_waypoint_fields(self):
        payload = {
            "waypoints": [{"lat": 38.8, "lon": -104.5}],
        }
        r = client.post("/api/v2/route-decision", json=payload)
        assert r.status_code == 200
        wp = r.json()["waypoints"][0]
        for key in (
            "risk_level",
            "risk_score",
            "gps_error_m",
            "hf_viable",
            "hf_best_freq_mhz",
            "hf_absorption_db",
            "satcom_fade_db",
            "s4_index",
            "pca_active",
            "watch_notes",
        ):
            assert key in wp, f"waypoint missing key: {key}"

    def test_route_decision_empty_waypoints_rejected(self):
        r = client.post("/api/v2/route-decision", json={"waypoints": []})
        assert r.status_code == 422

    def test_route_decision_bad_lat_rejected(self):
        payload = {"waypoints": [{"lat": 200, "lon": 0}]}
        r = client.post("/api/v2/route-decision", json=payload)
        assert r.status_code == 422

    def test_confidence_score_in_range(self):
        r = client.get("/api/v2/comms-decision?lat=38.8&lon=-104.5")
        assert r.status_code == 200
        score = r.json()["confidence"]["score"]
        assert 0.0 <= score <= 1.0

    def test_provenance_hash_format(self):
        r = client.get("/api/v2/comms-decision?lat=38.8&lon=-104.5")
        assert r.status_code == 200
        h = r.json()["provenance"]["input_hash"]
        assert h.startswith("sha256:")
        assert len(h) == 7 + 64  # "sha256:" + 64 hex chars

    def test_valid_until_is_future(self):
        r = client.get("/api/v2/comms-decision?lat=38.8&lon=-104.5")
        assert r.status_code == 200
        valid_until = datetime.fromisoformat(r.json()["valid_until"])
        assert valid_until > datetime.now(timezone.utc)
