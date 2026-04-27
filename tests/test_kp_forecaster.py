"""Phase 2 — Kp forecaster: featurization, training, prediction, API."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module
from app.main import app
from app.models import kp_forecaster as kpf


# ── Featurization ────────────────────────────────────────────────────────────


def _row(t: datetime, kp: float, bz: float, v: float) -> dict:
    return {"fetched_at": t, "kp": kp, "bz_nt": bz, "wind_speed": v}


def test_featurize_returns_none_for_empty_history():
    assert kpf.featurize_window([]) is None
    assert kpf.featurize_window([_row(datetime.now(timezone.utc), 1, 0, 400)]) is None


def test_featurize_produces_expected_length():
    now = datetime.now(timezone.utc)
    history = [
        _row(now - timedelta(hours=12), 2.0, -1, 380),
        _row(now - timedelta(hours=6), 3.0, -3, 420),
        _row(now - timedelta(hours=3), 4.0, -5, 450),
        _row(now - timedelta(hours=1), 5.0, -8, 500),
        _row(now, 6.0, -10, 520),
    ]
    feats = kpf.featurize_window(history)
    assert feats is not None
    # 3 features per lag offset × 5 offsets + 4 aggregates = 19
    assert len(feats) == 3 * len(kpf.LAG_OFFSETS_H) + 4
    assert len(feats) == len(kpf.FEATURE_NAMES)


def test_featurize_interpolates_at_lag_offsets():
    now = datetime.now(timezone.utc)
    history = [
        _row(now - timedelta(hours=12), 1.0, 0.0, 400.0),
        _row(now, 5.0, -10.0, 600.0),
    ]
    # Need at least 3 samples — add one
    history.insert(1, _row(now - timedelta(hours=6), 3.0, -5.0, 500.0))
    feats = kpf.featurize_window(history)
    assert feats is not None
    # Feature [0] is kp_t+0h — most recent kp = 5.0
    assert feats[0] == pytest.approx(5.0)


# ── Training ─────────────────────────────────────────────────────────────────


def test_train_with_empty_db_falls_back_to_synth():
    artifact = kpf.train([])
    assert artifact["training_source"] == "synthetic"
    assert artifact["n_train_real"] == 0
    assert artifact["n_train_total"] >= 100
    assert len(artifact["weights"]) == len(kpf.FEATURE_NAMES) + 1  # + bias
    assert len(artifact["weights"][0]) == len(kpf.HORIZONS_H)


def test_train_rmse_is_finite_and_bounded():
    artifact = kpf.train([])
    rmse = artifact["metrics"]["rmse_per_horizon"]
    assert all(math.isfinite(r) for r in rmse)
    # On synth data we expect RMSE well under Kp range (0..9)
    assert all(r < 5.0 for r in rmse)


def test_predict_returns_clamped_kp_per_horizon():
    artifact = kpf.train([])
    feats = [0.0] * len(kpf.FEATURE_NAMES)
    preds = kpf.predict(feats, artifact)
    assert set(preds.keys()) == {f"h{h}" for h in kpf.HORIZONS_H}
    for v in preds.values():
        assert 0.0 <= v <= 9.0


def test_severity_buckets_match_noaa_g_scale():
    assert kpf.kp_to_severity(9.5) == "G5"
    assert kpf.kp_to_severity(8.0) == "G4"
    assert kpf.kp_to_severity(7.0) == "G3"
    assert kpf.kp_to_severity(6.0) == "G2"
    assert kpf.kp_to_severity(5.0) == "G1"
    assert kpf.kp_to_severity(4.9) == "G0"
    assert kpf.kp_to_severity(0.0) == "G0"


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kpf, "ARTIFACT_PATH", tmp_path / "kp.json")
    artifact = kpf.train([])
    kpf.save(artifact)
    loaded = kpf.load()
    assert loaded is not None
    assert loaded["horizons_h"] == artifact["horizons_h"]
    assert loaded["weights"] == artifact["weights"]


def test_load_returns_none_when_artifact_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(kpf, "ARTIFACT_PATH", tmp_path / "missing.json")
    assert kpf.load() is None


# ── DB-backed training ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_train_from_db_seeds_with_synth_when_empty(memory_db):
    artifact = await kpf.train_from_db()
    assert artifact["training_source"] == "synthetic"


@pytest.mark.asyncio
async def test_train_from_db_uses_real_rows_when_present(memory_db, tmp_path, monkeypatch):
    monkeypatch.setattr(kpf, "ARTIFACT_PATH", tmp_path / "kp.json")
    # Seed 80 hourly snapshots so sliding windows produce >= 20 samples
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with memory_db.begin() as conn:
        for i in range(80):
            kp = 2.0 + (i % 10) * 0.3
            await conn.execute(
                insert(db_module.noaa_snapshots).values(
                    fetched_at=base + timedelta(hours=i),
                    kp=kp,
                    bz_nt=-3.0 + (i % 7) - 3,
                    wind_speed_km_s=420.0 + (i % 5) * 30,
                    xray_flux=1e-7,
                    proton_flux_10mev=0.5,
                    fetch_source="test",
                    feeds_available="kp,bz,wind",
                    feeds_unavailable="",
                )
            )
    artifact = await kpf.train_from_db()
    assert artifact["training_source"] == "noaa_snapshots"
    assert artifact["n_train_real"] >= 1


# ── HTTP API ─────────────────────────────────────────────────────────────────


def test_forecast_endpoint_returns_5_horizons():
    with TestClient(app) as client:
        r = client.get("/api/v3/forecast/kp")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["horizons_h"] == kpf.HORIZONS_H
        assert len(body["entries"]) == len(kpf.HORIZONS_H)
        for entry in body["entries"]:
            assert 0.0 <= entry["kp_predicted"] <= 9.0
            assert entry["severity"] in {"G0", "G1", "G2", "G3", "G4", "G5"}


def test_retrain_endpoint_admin_guard(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_bearer", "")
    with TestClient(app) as client:
        r = client.post("/api/v3/forecast/kp/retrain")
        assert r.status_code == 403


def test_retrain_endpoint_works_with_admin_bearer(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_bearer", "test-admin")
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/forecast/kp/retrain",
            headers={"Authorization": "Bearer test-admin"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "trained_at" in body
        assert "rmse_per_horizon" in body
