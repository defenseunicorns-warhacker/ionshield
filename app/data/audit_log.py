"""
Phase 1 — per-request audit log.

Records {tenant, key_id, method, path, status, IP, UA, timestamp} for every
authenticated request that hits an audited route. Used for billing,
forensics, and compliance evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select

from app.data.db import api_audit_log as table
from app.data.db import get_engine


async def record(
    *,
    tenant_id: str | None,
    key_id: int | None,
    method: str,
    path: str,
    status_code: int,
    remote_addr: str | None,
    user_agent: str | None,
) -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            insert(table).values(
                at=datetime.now(timezone.utc),
                tenant_id=tenant_id,
                key_id=key_id,
                method=method,
                path=path,
                status_code=status_code,
                remote_addr=remote_addr,
                user_agent=user_agent,
            )
        )


async def recent(limit: int = 100, tenant_id: str | None = None) -> list[dict[str, Any]]:
    engine = get_engine()
    async with engine.begin() as conn:
        stmt = select(table).order_by(table.c.at.desc()).limit(limit)
        if tenant_id is not None:
            stmt = stmt.where(table.c.tenant_id == tenant_id)
        rows = (await conn.execute(stmt)).mappings().all()
    return [
        {
            "at": r["at"].isoformat() if r["at"] else None,
            "tenant_id": r["tenant_id"],
            "key_id": r["key_id"],
            "method": r["method"],
            "path": r["path"],
            "status_code": r["status_code"],
            "remote_addr": r["remote_addr"],
            "user_agent": r["user_agent"],
        }
        for r in rows
    ]
