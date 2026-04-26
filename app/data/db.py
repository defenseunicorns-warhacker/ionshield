"""
IonShield async database layer.

Default backend: SQLite (sqlite+aiosqlite:///./ionshield.db)
Production backend: set DATABASE_URL=postgresql+asyncpg://user:pass@host/db

Tables
------
noaa_snapshots  — one row per NOAA fetch cycle (default ~5 min cadence).
pilot_inquiries — contact / pilot-program form submissions.

Schema versioning
-----------------
Development / test: tables are created via create_all() in init_db().
Production: run migrations/0001_initial.sql before the first deploy, then
  use alembic (or the SQL file directly) for subsequent schema changes.
"""

from __future__ import annotations

import logging

from sqlalchemy import Column, DateTime, Float, Index, Integer, MetaData, Table, Text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

metadata = MetaData()

noaa_snapshots = Table(
    "noaa_snapshots",
    metadata,
    # Surrogate PK
    Column("id", Integer, primary_key=True, autoincrement=True),
    # When the fetch completed (UTC)
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    # "live" | "fallback" | "hardcoded_validation"
    Column("fetch_source", Text, nullable=False, server_default="live"),
    # Derived scalar values — what the risk / decision engines actually use
    Column("kp", Float, nullable=False),
    Column("bz_nt", Float, nullable=False),
    Column("xray_flux", Float, nullable=False),
    Column("proton_flux_10mev", Float, nullable=False),
    Column("wind_speed_km_s", Float, nullable=False),
    # 24-hour peak forecast Kp — NULL when kp_forecast feed was unavailable
    Column("kp_forecast_24h", Float, nullable=True),
    # JSON arrays: ["kp", "xray", ...] — mirrors EnvironmentSnapshot fields
    Column("feeds_available", Text, nullable=False),
    Column("feeds_unavailable", Text, nullable=False),
    # Seconds between data timestamp and this archive write (usually ~0)
    Column("data_age_seconds", Integer, nullable=False, server_default="0"),
)

# Index for efficient lookup by time (used by the ?at= replay locator)
Index("ix_noaa_snapshots_fetched_at", noaa_snapshots.c.fetched_at)

# ── pilot_inquiries ───────────────────────────────────────────────────────────

pilot_inquiries = Table(
    "pilot_inquiries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("org", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("sector", Text, nullable=False, server_default="Other"),
    Column("interest", Text, nullable=False, server_default=""),
    # SHA-256 of client IP — stored for abuse detection, not for attribution.
    # Raw IPs are never persisted.
    Column("ip_hash", Text, nullable=False, server_default=""),
    # 0 = not sent (SMTP disabled or error); 1 = sent
    Column("email_sent", Integer, nullable=False, server_default="0"),
    # "new" | "read" | "spam" (spam = honeypot triggered)
    Column("status", Text, nullable=False, server_default="new"),
)

Index("ix_pilot_inquiries_created_at", pilot_inquiries.c.created_at)

# ── Engine management ─────────────────────────────────────────────────────────

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the module-level async engine, creating it on first call."""
    global _engine
    if _engine is None:
        from app.config import settings

        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            future=True,
        )
    return _engine


def override_engine(engine: AsyncEngine | None) -> None:
    """
    Replace the module-level engine.

    Pass None to clear it (next get_engine() call will re-create from settings).
    Used in tests to inject an in-memory SQLite engine without touching settings.
    """
    global _engine
    _engine = engine


# ── Initialisation ────────────────────────────────────────────────────────────


async def init_db() -> None:
    """
    Create all tables if they do not already exist (idempotent).

    Called once during app startup lifespan. Safe to call multiple times.
    In production, prefer running migrations/0001_initial.sql explicitly so
    schema changes are tracked; this function is fine for dev / test.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    logger.info("Database ready (create_all completed).")
