"""B1 — scenario export module + API endpoint."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module
from app.data.db import noaa_snapshots
from app.main import app
from app.outputs.scenario_export import (
    _downsample,
    build_scenario_csv,
    build_scenario_geojson,
    export_scenario,
)


@pytest_asyncio.fixture
async def memory_db_with_snapshots():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
        # Seed 6 snapshots, 5 min apart, kp climbing 3→7 to simulate a storm
        base = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        for i, kp in enumerate([3.0, 4.5, 5.5, 6.5, 7.0, 4.0]):
            await conn.execute(
                insert(noaa_snapshots).values(
                    fetched_at=base + timedelta(minutes=5 * i),
                    fetch_source="live",
                    kp=kp, bz_nt=-5.0 - i, xray_flux=1e-6,
                    proton_flux_10mev=1.0,
                    wind_speed_km_s=420.0 + i * 30,
                    feeds_available='["kp"]', feeds_unavailable="[]",
                    data_age_seconds=0,
                )
            )
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


# ── Module-level ─────────────────────────────────────────────────────────────


def test_downsample_keeps_one_per_step():
    base = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    snaps = [{"fetched_at": base + timedelta(minutes=i)} for i in range(10)]
    out = _downsample(snaps, step_seconds=300)
    # 10 minutes of 1-min data, 5-min step → 3 rows (0, 5, 10... but only 0-9 input)
    assert len(out) == 2  # t=0 and t=5
    out_zero = _downsample(snaps, step_seconds=0)
    assert len(out_zero) == 10


@pytest.mark.asyncio
async def test_export_geojson_shape(memory_db_with_snapshots):
    payload, meta = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="geojson", step_seconds=0, max_snapshots=10,
    )
    assert payload["type"] == "FeatureCollection"
    assert meta["downsampled_count"] == 6
    # 6 snapshots × 324 regions
    assert len(payload["features"]) == 6 * 324
    f = payload["features"][0]
    assert f["geometry"]["type"] == "Polygon"
    assert "time_tag" in f["properties"]
    assert "kp" in f["properties"]
    assert "gps_l1_error_m" in f["properties"]


@pytest.mark.asyncio
async def test_export_geojson_region_filter(memory_db_with_snapshots):
    payload, meta = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="geojson", step_seconds=0, max_snapshots=10,
        region_filter=["R+035-090"],
    )
    assert len(payload["features"]) == 6  # one region × 6 snapshots
    for f in payload["features"]:
        assert f["properties"]["region_id"] == "R+035-090"


@pytest.mark.asyncio
async def test_export_geojson_point_geometry(memory_db_with_snapshots):
    payload, _ = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="geojson", step_seconds=0, geometry="point",
        region_filter=["R+035-090"],
    )
    assert payload["features"][0]["geometry"]["type"] == "Point"


@pytest.mark.asyncio
async def test_export_csv_shape(memory_db_with_snapshots):
    payload, meta = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="csv", step_seconds=0,
        region_filter=["R+035-090", "R+045-090"],
    )
    reader = csv.reader(io.StringIO(payload))
    rows = list(reader)
    # Header + 6 snapshots × 2 regions
    assert rows[0][0] == "time_tag"
    assert len(rows) - 1 == 6 * 2


@pytest.mark.asyncio
async def test_export_storm_drives_increasing_gps_error(memory_db_with_snapshots):
    """Quietest snapshot (kp=3) should give a smaller GPS error than peak (kp=7)."""
    payload, _ = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="geojson", step_seconds=0,
        region_filter=["R+035-090"],
    )
    # First feature is t=0 (kp=3), peak is t=4 (kp=7)
    quiet = payload["features"][0]["properties"]["gps_l1_error_m"]
    storm = payload["features"][4]["properties"]["gps_l1_error_m"]
    assert storm > quiet


@pytest.mark.asyncio
async def test_export_step_seconds_downsamples(memory_db_with_snapshots):
    payload, meta = await export_scenario(
        start=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        fmt="geojson", step_seconds=600, max_snapshots=10,
        region_filter=["R+035-090"],
    )
    # 6 snapshots 5 min apart with step=600 (10 min) → 3 rows
    assert meta["downsampled_count"] == 3
    assert len(payload["features"]) == 3


def test_export_unknown_format_raises():
    with pytest.raises(ValueError):
        # Sync call to test pure-Python path
        from app.outputs.scenario_export import build_scenario_geojson
        # Just verify the high-level entry rejects bad fmt
        import asyncio
        asyncio.run(export_scenario(
            start=datetime(2026, 4, 26, tzinfo=timezone.utc),
            end=datetime(2026, 4, 26, 1, tzinfo=timezone.utc),
            fmt="xml",
        ))


# ── HTTP endpoint ────────────────────────────────────────────────────────────


def test_scenario_export_endpoint_geojson():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "FeatureCollection"
        # Empty time range against the production DB → 0 features
        assert isinstance(body["features"], list)


def test_scenario_export_endpoint_csv_content_type():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "csv",
        })
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert r.text.startswith("time_tag,")


def test_scenario_export_endpoint_bad_dates_400():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "not-a-date",
            "end": "2026-04-26T12:00:00Z",
        })
        assert r.status_code == 400


def test_scenario_export_endpoint_end_before_start_400():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2026-04-26T13:00:00Z",
            "end": "2026-04-26T12:00:00Z",
        })
        assert r.status_code == 400


def test_scenario_export_endpoint_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert "/api/v3/scenarios/export" in schema["paths"]
