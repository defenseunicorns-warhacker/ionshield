"""
Tests for the D-RAP (authoritative HF absorption) and NANU (GPS outage,
prototype) feeds and their additive effect on mission assessment.
"""

from starlette.testclient import TestClient

from app.data import drap, nanu
from app.main import app

# A trimmed-but-real-format D-RAP product (header + lon axis >10 cols + rows).
DRAP_SAMPLE = """# DRAP Tabular Values
# Product Valid At : 2026-06-17 21:57 UTC
#  X-RAY Message : Normal X-ray Background
#  Proton Message : Normal Proton Background
# Frequency (MHz) as a function of Latitude and Longitude
      -178 -150 -120  -90  -60  -30   -2    2   30   60  120  178
-----------------------------------------------------------------
 89 |  1.2  1.2  1.2  1.2  1.2  1.1  1.1  1.1  1.1  1.1  1.2  1.2
 31 |  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0
 -1 |  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0
"""


# ── D-RAP parser ──────────────────────────────────────────────────────────────


def test_drap_parses_grid_and_header():
    parsed = drap._parse_drap_text(DRAP_SAMPLE)
    assert parsed["valid_at"] == "2026-06-17 21:57 UTC"
    assert "Normal X-ray" in parsed["xray_message"]
    g = parsed["grid"]
    assert g["lats"] == [89.0, 31.0, -1.0]
    assert len(g["lons"]) == 12
    assert g["values"][0][0] == 1.2


def test_drap_accessors_and_risk_levels():
    drap._cache.update(drap._parse_drap_text(DRAP_SAMPLE))
    drap._cache["source"] = "NOAA SWPC D-RAP"
    try:
        # high latitude → ~1.2 MHz → MINIMAL
        r = drap.hf_risk_at(89, -178)
        assert r["level"] == "MINIMAL" and r["blackout"] is False
        # equator → 0 → MINIMAL
        assert drap.absorption_freq_at(-1, 2) == 0.0
        assert drap.available() is True
    finally:
        drap.clear()


def test_drap_demo_blackout_is_severe_and_labeled():
    drap.set_demo_blackout()
    try:
        assert drap._cache["source"] == "DEMO"
        r = drap.hf_risk_at(75, 20)  # high latitude in the demo blackout
        assert r["level"] == "SEVERE" and r["blackout"] is True
        snap = drap.cache_snapshot()
        assert snap["source"] == "DEMO" and snap["global_max_mhz"] >= 15
    finally:
        drap.clear()


def test_drap_unavailable_when_no_grid():
    drap.clear()
    assert drap.available() is False
    assert drap.route_hf_risk([{"name": "WP", "lat": 35, "lon": -117}]) is None


# ── NANU ──────────────────────────────────────────────────────────────────────


def test_nanu_unavailable_by_default():
    nanu.clear()
    assert nanu.has_active_outage() is False
    snap = nanu.cache_snapshot()
    assert snap["available"] is False


def test_nanu_demo_outage_active_and_labeled():
    nanu.set_demo_outage()
    try:
        assert nanu.has_active_outage() is True
        snap = nanu.cache_snapshot()
        assert snap["source"] == "DEMO" and snap["advisory_count"] >= 1
    finally:
        nanu.clear()


# ── State cache round-trip (v2 includes drap + nanu) ──────────────────────────


def test_state_cache_persists_new_feeds(tmp_path, monkeypatch):
    from app.config import settings
    from app.data import state_cache

    monkeypatch.setattr(settings, "state_cache_file", str(tmp_path / "s.json"))
    drap.set_demo_blackout()
    nanu.set_demo_outage()
    try:
        assert state_cache.save_state()
        drap.clear()
        nanu.clear()
        assert state_cache.hydrate()
        assert drap.available() is True
        assert nanu.has_active_outage() is True
    finally:
        drap.clear()
        nanu.clear()
        state_cache.mark_live()


# ── HTTP: feeds layer in mission assess ───────────────────────────────────────


def _payload(**kw):
    base = {
        "mission_type": "sof-comms",
        "gnss_dependence": "high",
        "comms_dependence": "high",
        "risk_tolerance": "low",
        "waypoints": [{"name": "OP", "lat": 64.8, "lon": -147.7}],
        "equipment": ["hf_radio", "sincgars_fm"],
    }
    base.update(kw)
    return base


def test_assess_includes_operational_feeds_block():
    with TestClient(app) as c:
        b = c.post("/api/v3/mission/assess", json=_payload()).json()
    assert "operational_feeds" in b
    assert "drap" in b["operational_feeds"] and "nanu" in b["operational_feeds"]


def test_drap_demo_blackout_escalates_and_recommends():
    try:
        with TestClient(app) as c:
            b = c.post("/api/v3/mission/assess", json=_payload(feeds_demo=["drap_blackout"])).json()
        feeds = b["operational_feeds"]
        assert feeds["drap"]["feed_label"] == "demo"
        assert feeds["drap"]["used_in_assessment"] is True
        # comms-dependent + severe HF → verdict escalated above CLEAR
        assert b["mission_risk_level"] in ("CAUTION", "HIGH_RISK", "DELAY")
        assert any("D-RAP" in r or "SATCOM" in r for r in b["recommended_actions"])
        assert any("HF reachback" in fc["fail"] or "D-RAP" in fc["fail"] for fc in b.get("feed_consequences", []))
    finally:
        drap.clear()


def test_nanu_demo_outage_flags_pnt():
    try:
        with TestClient(app) as c:
            b = c.post(
                "/api/v3/mission/assess",
                json=_payload(mission_type="precision-ag", gnss_dependence="rtk", feeds_demo=["nanu_outage"]),
            ).json()
        assert b["operational_feeds"]["nanu"]["used_in_assessment"] is True
        assert any("constellation" in r.lower() or "RTK" in r for r in b["recommended_actions"])
    finally:
        nanu.clear()


def test_no_demo_means_feeds_not_driving():
    # With no NANU data at all (cleared after startup, no demo), the feed must
    # not drive the assessment or fabricate an outage. Clear inside the client
    # context so the startup live fetch can't repopulate before the request.
    try:
        with TestClient(app) as c:
            nanu.clear()
            b = c.post("/api/v3/mission/assess", json=_payload()).json()
        assert b["operational_feeds"]["nanu"]["used_in_assessment"] is False
        assert b["operational_feeds"]["nanu"]["feed_label"] == "unavailable"
    finally:
        nanu.clear()
