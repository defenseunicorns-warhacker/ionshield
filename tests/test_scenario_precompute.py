"""B3 — pre-computed scenario datasets."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module, scenario_precompute
from app.data.db import noaa_snapshots
from app.main import app


CATALOG_PATH = Path(__file__).parent.parent / "app" / "static" / "scenarios.json"


@pytest_asyncio.fixture
async def memory_db_with_storm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
        # Seed 4 May-2024 backfill rows so the precompute has data to chew
        base = datetime(2024, 5, 11, 0, 30, tzinfo=timezone.utc)
        for i, kp in enumerate([7.0, 9.0, 8.7, 6.5]):
            await conn.execute(
                insert(noaa_snapshots).values(
                    fetched_at=base + timedelta(hours=i),
                    fetch_source="historical_backfill",
                    kp=kp,
                    bz_nt=-30.0,
                    xray_flux=4e-4,
                    proton_flux_10mev=200.0,
                    wind_speed_km_s=800.0,
                    feeds_available="[]",
                    feeds_unavailable="[]",
                    data_age_seconds=0,
                )
            )
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


# ── Catalog shape ────────────────────────────────────────────────────────────


def test_catalog_has_all_four_impact_scenarios():
    catalog = json.loads(CATALOG_PATH.read_text())
    ids = {s["id"] for s in catalog["scenarios"]}
    assert "geomag-progression" in ids
    assert "gps-spread" in ids
    assert "hf-blackout" in ids
    assert "satcom-disruption" in ids


def test_every_concrete_scenario_has_precomputed_urls():
    catalog = json.loads(CATALOG_PATH.read_text())
    for sc in catalog["scenarios"]:
        if str(sc.get("start", "")).startswith("live"):
            continue
        pc = sc.get("precomputed")
        assert pc is not None, sc["id"]
        assert pc["geojson_url"].startswith("/static/scenarios/")
        assert pc["kmz_url"].endswith(".kmz")
        assert pc["keyframes_url"].endswith(".csv")


def test_live_scenarios_have_no_precomputed_block():
    catalog = json.loads(CATALOG_PATH.read_text())
    live = next(s for s in catalog["scenarios"] if s["id"] == "live-7d")
    assert live["precomputed"] is None


# ── Precompute helpers ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_precompute_skips_live_window(memory_db_with_storm, tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    sc = {"id": "live-7d", "start": "live-7d", "end": "now"}
    result = await scenario_precompute.precompute_scenario(sc)
    assert result["written"] == []
    assert result["skipped_reason"] == "live_window"


@pytest.mark.asyncio
async def test_precompute_returns_no_features_when_db_empty(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    sc = {
        "id": "future-event",
        "start": "2099-01-01T00:00:00Z",
        "end": "2099-01-02T00:00:00Z",
        "step_seconds": 3600,
    }
    result = await scenario_precompute.precompute_scenario(sc)
    assert result["skipped_reason"] == "no_features"


@pytest.mark.asyncio
async def test_precompute_writes_three_artifacts(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    out_dir = tmp_path / "out"
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", out_dir)
    sc = {
        "id": "test-scenario",
        "title": "Test",
        "start": "2024-05-11T00:00:00Z",
        "end": "2024-05-11T04:00:00Z",
        "step_seconds": 0,
    }
    result = await scenario_precompute.precompute_scenario(sc)
    assert result["written"] == [
        "/static/scenarios/test-scenario/scenario.geojson",
        "/static/scenarios/test-scenario/scenario.kmz",
        "/static/scenarios/test-scenario/keyframes.csv",
    ]
    # Verify files exist + non-trivial size
    geojson_path = out_dir / "test-scenario" / "scenario.geojson"
    kmz_path = out_dir / "test-scenario" / "scenario.kmz"
    keyframes_path = out_dir / "test-scenario" / "keyframes.csv"
    assert geojson_path.exists() and geojson_path.stat().st_size > 200
    assert kmz_path.exists() and kmz_path.stat().st_size > 500
    assert keyframes_path.exists() and keyframes_path.stat().st_size > 200
    # KMZ must be a valid zip
    assert kmz_path.read_bytes()[:2] == b"PK"
    # GeoJSON must parse
    fc = json.loads(geojson_path.read_text())
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) > 0


@pytest.mark.asyncio
async def test_precompute_is_idempotent_overwrite(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    out_dir = tmp_path / "out"
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", out_dir)
    sc = {
        "id": "test",
        "title": "T",
        "start": "2024-05-11T00:00:00Z",
        "end": "2024-05-11T04:00:00Z",
        "step_seconds": 0,
    }
    r1 = await scenario_precompute.precompute_scenario(sc)
    r2 = await scenario_precompute.precompute_scenario(sc)
    assert r1["written"] == r2["written"]
    # Second run produced the same files (no FileExistsError)
    assert r1["geojson_bytes"] == r2["geojson_bytes"]


# ── HTTP endpoint ────────────────────────────────────────────────────────────


def test_precompute_endpoint_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert "/api/v3/scenarios/precompute" in schema["paths"]


def test_precompute_endpoint_returns_results():
    with TestClient(app) as client:
        r = client.post("/api/v3/scenarios/precompute", params={"only_id": "live-7d"})
        assert r.status_code == 200
        body = r.json()
        assert "results" in body
        assert body["scenarios_processed"] == 1
        # live-7d skips with reason
        assert body["results"][0]["skipped_reason"] == "live_window"


# ── simulation.js prefers precomputed ───────────────────────────────────────


def test_simulation_js_checks_precomputed_first():
    js = (Path(__file__).parent.parent / "app" / "static" / "simulation.js").read_text()
    assert "sc.precomputed && sc.precomputed.geojson_url" in js
    assert "Fall back to the" in js  # comment proving fallback documented


# ── Caveat 1: lifespan auto-bootstrap ───────────────────────────────────────


def test_main_lifespan_runs_scenario_bootstrap():
    main_text = (Path(__file__).parent.parent / "app" / "main.py").read_text()
    assert "_bootstrap_scenarios" in main_text
    assert "scenario_precompute" in main_text


# ── Caveat 2: per-scenario geometry override ────────────────────────────────


@pytest.mark.asyncio
async def test_precompute_respects_geometry_override(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    sc = {
        "id": "pt-test",
        "title": "T",
        "start": "2024-05-11T00:00:00Z",
        "end": "2024-05-11T04:00:00Z",
        "step_seconds": 0,
        "geometry": "point",
    }
    await scenario_precompute.precompute_scenario(sc)
    fc = json.loads((tmp_path / "out" / "pt-test" / "scenario.geojson").read_text())
    types = {f["geometry"]["type"] for f in fc["features"]}
    assert types == {"Point"}


# ── Caveat 3: manifest + cache-busting ──────────────────────────────────────


@pytest.mark.asyncio
async def test_precompute_writes_manifest_with_per_file_hashes(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    sc = {
        "id": "h-test",
        "title": "T",
        "start": "2024-05-11T00:00:00Z",
        "end": "2024-05-11T04:00:00Z",
        "step_seconds": 0,
    }
    await scenario_precompute.precompute_scenario(sc)
    manifest = json.loads((tmp_path / "out" / "h-test" / "manifest.json").read_text())
    assert set(manifest["files"].keys()) == {
        "scenario.geojson",
        "scenario.kmz",
        "keyframes.csv",
    }
    for entry in manifest["files"].values():
        assert len(entry["hash"]) == 8
        assert entry["bytes"] > 0


@pytest.mark.asyncio
async def test_load_manifest_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    assert scenario_precompute.load_manifest("nonexistent") is None


@pytest.mark.asyncio
async def test_precompute_hash_changes_when_content_changes(
    memory_db_with_storm,
    tmp_path,
    monkeypatch,
):
    """Re-running with different windows should produce different hashes."""
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")
    sc1 = {"id": "x", "start": "2024-05-11T00:00:00Z", "end": "2024-05-11T02:00:00Z", "step_seconds": 0}
    sc2 = {"id": "x", "start": "2024-05-11T00:00:00Z", "end": "2024-05-11T04:00:00Z", "step_seconds": 0}
    await scenario_precompute.precompute_scenario(sc1)
    h1 = scenario_precompute.load_manifest("x")["files"]["scenario.geojson"]["hash"]
    await scenario_precompute.precompute_scenario(sc2)
    h2 = scenario_precompute.load_manifest("x")["files"]["scenario.geojson"]["hash"]
    assert h1 != h2


def test_catalog_endpoint_appends_cache_bust_when_manifest_exists(
    tmp_path,
    monkeypatch,
):
    """Mock OUTPUT_ROOT to a tmp dir with a fake manifest, hit /api/v3/scenarios."""
    out_dir = tmp_path / "out" / "may-2024-g5"
    out_dir.mkdir(parents=True)
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "scenario_id": "may-2024-g5",
                "computed_at": "2026-01-01T00:00:00+00:00",
                "n_features": 100,
                "n_snapshots": 10,
                "files": {
                    "scenario.geojson": {"hash": "abc12345", "bytes": 1234},
                    "scenario.kmz": {"hash": "def45678", "bytes": 567},
                    "keyframes.csv": {"hash": "fed98765", "bytes": 89},
                },
            }
        )
    )
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "out")

    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios")
        assert r.status_code == 200
        scenarios = r.json()["scenarios"]
        may = next(s for s in scenarios if s["id"] == "may-2024-g5")
        assert may["precomputed"]["geojson_url"].endswith("?v=abc12345")
        assert may["precomputed"]["kmz_url"].endswith("?v=def45678")
        assert may["precomputed"]["keyframes_url"].endswith("?v=fed98765")
        assert may["precomputed_manifest"]["n_features"] == 100


def test_catalog_endpoint_omits_cache_bust_when_no_manifest(tmp_path, monkeypatch):
    """No manifest → no ?v= suffix; URLs match the catalog file as-is."""
    monkeypatch.setattr(scenario_precompute, "OUTPUT_ROOT", tmp_path / "empty")
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios")
        scenarios = r.json()["scenarios"]
        may = next(s for s in scenarios if s["id"] == "may-2024-g5")
        # No manifest exists → unchanged URL
        assert "?v=" not in may["precomputed"]["geojson_url"]


# ── Caveat 4: download buttons ──────────────────────────────────────────────


def test_simulation_html_has_download_buttons():
    html = (Path(__file__).parent.parent / "app" / "pages" / "simulation.html").read_text()
    assert 'id="dl-kmz"' in html
    assert 'id="dl-csv"' in html
    assert 'id="dl-gj"' in html
    assert "download" in html  # download attribute


def test_simulation_js_wires_download_buttons():
    js = (Path(__file__).parent.parent / "app" / "static" / "simulation.js").read_text()
    assert "dl-kmz" in js and "dl-csv" in js and "dl-gj" in js
    assert "pc.kmz_url" in js
