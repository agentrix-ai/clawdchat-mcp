"""Subscription storage: Redis only, keys expire with the granted TTL.

Signing secrets are Fernet-encrypted with ``MCP_EVENTS_SECRET_KEY`` before they
reach Redis, so RDB/AOF snapshots never hold plaintext secrets.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings
from .signing import canonical_json

VERIFY_CACHE_SECONDS = 3600
SENT_DEDUPE_SECONDS = 7 * 24 * 3600
ROTATION_WINDOW_SECONDS = 600
CONSECUTIVE_FAIL_DROP = 20  # same as clawdchat WEBHOOK_CONSECUTIVE_FAIL_DISABLE


def subscription_id(agent_id: str, url: str, name: str, arguments: dict) -> str:
    raw = "\n".join([agent_id, url, name, canonical_json(arguments or {})])
    return "sub_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass
class Subscription:
    id: str
    agent_id: str
    agent_name: str
    client_id: str
    name: str
    arguments: dict
    url: str
    secret_enc: str
    expires_at: float
    prev_secret_enc: str = ""
    prev_until: float = 0.0
    created_at: float = field(default_factory=time.time)


class SubscriptionStore:
    def __init__(self, redis: Any, *, fernet_key: Optional[str] = None, prefix: Optional[str] = None):
        self.redis = redis
        self.fernet = Fernet((fernet_key or settings.mcp_events_secret_key).encode())
        self.prefix = prefix or f"mcp:ev:{settings.mcp_events_instance}:"

    # ---- secrets ----
    def encrypt(self, secret: str) -> str:
        return self.fernet.encrypt(secret.encode()).decode()

    def decrypt(self, token: str) -> Optional[str]:
        try:
            return self.fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None

    def signing_secrets(self, sub: Subscription, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        out = [s for s in [self.decrypt(sub.secret_enc)] if s]
        if sub.prev_secret_enc and sub.prev_until > now:
            prev = self.decrypt(sub.prev_secret_enc)
            if prev and prev not in out:
                out.append(prev)
        return out

    # ---- keys ----
    def _sub_key(self, sid: str) -> str:
        return f"{self.prefix}sub:{sid}"

    def _agent_key(self, agent_id: str) -> str:
        return f"{self.prefix}agent:{agent_id}"

    # ---- CRUD ----
    async def get(self, sid: str) -> Optional[Subscription]:
        raw = await self.redis.get(self._sub_key(sid))
        if not raw:
            return None
        sub = Subscription(**json.loads(raw))
        if sub.expires_at <= time.time():
            return None
        return sub

    async def put(self, sub: Subscription) -> None:
        ttl = max(1, int(sub.expires_at - time.time()) + 1)
        await self.redis.set(self._sub_key(sub.id), json.dumps(asdict(sub)), ex=ttl)
        await self.redis.sadd(self._agent_key(sub.agent_id), sub.id)

    async def delete(self, sid: str, agent_id: Optional[str] = None) -> None:
        if agent_id is None:
            sub = await self.get(sid)
            agent_id = sub.agent_id if sub else None
        await self.redis.delete(self._sub_key(sid), f"{self.prefix}fails:{sid}")
        if agent_id:
            await self.redis.srem(self._agent_key(agent_id), sid)

    async def for_agent(self, agent_id: str) -> list[Subscription]:
        ids = await self.redis.smembers(self._agent_key(agent_id))
        subs: list[Subscription] = []
        for sid in ids or []:
            sid = sid.decode() if isinstance(sid, bytes) else sid
            sub = await self.get(sid)
            if sub is None:
                await self.redis.srem(self._agent_key(agent_id), sid)  # expired
            else:
                subs.append(sub)
        return subs

    # ---- verification cache (principal + callback URL) ----
    async def is_verified(self, agent_id: str, url: str) -> bool:
        return bool(await self.redis.get(f"{self.prefix}verified:{_h(agent_id + chr(10) + url)}"))

    async def mark_verified(self, agent_id: str, url: str) -> None:
        await self.redis.set(
            f"{self.prefix}verified:{_h(agent_id + chr(10) + url)}", "1", ex=VERIFY_CACHE_SECONDS
        )

    # ---- delivery bookkeeping ----
    async def already_sent(self, sid: str, event_id: str) -> bool:
        return bool(await self.redis.exists(f"{self.prefix}sent:{sid}:{event_id}"))

    async def mark_sent(self, sid: str, event_id: str) -> None:
        await self.redis.set(f"{self.prefix}sent:{sid}:{event_id}", "1", ex=SENT_DEDUPE_SECONDS)
        await self.redis.delete(f"{self.prefix}fails:{sid}")

    async def record_failure(self, sid: str) -> int:
        key = f"{self.prefix}fails:{sid}"
        n = await self.redis.incr(key)
        await self.redis.expire(key, SENT_DEDUPE_SECONDS)
        return int(n)
