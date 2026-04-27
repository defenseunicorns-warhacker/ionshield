"""
Training-sample archiver.

Caps the local SQLite footprint by uploading aged training samples to the
Foundry training-archive dataset and deleting them from the local DB.

Policy (configurable via app.config.settings):
  sample_archive_max_age_days       — rows older than this are eligible
  sample_archive_batch_size         — uploaded in one Foundry transaction
  sample_archive_interval_seconds   — how often the background loop runs

Failure modes are non-fatal:
  - Foundry sync error → rows remain in DB; retried next interval.
  - DB delete error → rows are not deleted; warning logged.

The archived JSONL is shaped so a Foundry transform can reconstruct
the original row exactly (created_at, region_id, features as JSON-string,
rule_label, ml_label, confidence, user_feedback, outcome_label).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.data.db import get_engine, training_samples
from app.data.foundry_sync import sync_rows

logger = logging.getLogger(__name__)


def _row_to_archive_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "created_at": (
            row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat") else str(row["created_at"])
        ),
        "region_id": row["region_id"],
        "features_json": row["features_json"],
        "rule_label": row["rule_label"],
        "ml_label": row["ml_label"],
        "ml_confidence": row["ml_confidence"],
        "challenger_label": row.get("challenger_label"),
        "challenger_confidence": row.get("challenger_confidence"),
        "user_feedback": row["user_feedback"],
        "user_feedback_at": (
            row["user_feedback_at"].isoformat()
            if row["user_feedback_at"] and hasattr(row["user_feedback_at"], "isoformat")
            else None
        ),
        "outcome_label": row["outcome_label"],
        "event_id": row["event_id"],
    }


async def archive_aged_samples(
    *,
    max_age_days: int | None = None,
    batch_size: int | None = None,
    foundry_dataset_rid: str | None = None,
) -> dict:
    """
    Move rows older than `max_age_days` to Foundry, delete from local DB.

    Returns a result dict suitable for the operator-visible /health view:
      {"archived": int, "deleted": int, "skipped_reason": str|None}
    """
    age = max_age_days or settings.sample_archive_max_age_days
    batch = batch_size or settings.sample_archive_batch_size
    rid = foundry_dataset_rid or settings.foundry_training_archive_rid
    cutoff = datetime.now(timezone.utc) - timedelta(days=age)

    # Archive only needs stack URL + token + the archive RID — it does NOT
    # depend on the primary `foundry_space_weather_raw_rid` setting.
    if not (
        settings.foundry_sync_enabled
        and settings.foundry_stack_url
        and settings.foundry_token.get_secret_value()
        and rid
    ):
        return {"archived": 0, "deleted": 0,
                "skipped_reason": "foundry_archive_not_configured"}

    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(training_samples)
            .where(training_samples.c.created_at < cutoff)
            .limit(batch)
        )).mappings().all()

    if not rows:
        return {"archived": 0, "deleted": 0, "skipped_reason": "no_aged_rows"}

    payload = [_row_to_archive_dict(dict(r)) for r in rows]
    ok = await sync_rows(
        payload,
        stack_url=settings.foundry_stack_url,
        dataset_rid=rid,
        token=settings.foundry_token.get_secret_value(),
    )
    if not ok:
        return {"archived": 0, "deleted": 0,
                "skipped_reason": "foundry_sync_failed"}

    ids = [r["id"] for r in rows]
    try:
        async with engine.begin() as conn:
            await conn.execute(
                delete(training_samples)
                .where(training_samples.c.id.in_(ids))
            )
    except Exception as exc:
        logger.warning("Sample-archive delete failed: %s", exc)
        return {"archived": len(rows), "deleted": 0,
                "skipped_reason": f"db_delete_error:{exc}"}

    logger.info("Archived %d training samples to Foundry; deleted from DB",
                len(rows))
    return {"archived": len(rows), "deleted": len(rows), "skipped_reason": None}
