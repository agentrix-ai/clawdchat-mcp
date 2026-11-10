"""Hosted MCP: creating another Agent needs the owner's confirmation (2026-11-10).

Owner picked an existing Agent (or already used the one "create" grant) on the consent
page → switch_agent(create) only returns a confirm link. The owner opens it, signs in with
the same ClawdChat account and clicks Allow; only then can the session create one Agent.
The AI cannot complete the sign-in, so it cannot approve for the owner.
"""

from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from clawdchat_mcp.storage import store

from test_account_first import _as, _run_flow, clean_store  # noqa: F401  (fixture)
from test_p0_auth_confirm import FakeBackend, _tool_payload


def _jwt(sub: str) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'HS256'})}.{b64({'sub': sub, 'typ': 'external', 'exp': time.time() + 86400})}.sig"


def _backend(signed_in: list[str]) -> FakeBackend:
    """Each external sign-in returns the next user id in ``signed_in``."""
    backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}])
    queue = list(signed_in)

    async def exchange(code: str):
        backend.exchange_calls.append(code)
        return {"jwt": _jwt(queue.pop(0) if queue else "user-1"), "user": {}}

    backend.exchange_external_code = exchange
    return backend


async def _ask_create(mcp, client_id, access, name="beta-bot"):
    with _as(access, client_id):
        return _tool_payload(await mcp.call_tool("switch_agent", {"action": "create", "name": name}))


def _ticket(confirm_url: str) -> str:
    return parse_qs(urlparse(confirm_url).query)["t"][0]


async def _owner_signs_in(http: httpx.AsyncClient, ticket: str):
    page = await http.get(f"/auth/confirm-create?t={ticket}")
    assert page.status_code == 200 and "external/authorize" in page.text
    return await http.get("/auth/confirm-create/callback", params={"code": "c2", "state": ticket})


def _nonce(page_text: str) -> str:
    raw = re.search(r'data-body="([^"]+)"', page_text).group(1).replace("&quot;", '"')
    return json.loads(raw)["nonce"]


def test_bound_session_cannot_create_without_owner(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        assert res["success"] is False and res["needs_owner_confirmation"] is True
        assert "/auth/confirm-create?t=" in res["confirm_url"]

    _run_flow(backend, {"agent_id": "agent-a"}, calls)
    assert backend.created == []


def test_owner_confirms_then_one_create(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "user-1"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            # 发起方（AI）知道 ticket，但没登录拿不到 nonce，直接 POST 不算数
            forged = await http.post("/auth/confirm-create", json={"ticket": ticket, "approve": True})
            assert forged.status_code == 403
            page = await _owner_signs_in(http, ticket)
            assert page.status_code == 200 and "beta-bot" in page.text
            ok = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
            )
            assert ok.status_code == 200 and ok.json()["success"] is True
            # 链接只能用一次
            again = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
            )
            assert again.status_code == 403
        assert store.get_access_token(access).create_grants == 1
        created = await _ask_create(mcp, client_id, access)
        assert created["success"] is True and created["agent_name"] == "beta-bot"
        assert store.get_access_token(access).create_grants == 0
        third = await _ask_create(mcp, client_id, access, name="gamma-bot")
        assert third.get("needs_owner_confirmation") is True

    _run_flow(backend, {"agent_id": "agent-a"}, calls)
    assert backend.created == ["beta-bot"]


def test_other_account_cannot_confirm(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "someone-else"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            page = await _owner_signs_in(http, ticket)
            assert page.status_code == 403
        assert store.get_access_token(access).create_grants == 0

    _run_flow(backend, {"agent_id": "agent-a"}, calls)
    assert backend.created == []


def test_owner_declines(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "user-1"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            page = await _owner_signs_in(http, ticket)
            no = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": False},
            )
            assert no.status_code == 200 and no.json()["success"] is False
        assert store.get_access_token(access).create_grants == 0

    _run_flow(backend, {"agent_id": "agent-a"}, calls)


def test_expired_ticket(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1"])

    async def calls(mcp, client_id, access, consent):
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            page = await http.get("/auth/confirm-create?t=nope")
            assert page.status_code == 400

    _run_flow(backend, {"agent_id": "agent-a"}, calls)


def test_consent_page_hides_create_when_full(clean_store) -> None:  # noqa: F811
    from clawdchat_mcp.auth_provider import (
        _quota_view,
        claim_limit_payload,
        quota_is_full,
        quota_snapshot,
    )

    assert _quota_view({"claimed_count": 2, "max_agents": 2}, True)[0] is False
    assert "名额已满（2/2）" in _quota_view({"claimed_count": 2, "max_agents": 2}, True)[1]
    assert _quota_view({"claimed_count": 1, "max_agents": 2}, True) == (True, "")
    assert _quota_view({}, False) == (True, "")
    assert quota_is_full({"claimed_count": 1, "max_agents": 1}) is True
    assert quota_is_full({"claimed_count": 1, "max_agents": 2}) is False
    assert quota_is_full({}) is False
    assert quota_is_full({"claimed_count": "1", "max_agents": 1}) is False
    assert quota_snapshot({"claimed_count": 1, "max_agents": 3, "agents": [{}, {}]}) == {
        "claimed": 1, "max": 3, "total": 1,
    }
    assert quota_snapshot({"agents": [{"id": "a"}]}) == {"total": 1}
    payload = claim_limit_payload(
        {"claimed_count": 1, "max_agents": 1, "agents": [{"id": "a", "name": "Alpha"}]}
    )
    assert payload["reason"] == "claim_limit_reached"
    assert payload["claimed"] == 1 and payload["max"] == 1
    assert "confirm_url" not in payload
    assert "1/1" in payload["message"]
