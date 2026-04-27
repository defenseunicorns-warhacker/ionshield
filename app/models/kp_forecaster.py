"""
Phase 2 — 24-hour Kp forecaster.

Predicts Kp(t+h) for h in {1, 3, 6, 12, 24} hours from a short lookback
window of solar wind drivers + recent Kp. This is the headline ML upgrade
that turns IonShield from a "what's happening now" monitor into a "what's
coming" forecaster — the feature defense and aerospace customers pay
materially more for.

Why this design:
  - Multi-output ridge regression, one weight vector per horizon.
  - Closed-form solver  W = (XᵀX + λI)⁻¹ Xᵀy  → no iterative training,
    deterministic, ~milliseconds for our data size.
  - Features chosen for physical interpretability: solar wind speed and
    Bz drive Kp; we don't need a deep net to capture that — just lagged
    versions of the drivers.
  - Trained on the noaa_snapshots table populated from NASA OMNI
    historical backfill (real Bz, wind, Kp through the space age) plus
    the live 5-min cadence loop. Synthetic seed data is used only when
    the table is empty so the API is never broken.

Stored artifact: app/models/kp_forecaster.json — human-readable JSON with
weights per horizon. Re-train via `POST /api/v3/forecast/kp/retrain`
(admin) or by calling `train_from_db()` at startup if the artifact is
missing.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select

logger = logging.getLogger(__name__)

ARTIFACT_PATH = Path(__file__).parent / "kp_forecaster.json"

# Forecast horizons (hours ahead) — covers near-term ops + 24h planning.
HORIZONS_H = [1, 3, 6, 12, 24]

# Lookback offsets (hours into past) used to build feature lags.
# 0 = current, -1 = 1h ago, etc. Spaced log-ish so the window covers ~24h
# without exploding feature count.
LAG_OFFSETS_H = [0, -1, -3, -6, -12]

FEATURE_NAMES: list[str] = []
for off in LAG_OFFSETS_H:
    FEATURE_NAMES += [f"kp_t{off:+d}h", f"bz_t{off:+d}h", f"v_t{off:+d}h"]
FEATURE_NAMES += ["kp_max_recent", "kp_min_recent", "v_mean_recent", "bz_min_recent"]


# ── Featurization ────────────────────────────────────────────────────────────


def featurize_window(history: list[dict]) -> list[float] | None:
    """
    Build a feature vector from a chronologically-ordered list of recent
    snapshots. `history[-1]` is the most recent observation.

    Each snapshot dict needs `kp` (float), `bz_nt` (float), `wind_speed` (float),
    `fetched_at` (datetime). Missing observations at the requested lag offsets
    are filled by linear interpolation across present samples; if there are
    fewer than 3 samples total we return None and the caller falls back to a
    persistence-style baseline.
    """
    if len(history) < 3:
        return None

    # Sort just in case
    history = sorted(history, key=lambda s: s["fetched_at"])
    now = history[-1]["fetched_at"]

    # Pull series of (delta_hours_from_now, kp, bz, v)
    series: list[tuple[float, float, float, float]] = []
    for s in history:
        dt_h = (s["fetched_at"] - now).total_seconds() / 3600.0
        kp = float(s.get("kp", 0.0) or 0.0)
        bz = float(s.get("bz_nt", 0.0) or 0.0)
        v = float(s.get("wind_speed", 400.0) or 400.0)
        series.append((dt_h, kp, bz, v))

    def _at(target_h: float, idx: int) -> float:
        # Linear interpolation across the series at target_h hours from now.
        # idx: 1=kp, 2=bz, 3=v.
        if not series:
            return 0.0
        # Clamp to series bounds (no extrapolation beyond observed range).
        if target_h <= series[0][0]:
            return series[0][idx]
        if target_h >= series[-1][0]:
            return series[-1][idx]
        for i in range(len(series) - 1):
            t0, t1 = series[i][0], series[i + 1][0]
            if t0 <= target_h <= t1:
                if t1 == t0:
                    return series[i][idx]
                a = (target_h - t0) / (t1 - t0)
                return series[i][idx] * (1 - a) + series[i + 1][idx] * a
        return series[-1][idx]

    feats: list[float] = []
    for off in LAG_OFFSETS_H:
        feats.append(_at(off, 1))  # kp
        feats.append(_at(off, 2))  # bz
        feats.append(_at(off, 3))  # v
    # Recent aggregates (last 6h)
    recent_kp = [s[1] for s in series if s[0] >= -6]
    recent_v = [s[3] for s in series if s[0] >= -6]
    recent_bz = [s[2] for s in series if s[0] >= -6]
    feats.append(max(recent_kp) if recent_kp else 0.0)
    feats.append(min(recent_kp) if recent_kp else 0.0)
    feats.append(sum(recent_v) / len(recent_v) if recent_v else 400.0)
    feats.append(min(recent_bz) if recent_bz else 0.0)
    return feats


# ── Model ────────────────────────────────────────────────────────────────────


def _ridge_solve(X: np.ndarray, Y: np.ndarray, lam: float) -> np.ndarray:
    """Closed-form ridge: W = (XᵀX + λI)⁻¹ Xᵀy. Returns (n_features, n_horizons)."""
    n_features = X.shape[1]
    A = X.T @ X + lam * np.eye(n_features)
    return np.linalg.solve(A, X.T @ Y)


def _add_bias(X: np.ndarray) -> np.ndarray:
    return np.hstack([X, np.ones((X.shape[0], 1))])


def predict(features: list[float], artifact: dict[str, Any]) -> dict[str, float]:
    """Return {horizon_h: kp_predicted} given a feature vector + trained artifact."""
    x = np.asarray(features + [1.0], dtype=float)
    W = np.asarray(artifact["weights"], dtype=float)  # shape (n_features+1, n_horizons)
    y = x @ W
    y = np.clip(y, 0.0, 9.0)
    return {f"h{h}": float(round(v, 2)) for h, v in zip(artifact["horizons_h"], y)}


def kp_to_severity(kp: float) -> str:
    if kp >= 9:
        return "G5"
    if kp >= 8:
        return "G4"
    if kp >= 7:
        return "G3"
    if kp >= 6:
        return "G2"
    if kp >= 5:
        return "G1"
    return "G0"


# ── Training ─────────────────────────────────────────────────────────────────


def _build_dataset(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """
    Build (X, Y) from a chronologically-ordered list of snapshot rows.
    Each row: {fetched_at, kp, bz_nt, wind_speed}.
    Returns features (n, F) and targets (n, len(HORIZONS)) for sliding windows
    where every horizon has a real future observation.
    """
    rows = sorted(rows, key=lambda r: r["fetched_at"])
    if len(rows) < 30:
        return np.zeros((0, len(FEATURE_NAMES))), np.zeros((0, len(HORIZONS_H)))

    # Index by hour-rounded timestamp for O(1) future lookup.
    by_hour: dict[datetime, dict] = {}
    for r in rows:
        h = r["fetched_at"].replace(minute=0, second=0, microsecond=0)
        by_hour[h] = r

    X_list, Y_list = [], []
    LOOKBACK_H = -min(LAG_OFFSETS_H)  # 12

    for i, anchor in enumerate(rows):
        anchor_h = anchor["fetched_at"].replace(minute=0, second=0, microsecond=0)
        # Need enough history before the anchor
        history = [
            r
            for r in rows[max(0, i - 60) : i + 1]
            if (anchor["fetched_at"] - r["fetched_at"]).total_seconds() / 3600.0 <= LOOKBACK_H + 1
        ]
        if len(history) < 3:
            continue
        feats = featurize_window(history)
        if feats is None:
            continue
        # Need real future observations at every horizon
        targets: list[float] = []
        ok = True
        for h in HORIZONS_H:
            future_h = anchor_h + timedelta(hours=h)
            future_row = by_hour.get(future_h)
            if future_row is None:
                ok = False
                break
            targets.append(float(future_row["kp"]))
        if not ok:
            continue
        X_list.append(feats)
        Y_list.append(targets)

    return np.asarray(X_list, dtype=float), np.asarray(Y_list, dtype=float)


def train(rows: list[dict], lam: float = 1.0) -> dict[str, Any]:
    """Train the multi-horizon ridge model. Returns the artifact dict."""
    X, Y = _build_dataset(rows)
    if X.shape[0] < 20:
        # Not enough real data — synthesise so the API is never broken.
        X, Y = _synth_training_data(n=600)
        source = "synthetic"
        n_real = 0
    else:
        source = "noaa_snapshots"
        n_real = X.shape[0]

    Xb = _add_bias(X)
    W = _ridge_solve(Xb, Y, lam=lam)

    pred = Xb @ W
    rmse = [float(np.sqrt(np.mean((pred[:, j] - Y[:, j]) ** 2))) for j in range(Y.shape[1])]
    mae = [float(np.mean(np.abs(pred[:, j] - Y[:, j]))) for j in range(Y.shape[1])]

    return {
        "version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "horizons_h": HORIZONS_H,
        "feature_names": FEATURE_NAMES,
        "lambda": lam,
        "n_train_real": n_real,
        "n_train_total": X.shape[0],
        "training_source": source,
        "weights": W.tolist(),
        "metrics": {
            "rmse_per_horizon": rmse,
            "mae_per_horizon": mae,
        },
    }


def _synth_training_data(n: int = 600, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """
    Physics-aware synthetic data: Kp(t+h) is correlated with Bz_min over the
    past 6h (more negative Bz → higher Kp lagged) and elevated wind speed.
    Used only when the live DB is empty (cold-start).
    """
    rng = random.Random(seed)
    X_list, Y_list = [], []
    for _ in range(n):
        # Underlying state — a draw of solar wind + Kp baseline
        bz_baseline = rng.gauss(-1.0, 4.0)  # negative skew toward southward
        v_baseline = rng.gauss(450, 80)
        kp_history = [max(0.0, min(9.0, rng.gauss(2.5, 1.2))) for _ in range(6)]
        # Forward integrate: Kp at t+h is driven by recent Bz minimum + wind
        bz_min = bz_baseline + rng.gauss(0, 1)
        future_kp_floor = max(0.0, 1.5 - 0.4 * bz_min + 0.004 * (v_baseline - 400.0))
        feats: list[float] = []
        for off in LAG_OFFSETS_H:
            kp = max(0.0, min(9.0, kp_history[-1] + 0.05 * off + rng.gauss(0, 0.4)))
            bz = bz_baseline + rng.gauss(0, 1.5)
            v = v_baseline + rng.gauss(0, 30)
            feats.extend([kp, bz, v])
        feats.extend(
            [
                max(kp_history),
                min(kp_history),
                v_baseline + rng.gauss(0, 20),
                bz_min,
            ]
        )
        targets = []
        for h in HORIZONS_H:
            decay = math.exp(-h / 18.0)  # storm response decays over ~18h
            kp_pred = future_kp_floor * decay + rng.gauss(0, 0.4) + kp_history[-1] * (1 - decay) * 0.3
            targets.append(max(0.0, min(9.0, kp_pred)))
        X_list.append(feats)
        Y_list.append(targets)
    return np.asarray(X_list, dtype=float), np.asarray(Y_list, dtype=float)


async def train_from_db() -> dict[str, Any]:
    """Pull recent snapshots from noaa_snapshots and train. Persists artifact."""
    from app.data.db import get_engine, noaa_snapshots

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(
                noaa_snapshots.c.fetched_at,
                noaa_snapshots.c.kp,
                noaa_snapshots.c.bz_nt,
                noaa_snapshots.c.wind_speed_km_s.label("wind_speed"),
            ).order_by(noaa_snapshots.c.fetched_at)
        )
        rows = [dict(r._mapping) for r in result.all()]

    artifact = train(rows)
    save(artifact)
    return artifact


def save(artifact: dict[str, Any]) -> None:
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2))


def load() -> dict[str, Any] | None:
    if not ARTIFACT_PATH.exists():
        return None
    try:
        return json.loads(ARTIFACT_PATH.read_text())
    except Exception as exc:
        logger.warning("kp_forecaster artifact unreadable: %s", exc)
        return None


# ── Live-window feature builder ──────────────────────────────────────────────


async def build_live_features() -> list[float] | None:
    """Pull the last few hours of snapshots and build a feature vector."""
    from app.data.db import get_engine, noaa_snapshots

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(
                noaa_snapshots.c.fetched_at,
                noaa_snapshots.c.kp,
                noaa_snapshots.c.bz_nt,
                noaa_snapshots.c.wind_speed_km_s.label("wind_speed"),
            )
            .order_by(noaa_snapshots.c.fetched_at.desc())
            .limit(48)
        )
        rows = [dict(r._mapping) for r in result.all()]
    if not rows:
        return None
    rows.reverse()  # chronological
    return featurize_window(rows)


# ── CLI entrypoint ───────────────────────────────────────────────────────────


if __name__ == "__main__":
    import asyncio

    artifact = asyncio.run(train_from_db())
    print(f"Trained on {artifact['n_train_real']} real samples ({artifact['training_source']}).")
    print(f"RMSE per horizon: {artifact['metrics']['rmse_per_horizon']}")
    print(f"Saved to {ARTIFACT_PATH}")
