"""Tests for app.data.event_store — DB persistence + idempotent transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.data import db as db_module
from app.data.event_store import detect_and_persist, list_events
from app.models.events import EventState, MLClassifierStub
from app.models.ontology import EventType, FusedObservation, Region


def _obs(when, *, kp=2.0, xray=1e-7, proton=0.1) -> FusedObservation:
    return FusedObservation(
        region=Region.from_center(0, 0),
        when=when,
        kp_index=kp,
        bz_nt=0.0,
        wind_speed_km_s=400.0,
        xray_flux_wm2=xray,
        proton_flux_10mev_pfu=proton,
        f107_sfu=70.0,
        tec_tecu=15.0,
        tec_anomaly_tecu=0.0,
        hmf2_km=300.0,
        nmf2=1.5e11,
    )


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
async def test_storm_lifecycle_through_db(memory_db):
    base = datetime(2026, 4, 26, tzinfo=timezone.utc)

    # 1. Quiet — no events emitted
    out = await detect_and_persist(_obs(base, kp=3.0))
    assert out["onset"] == [] and out["ended"] == []

    # 2. Kp jumps to 5.5 — onset
    out = await detect_and_persist(_obs(base + timedelta(minutes=5), kp=5.5))
    assert len(out["onset"]) == 1
    assert out["onset"][0].event_type == EventType.GEOMAG_MAIN
    assert out["onset"][0].severity == "G1"

    # 3. Kp climbs to 7.5 — peak update, severity escalates to G3
    out = await detect_and_persist(_obs(base + timedelta(minutes=10), kp=7.5))
    assert out["onset"] == []
    assert len(out["ongoing"]) == 1

    # 4. Kp falls to 3.5 — ended
    out = await detect_and_persist(_obs(base + timedelta(minutes=20), kp=3.5))
    assert len(out["ended"]) == 1

    # Persistence: one row, peaked at 7.5, ended
    rows = await list_events(limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == EventState.ENDED.value
    assert rows[0]["peak_value"] == 7.5
    assert rows[0]["severity"] == "G3"


@pytest.mark.asyncio
async def test_no_duplicate_onset_during_same_event(memory_db):
    base = datetime(2026, 4, 26, tzinfo=timezone.utc)
    await detect_and_persist(_obs(base, kp=5.5))
    await detect_and_persist(_obs(base + timedelta(minutes=5), kp=5.6))
    await detect_and_persist(_obs(base + timedelta(minutes=10), kp=6.0))
    rows = await list_events(limit=10)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_concurrent_independent_events(memory_db):
    base = datetime(2026, 4, 26, tzinfo=timezone.utc)
    # Storm + flare at the same instant should be two distinct events
    out = await detect_and_persist(_obs(base, kp=5.5, xray=2e-4))
    types = {e.event_type for e in out["onset"]}
    assert EventType.GEOMAG_MAIN in types
    assert EventType.FLARE_M in types
    assert EventType.FLARE_X in types


@pytest.mark.asyncio
async def test_ml_stub_records_classifier_name(memory_db):
    base = datetime(2026, 4, 26, tzinfo=timezone.utc)
    clf = MLClassifierStub()
    await detect_and_persist(_obs(base, kp=5.5), classifier=clf)
    rows = await list_events(limit=1)
    assert rows[0]["classifier"] == "ml-stub"
    # Confidence comes from the stub
    assert rows[0]["confidence"] != 1.0
