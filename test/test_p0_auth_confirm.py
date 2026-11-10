"""P0 Agent confirmation gates for hosted HTTP MCP and stdio.

Local fixtures only. No real ClawdChat accounts, SMS, or upstream quota.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from clawdchat_mcp.auth_confirm import (
    CONFIRM_RESET_MUST_BE_BOOL,
    ConfirmResetTypeError,
    confirm_reset_from_mapping,
    confirm_reset_is_true,
    find_owned_agent,
    parse_confirm_reset,
)
from clawdchat_mcp.server import create_mcp_server, _get_agent_client
from clawdchat_mcp.stdio_auth import StdioAuthManager
from clawdchat_mcp.storage import store


# ---------------------------------------------------------------------------
# confirm_reset parser
# ---------------------------------------------------------------------------


class TestConfirmResetStrict:
    def test_only_bool_true(self) -> None:
        assert confirm_reset_is_true(True) is True
        assert confirm_reset_is_true(False) is False
        assert confirm_reset_is_true(None) is False
        assert confirm_reset_is_true("true") is False
        assert confirm_reset_is_true("1") is False
        assert confirm_reset_is_true(1) is False
        assert confirm_reset_is_true("yes") is False

    @pytest.mark.parametrize("value", [False, None])
    def test_missing_or_false_does_not_confirm(self, value: Any) -> None:
        assert parse_confirm_reset(value, present=value is not None) is False

    @pytest.mark.parametrize("value", ["true", "True", "1", 1, "yes", ["true"]])
    def test_non_bool_is_tamper(self, value: Any) -> None:
        with pytest.raises(ConfirmResetTypeError, match="boolean"):
            parse_confirm_reset(value, present=True)

    def test_mapping_omit_false_true_and_tamper(self) -> None:
        assert confirm_reset_from_mapping({}) is False
        assert confirm_reset_from_mapping({"confirm_reset": False}) is False
        assert confirm_reset_from_mapping({"confirm_reset": True}) is True
        with pytest.raises(ConfirmResetTypeError):
            confirm_reset_from_mapping({"confirm_reset": "true"})


class TestOwnedAgentLookup:
    def test_match_id_or_name_and_reject_other(self) -> None:
        agents = [{"id": "a1", "name": "Alpha"}, {"id": "b1", "name": "Beta"}]
        assert find_owned_agent(agents, "a1")["name"] == "Alpha"
        assert find_owned_agent(agents, "Beta")["id"] == "b1"
        assert find_owned_agent(agents, "other") is None
        assert find_owned_agent([], "a1") is None


# ---------------------------------------------------------------------------
# Fake ClawdChat user API (no network)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, data: dict[str, Any]):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self) -> dict[str, Any]:
        return self._data


class FakeBackend:
    def __init__(self, agents: list[dict[str, Any]] | None = None):
        self.agents = agents if agents is not None else []
        self.reset_calls: list[str] = []
        self.credential_calls: list[str] = []
        self.exchange_calls: list[str] = []
        self.auto_create_flags: list[bool] = []
        self.refresh_calls: list[str] = []
        self.created: list[str] = []
        self.max_agents = 10
        self.fail_get_my_agents = False

    async def create_agent(self, name: str, display_name: Any = None, description: Any = None) -> dict[str, Any]:
        from clawdchat_mcp.api_client import ClawdChatAPIError

        if len(self.agents) >= self.max_agents:
            raise ClawdChatAPIError(
                409,
                "limit",
                {
                    "reason": "claim_limit_reached",
                    "message": "limit reached",
                    "claimed_count": len(self.agents),
                    "max_agents": self.max_agents,
                    "existing_agents": self.public_agents(),
                },
            )
        agent = {"id": f"agent-{name}", "name": name, "api_key": f"clawdchat_new_{name}"}
        self.agents.append(agent)
        self.created.append(name)
        return {
            "success": True,
            "agent": {"id": agent["id"], "name": name, "display_name": display_name},
            "api_key": agent["api_key"],
        }

    def public_agents(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in agent.items() if k != "api_key"}
            for agent in self.agents
        ]

    def _find(self, agent_id: str) -> dict[str, Any] | None:
        return find_owned_agent(self.agents, agent_id)

    async def exchange_external_code(self, code: str) -> dict[str, Any]:
        self.exchange_calls.append(code)
        return {"jwt": "jwt-test-user", "user": {"id": "user-1"}}

    def agents_payload(self) -> dict[str, Any]:
        agents = self.public_agents()
        return {
            "agents": agents,
            "claimed_count": len(agents),
            "max_agents": self.max_agents,
        }

    async def get_my_agents(self) -> dict[str, Any]:
        if self.fail_get_my_agents:
            from clawdchat_mcp.api_client import ClawdChatAPIError

            raise ClawdChatAPIError(503, "unavailable")
        return self.agents_payload()

    async def get_agent_credentials(self, agent_id: str) -> dict[str, Any]:
        self.credential_calls.append(agent_id)
        agent = self._find(agent_id)
        if not agent:
            from clawdchat_mcp.api_client import ClawdChatAPIError

            raise ClawdChatAPIError(403, "not owner")
        return {"api_key": agent.get("api_key"), "agent_name": agent.get("name", "")}

    async def reset_agent_key(self, agent_id: str) -> dict[str, Any]:
        self.reset_calls.append(agent_id)
        agent = self._find(agent_id)
        if not agent:
            from clawdchat_mcp.api_client import ClawdChatAPIError

            raise ClawdChatAPIError(403, "not owner")
        agent["api_key"] = f"clawdchat_rotated_{agent_id}"
        return {"api_key": agent["api_key"], "agent_name": agent.get("name", "")}

    def httpx_get(self, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/users/me/agents"):
            return FakeResponse(200, self.agents_payload())
        if "/credentials" in url:
            agent_id = url.rstrip("/").split("/")[-2]
            self.credential_calls.append(agent_id)
            agent = self._find(agent_id)
            if not agent:
                return FakeResponse(403, {"detail": "not owner"})
            return FakeResponse(200, {
                "api_key": agent.get("api_key"),
                "agent_name": agent.get("name", ""),
            })
        return FakeResponse(404, {"detail": "not found"})

    def httpx_post(self, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/auth/external/token"):
            self.exchange_calls.append((kwargs.get("json") or {}).get("code", ""))
            return FakeResponse(200, {"jwt": "jwt-test-user", "user": {"id": "user-1"}})
        if url.endswith("/reset-key"):
            agent_id = url.rstrip("/").split("/")[-2]
            self.reset_calls.append(agent_id)
            agent = self._find(agent_id)
            if not agent:
                return FakeResponse(403, {"detail": "not owner"})
            agent["api_key"] = f"clawdchat_rotated_{agent_id}"
            return FakeResponse(200, {
                "api_key": agent["api_key"],
                "agent_name": agent.get("name", ""),
            })
        return FakeResponse(404, {"detail": "not found"})


class FakeUserClient:
    def __init__(self, backend: FakeBackend, jwt: str):
        self.backend = backend
        self.jwt = jwt

    async def exchange_external_code(self, code: str) -> dict[str, Any]:
        return await self.backend.exchange_external_code(code)

    async def get_my_agents(self, auto_create: bool = True) -> dict[str, Any]:
        self.backend.auto_create_flags.append(auto_create)
        return await self.backend.get_my_agents()

    async def create_agent(self, name: str, display_name: Any = None, description: Any = None) -> dict[str, Any]:
        return await self.backend.create_agent(name, display_name, description)

    async def refresh_external_jwt(self) -> dict[str, Any]:
        self.backend.refresh_calls.append(self.jwt)
        return {"jwt": f"{self.jwt}-renewed"}

    async def get_agent_credentials(self, agent_id: str) -> dict[str, Any]:
        return await self.backend.get_agent_credentials(agent_id)

    async def reset_agent_key(self, agent_id: str) -> dict[str, Any]:
        return await self.backend.reset_agent_key(agent_id)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _http_app():
    return create_mcp_server(transport="streamable-http").streamable_http_app()


def _tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and result:
        structured = result[1] if len(result) > 1 else None
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            try:
                return json.loads(structured["result"])
            except json.JSONDecodeError:
                return {"raw": structured["result"]}
        result = result[0]
    if isinstance(result, dict):
        return result
    texts: list[str] = []
    if isinstance(result, list):
        for block in result:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
    raw = "\n".join(texts) if texts else str(result)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


async def _register_and_start_login(client: httpx.AsyncClient) -> tuple[str, str, str]:
    verifier, challenge = _pkce()
    registered = await client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9876/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": "p0-auth-confirm-test",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    authorize = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "http://localhost:9876/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "p0-oauth-state",
        },
        follow_redirects=False,
    )
    assert authorize.status_code in (302, 303), authorize.text
    return client_id, verifier, authorize.headers["location"]


@pytest.fixture
def clean_store():
    store.pending_logins.clear()
    store.auth_codes.clear()
    yield
    store.pending_logins.clear()


def _patch_user_client(backend: FakeBackend):
    def factory(_api_url: str, jwt: str) -> FakeUserClient:
        return FakeUserClient(backend, jwt)

    return (
        patch("clawdchat_mcp.auth_provider.ClawdChatUserClient", side_effect=factory),
        patch("clawdchat_mcp.server.ClawdChatUserClient", side_effect=factory),
    )


# ---------------------------------------------------------------------------
# Hosted HTTP MCP
# ---------------------------------------------------------------------------


class TestHostedLoginConfirm:
    def test_zero_agents_no_autogrant(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    resp = await client.get(
                        "/auth/clawdchat/callback",
                        params={"code": "ext-code", "state": state},
                        follow_redirects=False,
                    )
                    # Account-first: zero Agents still reaches the consent page (no error),
                    # never auto-creates a default Agent and never issues a code by itself.
                    assert resp.status_code in (302, 303)
                    assert "/auth/select-agent" in resp.headers["location"]
                    page = await client.get(f"/auth/select-agent?state={state}")
                    assert page.status_code == 200
                    assert "createNewBtn" in page.text and "bindLater" not in page.text
            assert store.auth_codes == {}
            assert backend.reset_calls == []
            assert backend.credential_calls == []
            assert backend.auto_create_flags and not any(backend.auto_create_flags)

        asyncio.run(_run())

    def test_one_agent_still_needs_select(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 1, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    resp = await client.get(
                        "/auth/clawdchat/callback",
                        params={"code": "ext-code", "state": state},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 302
                    location = resp.headers["location"]
                    assert "/auth/select-agent" in location
                    assert store.auth_codes == {}
                    assert backend.reset_calls == []
                    assert backend.credential_calls == []
                    pending = store.get_pending_login(state)
                    assert pending is not None
                    assert pending.user_jwt == "jwt-test-user"

                    page = await client.get(location, headers={"Host": "mcp.clawdchat.ai"})
                    assert page.status_code == 200
                    assert "Authorize this Agent" in page.text
                    assert "Select Agent" in page.text
                    assert "confirmSelection()" in page.text
                    assert "auto-submit" in page.text or "never auto-submit" in page.text
                    assert not any("\u4e00" <= ch <= "\u9fff" for ch in page.text)

        asyncio.run(_run())

    def test_multi_agent_select_page(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
                {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    resp = await client.get(
                        "/auth/clawdchat/callback",
                        params={"code": "ext-code", "state": state},
                        follow_redirects=False,
                    )
                    page = await client.get(
                        resp.headers["location"],
                        headers={"Host": "mcp.clawdchat.cn"},
                    )
            assert resp.status_code == 302
            assert "Alpha" in page.text and "Beta" in page.text
            assert "授权此 Agent" in page.text
            assert store.auth_codes == {}
            assert backend.reset_calls == []

        asyncio.run(_run())

    def test_select_stored_key_unchanged(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a"},
                    )
            assert selected.status_code == 200
            body = selected.json()
            assert "redirect" in body
            assert store.get_pending_login(state) is None
            assert len(store.auth_codes) == 1
            code_data = next(iter(store.auth_codes.values()))
            assert code_data.agent_api_key == "clawdchat_a"
            assert backend.reset_calls == []
            assert backend.agents[0]["api_key"] == "clawdchat_a"

        asyncio.run(_run())

    @pytest.mark.parametrize("payload_extra", [{}, {"confirm_reset": False}])
    def test_select_missing_key_without_true_no_reset(self, clean_store, payload_extra: dict) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    body = {"state": state, "agent_id": "agent-a", **payload_extra}
                    selected = await client.post("/auth/select-agent", json=body)
            assert selected.status_code == 200
            data = selected.json()
            assert data.get("needs_reset") is True
            assert "redirect" not in data
            assert store.auth_codes == {}
            assert store.get_pending_login(state) is not None
            assert backend.reset_calls == []
            assert backend.agents[0]["api_key"] is None

        asyncio.run(_run())

    def test_select_tampered_confirm_reset_rejected(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a", "confirm_reset": "true"},
                    )
            assert selected.status_code == 422
            assert "boolean" in selected.json()["error"]
            assert store.auth_codes == {}
            assert backend.reset_calls == []
            assert backend.agents[0]["api_key"] is None

        asyncio.run(_run())

    def test_select_missing_key_confirm_true_rotates_only_target(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
                {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a", "confirm_reset": True},
                    )
            assert selected.status_code == 200
            assert "redirect" in selected.json()
            assert backend.reset_calls == ["agent-a"]
            assert backend.agents[0]["api_key"] == "clawdchat_rotated_agent-a"
            assert backend.agents[1]["api_key"] == "clawdchat_b"
            code_data = next(iter(store.auth_codes.values()))
            assert code_data.agent_api_key == "clawdchat_rotated_agent-a"

        asyncio.run(_run())

    def test_cancel_keeps_session_without_key(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    needs = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a"},
                    )
                    assert needs.json().get("needs_reset") is True
                    # Cancel = no second POST. Session stays pending, no key issued.
            assert store.auth_codes == {}
            assert store.get_pending_login(state) is not None
            assert backend.reset_calls == []

        asyncio.run(_run())

    def test_cross_ownership_rejected(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    _cid, _v, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-other", "confirm_reset": True},
                    )
            assert selected.status_code == 403
            assert store.auth_codes == {}
            assert backend.reset_calls == []
            assert backend.credential_calls == []

        asyncio.run(_run())

    def test_select_before_login_rejected(self, clean_store) -> None:
        async def _run() -> None:
            app = _http_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                selected = await client.post(
                    "/auth/select-agent",
                    json={"state": "no-such-state", "agent_id": "agent-a", "confirm_reset": True},
                )
            assert selected.status_code == 400
            assert store.auth_codes == {}

        asyncio.run(_run())


class TestHostedSwitchAgent:
    def test_switch_stored_key_readonly_and_original_unchanged(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
                {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            mcp = create_mcp_server(transport="streamable-http")
            app = mcp.streamable_http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    client_id, verifier, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a"},
                    )
                    code = _query(selected.json()["redirect"])["code"]
                    token_resp = await client.post(
                        "/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": "http://localhost:9876/callback",
                            "client_id": client_id,
                            "code_verifier": verifier,
                        },
                    )
                    assert token_resp.status_code == 200, token_resp.text
                    access_token = token_resp.json()["access_token"]

                from clawdchat_mcp import server as server_mod

                with patch.object(
                    server_mod,
                    "get_access_token",
                    lambda: AccessToken(token=access_token, client_id=client_id, scopes=["agent"]),
                ):
                    switched = _tool_payload(await mcp.call_tool(
                        "switch_agent",
                        {"action": "switch", "agent_id": "agent-b"},
                    ))
                    assert switched.get("success") is True
                    assert switched["agent_id"] == "agent-b"
                    client_obj = _get_agent_client()
                    assert client_obj.api_key == "clawdchat_b"

            assert backend.reset_calls == []
            assert backend.agents[0]["api_key"] == "clawdchat_a"
            assert backend.agents[1]["api_key"] == "clawdchat_b"
            token_data = store.get_access_token(access_token)
            assert token_data is not None
            assert token_data.agent_api_key == "clawdchat_b"
            assert token_data.agent_id == "agent-b"

        asyncio.run(_run())

    def test_switch_missing_key_omit_confirm_no_reset(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
                {"id": "agent-b", "name": "Beta", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            mcp = create_mcp_server(transport="streamable-http")
            app = mcp.streamable_http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    client_id, verifier, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a"},
                    )
                    code = _query(selected.json()["redirect"])["code"]
                    token_resp = await client.post(
                        "/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": "http://localhost:9876/callback",
                            "client_id": client_id,
                            "code_verifier": verifier,
                        },
                    )
                    access_token = token_resp.json()["access_token"]

                from clawdchat_mcp import server as server_mod

                with patch.object(
                    server_mod,
                    "get_access_token",
                    lambda: AccessToken(token=access_token, client_id=client_id, scopes=["agent"]),
                ):
                    result = _tool_payload(await mcp.call_tool(
                        "switch_agent",
                        {"action": "switch", "agent_id": "agent-b"},
                    ))
                    assert result.get("needs_reset") is True
                    client_obj = _get_agent_client()
                    assert client_obj.api_key == "clawdchat_a"

            assert backend.reset_calls == []
            assert backend.agents[0]["api_key"] == "clawdchat_a"
            assert backend.agents[1]["api_key"] is None

        asyncio.run(_run())

    def test_switch_unowned_rejected(self, clean_store) -> None:
        async def _run() -> None:
            backend = FakeBackend([
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
            ])
            mcp = create_mcp_server(transport="streamable-http")
            app = mcp.streamable_http_app()
            transport = httpx.ASGITransport(app=app)
            p_auth, p_server = _patch_user_client(backend)
            with p_auth, p_server:
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    client_id, verifier, login_url = await _register_and_start_login(client)
                    state = _query(login_url)["state"]
                    await client.get("/auth/clawdchat/callback", params={"code": "c", "state": state})
                    selected = await client.post(
                        "/auth/select-agent",
                        json={"state": state, "agent_id": "agent-a"},
                    )
                    code = _query(selected.json()["redirect"])["code"]
                    token_resp = await client.post(
                        "/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": "http://localhost:9876/callback",
                            "client_id": client_id,
                            "code_verifier": verifier,
                        },
                    )
                    access_token = token_resp.json()["access_token"]

                from clawdchat_mcp import server as server_mod

                with patch.object(
                    server_mod,
                    "get_access_token",
                    lambda: AccessToken(token=access_token, client_id=client_id, scopes=["agent"]),
                ):
                    result = await mcp.call_tool(
                        "switch_agent",
                        {"action": "switch", "agent_id": "agent-other", "confirm_reset": True},
                    )
                    payload = _tool_payload(result)
                    text = json.dumps(payload, ensure_ascii=False)
                    assert "not in the owned" in text

            assert backend.reset_calls == []
            assert store.get_access_token(access_token).agent_api_key == "clawdchat_a"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------


def _stdio_manager(backend: FakeBackend, agents: list[dict[str, Any]]) -> StdioAuthManager:
    mgr = StdioAuthManager()
    mgr.jwt = "jwt-test-user"
    mgr.agents = backend.public_agents()
    return mgr


def _clear_stdio_auth() -> None:
    from clawdchat_mcp.stdio_auth import stdio_auth

    stdio_auth.jwt = ""
    stdio_auth.api_key = ""
    stdio_auth.agent_id = ""
    stdio_auth.agent_name = ""
    stdio_auth.agents = []
    stdio_auth._error = ""


def _stdio_no_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force _get_agent_client() onto the process-local stdio_auth memory key."""
    from clawdchat_mcp import server as server_mod

    monkeypatch.setattr(server_mod, "get_access_token", lambda: None)
    monkeypatch.setattr(server_mod.settings, "clawdchat_api_key", "")


class TestStdioLoginConfirm:
    def test_zero_agents_raises_no_reset(self) -> None:
        backend = FakeBackend([])
        mgr = StdioAuthManager()
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            with pytest.raises(RuntimeError, match="No claimed agents"):
                mgr._do_auth_exchange("ext-code")
        assert mgr.api_key == ""
        assert backend.reset_calls == []

    def test_one_and_multi_wait_for_confirm(self) -> None:
        for agents in (
            [{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}],
            [
                {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
                {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
            ],
        ):
            backend = FakeBackend(agents)
            mgr = StdioAuthManager()
            with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
                 patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
                mgr._do_auth_exchange("ext-code")
            assert mgr.jwt == "jwt-test-user"
            assert mgr.api_key == ""
            assert mgr.needs_agent_selection is True
            assert mgr.is_authenticated is False
            assert backend.reset_calls == []
            assert backend.credential_calls == []
            status = mgr.get_status()
            assert status["status"] == "needs_selection"
            assert len(status["agents"]) == len(agents)

    def test_stored_key_bind_does_not_reset(self) -> None:
        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        mgr = _stdio_manager(backend, backend.agents)
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            result = mgr.select_agent("agent-a")
        assert result["status"] == "authenticated"
        assert mgr.api_key == "clawdchat_a"
        assert backend.reset_calls == []
        assert backend.agents[1]["api_key"] == "clawdchat_b"

    @pytest.mark.parametrize("confirm", [False, None, "omit"])
    def test_missing_key_without_true_no_reset(self, confirm: Any) -> None:
        backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": None}])
        mgr = _stdio_manager(backend, backend.agents)
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            if confirm == "omit":
                result = mgr.select_agent("agent-a")
            else:
                result = mgr.select_agent("agent-a", confirm_reset=confirm)
        assert result.get("needs_reset") is True
        assert mgr.api_key == ""
        assert backend.reset_calls == []
        assert backend.agents[0]["api_key"] is None

    def test_tampered_confirm_reset_rejected(self) -> None:
        backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": None}])
        mgr = _stdio_manager(backend, backend.agents)
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            result = mgr.select_agent("agent-a", confirm_reset="true")
        assert result.get("error") == CONFIRM_RESET_MUST_BE_BOOL
        assert mgr.api_key == ""
        assert backend.reset_calls == []

    def test_confirm_true_rotates_only_target(self) -> None:
        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": None},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        mgr = _stdio_manager(backend, backend.agents)
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            result = mgr.select_agent("agent-a", confirm_reset=True)
        assert result["status"] == "authenticated"
        assert mgr.api_key == "clawdchat_rotated_agent-a"
        assert backend.reset_calls == ["agent-a"]
        assert backend.agents[1]["api_key"] == "clawdchat_b"

    def test_switch_keeps_original_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        _stdio_no_oauth(monkeypatch)
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.agents = backend.public_agents()
        try:
            with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
                 patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
                assert stdio_auth.select_agent("agent-a")["status"] == "authenticated"
                original_a = backend.agents[0]["api_key"]
                switched = stdio_auth.select_agent("agent-b")
                assert switched["status"] == "authenticated"
                assert stdio_auth.api_key == "clawdchat_b"
                followup = _get_agent_client()
                assert followup.api_key == "clawdchat_b"
                assert backend.agents[0]["api_key"] == original_a
                assert backend.reset_calls == []
        finally:
            _clear_stdio_auth()

    def test_switch_to_missing_key_keeps_previous_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": None},
        ])
        _stdio_no_oauth(monkeypatch)
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.agents = backend.public_agents()
        try:
            with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
                 patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
                stdio_auth.select_agent("agent-a")
                result = stdio_auth.select_agent("agent-b")
                assert result.get("needs_reset") is True
                assert stdio_auth.api_key == "clawdchat_a"
                assert stdio_auth.agent_id == "agent-a"
                followup = _get_agent_client()
                assert followup.api_key == "clawdchat_a"
                assert backend.reset_calls == []
                assert backend.agents[1]["api_key"] is None
        finally:
            _clear_stdio_auth()

    def test_unowned_does_not_bind(self) -> None:
        backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}])
        mgr = _stdio_manager(backend, backend.agents)
        mgr.api_key = "clawdchat_a"
        mgr.agent_id = "agent-a"
        with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
             patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
            result = mgr.select_agent("agent-other", confirm_reset=True)
        assert "not in the owned" in result["error"]
        assert mgr.api_key == "clawdchat_a"
        assert backend.reset_calls == []
        assert backend.credential_calls == []

    def _patch_stdio_httpx(self, backend: FakeBackend):
        real_get = httpx.get
        real_post = httpx.post

        def fake_get(url: str, *args: Any, **kwargs: Any):
            if "127.0.0.1" in str(url) or "localhost" in str(url):
                return real_get(url, *args, **kwargs)
            return backend.httpx_get(url, **kwargs)

        def fake_post(url: str, *args: Any, **kwargs: Any):
            if "127.0.0.1" in str(url) or "localhost" in str(url):
                return real_post(url, *args, **kwargs)
            return backend.httpx_post(url, **kwargs)

        return (
            patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=fake_post),
            patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=fake_get),
        )

    def test_local_http_callback_shows_select_page_for_one_agent(self) -> None:
        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a", "post_count": 0, "follower_count": 0, "karma": 0},
        ])
        mgr = StdioAuthManager()
        p_post, p_get = self._patch_stdio_httpx(backend)
        with p_post, p_get:
            auth_url = mgr.get_auth_url()
            assert "external/authorize" in auth_url
            try:
                page = httpx.get(f"http://127.0.0.1:{mgr._port}/callback?code=ext-code", timeout=5.0)
            finally:
                mgr._shutdown_server()
        assert page.status_code == 200
        assert "Authorize this Agent" in page.text or "授权此 Agent" in page.text
        assert mgr.api_key == ""
        assert mgr.needs_agent_selection is True
        assert backend.reset_calls == []
        assert backend.credential_calls == []

    def test_local_http_select_omit_confirm_no_reset(self) -> None:
        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": None, "post_count": 0, "follower_count": 0, "karma": 0},
        ])
        mgr = StdioAuthManager()
        p_post, p_get = self._patch_stdio_httpx(backend)
        with p_post, p_get:
            mgr.get_auth_url()
            try:
                httpx.get(f"http://127.0.0.1:{mgr._port}/callback?code=ext-code", timeout=5.0)
                omitted = httpx.post(
                    f"http://127.0.0.1:{mgr._port}/select",
                    json={"agent_id": "agent-a"},
                    timeout=5.0,
                )
                tampered = httpx.post(
                    f"http://127.0.0.1:{mgr._port}/select",
                    json={"agent_id": "agent-a", "confirm_reset": "true"},
                    timeout=5.0,
                )
                confirmed = httpx.post(
                    f"http://127.0.0.1:{mgr._port}/select",
                    json={"agent_id": "agent-a", "confirm_reset": True},
                    timeout=5.0,
                )
            finally:
                mgr._shutdown_server()
        assert omitted.json().get("needs_reset") is True
        assert tampered.status_code == 422
        assert confirmed.json().get("success") is True
        assert backend.reset_calls == ["agent-a"]
        assert mgr.api_key == "clawdchat_rotated_agent-a"


class TestStdioListMyAgents:
    def test_current_includes_quota_and_list_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        backend.max_agents = 4
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.api_key = "clawdchat_a"
        stdio_auth.agent_id = "agent-a"
        stdio_auth.agent_name = "Alpha"
        stdio_auth.agents = backend.public_agents()
        mcp = create_mcp_server(transport="stdio")
        _stdio_no_oauth(monkeypatch)
        p_auth, p_server = _patch_user_client(backend)
        try:
            with p_auth, p_server:
                current = _tool_payload(asyncio.run(mcp.call_tool(
                    "list_my_agents", {"action": "current"}
                )))
                listed = _tool_payload(asyncio.run(mcp.call_tool(
                    "list_my_agents", {"action": "list"}
                )))
            assert current["current_agent_id"] == "agent-a"
            assert current.get("claimed") == 2 and current.get("max") == 4
            assert current.get("total") == 2
            assert "action=list" in current.get("hint", "")
            assert {a["name"] for a in listed["agents"]} == {"Alpha", "Beta"}
            assert listed.get("claimed") == 2 and listed.get("max") == 4
            assert listed.get("total") == 2
            assert "action=list" not in listed.get("hint", "")
        finally:
            _clear_stdio_auth()

    def test_list_fallback_does_not_hint_action_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        backend.fail_get_my_agents = True
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.api_key = "clawdchat_a"
        stdio_auth.agent_id = "agent-a"
        stdio_auth.agent_name = "Alpha"
        stdio_auth.agents = backend.public_agents()
        mcp = create_mcp_server(transport="stdio")
        _stdio_no_oauth(monkeypatch)
        p_auth, p_server = _patch_user_client(backend)
        try:
            with p_auth, p_server:
                listed = _tool_payload(asyncio.run(mcp.call_tool(
                    "list_my_agents", {"action": "list"}
                )))
            assert {a["name"] for a in listed["agents"]} == {"Alpha", "Beta"}
            assert listed.get("total") == 2
            assert "action=list" not in listed.get("hint", "")
            assert "claimed" not in listed and "max" not in listed
        finally:
            _clear_stdio_auth()

    def test_create_still_requires_hosted_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([{"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"}])
        backend.max_agents = 1
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.api_key = "clawdchat_a"
        stdio_auth.agent_id = "agent-a"
        stdio_auth.agent_name = "Alpha"
        stdio_auth.agents = backend.public_agents()
        mcp = create_mcp_server(transport="stdio")
        _stdio_no_oauth(monkeypatch)
        try:
            res = _tool_payload(asyncio.run(mcp.call_tool(
                "switch_agent", {"action": "create", "name": "nope"}
            )))
            text = json.dumps(res)
            assert "confirm_url" not in text
            assert "account" in text.lower() or "hosted" in text.lower()
        finally:
            _clear_stdio_auth()


class TestStdioSwitchTool:
    def test_switch_stored_key_followup_get_agent_client_uses_b(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": "clawdchat_b"},
        ])
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.api_key = "clawdchat_a"
        stdio_auth.agent_id = "agent-a"
        stdio_auth.agent_name = "Alpha"
        stdio_auth.agents = backend.public_agents()
        mcp = create_mcp_server(transport="stdio")
        _stdio_no_oauth(monkeypatch)
        try:
            with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
                 patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
                before = _get_agent_client()
                assert before.api_key == "clawdchat_a"
                switched = _tool_payload(asyncio.run(mcp.call_tool(
                    "switch_agent", {"action": "switch", "agent_id": "agent-b"}
                )))
                assert switched.get("status") == "authenticated" or switched.get("success") is True
                followup = _get_agent_client()
                assert followup.api_key == "clawdchat_b"
                assert followup is not before
                assert backend.agents[0]["api_key"] == "clawdchat_a"
                assert backend.agents[1]["api_key"] == "clawdchat_b"
                assert backend.reset_calls == []
        finally:
            _clear_stdio_auth()

    def test_switch_agent_tool_uses_confirm_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawdchat_mcp.stdio_auth import stdio_auth

        backend = FakeBackend([
            {"id": "agent-a", "name": "Alpha", "api_key": "clawdchat_a"},
            {"id": "agent-b", "name": "Beta", "api_key": None},
        ])
        stdio_auth.jwt = "jwt-test-user"
        stdio_auth.api_key = "clawdchat_a"
        stdio_auth.agent_id = "agent-a"
        stdio_auth.agent_name = "Alpha"
        stdio_auth.agents = backend.public_agents()
        mcp = create_mcp_server(transport="stdio")
        _stdio_no_oauth(monkeypatch)
        try:
            with patch("clawdchat_mcp.stdio_auth.httpx.post", side_effect=backend.httpx_post), \
                 patch("clawdchat_mcp.stdio_auth.httpx.get", side_effect=backend.httpx_get):
                omitted = _tool_payload(asyncio.run(mcp.call_tool(
                    "switch_agent", {"action": "switch", "agent_id": "agent-b"}
                )))
                assert omitted.get("needs_reset") is True
                assert stdio_auth.api_key == "clawdchat_a"
                unconfirmed = _get_agent_client()
                assert unconfirmed.api_key == "clawdchat_a"
                assert backend.reset_calls == []

                confirmed = _tool_payload(asyncio.run(mcp.call_tool(
                    "switch_agent",
                    {"action": "switch", "agent_id": "agent-b", "confirm_reset": True},
                )))
                assert confirmed.get("status") == "authenticated"
                assert stdio_auth.api_key == "clawdchat_rotated_agent-b"
                followup = _get_agent_client()
                assert followup.api_key == "clawdchat_rotated_agent-b"
                assert backend.agents[0]["api_key"] == "clawdchat_a"
        finally:
            _clear_stdio_auth()
