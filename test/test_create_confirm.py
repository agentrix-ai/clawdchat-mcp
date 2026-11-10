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
            assert forged.json()["error"] == "no_nonce"
            assert "verification parameter" in forged.json()["message"]
            page = await _owner_signs_in(http, ticket)
            assert page.status_code == 200 and "beta-bot" in page.text
            assert "Account: ac***@example.com" in page.text
            assert "Quota: 1/10" in page.text
            assert 'id="y"' in page.text
            ok = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
            )
            assert ok.status_code == 200 and ok.json()["success"] is True
            assert ok.json()["title"] == "Approved"
            assert ok.json()["account"] == "Account: ac***@example.com"
            assert ok.json()["quota"] == "Quota: 1/10"
            # 链接只能用一次 → 已使用，不是「过期」
            again = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
            )
            assert again.status_code == 410
            assert again.json()["error"] == "used"
            used_page = await http.get(f"/auth/confirm-create?t={ticket}")
            assert used_page.status_code == 410
            assert "already been used" in used_page.text
            assert "expired" not in used_page.text.lower()
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
            assert "has expired" in page.text
            assert "already been used" not in page.text
            assert "verification parameter" not in page.text
            cn = await http.get(
                "/auth/confirm-create?t=nope",
                headers={"host": "mcp.clawdchat.cn"},
            )
            assert cn.status_code == 400
            assert "已过期" in cn.text
            assert "已经用过" not in cn.text
            assert "缺少校验参数" not in cn.text

    _run_flow(backend, {"agent_id": "agent-a"}, calls)


def test_ttl_expired_ticket_is_expired_not_used(clean_store) -> None:  # noqa: F811
    from clawdchat_mcp.storage import CREATE_TICKET_TTL

    store.store_create_ticket(
        "old",
        {"access_token": "tok", "name": "x", "created_at": time.time() - CREATE_TICKET_TTL - 1},
    )
    assert store.get_create_ticket("old") is None


def test_cleanup_expired_drops_used_tickets_only_after_ttl(clean_store) -> None:  # noqa: F811
    from clawdchat_mcp.storage import CREATE_TICKET_TTL

    store.store_create_ticket("fresh-used", {"access_token": "tok", "name": "x"})
    assert store.consume_create_ticket("fresh-used") is not None
    assert store.get_create_ticket("fresh-used")["used"] is True
    store.cleanup_expired()
    still = store.get_create_ticket("fresh-used")
    assert still is not None and still.get("used") is True

    store.create_tickets["old-used"] = {
        "access_token": "tok",
        "name": "x",
        "used": True,
        "created_at": time.time() - CREATE_TICKET_TTL - 1,
    }
    store.create_tickets["old-unused"] = {
        "access_token": "tok",
        "name": "y",
        "created_at": time.time() - CREATE_TICKET_TTL - 1,
    }
    store.cleanup_expired()
    assert "old-used" not in store.create_tickets
    assert "old-unused" not in store.create_tickets
    assert store.get_create_ticket("old-used") is None
    assert store.get_create_ticket("fresh-used")["used"] is True


def test_confirm_errors_cn_en_distinct(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "user-1"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        headers = {"host": "mcp.clawdchat.cn"}
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            forged = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "approve": True},
                headers=headers,
            )
            assert forged.status_code == 403 and forged.json()["error"] == "no_nonce"
            assert "缺少校验参数" in forged.json()["message"]
            page = await http.get(
                "/auth/confirm-create/callback",
                params={"code": "c2", "state": ticket},
                headers=headers,
            )
            assert page.status_code == 200
            assert "账号：ac***@example.com" in page.text
            assert "名额：1/10" in page.text
            ok = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
                headers=headers,
            )
            assert ok.status_code == 200
            used = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
                headers=headers,
            )
            assert used.status_code == 410 and used.json()["error"] == "used"
            assert "已经用过" in used.json()["message"]

    _run_flow(backend, {"agent_id": "agent-a"}, calls)


def test_confirm_hides_approve_when_quota_full(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "user-1"])

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        backend.max_agents = 1  # 票发出后名额才满（#22 合入后满额不再签发 confirm_url）
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            page = await _owner_signs_in(http, ticket)
            assert page.status_code == 200
            assert "Quota: 1/1" in page.text
            assert "Account: ac***@example.com" in page.text
            assert 'id="y"' not in page.text
            assert 'id="n"' in page.text
            assert "limit is full" in page.text
            blocked = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"] == "full"
            assert blocked.json()["title"] == "Could not create: Agent limit is full"
            assert blocked.json()["title"] != "Declined"
            cn = await http.post(
                "/auth/confirm-create",
                json={"ticket": ticket, "nonce": _nonce(page.text), "approve": True},
                headers={"host": "mcp.clawdchat.cn"},
            )
            assert cn.status_code == 409
            assert cn.json()["title"] == "未能创建：名额已满"
            assert "已拒绝" not in cn.json()["title"]
            assert store.get_create_ticket(ticket) is not None
            assert store.get_create_ticket(ticket).get("used") is not True
        assert store.get_access_token(access).create_grants == 0

    _run_flow(backend, {"agent_id": "agent-a"}, calls)
    assert backend.created == []


def test_confirm_omits_account_quota_when_main_site_silent(clean_store) -> None:  # noqa: F811
    backend = _backend(["user-1", "user-1"])
    backend.include_quota = False
    backend.user_email = None

    async def calls(mcp, client_id, access, consent):
        res = await _ask_create(mcp, client_id, access)
        ticket = _ticket(res["confirm_url"])
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as http:
            page = await _owner_signs_in(http, ticket)
            assert "Account:" not in page.text
            assert "Quota:" not in page.text
            assert 'id="y"' in page.text

    _run_flow(backend, {"agent_id": "agent-a"}, calls)


def test_consent_page_hides_create_when_full(clean_store) -> None:  # noqa: F811
    from clawdchat_mcp.auth_provider import (
        _quota_view,
        claim_limit_payload,
        mask_account,
        quota_is_full,
        quota_snapshot,
        render_select_agent_html,
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
    assert mask_account("acctest@example.com") == "ac***@example.com"
    assert mask_account("a@x.com") == "a***@x.com"
    assert mask_account("not-an-email") == ""

    full_cn = render_select_agent_html(
        state="s",
        agents=[{
            "id": "1",
            "name": "e2e78-zero",
            "display_name": "零号e2e",
            "post_count": 0,
            "follower_count": 0,
            "karma": 0,
        }],
        is_cn=True,
        account_mode=True,
        can_create=False,
        quota_note="名额已满（2/2），只能选已有的 Agent。",
    )
    assert "零号e2e" in full_cn
    assert "@e2e78-zero" in full_cn
    assert "或新建一个" not in full_cn
    assert 'id="createNewBtn"' not in full_cn
    assert "cancelBtn" in full_cn and "取消" in full_cn

    full_en = render_select_agent_html(
        state="s",
        agents=[{"id": "1", "name": "alpha", "post_count": 0, "follower_count": 0, "karma": 0}],
        is_cn=False,
        account_mode=True,
        can_create=False,
        quota_note="Your limit is full (2/2); pick an existing Agent.",
    )
    assert "or create a new one" not in full_en
    assert 'id="createNewBtn"' not in full_en
    assert "Cancel" in full_en

    open_cn = render_select_agent_html(
        state="s",
        agents=[{"id": "1", "name": "alpha", "post_count": 0, "follower_count": 0, "karma": 0}],
        is_cn=True,
        account_mode=True,
        can_create=True,
    )
    assert "或新建一个" in open_cn
    assert "createNewBtn" in open_cn
