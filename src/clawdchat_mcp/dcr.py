"""Resilient OAuth Dynamic Client Registration (RFC 7591).

The MCP SDK RegistrationHandler returns 500 when the body is empty, not JSON,
or omits required Pydantic fields such as redirect_uris. Directory clients
like coda-agent-runtime send partial metadata. Normalize those requests and
never leak an uncaught 500.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.settings import ClientRegistrationOptions

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://127.0.0.1/callback"
DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
DEFAULT_RESPONSE_TYPES = ["code"]
DEFAULT_TOKEN_AUTH_METHOD = "none"


def _is_safe_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def normalize_dcr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill missing RFC 7591 fields and coerce mildly non-standard shapes."""
    body = dict(payload)

    if not body.get("grant_types"):
        body["grant_types"] = list(DEFAULT_GRANT_TYPES)
    if not body.get("response_types"):
        body["response_types"] = list(DEFAULT_RESPONSE_TYPES)
    if not body.get("token_endpoint_auth_method"):
        body["token_endpoint_auth_method"] = DEFAULT_TOKEN_AUTH_METHOD

    uris = body.get("redirect_uris")
    if isinstance(uris, str):
        uris = [uris] if uris.strip() else []
    elif not isinstance(uris, list):
        uris = []

    cleaned: list[str] = []
    for item in uris:
        value = str(item or "").strip()
        if value and _is_safe_redirect_uri(value):
            cleaned.append(value)
    if not cleaned:
        cleaned = [DEFAULT_REDIRECT_URI]
    body["redirect_uris"] = cleaned
    return body


def _error(description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_client_metadata", "error_description": description},
        status_code=status_code,
    )


def make_dcr_handler(
    provider: OAuthAuthorizationServerProvider[Any, Any, Any],
    options: ClientRegistrationOptions | None = None,
):
    """Return a Starlette endpoint that never raises 500 for DCR requests."""
    inner = RegistrationHandler(provider, options or ClientRegistrationOptions())

    async def handle(request: Request) -> Response:
        try:
            raw = await request.body()
            if not raw or not raw.strip():
                payload: Any = {}
            else:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return _error("Request body must be valid JSON")
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                return _error("Request body must be a JSON object")

            normalized = normalize_dcr_payload(payload)
            request._body = json.dumps(normalized).encode("utf-8")
            if hasattr(request, "_json"):
                request._json = normalized
            return await inner.handle(request)
        except Exception as exc:
            logger.exception("Dynamic client registration failed")
            return _error(str(exc) or "Invalid client metadata")

    return handle
