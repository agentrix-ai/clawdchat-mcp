"""MCP Events v1 (offline): signing, subscribe lifecycle, verification, delivery, consumer.

Uses fakeredis and an in-process fake ChatGPT webhook receiver (httpx.MockTransport
behind the real SSRF-pinned transport). No network, no ClawdChat accounts needed.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
from cryptography.fernet import Fernet

from clawdchat_mcp.config import settings
from clawdchat_mcp.events import delivery, handlers, registry, safety, signing
from clawdchat_mcp.events.consumer import ensure_group, process_entry
from clawdchat_mcp.events.handlers import Principal, RpcError, handle
from clawdchat_mcp.events.middleware import EventsMiddleware
from clawdchat_mcp.events.store import SubscriptionStore, subscription_id

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
SECRET2 = "whsec_" + base64.b64encode(b"z" * 32).decode()
URL = "https://receiver.example.com/mcp-events/cb_1"


class FakeReceiver:
    """Behaves like ChatGPT's callback endpoint."""

    def __init__(self, secret=SECRET, statuses=None, echo=True):
        self.secret, self.statuses, self.echo = secret, list(statuses or []), echo
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.content
        h = request.headers
        assert h["content-type"] == "application/json"
        assert signing.verify(
            self.secret, h["webhook-id"], int(h["webhook-timestamp"]), body, h["webhook-signature"]
        ), "bad signature"
        assert h["x-mcp-subscription-id"].startswith("sub_")
        payload = json.loads(body)
        if payload.get("type") == "verification":
            assert h["webhook-id"].startswith("msg_verification_")
            return httpx.Response(200, json={"challenge": payload["challenge"] if self.echo else "nope"})
        assert h["webhook-id"] == payload["eventId"]
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status)

    def post(self):
        transport = httpx.MockTransport(self)

        async def _post(url, body, headers):
            return await delivery.safe_post(url, body, headers, inner_transport=transport)

        return _post


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def fake_resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(safety, "resolve_ips", fake_resolve)


@pytest.fixture
def store():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    return SubscriptionStore(r, fernet_key=Fernet.generate_key().decode(), prefix="t:")


@pytest.fixture
def principal():
    return Principal("agent-1", "babylon", "client-1")


@pytest.fixture
def tokens():
    return SimpleNamespace(
        access_tokens={
            "t": SimpleNamespace(agent_id="agent-1", agent_name="babylon", client_id="client-1", expires_at=None)
        }
    )


def sub_params(secret=SECRET, args=None, **extra):
    return {
        "name": registry.DM_EVENT,
        "arguments": args if args is not None else {},
        "delivery": {"mode": "webhook", "url": URL, "secret": secret},
        "cursor": None,
        **extra,
    }


def entry(**kw):
    base = {
        "v": "1",
        "type": "dm.message_received",
        "recipient_id": "agent-1",
        "conversation_id": "conv-1",
        "message_id": "m-1",
        "sender_name": "Alice",
        "created_at": "2026-11-01T21:00:00Z",
        "preview": "hi there",
    }
    base.update(kw)
    return base


async def nosleep(_):
    return None


# ---------------- signing ----------------
def test_standard_webhooks_signature_known_vector():
    # Reference vector from standardwebhooks test-suite
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    sig = signing.sign(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, b'{"test": 2432232314}')
    assert sig == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


@pytest.mark.parametrize("bad", ["nope", "whsec_!!!", "whsec_" + base64.b64encode(b"x" * 10).decode(),
                                 "whsec_" + base64.b64encode(b"x" * 65).decode()])
def test_secret_validation(bad):
    with pytest.raises(signing.InvalidSecret):
        signing.decode_secret(bad)


def test_dual_signature_rotation():
    body = b"{}"
    header = signing.sign_all([SECRET2, SECRET], "evt", 1, body)
    assert " " in header
    assert signing.verify(SECRET, "evt", 1, body, header)
    assert signing.verify(SECRET2, "evt", 1, body, header)


# ---------------- list / capability ----------------
@pytest.mark.asyncio
async def test_events_list(store, principal):
    res = await handle("events/list", {}, principal, store)
    (ev,) = res["events"]
    assert ev["name"] == "dm.message_received"
    assert ev["delivery"] == ["webhook"]
    assert ev["inputSchema"]["additionalProperties"] is False
    assert "agent_id" not in ev["inputSchema"]["properties"]


def test_capability_events_advertised(monkeypatch):
    from clawdchat_mcp.events import runtime

    class Caps:
        def __init__(self, **kw):
            self.kw = kw

        def model_dump(self, exclude_none=True):
            return dict(self.kw)

        @classmethod
        def model_validate(cls, d):
            return cls(**d)

    server = SimpleNamespace(get_capabilities=lambda *a, **k: Caps(tools={}))
    runtime.patch_capabilities(server)
    monkeypatch.setattr(settings, "mcp_events_enabled", False)
    assert "events" not in server.get_capabilities().kw
    monkeypatch.setattr(settings, "mcp_events_enabled", True)
    monkeypatch.setattr(settings, "mcp_events_secret_key", "x")
    assert server.get_capabilities().kw["events"] == {}


def test_capability_real_sdk_model():
    from mcp.types import ServerCapabilities

    caps = ServerCapabilities.model_validate({"tools": {}, "events": {}})
    assert caps.model_dump(exclude_none=True)["events"] == {}


# ---------------- subscribe ----------------
@pytest.mark.asyncio
async def test_subscribe_verifies_and_is_idempotent(store, principal):
    rx = FakeReceiver()
    r1 = await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    assert r1["id"].startswith("sub_") and r1["cursor"] is None and r1["truncated"] is False
    assert r1["refreshBefore"].endswith("Z")
    assert len(rx.requests) == 1  # verification challenge
    # renewal with reordered/equal args -> same id, no second challenge (cache)
    r2 = await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    assert r2["id"] == r1["id"] and len(rx.requests) == 1
    assert len(await store.for_agent("agent-1")) == 1


@pytest.mark.asyncio
async def test_subscription_id_canonical_args():
    a = subscription_id("a", URL, "e", {"x": "1", "y": "2"})
    b = subscription_id("a", URL, "e", {"y": "2", "x": "1"})
    assert a == b
    assert a != subscription_id("other-agent", URL, "e", {"x": "1", "y": "2"})


@pytest.mark.asyncio
async def test_secret_stored_encrypted(store, principal):
    res = await handle("events/subscribe", sub_params(), principal, store, post=FakeReceiver().post())
    raw = await store.redis.get(f"t:sub:{res['id']}")
    assert SECRET not in raw and SECRET[6:] not in raw
    ttl = await store.redis.ttl(f"t:sub:{res['id']}")
    assert 0 < ttl <= 24 * 3600 + 1


@pytest.mark.asyncio
async def test_ttl_rules(store, principal, monkeypatch):
    rx = FakeReceiver().post()
    now = time.time()
    r = await handle("events/subscribe", sub_params(ttlMs=120_000), principal, store, post=rx)
    sub = await store.get(r["id"])
    assert sub.expires_at - now == pytest.approx(120, abs=3)
    r = await handle("events/subscribe", sub_params(ttlMs=1), principal, store, post=rx)  # min clamp
    assert (await store.get(r["id"])).expires_at - now == pytest.approx(60, abs=3)
    r = await handle("events/subscribe", sub_params(ttlMs=None), principal, store, post=rx)  # no unlimited
    assert r["refreshBefore"] is not None
    with pytest.raises(RpcError) as e:
        await handle("events/subscribe", sub_params(ttlMs=-5), principal, store, post=rx)
    assert e.value.code == -32602


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        sub_params(secret="bad"),
        sub_params(args={"agent_id": "someone-else"}),
        {**sub_params(), "name": "nope"},
        {**sub_params(), "delivery": {"mode": "webhook", "url": "http://x.example.com/", "secret": SECRET}},
        {**sub_params(), "delivery": {"mode": "poll", "url": URL, "secret": SECRET}},
    ],
)
async def test_subscribe_invalid_params(store, principal, params):
    with pytest.raises(RpcError) as e:
        await handle("events/subscribe", params, principal, store, post=FakeReceiver().post())
    assert e.value.code == -32602


@pytest.mark.asyncio
async def test_verification_failure_returns_32015(store, principal):
    with pytest.raises(RpcError) as e:
        await handle("events/subscribe", sub_params(), principal, store, post=FakeReceiver(echo=False).post())
    assert e.value.code == -32015 and e.value.data == {"reason": "challenge_failed"}
    assert await store.for_agent("agent-1") == []


@pytest.mark.asyncio
async def test_verification_private_ip_blocked(store, principal, monkeypatch):
    async def private(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(safety, "resolve_ips", private)
    with pytest.raises(RpcError) as e:
        await handle("events/subscribe", sub_params(), principal, store, post=FakeReceiver().post())
    assert e.value.code == -32015 and e.value.data["reason"] == "invalid_url"


@pytest.mark.asyncio
async def test_redirect_not_followed():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    res = await delivery.safe_post(URL, b"{}", {}, inner_transport=httpx.MockTransport(handler))
    assert res.status == 302 and len(calls) == 1


@pytest.mark.asyncio
async def test_unsubscribe_idempotent_and_scoped(store, principal):
    res = await handle("events/subscribe", sub_params(), principal, store, post=FakeReceiver().post())
    other = Principal("agent-2", "mallory", "client-9")
    unsub = {"name": registry.DM_EVENT, "arguments": {}, "delivery": {"mode": "webhook", "url": URL}}
    assert await handle("events/unsubscribe", unsub, other, store) == {}
    assert await store.get(res["id"]) is not None  # other principal can't remove it
    assert await handle("events/unsubscribe", unsub, principal, store) == {}
    assert await handle("events/unsubscribe", unsub, principal, store) == {}
    assert await store.get(res["id"]) is None


# ---------------- delivery ----------------
@pytest.mark.asyncio
async def test_delivery_envelope_and_headers(store, principal, tokens):
    rx = FakeReceiver()
    await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    out = await process_entry(entry(), store, tokens, post=rx.post(), sleep=nosleep)
    assert out == ["delivered"]
    req = rx.requests[-1]
    body = json.loads(req.content)
    assert body["eventId"] == req.headers["webhook-id"] == "evt_dm_m-1"
    assert body["name"] == "dm.message_received" and body["cursor"] is None
    assert set(body["data"]) == set(registry.DM_EVENT_DEFINITION["payloadSchema"]["required"])
    assert body["data"]["agent"] == "babylon" and body["data"]["from"] == "Alice"
    # duplicate stream entry -> not re-sent
    assert await process_entry(entry(), store, tokens, post=rx.post(), sleep=nosleep) == ["duplicate"]


@pytest.mark.asyncio
async def test_only_own_dms_and_filters(store, principal, tokens):
    rx = FakeReceiver()
    await handle("events/subscribe", sub_params(args={"from_agent": "alice"}), principal, store, post=rx.post())
    assert await process_entry(entry(recipient_id="agent-2"), store, tokens, post=rx.post()) == []
    assert await process_entry(entry(sender_name="bob"), store, tokens, post=rx.post()) == ["filtered"]
    assert await process_entry(entry(sender_name="ALICE", message_id="m-2"), store, tokens,
                               post=rx.post(), sleep=nosleep) == ["delivered"]


@pytest.mark.asyncio
async def test_retry_backoff_then_success_fresh_signature(store, principal, tokens):
    rx = FakeReceiver(statuses=[500, 503])
    await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    sleeps = []

    async def rec(s):
        sleeps.append(s)

    assert await process_entry(entry(), store, tokens, post=rx.post(), sleep=rec) == ["delivered"]
    assert sleeps == [0.5, 1.0]
    ids = {r.headers["webhook-id"] for r in rx.requests[1:]}
    assert ids == {"evt_dm_m-1"}


@pytest.mark.asyncio
async def test_bounded_attempts(store, principal, tokens):
    rx = FakeReceiver(statuses=[500] * 10)
    await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    assert await process_entry(entry(), store, tokens, post=rx.post(), sleep=nosleep) == ["failed"]
    assert len(rx.requests) - 1 == delivery.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_410_no_retry_and_drops_subscription(store, principal, tokens):
    rx = FakeReceiver(statuses=[410])
    res = await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    assert await process_entry(entry(), store, tokens, post=rx.post(), sleep=nosleep) == ["gone"]
    assert len(rx.requests) == 2 and await store.get(res["id"]) is None


@pytest.mark.asyncio
async def test_413_no_retry_keeps_subscription(store, principal, tokens):
    rx = FakeReceiver(statuses=[413])
    res = await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    assert await process_entry(entry(), store, tokens, post=rx.post(), sleep=nosleep) == ["dropped"]
    assert len(rx.requests) == 2 and await store.get(res["id"]) is not None


@pytest.mark.asyncio
async def test_body_cap_256kib():
    called = []

    async def post(*a):
        called.append(a)

    ev = registry.build_event(entry(), agent_name="b")
    ev["data"]["preview"] = "x" * (256 * 1024)
    out = await delivery.deliver_event(URL, ev, [SECRET], "sub_x", post=post)
    assert out.dropped and not called


@pytest.mark.asyncio
async def test_revoked_access_stops_delivery(store, principal):
    rx = FakeReceiver()
    res = await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    no_tokens = SimpleNamespace(access_tokens={})
    assert await process_entry(entry(), store, no_tokens, post=rx.post()) == ["revoked"]
    assert await store.get(res["id"]) is None and len(rx.requests) == 1


@pytest.mark.asyncio
async def test_secret_rotation_dual_signs(store, principal, tokens):
    await handle("events/subscribe", sub_params(), principal, store, post=FakeReceiver().post())
    await handle("events/subscribe", sub_params(secret=SECRET2), principal, store, post=FakeReceiver(SECRET2).post())
    old_rx, new_rx = FakeReceiver(SECRET), FakeReceiver(SECRET2)
    assert await process_entry(entry(), store, tokens, post=old_rx.post(), sleep=nosleep) == ["delivered"]
    assert await process_entry(entry(message_id="m-9"), store, tokens, post=new_rx.post(),
                               sleep=nosleep) == ["delivered"]
    assert len(old_rx.requests[0].headers["webhook-signature"].split()) == 2


@pytest.mark.asyncio
async def test_preview_truncated_in_payload():
    ev = registry.build_event(entry(preview="y" * 2000), agent_name="b")
    assert len(ev["data"]["preview"]) == registry.PREVIEW_MAX_CHARS + 1


@pytest.mark.asyncio
async def test_consumer_group_reads_stream(store, principal, tokens):
    rx = FakeReceiver()
    await handle("events/subscribe", sub_params(), principal, store, post=rx.post())
    r = store.redis
    await ensure_group(r, "dm:events", "g")
    await ensure_group(r, "dm:events", "g")  # BUSYGROUP tolerated
    await r.xadd("dm:events", entry())
    resp = await r.xreadgroup("g", "c", {"dm:events": ">"}, count=10)
    (eid, fields), = resp[0][1]
    assert await process_entry(fields, store, tokens, post=rx.post(), sleep=nosleep) == ["delivered"]
    await r.xack("dm:events", "g", eid)
    pending = await r.xpending("dm:events", "g")
    assert pending["pending"] == 0


# ---------------- middleware (same authenticated /mcp endpoint) ----------------
@pytest.mark.asyncio
async def test_middleware_routes_events_and_passes_through(store, tokens):
    hits = []

    async def sdk_app(scope, receive, send):
        hits.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"sdk"})

    tokens.get_access_token = lambda t: tokens.access_tokens.get(t)
    mw = EventsMiddleware(sdk_app, get_store=lambda: store, token_store=tokens)

    async def app(scope, receive, send):
        scope["user"] = SimpleNamespace(access_token=SimpleNamespace(token="t"))
        await mw(scope, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "events/list"})
        assert r.json()["id"] == 7 and r.json()["result"]["events"][0]["name"] == "dm.message_received"
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
        assert r.text == "sdk" and hits == ["/mcp"]


def test_events_methods_not_anonymous():
    from clawdchat_mcp.anon_discovery import is_anonymous_discovery

    assert not is_anonymous_discovery(b'{"jsonrpc":"2.0","id":1,"method":"events/subscribe"}')


# ---------------- full app smoke (flag on / off) ----------------
@pytest.mark.parametrize("enabled", [True, False])
def test_initialize_advertises_events_only_when_enabled(monkeypatch, enabled):
    from starlette.testclient import TestClient

    from clawdchat_mcp.server import create_mcp_server

    monkeypatch.setattr(settings, "mcp_events_enabled", enabled)
    monkeypatch.setattr(settings, "mcp_events_secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    app = create_mcp_server("streamable-http").streamable_http_app()
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json",
         "host": "localhost:8347"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    with TestClient(app) as c:
        r = c.post("/mcp", headers=h, json=init)
        line = next(x for x in r.text.splitlines() if x.startswith("data:"))
        caps = json.loads(line[5:])["result"]["capabilities"]
        assert ("events" in caps) is enabled
        r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "events/list"})
        assert r.status_code == 401  # same OAuth-protected endpoint as tools
