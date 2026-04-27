-- A7 caveat fixes — additive migration.
--
-- Adds:
--   training_samples.challenger_label / challenger_confidence  (shadow mode)
--   model_versions.challenger                                  (champion/challenger flag)
--
-- ALL changes are ADDITIVE so this migration is safe to apply against an
-- existing database without data loss. SQLite supports `ALTER TABLE ADD
-- COLUMN`; PostgreSQL also supports it.

ALTER TABLE training_samples
    ADD COLUMN challenger_label TEXT;

ALTER TABLE training_samples
    ADD COLUMN challenger_confidence REAL;

ALTER TABLE model_versions
    ADD COLUMN challenger INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_model_versions_challenger
    ON model_versions (challenger);
