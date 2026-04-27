"""
Auto-pilot — periodic background tasks that close the AI feedback loop:

  • drift-driven retrain: when champion-vs-rule agreement drops below a
    configured threshold, kick off `retrain_and_maybe_swap`.
  • shadow-mode auto-promote: when the registered challenger has accumulated
    enough comparison samples and shows a non-negative advantage on real
    data, promote it to active.
  • sample archive: ship aged training rows to the Foundry archive dataset.

All loops are independent and never fatal — exceptions are logged and the
loop keeps ticking. They share a single cooldown clock so a recent retrain
prevents another for `auto_retrain_cooldown_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import settings
from app.data import feedback_store, sample_archive
from app.models import retrain as retrain_module

logger = logging.getLogger(__name__)


# Process-local cooldown; for multi-replica deployments this would move to
# a shared lock store.
_last_retrain_at: float = 0.0


async def _drift_below_threshold() -> bool:
    metrics = await feedback_store.drift_metrics(
        window=settings.auto_retrain_min_samples,
    )
    if metrics["n"] < settings.auto_retrain_min_samples:
        return False
    if metrics["agreement"] is None:
        return False
    return metrics["agreement"] < settings.auto_retrain_drift_threshold


async def auto_retrain_tick() -> dict[str, Any]:
    """
    One pass of the drift-retrain check. Returns a structured outcome
    suitable for logging or `/api/v3/training/auto-pilot`.
    """
    global _last_retrain_at
    if not settings.auto_retrain_enabled:
        return {"action": "skipped", "reason": "disabled"}

    now = time.monotonic()
    if now - _last_retrain_at < settings.auto_retrain_cooldown_seconds:
        return {"action": "skipped", "reason": "cooldown_active",
                "seconds_until_eligible": round(
                    settings.auto_retrain_cooldown_seconds - (now - _last_retrain_at),
                )}

    if not await _drift_below_threshold():
        return {"action": "skipped", "reason": "drift_within_threshold"}

    logger.info("Auto-retrain triggered by drift below %.2f",
                settings.auto_retrain_drift_threshold)
    result = await retrain_module.retrain_and_maybe_swap(
        notes=f"auto-retrain: drift below {settings.auto_retrain_drift_threshold}",
    )
    _last_retrain_at = time.monotonic()
    return {"action": "retrained", "result": result}


async def auto_promote_tick() -> dict[str, Any]:
    """One pass of the challenger-promotion check."""
    return {"action": "checked", "result": await retrain_module.maybe_auto_promote(
        min_samples=settings.shadow_window_min_samples,
        min_advantage=settings.shadow_promotion_min_advantage,
    )}


async def archive_tick() -> dict[str, Any]:
    """One pass of the sample-archive job."""
    return {"action": "archived",
            "result": await sample_archive.archive_aged_samples()}


async def run_loop() -> None:
    """
    Single background loop running every `auto_retrain_check_interval_seconds`.

    Each tick:
      1. auto-retrain if drift sustained below threshold
      2. auto-promote a challenger if shadow comparison favors it
      3. archive aged samples (less frequently — once per N intervals)

    Logged result of each pass is visible via /api/v3/training/auto-pilot.
    """
    archive_every = max(
        1,
        int(settings.sample_archive_interval_seconds /
            max(1, settings.auto_retrain_check_interval_seconds)),
    )
    counter = 0
    while True:
        try:
            await asyncio.sleep(settings.auto_retrain_check_interval_seconds)
            await auto_retrain_tick()
            await auto_promote_tick()
            counter += 1
            if counter % archive_every == 0:
                await archive_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("auto_pilot tick error: %s", exc, exc_info=False)
