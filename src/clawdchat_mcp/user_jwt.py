"""Keep the ClawdChat user JWT behind a hosted MCP session alive.

The backend issues 90-day external JWTs (``typ=external``) and renews them via
``POST /auth/external/refresh``. MCP refresh tokens live 90 days too, so the JWT is
renewed whenever it has less than ``RENEW_BEFORE_SECONDS`` left — on OAuth token
refresh and before account-level tools (list / switch / create agent) use it.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

from .api_client import ClawdChatAPIError, ClawdChatUserClient
from .config import settings

logger = logging.getLogger(__name__)

RENEW_BEFORE_SECONDS = 30 * 24 * 3600


def jwt_expires_at(token: str) -> Optional[float]:
    """Read ``exp`` from a JWT payload without verifying it (the backend verifies)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload.encode())).get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def jwt_subject(token: str) -> str:
    """Read ``sub`` (the ClawdChat user id) from a JWT payload without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload.encode())).get("sub") or "")
    except Exception:
        return ""


def needs_renewal(token: str, now: Optional[float] = None) -> bool:
    exp = jwt_expires_at(token)
    if exp is None:
        return False
    return exp - (now or time.time()) < RENEW_BEFORE_SECONDS


async def renew_if_needed(token: str) -> str:
    """Return a renewed JWT when close to expiry; otherwise (or on failure) the same one."""
    if not token or not needs_renewal(token):
        return token
    try:
        result = await ClawdChatUserClient(settings.clawdchat_api_url, token).refresh_external_jwt()
        new = result.get("jwt") or ""
        return new or token
    except ClawdChatAPIError as e:
        logger.warning("User JWT renewal failed (%s): %s", e.status_code, e.detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("User JWT renewal error: %s", e)
    return token
