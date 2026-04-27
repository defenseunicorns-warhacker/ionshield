"""
Data source registry — single point where the refresh loop discovers what to
fetch each tick.

A `DataSource` declares its name, target cadence, fetch coroutine, and a
status snapshot used by the /health endpoint. The registry composes a
circuit breaker + latency instrumentation around every fetch automatically,
so a new feed plugs in by:

    register(DataSource(
        name="my_feed",
        cadence_seconds=300,
        fetch_async=lambda timeout: my_module.fetch(timeout=timeout),
        status_async=my_module.cache_snapshot,
    ))

Thereafter the refresh loop calls it with breaker / timing / parallelism for
free, and `/api/v3/health` exposes its state.

Design notes:
  - Sources run **concurrently** via asyncio.gather, so a slow feed doesn't
    serialize behind a fast one.
  - Each source has an independent timeout; one timeout doesn't take down
    the rest.
  - Failures don't propagate — they're recorded on the breaker and counted
    in instrumentation, then swallowed. The refresh loop is never fatal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.data.circuit_breaker import BreakerConfig, CircuitBreaker
from app.data.instrumentation import time_source

logger = logging.getLogger(__name__)


FetchAsync = Callable[[float], Awaitable[None]]
StatusAsync = Callable[[], dict[str, Any]]


@dataclass
class DataSource:
    name: str
    cadence_seconds: int
    fetch_async: FetchAsync
    status_async: StatusAsync | None = None
    timeout_seconds: float = 10.0
    breaker_config: BreakerConfig = field(default_factory=BreakerConfig)

    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self) -> None:
        cfg = BreakerConfig(
            failure_threshold=self.breaker_config.failure_threshold,
            cooldown_seconds=self.breaker_config.cooldown_seconds,
            name=self.name,
        )
        self.breaker = CircuitBreaker(cfg)


_sources: dict[str, DataSource] = {}


def register(source: DataSource) -> None:
    """Register a data source. Re-registering the same name overwrites."""
    _sources[source.name] = source
    logger.info("Registered data source: %s (cadence=%ds)", source.name, source.cadence_seconds)


def unregister(name: str) -> None:
    _sources.pop(name, None)


def list_sources() -> list[DataSource]:
    return list(_sources.values())


def get(name: str) -> DataSource | None:
    return _sources.get(name)


def reset() -> None:
    """Test helper — clears the registry."""
    _sources.clear()


# ── Runner ──────────────────────────────────────────────────────────────────


async def _run_one(source: DataSource) -> str:
    """
    Execute a single source fetch with breaker + timing.

    Returns one of: "ok", "skipped", "timeout", "error".
    """
    if not await source.breaker.allow():
        logger.debug("Breaker[%s] OPEN — skipping fetch", source.name)
        return "skipped"
    try:
        async with time_source(source.name):
            await asyncio.wait_for(
                source.fetch_async(source.timeout_seconds),
                timeout=source.timeout_seconds + 1.0,
            )
        await source.breaker.record_success()
        return "ok"
    except asyncio.TimeoutError:
        await source.breaker.record_failure()
        logger.warning("%s fetch timed out", source.name)
        return "timeout"
    except Exception as exc:
        await source.breaker.record_failure()
        logger.warning("%s fetch error: %s", source.name, exc)
        return "error"


async def run_all() -> dict[str, str]:
    """
    Fetch all registered sources concurrently. Returns a dict of
    source.name → result string ("ok" | "skipped" | "timeout" | "error").
    """
    sources = list(_sources.values())
    if not sources:
        return {}
    results = await asyncio.gather(*(_run_one(s) for s in sources))
    return {s.name: r for s, r in zip(sources, results)}


def health_snapshot() -> dict[str, Any]:
    """Per-source breaker + status for /api/v3/health."""
    out: dict[str, Any] = {}
    for s in _sources.values():
        entry: dict[str, Any] = {
            "cadence_seconds": s.cadence_seconds,
            "breaker": s.breaker.snapshot(),
        }
        if s.status_async:
            try:
                entry["status"] = s.status_async()
            except Exception as exc:
                entry["status_error"] = str(exc)
        out[s.name] = entry
    return out
