"""Hosted MCP: when the bound Agent's key is rejected (401), re-read its current key once.

The key can be rotated outside this session (owner reset on the website, another channel).
Account-mode sessions hold the owner's JWT, so they re-read the Agent's *current* key and
rebind instead of failing until the user reconnects. They never reset a key.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from clawdchat_mcp import api_client as api_mod
from clawdchat_mcp import server as server_mod
from clawdchat_mcp.api_client import ClawdChatAgentClient, ClawdChatAPIError
from clawdchat_mcp.storage import AccessTokenData, store


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_mod.httpx, "AsyncClient", factory)


def _keyed_backend(valid_key: str, seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].split()[-1]
        seen.append(key)
        if key != valid_key:
            return httpx.Response(401, json={"detail": "无效的 API Key"})
        return httpx.Response(200, json={"success": True, "name": "alpha"})

    return handler


def test_retries_once_with_refreshed_key(monkeypatch) -> None:
    seen: list[str] = []
    _mock_http(monkeypatch, _keyed_backend("clawdchat_new", seen))
    calls = []

    async def refresh():
        calls.append(1)
        return "clawdchat_new"

    client = ClawdChatAgentClient("http://x", "clawdchat_old", on_invalid_key=refresh)
    result = asyncio.run(client.get_me())
    assert result["name"] == "alpha"
    assert seen == ["clawdchat_old", "clawdchat_new"]
    assert calls == [1] and client.api_key == "clawdchat_new"


def test_no_refresh_keeps_original_401(monkeypatch) -> None:
    seen: list[str] = []
    _mock_http(monkeypatch, _keyed_backend("clawdchat_other", seen))

    async def refresh():
        return None

    client = ClawdChatAgentClient("http://x", "clawdchat_old", on_invalid_key=refresh)
    with pytest.raises(ClawdChatAPIError) as e:
        asyncio.run(client.get_me())
    assert e.value.status_code == 401
    assert seen == ["clawdchat_old"]


def test_refresh_used_at_most_once(monkeypatch) -> None:
    seen: list[str] = []
    _mock_http(monkeypatch, _keyed_backend("clawdchat_never", seen))
    calls = []

    async def refresh():
        calls.append(1)
        return f"clawdchat_try{len(calls)}"

    client = ClawdChatAgentClient("http://x", "clawdchat_old", on_invalid_key=refresh)
    with pytest.raises(ClawdChatAPIError):
        asyncio.run(client.get_me())
    with pytest.raises(ClawdChatAPIError):
        asyncio.run(client.get_me())
    assert calls == [1]
    assert seen == ["clawdchat_old", "clawdchat_try1", "clawdchat_try1"]


def test_other_errors_do_not_refresh(monkeypatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(403, json={"detail": "该账号已被禁用"}))
    calls = []

    async def refresh():
        calls.append(1)
        return "clawdchat_new"

    client = ClawdChatAgentClient("http://x", "clawdchat_old", on_invalid_key=refresh)
    with pytest.raises(ClawdChatAPIError) as e:
        asyncio.run(client.get_me())
    assert e.value.status_code == 403 and calls == []


# ---------------------------------------------------------------- session wiring


@pytest.fixture
def bound_session():
    token = f"tok-{time.time_ns()}"
    store.access_tokens[token] = AccessTokenData(
        token=token,
        client_id="c",
        scopes=["agent"],
        expires_at=int(time.time()) + 3600,
        agent_api_key="clawdchat_old",
        agent_id="agent-a",
        agent_name="alpha",
        user_jwt="jwt-user",
    )
    yield token
    store.access_tokens.pop(token, None)


def _cred_client(monkeypatch, cred: dict, calls: list):
    class FakeUserClient:
        def __init__(self, base_url, jwt):
            self.jwt = jwt

        async def get_agent_credentials(self, agent_id):
            calls.append(("cred", agent_id, self.jwt))
            return cred

        async def reset_agent_key(self, agent_id):  # must never be called
            calls.append(("reset", agent_id))
            return {"api_key": "clawdchat_reset"}

    monkeypatch.setattr(server_mod, "ClawdChatUserClient", FakeUserClient)

    async def same(jwt):
        return jwt

    monkeypatch.setattr("clawdchat_mcp.user_jwt.renew_if_needed", same)


def _client_for(token, monkeypatch):
    monkeypatch.setattr(
        server_mod,
        "get_access_token",
        lambda: AccessToken(token=token, client_id="c", scopes=["agent"]),
    )
    return server_mod._get_agent_client()


def test_session_rebinds_to_current_key(monkeypatch, bound_session) -> None:
    calls: list = []
    _cred_client(monkeypatch, {"api_key": "clawdchat_new", "agent_name": "alpha"}, calls)
    seen: list[str] = []
    _mock_http(monkeypatch, _keyed_backend("clawdchat_new", seen))

    client = _client_for(bound_session, monkeypatch)
    assert asyncio.run(client.get_me())["name"] == "alpha"
    assert calls == [("cred", "agent-a", "jwt-user")]
    assert store.access_tokens[bound_session].agent_api_key == "clawdchat_new"
    # next client in the same session uses the stored new key directly
    assert _client_for(bound_session, monkeypatch).api_key == "clawdchat_new"


def test_session_never_resets_missing_key(monkeypatch, bound_session) -> None:
    calls: list = []
    _cred_client(monkeypatch, {"api_key": None, "agent_name": "alpha"}, calls)
    _mock_http(monkeypatch, _keyed_backend("clawdchat_whatever", []))

    client = _client_for(bound_session, monkeypatch)
    with pytest.raises(ClawdChatAPIError):
        asyncio.run(client.get_me())
    assert [c[0] for c in calls] == ["cred"]  # no reset
    assert store.access_tokens[bound_session].agent_api_key == "clawdchat_old"


def test_api_key_only_session_has_no_refresh(monkeypatch, bound_session) -> None:
    store.access_tokens[bound_session].user_jwt = ""
    client = _client_for(bound_session, monkeypatch)
    assert client._on_invalid_key is None
