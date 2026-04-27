"""B4 — Earth Studio export workflow: recipe endpoint, video sidecar, runbook + scripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app


# ── Recipe data in scenarios.json ───────────────────────────────────────────


CATALOG_PATH = Path(__file__).parent.parent / "app" / "static" / "scenarios.json"


def test_every_concrete_scenario_has_a_recipe():
    catalog = json.loads(CATALOG_PATH.read_text())
    for sc in catalog["scenarios"]:
        if str(sc.get("start", "")).startswith("live"):
            continue
        assert "recipe" in sc, f"scenario {sc['id']} missing recipe"
        r = sc["recipe"]
        assert r["duration_seconds"] > 0
        assert r["frame_rate"] > 0
        assert isinstance(r["camera"], list) and len(r["camera"]) >= 2
        # Each waypoint has the required keys
        for wp in r["camera"]:
            for k in ("t", "lat", "lon", "altitude_m", "heading", "tilt"):
                assert k in wp, f"{sc['id']} waypoint missing {k}"
        assert r["render"]["format"] == "mp4"
        # Camera waypoints must be monotonic in time
        ts = [wp["t"] for wp in r["camera"]]
        assert ts == sorted(ts)
        # Final waypoint must not exceed duration
        assert ts[-1] <= r["duration_seconds"]


def test_live_scenario_has_no_recipe():
    catalog = json.loads(CATALOG_PATH.read_text())
    live = next(s for s in catalog["scenarios"] if s["id"] == "live-7d")
    assert "recipe" not in live


# ── Recipe endpoint ─────────────────────────────────────────────────────────


def test_recipe_endpoint_returns_camera_path():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/may-2024-g5/recipe")
        assert r.status_code == 200
        body = r.json()
        assert body["scenario_id"] == "may-2024-g5"
        assert "recipe" in body
        assert body["recipe"]["duration_seconds"] == 30
        assert len(body["recipe"]["camera"]) == 4
        # Includes precomputed download URLs for the prepare script
        assert "downloads" in body
        assert "kmz_url" in body["downloads"]


def test_recipe_endpoint_404_for_unknown_scenario():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/not-a-scenario/recipe")
        assert r.status_code == 404


def test_recipe_endpoint_404_for_live_scenario():
    """Live scenarios have no recipe; endpoint returns 404 with helpful detail."""
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/live-7d/recipe")
        assert r.status_code == 404
        assert "no recipe" in r.json()["detail"]


# ── Video sidecar ───────────────────────────────────────────────────────────


@pytest.fixture
def isolate_video_sidecars(tmp_path, monkeypatch):
    """Redirect video sidecar writes into a tmp dir so tests don't litter."""
    from app.data import scenario_precompute as sp

    monkeypatch.setattr(sp, "OUTPUT_ROOT", tmp_path / "out")
    yield tmp_path


def test_register_video_writes_sidecar(isolate_video_sidecars):
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/scenarios/may-2024-g5/video",
            json={"video_url": "https://cdn.example.com/may.mp4", "duration_seconds": 30, "notes": "test render"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["registered"] is True
        assert body["video"]["video_url"] == "https://cdn.example.com/may.mp4"
        # File written
        sidecar = isolate_video_sidecars / "out" / "may-2024-g5" / "video.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["video_url"] == "https://cdn.example.com/may.mp4"
        assert data["duration_seconds"] == 30
        assert "rendered_at" in data


def test_register_video_unknown_scenario_404(isolate_video_sidecars):
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/scenarios/garbage/video",
            json={"video_url": "https://x.example.com/v.mp4"},
        )
        assert r.status_code == 404


def test_unregister_video_removes_sidecar(isolate_video_sidecars):
    with TestClient(app) as client:
        client.post(
            "/api/v3/scenarios/may-2024-g5/video",
            json={"video_url": "https://x.example.com/v.mp4"},
        )
        r = client.delete("/api/v3/scenarios/may-2024-g5/video")
        assert r.status_code == 200
        assert r.json()["removed"] is True

    sidecar = isolate_video_sidecars / "out" / "may-2024-g5" / "video.json"
    assert not sidecar.exists()


def test_unregister_video_404_when_not_registered(isolate_video_sidecars):
    with TestClient(app) as client:
        r = client.delete("/api/v3/scenarios/may-2024-g5/video")
        assert r.status_code == 404


def test_catalog_merges_video_sidecar(isolate_video_sidecars):
    with TestClient(app) as client:
        # Pre-state: video_url is null (catalog default)
        body = client.get("/api/v3/scenarios").json()
        may = next(s for s in body["scenarios"] if s["id"] == "may-2024-g5")
        assert may.get("video_url") in (None, "")

        # Register
        client.post(
            "/api/v3/scenarios/may-2024-g5/video",
            json={"video_url": "https://cdn.example.com/may.mp4", "duration_seconds": 30, "notes": "demo render"},
        )

        # After-state: catalog merges sidecar over the catalog field
        body = client.get("/api/v3/scenarios").json()
        may = next(s for s in body["scenarios"] if s["id"] == "may-2024-g5")
        assert may["video_url"] == "https://cdn.example.com/may.mp4"
        assert may["video_meta"]["duration_seconds"] == 30
        assert may["video_meta"]["notes"] == "demo render"


# ── Runbook + scripts ───────────────────────────────────────────────────────


def test_runbook_exists_and_documents_workflow():
    runbook = Path(__file__).parent.parent / "docs" / "earth-studio-workflow.md"
    assert runbook.exists()
    text = runbook.read_text()
    # Spot-check the core workflow steps
    assert "scripts/prepare_scenario.sh" in text
    assert "scripts/publish_scenario_video.sh" in text
    assert "Earth Studio" in text
    assert "scenario.kmz" in text
    assert "recipe.json" in text
    assert "video.json" in text


def test_prepare_script_references_recipe_endpoint():
    script = Path(__file__).parent.parent / "scripts" / "prepare_scenario.sh"
    assert script.exists()
    text = script.read_text()
    assert "/api/v3/scenarios" in text
    assert "/recipe" in text


def test_publish_script_calls_video_endpoint():
    script = Path(__file__).parent.parent / "scripts" / "publish_scenario_video.sh"
    assert script.exists()
    text = script.read_text()
    assert "/api/v3/scenarios/" in text
    assert "/video" in text


def test_b4_endpoints_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/api/v3/scenarios/{scenario_id}/recipe" in paths
        assert "/api/v3/scenarios/{scenario_id}/video" in paths
