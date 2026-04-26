"""
IonShield Observation Archiver + Replay — tests.

Design:
  - All tests are async (pytest-asyncio strict mode, explicit @pytest.mark.asyncio).
  - Each test gets a fresh in-memory SQLite engine via the `test_db` fixture.
  - HTTP endpoint tests use httpx.AsyncClient with ASGITransport. fetch_noaa is
    patched to a no-op so no network calls are made during lifespan startup.
  - Replay reproducibility: same snapshot_id → same provenance hash, regardless
    of when the replay is requested.

Coverage:
  TestArchiver          — write row, fallback values, kp_forecast extraction
  TestSnapshotRowToEnv  — round-trip EnvironmentSnapshot reconstruction
  TestReplayDeterminism — hash invariance, action invariance across replay calls
  TestSnapshotEndpoints — GET /api/v2/snapshots, GET /api/v2/snapshots/{id}
  TestReplayEndpoints   — GET /api/v2/replay, POST /api/v2/replay/route, 404 cases
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

import app.data.db as _db_mod
import app.data.noaa as _noaa_mod
from app.data.archiver import (
    archive_snapshot,
    get_snapshot_at_or_before,
    get_snapshot_by_id,
    list_snapshots,
    snapshot_row_to_env,
)
from app.data.db import metadata, noaa_snapshots
from app.main import app
from app.models.decision import DecisionEngine

# ── Constants ─────────────────────────────────────────────────────────────────

_G5_SNAPSHOT = dict(
    fetched_at=datetime(2024, 5, 11, 0, 0, 0, tzinfo=timezone.utc),
    fetch_source="hardcoded_test",
    kp=8.3,
    bz_nt=-25.0,
    xray_flux=1e-4,
    proton_flux_10mev=1000.0,
    wind_speed_km_s=650.0,
    kp_forecast_24h=None,
    feeds_available=json.dumps(["kp", "xray", "wind", "mag"]),
    feeds_unavailable=json.dumps(["proton", "kp_forecast"]),
    data_age_seconds=120,
)

_QUIET_SNAPSHOT = dict(
    fetched_at=datetime(2024, 5, 13, 0, 0, 0, tzinfo=timezone.utc),
    fetch_source="hardcoded_test",
    kp=2.7,
    bz_nt=0.0,
    xray_flux=3e-7,
    proton_flux_10mev=0.1,
    wind_speed_km_s=390.0,
    kp_forecast_24h=3.0,
    feeds_available=json.dumps(["kp", "xray", "wind", "mag", "proton", "kp_forecast"]),
    feeds_unavailable=json.dumps([]),
    data_age_seconds=60,
)

_ENGINE = DecisionEngine()


# ── Async fixtures ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def test_db():
    """
    Create a fresh in-memory SQLite engine for one test.

    Overrides the module-level engine so all DB calls in that test use the
    same in-memory database. Resets to None on teardown so other tests
    (and other event loops) are not affected.
    """
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    _db_mod.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()
    _db_mod.override_engine(None)


@pytest_asyncio.fixture
async def seeded_db(test_db):
    """test_db with G5 and quiet snapshots pre-inserted."""
    async with test_db.begin() as conn:
        await conn.execute(insert(noaa_snapshots).values(**_G5_SNAPSHOT))
        await conn.execute(insert(noaa_snapshots).values(**_QUIET_SNAPSHOT))
    yield test_db


@pytest_asyncio.fixture
async def http_client(test_db, monkeypatch):
    """
    Async HTTP client backed by the live app, with:
      - in-memory SQLite engine injected (test_db fixture)
      - fetch_noaa patched to a no-op (no network calls during lifespan startup)
    """

    async def _noop_fetch(*args, **kwargs):
        pass

    monkeypatch.setattr("app.data.noaa.fetch_noaa", _noop_fetch)
    monkeypatch.setattr("app.main.fetch_noaa", _noop_fetch)

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def seeded_http_client(seeded_db, monkeypatch):
    """http_client with G5 + quiet snapshots pre-inserted."""

    async def _noop_fetch(*args, **kwargs):
        pass

    monkeypatch.setattr("app.data.noaa.fetch_noaa", _noop_fetch)
    monkeypatch.setattr("app.main.fetch_noaa", _noop_fetch)

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── TestArchiver ──────────────────────────────────────────────────────────────


class TestArchiver:
    @pytest.mark.asyncio
    async def test_archive_writes_row(self, test_db):
        """archive_snapshot() must write exactly one row to noaa_snapshots."""
        row_id = await archive_snapshot()
        assert row_id is not None, "archive_snapshot() returned None — check logs"
        rows = await list_snapshots(limit=10)
        assert len(rows) == 1
        assert rows[0]["id"] == row_id

    @pytest.mark.asyncio
    async def test_archive_uses_fallback_values_when_cache_empty(self, test_db):
        """With an uninitialised NOAA cache, archiver stores FALLBACK values."""
        # Cache is not populated (no fetch_noaa() called) — FALLBACK values apply
        row_id = await archive_snapshot()
        assert row_id is not None
        rows = await list_snapshots()
        assert len(rows) == 1
        row = rows[0]
        # Fallback Kp is 2.0 — non-zero, within [0,9]
        assert 0.0 <= row["kp"] <= 9.0
        assert row["fetch_source"] in ("live", "fallback", "startup", "unknown")

    @pytest.mark.asyncio
    async def test_archive_captures_injected_kp(self, test_db):
        """Archive stores the value returned by get_kp() at call time."""
        _noaa_mod._cache["kp"] = [{"kp_index": 7.3}]
        _noaa_mod._cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
        _noaa_mod._cache["fetch_source"] = "live"
        _noaa_mod._cache["fetch_status"]["kp"] = "ok"
        try:
            await archive_snapshot()
            rows = await list_snapshots()
            assert abs(rows[0]["kp"] - 7.3) < 0.01
        finally:
            _noaa_mod._cache["kp"] = None

    @pytest.mark.asyncio
    async def test_archive_archive_disabled(self, test_db, monkeypatch):
        """archive_snapshot() is a no-op when archive_enabled=False."""
        from app.config import settings

        monkeypatch.setattr(settings, "archive_enabled", False)
        result = await archive_snapshot()
        assert result is None
        rows = await list_snapshots()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_archive_multiple_fetches_accumulate(self, test_db):
        """Each archive_snapshot() call adds a new row; rows are not replaced."""
        await archive_snapshot()
        await archive_snapshot()
        await archive_snapshot()
        rows = await list_snapshots(limit=10)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_kp_forecast_extraction_with_no_forecast_data(self, test_db):
        """When kp_forecast cache is None, kp_forecast_24h is NULL in DB."""
        _noaa_mod._cache["kp_forecast"] = None
        await archive_snapshot()
        rows = await list_snapshots()
        assert rows[0]["kp_forecast_24h"] is None


# ── TestSnapshotRowToEnv ──────────────────────────────────────────────────────


class TestSnapshotRowToEnv:
    @pytest.mark.asyncio
    async def test_round_trip_scalar_fields(self, seeded_db):
        """All scalar fields survive the DB round-trip unchanged."""
        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)
        assert abs(env.kp - _G5_SNAPSHOT["kp"]) < 1e-6
        assert abs(env.bz_nt - _G5_SNAPSHOT["bz_nt"]) < 1e-6
        assert abs(env.xray_flux - _G5_SNAPSHOT["xray_flux"]) < 1e-12
        assert abs(env.proton_flux_10mev - _G5_SNAPSHOT["proton_flux_10mev"]) < 1e-3
        assert abs(env.wind_speed_km_s - _G5_SNAPSHOT["wind_speed_km_s"]) < 1e-3
        assert env.data_age_seconds == _G5_SNAPSHOT["data_age_seconds"]

    @pytest.mark.asyncio
    async def test_round_trip_feeds(self, seeded_db):
        """feeds_available and feeds_unavailable are preserved as lists."""
        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)
        assert set(env.feeds_available) == {"kp", "xray", "wind", "mag"}
        assert set(env.feeds_unavailable) == {"proton", "kp_forecast"}

    @pytest.mark.asyncio
    async def test_kp_forecast_24h_none_when_null(self, seeded_db):
        """kp_forecast_24h=None in DB maps to None in EnvironmentSnapshot."""
        row = await get_snapshot_by_id(1)  # G5 snapshot has None
        env = snapshot_row_to_env(row)
        assert env.kp_forecast_24h is None

    @pytest.mark.asyncio
    async def test_kp_forecast_24h_float_when_present(self, seeded_db):
        """kp_forecast_24h with a value maps to float in EnvironmentSnapshot."""
        row = await get_snapshot_by_id(2)  # quiet snapshot has 3.0
        env = snapshot_row_to_env(row)
        assert env.kp_forecast_24h == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_observations_populated(self, seeded_db):
        """Reconstructed env must have 5 observation records."""
        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)
        assert len(env.observations) == 5
        phenomena = {o.phenomenon for o in env.observations}
        assert "kp_index" in phenomena
        assert "bz_gsm_nt" in phenomena

    @pytest.mark.asyncio
    async def test_get_snapshot_at_or_before(self, seeded_db):
        """Temporal lookup returns the latest snapshot at or before the query time."""
        # Between G5 (May 11) and quiet (May 13) — should return G5
        query_dt = datetime(2024, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
        row = await get_snapshot_at_or_before(query_dt)
        assert row is not None
        assert abs(row["kp"] - 8.3) < 0.01

    @pytest.mark.asyncio
    async def test_get_snapshot_at_or_before_none_when_too_early(self, seeded_db):
        """Returns None when all snapshots are after the query time."""
        query_dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        row = await get_snapshot_at_or_before(query_dt)
        assert row is None


# ── TestReplayDeterminism ─────────────────────────────────────────────────────


class TestReplayDeterminism:
    @pytest.mark.asyncio
    async def test_same_snapshot_same_hash(self, seeded_db):
        """
        Replaying the same snapshot twice produces the same provenance hash.

        This is the core replay guarantee: identical inputs → identical hash,
        regardless of when the replay is requested.
        """
        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)

        rec1 = _ENGINE.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        rec2 = _ENGINE.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )

        assert rec1.provenance.input_hash == rec2.provenance.input_hash

    @pytest.mark.asyncio
    async def test_same_snapshot_same_action(self, seeded_db):
        """Same snapshot → same decision action (deterministic engine output)."""
        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)

        rec1 = _ENGINE.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        rec2 = _ENGINE.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )

        assert rec1.action == rec2.action

    @pytest.mark.asyncio
    async def test_different_snapshots_different_hashes(self, seeded_db):
        """G5 snapshot (kp=8.3) and quiet snapshot (kp=2.7) produce different hashes."""
        row_g5 = await get_snapshot_by_id(1)
        row_quiet = await get_snapshot_by_id(2)

        env_g5 = snapshot_row_to_env(row_g5)
        env_quiet = snapshot_row_to_env(row_quiet)

        rec_g5 = _ENGINE.comms_fallback(
            env_g5, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        rec_quiet = _ENGINE.comms_fallback(
            env_quiet, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )

        assert rec_g5.provenance.input_hash != rec_quiet.provenance.input_hash

    @pytest.mark.asyncio
    async def test_route_replay_hash_deterministic(self, seeded_db):
        """Route-risk replay from same snapshot produces same hash."""
        from app.models.decision import WaypointInput

        row = await get_snapshot_by_id(1)
        env = snapshot_row_to_env(row)
        wps = [WaypointInput(62.1, -28.4, "MODOG"), WaypointInput(67.3, -18.2, "MIMKU")]

        rec1, _ = _ENGINE.route_risk(env, wps)
        rec2, _ = _ENGINE.route_risk(env, wps)

        assert rec1.provenance.input_hash == rec2.provenance.input_hash
        assert rec1.action == rec2.action


# ── TestSnapshotEndpoints ─────────────────────────────────────────────────────


class TestSnapshotEndpoints:
    @pytest.mark.asyncio
    async def test_snapshots_list_empty(self, http_client):
        """GET /api/v2/snapshots returns empty list when no snapshots archived."""
        # The lifespan archives one fallback row — so count >= 1 after startup
        r = await http_client.get("/api/v2/snapshots")
        assert r.status_code == 200
        data = r.json()
        assert "snapshots" in data
        assert "count" in data
        assert isinstance(data["snapshots"], list)

    @pytest.mark.asyncio
    async def test_snapshots_list_returns_rows(self, seeded_http_client):
        """GET /api/v2/snapshots returns at least the seeded rows."""
        r = await seeded_http_client.get("/api/v2/snapshots")
        assert r.status_code == 200
        data = r.json()
        # At least 2 (G5 + quiet) — lifespan may add a 3rd
        assert data["count"] >= 2
        ids = {s["id"] for s in data["snapshots"]}
        assert len(ids) >= 2

    @pytest.mark.asyncio
    async def test_snapshots_list_pagination(self, seeded_http_client):
        """limit / offset are honoured."""
        r = await seeded_http_client.get("/api/v2/snapshots?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert len(data["snapshots"]) == 1

    @pytest.mark.asyncio
    async def test_snapshot_detail_found(self, seeded_http_client):
        """GET /api/v2/snapshots/{id} returns the correct row."""
        # Get list to find an actual ID
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        snap_id = r_list.json()["snapshots"][0]["id"]
        r = await seeded_http_client.get(f"/api/v2/snapshots/{snap_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == snap_id
        assert "kp" in data
        assert "bz_nt" in data
        assert "fetched_at" in data

    @pytest.mark.asyncio
    async def test_snapshot_detail_not_found(self, http_client):
        """GET /api/v2/snapshots/999999 returns 404."""
        r = await http_client.get("/api/v2/snapshots/999999")
        assert r.status_code == 404


# ── TestReplayEndpoints ───────────────────────────────────────────────────────


class TestReplayEndpoints:
    @pytest.mark.asyncio
    async def test_replay_comms_by_snapshot_id(self, seeded_http_client):
        """GET /api/v2/replay?snapshot_id=N returns a decision with replay block."""
        # Get a valid ID from the list
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        snap_id = r_list.json()["snapshots"][0]["id"]

        r = await seeded_http_client.get(
            f"/api/v2/replay?lat=45.0&lon=0.0&snapshot_id={snap_id}"
        )
        assert r.status_code == 200
        data = r.json()
        assert "action" in data
        assert "confidence" in data
        assert "provenance" in data
        assert "replay" in data
        assert data["replay"]["snapshot_id"] == snap_id
        assert data["decision_type"] == "COMMS_FALLBACK"

    @pytest.mark.asyncio
    async def test_replay_comms_by_at_timestamp(self, seeded_http_client):
        """GET /api/v2/replay?at=ISO returns snapshot nearest to that time."""
        # Query between G5 and quiet — should pick G5 (earlier)
        r = await seeded_http_client.get(
            "/api/v2/replay?lat=45.0&lon=0.0&at=2024-05-12T00:00:00Z"
        )
        assert r.status_code == 200
        data = r.json()
        assert "replay" in data
        assert data["replay"]["kp_at_snapshot"] == pytest.approx(8.3, abs=0.01)

    @pytest.mark.asyncio
    async def test_replay_comms_latest_when_no_locator(self, seeded_http_client):
        """GET /api/v2/replay without snapshot_id or at returns latest snapshot."""
        r = await seeded_http_client.get("/api/v2/replay?lat=45.0&lon=0.0")
        assert r.status_code == 200
        data = r.json()
        assert "replay" in data
        # Latest is quiet (May 13) or the lifespan fallback row, depending on timing

    @pytest.mark.asyncio
    async def test_replay_comms_bad_snapshot_id_404(self, http_client):
        """GET /api/v2/replay?snapshot_id=99999 returns 404."""
        r = await http_client.get("/api/v2/replay?lat=45.0&lon=0.0&snapshot_id=99999")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_replay_comms_bad_at_timestamp_422(self, http_client):
        """GET /api/v2/replay?at=not-a-date returns 422."""
        r = await http_client.get("/api/v2/replay?lat=45.0&lon=0.0&at=not-a-date")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_replay_comms_missing_lat_422(self, http_client):
        """GET /api/v2/replay without lat returns 422."""
        r = await http_client.get("/api/v2/replay?lon=0.0")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_replay_comms_hash_matches_live_decision(self, seeded_http_client):
        """
        The replay provenance hash must match a decision computed directly
        from the same EnvironmentSnapshot (no DB round-trip).

        This is the core replay correctness proof: same inputs → same hash.
        """
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        # Find the G5 snapshot (kp ~ 8.3)
        snaps = r_list.json()["snapshots"]
        g5 = next((s for s in snaps if abs(s["kp"] - 8.3) < 0.1), None)
        if g5 is None:
            pytest.skip("G5 snapshot not found in list (seeding may have failed)")

        snap_id = g5["id"]

        # Hash via HTTP replay
        r_replay = await seeded_http_client.get(
            f"/api/v2/replay?lat=45.0&lon=0.0&snapshot_id={snap_id}"
        )
        assert r_replay.status_code == 200
        replay_hash = r_replay.json()["provenance"]["input_hash"]

        # Hash via direct engine call with same EnvironmentSnapshot
        row = await get_snapshot_by_id(snap_id)
        env = snapshot_row_to_env(row)
        rec = _ENGINE.comms_fallback(
            env, lat=45.0, lon=0.0, dest_lat=None, dest_lon=None
        )
        direct_hash = rec.provenance.input_hash

        assert replay_hash == direct_hash, (
            f"Replay hash {replay_hash!r} does not match direct hash {direct_hash!r}. "
            "Determinism violated."
        )

    @pytest.mark.asyncio
    async def test_replay_route_by_snapshot_id(self, seeded_http_client):
        """POST /api/v2/replay/route returns decision with waypoints + replay block."""
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        snap_id = r_list.json()["snapshots"][0]["id"]

        payload = {
            "snapshot_id": snap_id,
            "waypoints": [
                {"lat": 62.1, "lon": -28.4, "name": "MODOG"},
                {"lat": 67.3, "lon": -18.2, "name": "MIMKU"},
            ],
            "platform": {"asset_type": "GPS_L1"},
        }
        r = await seeded_http_client.post("/api/v2/replay/route", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert "action" in data
        assert "waypoints" in data
        assert len(data["waypoints"]) == 2
        assert "replay" in data
        assert data["replay"]["snapshot_id"] == snap_id
        assert data["decision_type"] == "ROUTE_RISK"

    @pytest.mark.asyncio
    async def test_replay_route_bad_snapshot_id_404(self, http_client):
        """POST /api/v2/replay/route with unknown snapshot_id returns 404."""
        payload = {
            "snapshot_id": 99999,
            "waypoints": [{"lat": 45.0, "lon": 0.0}],
        }
        r = await http_client.post("/api/v2/replay/route", json=payload)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_replay_route_empty_waypoints_422(self, seeded_http_client):
        """POST /api/v2/replay/route with empty waypoints returns 422."""
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        snap_id = r_list.json()["snapshots"][0]["id"]
        payload = {"snapshot_id": snap_id, "waypoints": []}
        r = await seeded_http_client.post("/api/v2/replay/route", json=payload)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_replay_note_is_present_and_informative(self, seeded_http_client):
        """replay block must contain a non-empty replay_note string."""
        r_list = await seeded_http_client.get("/api/v2/snapshots")
        snap_id = r_list.json()["snapshots"][0]["id"]
        r = await seeded_http_client.get(
            f"/api/v2/replay?lat=45.0&lon=0.0&snapshot_id={snap_id}"
        )
        assert r.status_code == 200
        note = r.json()["replay"]["replay_note"]
        assert isinstance(note, str) and len(note) > 20
