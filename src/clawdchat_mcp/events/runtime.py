"""Wiring: Redis client, store singleton, capability patch, consumer lifespan."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional

from ..config import settings
from . import events_enabled
from .store import SubscriptionStore

logger = logging.getLogger(__name__)

_redis: Any = None
_store: Optional[SubscriptionStore] = None


def get_redis() -> Any:
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def get_store() -> SubscriptionStore:
    global _store
    if _store is None:
        _store = SubscriptionStore(get_redis())
    return _store


def patch_capabilities(lowlevel_server: Any) -> None:
    """Advertise top-level ``capabilities.events`` (what ChatGPT reads)."""
    original = lowlevel_server.get_capabilities

    def get_capabilities(*args: Any, **kwargs: Any):
        caps = original(*args, **kwargs)
        if not events_enabled():
            return caps
        return type(caps).model_validate({**caps.model_dump(exclude_none=True), "events": {}})

    lowlevel_server.get_capabilities = get_capabilities


def wrap_lifespan(app: Any, token_store: Any) -> None:
    from .consumer import run_consumer

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any):
        async with inner(a) as state:
            stop = asyncio.Event()
            task = asyncio.create_task(run_consumer(get_redis(), get_store(), token_store, stop))
            logger.info("MCP events consumer started (group=%s)", settings.mcp_events_instance)
            try:
                yield state
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    app.router.lifespan_context = lifespan
