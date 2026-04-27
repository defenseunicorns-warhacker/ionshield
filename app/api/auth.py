"""
IonShield API authentication.

Phase 1 — supports two credential paths:

  1. Authorization: Bearer iks_<token>   (preferred — DB-backed, per-tenant)
  2. X-API-Key: <legacy-shared-secret>   (kept for backward compat)

If `settings.api_key` is unset *and* no DB-backed keys exist, the API runs
unauthenticated (dev / open-demo mode). The moment either is configured,
auth is required for routes that depend on `verify_api_key`.

Resolved identity is attached to `request.state.tenant_id` /
`request.state.key_id` so downstream code (rate-limit key_func, audit log,
per-tenant filtering) can read it without re-parsing the header.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from slowapi.util import get_remote_address

from app.config import settings
from app.data import api_keys

logger = logging.getLogger(__name__)


def _legacy_key_match(request: Request) -> bool:
    if not settings.api_key:
        return False
    return request.headers.get("X-API-Key", "") == settings.api_key


def _bearer_token(request: Request) -> str:
    raw = request.headers.get("Authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


async def verify_api_key(request: Request) -> None:
    """
    Reject unauthenticated requests. Attaches identity to request.state on success.

    - Legacy single-key auth (X-API-Key matches settings.api_key) → tenant="legacy"
    - Bearer iks_<token> → tenant resolved from DB
    - Open-demo mode (no auth configured) → tenant="anonymous"
    """
    # Open-demo mode: only when no legacy key is set. (DB-backed keys may exist
    # but are optional in this mode — they identify a caller without blocking
    # anonymous access. Operators wanting strict auth set IONSHIELD_API_KEY.)
    if _legacy_key_match(request):
        request.state.tenant_id = "legacy"
        request.state.key_id = None
        return

    token = _bearer_token(request)
    if token:
        identity = await api_keys.lookup_key(token)
        if identity is None:
            logger.warning("Invalid bearer token from %s", get_remote_address(request))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.tenant_id = identity["tenant_id"]
        request.state.key_id = identity["id"]
        request.state.scopes = identity["scopes"]
        return

    if not settings.auth_enabled:
        # Open-demo mode — anonymous is OK
        request.state.tenant_id = "anonymous"
        request.state.key_id = None
        return

    logger.warning("Unauthorized request from %s", get_remote_address(request))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid credentials. Provide Authorization: Bearer iks_… or X-API-Key.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def tenant_or_ip_key(request: Request) -> str:
    """slowapi key_func: per-tenant rate limit when authenticated, IP otherwise."""
    tenant = getattr(request.state, "tenant_id", None) if hasattr(request, "state") else None
    if tenant and tenant != "anonymous":
        return f"tenant:{tenant}"
    return f"ip:{get_remote_address(request)}"


# Backward-compat: routes.py and routes_v2.py import `_auth` directly.
from fastapi import Depends  # noqa: E402

_auth = Depends(verify_api_key)
