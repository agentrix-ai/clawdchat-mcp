"""ASGI layer inside the Bearer-auth middleware on /mcp: answers events/* itself.

Anything that is not a single JSON-RPC ``events/*`` request is replayed to the
SDK unchanged. Anonymous callers never reach here (events/* is not in the
anonymous discovery allowlist, so they get 401).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from starlette.types import Receive, Scope, Send

from ..anon_discovery import buffer_request_body, replay_receive
from .handlers import EVENT_METHODS, Principal, RpcError, handle, rpc_response

logger = logging.getLogger(__name__)


class EventsMiddleware:
    def __init__(self, app: Any, *, get_store: Callable[[], Any], token_store: Any):
        self.app = app
        self.get_store = get_store
        self.token_store = token_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (scope.get("method") or "").upper() != "POST":
            await self.app(scope, receive, send)
            return
        body = await buffer_request_body(receive)
        req = self._events_request(body)
        if req is None:
            await self.app(scope, replay_receive(body, receive), send)
            return
        req_id = req.get("id")
        try:
            principal = self._principal(scope)
            if principal is None:
                raise RpcError(-32001, "Authentication required: select an agent first")
            result = await handle(req["method"], req.get("params"), principal, self.get_store())
            out = rpc_response(req_id, result=result)
        except RpcError as exc:
            out = rpc_response(req_id, error=exc)
        except Exception:
            logger.exception("MCP events handler error")
            out = rpc_response(req_id, error=RpcError(-32603, "Internal error"))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(out)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": out})

    @staticmethod
    def _events_request(body: bytes) -> Optional[dict]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if isinstance(payload, dict) and payload.get("method") in EVENT_METHODS:
            return payload
        return None

    def _principal(self, scope: Scope) -> Optional[Principal]:
        user = scope.get("user")
        token = getattr(getattr(user, "access_token", None), "token", None)
        if not token:
            return None
        data = self.token_store.get_access_token(token)
        if not data or not data.agent_id:
            return None
        return Principal(data.agent_id, data.agent_name, data.client_id)
