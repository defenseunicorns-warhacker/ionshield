"""
IonShield API key authentication dependency.

Extracted from routes.py so routes_v2.py can import it without creating a
circular dependency between the two route modules.
"""

import logging

from fastapi import Depends, HTTPException, Request, status
from slowapi.util import get_remote_address

from app.config import settings

logger = logging.getLogger(__name__)


def verify_api_key(request: Request) -> None:
    """Reject requests missing a valid X-API-Key header when auth is enabled."""
    if not settings.auth_enabled:
        return
    provided = request.headers.get("X-API-Key", "")
    if provided != settings.api_key:
        logger.warning("Unauthorized request from %s", get_remote_address(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


_auth = Depends(verify_api_key)
