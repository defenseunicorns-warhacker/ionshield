"""Tests for A6 — circuit breaker, instrumentation, source registry."""

from __future__ import annotations

import asyncio

import pytest

from app.data import instrumentation, registry
from app.data.circuit_breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreaker,
)
from app.data.registry import DataSource, register, run_all


@pytest.fixture(autouse=True)
def _reset_registry_and_instrumentation():
    registry.reset()
    instrumentation.reset()
    yield
    registry.reset()
    instrumentation.reset()


# ── Circuit breaker ──────────────────────────────────────────────────────────


def test_breaker_starts_closed():
    cb = CircuitBreaker(BreakerConfig(failure_threshold=3, name="t"))
    assert cb.stats.state == BreakerState.CLOSED
    assert asyncio.run(cb.allow()) is True


def test_breaker_opens_after_failures():
    cb = CircuitBreaker(BreakerConfig(failure_threshold=3, cooldown_seconds=60, name="t"))

    async def go():
        for _ in range(3):
            await cb.record_failure()

    asyncio.run(go())
    assert cb.stats.state == BreakerState.OPEN
    assert asyncio.run(cb.allow()) is False


def test_breaker_resets_failure_count_on_success():
    cb = CircuitBreaker(BreakerConfig(failure_threshold=3, name="t"))

    async def go():
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        return cb.stats.consecutive_failures

    assert asyncio.run(go()) == 0
    assert cb.stats.state == BreakerState.CLOSED


def test_breaker_half_open_after_cooldown():
    cb = CircuitBreaker(BreakerConfig(
        failure_threshold=2, cooldown_seconds=0.01, name="t",
    ))

    async def go():
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.05)
        return await cb.allow()

    allowed = asyncio.run(go())
    assert allowed is True
    assert cb.stats.state == BreakerState.HALF_OPEN


def test_breaker_half_open_success_closes():
    cb = CircuitBreaker(BreakerConfig(
        failure_threshold=2, cooldown_seconds=0.01, name="t",
    ))

    async def go():
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.05)
        await cb.allow()
        await cb.record_success()

    asyncio.run(go())
    assert cb.stats.state == BreakerState.CLOSED


def test_breaker_half_open_failure_reopens():
    cb = CircuitBreaker(BreakerConfig(
        failure_threshold=2, cooldown_seconds=0.01, name="t",
    ))

    async def go():
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.05)
        await cb.allow()           # → HALF_OPEN
        await cb.record_failure()  # probe failed

    asyncio.run(go())
    assert cb.stats.state == BreakerState.OPEN


# ── Instrumentation ─────────────────────────────────────────────────────────


def test_instrumentation_stage_timer_records():
    with instrumentation.time_stage("test_stage"):
        pass
    snap = instrumentation.snapshot()
    assert "test_stage" in snap["stages"]
    assert snap["stages"]["test_stage"]["n"] == 1


def test_instrumentation_async_source_timer_records():
    async def go():
        async with instrumentation.time_source("test_src"):
            await asyncio.sleep(0.001)

    asyncio.run(go())
    snap = instrumentation.snapshot()
    assert "test_src" in snap["sources"]
    assert snap["sources"]["test_src"]["p50_ms"] is not None


def test_instrumentation_loop_tick_tracks_interval():
    instrumentation.begin_loop_tick()
    instrumentation.begin_loop_tick()
    snap = instrumentation.snapshot()
    assert snap["loop_interval"]["n"] >= 1


# ── Registry ────────────────────────────────────────────────────────────────


def test_run_all_executes_registered_sources_concurrently():
    calls: list[str] = []

    async def fetch_a(timeout):
        await asyncio.sleep(0.001)
        calls.append("a")

    async def fetch_b(timeout):
        await asyncio.sleep(0.001)
        calls.append("b")

    register(DataSource(name="A", cadence_seconds=60, fetch_async=fetch_a))
    register(DataSource(name="B", cadence_seconds=60, fetch_async=fetch_b))

    results = asyncio.run(run_all())
    assert results == {"A": "ok", "B": "ok"}
    assert set(calls) == {"a", "b"}


def test_run_all_isolates_failures_per_source():
    async def fetch_ok(timeout):
        return None

    async def fetch_bad(timeout):
        raise RuntimeError("boom")

    register(DataSource(name="ok", cadence_seconds=60, fetch_async=fetch_ok))
    register(DataSource(name="bad", cadence_seconds=60, fetch_async=fetch_bad))

    results = asyncio.run(run_all())
    assert results["ok"] == "ok"
    assert results["bad"] == "error"


def test_run_all_skips_when_breaker_open():
    async def fetch_bad(timeout):
        raise RuntimeError("boom")

    register(DataSource(
        name="bad",
        cadence_seconds=60,
        fetch_async=fetch_bad,
        breaker_config=BreakerConfig(failure_threshold=2, cooldown_seconds=60),
    ))

    asyncio.run(run_all())
    asyncio.run(run_all())  # 2 failures → OPEN
    results = asyncio.run(run_all())
    assert results["bad"] == "skipped"


def test_run_all_respects_per_source_timeout():
    async def fetch_slow(timeout):
        await asyncio.sleep(2.0)

    register(DataSource(
        name="slow", cadence_seconds=60, fetch_async=fetch_slow,
        timeout_seconds=0.05,
    ))
    results = asyncio.run(run_all())
    assert results["slow"] == "timeout"


def test_health_snapshot_shape():
    async def fetch_ok(timeout):
        return None

    register(DataSource(
        name="src",
        cadence_seconds=300,
        fetch_async=fetch_ok,
        status_async=lambda: {"x": 1},
    ))
    snap = registry.health_snapshot()
    assert "src" in snap
    assert snap["src"]["cadence_seconds"] == 300
    assert "breaker" in snap["src"]
    assert snap["src"]["status"] == {"x": 1}
