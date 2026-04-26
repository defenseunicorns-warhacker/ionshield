-- IonShield schema migration 0001 — initial
-- Run once against a fresh database before first deploy.
-- For SQLite: sqlite3 ionshield.db < migrations/0001_initial.sql
-- For PostgreSQL: psql $DATABASE_URL < migrations/0001_initial.sql
--
-- Development / test: the app auto-creates these tables via SQLAlchemy
-- create_all() on startup (init_db()). This file is the authoritative
-- schema definition for production deployments.

CREATE TABLE IF NOT EXISTS noaa_snapshots (
    id                INTEGER  PRIMARY KEY AUTOINCREMENT,  -- use SERIAL for PostgreSQL
    fetched_at        DATETIME NOT NULL,
    fetch_source      TEXT     NOT NULL DEFAULT 'live',
    kp                REAL     NOT NULL,
    bz_nt             REAL     NOT NULL,
    xray_flux         REAL     NOT NULL,
    proton_flux_10mev REAL     NOT NULL,
    wind_speed_km_s   REAL     NOT NULL,
    kp_forecast_24h   REAL,                               -- NULL when feed unavailable
    feeds_available   TEXT     NOT NULL,                  -- JSON array of feed names
    feeds_unavailable TEXT     NOT NULL,                  -- JSON array of feed names
    data_age_seconds  INTEGER  NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_noaa_snapshots_fetched_at
    ON noaa_snapshots (fetched_at);

-- PostgreSQL variant (comment out above and uncomment below when using Postgres):
--
-- CREATE TABLE IF NOT EXISTS noaa_snapshots (
--     id                SERIAL   PRIMARY KEY,
--     fetched_at        TIMESTAMPTZ NOT NULL,
--     fetch_source      TEXT     NOT NULL DEFAULT 'live',
--     kp                REAL     NOT NULL,
--     bz_nt             REAL     NOT NULL,
--     xray_flux         REAL     NOT NULL,
--     proton_flux_10mev REAL     NOT NULL,
--     wind_speed_km_s   REAL     NOT NULL,
--     kp_forecast_24h   REAL,
--     feeds_available   TEXT     NOT NULL,
--     feeds_unavailable TEXT     NOT NULL,
--     data_age_seconds  INTEGER  NOT NULL DEFAULT 0
-- );
--
-- CREATE INDEX IF NOT EXISTS ix_noaa_snapshots_fetched_at
--     ON noaa_snapshots (fetched_at);
