-- Phase 1: per-tenant API keys + audit log.
--
-- Keys are stored as sha256(key_plaintext) — plaintext is shown to the user
-- exactly once at mint time and never persisted. `prefix` is the first 12
-- chars of the plaintext for display purposes (e.g. "iks_a3f01b2c…").

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT     NOT NULL,
    label       TEXT     NOT NULL DEFAULT '',
    prefix      TEXT     NOT NULL,
    key_hash    TEXT     NOT NULL UNIQUE,
    scopes      TEXT     NOT NULL DEFAULT 'read',
    created_at  TIMESTAMP NOT NULL,
    last_used   TIMESTAMP,
    revoked_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at);


CREATE TABLE IF NOT EXISTS api_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TIMESTAMP NOT NULL,
    tenant_id    TEXT,
    key_id       INTEGER,
    method       TEXT     NOT NULL,
    path         TEXT     NOT NULL,
    status_code  INTEGER  NOT NULL,
    remote_addr  TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON api_audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON api_audit_log(tenant_id);
