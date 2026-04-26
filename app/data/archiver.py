"""
NOAA observation archiver.

Called after each fetch_noaa() to persist the current NOAA cache state as a
noaa_snapshots row. Failures are non-fatal — logged as warnings and swallowed
so a DB outage never takes down the API.

Also provides snapshot_row_to_env() to reconstruct an EnvironmentSnapshot
from a stored row, enabling deterministic replay.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, insert, select

from app.data.db import get_engine, noaa_snapshots
from app.models.decision import EnvironmentSnapshot, ObservationInput

logger = logging.getLogger(__name__)


async def archive_snapshot() -> int | None:
    """
    Persist the current NOAA in-memory cache to a noaa_snapshots row.

    Returns the new row ID on success, None if archiving is disabled or fails.
    """
    from app.config import settings

    if not settings.archive_enabled:
        return None

    # Deferred import to avoid circular import at module load time
    from app.data import noaa as _noaa

    try:
        snap = _noaa.cache_snapshot()
        feed_status: dict[str, str] = snap["fetch_status"]

        feeds_available = [k for k, v in feed_status.items() if v == "ok"]
        feeds_unavailable = [k for k, v in feed_status.items() if v != "ok"]

        kp_val = _noaa.get_kp()
        bz_val = _noaa.get_bz()
        xray_val = _noaa.get_xray_flux()
        proton_val = _noaa.get_proton_flux_10mev()
        wind_val = _noaa.get_wind_speed()
        kp_forecast = _extract_kp_forecast_24h(_noaa._cache)

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                insert(noaa_snapshots).values(
                    fetched_at=datetime.now(timezone.utc),
                    fetch_source=snap["fetch_source"] or "unknown",
                    kp=kp_val,
                    bz_nt=bz_val,
                    xray_flux=xray_val,
                    proton_flux_10mev=proton_val,
                    wind_speed_km_s=wind_val,
                    kp_forecast_24h=kp_forecast,
                    feeds_available=json.dumps(feeds_available),
                    feeds_unavailable=json.dumps(feeds_unavailable),
                    data_age_seconds=snap["data_age_seconds"],
                )
            )
        row_id = result.lastrowid
        logger.debug("Archived NOAA snapshot id=%s (kp=%.1f)", row_id, kp_val)
        return row_id

    except Exception as exc:
        logger.warning("Failed to archive NOAA snapshot: %s", exc)
        return None


def _extract_kp_forecast_24h(cache: dict) -> float | None:
    """
    Extract the 24-hour peak forecast Kp from the NOAA kp_forecast cache entry.

    Mirrors the same logic used in routes_v2._build_env() to ensure consistency
    between live env snapshots and archived ones.
    Returns None when the forecast feed was unavailable or contained no predicted rows.
    """
    forecast_data = cache.get("kp_forecast")
    if not forecast_data or len(forecast_data) < 2:
        return None
    try:
        rows = forecast_data[1:]  # row 0 is the header
        now_dt = datetime.now(timezone.utc)
        future_kps: list[float] = []
        for row in rows:
            if len(row) < 3:
                continue
            obs_pred = str(row[2]).strip().lower()
            if obs_pred not in ("predicted", "estimated"):
                continue
            try:
                row_dt = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                delta_h = (row_dt - now_dt).total_seconds() / 3600
                if 0 <= delta_h <= 24:
                    future_kps.append(float(row[1]))
            except (ValueError, TypeError):
                continue
        return max(future_kps) if future_kps else None
    except Exception:
        return None


def snapshot_row_to_env(row: Any) -> EnvironmentSnapshot:
    """
    Reconstruct a fully typed EnvironmentSnapshot from a noaa_snapshots row.

    `row` may be a SQLAlchemy Row, RowMapping, or any object whose attributes
    match the noaa_snapshots column names.
    """
    feeds_available: list[str] = json.loads(row.feeds_available or "[]")
    feeds_unavailable: list[str] = json.loads(row.feeds_unavailable or "[]")

    fetched_at = row.fetched_at
    fetched_at_iso = (
        fetched_at.isoformat()
        if hasattr(fetched_at, "isoformat")
        else str(fetched_at)
    )
    age = int(row.data_age_seconds or 0)

    observations = [
        ObservationInput(
            "NOAA_SWPC", "kp_index", float(row.kp), "index", fetched_at_iso, age
        ),
        ObservationInput(
            "NOAA_SWPC", "bz_gsm_nt", float(row.bz_nt), "nT", fetched_at_iso, age
        ),
        ObservationInput(
            "NOAA_SWPC",
            "xray_flux_wm2",
            float(row.xray_flux),
            "W/m²",
            fetched_at_iso,
            age,
        ),
        ObservationInput(
            "NOAA_SWPC",
            "proton_flux_10mev_pfu",
            float(row.proton_flux_10mev),
            "pfu",
            fetched_at_iso,
            age,
        ),
        ObservationInput(
            "NOAA_SWPC",
            "solar_wind_km_s",
            float(row.wind_speed_km_s),
            "km/s",
            fetched_at_iso,
            age,
        ),
    ]

    return EnvironmentSnapshot(
        kp=float(row.kp),
        bz_nt=float(row.bz_nt),
        xray_flux=float(row.xray_flux),
        proton_flux_10mev=float(row.proton_flux_10mev),
        wind_speed_km_s=float(row.wind_speed_km_s),
        data_age_seconds=age,
        feeds_available=feeds_available,
        feeds_unavailable=feeds_unavailable,
        observations=observations,
        kp_forecast_24h=(
            float(row.kp_forecast_24h)
            if row.kp_forecast_24h is not None
            else None
        ),
    )


async def get_snapshot_by_id(snapshot_id: int) -> Any | None:
    """Fetch a single noaa_snapshots row by primary key. Returns None if not found."""
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(noaa_snapshots).where(noaa_snapshots.c.id == snapshot_id)
        )
        return result.mappings().first()


async def get_snapshot_at_or_before(at: datetime) -> Any | None:
    """
    Fetch the most recent snapshot at or before `at` (UTC).
    Returns None if no snapshot exists before that time.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(noaa_snapshots)
            .where(noaa_snapshots.c.fetched_at <= at)
            .order_by(noaa_snapshots.c.fetched_at.desc())
            .limit(1)
        )
        return result.mappings().first()


async def list_snapshots(limit: int = 20, offset: int = 0) -> list[dict]:
    """Return a paginated list of snapshot rows, most-recent first."""
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            select(noaa_snapshots)
            .order_by(noaa_snapshots.c.fetched_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = result.mappings().all()

    return [
        {
            "id": r["id"],
            "fetched_at": (
                r["fetched_at"].isoformat()
                if hasattr(r["fetched_at"], "isoformat")
                else str(r["fetched_at"])
            ),
            "fetch_source": r["fetch_source"],
            "kp": r["kp"],
            "bz_nt": r["bz_nt"],
            "xray_flux": r["xray_flux"],
            "proton_flux_10mev": r["proton_flux_10mev"],
            "wind_speed_km_s": r["wind_speed_km_s"],
            "kp_forecast_24h": r["kp_forecast_24h"],
            "feeds_available": json.loads(r["feeds_available"] or "[]"),
            "feeds_unavailable": json.loads(r["feeds_unavailable"] or "[]"),
            "data_age_seconds": r["data_age_seconds"],
        }
        for r in rows
    ]


async def count_snapshots() -> int:
    """Return total number of archived snapshots."""
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(select(func.count()).select_from(noaa_snapshots))
        return result.scalar() or 0
