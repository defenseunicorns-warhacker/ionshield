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
    """The hub must surface every integration pathway — single source of truth.

    Post-S4 redesign: dashboard lives in the top nav (it's not an "integration",
    it's the product); the simulation card is removed in favour of GIS-export
    framing; CoT was folded into the ATAK card. The hub now mentions each
    integration's main path either as a link or as a code reference, both of
    which produce a substring match.
    """
    with TestClient(app) as client:
        body = client.get("/integrations").text
    for path in (
        "/atak",
        "/atak/network-link.kml",
        "/atak/offline-pack.kmz",
        "/foundry",
        "/api/v3/foundry/pack",
        "/api/v3/foundry/ontology",
        "/api-console",
        "/overlay/ionshield.cot",
        "/overlay/risk.kml",
        "/overlay/risk.geojson",
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
    """Landing must have a visible CTA into the integrations hub. The exact
    button text is "See Integrations" in the redesigned hero; older copy
    used "Integrations Hub". Either is fine — the link target is what matters."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert "/integrations" in body
    # Either the button label or any case-insensitive mention of "Integrations"
    assert "Integrations" in body
