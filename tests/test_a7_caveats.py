"""
Tests for the A7 caveat fixes:
  1. Sample TTL + Foundry archive
  2. Auto-retrain scheduler (drift-driven) + auto-promote
  3. Classifier hot-cache + invalidate_classifier
  4. Champion/challenger shadow mode + promote/retire flow
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.data import db as db_module, feedback_store, sample_archive
from app.models import auto_pilot, ml_classifier as mlc


@pytest_asyncio.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


# ── Caveat 1: sample archive ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_skipped_when_unconfigured(memory_db):
    out = await sample_archive.archive_aged_samples()
    assert out["archived"] == 0
    assert out["skipped_reason"] == "foundry_archive_not_configured"


@pytest.mark.asyncio
async def test_archive_returns_no_aged_rows(memory_db, monkeypatch):
    """Configured but no rows older than cutoff → no_aged_rows."""
    # Inject fake foundry config
    from app.config import settings

    monkeypatch.setattr(settings, "foundry_sync_enabled", True)
    monkeypatch.setattr(settings, "foundry_stack_url", "https://x")
    monkeypatch.setattr(settings, "foundry_token", type(settings.foundry_token)("T"))
    monkeypatch.setattr(settings, "foundry_training_archive_rid", "rid")

    # Insert a fresh sample (not aged)
    await feedback_store.record_sample(
        features=[0] * 7,
        rule_label="BACKGROUND",
        ml_label="BACKGROUND",
        ml_confidence=0.9,
    )
    out = await sample_archive.archive_aged_samples(max_age_days=1)
    assert out["archived"] == 0
    assert out["skipped_reason"] == "no_aged_rows"


@pytest.mark.asyncio
async def test_archive_uploads_and_deletes(memory_db, monkeypatch):
    """Aged rows → uploaded then removed from DB."""
    from app.config import settings

    monkeypatch.setattr(settings, "foundry_sync_enabled", True)
    monkeypatch.setattr(settings, "foundry_stack_url", "https://x")
    monkeypatch.setattr(settings, "foundry_token", type(settings.foundry_token)("T"))
    monkeypatch.setattr(settings, "foundry_training_archive_rid", "rid")

    # Stub sync_rows to "succeed" without hitting the network
    captured: list[list[dict]] = []

    async def fake_sync_rows(rows, **kw):
        captured.append(list(rows))
        return True

    monkeypatch.setattr(sample_archive, "sync_rows", fake_sync_rows)

    sid = await feedback_store.record_sample(
        features=[1.0] * 7,
        rule_label="BACKGROUND",
        ml_label="BACKGROUND",
        ml_confidence=0.95,
    )
    # Force created_at into the past
    from sqlalchemy import update

    async with db_module.get_engine().begin() as conn:
        await conn.execute(
            update(db_module.training_samples)
            .where(db_module.training_samples.c.id == sid)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=60))
        )

    out = await sample_archive.archive_aged_samples(max_age_days=30)
    assert out["archived"] == 1
    assert out["deleted"] == 1
    assert len(captured) == 1
    assert captured[0][0]["features_json"] == "[1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]"

    # DB should now be empty
    rows = await feedback_store.list_samples()
    assert rows == []


# ── Caveat 3: classifier cache + invalidate ─────────────────────────────────


def test_classifier_cache_returns_same_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(mlc, "ARTIFACT_PATH", Path(__file__).parent.parent / "app/models/event_classifier.json")
    mlc.invalidate_classifier()
    a = mlc.get_classifier()
    b = mlc.get_classifier()
    assert a is b


def test_invalidate_classifier_forces_reload(tmp_path):
    mlc.invalidate_classifier()
    a = mlc.get_classifier()
    mlc.invalidate_classifier()
    b = mlc.get_classifier()
    # New instance but same classes
    assert a is not None and b is not None
    assert a is not b
    assert a.classes == b.classes


# ── Caveat 4: champion/challenger ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_active_then_challenger(memory_db):
    await feedback_store.register_model_version(
        version="logreg-v2",
        n_train=100,
        n_real_samples=10,
        train_accuracy=0.9,
        artifact_path="/a.json",
        activate=True,
        challenger=False,
    )
    await feedback_store.register_model_version(
        version="logreg-v3",
        n_train=200,
        n_real_samples=50,
        train_accuracy=0.92,
        artifact_path="/b.json",
        activate=False,
        challenger=True,
    )
    active = await feedback_store.active_model_version()
    challenger = await feedback_store.challenger_model_version()
    assert active["version"] == "logreg-v2"
    assert challenger["version"] == "logreg-v3"


@pytest.mark.asyncio
async def test_promote_challenger_swaps_active(memory_db):
    await feedback_store.register_model_version(
        version="logreg-v2",
        n_train=100,
        n_real_samples=10,
        train_accuracy=0.9,
        artifact_path="/a.json",
        activate=True,
        challenger=False,
    )
    await feedback_store.register_model_version(
        version="logreg-v3",
        n_train=200,
        n_real_samples=50,
        train_accuracy=0.92,
        artifact_path="/b.json",
        activate=False,
        challenger=True,
    )
    ok = await feedback_store.promote_challenger("logreg-v3")
    assert ok is True
    active = await feedback_store.active_model_version()
    assert active["version"] == "logreg-v3"
    challenger = await feedback_store.challenger_model_version()
    assert challenger is None


@pytest.mark.asyncio
async def test_retire_challenger(memory_db):
    await feedback_store.register_model_version(
        version="logreg-v3",
        n_train=200,
        n_real_samples=50,
        train_accuracy=0.92,
        artifact_path="/b.json",
        activate=False,
        challenger=True,
    )
    assert await feedback_store.retire_challenger() is True
    assert await feedback_store.challenger_model_version() is None


@pytest.mark.asyncio
async def test_register_model_version_rejects_active_and_challenger(memory_db):
    with pytest.raises(ValueError):
        await feedback_store.register_model_version(
            version="x",
            n_train=1,
            n_real_samples=1,
            train_accuracy=0.9,
            artifact_path="/x",
            activate=True,
            challenger=True,
        )


@pytest.mark.asyncio
async def test_shadow_metrics_compute_advantage(memory_db):
    # 3 samples where champion matches rule, challenger mostly does
    for i in range(3):
        await feedback_store.record_sample(
            features=[0] * 7,
            rule_label="BACKGROUND",
            ml_label="BACKGROUND",
            ml_confidence=0.9,
            challenger_label=("BACKGROUND" if i < 2 else "FLARE_M"),
            challenger_confidence=0.8,
        )
    m = await feedback_store.shadow_metrics(window=10)
    assert m["n"] == 3
    assert m["champion_agreement"] == 1.0
    assert m["challenger_agreement"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["advantage"] < 0


# ── Caveat 2: auto-pilot ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_retrain_skipped_when_drift_within_threshold(memory_db):
    # Reset module-level cooldown state. Setting far in the past — `0.0` is
    # not enough on fresh CI runners where `time.monotonic()` is also small,
    # which would falsely register a recent retrain.
    auto_pilot._last_retrain_at = -1e10
    # Seed samples where ml agrees with rule → high agreement → no retrain
    for _ in range(250):
        await feedback_store.record_sample(
            features=[0] * 7,
            rule_label="BACKGROUND",
            ml_label="BACKGROUND",
            ml_confidence=0.95,
        )
    out = await auto_pilot.auto_retrain_tick()
    assert out["action"] == "skipped"
    assert out["reason"] == "drift_within_threshold"


@pytest.mark.asyncio
async def test_auto_retrain_triggers_when_drift_below_threshold(
    memory_db,
    tmp_path,
    monkeypatch,
):
    # Seed samples with high disagreement so drift trips. Use synthetic
    # feature distributions so retrain has variety to learn from.
    import random

    rng = random.Random(7)
    for cls in mlc.CLASSES:
        for _ in range(60):
            await feedback_store.record_sample(
                features=mlc._synth_sample(rng, cls),
                rule_label=cls,
                ml_label="BACKGROUND",  # always wrong → low agreement
                ml_confidence=0.6,
            )
    monkeypatch.setattr(mlc, "ARTIFACT_PATH", tmp_path / "live.json")

    # Reset cooldown (module-level state from earlier tests). Set far in the
    # past so the diff exceeds cooldown_seconds even on fresh CI runners.
    auto_pilot._last_retrain_at = -1e10

    out = await auto_pilot.auto_retrain_tick()
    assert out["action"] == "retrained"
    assert out["result"]["status"] in {"swapped", "challenger_registered", "rejected"}


@pytest.mark.asyncio
async def test_auto_promote_skipped_when_no_challenger(memory_db):
    out = await auto_pilot.auto_promote_tick()
    assert out["result"]["promoted"] is False
    assert out["result"]["reason"] == "no_challenger"
