"""
DB-backed persistence of circuit-breaker state.

Single-replica scope: this is enough to ensure an OPEN breaker recorded
just before a process restart stays OPEN until its cooldown elapses.

Multi-replica / scale-out contract: replace the SQLAlchemy `breaker_state`
backing with a shared store (Redis hash, Foundry dataset, etc.) keyed on
`name`. The CircuitBreaker module talks to this layer through
`set_persistor` + `hydrate(...)` only — swap the implementation, no caller
changes required.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.data.db import breaker_state, get_engine

logger = logging.getLogger(__name__)


async def hydrate_all() -> dict[str, dict]:
    """Return every persisted breaker snapshot keyed by name."""
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(select(breaker_state))
        return {r["name"]: dict(r) for r in result.mappings().all()}


async def persist(name: str, snapshot: dict) -> None:
    """
    Upsert a breaker snapshot. Uses SQLite's ON CONFLICT — this is also
    valid syntax in PostgreSQL when the dialect detects it; for other
    backends the column-level update gives the same effect.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        # Portable upsert via DELETE+INSERT in one transaction. SQLAlchemy's
        # dialect-specific upsert isn't worth the branching for a single row.
        from sqlalchemy import delete

        await conn.execute(delete(breaker_state).where(breaker_state.c.name == name))
        await conn.execute(breaker_state.insert().values(name=name, **snapshot))
