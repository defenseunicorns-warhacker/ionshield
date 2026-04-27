"""
Phase 1 — per-tenant Bearer-token API keys.

Plaintext keys are shown to the operator exactly once at mint time and never
persisted. Storage is sha256(plaintext); the first 12 chars (`prefix`) are
kept for display so a list of keys can show "iks_a3f01b2c…" without leaking
the secret. Scopes are a comma-separated list ("read", "read,write").
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update

from app.data.db import api_keys as table
from app.data.db import get_engine

KEY_PREFIX = "iks_"
KEY_LEN = 32  # post-prefix urlsafe-base64 chars → 192 bits of entropy


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_LEN)[:KEY_LEN]


async def mint_key(tenant_id: str, label: str = "", scopes: str = "read") -> dict[str, Any]:
    """Create a new key. Returns {plaintext, prefix, id, ...}. Plaintext shown once."""
    plaintext = _generate()
    key_hash = _hash(plaintext)
    prefix = plaintext[: len(KEY_PREFIX) + 8]
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(table).values(
                tenant_id=tenant_id,
                label=label,
                prefix=prefix,
                key_hash=key_hash,
                scopes=scopes,
                created_at=_now(),
            )
        )
        key_id = result.inserted_primary_key[0]
    return {
        "id": key_id,
        "tenant_id": tenant_id,
        "label": label,
        "scopes": scopes,
        "prefix": prefix,
        "plaintext": plaintext,  # CALLER MUST DISPLAY ONCE AND DISCARD
        "created_at": _now().isoformat(),
    }


async def lookup_key(plaintext: str) -> dict[str, Any] | None:
    """Resolve a plaintext token → tenant_id + scopes, or None if invalid/revoked.

    Touches `last_used` on success."""
    if not plaintext:
        return None
    key_hash = _hash(plaintext)
    engine = get_engine()
    async with engine.begin() as conn:
        row = (
            (await conn.execute(select(table).where(table.c.key_hash == key_hash).where(table.c.revoked_at.is_(None))))
            .mappings()
            .first()
        )
        if not row:
            return None
        await conn.execute(update(table).where(table.c.id == row["id"]).values(last_used=_now()))
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "scopes": row["scopes"],
        "label": row["label"],
    }


async def list_keys(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return non-secret metadata for all keys, optionally filtered by tenant."""
    engine = get_engine()
    async with engine.begin() as conn:
        stmt = select(table).order_by(table.c.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(table.c.tenant_id == tenant_id)
        rows = (await conn.execute(stmt)).mappings().all()
    return [
        {
            "id": r["id"],
            "tenant_id": r["tenant_id"],
            "label": r["label"],
            "prefix": r["prefix"],
            "scopes": r["scopes"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_used": r["last_used"].isoformat() if r["last_used"] else None,
            "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
            "active": r["revoked_at"] is None,
        }
        for r in rows
    ]


async def revoke_key(key_id: int) -> bool:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table).where(table.c.id == key_id).where(table.c.revoked_at.is_(None)).values(revoked_at=_now())
        )
    return result.rowcount > 0
