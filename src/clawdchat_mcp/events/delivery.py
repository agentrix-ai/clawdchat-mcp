"""Signed webhook POSTs: verification challenge and event delivery.

Parameters reuse lxyd-ai/clawdchat ``webhook_push`` where compatible
(5 attempts, 0.5/1/2/4s backoff, SSRF pinned-IP transport, no redirects,
masked logging, auto-drop after 20 consecutive failures). ChatGPT MCP Events
rules win where they differ: Standard Webhooks signature, 10s timeout,
256 KiB body cap, no retry on 410/413.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets as _secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from . import safety
from .signing import dump_body, sign_all

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
MAX_ATTEMPTS = 5
BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
NO_RETRY_STATUSES = frozenset({410, 413})
RESPONSE_READ_LIMIT = 4096
# Callback hosts behind Cloudflare (e.g. chatgpt.com) answer the default
# "python-httpx/x" User-Agent with a 403 bot challenge, so verification fails
# with reason=http_403. Always send an explicit product User-Agent.
USER_AGENT = "ClawdChat-MCP-Events/1.0 (+https://clawdchat.ai)"


@dataclass
class PostResult:
    status: Optional[int]  # None = network error / unsafe URL
    body: bytes = b""
    error: str = ""


PostFn = Callable[[str, bytes, dict], Awaitable[PostResult]]
SleepFn = Callable[[float], Awaitable[None]]


async def safe_post(
    url: str, body: bytes, headers: dict, *, inner_transport: Optional[httpx.AsyncBaseTransport] = None
) -> PostResult:
    """SSRF-checked POST to a pinned IP; never follows redirects; 10s budget."""
    try:
        resolved = await safety.resolve_webhook_url(url)
    except safety.UnsafeWebhookURL as exc:
        return PostResult(None, error=f"unsafe_url:{exc.reason}")
    transport = safety.PinnedIPAsyncTransport(
        hostname=resolved.hostname, ip=resolved.ip, inner=inner_transport
    )
    timeout = httpx.Timeout(
        safety.WEBHOOK_TOTAL_TIMEOUT_SECONDS, connect=safety.WEBHOOK_CONNECT_TIMEOUT_SECONDS
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:

            async def _send() -> PostResult:
                async with client.stream("POST", resolved.url, headers=headers, content=body) as resp:
                    data = await safety._read_limited_body(resp, RESPONSE_READ_LIMIT)
                    return PostResult(resp.status_code, data)

            return await asyncio.wait_for(_send(), timeout=safety.WEBHOOK_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return PostResult(None, error="timeout")
    except Exception as exc:
        return PostResult(None, error=type(exc).__name__)


def signed_headers(msg_id: str, body: bytes, secrets: list[str], sub_id: str) -> dict:
    ts = int(time.time())  # fresh timestamp + signature per attempt
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign_all(secrets, msg_id, ts, body),
        "X-MCP-Subscription-Id": sub_id,
    }


class VerificationError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def verify_callback(
    url: str, secret: str, sub_id: str, *, post: Optional[PostFn] = None
) -> None:
    """Send a signed single-use challenge; require 2xx + echoed challenge."""
    poster = post or safe_post
    challenge = _secrets.token_urlsafe(32)
    msg_id = f"msg_verification_{_secrets.token_hex(12)}"
    body = dump_body({"type": "verification", "challenge": challenge})
    result = await poster(url, body, signed_headers(msg_id, body, [secret], sub_id))
    if result.status is None:
        if result.error.startswith("unsafe_url"):
            raise VerificationError("invalid_url")
        raise VerificationError("timeout" if result.error == "timeout" else "unreachable")
    if not 200 <= result.status < 300:
        raise VerificationError(f"http_{result.status}")
    try:
        echoed = json.loads(result.body.decode("utf-8")).get("challenge")
    except Exception:
        raise VerificationError("challenge_failed")
    if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
        raise VerificationError("challenge_failed")


@dataclass
class DeliveryOutcome:
    ok: bool
    status: Optional[int]
    attempts: int
    gone: bool = False  # 410 -> caller drops the subscription
    dropped: bool = False  # 413 / oversize -> event discarded


async def deliver_event(
    url: str,
    event: dict,
    secrets: list[str],
    sub_id: str,
    *,
    post: Optional[PostFn] = None,
    sleep: Optional[SleepFn] = None,
) -> DeliveryOutcome:
    poster = post or safe_post
    sleeper = sleep or asyncio.sleep
    body = dump_body(event)
    event_id = event["eventId"]
    if len(body) > MAX_BODY_BYTES:
        logger.warning("MCP event %s dropped: body %d > 256KiB", event_id, len(body))
        return DeliveryOutcome(False, None, 0, dropped=True)
    status: Optional[int] = None
    for attempt in range(MAX_ATTEMPTS):
        result = await poster(url, body, signed_headers(event_id, body, secrets, sub_id))
        status = result.status
        if status is not None and 200 <= status < 300:
            logger.info("MCP event delivered event_id=%s sub=%s attempts=%d", event_id, sub_id, attempt + 1)
            return DeliveryOutcome(True, status, attempt + 1)
        if status in NO_RETRY_STATUSES:
            logger.warning("MCP event %s sub=%s got %s, not retrying", event_id, sub_id, status)
            return DeliveryOutcome(False, status, attempt + 1, gone=status == 410, dropped=status == 413)
        if result.error.startswith("unsafe_url"):
            break
        if attempt + 1 < MAX_ATTEMPTS:
            await sleeper(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
    logger.warning("MCP event failed event_id=%s sub=%s last_status=%s", event_id, sub_id, status)
    return DeliveryOutcome(False, status, MAX_ATTEMPTS)
