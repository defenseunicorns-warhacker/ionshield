"""
B4 caveat fixes — durable scenario-video store + URL validation.

Persists video registrations in SQL (survives ephemeral disks on free-tier
deploys) while keeping the per-scenario `video.json` sidecar as a
write-through cache so the existing precompute-dir consumers still work
unchanged.

URL validation: enforces `https://`, rejects schemes that allow client-side
script injection (`javascript:`, `data:`, etc.), and supports an optional
`IONSHIELD_VIDEO_DOMAIN_ALLOWLIST` env var (comma-separated host suffixes)
so an operator can lock the catalog to known CDN hosts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import delete, select

from app.config import settings
from app.data.db import get_engine, scenario_videos

logger = logging.getLogger(__name__)


# ── URL validation ──────────────────────────────────────────────────────────


class InvalidVideoURL(ValueError):
    """Raised when a registration URL fails the safety checks."""


def _allowed_domains() -> tuple[str, ...]:
    raw = getattr(settings, "video_domain_allowlist", "") or ""
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


def validate_video_url(url: str) -> str:
    """
    Reject obviously-unsafe video URLs before persisting them.

    Rules:
      - scheme must be `https` (or `http://localhost*` for local dev)
      - host must not be empty
      - if `IONSHIELD_VIDEO_DOMAIN_ALLOWLIST` is set, the host (or one
        of its parent suffixes) must match a configured entry
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidVideoURL("video_url must be a non-empty string")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("https", "http"):
        raise InvalidVideoURL(
            f"video_url scheme {parsed.scheme!r} is not http(s); "
            "embedded inline schemes (javascript:, data:) are rejected",
        )
    # http only allowed against localhost for local dev — never accept it
    # against external hosts because it can be intercepted/MITM'd.
    if parsed.scheme.lower() == "http":
        host = (parsed.hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise InvalidVideoURL(
                "http:// is only allowed for localhost; use https for "
                "external hosts",
            )
    if not parsed.hostname:
        raise InvalidVideoURL("video_url is missing a host")

    allowlist = _allowed_domains()
    if allowlist:
        host = parsed.hostname.lower()
        if not any(host == d or host.endswith("." + d) for d in allowlist):
            raise InvalidVideoURL(
                f"host {host!r} not in IONSHIELD_VIDEO_DOMAIN_ALLOWLIST "
                f"({', '.join(allowlist)})",
            )

    return url.strip()


# ── DB-backed registration ──────────────────────────────────────────────────


async def register(
    scenario_id: str,
    *,
    video_url: str,
    duration_seconds: float | None = None,
    rendered_at: datetime | None = None,
    notes: str = "",
) -> dict:
    """Upsert a scenario→video registration. Returns the persisted row."""
    safe_url = validate_video_url(video_url)
    rendered_at = rendered_at or datetime.now(timezone.utc)

    engine = get_engine()
    async with engine.begin() as conn:
        # Portable upsert: delete then insert. One row per scenario_id.
        await conn.execute(
            delete(scenario_videos).where(
                scenario_videos.c.scenario_id == scenario_id,
            )
        )
        await conn.execute(scenario_videos.insert().values(
            scenario_id=scenario_id,
            video_url=safe_url,
            duration_seconds=duration_seconds,
            rendered_at=rendered_at,
            notes=notes,
            created_at=datetime.now(timezone.utc),
        ))

    return {
        "scenario_id": scenario_id,
        "video_url": safe_url,
        "duration_seconds": duration_seconds,
        "rendered_at": rendered_at.isoformat() if rendered_at else None,
        "notes": notes,
    }


async def lookup(scenario_id: str) -> dict | None:
    engine = get_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(scenario_videos).where(
                scenario_videos.c.scenario_id == scenario_id,
            )
        )).mappings().first()
    if row is None:
        return None
    return {
        "scenario_id": row["scenario_id"],
        "video_url": row["video_url"],
        "duration_seconds": row["duration_seconds"],
        "rendered_at": (
            row["rendered_at"].isoformat()
            if hasattr(row["rendered_at"], "isoformat")
            else row["rendered_at"]
        ),
        "notes": row["notes"],
    }


async def lookup_all() -> dict[str, dict]:
    """Return a `{scenario_id: row}` map for the catalog merge step."""
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(select(scenario_videos))).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["scenario_id"]] = {
            "scenario_id": r["scenario_id"],
            "video_url": r["video_url"],
            "duration_seconds": r["duration_seconds"],
            "rendered_at": (
                r["rendered_at"].isoformat()
                if hasattr(r["rendered_at"], "isoformat")
                else r["rendered_at"]
            ),
            "notes": r["notes"],
        }
    return out


async def unregister(scenario_id: str) -> bool:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            delete(scenario_videos).where(
                scenario_videos.c.scenario_id == scenario_id,
            )
        )
    return (result.rowcount or 0) > 0
