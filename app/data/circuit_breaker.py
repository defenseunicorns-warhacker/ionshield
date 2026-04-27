"""
Per-source circuit breaker.

Wraps an async fetch with three states:

  CLOSED   — normal operation; failures increment a counter.
  OPEN     — too many consecutive failures; calls short-circuit immediately
             without attempting the remote until the cooldown expires.
  HALF_OPEN — single probe call after cooldown; success → CLOSED, failure → OPEN
              again with reset cooldown.

Why this matters at A6: when NOAA SWPC has a multi-hour outage of one product
(e.g. GloTEC listing), the refresh loop should not waste 10s/tick waiting on
its timeout — it should skip that feed entirely until probing succeeds, while
all other feeds continue at full speed.

Pure stdlib, in-process state. For multi-replica deployments swap _state for
Redis-backed shared state — the public API doesn't change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# Optional persistence hook — main.py wires this to the DB on startup.
# Signature: persistor(name, snapshot_dict) -> awaitable. Failures are swallowed.
PersistFn = Callable[[str, dict], Awaitable[None]]
_persistor: PersistFn | None = None


def set_persistor(fn: PersistFn | None) -> None:
    """Install (or clear) the persistence hook used by all breakers."""
    global _persistor
    _persistor = fn


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class BreakerConfig:
    failure_threshold: int = 4         # consecutive failures to open
    cooldown_seconds: float = 300.0    # wait before half-open probe
    name: str = ""


@dataclass
class BreakerStats:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    last_change_at: float = field(default_factory=time.monotonic)
    # Wall-clock epoch seconds for cross-process / restart-survivable persistence.
    # Distinct from last_change_at (monotonic) which only makes sense in-process.
    last_change_epoch: float | None = None
    last_failure_epoch: float | None = None
    last_success_epoch: float | None = None


class CircuitBreaker:
    """One breaker per data source. Thread-safe via a single asyncio lock."""

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self.config = config or BreakerConfig()
        self.stats = BreakerStats()
        self._lock = asyncio.Lock()

    # ── Restart-survivable persistence ──────────────────────────────────────

    def hydrate(self, persisted: dict | None) -> None:
        """
        Rehydrate breaker stats from a persisted snapshot.

        Used at lifespan start so an OPEN breaker recorded before a restart
        stays OPEN until its cooldown elapses. If `persisted["state"]` is OPEN
        and the cooldown has already passed, the breaker is restored as
        HALF_OPEN so the next call attempts a single probe.
        """
        if not persisted:
            return
        self.stats.state = BreakerState(persisted.get("state", "CLOSED"))
        self.stats.consecutive_failures = int(persisted.get("consecutive_failures", 0))
        self.stats.total_failures = int(persisted.get("total_failures", 0))
        self.stats.total_successes = int(persisted.get("total_successes", 0))
        self.stats.last_change_epoch = persisted.get("last_change_epoch")
        self.stats.last_failure_epoch = persisted.get("last_failure_epoch")
        self.stats.last_success_epoch = persisted.get("last_success_epoch")

        # Translate persisted wall-clock change time into an in-process
        # monotonic anchor so the cooldown logic (which uses monotonic) can
        # decide whether enough time has passed.
        if self.stats.last_change_epoch is not None:
            now_wall = datetime.now(timezone.utc).timestamp()
            elapsed = max(0.0, now_wall - self.stats.last_change_epoch)
            self.stats.last_change_at = time.monotonic() - elapsed

            if (
                self.stats.state == BreakerState.OPEN
                and elapsed >= self.config.cooldown_seconds
            ):
                self.stats.state = BreakerState.HALF_OPEN

    def _to_persistable(self) -> dict:
        return {
            "state": self.stats.state.value,
            "consecutive_failures": self.stats.consecutive_failures,
            "total_failures": self.stats.total_failures,
            "total_successes": self.stats.total_successes,
            "last_change_epoch": self.stats.last_change_epoch,
            "last_failure_epoch": self.stats.last_failure_epoch,
            "last_success_epoch": self.stats.last_success_epoch,
        }

    async def _persist(self) -> None:
        if _persistor is None or not self.config.name:
            return
        try:
            await _persistor(self.config.name, self._to_persistable())
        except Exception as exc:
            logger.debug("Breaker[%s] persistence failed: %s",
                         self.config.name, exc)

    async def allow(self) -> bool:
        """Decide whether a call should be made now."""
        transitioned = False
        async with self._lock:
            now = time.monotonic()
            if self.stats.state == BreakerState.CLOSED:
                return True
            if self.stats.state == BreakerState.OPEN:
                if now - self.stats.last_change_at >= self.config.cooldown_seconds:
                    self.stats.state = BreakerState.HALF_OPEN
                    self.stats.last_change_at = now
                    self.stats.last_change_epoch = (
                        datetime.now(timezone.utc).timestamp()
                    )
                    transitioned = True
                    logger.info(
                        "Breaker[%s] OPEN→HALF_OPEN after %.0fs cooldown",
                        self.config.name, self.config.cooldown_seconds,
                    )
                else:
                    return False
            # HALF_OPEN: only allow one probe at a time
        if transitioned:
            await self._persist()
        return True

    async def record_success(self) -> None:
        async with self._lock:
            now = time.monotonic()
            now_wall = datetime.now(timezone.utc).timestamp()
            self.stats.consecutive_failures = 0
            self.stats.consecutive_successes += 1
            self.stats.total_successes += 1
            self.stats.last_success_at = now
            self.stats.last_success_epoch = now_wall
            if self.stats.state in (BreakerState.HALF_OPEN, BreakerState.OPEN):
                logger.info(
                    "Breaker[%s] %s→CLOSED after success",
                    self.config.name, self.stats.state.value,
                )
                self.stats.state = BreakerState.CLOSED
                self.stats.last_change_at = now
                self.stats.last_change_epoch = now_wall
        await self._persist()

    async def record_failure(self) -> None:
        async with self._lock:
            now = time.monotonic()
            now_wall = datetime.now(timezone.utc).timestamp()
            self.stats.consecutive_successes = 0
            self.stats.consecutive_failures += 1
            self.stats.total_failures += 1
            self.stats.last_failure_at = now
            self.stats.last_failure_epoch = now_wall
            if (
                self.stats.state == BreakerState.CLOSED
                and self.stats.consecutive_failures >= self.config.failure_threshold
            ):
                logger.warning(
                    "Breaker[%s] CLOSED→OPEN after %d consecutive failures",
                    self.config.name, self.stats.consecutive_failures,
                )
                self.stats.state = BreakerState.OPEN
                self.stats.last_change_at = now
                self.stats.last_change_epoch = now_wall
            elif self.stats.state == BreakerState.HALF_OPEN:
                logger.warning(
                    "Breaker[%s] HALF_OPEN→OPEN after probe failure",
                    self.config.name,
                )
                self.stats.state = BreakerState.OPEN
                self.stats.last_change_at = now
                self.stats.last_change_epoch = now_wall
        await self._persist()

    def snapshot(self) -> dict:
        s = self.stats
        return {
            "state": s.state.value,
            "consecutive_failures": s.consecutive_failures,
            "total_failures": s.total_failures,
            "total_successes": s.total_successes,
            "last_failure_at": s.last_failure_at,
            "last_success_at": s.last_success_at,
        }
