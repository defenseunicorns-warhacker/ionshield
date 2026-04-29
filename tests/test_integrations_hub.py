"""Phase 3.5 — Integrations Hub + API Console pages."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from app.main import app


# ── /integrations hub ───────────────────────────────────────────────────────


def test_integrations_page_renders():
    with TestClient(app) as client:
        r = client.get("/integrations")
    assert r.status_code == 200


def test_integrations_page_links_every_surface():
    """The hub must link to every integration we've shipped — single source of truth."""
    with TestClient(app) as client:
        body = client.get("/integrations").text
    for path in (
        "/dashboard",
        "/simulation",
        "/atak",
        "/atak/network-link.kml",
        "/atak/offline-pack.kmz",
        "/foundry",
        "/api/v3/foundry/pack",
        "/api/v3/foundry/ontology",
        "/api-console",
        "/api/v3/forecast/kp",
        "/overlay/ionshield.cot",
        "/overlay/risk.kml",
        "/overlay/risk.geojson",
        "/api/v3/scenarios",
    ):
        assert path in body, f"hub missing link to {path}"


def test_integrations_page_pulls_live_status():
    with TestClient(app) as client:
        body = client.get("/integrations").text
    # The live-strip JS hits /api/v3/health, /forecast/kp, /scenarios on load.
    assert "/api/v3/health" in body
    assert "/api/v3/forecast/kp" in body
    assert "/api/v3/scenarios" in body


# ── /api-console ────────────────────────────────────────────────────────────


def test_api_console_page_renders():
    with TestClient(app) as client:
        r = client.get("/api-console")
    assert r.status_code == 200


def test_api_console_offers_at_least_8_endpoints():
    with TestClient(app) as client:
        body = client.get("/api-console").text
    # Endpoint picker buttons — verify we list a useful set
    expected = [
        "/api/v3/health",
        "/api/v3/risk-map",
        "/api/v3/forecast/kp",
        "/api/v3/events/active",
        "/api/v3/scenarios",
        "/api/v3/customers",
        "/api/v3/foundry/pack",
        "/api/v3/training/models",
    ]
    for path in expected:
        assert path in body, f"console missing {path}"


def test_api_console_supports_bearer_token_input():
    with TestClient(app) as client:
        body = client.get("/api-console").text
    assert "Bearer" in body
    assert 'id="token"' in body
    # Token persistence to localStorage so users don't re-paste every time
    assert "localStorage" in body


# ── Nav wiring ──────────────────────────────────────────────────────────────


def test_integrations_link_in_nav():
    js = (Path(__file__).parent.parent / "app" / "static" / "nav.js").read_text()
    assert "/integrations" in js
    assert "Integrations" in js


def test_landing_page_has_integrations_cta():
    """Landing must have a working CTA into the integrations hub. Button copy
    has shifted between "Integrations Hub" and "See Integrations" — the
    link target /integrations is what matters."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert "/integrations" in body
    assert "Integrations" in body
