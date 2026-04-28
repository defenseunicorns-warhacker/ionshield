"""B6 — customer profile loader + per-customer scenario API."""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from app.data import customer_profile as cp
from app.main import app


CUSTOMERS_PATH = Path(__file__).parent.parent / "app" / "static" / "customers.json"


# ── Catalog file ────────────────────────────────────────────────────────────


def test_customers_catalog_has_three_default_profiles():
    catalog = json.loads(CUSTOMERS_PATH.read_text())
    ids = {c["id"] for c in catalog["customers"]}
    assert "defense-cyber" in ids
    assert "aerospace-launch" in ids
    assert "commercial-grid" in ids


def test_every_profile_has_required_fields():
    for c in cp.list_profiles():
        for k in ("id", "title", "tagline", "summary", "region_filter", "layer_default", "branding"):
            assert k in c, f"profile {c.get('id')} missing {k}"
        assert c["branding"].get("accent_color")


def test_layer_default_is_one_of_supported():
    for c in cp.list_profiles():
        assert c["layer_default"] in ("hf", "gps", "sat")


# ── Profile loader ──────────────────────────────────────────────────────────


def test_get_profile_returns_none_for_unknown():
    assert cp.get_profile("not-a-customer") is None


def test_apply_profile_overrides_layer_and_region_filter():
    profile = cp.get_profile("defense-cyber")
    base = {
        "id": "may-2024-g5",
        "title": "May 2024",
        "tagline": "x",
        "start": "2024-05-10T00:00:00Z",
        "end": "2024-05-12T00:00:00Z",
        "precomputed": {
            "geojson_url": "/static/scenarios/may-2024-g5/scenario.geojson",
            "kmz_url": "/static/scenarios/may-2024-g5/scenario.kmz",
            "keyframes_url": "/static/scenarios/may-2024-g5/keyframes.csv",
        },
    }
    derived = cp.apply_profile(base, profile)
    assert derived["id"] == "may-2024-g5:defense-cyber"
    assert derived["base_id"] == "may-2024-g5"
    assert derived["customer_id"] == "defense-cyber"
    assert derived["layer"] == "hf"
    assert derived["region_filter"] == profile["region_filter"]
    assert "Defense" in derived["title"]
    # Precomputed URLs got customer suffix injected
    assert "/defense-cyber/" in derived["precomputed"]["geojson_url"]


def test_apply_profile_does_not_mutate_input():
    profile = cp.get_profile("commercial-grid")
    base = {"id": "x", "title": "X", "start": "2024-05-10T00:00:00Z"}
    cp.apply_profile(base, profile)
    assert "layer" not in base  # base unchanged
    assert "customer_id" not in base


def test_derive_scenarios_skips_live_windows():
    derived = cp.derive_scenarios(
        [
            {"id": "live-7d", "start": "live-7d"},
            {"id": "may-2024-g5", "start": "2024-05-10T00:00:00Z"},
        ],
        "defense-cyber",
    )
    assert len(derived) == 1
    assert derived[0]["base_id"] == "may-2024-g5"


def test_derive_scenarios_unknown_customer_returns_empty():
    assert (
        cp.derive_scenarios(
            [{"id": "x", "start": "2024-05-10T00:00:00Z"}],
            "garbage",
        )
        == []
    )


# ── HTTP endpoints ──────────────────────────────────────────────────────────


def test_customers_endpoint_lists_all():
    with TestClient(app) as client:
        body = client.get("/api/v3/customers").json()
        assert "customers" in body
        ids = {c["id"] for c in body["customers"]}
        assert {"defense-cyber", "aerospace-launch", "commercial-grid"}.issubset(ids)


def test_customer_detail_endpoint():
    with TestClient(app) as client:
        body = client.get("/api/v3/customers/defense-cyber").json()
        assert body["profile"]["id"] == "defense-cyber"
        # Concrete scenarios exposed; live one is skipped
        assert len(body["derived_scenarios"]) >= 3
        for sc in body["derived_scenarios"]:
            assert sc["customer_id"] == "defense-cyber"
            assert sc["layer"] == "hf"


def test_customer_detail_endpoint_404():
    with TestClient(app) as client:
        r = client.get("/api/v3/customers/garbage")
        assert r.status_code == 404


def test_customer_aware_scenarios_endpoint():
    with TestClient(app) as client:
        body = client.get("/api/v3/scenarios/customer/aerospace-launch").json()
        assert "scenarios" in body
        assert "profile" in body
        assert body["profile"]["id"] == "aerospace-launch"
        for sc in body["scenarios"]:
            assert sc["customer_id"] == "aerospace-launch"
            assert sc["layer"] == "gps"
            # Region filter applied
            assert "region_filter" in sc


def test_customer_endpoints_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        for p in (
            "/api/v3/customers",
            "/api/v3/customers/{customer_id}",
            "/api/v3/scenarios/customer/{customer_id}",
        ):
            assert p in schema["paths"]


# ── Frontend wiring ─────────────────────────────────────────────────────────


def test_simulation_page_has_customer_picker():
    # Customer picker now lives on the live sim page (/simulation/run);
    # /simulation is the marketing landing.
    html = (Path(__file__).parent.parent / "app" / "pages" / "simulation_run.html").read_text()
    assert 'id="customer-picker"' in html
    assert "All audiences" in html


def test_simulation_js_loads_customers_and_reacts():
    js = (Path(__file__).parent.parent / "app" / "static" / "simulation.js").read_text()
    assert "loadCustomers" in js
    assert "/api/v3/customers" in js
    assert "/api/v3/scenarios/customer/" in js
    assert "applyBranding" in js
