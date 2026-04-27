"""Tests for historical storm backfill — real OMNI fetcher + persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module
from app.data import historical_backfill as hb
from app.main import app


@pytest_asyncio.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module.override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all)
    yield engine
    db_module.override_engine(None)
    await engine.dispose()


def _omni(when: datetime, *, kp, bz, wind, proton=None) -> hb.OmniRow:
    return hb.OmniRow(
        when=when,
        kp=kp,
        bz_nt=bz,
        wind_km_s=wind,
        density_cm3=5.0,
        proton_flux_pfu=proton,
    )


# ── OMNI parser ──────────────────────────────────────────────────────────────


def test_parse_omni_csv_drops_only_when_kp_is_fill():
    """Fill Bz/wind/proton are coerced to defaults, not dropped — but a
    missing Kp removes the row entirely (no canonical severity)."""
    csv_text = (
        "2024-05-10T00:30:00.000Z,-3.0,5.4,410,99999.99,27\n"  # all valid
        "2024-05-10T01:30:00.000Z,999.9,5.4,410,99999.99,27\n"  # Bz fill → 0.0
        "2024-05-10T02:30:00.000Z,-2.0,5.4,9999.0,99999.99,27\n"  # V fill → 400
        "2024-05-10T03:30:00.000Z,-2.0,5.4,410,99999.99,99\n"  # Kp fill → DROP
        "2024-05-10T04:30:00.000Z,-2.0,5.4,410,1220.00,87\n"  # real proton
    )
    out = hb._parse_omni_csv(csv_text)
    assert len(out) == 4  # everything kept except Kp-fill row
    by_time = {r.when.isoformat(): r for r in out}
    # Bz fill coerced to 0
    assert by_time["2024-05-10T01:30:00+00:00"].bz_nt == 0.0
    # V fill coerced to baseline 400
    assert by_time["2024-05-10T02:30:00+00:00"].wind_km_s == 400.0
    # Kp 8.7 row carries real proton
    assert by_time["2024-05-10T04:30:00+00:00"].proton_flux_pfu == 1220.0


def test_parse_omni_csv_marks_missing_proton():
    csv_text = "2024-05-10T00:30:00.000Z,-3.0,5.4,410,99999.99,27\n"
    out = hb._parse_omni_csv(csv_text)
    assert out[0].proton_flux_pfu is None  # OMNI fill → None


# ── X-ray flare timeline ─────────────────────────────────────────────────────


def test_xray_at_quiescent_far_from_any_flare():
    profile = hb.STORM_PROFILES["may-2024-g5"]
    # 4 days before the first flare → quiescent
    t = datetime(2024, 5, 4, 0, tzinfo=timezone.utc)
    assert hb._xray_at(t, profile) == hb.XRAY_QUIESCENT_WM2


def test_xray_at_peak_returns_class_value():
    profile = hb.STORM_PROFILES["may-2024-g5"]
    # X8.7 flare at 2024-05-14T16:51 → 8.7e-4 W/m²
    flare = profile.flares[-1]
    assert hb._xray_at(flare.peak_time, profile) == flare.peak_flux_wm2


def test_xray_at_decays_exponentially_after_peak():
    profile = hb.STORM_PROFILES["may-2024-g5"]
    fl = profile.flares[-1]  # X8.7 flare
    t30 = fl.peak_time + timedelta(minutes=30)  # 1× decay constant
    flux30 = hb._xray_at(t30, profile)
    # exp(-1) ≈ 0.368
    assert 0.30 * fl.peak_flux_wm2 < flux30 < 0.40 * fl.peak_flux_wm2


def test_xray_at_picks_nearest_flare_when_overlapping():
    profile = hb.STORM_PROFILES["halloween-2003"]
    # 1 minute after the X28 — should be ~X28 not the older X10
    t = datetime(2003, 11, 4, 19, 54, tzinfo=timezone.utc)
    x = hb._xray_at(t, profile)
    assert x > 1e-3  # X-class
    assert x < 4e-3


def test_flare_class_values_match_noaa_scale():
    fl_x = hb.FlareEvent(datetime.now(timezone.utc), "X", 8.7)
    assert fl_x.peak_flux_wm2 == 8.7e-4
    fl_m = hb.FlareEvent(datetime.now(timezone.utc), "M", 5.0)
    assert fl_m.peak_flux_wm2 == 5e-5
    fl_c = hb.FlareEvent(datetime.now(timezone.utc), "C", 1.0)
    assert fl_c.peak_flux_wm2 == 1e-6


# ── Row construction ─────────────────────────────────────────────────────────


def test_row_from_omni_uses_real_drivers():
    profile = hb.STORM_PROFILES["may-2024-g5"]
    # Time chosen 67 minutes after the documented X5.8 flare peak
    o = _omni(datetime(2024, 5, 11, 2, 30, tzinfo=timezone.utc), kp=9.0, bz=-20.5, wind=738, proton=1220.0)
    row = hb._row_from_omni(o, profile)
    assert row["kp"] == 9.0
    assert row["bz_nt"] == -20.5
    assert row["wind_speed_km_s"] == 738.0
    assert row["proton_flux_10mev"] == 1220.0
    assert row["fetch_source"] == hb.BACKFILL_TAG
    # X-ray is on the decay tail of the X5.8 flare ~67 min earlier — must be
    # well above quiescent and well below the X5.8 peak (5.8e-4).
    assert hb.XRAY_QUIESCENT_WM2 < row["xray_flux"] < 5.8e-4
    # feeds_available now lists the flare-timeline source
    feeds = row["feeds_available"]
    assert "kp_omni" in feeds and "bz_omni" in feeds and "proton_omni" in feeds
    assert "xray_flare_timeline" in feeds


def test_row_from_omni_handles_missing_proton():
    profile = hb.STORM_PROFILES["may-2024-g5"]
    o = _omni(datetime(2024, 5, 10, 0, 30, tzinfo=timezone.utc), kp=2.7, bz=-3.0, wind=410, proton=None)
    row = hb._row_from_omni(o, profile)
    assert row["proton_flux_10mev"] == 0.1  # quiet baseline fallback
    assert "proton_omni" in row["feeds_unavailable"]


# ── End-to-end backfill ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_storm_inserts_real_omni(memory_db):
    fake_data = [
        _omni(datetime(2024, 5, 10, 0, 30, tzinfo=timezone.utc), kp=2.7, bz=-3.0, wind=410),
        _omni(datetime(2024, 5, 10, 20, 30, tzinfo=timezone.utc), kp=7.7, bz=-30.0, wind=700, proton=50.0),
        _omni(datetime(2024, 5, 11, 2, 30, tzinfo=timezone.utc), kp=9.0, bz=-50.0, wind=900, proton=200.0),
    ]

    async def fake_fetch(start, end):
        return fake_data

    result = await hb.backfill_storm(
        "may-2024-g5",
        datetime(2024, 5, 10, tzinfo=timezone.utc),
        datetime(2024, 5, 12, tzinfo=timezone.utc),
        fetch_omni=fake_fetch,
    )
    assert result["inserted"] == 3
    assert result["peak_kp"] == 9.0
    assert result["min_bz_nt"] == -50.0
    assert result["peak_wind_km_s"] == 900
    assert result["peak_proton_pfu"] == 200.0


@pytest.mark.asyncio
async def test_backfill_idempotent(memory_db):
    fake_data = [_omni(datetime(2024, 5, 10, 0, 30, tzinfo=timezone.utc), kp=5.0, bz=-10.0, wind=500)]

    async def fake_fetch(s, e):
        return fake_data

    r1 = await hb.backfill_storm(
        "may-2024-g5",
        datetime(2024, 5, 10, tzinfo=timezone.utc),
        datetime(2024, 5, 11, tzinfo=timezone.utc),
        fetch_omni=fake_fetch,
    )
    r2 = await hb.backfill_storm(
        "may-2024-g5",
        datetime(2024, 5, 10, tzinfo=timezone.utc),
        datetime(2024, 5, 11, tzinfo=timezone.utc),
        fetch_omni=fake_fetch,
    )
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0
    assert r2["reason"] == "already_backfilled"


@pytest.mark.asyncio
async def test_backfill_unknown_profile(memory_db):
    async def empty(s, e):
        return []

    r = await hb.backfill_storm(
        "not-a-profile",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        fetch_omni=empty,
    )
    assert r["inserted"] == 0
    assert "unknown profile" in r["reason"]


@pytest.mark.asyncio
async def test_backfill_no_omni_data(memory_db):
    async def empty(s, e):
        return []

    r = await hb.backfill_storm(
        "may-2024-g5",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        fetch_omni=empty,
    )
    assert r["inserted"] == 0
    assert r["reason"] == "no_omni_data_returned"


# ── HTTP endpoint ────────────────────────────────────────────────────────────


def test_backfill_endpoint_unknown_profile_400():
    with TestClient(app) as client:
        r = client.post("/api/v3/scenarios/backfill", params={"profile_id": "garbage"})
        assert r.status_code == 400


def test_backfill_endpoint_in_openapi():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert "/api/v3/scenarios/backfill" in schema["paths"]
