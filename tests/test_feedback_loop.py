"""
A7 — feedback loop: persistence, drift, retrain pipeline, API endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module
from app.data import feedback_store
from app.main import app
from app.models import retrain as retrain_module


@pytest_asyncio.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


# ── feedback_store ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_sample_and_list(memory_db):
    sid = await feedback_store.record_sample(
        features=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
        rule_label="BACKGROUND",
        ml_label="BACKGROUND",
        ml_confidence=0.92,
    )
    assert sid is not None
    rows = await feedback_store.list_samples()
    assert len(rows) == 1
    assert rows[0]["rule_label"] == "BACKGROUND"
    assert rows[0]["ml_confidence"] == 0.92


@pytest.mark.asyncio
async def test_attach_feedback_persists_correction(memory_db):
    sid = await feedback_store.record_sample(
        features=[0] * 7,
        rule_label="BACKGROUND",
        ml_label="FLARE_M",
        ml_confidence=0.6,
    )
    ok = await feedback_store.attach_feedback(sid, "NOT_AN_EVENT")
    assert ok is True
    rows = await feedback_store.list_samples(only_with_feedback=True)
    assert len(rows) == 1
    assert rows[0]["user_feedback"] == "NOT_AN_EVENT"


@pytest.mark.asyncio
async def test_drift_metrics_compute_agreement(memory_db):
    # 3 agreements + 1 disagreement → 0.75 agreement
    for _ in range(3):
        await feedback_store.record_sample(
            features=[0] * 7,
            rule_label="BACKGROUND",
            ml_label="BACKGROUND",
            ml_confidence=0.9,
        )
    await feedback_store.record_sample(
        features=[0] * 7,
        rule_label="BACKGROUND",
        ml_label="FLARE_M",
        ml_confidence=0.5,
    )
    d = await feedback_store.drift_metrics()
    assert d["n"] == 4
    assert d["agreement"] == 0.75
    # Confusion table: 3 BG→BG, 1 BG→FLARE_M
    assert d["by_class"]["BACKGROUND"]["BACKGROUND"] == 3
    assert d["by_class"]["BACKGROUND"]["FLARE_M"] == 1


@pytest.mark.asyncio
async def test_record_outcome(memory_db):
    rid = await feedback_store.record_outcome(
        system="GPS",
        subsystem="GPS_L1",
        metric="error_m",
        observed_value=4.7,
        observed_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
        region_id="R+035-090",
        source="receiver-1",
    )
    assert rid is not None
    rows = await feedback_store.list_outcomes()
    assert rows[0]["observed_value"] == 4.7


@pytest.mark.asyncio
async def test_register_model_version_atomic_swap(memory_db):
    v1 = await feedback_store.register_model_version(
        version="logreg-v2",
        n_train=100,
        n_real_samples=10,
        train_accuracy=0.9,
        artifact_path="/x.json",
    )
    v2 = await feedback_store.register_model_version(
        version="logreg-v3",
        n_train=200,
        n_real_samples=50,
        train_accuracy=0.95,
        artifact_path="/y.json",
    )
    assert v1 != v2
    active = await feedback_store.active_model_version()
    assert active["version"] == "logreg-v3"  # latest is active
    versions = await feedback_store.list_model_versions()
    actives = [v for v in versions if v["active"]]
    assert len(actives) == 1


# ── retrain pipeline ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrain_noop_with_no_samples(memory_db):
    result = await retrain_module.retrain_and_maybe_swap()
    assert result["status"] == "noop"


@pytest.mark.asyncio
async def test_retrain_swaps_when_above_threshold(memory_db, tmp_path, monkeypatch):
    """Seed enough rule-labeled samples for a clean swap."""
    from app.models import ml_classifier as mlc

    # Redirect the live artifact path into tmp so we don't clobber the bundled one
    monkeypatch.setattr(mlc, "ARTIFACT_PATH", tmp_path / "live.json")

    # Seed 200 BACKGROUND samples with realistic feature vectors
    import random

    rng = random.Random(11)
    for _ in range(200):
        feats = mlc._synth_sample(rng, "BACKGROUND")
        await feedback_store.record_sample(
            features=feats,
            rule_label="BACKGROUND",
            ml_label="BACKGROUND",
            ml_confidence=0.8,
        )

    result = await retrain_module.retrain_and_maybe_swap()
    assert result["status"] == "swapped"
    assert result["validation_accuracy"] >= 0.80
    assert result["version"].startswith("logreg-v")
    # Active version recorded
    active = await feedback_store.active_model_version()
    assert active["version"] == result["version"]


# ── API endpoints ────────────────────────────────────────────────────────────


def test_v3_feedback_endpoint_rejects_bad_label():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/training/samples/1/feedback",
            json={"user_feedback": "NOT_A_REAL_LABEL"},
        )
        assert r.status_code == 400


def test_v3_feedback_endpoint_404_on_unknown_sample():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/training/samples/9999999/feedback",
            json={"user_feedback": "BACKGROUND"},
        )
        assert r.status_code == 404


def test_v3_outcome_endpoint_persists():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/outcomes",
            json={
                "system": "GPS",
                "subsystem": "GPS_L1",
                "metric": "error_m",
                "observed_value": 3.2,
                "observed_at": "2026-04-26T12:00:00Z",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["accepted"] is True


def test_v3_outcome_endpoint_bad_timestamp():
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/outcomes",
            json={
                "system": "GPS",
                "subsystem": "GPS_L1",
                "metric": "error_m",
                "observed_value": 3.2,
                "observed_at": "not-a-time",
            },
        )
        assert r.status_code == 400


def test_v3_training_drift_endpoint():
    with TestClient(app) as client:
        r = client.get("/api/v3/training/drift")
        assert r.status_code == 200
        body = r.json()
        assert "agreement" in body
        assert "by_class" in body


def test_v3_training_models_endpoint():
    with TestClient(app) as client:
        r = client.get("/api/v3/training/models")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_v3_training_samples_endpoint():
    with TestClient(app) as client:
        r = client.get("/api/v3/training/samples")
        assert r.status_code == 200
        assert "total" in r.json()


def test_v3_training_retrain_endpoint_returns_status():
    with TestClient(app) as client:
        r = client.post("/api/v3/training/retrain")
        assert r.status_code == 200
        # Depends on local sample state: "noop" (no samples), "rejected"
        # (worse than champion), "swapped" (instant promote), or
        # "challenger_registered" (champion/challenger shadow window).
        assert r.json()["status"] in {
            "noop",
            "swapped",
            "rejected",
            "challenger_registered",
        }


def test_v3_a7_endpoints_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        for p in (
            "/api/v3/training/samples/{sample_id}/feedback",
            "/api/v3/outcomes",
            "/api/v3/training/samples",
            "/api/v3/training/drift",
            "/api/v3/training/models",
            "/api/v3/training/retrain",
        ):
            assert p in paths
