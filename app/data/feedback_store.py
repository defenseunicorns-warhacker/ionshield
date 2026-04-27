"""
A7 — Persistence layer for the AI feedback loop.

Provides three responsibilities:

  1. `record_sample(...)` — write a feature vector + rule label + ML prediction
     after every detection tick. This is the primary training-data feed.

  2. `attach_feedback(sample_id, user_feedback)` — operator correction. Lets a
     human flag a misclassification ("this was actually FLARE_M, not BACKGROUND")
     so the next retrain weights it correctly.

  3. `record_outcome(...)` — observed ground truth from downstream systems
     (real GPS error from receivers, real HF availability). Drives drift
     detection and quantitative model evaluation.

Plus the model-version registry that retraining writes to and the API reads.

All functions are async + transactional. Failures are not fatal — they log
and return None so a DB hiccup never takes the API down.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, insert, select, update

from app.data.db import (
    get_engine,
    model_versions,
    outcomes as outcomes_table,
    training_samples,
)

logger = logging.getLogger(__name__)


# ── Training samples ─────────────────────────────────────────────────────────


async def record_sample(
    *,
    features: list[float],
    rule_label: str,
    ml_label: str | None,
    ml_confidence: float | None,
    region_id: str = "GLOBAL",
    event_id: int | None = None,
    challenger_label: str | None = None,
    challenger_confidence: float | None = None,
) -> int | None:
    """Persist one training sample. Returns the new row id, or None on failure."""
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                insert(training_samples).values(
                    created_at=datetime.now(timezone.utc),
                    region_id=region_id,
                    features_json=json.dumps(features),
                    rule_label=rule_label,
                    ml_label=ml_label,
                    ml_confidence=ml_confidence,
                    event_id=event_id,
                    challenger_label=challenger_label,
                    challenger_confidence=challenger_confidence,
                )
            )
        return result.lastrowid
    except Exception as exc:
        logger.warning("record_sample failed: %s", exc)
        return None


async def attach_feedback(sample_id: int, user_feedback: str) -> bool:
    """Attach an operator label correction. Returns True if a row updated."""
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            r = await conn.execute(
                update(training_samples)
                .where(training_samples.c.id == sample_id)
                .values(
                    user_feedback=user_feedback,
                    user_feedback_at=datetime.now(timezone.utc),
                )
            )
        return (r.rowcount or 0) > 0
    except Exception as exc:
        logger.warning("attach_feedback failed: %s", exc)
        return False


async def list_samples(limit: int = 100, only_with_feedback: bool = False) -> list[dict]:
    engine = get_engine()
    async with engine.begin() as conn:
        stmt = (
            select(training_samples)
            .order_by(desc(training_samples.c.created_at))
            .limit(limit)
        )
        if only_with_feedback:
            stmt = stmt.where(training_samples.c.user_feedback != "")
        rows = (await conn.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]


async def count_samples(only_with_feedback: bool = False) -> int:
    engine = get_engine()
    async with engine.begin() as conn:
        stmt = select(func.count()).select_from(training_samples)
        if only_with_feedback:
            stmt = stmt.where(training_samples.c.user_feedback != "")
        return int((await conn.execute(stmt)).scalar() or 0)


async def fetch_for_training(limit: int = 5000) -> list[dict]:
    """
    Pull recent samples for retraining. Newest first; operator-corrected
    samples are returned with `effective_label = user_feedback`, others fall
    back to the rule label.
    """
    rows = await list_samples(limit=limit, only_with_feedback=False)
    out: list[dict] = []
    for r in rows:
        out.append({
            "features": json.loads(r["features_json"]),
            "label": r["user_feedback"] if r["user_feedback"] else r["rule_label"],
            "is_user_corrected": bool(r["user_feedback"]),
            "ml_label": r["ml_label"],
            "ml_confidence": r["ml_confidence"],
        })
    return out


# ── Drift ────────────────────────────────────────────────────────────────────


async def drift_metrics(window: int = 500) -> dict[str, Any]:
    """
    Compute prediction-vs-rule divergence over the latest `window` samples.

    Two scalar metrics:
      agreement      — fraction of samples where ml_label == rule_label
      mean_confidence — mean ml_confidence (model self-rated)
    Plus a per-class confusion summary so an operator can see *where* drift is.
    """
    rows = await list_samples(limit=window)
    if not rows:
        return {
            "n": 0, "agreement": None, "mean_confidence": None, "by_class": {},
        }
    matches = 0
    confs: list[float] = []
    confusion: dict[str, dict[str, int]] = {}
    for r in rows:
        rule = r["rule_label"]
        ml = r["ml_label"] or "UNKNOWN"
        if r["ml_confidence"] is not None:
            confs.append(float(r["ml_confidence"]))
        if rule == ml:
            matches += 1
        confusion.setdefault(rule, {}).setdefault(ml, 0)
        confusion[rule][ml] += 1
    return {
        "n": len(rows),
        "agreement": matches / len(rows),
        "mean_confidence": sum(confs) / len(confs) if confs else None,
        "by_class": confusion,
    }


# ── Outcomes ─────────────────────────────────────────────────────────────────


async def record_outcome(
    *,
    system: str,
    subsystem: str,
    metric: str,
    observed_value: float,
    observed_at: datetime,
    region_id: str | None = None,
    source: str = "user",
    notes: str = "",
) -> int | None:
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            r = await conn.execute(
                insert(outcomes_table).values(
                    created_at=datetime.now(timezone.utc),
                    region_id=region_id,
                    system=system,
                    subsystem=subsystem,
                    metric=metric,
                    observed_value=observed_value,
                    observed_at=observed_at,
                    source=source,
                    notes=notes,
                )
            )
        return r.lastrowid
    except Exception as exc:
        logger.warning("record_outcome failed: %s", exc)
        return None


async def list_outcomes(limit: int = 100) -> list[dict]:
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(outcomes_table).order_by(desc(outcomes_table.c.observed_at)).limit(limit)
        )).mappings().all()
        return [dict(r) for r in rows]


# ── Model versions ───────────────────────────────────────────────────────────


async def register_model_version(
    *,
    version: str,
    n_train: int,
    n_real_samples: int,
    train_accuracy: float | None,
    artifact_path: str,
    notes: str = "",
    activate: bool = True,
    challenger: bool = False,
) -> int | None:
    """
    Insert a new model_versions row.

    `activate=True` flips it to champion (clears prior active flag in the
    same transaction). `challenger=True` registers it as the shadow
    challenger (clears prior challenger flag). The two are mutually
    exclusive — passing both raises ValueError.
    """
    if activate and challenger:
        raise ValueError("a model can be active OR challenger, not both")
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            if activate:
                await conn.execute(
                    update(model_versions).values(active=0).where(
                        model_versions.c.active == 1
                    )
                )
            if challenger:
                await conn.execute(
                    update(model_versions).values(challenger=0).where(
                        model_versions.c.challenger == 1
                    )
                )
            r = await conn.execute(
                insert(model_versions).values(
                    version=version,
                    trained_at=datetime.now(timezone.utc),
                    n_train=n_train,
                    n_real_samples=n_real_samples,
                    train_accuracy=train_accuracy,
                    artifact_path=artifact_path,
                    notes=notes,
                    active=1 if activate else 0,
                    challenger=1 if challenger else 0,
                )
            )
        return r.lastrowid
    except Exception as exc:
        logger.warning("register_model_version failed: %s", exc)
        return None


async def challenger_model_version() -> dict | None:
    engine = get_engine()
    async with engine.begin() as conn:
        r = (await conn.execute(
            select(model_versions).where(model_versions.c.challenger == 1).limit(1)
        )).mappings().first()
        return dict(r) if r else None


async def promote_challenger(version: str) -> bool:
    """Atomically swap challenger → active. Returns True if a row updated."""
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            # Clear any prior champion + clear challenger flag for promoted row
            await conn.execute(
                update(model_versions).values(active=0).where(
                    model_versions.c.active == 1
                )
            )
            r = await conn.execute(
                update(model_versions)
                .values(active=1, challenger=0)
                .where(model_versions.c.version == version)
            )
        return (r.rowcount or 0) > 0
    except Exception as exc:
        logger.warning("promote_challenger failed: %s", exc)
        return False


async def retire_challenger() -> bool:
    """Drop the current challenger flag. Active model is unchanged."""
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            r = await conn.execute(
                update(model_versions).values(challenger=0).where(
                    model_versions.c.challenger == 1
                )
            )
        return (r.rowcount or 0) > 0
    except Exception as exc:
        logger.warning("retire_challenger failed: %s", exc)
        return False


async def shadow_metrics(window: int = 200) -> dict:
    """
    Compare champion vs challenger on the latest `window` samples that
    carry both predictions. Used to decide whether to auto-promote.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(training_samples)
            .where(training_samples.c.challenger_label.is_not(None))
            .order_by(desc(training_samples.c.created_at))
            .limit(window)
        )).mappings().all()

    n = len(rows)
    if n == 0:
        return {"n": 0, "champion_agreement": None,
                "challenger_agreement": None, "advantage": None}

    champ_match = sum(1 for r in rows if r["ml_label"] == r["rule_label"])
    chal_match = sum(1 for r in rows if r["challenger_label"] == r["rule_label"])
    champ = champ_match / n
    chal = chal_match / n
    return {
        "n": n,
        "champion_agreement": round(champ, 4),
        "challenger_agreement": round(chal, 4),
        "advantage": round(chal - champ, 4),
    }


async def list_model_versions(limit: int = 20) -> list[dict]:
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(model_versions).order_by(desc(model_versions.c.trained_at)).limit(limit)
        )).mappings().all()
        return [dict(r) for r in rows]


async def active_model_version() -> dict | None:
    engine = get_engine()
    async with engine.begin() as conn:
        r = (await conn.execute(
            select(model_versions).where(model_versions.c.active == 1).limit(1)
        )).mappings().first()
        return dict(r) if r else None
