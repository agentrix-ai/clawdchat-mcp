"""Consume ``dm:events`` (written by lxyd-ai/clawdchat) and push to subscribers."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Optional

from ..config import settings
from . import registry
from .delivery import PostFn, SleepFn, deliver_event
from .handlers import principal_active
from .store import CONSECUTIVE_FAIL_DROP, Subscription, SubscriptionStore

logger = logging.getLogger(__name__)


async def ensure_group(redis: Any, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _deliver_one(
    sub: Subscription,
    entry: dict,
    store: SubscriptionStore,
    token_store: Any,
    post: Optional[PostFn],
    sleep: Optional[SleepFn],
) -> str:
    if not registry.matches(sub.arguments, entry):
        return "filtered"
    if not principal_active(token_store, sub.agent_id, sub.client_id):
        await store.delete(sub.id, sub.agent_id)
        logger.info("MCP events sub=%s dropped: access revoked", sub.id)
        return "revoked"
    event = registry.build_event(entry, agent_name=sub.agent_name)
    if await store.already_sent(sub.id, event["eventId"]):
        return "duplicate"
    secrets = store.signing_secrets(sub)
    if not secrets:
        return "no_secret"
    outcome = await deliver_event(sub.url, event, secrets, sub.id, post=post, sleep=sleep)
    if outcome.ok:
        await store.mark_sent(sub.id, event["eventId"])
        return "delivered"
    if outcome.gone:  # our policy: 410 drops the subscription
        await store.delete(sub.id, sub.agent_id)
        return "gone"
    if outcome.dropped:
        return "dropped"
    if await store.record_failure(sub.id) >= CONSECUTIVE_FAIL_DROP:
        await store.delete(sub.id, sub.agent_id)
        logger.warning("MCP events sub=%s dropped after %d failures", sub.id, CONSECUTIVE_FAIL_DROP)
    return "failed"


async def process_entry(
    entry: dict,
    store: SubscriptionStore,
    token_store: Any,
    *,
    post: Optional[PostFn] = None,
    sleep: Optional[SleepFn] = None,
) -> list[str]:
    if entry.get("type") != registry.DM_EVENT or not entry.get("recipient_id"):
        return []
    subs = await store.for_agent(entry["recipient_id"])
    return list(
        await asyncio.gather(
            *(_deliver_one(s, entry, store, token_store, post, sleep) for s in subs)
        )
    )


async def run_consumer(redis: Any, store: SubscriptionStore, token_store: Any, stop: asyncio.Event) -> None:
    stream = settings.mcp_events_stream_key
    group = settings.mcp_events_instance
    consumer = f"{socket.gethostname()}-{group}"
    await ensure_group(redis, stream, group)
    last_id = "0"  # first drain our own pending (un-acked after a crash/restart)
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(group, consumer, {stream: last_id}, count=50, block=5000)
            entries = resp[0][1] if resp else []
            if last_id == "0" and not entries:
                last_id = ">"
                continue
            for entry_id, fields in entries:
                try:
                    await process_entry(fields, store, token_store)
                except Exception:
                    logger.exception("MCP events entry %s failed", entry_id)
                await redis.xack(stream, group, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("MCP events consumer error: %s", type(exc).__name__)
            await asyncio.sleep(2)
