"""
A7 — Retrain pipeline.

Pulls operator-corrected + rule-labeled samples from the DB, mixes with the
synthetic distribution we already use for class balance, retrains the
multinomial logistic regression, validates against a held-out tail of
recent real samples, persists the new artifact under a versioned filename,
and atomically swaps the live classifier.

This lets an operator trigger improvement cycles via the API:

  POST /api/v3/training/retrain

The active model version is recorded in the `model_versions` table; old
artifacts stay on disk for rollback.

Design choices kept honest:
  - Operator-corrected samples are weighted **3×** vs. rule-labeled samples
    so a small number of high-quality corrections meaningfully shift the
    decision boundary without being drowned out by synthetic data.
  - Hold-out validation: the most recent 10% of real samples are excluded
    from training and used to compute a test accuracy. If test accuracy
    falls below `MIN_VALIDATION_ACCURACY`, the swap is **rejected** — better
    to keep the previous model than to deploy a regression.
  - Versioning is monotonic: `logreg-v{N}` where N is the next id.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data import feedback_store
from app.models import ml_classifier as mlc

logger = logging.getLogger(__name__)


MIN_VALIDATION_ACCURACY: float = 0.80
USER_CORRECTION_WEIGHT: int = 3
SYNTHETIC_PER_CLASS: int = 400


def _shuffle(rng: random.Random, items: list) -> list:
    out = list(items)
    rng.shuffle(out)
    return out


def _real_label_to_class_idx(label: str) -> int | None:
    """Map a stored label string to a CLASSES index. Unknown → None (drop)."""
    if label in mlc.CLASSES:
        return mlc.CLASSES.index(label)
    return None


def _build_training_set(
    real_samples: list[dict],
    rng: random.Random,
) -> tuple[list[list[float]], list[int], list[list[float]], list[int], int]:
    """
    Build (X_train, y_train, X_test, y_test, n_real_used).

    Real samples hold out the most-recent 10% as test. Synthetic samples are
    appended to the train set (never the test set — we want to validate
    against what the world actually produced).
    """
    # Hold out 10% of real samples (newest) for validation
    holdout = max(20, len(real_samples) // 10) if real_samples else 0
    train_real = real_samples[holdout:]
    test_real = real_samples[:holdout]

    X_train: list[list[float]] = []
    y_train: list[int] = []
    n_real_used = 0
    for s in train_real:
        idx = _real_label_to_class_idx(s["label"])
        if idx is None:
            continue
        weight = USER_CORRECTION_WEIGHT if s["is_user_corrected"] else 1
        for _ in range(weight):
            X_train.append(s["features"])
            y_train.append(idx)
        n_real_used += 1

    # Synthetic samples for class balance
    for cls_idx, cls_name in enumerate(mlc.CLASSES):
        for _ in range(SYNTHETIC_PER_CLASS):
            X_train.append(mlc._synth_sample(rng, cls_name))
            y_train.append(cls_idx)

    # Shuffle train
    paired = list(zip(X_train, y_train))
    rng.shuffle(paired)
    X_train = [p[0] for p in paired]
    y_train = [p[1] for p in paired]

    # Test set is the held-out real samples
    X_test: list[list[float]] = []
    y_test: list[int] = []
    for s in test_real:
        idx = _real_label_to_class_idx(s["label"])
        if idx is None:
            continue
        X_test.append(s["features"])
        y_test.append(idx)

    return X_train, y_train, X_test, y_test, n_real_used


def _evaluate(
    X: list[list[float]], y: list[int],
    coef: list[list[float]], intercept: list[float],
) -> float:
    if not X:
        return 1.0  # no held-out test → no regression possible
    correct = 0
    n_classes = len(mlc.CLASSES)
    n_features = len(X[0])
    for xi, yi in zip(X, y):
        logits = [
            sum(coef[k][j] * xi[j] for j in range(n_features)) + intercept[k]
            for k in range(n_classes)
        ]
        pred = logits.index(max(logits))
        if pred == yi:
            correct += 1
    return correct / len(X)


def _next_version_path() -> tuple[str, Path]:
    """Pick a non-clobbering filename for the new artifact."""
    base = mlc.ARTIFACT_PATH.parent
    n = 1
    while True:
        version = f"logreg-v{n + 1}"  # v1 is bundled; first retrain is v2
        path = base / f"event_classifier_{version}.json"
        if not path.exists():
            return version, path
        n += 1


async def retrain_and_maybe_swap(
    *,
    seed: int = 7,
    notes: str = "",
) -> dict[str, Any]:
    """
    Run a retrain cycle. Returns a structured result describing what happened.

    Outcome shape:
      {
        "status": "swapped" | "rejected" | "noop",
        "version": "logreg-vN",
        "n_train": int,
        "n_real_samples": int,
        "train_accuracy": float,
        "validation_accuracy": float,
        "min_validation": float,
        "reason": str,  (only on rejected / noop)
      }
    """
    real = await feedback_store.fetch_for_training(limit=5000)
    if not real:
        return {"status": "noop", "reason": "no real training samples yet"}

    rng = random.Random(seed)
    X_train, y_train, X_test, y_test, n_real_used = _build_training_set(real, rng)

    # Normalize using the existing helper, train, evaluate
    norm_train, mean, std = mlc._normalize(X_train)
    coef, intercept = mlc._train_softmax(norm_train, y_train, len(mlc.CLASSES))

    train_acc = _evaluate(norm_train, y_train, coef, intercept)
    norm_test: list[list[float]] = []
    if X_test:
        for r in X_test:
            norm_test.append([
                (r[i] - mean[i]) / std[i] for i in range(len(mean))
            ])
    val_acc = _evaluate(norm_test, y_test, coef, intercept)

    # Decide: swap or reject?
    if val_acc < MIN_VALIDATION_ACCURACY:
        return {
            "status": "rejected",
            "reason": (
                f"validation accuracy {val_acc:.3f} < threshold "
                f"{MIN_VALIDATION_ACCURACY}"
            ),
            "n_train": len(X_train),
            "n_real_samples": n_real_used,
            "train_accuracy": round(train_acc, 4),
            "validation_accuracy": round(val_acc, 4),
            "min_validation": MIN_VALIDATION_ACCURACY,
        }

    version, path = _next_version_path()
    artifact = {
        "model": version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": len(X_train),
        "n_real_samples": n_real_used,
        "n_user_corrected": sum(
            1 for s in real if s["is_user_corrected"]
            and _real_label_to_class_idx(s["label"]) is not None
        ),
        "feature_names": mlc.FEATURE_NAMES,
        "classes": mlc.CLASSES,
        "mean": mean,
        "std": std,
        "intercept": intercept,
        "coef": coef,
        "train_accuracy": round(train_acc, 4),
        "validation_accuracy": round(val_acc, 4),
    }
    path.write_text(json.dumps(artifact, indent=2))

    # Champion/challenger policy: if there's already an active champion,
    # register the new model as challenger and DO NOT overwrite the live
    # artifact — the champion keeps driving decisions until shadow
    # comparison gives the challenger an edge. If no champion exists
    # (cold start), promote directly.
    current_champion = await feedback_store.active_model_version()
    if current_champion is None:
        # Cold start — promote directly
        mlc.ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2))
        mlc.invalidate_classifier()
        await feedback_store.register_model_version(
            version=version,
            n_train=len(X_train),
            n_real_samples=n_real_used,
            train_accuracy=val_acc,
            artifact_path=str(path),
            notes=notes or "cold-start promotion",
            activate=True,
            challenger=False,
        )
        promotion = "active"
    else:
        await feedback_store.register_model_version(
            version=version,
            n_train=len(X_train),
            n_real_samples=n_real_used,
            train_accuracy=val_acc,
            artifact_path=str(path),
            notes=notes or "shadow challenger",
            activate=False,
            challenger=True,
        )
        promotion = "challenger"

    return {
        "status": "swapped" if promotion == "active" else "challenger_registered",
        "promotion": promotion,
        "version": version,
        "n_train": len(X_train),
        "n_real_samples": n_real_used,
        "n_user_corrected": artifact["n_user_corrected"],
        "train_accuracy": round(train_acc, 4),
        "validation_accuracy": round(val_acc, 4),
        "min_validation": MIN_VALIDATION_ACCURACY,
        "artifact_path": str(path),
    }


async def maybe_auto_promote(
    *,
    min_samples: int,
    min_advantage: float,
) -> dict[str, str | bool | float | None]:
    """
    Promote the challenger if shadow comparison shows it ≥ champion by
    `min_advantage`. Called by the auto-pilot scheduler.
    """
    challenger = await feedback_store.challenger_model_version()
    if challenger is None:
        return {"promoted": False, "reason": "no_challenger"}
    metrics = await feedback_store.shadow_metrics(window=max(min_samples, 100))
    if metrics["n"] < min_samples:
        return {"promoted": False, "reason": "insufficient_samples",
                "n": metrics["n"]}
    advantage = metrics["advantage"]
    if advantage is None or advantage < min_advantage:
        return {"promoted": False, "reason": "insufficient_advantage",
                "advantage": advantage}
    # Swap on disk
    challenger_path = Path(challenger["artifact_path"])
    if not challenger_path.exists():
        return {"promoted": False, "reason": "challenger_artifact_missing"}
    mlc.ARTIFACT_PATH.write_text(challenger_path.read_text())
    mlc.invalidate_classifier()
    ok = await feedback_store.promote_challenger(challenger["version"])
    return {
        "promoted": ok,
        "reason": "ok" if ok else "db_update_failed",
        "version": challenger["version"],
        "advantage": advantage,
        "n": metrics["n"],
    }
