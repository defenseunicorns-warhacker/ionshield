"""
Tests for the three A6 caveat fixes:
  1. Breaker state survives a process restart (DB-backed)
  2. Fire-and-forget pushes are bounded by per-label semaphore
  3. /metrics endpoint emits Prometheus exposition
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.api.metrics import render as render_metrics
from app.data import breaker_store, db as db_module, instrumentation, registry
from app.data.circuit_breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreaker,
    set_persistor,
)
from app.data.registry import DataSource, register
from app.main import _fire_and_forget, _egress_locks, app


# ── Caveat 1: persistence ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
    yield engine
    db_module.override_engine(None)
    set_persistor(None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_breaker_persists_open_state_across_restart(memory_db):
    set_persistor(breaker_store.persist)
    cfg = BreakerConfig(failure_threshold=2, cooldown_seconds=300, name="src1")

    # Drive breaker OPEN, persist
    cb1 = CircuitBreaker(cfg)
    await cb1.record_failure()
    await cb1.record_failure()
    assert cb1.stats.state == BreakerState.OPEN

    persisted = await breaker_store.hydrate_all()
    assert "src1" in persisted
    assert persisted["src1"]["state"] == "OPEN"

    # Simulate restart: new breaker, hydrate from DB
    cb2 = CircuitBreaker(cfg)
    cb2.hydrate(persisted["src1"])
    assert cb2.stats.state == BreakerState.OPEN
    # Cooldown not elapsed → still doesn't allow
    assert await cb2.allow() is False


@pytest.mark.asyncio
async def test_breaker_hydrate_promotes_open_to_half_open_after_cooldown(memory_db):
    set_persistor(breaker_store.persist)
    cfg = BreakerConfig(failure_threshold=2, cooldown_seconds=0.001, name="src2")

    cb1 = CircuitBreaker(cfg)
    await cb1.record_failure()
    await cb1.record_failure()
    persisted = await breaker_store.hydrate_all()

    # Wait past cooldown
    await asyncio.sleep(0.01)

    cb2 = CircuitBreaker(cfg)
    cb2.hydrate(persisted["src2"])
    # Cooldown already elapsed by wall clock → transitions to HALF_OPEN
    assert cb2.stats.state == BreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_breaker_persistence_survives_success(memory_db):
    set_persistor(breaker_store.persist)
    cb = CircuitBreaker(BreakerConfig(failure_threshold=2, name="src3"))
    await cb.record_failure()
    await cb.record_failure()
    await cb.record_success()  # → CLOSED
    persisted = await breaker_store.hydrate_all()
    assert persisted["src3"]["state"] == "CLOSED"
    assert persisted["src3"]["consecutive_failures"] == 0
    assert persisted["src3"]["total_successes"] == 1


# ── Caveat 2: bounded fire-and-forget ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_egress_locks():
    _egress_locks.clear()
    yield
    _egress_locks.clear()


def test_fire_and_forget_skips_when_previous_in_flight(caplog):
    started = asyncio.Event()
    release = asyncio.Event()
    completed_count = 0

    async def slow_task() -> None:
        nonlocal completed_count
        started.set()
        await release.wait()
        completed_count += 1

    async def go() -> int:
        await _fire_and_forget(slow_task, label="L")
        await started.wait()
        # Second call should be skipped because the first holds the semaphore
        await _fire_and_forget(slow_task, label="L")
        await asyncio.sleep(0.01)
        release.set()
        # Give the first task time to complete
        await asyncio.sleep(0.05)
        return completed_count

    n = asyncio.run(go())
    # Only the first task ran; the second was rejected
    assert n == 1


def test_fire_and_forget_independent_labels_run_concurrently():
    completions: list[str] = []

    async def task_a():
        completions.append("a")

    async def task_b():
        completions.append("b")

    async def go():
        await _fire_and_forget(task_a, label="A")
        await _fire_and_forget(task_b, label="B")
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert set(completions) == {"a", "b"}


# ── Caveat 3: /metrics exposition ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_registry_and_inst():
    registry.reset()
    instrumentation.reset()
    yield
    registry.reset()
    instrumentation.reset()


def test_metrics_renders_empty_with_no_sources():
    out = render_metrics()
    # Prometheus format requires at least the HELP/TYPE descriptors
    assert "ionshield_source_fetch_duration_ms" in out
    assert "ionshield_breaker_state" in out


def test_metrics_includes_source_latency_after_fetch():
    async def fetch_ok(timeout):
        return None

    register(DataSource(name="m_src", cadence_seconds=60, fetch_async=fetch_ok))

    async def go():
        from app.data.registry import run_all

        await run_all()

    asyncio.run(go())
    out = render_metrics()
    assert 'ionshield_source_fetch_duration_ms{source="m_src"' in out
    assert 'ionshield_breaker_state{source="m_src"' in out


def test_metrics_endpoint_returns_plain_text():
    client = TestClient(app)
    r = client.get("/api/v3/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# HELP ionshield_" in r.text
    assert "# TYPE ionshield_" in r.text


def test_metrics_endpoint_excluded_from_openapi():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    assert "/api/v3/metrics" not in schema["paths"]


def test_metrics_breaker_gauge_emits_one_per_state():
    async def fetch_ok(timeout):
        return None

    register(DataSource(name="g_src", cadence_seconds=60, fetch_async=fetch_ok))
    out = render_metrics()
    # All 3 states must appear for each source as gauge labels
    assert 'ionshield_breaker_state{source="g_src",state="CLOSED"} 1' in out
    assert 'ionshield_breaker_state{source="g_src",state="OPEN"} 0' in out
    assert 'ionshield_breaker_state{source="g_src",state="HALF_OPEN"} 0' in out
