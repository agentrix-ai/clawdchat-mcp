"""Hosted MCP account-first flow (offline, fake ClawdChat backend).

Sign in with an account → consent page → session bound to the user; Agents are
bound / switched / created afterwards with switch_agent. Also: user JWT renewal,
bindings surviving token refresh, and secrets encrypted at rest.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from clawdchat_mcp import server as server_mod
from clawdchat_mcp.config import settings as config_settings
from clawdchat_mcp.server import NO_AGENT_BOUND_MESSAGE, _get_agent_client, create_mcp_server
from clawdchat_mcp.storage import AccessTokenData, RefreshTokenData, TokenStore, store
from clawdchat_mcp.user_jwt import jwt_expires_at, needs_renewal

from test_p0_auth_confirm import (  # noqa: E402  (shared fakes / helpers)
    FakeBackend,
    _patch_user_client,
    _query,
    _register_and_start_login,
    _tool_payload,
)

REDIRECT = "http://localhost:9876/callback"


def _jwt(exp: float) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'HS256'})}.{b64({'sub': 'user-1', 'typ': 'external', 'exp': exp})}.sig"


@pytest.fixture
def clean_store():
    store.pending_logins.clear()
    store.auth_codes.clear()
    yield
    store.pending_logins.clear()


async def _login(client: httpx.AsyncClient, body: dict[str, Any]) -> tuple[str, str, httpx.Response]:
    """Run the hosted login up to the consent POST; return (client_id, access_token, consent_resp)."""
    client_id, verifier, login_url = await _register_and_start_login(client)
    state = _query(login_url)["state"]
    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
    consent = await client.post("/auth/select-agent", json={"state": state, **body})
    if "redirect" not in consent.json():
        return client_id, "", consent
    code = _query(consent.json()["redirect"])["code"]
    tok = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return client_id, tok.json()["access_token"], consent


def _as(access_token: str, client_id: str):
    return patch.object(
        server_mod,
        "get_access_token",
        lambda: AccessToken(token=access_token, client_id=client_id, scopes=["agent"]),
    )


def _run_flow(backend: FakeBackend, body: dict[str, Any], tool_calls):
    """Login with ``body`` at the consent step, then run ``tool_calls(mcp, client_id, token)``."""

    async def _go():
        mcp = create_mcp_server(transport="streamable-http")
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        p_auth, p_server = _patch_user_client(backend)
        with p_auth, p_server:
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                client_id, access, consent = await _login(client, body)
            return await tool_calls(mcp, client_id, access, consent)

    return asyncio.run(_go())


# ---------------------------------------------------------------- consent / unbound session


def test_create_new_issues_unbound_session_with_one_grant(clean_store) -> None:
    backend = FakeBackend([])

    async def calls(mcp, client_id, access, consent):
        token = store.get_access_token(access)
        assert token is not None and token.user_jwt and not token.agent_api_key
        assert token.create_grants == 1  # 主人在授权页上选了「新建」
        with _as(access, client_id):
            with pytest.raises(ValueError) as exc:
                _get_agent_client()
            assert "switch_agent" in str(exc.value)
            current = _tool_payload(await mcp.call_tool("list_my_agents", {"action": "current"}))
            assert current.get("bound") is False
            listed = _tool_payload(await mcp.call_tool("list_my_agents", {"action": "list"}))
            assert listed.get("agents") == [] and "create" in listed.get("hint", "")
            posted = _tool_payload(await mcp.call_tool("create_post", {"title": "t", "content": "c"}))
            assert NO_AGENT_BOUND_MESSAGE.split(".")[0] in json.dumps(posted)

    _run_flow(backend, {"create_new": True}, calls)
    assert backend.created == [] and backend.reset_calls == []
    assert backend.auto_create_flags and not any(backend.auto_create_flags)


def test_create_new_must_be_true_bool_and_bind_later_gone(clean_store) -> None:
    for body in ({"create_new": "true"}, {"bind_later": True}):
        backend = FakeBackend([])

        async def calls(mcp, client_id, access, consent):
            assert access == ""
            assert consent.status_code == 400

        _run_flow(backend, body, calls)
        assert store.auth_codes == {}


# ---------------------------------------------------------------- create


def test_create_binds_new_agent(clean_store) -> None:
    backend = FakeBackend([])

    async def calls(mcp, client_id, access, consent):
        with _as(access, client_id):
            created = _tool_payload(await mcp.call_tool(
                "switch_agent",
                {"action": "create", "name": "claude-neo", "display_name": "Claude-Neo"},
            ))
            assert created.get("success") is True and created["agent_name"] == "claude-neo"
            assert "api_key" not in json.dumps(created)  # never hand the key to the model
            assert _get_agent_client().api_key == "clawdchat_new_claude-neo"
            listed = _tool_payload(await mcp.call_tool("list_my_agents", {"action": "list"}))
            assert [a["is_current"] for a in listed["agents"]] == [True]
            assert store.get_access_token(access).create_grants == 0
            # 一次「新建」用完了：再建要主人确认
            again = _tool_payload(await mcp.call_tool(
                "switch_agent", {"action": "create", "name": "claude-two"}
            ))
            assert again.get("needs_owner_confirmation") is True
            assert "/auth/confirm-create?t=" in again["confirm_url"]

    _run_flow(backend, {"create_new": True}, calls)
    assert backend.created == ["claude-neo"]


def test_create_at_limit_keeps_binding_and_lists_existing(clean_store) -> None:
    backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}])
    backend.max_agents = 1

    async def calls(mcp, client_id, access, consent):
        store.change_create_grants(access, +1)  # 主人已确认过新建
        with _as(access, client_id):
            res = _tool_payload(await mcp.call_tool(
                "switch_agent", {"action": "create", "name": "second"}
            ))
            assert res.get("reason") == "claim_limit_reached"
            assert [a["name"] for a in res["existing_agents"]] == ["Alpha"]
            assert _get_agent_client().api_key == "clawdchat_a"

    _run_flow(backend, {"agent_id": "agent-a"}, calls)
    assert backend.created == []


def test_create_requires_name(clean_store) -> None:
    backend = FakeBackend([])

    async def calls(mcp, client_id, access, consent):
        with _as(access, client_id):
            res = _tool_payload(await mcp.call_tool("switch_agent", {"action": "create"}))
            assert "name is required" in json.dumps(res)

    _run_flow(backend, {"create_new": True}, calls)


def test_api_key_session_cannot_create(clean_store) -> None:
    store.access_tokens["tok-key-only"] = AccessTokenData(
        token="tok-key-only", client_id="c", scopes=["agent"], expires_at=None,
        agent_api_key="clawdchat_x", agent_id="agent-x", agent_name="X", user_jwt="",
    )
    mcp = create_mcp_server(transport="streamable-http")
    try:
        with _as("tok-key-only", "c"):
            res = _tool_payload(asyncio.run(mcp.call_tool(
                "switch_agent", {"action": "create", "name": "nope"}
            )))
        assert "account" in json.dumps(res).lower()
    finally:
        store.access_tokens.pop("tok-key-only", None)


def test_switch_from_unbound_session(clean_store) -> None:
    backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}])

    async def calls(mcp, client_id, access, consent):
        with _as(access, client_id):
            res = _tool_payload(await mcp.call_tool(
                "switch_agent", {"action": "switch", "agent_id": "agent-a"}
            ))
            assert res.get("success") is True
            assert _get_agent_client().api_key == "clawdchat_a"

    _run_flow(backend, {"create_new": True}, calls)
    assert backend.reset_calls == []


# ---------------------------------------------------------------- refresh keeps binding, renews JWT


def test_refresh_keeps_switched_binding_and_renews_jwt(clean_store) -> None:
    backend = FakeBackend([
        {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
        {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
    ])

    async def go():
        mcp = create_mcp_server(transport="streamable-http")
        transport = httpx.ASGITransport(app=mcp.streamable_http_app())
        p_auth, p_server = _patch_user_client(backend)
        with p_auth, p_server, patch(
            "clawdchat_mcp.user_jwt.ClawdChatUserClient",
            side_effect=lambda _u, jwt: type(
                "R", (), {"refresh_external_jwt": staticmethod(
                    lambda: _coro({"jwt": _jwt(time.time() + 90 * 86400)})
                )}
            )(),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                client_id, verifier, login_url = await _register_and_start_login(client)
                state = _query(login_url)["state"]
                await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                consent = await client.post(
                    "/auth/select-agent", json={"state": state, "agent_id": "agent-a"}
                )
                code = _query(consent.json()["redirect"])["code"]
                tok = (await client.post("/token", data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                    "client_id": client_id, "code_verifier": verifier,
                })).json()
                # Make the stored user JWT close to expiry (10 days left)
                soon = _jwt(time.time() + 10 * 86400)
                store.update_user_jwt(tok["access_token"], soon)

                with _as(tok["access_token"], client_id):
                    await mcp.call_tool("switch_agent", {"action": "switch", "agent_id": "agent-b"})

                refreshed = await client.post("/token", data={
                    "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": client_id,
                })
                assert refreshed.status_code == 200, refreshed.text
                new_access = refreshed.json()["access_token"]
        data = store.get_access_token(new_access)
        assert data.agent_id == "agent-b" and data.agent_api_key == "clawdchat_b"
        assert jwt_expires_at(data.user_jwt) > time.time() + 80 * 86400
        assert data.refresh_token == refreshed.json()["refresh_token"]

    asyncio.run(go())


async def _coro(value):
    return value


def test_needs_renewal_window() -> None:
    now = time.time()
    assert needs_renewal(_jwt(now + 10 * 86400), now) is True
    assert needs_renewal(_jwt(now + 80 * 86400), now) is False
    assert needs_renewal("not-a-jwt", now) is False


# ---------------------------------------------------------------- encryption at rest


def test_secrets_encrypted_at_rest(tmp_path, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    from clawdchat_mcp import storage as storage_mod

    monkeypatch.setattr(storage_mod, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(storage_mod, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(storage_mod, "TOKEN_KEY_FILE", tmp_path / "key")
    monkeypatch.setattr(config_settings, "mcp_token_encryption_key", Fernet.generate_key().decode())

    s = TokenStore()
    s.store_refresh_token(RefreshTokenData(
        token="rt1", client_id="c", scopes=["agent"], expires_at=None,
        agent_api_key="clawdchat_secret_key", agent_id="a", agent_name="A", user_jwt="jwt-secret",
    ))
    s.store_access_token(AccessTokenData(
        token="at1", client_id="c", scopes=["agent"], expires_at=None,
        agent_api_key="clawdchat_secret_key", agent_id="a", agent_name="A", user_jwt="jwt-secret",
        refresh_token="rt1",
    ))
    raw = (tmp_path / "tokens.json").read_text()
    assert "clawdchat_secret_key" not in raw and "jwt-secret" not in raw
    assert "enc:v1:" in raw

    reloaded = TokenStore()
    assert reloaded.access_tokens["at1"].agent_api_key == "clawdchat_secret_key"
    assert reloaded.refresh_tokens["rt1"].user_jwt == "jwt-secret"


def test_legacy_plaintext_tokens_still_load(tmp_path, monkeypatch) -> None:
    from clawdchat_mcp import storage as storage_mod

    monkeypatch.setattr(storage_mod, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(storage_mod, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(storage_mod, "TOKEN_KEY_FILE", tmp_path / "key")
    monkeypatch.setattr(config_settings, "mcp_token_encryption_key", "")
    (tmp_path / "tokens.json").write_text(json.dumps({
        "access_tokens": {"old": {
            "token": "old", "client_id": "c", "scopes": ["agent"], "expires_at": None,
            "agent_api_key": "clawdchat_plain", "agent_id": "a", "agent_name": "A",
            "user_jwt": "jwt-plain", "resource": None,
        }},
        "refresh_tokens": {},
    }))
    s = TokenStore()
    assert s.access_tokens["old"].agent_api_key == "clawdchat_plain"
    assert (tmp_path / "key").exists()  # generated once when no env key
    s._save_tokens()
    assert "clawdchat_plain" not in (tmp_path / "tokens.json").read_text()
