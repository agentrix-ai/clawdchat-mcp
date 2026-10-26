"""Allow anonymous MCP directory probes while keeping tool calls OAuth-protected.

Directory crawlers POST initialize / tools/list without a Bearer token. Those
JSON-RPC methods are allowlisted. tools/call, prompts/get, resources/read and
any other method still require a valid Bearer token and return RFC 6750 401
with a Host-aware WWW-Authenticate resource_metadata URL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import Receive, Scope, Send

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, authorization_context
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

logger = logging.getLogger(__name__)

ANONYMOUS_DISCOVERY_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "ping",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }
)

DEFAULT_RESOURCE_METADATA = "https://mcp.clawdchat.ai/.well-known/oauth-protected-resource"


def resource_metadata_url(scope: Scope) -> str:
    """Build WWW-Authenticate resource_metadata from Host / X-Forwarded-*."""
    headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").strip()
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if not host:
        return DEFAULT_RESOURCE_METADATA
    hostname = host.split(":")[0].lower()
    if hostname in {"mcp.clawdchat.ai", "mcp.clawdchat.cn"}:
        return f"https://{hostname}/.well-known/oauth-protected-resource"
    if not proto:
        proto = "http" if hostname in {"localhost", "127.0.0.1", "::1"} else "https"
    return f"{proto}://{host}/.well-known/oauth-protected-resource"


def www_authenticate_header(scope: Scope) -> str:
    return f'Bearer resource_metadata="{resource_metadata_url(scope)}"'


def jsonrpc_methods(body: bytes) -> list[str] | None:
    """Extract JSON-RPC method names from a single object or a batch array."""
    if not body or not body.strip():
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        method = payload.get("method")
        return [method] if isinstance(method, str) else None
    if isinstance(payload, list) and payload:
        methods: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("method"), str):
                return None
            methods.append(item["method"])
        return methods
    return None


def is_anonymous_discovery(body: bytes) -> bool:
    methods = jsonrpc_methods(body)
    return bool(methods) and all(method in ANONYMOUS_DISCOVERY_METHODS for method in methods)


async def buffer_request_body(receive: Receive) -> bytes:
    """Read the full ASGI request body so it can be replayed downstream."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b"") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def replay_receive(body: bytes) -> Receive:
    """Return an ASGI receive callable that replays a buffered request body."""
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class DiscoveryAwareAuthMiddleware:
    """Require Bearer auth except for anonymous JSON-RPC discovery methods."""

    def __init__(
        self,
        app: Any,
        required_scopes: list[str] | None = None,
        session_manager: Any | None = None,
    ) -> None:
        self.app = app
        self.required_scopes = required_scopes or []
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        user = scope.get("user")
        if isinstance(user, AuthenticatedUser):
            auth_credentials = scope.get("auth")
            for required_scope in self.required_scopes:
                if auth_credentials is None or required_scope not in auth_credentials.scopes:
                    await self._send_unauthorized(scope, send)
                    return
            self._upgrade_anonymous_session(scope, user)
            await self.app(scope, receive, send)
            return

        if method != "POST":
            await self._send_unauthorized(scope, send)
            return

        body = await buffer_request_body(receive)
        if is_anonymous_discovery(body):
            await self.app(scope, replay_receive(body), send)
            return

        await self._send_unauthorized(scope, send)

    def _upgrade_anonymous_session(self, scope: Scope, user: AuthenticatedUser) -> None:
        """Bind a later Bearer token to a session that was opened anonymously."""
        manager = self.session_manager
        if manager is None:
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        session_id = headers.get(MCP_SESSION_ID_HEADER.lower())
        if not session_id:
            return
        instances = getattr(manager, "_server_instances", None)
        owners = getattr(manager, "_session_owners", None)
        if not isinstance(instances, dict) or not isinstance(owners, dict):
            return
        if session_id in instances and session_id not in owners:
            owners[session_id] = authorization_context(user)

    async def _send_unauthorized(self, scope: Scope, send: Send) -> None:
        body = {
            "error": "invalid_token",
            "error_description": "Authentication required",
        }
        body_bytes = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode("ascii")),
                    (b"www-authenticate", www_authenticate_header(scope).encode("latin-1")),
                    (b"access-control-allow-origin", b"*"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_bytes})
