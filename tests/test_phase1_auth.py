"""Phase 1 — per-tenant Bearer auth, admin endpoints, audit log."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.data import api_keys, audit_log
from app.data import db as db_module
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


# ── api_keys store ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_and_lookup_roundtrip(memory_db):
    minted = await api_keys.mint_key(tenant_id="acme", label="ops")
    assert minted["plaintext"].startswith("iks_")
    assert minted["tenant_id"] == "acme"

    resolved = await api_keys.lookup_key(minted["plaintext"])
    assert resolved is not None
    assert resolved["tenant_id"] == "acme"
    assert resolved["id"] == minted["id"]


@pytest.mark.asyncio
async def test_lookup_unknown_returns_none(memory_db):
    assert await api_keys.lookup_key("iks_doesnotexist") is None
    assert await api_keys.lookup_key("") is None


@pytest.mark.asyncio
async def test_revoked_key_no_longer_resolves(memory_db):
    minted = await api_keys.mint_key(tenant_id="acme")
    assert await api_keys.revoke_key(minted["id"]) is True
    assert await api_keys.lookup_key(minted["plaintext"]) is None
    # Double-revoke is a no-op
    assert await api_keys.revoke_key(minted["id"]) is False


@pytest.mark.asyncio
async def test_list_keys_filters_by_tenant(memory_db):
    await api_keys.mint_key(tenant_id="acme", label="a")
    await api_keys.mint_key(tenant_id="initech", label="b")
    acme = await api_keys.list_keys(tenant_id="acme")
    assert len(acme) == 1 and acme[0]["tenant_id"] == "acme"
    all_keys = await api_keys.list_keys()
    assert len(all_keys) == 2


@pytest.mark.asyncio
async def test_list_keys_does_not_leak_plaintext(memory_db):
    await api_keys.mint_key(tenant_id="acme")
    rows = await api_keys.list_keys()
    for r in rows:
        assert "plaintext" not in r
        assert "key_hash" not in r


@pytest.mark.asyncio
async def test_minted_keys_have_unique_plaintext(memory_db):
    a = await api_keys.mint_key(tenant_id="acme")
    b = await api_keys.mint_key(tenant_id="acme")
    assert a["plaintext"] != b["plaintext"]


# ── audit log ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_records_and_reads(memory_db):
    await audit_log.record(
        tenant_id="acme",
        key_id=1,
        method="GET",
        path="/api/v3/risk-map",
        status_code=200,
        remote_addr="1.2.3.4",
        user_agent="test",
    )
    rows = await audit_log.recent()
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "acme"
    assert rows[0]["path"] == "/api/v3/risk-map"


# ── HTTP: admin guard ────────────────────────────────────────────────────────


def test_admin_endpoint_blocked_when_admin_bearer_unset(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_bearer", "")
    with TestClient(app) as client:
        r = client.post("/api/v3/admin/keys", json={"tenant_id": "acme"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


def test_admin_endpoint_rejects_wrong_bearer(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_bearer", "supersecret")
    with TestClient(app) as client:
        r = client.post(
            "/api/v3/admin/keys",
            json={"tenant_id": "acme"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert r.status_code == 401


# ── HTTP: bearer token end-to-end ───────────────────────────────────────────


def test_bearer_auth_resolves_tenant(monkeypatch):
    """API_KEY set forces auth on. Bearer token from a minted key must work."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "legacy-secret")  # auth_enabled = True
    monkeypatch.setattr(settings, "admin_bearer", "admin-secret")

    with TestClient(app) as client:
        # Mint a key via admin endpoint
        r = client.post(
            "/api/v3/admin/keys",
            json={"tenant_id": "acme", "label": "ci"},
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert r.status_code == 201, r.text
        plaintext = r.json()["plaintext"]
        assert plaintext.startswith("iks_")

        # Use that key on a real endpoint
        r2 = client.get(
            "/api/v3/risk-map",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert r2.status_code == 200

        # Wrong bearer is rejected
        r3 = client.get(
            "/api/v3/risk-map",
            headers={"Authorization": "Bearer iks_garbage"},
        )
        assert r3.status_code == 401


def test_legacy_x_api_key_still_works(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "legacy-secret")
    with TestClient(app) as client:
        r = client.get("/api/v3/risk-map", headers={"X-API-Key": "legacy-secret"})
        assert r.status_code == 200
        r2 = client.get("/api/v3/risk-map", headers={"X-API-Key": "wrong"})
        assert r2.status_code == 401
