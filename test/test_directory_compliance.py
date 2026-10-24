"""Claude / OpenAI / Smithery directory compliance tests (offline, no live API)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from clawdchat_mcp import __version__
from clawdchat_mcp.api_client import ClawdChatAPIError
from clawdchat_mcp.oauth_redirect import (
    CLAUDE_CALLBACK_URIS,
    CHATGPT_PLATFORM_REDIRECT,
    describe_allowed_oauth_redirects,
    is_allowed_oauth_redirect_uri,
)
from clawdchat_mcp.server import create_mcp_server

READ_TOOLS = {
    "read_posts",
    "read_circles",
    "read_social",
    "read_my_status",
    "read_comments",
    "read_direct_messages",
    "list_my_agents",
    "discover_tools",
}

WRITE_TOOLS = {
    "create_post",
    "upload_file",
    "manage_circles",
    "social",
    "my_status",
    "interact",
    "direct_message",
    "switch_agent",
    "use_tools",
}

READ_ACTIONS: dict[str, set[str]] = {
    "read_circles": {"list", "get"},
    "read_social": {"stats", "profile", "followers", "following", "active_agents"},
    "read_my_status": {"profile", "status", "current_agent"},
    "read_comments": {"list_comments"},
    "read_direct_messages": {"inbox", "list", "conversations", "get_conversation", "thread"},
    "list_my_agents": {"current", "list"},
    "discover_tools": {"categories", "search", "search_servers", "credits"},
}

WRITE_ACTIONS: dict[str, set[str]] = {
    "manage_circles": {"create", "update", "subscribe", "unsubscribe"},
    "social": {"follow", "unfollow"},
    "my_status": {"update_profile", "upload_avatar", "delete_avatar"},
    "interact": {
        "upvote_post",
        "downvote_post",
        "bookmark_post",
        "bookmark",
        "unbookmark",
        "comment",
        "reply",
        "upvote_comment",
        "downvote_comment",
        "edit_post",
        "delete_post",
        "delete_comment",
    },
    "direct_message": {"send", "action", "delete_conversation"},
    "switch_agent": {"switch"},
    "use_tools": {"call", "connect", "rate"},
}

ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "openWorldHint")


def _http_app():
    return create_mcp_server(transport="streamable-http").streamable_http_app()


async def _get_json(path: str, host: str | None = None) -> tuple[int, Any]:
    app = _http_app()
    headers = {"Host": host} if host else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
        response = await client.get(path, headers=headers)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.status_code, response.json()
        return response.status_code, response.text


def _action_enum(tool) -> set[str] | None:
    schema = tool.inputSchema or {}
    props = schema.get("properties") or {}
    action = props.get("action") or {}
    if "enum" in action:
        return set(action["enum"])
    if "const" in action:
        return {action["const"]}
    for variant in action.get("anyOf") or []:
        if "enum" in variant:
            return set(variant["enum"])
        if "const" in variant:
            return {variant["const"]}
    return None


def test_all_tools_have_title_and_full_annotations() -> None:
    server = create_mcp_server(transport="streamable-http")

    async def _list():
        return await server.list_tools()

    tools = asyncio.run(_list())
    assert tools, "server should export tools"

    names = {tool.name for tool in tools}
    assert READ_TOOLS <= names
    assert WRITE_TOOLS <= names

    for tool in tools:
        assert tool.title, f"{tool.name} missing title"
        assert tool.annotations is not None, f"{tool.name} missing annotations"
        dumped = tool.annotations.model_dump()
        assert dumped.get("title"), f"{tool.name} annotations.title is empty"
        for key in ANNOTATION_KEYS:
            assert key in dumped, f"{tool.name} annotations missing {key}"
            assert isinstance(dumped[key], bool), f"{tool.name} {key} must be bool"

        if tool.name in READ_TOOLS:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.openWorldHint is True
        if tool.name in WRITE_TOOLS:
            assert tool.annotations.readOnlyHint is False
            assert tool.annotations.destructiveHint is True
            assert tool.annotations.openWorldHint is True


def test_read_and_write_actions_are_fully_separated() -> None:
    server = create_mcp_server(transport="streamable-http")

    async def _list():
        return await server.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(_list())}

    for name, expected in READ_ACTIONS.items():
        enum = _action_enum(tools[name])
        assert enum is not None, f"{name} should expose an action enum"
        assert enum == expected, f"{name} read actions {enum} != {expected}"
        assert enum.isdisjoint(WRITE_ACTIONS.get(name.replace("read_", "manage_"), set()))

    for name, expected in WRITE_ACTIONS.items():
        enum = _action_enum(tools[name])
        assert enum is not None, f"{name} should expose an action enum"
        assert enum == expected, f"{name} write actions {enum} != {expected}"

    # Same-domain read/write pairs must not share any action names.
    pairs = (
        ("read_circles", "manage_circles"),
        ("read_social", "social"),
        ("read_my_status", "my_status"),
        ("read_comments", "interact"),
        ("read_direct_messages", "direct_message"),
        ("list_my_agents", "switch_agent"),
        ("discover_tools", "use_tools"),
    )
    for read_name, write_name in pairs:
        assert _action_enum(tools[read_name]).isdisjoint(_action_enum(tools[write_name]))


def test_stdio_authenticate_also_has_annotations() -> None:
    server = create_mcp_server(transport="stdio")

    async def _list():
        return await server.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(_list())}
    auth = tools["authenticate"]
    assert auth.title
    assert auth.annotations.readOnlyHint is False
    assert auth.annotations.destructiveHint is True
    assert auth.annotations.openWorldHint is True


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ],
)
def test_protected_resource_ends_with_mcp(path: str) -> None:
    status, body = asyncio.run(_get_json(path))
    assert status == 200
    assert isinstance(body, dict)
    assert body["resource"].endswith("/mcp")
    assert not body["resource"].endswith("//mcp")
    assert body["resource"] == "http://localhost:8347/mcp"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("mcp.clawdchat.ai", "https://mcp.clawdchat.ai/mcp"),
        ("mcp.clawdchat.cn", "https://mcp.clawdchat.cn/mcp"),
    ],
)
def test_protected_resource_host_ai_and_cn(host: str, expected: str) -> None:
    async def _fetch() -> dict:
        status, body = await _get_json("/.well-known/oauth-protected-resource", host=host)
        assert status == 200
        return body

    body = asyncio.run(_fetch())
    assert body["resource"] == expected
    status_mcp, body_mcp = asyncio.run(
        _get_json("/.well-known/oauth-protected-resource/mcp", host=host)
    )
    assert status_mcp == 200
    assert body_mcp["resource"] == expected


def test_oauth_redirect_allowlist_covers_claude_and_chatgpt() -> None:
    for uri in CLAUDE_CALLBACK_URIS:
        assert is_allowed_oauth_redirect_uri(uri)
    assert is_allowed_oauth_redirect_uri(CHATGPT_PLATFORM_REDIRECT)
    assert is_allowed_oauth_redirect_uri("http://localhost:8787/callback")
    assert is_allowed_oauth_redirect_uri("http://127.0.0.1:4321/callback")
    assert is_allowed_oauth_redirect_uri("https://chatgpt.com/connector/oauth/cb_abc123")
    assert is_allowed_oauth_redirect_uri("https://chatgpt.com/connector/oauth/*")
    assert not is_allowed_oauth_redirect_uri("https://evil.example/callback")
    assert "http://localhost:<port>/callback" in describe_allowed_oauth_redirects()


def test_dcr_and_authorize_accept_official_callbacks() -> None:
    official_uris = [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:9876/callback",
        "http://127.0.0.1:9876/callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/reviewer_callback_1",
    ]

    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            for redirect_uri in official_uris:
                registered = await client.post(
                    "/register",
                    json={
                        "redirect_uris": [redirect_uri],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                        "client_name": "directory-compliance-test",
                    },
                )
                assert registered.status_code == 201, registered.text
                client_id = registered.json()["client_id"]

                authorize = await client.get(
                    "/authorize",
                    params={
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "code_challenge": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
                        "code_challenge_method": "S256",
                        "state": "compliance-state",
                    },
                    follow_redirects=False,
                )
                assert authorize.status_code in (302, 303), (
                    f"{redirect_uri} rejected: {authorize.status_code} {authorize.text}"
                )
                assert "error=invalid_redirect_uri" not in authorize.headers.get("location", "")

            # ChatGPT connector callback_id may differ from the registered one.
            wildcard = await client.post(
                "/register",
                json={
                    "redirect_uris": ["https://chatgpt.com/connector/oauth/original_id"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "client_name": "chatgpt-wildcard",
                },
            )
            assert wildcard.status_code == 201, wildcard.text
            other = await client.get(
                "/authorize",
                params={
                    "client_id": wildcard.json()["client_id"],
                    "redirect_uri": "https://chatgpt.com/connector/oauth/another_callback",
                    "response_type": "code",
                    "code_challenge": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
                    "code_challenge_method": "S256",
                    "state": "cb-swap",
                },
                follow_redirects=False,
            )
            assert other.status_code in (302, 303), other.text

    asyncio.run(_run())


def test_server_card_and_openai_challenge() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            card_resp = await client.get("/.well-known/mcp/server-card.json")
            assert card_resp.status_code == 200
            card = card_resp.json()
            assert card["serverInfo"]["name"] == "ClawdChat"
            assert card["serverInfo"]["title"]
            assert card["serverInfo"]["version"] == "0.1.12"
            assert card["serverInfo"]["websiteUrl"]
            assert card["serverInfo"]["icons"]
            assert card["authentication"]["type"] == "oauth2"
            assert "oauth2" in card["authentication"]["schemes"]
            assert "bearer" in card["authentication"]["schemes"]
            names = {tool["name"] for tool in card["tools"]}
            assert READ_TOOLS <= names
            assert WRITE_TOOLS <= names
            for tool in card["tools"]:
                assert tool["title"]
                assert tool["description"]
                assert tool["inputSchema"]
                annotations = tool["annotations"]
                for key in ANNOTATION_KEYS:
                    assert isinstance(annotations[key], bool)

            challenge = await client.get("/.well-known/openai-apps-challenge")
            assert challenge.status_code == 200
            assert challenge.headers["content-type"].startswith("text/plain")

    asyncio.run(_run())


def test_openai_challenge_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from clawdchat_mcp import config as config_mod
    from clawdchat_mcp.discovery import openai_apps_challenge_response

    monkeypatch.setattr(config_mod.settings, "openai_apps_challenge_token", "openai-challenge-fixture")
    response = openai_apps_challenge_response()
    assert response.status_code == 200
    assert response.body.decode() == "openai-challenge-fixture"


EXPECTED_VERSION = "0.1.12"
TEST_API_KEY = "clawdchat_test_key_123"
TEST_AGENT_ID = "agent-reviewer-1"
TEST_AGENT_NAME = "ReviewerAgent"
TEST_PROFILE = {"id": TEST_AGENT_ID, "name": TEST_AGENT_NAME}


def test_version_is_0_1_12() -> None:
    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    server_meta = json.loads((root / "server.json").read_text(encoding="utf-8"))
    glama = json.loads((root / "glama.json").read_text(encoding="utf-8"))

    assert __version__ == EXPECTED_VERSION
    assert f'version = "{EXPECTED_VERSION}"' in pyproject
    assert server_meta["version"] == EXPECTED_VERSION
    assert glama["maintainers"] == ["xray918", "lxyd-ai"]
    assert glama["$schema"] == "https://glama.ai/mcp/schemas/server.json"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and result:
        structured = result[1] if len(result) > 1 else None
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            return json.loads(structured["result"])
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
    return json.loads(raw)


async def _register_and_start_login(
    client: httpx.AsyncClient,
    *,
    redirect_uri: str = "http://localhost:9876/callback",
    oauth_state: str = "reviewer-oauth-state",
) -> tuple[str, str, str]:
    verifier, challenge = _pkce()
    registered = await client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": "api-key-login-test",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]

    authorize = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": oauth_state,
        },
        follow_redirects=False,
    )
    assert authorize.status_code in (302, 303), authorize.text
    login_url = authorize.headers["location"]
    assert "/auth/login" in login_url
    return client_id, verifier, login_url


def test_login_page_renders_dual_channel_html() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            invalid = await client.get("/auth/login")
            assert invalid.status_code == 400

            expired = await client.get("/auth/login", params={"state": "missing-or-expired"})
            assert expired.status_code == 400

            _client_id, _verifier, login_url = await _register_and_start_login(client)
            page = await client.get(login_url)
            assert page.status_code == 200
            html = page.text
            assert "/api/v1/auth/external/authorize" in html
            assert 'name="api_key"' in html
            assert 'name="state"' in html
            assert 'method="POST"' in html
            assert 'action="/auth/login"' in html
            assert "Continue with Google (ClawdChat Account)" in html
            assert "Or connect directly with Agent API Key (clawdchat_...)" in html
            assert "Authorize Agent" in html
            assert "Phone" not in html
            assert "手机号" not in html

    asyncio.run(_run())


def test_login_page_ai_host_is_english_without_phone() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            _client_id, _verifier, login_url = await _register_and_start_login(client)
            page = await client.get(login_url, headers={"Host": "mcp.clawdchat.ai"})
            assert page.status_code == 200
            html = page.text
            assert "Continue with Google (ClawdChat Account)" in html
            assert "Or connect directly with Agent API Key (clawdchat_...)" in html
            assert "Authorize Agent" in html
            assert "Phone" not in html
            assert "手机号" not in html
            assert not any("\u4e00" <= ch <= "\u9fff" for ch in html)

            failing = AsyncMock(side_effect=ClawdChatAPIError(401, "invalid api key"))
            login_state = _query(login_url)["state"]
            with patch("clawdchat_mcp.auth_provider.ClawdChatClient.get_my_profile", failing):
                bad = await client.post(
                    "/auth/login",
                    data={"state": login_state, "api_key": "clawdchat_test_key_bad"},
                    headers={"Host": "mcp.clawdchat.ai"},
                    follow_redirects=False,
                )
            assert bad.status_code == 400
            assert "API Key verification failed" in bad.text
            assert "Phone" not in bad.text
            assert "手机号" not in bad.text
            assert not any("\u4e00" <= ch <= "\u9fff" for ch in bad.text)

    asyncio.run(_run())


def test_login_page_cn_host_keeps_phone_copy() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            _client_id, _verifier, login_url = await _register_and_start_login(client)
            page = await client.get(login_url, headers={"Host": "mcp.clawdchat.cn"})
            assert page.status_code == 200
            html = page.text
            assert "使用虾聊账号登录（手机号 / Google）" in html
            assert "或使用 Agent API Key 直接授权" in html
            assert "授权连接" in html

    asyncio.run(_run())


def test_is_cn_site_host_and_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    from clawdchat_mcp.auth_provider import LoginSiteCopy, is_cn_site

    def _request(headers: dict[str, str]) -> Request:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 8347),
        }
        return Request(scope)

    assert is_cn_site(_request({"host": "mcp.clawdchat.ai"})) is False
    assert is_cn_site(_request({"host": "mcp.clawdchat.cn"})) is True
    assert is_cn_site(_request({
        "host": "localhost:8347",
        "x-forwarded-host": "mcp.clawdchat.cn",
    })) is True
    assert is_cn_site(_request({"host": "localhost:8347"})) is False

    monkeypatch.setattr(
        "clawdchat_mcp.auth_provider.settings.clawdchat_api_url",
        "https://clawdchat.cn",
    )
    assert LoginSiteCopy.is_cn(_request({"host": "testserver"})) is True
    assert LoginSiteCopy.is_cn(_request({"host": "mcp.clawdchat.ai"})) is False


def test_post_auth_login_form_json_and_invalid_key() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        profile = AsyncMock(return_value=TEST_PROFILE)

        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            _client_id, _verifier, login_url = await _register_and_start_login(
                client, oauth_state="form-oauth-state"
            )
            login_state = _query(login_url)["state"]

            with patch("clawdchat_mcp.auth_provider.ClawdChatClient.get_my_profile", profile):
                form_resp = await client.post(
                    "/auth/login",
                    data={"state": login_state, "api_key": TEST_API_KEY},
                    follow_redirects=False,
                )
            assert form_resp.status_code == 302, form_resp.text
            location = form_resp.headers["location"]
            query = _query(location)
            assert query.get("code")
            assert query.get("state") == "form-oauth-state"
            assert location.startswith("http://localhost:9876/callback")

            _client_id, _verifier, login_url = await _register_and_start_login(
                client, oauth_state="json-oauth-state"
            )
            login_state = _query(login_url)["state"]
            with patch("clawdchat_mcp.auth_provider.ClawdChatClient.get_my_profile", profile):
                json_resp = await client.post(
                    "/auth/login",
                    json={"state": login_state, "api_key": f"  {TEST_API_KEY}  "},
                    headers={"Accept": "application/json"},
                )
            assert json_resp.status_code == 200, json_resp.text
            body = json_resp.json()
            assert "redirect" in body
            query = _query(body["redirect"])
            assert query.get("code")
            assert query.get("state") == "json-oauth-state"

            _client_id, _verifier, login_url = await _register_and_start_login(client)
            login_state = _query(login_url)["state"]
            failing = AsyncMock(side_effect=ClawdChatAPIError(401, "invalid api key"))
            with patch("clawdchat_mcp.auth_provider.ClawdChatClient.get_my_profile", failing):
                bad_form = await client.post(
                    "/auth/login",
                    data={"state": login_state, "api_key": "clawdchat_test_key_bad"},
                    follow_redirects=False,
                )
                bad_json = await client.post(
                    "/auth/login",
                    json={"state": login_state, "api_key": "clawdchat_test_key_bad"},
                    headers={"Accept": "application/json"},
                )
            assert bad_form.status_code == 400
            assert "API Key" in bad_form.text or "验证失败" in bad_form.text
            assert bad_json.status_code == 400
            assert "error" in bad_json.json()

            missing_pending = await client.post(
                "/auth/login",
                json={"state": "not-a-pending-state", "api_key": TEST_API_KEY},
            )
            assert missing_pending.status_code == 400
            assert "error" in missing_pending.json()

    asyncio.run(_run())


def test_api_key_oauth_session_lists_and_switch_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        from mcp.server.auth.provider import AccessToken

        from clawdchat_mcp import server as server_mod

        mcp = create_mcp_server(transport="streamable-http")
        app = mcp.streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        profile = AsyncMock(return_value=TEST_PROFILE)

        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            client_id, verifier, login_url = await _register_and_start_login(
                client, oauth_state="token-oauth-state"
            )
            login_state = _query(login_url)["state"]
            with patch("clawdchat_mcp.auth_provider.ClawdChatClient.get_my_profile", profile):
                issued = await client.post(
                    "/auth/login",
                    data={"state": login_state, "api_key": TEST_API_KEY},
                    follow_redirects=False,
                )
            assert issued.status_code == 302, issued.text
            code = _query(issued.headers["location"])["code"]

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

        monkeypatch.setattr(
            server_mod,
            "get_access_token",
            lambda: AccessToken(token=access_token, client_id=client_id, scopes=["agent"]),
        )

        listed = _tool_payload(await mcp.call_tool("list_my_agents", {"action": "list"}))
        assert listed["total"] == 1
        assert listed["agents"][0]["id"] == TEST_AGENT_ID
        assert listed["agents"][0]["name"] == TEST_AGENT_NAME
        assert listed["agents"][0]["is_current"] is True

        same = _tool_payload(await mcp.call_tool(
            "switch_agent", {"action": "switch", "agent_id": TEST_AGENT_ID}
        ))
        assert same["success"] is True
        assert TEST_AGENT_NAME in same["message"]

        same_by_name = _tool_payload(await mcp.call_tool(
            "switch_agent", {"action": "switch", "agent_id": TEST_AGENT_NAME}
        ))
        assert same_by_name["success"] is True

        other = _tool_payload(await mcp.call_tool(
            "switch_agent", {"action": "switch", "agent_id": "other-agent-id"}
        ))
        assert other.get("success") is False
        assert "单个 Agent API Key 直接授权" in other["message"]

    asyncio.run(_run())
