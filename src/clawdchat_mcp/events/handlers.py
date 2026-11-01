"""JSON-RPC handlers for events/list, events/subscribe, events/unsubscribe.

The principal is always the agent bound to the Bearer token; arguments can
never name another agent, so a subscriber only ever receives its own DMs.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import settings
from . import registry, safety
from .delivery import PostFn, VerificationError, verify_callback
from .signing import InvalidSecret, decode_secret
from .store import ROTATION_WINDOW_SECONDS, Subscription, SubscriptionStore, subscription_id

logger = logging.getLogger(__name__)

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
CALLBACK_ENDPOINT_ERROR = -32015
EVENT_METHODS = frozenset({"events/list", "events/subscribe", "events/unsubscribe"})


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Optional[dict] = None):
        self.code, self.message, self.data = code, message, data
        super().__init__(message)


class Principal:
    def __init__(self, agent_id: str, agent_name: str, client_id: str):
        self.agent_id, self.agent_name, self.client_id = agent_id, agent_name, client_id


def principal_active(token_store: Any, agent_id: str, client_id: str) -> bool:
    """Access recheck: a live access token still binds this client to this agent."""
    now = time.time()
    for data in list(getattr(token_store, "access_tokens", {}).values()):
        if data.agent_id == agent_id and data.client_id == client_id:
            if not data.expires_at or data.expires_at > now:
                return True
    return False


def grant_ttl_ms(params: dict) -> int:
    """Omitted / null -> server default (we never grant unlimited); else clamp."""
    requested = params.get("ttlMs", None)
    if requested is None:
        return settings.mcp_events_default_ttl_ms
    if isinstance(requested, bool) or not isinstance(requested, (int, float)) or requested <= 0:
        raise RpcError(INVALID_PARAMS, "ttlMs must be a positive number or null")
    return int(min(max(requested, settings.mcp_events_min_ttl_ms), settings.mcp_events_max_ttl_ms))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_common(params: Any, *, need_secret: bool) -> tuple[str, dict, str, Optional[str]]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    name = params.get("name")
    if name not in registry.EVENTS:
        raise RpcError(INVALID_PARAMS, f"unknown event: {name}")
    try:
        arguments = registry.normalize_arguments(name, params.get("arguments"))
    except registry.InvalidArguments as exc:
        raise RpcError(INVALID_PARAMS, str(exc))
    delivery = params.get("delivery")
    if not isinstance(delivery, dict):
        raise RpcError(INVALID_PARAMS, "delivery is required")
    if delivery.get("mode", "webhook") != "webhook":
        raise RpcError(INVALID_PARAMS, "only webhook delivery is supported")
    url = delivery.get("url")
    if not isinstance(url, str):
        raise RpcError(INVALID_PARAMS, "delivery.url is required")
    try:
        safety.parse_webhook_url(url)
    except safety.UnsafeWebhookURL:
        raise RpcError(INVALID_PARAMS, "delivery.url must be a public https URL")
    secret = delivery.get("secret")
    if need_secret:
        try:
            decode_secret(secret)
        except InvalidSecret as exc:
            raise RpcError(INVALID_PARAMS, f"delivery.secret invalid: {exc}")
    return name, arguments, url, secret


async def handle(
    method: str,
    params: Any,
    principal: Principal,
    store: SubscriptionStore,
    *,
    post: Optional[PostFn] = None,
) -> dict:
    if method == "events/list":
        return registry.list_events((params or {}).get("cursor") if isinstance(params, dict) else None)

    if method == "events/subscribe":
        name, arguments, url, secret = _parse_common(params, need_secret=True)
        ttl_ms = grant_ttl_ms(params)
        sid = subscription_id(principal.agent_id, url, name, arguments)
        now = time.time()
        existing = await store.get(sid)
        if not await store.is_verified(principal.agent_id, url):
            try:
                await verify_callback(url, secret, sid, post=post)
            except VerificationError as exc:
                logger.warning("MCP events verification failed sub=%s reason=%s", sid, exc.reason)
                raise RpcError(CALLBACK_ENDPOINT_ERROR, "CallbackEndpointError", {"reason": exc.reason})
            await store.mark_verified(principal.agent_id, url)
        sub = Subscription(
            id=sid,
            agent_id=principal.agent_id,
            agent_name=principal.agent_name,
            client_id=principal.client_id,
            name=name,
            arguments=arguments,
            url=url,
            secret_enc=store.encrypt(secret),
            expires_at=now + ttl_ms / 1000.0,
        )
        if existing is not None:
            sub.created_at = existing.created_at
            old = store.decrypt(existing.secret_enc)
            if old and old != secret:  # rotation: dual-sign for a short window
                sub.prev_secret_enc = existing.secret_enc
                sub.prev_until = now + ROTATION_WINDOW_SECONDS
            elif existing.prev_until > now:
                sub.prev_secret_enc, sub.prev_until = existing.prev_secret_enc, existing.prev_until
        await store.put(sub)
        logger.info(
            "MCP events subscribe sub=%s agent=%s renew=%s ttl_ms=%d",
            sid, principal.agent_name, existing is not None, ttl_ms,
        )
        return {"id": sid, "refreshBefore": _iso(sub.expires_at), "cursor": None, "truncated": False}

    if method == "events/unsubscribe":
        name, arguments, url, _ = _parse_common(params, need_secret=False)
        sid = subscription_id(principal.agent_id, url, name, arguments)
        await store.delete(sid, principal.agent_id)  # idempotent, scoped to caller
        logger.info("MCP events unsubscribe sub=%s agent=%s", sid, principal.agent_name)
        return {}

    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


def rpc_response(req_id: Any, *, result: Any = None, error: Optional[RpcError] = None) -> bytes:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        err: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            err["data"] = error.data
        payload["error"] = err
    else:
        payload["result"] = result
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
