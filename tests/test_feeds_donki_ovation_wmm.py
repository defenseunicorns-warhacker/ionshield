"""
Tests for the DONKI (event log), OVATION (aurora), and WMM (magnetic
declination) feeds, the live NAVCEN GPS-almanac NANU workaround, and their
additive effect on mission assessment.
"""

from starlette.testclient import TestClient

from app.data import donki, nanu, ovation, wmm
from app.main import app


# ── NANU live NAVCEN GPS-almanac workaround ───────────────────────────────────

YUMA_SAMPLE = """******** Week 375 almanac for PRN-01 ********
ID:                         01
Health:                     000
week:                        375

******** Week 375 almanac for PRN-13 ********
ID:                         13
Health:                     063
week:                        375

******** Week 375 almanac for PRN-22 ********
ID:                         22
Health:                     000
week:                        375
"""


def test_yuma_parser_splits_healthy_and_unhealthy():
    healthy, unhealthy = nanu._parse_yuma_almanac(YUMA_SAMPLE)
    assert healthy == [1, 22]
    assert unhealthy == [13]


def test_nanu_constellation_full_no_outage():
    nanu.clear()
    nanu._cache.update(
        {
            "constellation": {
                "operational_count": 31,
                "total_tracked": 31,
                "nominal": 31,
                "prns": list(range(1, 32)),
                "unhealthy": [],
            },
            "source": "NAVCEN GPS almanac",
        }
    )
    try:
        c = nanu.constellation_status()
        assert c["operational_count"] == 31 and c["degraded"] is False
        assert nanu.has_active_outage() is False
        snap = nanu.cache_snapshot()
        assert snap["source"] == "NAVCEN GPS almanac" and snap["available"] is True
    finally:
        nanu.clear()


def test_nanu_unhealthy_sv_flags_outage_without_degrade():
    # 31 healthy + 1 unhealthy → nominal met (not degraded) but a real outage
    nanu.clear()
    nanu._cache.update(
        {
            "constellation": {
                "operational_count": 31,
                "total_tracked": 32,
                "nominal": 31,
                "prns": list(range(1, 32)),
                "unhealthy": [13],
            },
            "source": "NAVCEN GPS almanac",
        }
    )
    try:
        c = nanu.constellation_status()
        assert c["degraded"] is False and c["unhealthy"] == [13]
        assert nanu.has_active_outage() is True
    finally:
        nanu.clear()


def test_nanu_constellation_degraded_flags_outage():
    nanu.clear()
    nanu._cache.update(
        {
            "constellation": {
                "operational_count": 28,
                "total_tracked": 28,
                "nominal": 31,
                "prns": list(range(1, 29)),
                "unhealthy": [],
            },
            "source": "NAVCEN GPS almanac",
        }
    )
    try:
        assert nanu.constellation_status()["degraded"] is True
        assert nanu.has_active_outage() is True
    finally:
        nanu.clear()


# ── DONKI ──────────────────────────────────────────────────────────────────────


def test_donki_demo_events_and_drivers():
    donki.set_demo_events()
    try:
        assert donki.has_significant_activity() is True
        drivers = donki.drivers_summary()
        assert any("flare" in d.lower() for d in drivers)
        assert any("X1.8" in d for d in drivers)  # flare class surfaced
        snap = donki.cache_snapshot()
        assert snap["source"] == "DEMO" and snap["event_count"] >= 3
    finally:
        donki.clear()


def test_donki_unavailable_by_default():
    donki.clear()
    assert donki.available() is False
    assert donki.drivers_summary() == []


# ── OVATION ─────────────────────────────────────────────────────────────────


def test_ovation_demo_aurora_high_latitude():
    ovation.set_demo_aurora()
    try:
        r = ovation.aurora_risk_at(67, 20)
        assert r["level"] == "HIGH"
        # low end of the modeled band is minimal
        assert ovation.aurora_risk_at(42, -117)["level"] == "MINIMAL"
        worst = ovation.route_aurora_risk([{"name": "OP", "lat": 67, "lon": 20}])
        assert worst["level"] == "HIGH" and worst["at"] == "OP"
        assert ovation.cache_snapshot()["source"] == "DEMO"
    finally:
        ovation.clear()


def test_ovation_unavailable_when_empty():
    ovation.clear()
    assert ovation.available() is False
    assert ovation.route_aurora_risk([{"name": "X", "lat": 60, "lon": 10}]) is None


def test_ovation_lon_folding():
    # 350°E should fold to -10°
    assert ovation._norm_lon(350) == -10
    assert ovation._norm_lon(10) == 10


# ── WMM (local model — always available) ──────────────────────────────────────


def test_wmm_declination_conus():
    assert wmm.available() is True
    r = wmm.declination_at(31.5, -110.3)  # Fort Huachuca area
    assert r is not None
    assert -20 < r["declination_deg"] < 30
    assert r["compass_reliability"] in ("RELIABLE", "CAUTION", "BLACKOUT")
    assert "G-M angle" in r["guidance"] or "compass" in r["guidance"].lower()


def test_wmm_route_declination_reliability():
    r = wmm.route_declination([{"name": "SP", "lat": 31.5, "lon": -110.3}])
    assert r is not None and r["at"] == "SP"
    assert "route_compass_reliability" in r


def test_wmm_high_latitude_compass_degraded():
    # very close to the north magnetic pole → caution/blackout
    r = wmm.declination_at(88.0, -120.0)
    assert r is not None
    assert r["compass_reliability"] in ("CAUTION", "BLACKOUT")


# ── HTTP: new feeds appear in operational_feeds ───────────────────────────────


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


def test_assess_includes_all_feed_blocks():
    with TestClient(app) as c:
        b = c.post("/api/v3/mission/assess", json=_payload()).json()
    feeds = b["operational_feeds"]
    for key in ("drap", "nanu", "donki", "ovation", "wmm"):
        assert key in feeds, f"missing {key}"
    # WMM is local + always available → always provides a declination
    assert feeds["wmm"]["used_in_assessment"] is True
    assert feeds["wmm"]["declination"] is not None


def test_aurora_storm_demo_escalates_high_latitude():
    try:
        with TestClient(app) as c:
            b = c.post("/api/v3/mission/assess", json=_payload(feeds_demo=["aurora_storm"])).json()
        ov = b["operational_feeds"]["ovation"]
        assert ov["feed_label"] == "demo"
        assert ov["used_in_assessment"] is True
        assert b["mission_risk_level"] in ("CAUTION", "HIGH_RISK", "DELAY")
        assert any("OVATION" in fc["fail"] or "aurora" in fc["fail"].lower() for fc in b.get("feed_consequences", []))
    finally:
        ovation.clear()


def test_donki_demo_adds_event_drivers():
    try:
        with TestClient(app) as c:
            b = c.post("/api/v3/mission/assess", json=_payload(feeds_demo=["donki_events"])).json()
        dk = b["operational_feeds"]["donki"]
        assert dk["feed_label"] == "demo"
        assert dk["used_in_assessment"] is True
        assert b.get("event_drivers")  # drivers surfaced for the operator
        assert any("DONKI" in r for r in b["recommended_actions"])
    finally:
        donki.clear()
