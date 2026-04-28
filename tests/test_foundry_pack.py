"""Phase 3b — Foundry Workshop pack: ontology, SQL samples, layout, install page."""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from app.main import app
from app.outputs import foundry_pack as fp


# ── Ontology object types ────────────────────────────────────────────────────


def test_ontology_has_four_object_types():
    objects = fp.ontology_objects()
    api_names = {o["apiName"] for o in objects}
    assert {"IonShieldRegion", "IonShieldStormEvent", "IonShieldImpactRow", "IonShieldRawObservation"}.issubset(
        api_names
    )


def test_each_object_type_has_required_keys():
    for obj in fp.ontology_objects():
        for k in ("apiName", "displayName", "primaryKey", "backingDataset", "properties"):
            assert k in obj, f"{obj.get('apiName')} missing {k}"
        assert obj["properties"], "no properties declared"


def test_backing_dataset_env_vars_match_render_yaml():
    """Each object's backingDataset.envVar should be one we actually configure in render.yaml."""
    expected = {
        "FOUNDRY_LOCATION_RISK_RID",
        "FOUNDRY_EVENTS_RID",
        "FOUNDRY_IMPACT_RID",
        "FOUNDRY_SPACE_WEATHER_RAW_RID",
    }
    seen = {o["backingDataset"]["envVar"] for o in fp.ontology_objects()}
    assert seen == expected


def test_relations_reference_existing_object_types():
    api_names = {o["apiName"] for o in fp.ontology_objects()}
    for obj in fp.ontology_objects():
        for rel in obj.get("relations", []):
            assert rel["toApiName"] in api_names, f"{obj['apiName']} → unknown {rel['toApiName']}"


# ── Sample SQL queries ──────────────────────────────────────────────────────


def test_sql_samples_have_at_least_five():
    queries = fp.sample_sql_queries()
    assert len(queries) >= 5
    for q in queries:
        assert q["name"]
        assert q["description"]
        assert "SELECT" in q["sql"].upper()


def test_sql_samples_reference_real_dataset_names():
    sql_blob = " ".join(q["sql"] for q in fp.sample_sql_queries()).lower()
    # At least two of the four dataset names should appear in the sample SQL
    hits = sum(1 for name in ("space_weather_raw", "location_risk", "events", "impact") if name in sql_blob)
    assert hits >= 2


# ── Workshop layout ─────────────────────────────────────────────────────────


def test_workshop_layout_has_three_tabs():
    layout = fp.workshop_layout()
    tabs = layout["tabs"]
    assert len(tabs) == 3
    tab_ids = {t["id"] for t in tabs}
    assert {"live", "history", "drilldown"}.issubset(tab_ids)


def test_workshop_widgets_bind_to_existing_object_types():
    api_names = {o["apiName"] for o in fp.ontology_objects()}
    layout = fp.workshop_layout()
    for tab in layout["tabs"]:
        for widget in tab["widgets"]:
            ot = widget.get("objectType") or widget.get("source")
            if ot:
                assert ot in api_names, f"widget references unknown object type: {ot}"


# ── Pack assembly + JSON serialisation ──────────────────────────────────────


def test_build_pack_is_json_serialisable():
    pack = fp.build_pack()
    j = fp.to_json(pack)
    parsed = json.loads(j)
    assert parsed["version"] == 1
    assert "ontology_objects" in parsed
    assert "sample_sql_queries" in parsed
    assert "workshop_layout" in parsed
    assert "datasets" in parsed


def test_pack_datasets_match_ontology_backing():
    pack = fp.build_pack()
    pack_ds = set(pack["datasets"].keys())
    ontology_ds = {o["backingDataset"]["label"] for o in pack["ontology_objects"]}
    assert pack_ds == ontology_ds


# ── HTTP endpoints ──────────────────────────────────────────────────────────


def test_foundry_pack_endpoint_returns_full_pack():
    with TestClient(app) as client:
        r = client.get("/api/v3/foundry/pack")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert len(body["ontology_objects"]) == 4


def test_foundry_ontology_endpoint_returns_objects():
    with TestClient(app) as client:
        r = client.get("/api/v3/foundry/ontology")
    assert r.status_code == 200
    body = r.json()
    assert "objects" in body
    assert len(body["objects"]) == 4


def test_foundry_sql_endpoint_returns_queries():
    with TestClient(app) as client:
        r = client.get("/api/v3/foundry/sql")
    assert r.status_code == 200
    queries = r.json()["queries"]
    assert len(queries) >= 5


def test_foundry_install_page_renders():
    with TestClient(app) as client:
        r = client.get("/foundry")
    assert r.status_code == 200
    body = r.text
    assert "Foundry" in body
    assert "/api/v3/foundry/pack" in body
    assert "Ontology" in body


def test_foundry_reachable_from_integrations_hub():
    """Foundry is now accessed via the consolidated /integrations hub, not
    the top nav. Verify the hub still surfaces every Foundry entry point."""
    with TestClient(app) as client:
        body = client.get("/integrations").text
    assert "/foundry" in body
    assert "/api/v3/foundry/pack" in body
    assert "/api/v3/foundry/ontology" in body
