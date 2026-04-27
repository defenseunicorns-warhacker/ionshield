"""Tests for the A5 v3 output API."""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)


# ── /health ──────────────────────────────────────────────────────────────────


def test_v3_health_returns_pipeline_status():
    r = client.get("/api/v3/health")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body
    assert "noaa_feeds" in body
    assert "iono_feeds" in body
    assert "data_age_seconds" in body
    assert isinstance(body["noaa_feeds"], dict)


# ── /risk-map ────────────────────────────────────────────────────────────────


def test_v3_risk_map_returns_global_grid():
    r = client.get("/api/v3/risk-map")
    assert r.status_code == 200
    body = r.json()
    assert body["n_regions"] == 324
    assert len(body["regions"]) == 324
    sample = body["regions"][0]
    assert {"region_id", "lat_deg", "lon_deg", "geomag_lat_deg",
            "tec_tecu", "gps_l1_error_m", "hf_absorption_db",
            "satcom_l_fade_db", "radar_l_range_bias_m"}.issubset(sample)
    assert "drivers" in body
    assert "kp_index" in body["drivers"]


def test_v3_risk_map_bbox_filters_grid():
    """CONUS bbox should yield ~6–9 cells (10°×20° grid resolution)."""
    r = client.get(
        "/api/v3/risk-map",
        params={"bbox": "25,-125,50,-65"},
    )
    assert r.status_code == 200
    body = r.json()
    assert 0 < body["n_regions"] < 50
    for reg in body["regions"]:
        assert 25 <= reg["lat_deg"] <= 50
        assert -125 <= reg["lon_deg"] <= -65


def test_v3_risk_map_bbox_invalid_format_rejected():
    r = client.get("/api/v3/risk-map", params={"bbox": "garbage"})
    # Pydantic regex validator → 422
    assert r.status_code in (400, 422)


# ── /forecast ────────────────────────────────────────────────────────────────


def test_v3_forecast_shape():
    r = client.get("/api/v3/forecast")
    assert r.status_code == 200
    body = r.json()
    assert "current_kp" in body
    assert "storm_probability_24h" in body
    assert isinstance(body["entries"], list)


# ── /events ──────────────────────────────────────────────────────────────────


def test_v3_events_returns_paginated():
    r = client.get("/api/v3/events", params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert "total" in body


def test_v3_events_limit_bounds_enforced():
    assert client.get("/api/v3/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/v3/events", params={"limit": 1000}).status_code == 422


def test_v3_events_active_alias():
    r = client.get("/api/v3/events/active")
    assert r.status_code == 200
    # Every returned row should be non-ENDED
    for e in r.json()["events"]:
        assert e["state"] != "ENDED"


def test_v3_events_unknown_event_type_rejected():
    r = client.get("/api/v3/events", params={"event_type": "NOT_A_TYPE"})
    assert r.status_code == 400


def test_v3_events_filter_by_event_type():
    r = client.get("/api/v3/events", params={"event_type": "GEOMAG_MAIN"})
    assert r.status_code == 200
    for e in r.json()["events"]:
        assert e["event_type"] == "GEOMAG_MAIN"


# ── /impact ──────────────────────────────────────────────────────────────────


def test_v3_impact_returns_full_grid():
    r = client.get("/api/v3/impact")
    assert r.status_code == 200
    body = r.json()
    # 324 regions × 12 systems = 3888 rows
    assert body["n_rows"] == 3888


def test_v3_impact_system_filter():
    r = client.get("/api/v3/impact", params={"system": "GPS"})
    assert r.status_code == 200
    body = r.json()
    # 324 × 5 GPS asset types = 1620
    assert body["n_rows"] == 324 * 5
    for row in body["rows"]:
        assert row["system"] == "GPS"


def test_v3_impact_subsystem_filter():
    r = client.get(
        "/api/v3/impact",
        params={"system": "GPS", "subsystem": "GPS_L1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["n_rows"] == 324  # one per region
    for row in body["rows"]:
        assert row["subsystem"] == "GPS_L1"


def test_v3_impact_unknown_system_rejected():
    r = client.get("/api/v3/impact", params={"system": "BANANAS"})
    assert r.status_code == 400


# ── /regions/{region_id} ─────────────────────────────────────────────────────


def test_v3_region_detail_known_region():
    r = client.get("/api/v3/regions/R+035-090")
    assert r.status_code == 200
    body = r.json()
    assert body["region"]["region_id"] == "R+035-090"
    assert "GPS_L1" in body["gps"]
    assert "L" in body["satcom"]
    assert "L" in body["radar"]
    assert "absorption_total_db" in body["hf"]


def test_v3_region_detail_unknown_404s():
    r = client.get("/api/v3/regions/R+999+999")
    assert r.status_code == 404


# ── OpenAPI ──────────────────────────────────────────────────────────────────


def test_v3_openapi_includes_all_endpoints():
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    expected = {
        "/api/v3/health",
        "/api/v3/risk-map",
        "/api/v3/forecast",
        "/api/v3/events",
        "/api/v3/events/active",
        "/api/v3/impact",
        "/api/v3/regions/{region_id}",
    }
    assert expected.issubset(paths.keys())
