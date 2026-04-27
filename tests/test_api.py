"""
IonShield API smoke tests.

TestClient is instantiated without the lifespan context manager so the startup
NOAA fetch is skipped — all data accessors return FALLBACK values, which is
sufficient to verify that routes are wired correctly and models don't blow up.

These are integration tests against the real FastAPI/Starlette stack; no mocks.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)


# ── Infrastructure ────────────────────────────────────────────────────────────


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_serves_marketing_page():
    """Root now serves the marketing landing page (not a redirect)."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_dashboard_serves_html():
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b"IonShield" in r.content
    assert r.headers["content-type"].startswith("text/html")


# ── /api/status ───────────────────────────────────────────────────────────────


def test_status_shape():
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "global_risk_level" in data
    assert "solar_drivers" in data
    drivers = data["solar_drivers"]
    for key in (
        "kp_current",
        "xray_class",
        "bz_nt",
        "solar_wind_km_s",
        "proton_flux_10mev_pfu",
    ):
        assert key in drivers, f"Missing solar driver key: {key}"


def test_status_global_risk_is_valid():
    r = client.get("/api/status")
    assert r.json()["global_risk_level"] in {
        "NOMINAL",
        "ELEVATED",
        "DEGRADED",
        "SEVERE",
    }


# ── /api/risk/location ────────────────────────────────────────────────────────


def test_risk_location_nominal():
    r = client.get("/api/risk/location?lat=38.8&lon=-104.5&asset_type=GPS_L1")
    assert r.status_code == 200
    data = r.json()
    assert "assessment" in data
    a = data["assessment"]
    assert "risk_level" in a
    assert "risk_score" in a
    assert "gps_error_m" in a


def test_risk_location_dual_freq_lower_error():
    """Dual-frequency GPS should have dramatically lower GPS error than L1-only."""
    r_l1 = client.get("/api/risk/location?lat=38.8&lon=-104.5&asset_type=GPS_L1")
    r_l1l2 = client.get("/api/risk/location?lat=38.8&lon=-104.5&asset_type=GPS_L1L2")
    assert r_l1.status_code == 200
    assert r_l1l2.status_code == 200
    err_l1 = r_l1.json()["assessment"]["gps_error_m"]
    err_l1l2 = r_l1l2.json()["assessment"]["gps_error_m"]
    assert err_l1l2 < err_l1, "Dual-freq GPS should have lower error than L1-only"


def test_risk_location_all_asset_types():
    for asset in ("GPS_L1", "GPS_L1L2", "GPS_L1L5", "GPS_INS", "SBAS"):
        r = client.get(f"/api/risk/location?lat=0&lon=0&asset_type={asset}")
        assert r.status_code == 200, f"Failed for asset_type={asset}"


def test_risk_location_lat_out_of_range():
    r = client.get("/api/risk/location?lat=999&lon=0")
    assert r.status_code == 422


def test_risk_location_lon_out_of_range():
    r = client.get("/api/risk/location?lat=0&lon=270")
    assert r.status_code == 422


def test_risk_location_missing_params():
    r = client.get("/api/risk/location")
    assert r.status_code == 422


# ── /api/risk/route ───────────────────────────────────────────────────────────


def test_route_single_waypoint():
    payload = {
        "waypoints": [{"lat": 38.8, "lon": -104.5, "name": "Alpha"}],
        "asset_type": "GPS_L1",
    }
    r = client.post("/api/risk/route", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert "route_summary" in data
    assert "waypoints" in data
    assert len(data["waypoints"]) == 1


def test_route_multi_waypoint():
    payload = {
        "waypoints": [
            {"lat": 38.8, "lon": -104.5},
            {"lat": 65.0, "lon": -18.0},  # sub-auroral
            {"lat": -10.0, "lon": 30.0},  # equatorial
        ],
        "asset_type": "GPS_INS",
    }
    r = client.post("/api/risk/route", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["route_summary"]["total_waypoints"] == 3
    assert "route_recommendation" in data["route_summary"]


def test_route_empty_waypoints():
    # min_length=1 on RouteRequest.waypoints — empty list is a validation error
    r = client.post("/api/risk/route", json={"waypoints": [], "asset_type": "GPS_L1"})
    assert r.status_code == 422


def test_route_invalid_asset_type():
    # Validator silently falls back to GPS_L1 rather than rejecting — returns 200
    payload = {"waypoints": [{"lat": 0, "lon": 0}], "asset_type": "INVALID"}
    r = client.post("/api/risk/route", json=payload)
    assert r.status_code == 200


def test_route_waypoint_lat_out_of_range():
    payload = {"waypoints": [{"lat": 100, "lon": 0}], "asset_type": "GPS_L1"}
    r = client.post("/api/risk/route", json=payload)
    assert r.status_code == 422


# ── /api/forecast ─────────────────────────────────────────────────────────────


def test_forecast_shape():
    r = client.get("/api/forecast")
    assert r.status_code == 200
    data = r.json()
    for key in ("summary", "windows", "timeline", "current_kp"):
        assert key in data, f"Missing forecast key: {key}"


def test_forecast_summary_keys():
    data = client.get("/api/forecast").json()
    summary = data["summary"]
    for key in (
        "max_kp_24h",
        "max_kp_72h",
        "storm_watch",
        "storm_warning",
        "outlook_text",
    ):
        assert key in summary, f"Missing summary key: {key}"


def test_forecast_has_seven_windows():
    data = client.get("/api/forecast").json()
    assert len(data["windows"]) == 7


def test_forecast_window_shape():
    data = client.get("/api/forecast").json()
    for w in data["windows"]:
        for key in (
            "label",
            "kp_forecast",
            "risk_level",
            "gps_impact",
            "hf_impact",
            "source",
        ):
            assert key in w, f"Window missing key: {key}"


# ── /api/locations ────────────────────────────────────────────────────────────


def test_locations_returns_list():
    r = client.get("/api/locations")
    assert r.status_code == 200
    data = r.json()
    assert "locations" in data
    assert "count" in data
    assert isinstance(data["locations"], list)
    assert data["count"] == len(data["locations"])


def test_locations_shape_when_populated(tmp_path, monkeypatch):
    """Write a locations.json, reload it via the API layer, verify structure."""
    import json

    loc_file = tmp_path / "locations.json"
    loc_file.write_text(
        json.dumps(
            [
                {
                    "id": "test_site",
                    "name": "Test Site",
                    "lat": 38.8,
                    "lon": -104.5,
                    "asset_type": "GPS_L1",
                    "alert_threshold": "ELEVATED",
                },
            ]
        )
    )
    # Call the location store directly (same in-process state the API uses)
    from app.data.locations import load_locations, assess_all
    from app.data.noaa import FALLBACK

    load_locations(str(loc_file), "ELEVATED")
    assess_all(FALLBACK["kp"])

    r = client.get("/api/locations")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    loc = next((item for item in data["locations"] if item["id"] == "test_site"), None)
    assert loc is not None
    assert "assessment" in loc
    assert "alert" in loc
    assert loc["alert"]["active"] is False  # not enough consecutive hits yet


def test_location_by_id_not_found():
    r = client.get("/api/locations/nonexistent_id_xyz")
    assert r.status_code == 404


# ── /api/alerts ───────────────────────────────────────────────────────────────


def test_alerts_shape():
    r = client.get("/api/alerts")
    assert r.status_code == 200
    data = r.json()
    assert "active_count" in data
    assert "total_locations" in data
    assert "alerts" in data
    assert isinstance(data["alerts"], list)
    assert data["active_count"] == len(data["alerts"])


# ── /overlay/ionshield.cot ────────────────────────────────────────────────────


def test_cot_feed_no_locations():
    """Returns 404 when no locations are configured."""
    from app.data import locations as loc_mod

    original = loc_mod._locations[:]
    loc_mod._locations.clear()
    try:
        r = client.get("/overlay/ionshield.cot")
        assert r.status_code == 404
    finally:
        loc_mod._locations.extend(original)


def test_cot_feed_with_locations(tmp_path, monkeypatch):
    import json
    from app.data.locations import load_locations, assess_all
    from app.data.noaa import FALLBACK

    loc_file = tmp_path / "locations.json"
    loc_file.write_text(
        json.dumps(
            [
                {"id": "cot_test", "name": "CoT Test", "lat": 38.8, "lon": -104.5},
            ]
        )
    )
    load_locations(str(loc_file))
    assess_all(FALLBACK["kp"])

    r = client.get("/overlay/ionshield.cot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    body = r.text
    assert "<events>" in body
    assert "IONSHIELD-cot_test" in body
    assert "IonShield" in body


# ── CoT XML unit tests ────────────────────────────────────────────────────────


def test_cot_event_well_formed():
    """build_cot_event must produce valid XML with required CoT attributes."""
    import xml.etree.ElementTree as ET
    from app.outputs.cot import build_cot_event

    loc = {
        "id": "test",
        "name": "Test",
        "lat": 38.8,
        "lon": -104.5,
        "asset_type": "GPS_L1",
        "alert_threshold": "ELEVATED",
        "assessment": None,
        "alert": {"active": False, "risk_level": "NOMINAL"},
    }
    xml_str = build_cot_event(loc)
    root = ET.fromstring(xml_str)
    assert root.tag == "event"
    assert root.attrib["uid"] == "IONSHIELD-test"
    assert root.attrib["type"] == "a-u-G"
    point = root.find("point")
    assert point is not None
    assert float(point.attrib["lat"]) == pytest.approx(38.8, abs=1e-4)


def test_cot_argb_values():
    """ARGB values must be valid signed int32 in ATAK's expected range."""
    from app.outputs.cot import _RISK_ARGB

    for level, argb in _RISK_ARGB.items():
        assert -(2**31) <= argb < 0, f"{level} ARGB should be negative signed int32, got {argb}"


# ── Security headers ──────────────────────────────────────────────────────────


def test_security_headers_present():
    r = client.get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "referrer-policy" in r.headers
