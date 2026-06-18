"""
Tests for the live mission-watch feature: per-segment × time forecast grid,
the watch registry + re-evaluation, the watch endpoints, and the SSE stream.
"""

from starlette.testclient import TestClient

from app.api.routes_v3 import run_mission_assessment
from app.data import mission_watch, noaa
from app.main import app

_FORECAST_QUIET = [
    {"time_tag": "2026-06-18T00:00:00", "kp": 1.33, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-06-18T03:00:00", "kp": 2.0, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-06-18T06:00:00", "kp": 3.0, "observed": "predicted", "noaa_scale": None},
]
_FORECAST_STORM = [
    {"time_tag": "2026-06-18T00:00:00", "kp": 2.0, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-06-18T03:00:00", "kp": 9.0, "observed": "predicted", "noaa_scale": "G5"},
    {"time_tag": "2026-06-18T06:00:00", "kp": 8.3, "observed": "predicted", "noaa_scale": "G4"},
]


def _payload(**kw):
    base = {
        "mission_type": "sof-comms",
        "gnss_dependence": "high",
        "comms_dependence": "high",
        "risk_tolerance": "low",
        "waypoints": [{"name": "OP", "lat": 64.8, "lon": -147.7}],
        "equipment": ["hf_radio"],
    }
    base.update(kw)
    return base


def _model(**kw):
    from app.api.routes_v3 import _MissionRequestModel

    return _MissionRequestModel(**_payload(**kw))


# ── Forecast grid ───────────────────────────────────────────────────────────


def test_segment_time_grid_tracks_forecast():
    noaa._cache["kp_forecast"] = _FORECAST_STORM
    try:
        out = run_mission_assessment(_model())
        grid = out.get("segment_time_grid")
        assert grid and len(grid) == 3
        # each window carries its forecast Kp + per-waypoint segments
        assert [round(w["kp"], 1) for w in grid] == [2.0, 9.0, 8.3]
        assert all(w["segments"] and "risk_level" in w["segments"][0] for w in grid)
        # the G5 window must be at least as severe as the quiet one
        order = {"NOMINAL": 0, "ELEVATED": 1, "DEGRADED": 2, "SEVERE": 3}
        assert order[grid[1]["overall_risk"]] >= order[grid[0]["overall_risk"]]
    finally:
        noaa._cache["kp_forecast"] = []


def test_grid_absent_for_replay():
    out = run_mission_assessment(_model(scenario="gannon-2024"))
    assert "segment_time_grid" not in out  # replay has no live forecast horizon


# ── Watch registry + re-evaluation ────────────────────────────────────────────


def test_watch_register_reassess_change_delete():
    noaa._cache["kp_forecast"] = _FORECAST_QUIET
    try:
        wid, a0 = mission_watch.register(_model(), run_mission_assessment)
        assert mission_watch.get(wid)["version"] == 1
        # no feed change → no version bump
        assert mission_watch.reassess_all(run_mission_assessment) == []
        assert mission_watch.get(wid)["version"] == 1
        # a storm enters the forecast → version bumps with a change note
        noaa._cache["kp_forecast"] = _FORECAST_STORM
        changed = mission_watch.reassess_all(run_mission_assessment)
        assert wid in changed
        w = mission_watch.get(wid)
        assert w["version"] == 2 and w["change"]
        assert mission_watch.delete(wid) is True
        assert mission_watch.get(wid) is None
    finally:
        noaa._cache["kp_forecast"] = []
        mission_watch.delete(wid) if "wid" in dir() else None


# ── Endpoints ──────────────────────────────────────────────────────────────


def test_watch_endpoints_lifecycle():
    with TestClient(app) as c:
        noaa._cache["kp_forecast"] = _FORECAST_QUIET
        r = c.post("/api/v3/mission/watch", json=_payload())
        assert r.status_code == 200
        j = r.json()
        wid = j["watch_id"]
        assert j["version"] == 1 and j["stream_url"].endswith(f"{wid}/stream")
        assert j["assessment"].get("segment_time_grid")
        assert c.get(f"/api/v3/mission/watch/{wid}").status_code == 200
        assert c.get("/api/v3/mission/watch/does-not-exist").status_code == 404
        assert c.delete(f"/api/v3/mission/watch/{wid}").status_code == 200
        assert c.delete(f"/api/v3/mission/watch/{wid}").status_code == 404
        noaa._cache["kp_forecast"] = []


def test_watch_rejected_in_offline_mode(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "offline_mode", True)
    with TestClient(app) as c:
        r = c.post("/api/v3/mission/watch", json=_payload())
    assert r.status_code == 409


def test_watch_stream_unknown_id_404():
    # The SSE stream is an infinite generator that doesn't terminate cleanly
    # under TestClient, so live event delivery is verified in the browser, not
    # here. We can still assert the fast pre-stream guard: an unknown id 404s
    # before any streaming begins.
    with TestClient(app) as c:
        assert c.get("/api/v3/mission/watch/nope/stream").status_code == 404
