"""
Tests for disconnected-ops data quality (cache-and-carry, ADVISORY mode,
manual observation entry).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.data import manual_obs, noaa, state_cache, ustec
from app.main import app
from starlette.testclient import TestClient


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Isolated state-cache file + pristine caches + no manual obs."""
    from app.config import settings

    monkeypatch.setattr(settings, "state_cache_file", str(tmp_path / "state.json"))
    saved_noaa = {k: v for k, v in noaa._cache.items()}
    saved_ustec = {k: v for k, v in ustec._cache.items()}
    manual_obs.clear_observation()
    state_cache.mark_live()
    yield tmp_path
    noaa._cache.update(saved_noaa)
    ustec._cache.update(saved_ustec)
    manual_obs.clear_observation()
    state_cache.mark_live()


# ── Cache-and-carry ──────────────────────────────────────────────────────────


def test_save_and_hydrate_roundtrip_preserves_data_and_age(clean_state):
    fetch_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    noaa._cache["kp"] = [{"kp_index": 6.33, "time_tag": fetch_time}]
    noaa._cache["last_fetch"] = fetch_time
    noaa._cache["fetch_source"] = "live"

    assert state_cache.save_state() is True

    # Simulate a fresh air-gapped boot: caches empty
    noaa._cache["kp"] = None
    noaa._cache["last_fetch"] = None

    assert state_cache.hydrate() is True
    # Carried data restored
    assert noaa.get_kp() == 6.33
    # Original fetch time preserved → honest data age (~2 h, not 0)
    assert noaa._cache["last_fetch"] == fetch_time
    # Source marked as carried state
    assert noaa._cache["fetch_source"] == "cached"


def test_hydrate_returns_false_when_no_file(clean_state):
    assert state_cache.load_state() is None
    assert state_cache.hydrate() is False
    assert state_cache.advisory_note() is None


def test_advisory_note_carries_sync_time_and_72h_validity(clean_state):
    noaa._cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
    assert state_cache.save_state()
    assert state_cache.hydrate()

    note = state_cache.advisory_note()
    assert note is not None
    assert "ADVISORY" in note
    assert "synced" in note
    # valid-until = saved_at + 72 h
    saved = datetime.fromisoformat(state_cache.hydrated_from())
    valid = datetime.fromisoformat(state_cache.advisory_valid_until())
    assert valid - saved == timedelta(hours=72)


def test_mark_live_clears_advisory(clean_state):
    noaa._cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
    state_cache.save_state()
    state_cache.hydrate()
    assert state_cache.advisory_note() is not None
    state_cache.mark_live()
    assert state_cache.advisory_note() is None


def test_save_disabled_when_path_empty(clean_state, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "state_cache_file", "")
    assert state_cache.save_state() is False


# ── Manual observation ───────────────────────────────────────────────────────


def test_manual_obs_set_get_clear():
    manual_obs.set_observation(kp=7.0, source_note="S2 weather brief 0600Z")
    obs = manual_obs.get_observation()
    assert obs is not None and obs.kp == 7.0
    manual_obs.clear_observation()
    assert manual_obs.get_observation() is None


def test_manual_obs_expires_after_ttl(monkeypatch):
    obs = manual_obs.set_observation(kp=5.0, source_note="test")
    # Backdate entry past the TTL
    monkeypatch.setattr(
        obs,
        "entered_at",
        (datetime.now(timezone.utc) - timedelta(seconds=manual_obs.MANUAL_OBS_TTL_SECONDS + 60)).isoformat(),
    )
    assert manual_obs.get_observation() is None
    manual_obs.clear_observation()


def test_manual_obs_xray_class_maps_to_flux():
    obs = manual_obs.set_observation(kp=3.0, source_note="test", xray_class="X")
    assert obs.xray_flux_wm2() == 1e-4
    manual_obs.clear_observation()


# ── HTTP integration ─────────────────────────────────────────────────────────


def _assess_payload(**kw):
    base = {
        "mission_type": "fires-support",
        "gnss_dependence": "high",
        "waypoints": [{"name": "FB-1", "lat": 35.0, "lon": -117.0}],
        "equipment": ["gps_single_freq", "counter_battery_radar", "sincgars_fm"],
    }
    base.update(kw)
    return base


def test_http_manual_observation_drives_assessment():
    with TestClient(app) as client:
        # Enter operator Kp 8 (storm) — quiet live data would say GREEN
        r = client.post(
            "/api/v3/manual-observation",
            json={"kp": 8.0, "source_note": "Space weather officer report 1200Z"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

        r2 = client.post("/api/v3/mission/assess", json=_assess_payload())
        body = r2.json()
        # Equipment rules ran on the operator-entered Kp → SEVERE
        assert body["equipment"]["weather_state"] == "SEVERE"
        # MANUAL labeling everywhere it matters
        assert any("MANUAL" in n for n in body["data_quality"]["notes"])
        assert body["inputs_echo"]["manual_observation"]["kp"] == 8.0
        # kp observation still present in provenance (replaced, not dropped)
        prov = body["raw_decision"]["provenance"]
        assert "kp_index" in prov["observations_used"]

        # Clear → back to feed data, labels gone
        r3 = client.delete("/api/v3/manual-observation")
        assert r3.json()["status"] == "cleared"
        r4 = client.post("/api/v3/mission/assess", json=_assess_payload())
        assert not any("MANUAL" in n for n in (r4.json()["data_quality"]["notes"] or []))


def test_http_manual_observation_validation():
    with TestClient(app) as client:
        r1 = client.post("/api/v3/manual-observation", json={"kp": 12.0, "source_note": "bad"})
        r2 = client.post("/api/v3/manual-observation", json={"kp": 5.0, "source_note": "ok", "xray_class": "Z"})
    assert r1.status_code == 422
    assert r2.status_code == 422


def test_http_replay_wins_over_manual():
    with TestClient(app) as client:
        client.post("/api/v3/manual-observation", json={"kp": 1.0, "source_note": "quiet brief"})
        r = client.post("/api/v3/mission/assess", json=_assess_payload(scenario="gannon-2024"))
        client.delete("/api/v3/manual-observation")
    body = r.json()
    # Scenario drivers (Kp 9) used, not the manual Kp 1; REPLAY labels, no MANUAL
    assert body["equipment"]["weather_state"] == "SEVERE"
    assert any("REPLAY" in n for n in body["data_quality"]["notes"])
    assert not any("MANUAL" in n for n in body["data_quality"]["notes"])


def test_http_advisory_label_when_running_on_carried_state(clean_state):
    try:
        with TestClient(app) as client:
            # Hydrate AFTER startup — the lifespan's initial live fetch would
            # otherwise supersede the carried state (which is correct
            # production behavior: live data always wins).
            noaa._cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
            state_cache.save_state()
            state_cache.hydrate()
            r = client.post("/api/v3/mission/assess", json=_assess_payload())
        body = r.json()
        assert any("ADVISORY" in n for n in body["data_quality"]["notes"])
        assert body["inputs_echo"]["advisory_mode"] is True
    finally:
        state_cache.mark_live()
