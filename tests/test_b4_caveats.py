"""B4 caveat fixes — recipe lint, camera CSV, DB-backed video, URL validation."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import db as db_module
from app.main import app
from app.outputs.earth_studio_recipe import (
    CAMERA_CSV_COLUMNS,
    _interp_lon,
    lint_catalog,
    recipe_to_camera_csv,
    validate_recipe,
)


# ── Caveat 1: recipe validation ─────────────────────────────────────────────


def _good_recipe() -> dict:
    return {
        "duration_seconds": 30,
        "frame_rate": 30,
        "camera": [
            {"t": 0, "lat": 0, "lon": 0, "altitude_m": 25_000_000, "heading": 0, "tilt": 0, "label": "wide"},
            {"t": 15, "lat": 60, "lon": -90, "altitude_m": 8_000_000, "heading": 30, "tilt": 25, "label": "approach"},
            {"t": 28, "lat": 50, "lon": -30, "altitude_m": 18_000_000, "heading": 0, "tilt": 10, "label": "pull back"},
        ],
        "render": {"format": "mp4", "width": 1920, "height": 1080},
    }


def test_validate_recipe_accepts_clean_input():
    assert validate_recipe(_good_recipe()) == []


def test_validate_recipe_rejects_non_monotonic_time():
    r = _good_recipe()
    r["camera"][2]["t"] = 5  # earlier than waypoint 1's t=15
    issues = validate_recipe(r)
    assert any("not strictly increasing" in i for i in issues)


def test_validate_recipe_rejects_out_of_bounds_lat_lon():
    r = _good_recipe()
    r["camera"][1]["lat"] = 95
    r["camera"][1]["lon"] = -200
    issues = validate_recipe(r)
    assert any("lat 95" in i for i in issues)
    assert any("lon -200" in i for i in issues)


def test_validate_recipe_rejects_impossible_altitudes():
    r = _good_recipe()
    r["camera"][1]["altitude_m"] = 0.0  # below Earth Studio min
    issues = validate_recipe(r)
    assert any("altitude" in i for i in issues)


def test_validate_recipe_rejects_final_waypoint_after_duration():
    r = _good_recipe()
    r["camera"][-1]["t"] = 999  # past duration_seconds=30
    issues = validate_recipe(r)
    assert any("exceeds duration_seconds" in i for i in issues)


def test_validate_recipe_rejects_short_camera_path():
    issues = validate_recipe(
        {
            "duration_seconds": 10,
            "frame_rate": 30,
            "camera": [{"t": 0, "lat": 0, "lon": 0, "altitude_m": 1e7, "heading": 0, "tilt": 0}],
        }
    )
    assert any("at least 2 waypoints" in i for i in issues)


def test_lint_catalog_includes_every_recipe():
    catalog = json.loads((Path(__file__).parent.parent / "app" / "static" / "scenarios.json").read_text())
    issues = lint_catalog(catalog)
    # Every concrete scenario has a recipe → entry in the lint map
    assert "may-2024-g5" in issues
    assert "halloween-2003" in issues
    # All shipped recipes pass (no issues)
    for sid, sc_issues in issues.items():
        assert sc_issues == [], f"{sid} recipe has issues: {sc_issues}"


# ── Caveat 2: camera-track CSV ──────────────────────────────────────────────


def test_camera_csv_columns_match_earth_studio_format():
    csv_text = recipe_to_camera_csv(_good_recipe())
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    assert tuple(header) == CAMERA_CSV_COLUMNS


def test_camera_csv_emits_one_row_per_frame():
    """30 s × 30 fps → 901 rows including the trailing frame."""
    csv_text = recipe_to_camera_csv(_good_recipe())
    rows = list(csv.reader(io.StringIO(csv_text)))
    # header + (duration*fps + 1) frames
    assert len(rows) - 1 == 30 * 30 + 1


def test_camera_csv_first_row_matches_first_waypoint():
    csv_text = recipe_to_camera_csv(_good_recipe())
    rows = list(csv.reader(io.StringIO(csv_text)))
    first = rows[1]
    assert first[0] == "0.0000"
    assert float(first[1]) == 0.0  # lat
    assert float(first[2]) == 0.0  # lon
    assert float(first[3]) == 25_000_000.0


def test_camera_csv_lerp_at_midpoint():
    csv_text = recipe_to_camera_csv(_good_recipe())
    rows = list(csv.reader(io.StringIO(csv_text)))[1:]
    # Halfway between waypoint 0 (t=0, lat=0) and waypoint 1 (t=15, lat=60)
    # → t=7.5s, lat ~30
    target = next(r for r in rows if abs(float(r[0]) - 7.5) < 0.02)
    assert abs(float(target[1]) - 30) < 0.5


def test_interp_lon_handles_date_line():
    # 175 → -175 should interp through 180, not back across 0
    out = _interp_lon(175, -175, 0.5)
    assert out > 175 or out < -175 or abs(out) > 179
    # Going west across date line — interp should travel ~10° not ~350°
    out_west = _interp_lon(170, -170, 0.5)
    assert abs(out_west) > 170  # near ±180 line


# ── Caveat 3: DB-backed video registration ──────────────────────────────────


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
async def test_video_store_register_and_lookup(memory_db):
    from app.data import scenario_video_store as svs

    row = await svs.register(
        "may-2024-g5",
        video_url="https://cdn.example.com/may.mp4",
        duration_seconds=30,
        notes="test",
    )
    assert row["video_url"] == "https://cdn.example.com/may.mp4"
    fetched = await svs.lookup("may-2024-g5")
    assert fetched["video_url"] == "https://cdn.example.com/may.mp4"
    assert fetched["duration_seconds"] == 30


@pytest.mark.asyncio
async def test_video_store_register_is_upsert(memory_db):
    from app.data import scenario_video_store as svs

    await svs.register("x", video_url="https://a.example.com/v.mp4")
    await svs.register("x", video_url="https://b.example.com/v.mp4")
    rows = await svs.lookup_all()
    assert len(rows) == 1
    assert rows["x"]["video_url"] == "https://b.example.com/v.mp4"


@pytest.mark.asyncio
async def test_video_store_unregister_returns_bool(memory_db):
    from app.data import scenario_video_store as svs

    await svs.register("x", video_url="https://a.example.com/v.mp4")
    assert await svs.unregister("x") is True
    assert await svs.unregister("x") is False


@pytest.mark.asyncio
async def test_register_endpoint_writes_db_first_sidecar_second(
    memory_db,
    tmp_path,
    monkeypatch,
):
    from app.data import scenario_precompute as sp

    monkeypatch.setattr(sp, "OUTPUT_ROOT", tmp_path / "out")

    with TestClient(app) as client:
        r = client.post(
            "/api/v3/scenarios/may-2024-g5/video",
            json={"video_url": "https://cdn.example.com/may.mp4", "duration_seconds": 30},
        )
        assert r.status_code == 200, r.text

    # DB row exists
    from app.data import scenario_video_store as svs

    row = await svs.lookup("may-2024-g5")
    assert row["video_url"] == "https://cdn.example.com/may.mp4"
    # Sidecar also exists (write-through)
    sidecar = tmp_path / "out" / "may-2024-g5" / "video.json"
    assert sidecar.exists()


@pytest.mark.asyncio
async def test_catalog_uses_db_when_sidecar_missing(memory_db, tmp_path, monkeypatch):
    """Free-tier scenario: ephemeral disk wiped, sidecar gone, DB persists."""
    from app.data import scenario_precompute as sp, scenario_video_store as svs

    monkeypatch.setattr(sp, "OUTPUT_ROOT", tmp_path / "empty")

    await svs.register(
        "may-2024-g5",
        video_url="https://cdn.example.com/db-only.mp4",
        duration_seconds=30,
    )
    with TestClient(app) as client:
        body = client.get("/api/v3/scenarios").json()
        may = next(s for s in body["scenarios"] if s["id"] == "may-2024-g5")
        assert may["video_url"] == "https://cdn.example.com/db-only.mp4"


# ── Caveat 4: URL validation + allowlist ────────────────────────────────────


def test_validate_url_rejects_javascript_scheme(memory_db):
    from app.data.scenario_video_store import validate_video_url, InvalidVideoURL

    with pytest.raises(InvalidVideoURL):
        validate_video_url("javascript:alert(1)")


def test_validate_url_rejects_data_scheme(memory_db):
    from app.data.scenario_video_store import validate_video_url, InvalidVideoURL

    with pytest.raises(InvalidVideoURL):
        validate_video_url("data:video/mp4;base64,AAA")


def test_validate_url_rejects_external_http(memory_db):
    from app.data.scenario_video_store import validate_video_url, InvalidVideoURL

    with pytest.raises(InvalidVideoURL):
        validate_video_url("http://untrusted.example.com/v.mp4")


def test_validate_url_allows_localhost_http_for_dev(memory_db):
    from app.data.scenario_video_store import validate_video_url

    assert validate_video_url("http://localhost:8000/v.mp4")
    assert validate_video_url("http://127.0.0.1:8000/v.mp4")


def test_validate_url_accepts_https(memory_db):
    from app.data.scenario_video_store import validate_video_url

    assert validate_video_url("https://cdn.example.com/v.mp4")


def test_validate_url_enforces_allowlist_when_configured(memory_db, monkeypatch):
    from app.config import settings
    from app.data.scenario_video_store import (
        InvalidVideoURL,
        validate_video_url,
    )

    monkeypatch.setattr(settings, "video_domain_allowlist", "cdn.example.com,r2.example.org")
    # Allowed
    assert validate_video_url("https://cdn.example.com/x.mp4")
    assert validate_video_url("https://sub.cdn.example.com/x.mp4")
    assert validate_video_url("https://r2.example.org/x.mp4")
    # Rejected
    with pytest.raises(InvalidVideoURL):
        validate_video_url("https://attacker.example.net/x.mp4")


def test_register_endpoint_400_on_bad_url(memory_db):
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/scenarios/may-2024-g5/video",
            json={"video_url": "javascript:alert(1)"},
        )
        assert r.status_code == 400
        assert "javascript" in r.json()["detail"] or "scheme" in r.json()["detail"]


# ── Recipe endpoint lint flag ───────────────────────────────────────────────


def test_recipe_endpoint_lint_flag_returns_issues_field():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/may-2024-g5/recipe?lint=true")
        assert r.status_code == 200
        body = r.json()
        assert "recipe_issues" in body
        # Shipped recipes are clean
        assert body["recipe_issues"] == []


# ── Migration file present ─────────────────────────────────────────────────


def test_b4_caveats_migration_exists():
    p = Path(__file__).parent.parent / "migrations" / "0003_b4_caveats.sql"
    assert p.exists()
    text = p.read_text()
    assert "scenario_videos" in text
    assert "video_url" in text
