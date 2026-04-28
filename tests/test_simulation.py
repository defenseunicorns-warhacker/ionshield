"""B5 — Simulation Mode page + scenarios catalog."""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app


# ── /simulation page ─────────────────────────────────────────────────────────


def test_simulation_page_renders():
    """`/simulation` is the product landing page; `/simulation/run` is the live sim."""
    with TestClient(app) as client:
        # Landing page — explains, CTAs into the live experience
        r = client.get("/simulation")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert "Simulation" in body
        # Must link to the live sim
        assert "/simulation/run" in body

        # Live sim page — Leaflet, scenario JS, customer picker
        r2 = client.get("/simulation/run")
        assert r2.status_code == 200
        body2 = r2.text
        assert "IONSHIELD" in body2
        assert "SIMULATION MODE" in body2
        assert "/static/simulation.js" in body2
        assert "leaflet" in body2.lower()


def test_simulation_js_served_from_static():
    with TestClient(app) as client:
        r = client.get("/static/simulation.js")
        assert r.status_code == 200
        assert "loadScenarios" in r.text
        assert "/api/v3/scenarios/export" in r.text


# ── /api/v3/scenarios catalog ────────────────────────────────────────────────


def test_scenarios_catalog_returns_predefined_storms():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios")
        assert r.status_code == 200
        body = r.json()
        assert "scenarios" in body
        ids = {s["id"] for s in body["scenarios"]}
        assert "may-2024-g5" in ids
        assert "halloween-2003" in ids
        assert "live-7d" in ids


def test_scenarios_each_has_required_fields():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios")
        for s in r.json()["scenarios"]:
            assert "id" in s
            assert "title" in s
            assert "tagline" in s
            assert "summary" in s
            assert "start" in s
            assert "end" in s
            assert "tags" in s


def test_scenarios_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert "/api/v3/scenarios" in schema["paths"]


def test_scenarios_static_file_served():
    """The catalog JSON is also reachable as a raw static asset."""
    with TestClient(app) as client:
        r = client.get("/static/scenarios.json")
        assert r.status_code == 200
        assert "scenarios" in r.json()


def test_simulation_links_to_dashboard():
    """The simulation landing page must link back to the live dashboard."""
    with TestClient(app) as client:
        r = client.get("/simulation")
        assert "/dashboard" in r.text


def test_simulation_run_has_back_link_to_landing():
    """The live sim page must offer a way back to the landing context."""
    with TestClient(app) as client:
        r = client.get("/simulation/run")
        assert r.status_code == 200
        # Either textual back-link or any href to /simulation root
        assert "/simulation" in r.text
