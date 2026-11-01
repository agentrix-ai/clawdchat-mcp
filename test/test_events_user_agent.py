from clawdchat_mcp.events import delivery


def test_signed_headers_set_explicit_user_agent():
    h = delivery.signed_headers("msg_1", b"{}", ["whsec_" + "A" * 32], "sub_1")
    assert h["User-Agent"] == delivery.USER_AGENT
    assert not h["User-Agent"].lower().startswith("python-httpx")
    for k in ("webhook-id", "webhook-timestamp", "webhook-signature", "X-MCP-Subscription-Id"):
        assert k in h


import asyncio

import pytest

from clawdchat_mcp.events import consumer, runtime


def test_redis_socket_timeout_exceeds_xreadgroup_block():
    assert runtime.REDIS_SOCKET_TIMEOUT_SECONDS > 5.0


@pytest.mark.asyncio
async def test_consumer_redrains_pending_after_error(monkeypatch):
    calls = []
    stop = asyncio.Event()

    class R:
        async def xgroup_create(self, *a, **k):
            return True

        async def xreadgroup(self, group, name, streams, count, block):
            calls.append(list(streams.values())[0])
            if len(calls) == 2:
                raise TimeoutError()
            if len(calls) >= 4:
                stop.set()
            return []

        async def xack(self, *a):
            pass

    async def nosleep(_):
        pass

    monkeypatch.setattr(consumer.asyncio, "sleep", nosleep)
    monkeypatch.setattr(consumer, "ensure_group", lambda *a: asyncio.sleep(0))
    await consumer.run_consumer(R(), None, None, stop)
    assert calls[:4] == ["0", ">", "0", ">"]
