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

# ── Events ────────────────────────────────────────────────────────────────────
# One row per detected space-weather event. The detector uses (event_type,
# state) plus the t_onset key as a natural identity: re-detecting the same
# ongoing storm updates t_peak / peak_value / drivers in place rather than
# inserting a duplicate.

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", Text, nullable=False),  # ontology.EventType.value
    Column("state", Text, nullable=False),  # "ONSET" | "PEAK" | "ENDED"
    Column("severity", Text, nullable=False),  # G1..G5 / S1..S5 / R1..R5 / NA
    Column("region_id", Text, nullable=False, server_default="GLOBAL"),
    Column("t_onset", DateTime(timezone=True), nullable=False),
    Column("t_peak", DateTime(timezone=True), nullable=True),
    Column("t_end", DateTime(timezone=True), nullable=True),
    Column("driver", Text, nullable=False),  # which Driver triggered it
    Column("peak_value", Float, nullable=True),
    Column("trigger_value", Float, nullable=False),
    Column("threshold_value", Float, nullable=False),
    Column("rationale", Text, nullable=False, server_default=""),
    Column("classifier", Text, nullable=False, server_default="rule"),  # rule|ml
    Column("confidence", Float, nullable=False, server_default="1.0"),
)

Index("ix_events_onset", events.c.t_onset)
Index("ix_events_active", events.c.event_type, events.c.state)

# ── Circuit-breaker state ────────────────────────────────────────────────────
# One row per registered data source. The CircuitBreaker rehydrates from this
# table at lifespan start and writes back on every state transition so an
# OPEN breaker survives an app restart — without a backing store, a fast
# restart loop would silently re-hammer a broken upstream.

# ── A7 — feedback loop tables ────────────────────────────────────────────────
# `training_samples` records the feature vector and rule-label + ML prediction
# at every refresh tick. Operators (or downstream outcomes) can later attach a
# `user_feedback` correction. Retraining uses these as training data.
#
# `model_versions` keeps an append-only history; one row is `active=1`. Atomic
# swap is a single UPDATE that flips `active` flags inside a transaction.
#
# `outcomes` is a generic ground-truth table (observed GPS error, observed HF
# link availability, etc.) so the platform can be evaluated against reality.

training_samples = Table(
    "training_samples",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("region_id", Text, nullable=False, server_default="GLOBAL"),
    # JSON-encoded feature vector matching FEATURE_NAMES in ml_classifier
    Column("features_json", Text, nullable=False),
    Column("rule_label", Text, nullable=False),  # ground truth from rules
    Column("ml_label", Text, nullable=True),  # classifier prediction
    Column("ml_confidence", Float, nullable=True),
    # Operator-provided correction; empty string = unlabeled
    Column("user_feedback", Text, nullable=False, server_default=""),
    Column("user_feedback_at", DateTime(timezone=True), nullable=True),
    # Free-form note about what actually happened — outcome label
    Column("outcome_label", Text, nullable=False, server_default=""),
    # Foreign-style link to events.id — soft pointer, not a hard FK
    Column("event_id", Integer, nullable=True),
    # Champion/challenger shadow predictions (A7 caveat fix)
    Column("challenger_label", Text, nullable=True),
    Column("challenger_confidence", Float, nullable=True),
)

Index("ix_training_samples_created", training_samples.c.created_at)
Index("ix_training_samples_event", training_samples.c.event_id)


model_versions = Table(
    "model_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version", Text, nullable=False, unique=True),
    Column("trained_at", DateTime(timezone=True), nullable=False),
    Column("n_train", Integer, nullable=False),
    Column("n_real_samples", Integer, nullable=False, server_default="0"),
    Column("train_accuracy", Float, nullable=True),
    Column("artifact_path", Text, nullable=False),
    Column("notes", Text, nullable=False, server_default=""),
    Column("active", Integer, nullable=False, server_default="0"),  # 1 = currently loaded
    # Champion/challenger shadow flag (A7 caveat fix). Exactly one row may have
    # active=1; a separate row may have challenger=1 during a shadow window.
    Column("challenger", Integer, nullable=False, server_default="0"),
)

Index("ix_model_versions_active", model_versions.c.active)
Index("ix_model_versions_challenger", model_versions.c.challenger)


outcomes = Table(
    "outcomes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("region_id", Text, nullable=True),
    Column("system", Text, nullable=False),  # GPS / HF / SATCOM / RADAR
    Column("subsystem", Text, nullable=False),  # GPS_L1, L, X, etc
    Column("metric", Text, nullable=False),  # error_m, fade_db, ...
    Column("observed_value", Float, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("source", Text, nullable=False, server_default="user"),
    Column("notes", Text, nullable=False, server_default=""),
)

Index("ix_outcomes_observed", outcomes.c.observed_at)
Index("ix_outcomes_system", outcomes.c.system, outcomes.c.subsystem)


# ── B4 caveat 3 fix: DB-backed scenario video registrations ─────────────────
# Per-scenario rendered-video URLs. The static-disk sidecar layer stays in
# place as a write-through cache, but the canonical store is now SQL so a
# free-tier deployment without a persistent disk doesn't lose registrations
# on cold restart. One row per scenario_id (UNIQUE), upsert semantics.

scenario_videos = Table(
    "scenario_videos",
    metadata,
    Column("scenario_id", Text, primary_key=True),
    Column("video_url", Text, nullable=False),
    Column("duration_seconds", Float, nullable=True),
    Column("rendered_at", DateTime(timezone=True), nullable=True),
    Column("notes", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


# Phase 1: per-tenant API keys (Bearer token auth) + audit log
api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", Text, nullable=False),
    Column("label", Text, nullable=False, server_default=""),
    Column("prefix", Text, nullable=False),
    Column("key_hash", Text, nullable=False, unique=True),
    Column("scopes", Text, nullable=False, server_default="read"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Index("idx_api_keys_tenant", "tenant_id"),
    Index("idx_api_keys_active", "revoked_at"),
)


api_audit_log = Table(
    "api_audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("tenant_id", Text, nullable=True),
    Column("key_id", Integer, nullable=True),
    Column("method", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("remote_addr", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    Index("idx_audit_at", "at"),
    Index("idx_audit_tenant", "tenant_id"),
)


breaker_state = Table(
    "breaker_state",
    metadata,
    Column("name", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("total_failures", Integer, nullable=False, server_default="0"),
    Column("total_successes", Integer, nullable=False, server_default="0"),
    # Wall-clock UTC of last state change (epoch seconds)
    Column("last_change_epoch", Float, nullable=True),
    Column("last_failure_epoch", Float, nullable=True),
    Column("last_success_epoch", Float, nullable=True),
)

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
