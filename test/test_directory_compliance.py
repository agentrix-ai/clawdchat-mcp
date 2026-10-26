"""Claude / OpenAI / Smithery directory compliance tests (offline, no live API)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
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

HTTP_TOOL_NAMES = frozenset(READ_TOOLS | WRITE_TOOLS)
ALL_TOOL_IDENTIFIERS = frozenset(HTTP_TOOL_NAMES | {"authenticate"})
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MY_STATUS_UPDATE_DATA_DESCRIPTION = (
    "Profile fields to update for update_profile: name (unique lowercase "
    "alphanumeric/hyphen handle, 2-50 chars, up to 3 renames), display_name "
    "(display name supporting Unicode and emoji), description (bio text), "
    "or avatar_url."
)
SWITCH_AGENT_CONFIRM_RESET_DESCRIPTION = (
    "Set to true to generate and bind a new API key when the target agent "
    "does not have a stored key."
)


def _tool_as_dict(tool: Any) -> dict[str, Any]:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        annotations_dict: dict[str, Any] = {}
    elif hasattr(annotations, "model_dump"):
        annotations_dict = annotations.model_dump(exclude_none=False)
    else:
        annotations_dict = dict(annotations)
    return {
        "name": tool.name,
        "title": getattr(tool, "title", None) or annotations_dict.get("title") or tool.name,
        "description": getattr(tool, "description", None) or "",
        "inputSchema": getattr(tool, "inputSchema", None) or {},
        "annotations": annotations_dict,
    }


def _assert_no_cjk_or_cross_tool_names(tools: list[dict[str, Any]], *, expected_count: int) -> None:
    assert len(tools) == expected_count, f"expected {expected_count} tools, got {len(tools)}"
    for tool in tools:
        meta_text = "\n".join(
            [
                str(tool.get("title") or ""),
                str(tool.get("description") or ""),
                json.dumps(tool.get("inputSchema") or {}, ensure_ascii=False),
            ]
        )
        assert CJK_RE.search(meta_text) is None, (
            f"{tool['name']} title/description/inputSchema contains CJK: {meta_text}"
        )
        dumped = json.dumps(tool, ensure_ascii=False)
        for other_name in ALL_TOOL_IDENTIFIERS:
            if other_name == tool["name"]:
                continue
            assert re.search(rf"\b{re.escape(other_name)}\b", dumped) is None, (
                f"{tool['name']} metadata mentions other tool {other_name}: {dumped}"
            )


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


def _my_status_docs(tool) -> str:
    schema = tool.inputSchema or {}
    props = schema.get("properties") or {}
    update_data = props.get("update_data") or {}
    return "\n".join(
        [
            tool.description or "",
            json.dumps(update_data, ensure_ascii=False),
            str(update_data.get("description") or ""),
        ]
    )


def test_my_status_documents_name_and_display_name() -> None:
    server = create_mcp_server(transport="streamable-http")

    async def _list():
        return await server.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(_list())}
    docs = _my_status_docs(tools["my_status"])
    assert "name" in docs
    assert "display_name" in docs
    assert "update_profile" in docs
    assert "2-50" in docs
    assert "Unicode" in docs or "emoji" in docs.lower()
    update_data = ((tools["my_status"].inputSchema or {}).get("properties") or {}).get("update_data") or {}
    assert update_data.get("description") == MY_STATUS_UPDATE_DATA_DESCRIPTION


def test_http_tools_list_is_english_without_cross_tool_names() -> None:
    server = create_mcp_server(transport="streamable-http")

    async def _list():
        return await server.list_tools()

    tools = [_tool_as_dict(tool) for tool in asyncio.run(_list())]
    names = {tool["name"] for tool in tools}
    assert names == HTTP_TOOL_NAMES
    _assert_no_cjk_or_cross_tool_names(tools, expected_count=17)

    switch = next(tool for tool in tools if tool["name"] == "switch_agent")
    confirm = ((switch.get("inputSchema") or {}).get("properties") or {}).get("confirm_reset") or {}
    assert confirm.get("description") == SWITCH_AGENT_CONFIRM_RESET_DESCRIPTION


def test_stdio_authenticate_is_english_without_cross_tool_names() -> None:
    server = create_mcp_server(transport="stdio")

    async def _list():
        return await server.list_tools()

    tools = [_tool_as_dict(tool) for tool in asyncio.run(_list())]
    auth = next(tool for tool in tools if tool["name"] == "authenticate")
    _assert_no_cjk_or_cross_tool_names([auth], expected_count=1)


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
            assert card["serverInfo"]["version"] == "0.1.15"
            assert card["serverInfo"]["websiteUrl"]
            assert card["serverInfo"]["icons"]
            assert card["authentication"]["type"] == "oauth2"
            assert "oauth2" in card["authentication"]["schemes"]
            assert "bearer" in card["authentication"]["schemes"]
            names = {tool["name"] for tool in card["tools"]}
            assert READ_TOOLS <= names
            assert WRITE_TOOLS <= names
            my_status = next(tool for tool in card["tools"] if tool["name"] == "my_status")
            card_docs = (my_status.get("description") or "") + json.dumps(
                (my_status.get("inputSchema") or {}).get("properties", {}).get("update_data") or {},
                ensure_ascii=False,
            )
            assert "name" in card_docs
            assert "display_name" in card_docs
            assert "2-50" in card_docs
            for tool in card["tools"]:
                assert tool["title"]
                assert tool["description"]
                assert tool["inputSchema"]
                annotations = tool["annotations"]
                for key in ANNOTATION_KEYS:
                    assert isinstance(annotations[key], bool)
            _assert_no_cjk_or_cross_tool_names(card["tools"], expected_count=17)

            listed = [_tool_as_dict(tool) for tool in await create_mcp_server(transport="streamable-http").list_tools()]
            listed_by_name = {tool["name"]: tool for tool in listed}
            for card_tool in card["tools"]:
                listed_tool = listed_by_name[card_tool["name"]]
                assert card_tool["title"] == listed_tool["title"]
                assert card_tool["description"] == listed_tool["description"]
                assert card_tool["inputSchema"] == listed_tool["inputSchema"]

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


EXPECTED_VERSION = "0.1.15"
TEST_API_KEY = "clawdchat_test_key_123"
TEST_AGENT_ID = "agent-reviewer-1"
TEST_AGENT_NAME = "ReviewerAgent"
TEST_PROFILE = {"id": TEST_AGENT_ID, "name": TEST_AGENT_NAME}


def test_version_is_0_1_15() -> None:
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
        assert "single Agent API Key" in other["message"]

    asyncio.run(_run())


MCP_ACCEPT = "application/json, text/event-stream"


def _mcp_jsonrpc(method: str, *, rpc_id: int | str | None = 1, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        payload["id"] = rpc_id
    if params is not None:
        payload["params"] = params
    return payload


def _parse_sse_or_json(body: bytes, content_type: str) -> dict[str, Any] | None:
    text = body.decode("utf-8", errors="replace")
    if "application/json" in content_type:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    for line in text.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


async def _asgi_post(
    app,
    path: str,
    payload: dict[str, Any] | list[Any] | None,
    *,
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """POST through ASGI without closing the request until the response finishes.

    Streamable HTTP uses SSE: disconnecting immediately after the body aborts the
    stream before the JSON-RPC result is written.
    """
    body = raw_body if raw_body is not None else json.dumps(payload if payload is not None else {}).encode("utf-8")
    header_pairs = [
        (b"host", b"localhost:8347"),
        (b"accept", MCP_ACCEPT.encode("latin-1")),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for key, value in (headers or {}).items():
        header_pairs.append((key.lower().encode("latin-1"), value.encode("latin-1")))

    sent_body = False
    status = 0
    response_headers: dict[str, str] = {}
    chunks: list[bytes] = []
    finished = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
            response_headers.update(
                {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            )
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                finished.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": header_pairs,
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 8347),
        },
        receive,
        send,
    )
    if not finished.is_set():
        finished.set()
    return status, response_headers, b"".join(chunks)


@asynccontextmanager
async def _mcp_app():
    mcp = create_mcp_server(transport="streamable-http")
    app = mcp.streamable_http_app()
    # SSE EventSourceResponse does not finish under a plain ASGI test client.
    # JSON mode still exercises the same auth middleware and tool schemas.
    mcp.session_manager.json_response = True
    async with mcp.session_manager.run():
        yield app


def test_anonymous_mcp_discovery_methods_return_200() -> None:
    async def _run() -> None:
        async with _mcp_app() as app:
            status, headers, body = await _asgi_post(
                app,
                "/mcp",
                _mcp_jsonrpc(
                    "initialize",
                    params={
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "directory-crawler", "version": "1.0"},
                    },
                ),
            )
            assert status == 200, body
            session_id = headers.get("mcp-session-id")
            assert session_id
            parsed = _parse_sse_or_json(body, headers.get("content-type", ""))
            assert parsed is not None
            assert parsed.get("result") or parsed.get("jsonrpc") == "2.0"

            session_headers = {"mcp-session-id": session_id}
            initialized_status, _, _ = await _asgi_post(
                app,
                "/mcp",
                _mcp_jsonrpc("notifications/initialized", rpc_id=None),
                headers=session_headers,
            )
            assert initialized_status in (200, 202)

            for method in ("ping", "tools/list", "prompts/list", "resources/list"):
                status, headers, body = await _asgi_post(
                    app,
                    "/mcp",
                    _mcp_jsonrpc(method, rpc_id=method),
                    headers=session_headers,
                )
                assert status == 200, f"{method}: {status} {body!r}"
                if method == "tools/list":
                    payload = _parse_sse_or_json(body, headers.get("content-type", ""))
                    assert payload is not None
                    tools = (payload.get("result") or {}).get("tools") or []
                    names = {tool.get("name") for tool in tools}
                    assert HTTP_TOOL_NAMES <= names
                    _assert_no_cjk_or_cross_tool_names(tools, expected_count=17)

    asyncio.run(_run())


def test_anonymous_or_invalid_token_protected_methods_return_401() -> None:
    async def _run() -> None:
        app = _http_app()
        call_status, call_headers, _ = await _asgi_post(
            app,
            "/mcp",
            _mcp_jsonrpc("tools/call", params={"name": "read_posts", "arguments": {"source": "feed"}}),
        )
        assert call_status == 401
        assert "resource_metadata=" in call_headers.get("www-authenticate", "")
        assert "oauth-protected-resource" in call_headers["www-authenticate"]

        prompt_status, prompt_headers, _ = await _asgi_post(
            app,
            "/mcp",
            _mcp_jsonrpc("prompts/get", params={"name": "daily_summary"}),
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert prompt_status == 401
        assert 'Bearer resource_metadata="' in prompt_headers.get("www-authenticate", "")
        assert "oauth-protected-resource" in prompt_headers["www-authenticate"]

        hosted_status, hosted_headers, _ = await _asgi_post(
            app,
            "/mcp",
            _mcp_jsonrpc("tools/call", params={"name": "read_posts", "arguments": {}}),
            headers={"Host": "mcp.clawdchat.ai"},
        )
        assert hosted_status == 401
        assert (
            hosted_headers["www-authenticate"]
            == 'Bearer resource_metadata="https://mcp.clawdchat.ai/.well-known/oauth-protected-resource"'
        )

        async with _mcp_app() as live_app:
            started_status, started_headers, _ = await _asgi_post(
                live_app,
                "/mcp",
                _mcp_jsonrpc(
                    "initialize",
                    params={
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "directory-crawler", "version": "1.0"},
                    },
                ),
            )
            assert started_status == 200
            session_id = started_headers.get("mcp-session-id")
            assert session_id
            same_session_status, same_session_headers, _ = await _asgi_post(
                live_app,
                "/mcp",
                _mcp_jsonrpc("tools/call", params={"name": "read_posts", "arguments": {}}),
                headers={"mcp-session-id": session_id},
            )
            assert same_session_status == 401
            assert "oauth-protected-resource" in same_session_headers.get("www-authenticate", "")

    asyncio.run(_run())


def test_register_never_returns_500_for_partial_or_invalid_bodies() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            cases = [
                {
                    "redirect_uris": ["http://localhost:9876/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "client_name": "directory-compliance-test",
                },
                {"client_name": "coda-agent-runtime"},
                {},
                {"client_name": "extra-fields", "unknown_field": True, "redirect_uris": "https://example.com/cb"},
            ]
            for payload in cases:
                response = await client.post("/register", json=payload)
                assert response.status_code != 500, payload
                assert response.status_code in (200, 201, 400), (payload, response.status_code, response.text)
                if response.status_code in (200, 201):
                    body = response.json()
                    assert body.get("client_id")
                else:
                    assert response.json().get("error") == "invalid_client_metadata"

            empty = await client.post("/register", content=b"", headers={"Content-Type": "application/json"})
            assert empty.status_code != 500
            assert empty.status_code in (200, 201, 400)

            invalid = await client.post(
                "/register",
                content=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"] == "invalid_client_metadata"

    asyncio.run(_run())


def test_public_discovery_aliases_and_robots_txt() -> None:
    async def _run() -> None:
        app = _http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            for path in (
                "/.well-known/mcp/server-card.json",
                "/.well-known/agent-card.json",
                "/.well-known/agent.json",
                "/.well-known/mcp.json",
            ):
                response = await client.get(path)
                assert response.status_code == 200, path
                assert response.headers.get("access-control-allow-origin") == "*"
                body = response.json()
                assert body["serverInfo"]["name"] == "ClawdChat"
                assert body["serverInfo"]["version"] == "0.1.15"
                assert {tool["name"] for tool in body["tools"]} >= HTTP_TOOL_NAMES
                options = await client.options(path)
                assert options.status_code in (200, 204)

            robots = await client.get("/robots.txt")
            assert robots.status_code == 200
            assert robots.headers["content-type"].startswith("text/plain")
            text = robots.text
            for agent in (
                "OAI-SearchBot",
                "ClaudeBot",
                "GPTBot",
                "PerplexityBot",
                "Googlebot",
                "*",
            ):
                assert f"User-agent: {agent}" in text
            assert "Allow: /" in text

    asyncio.run(_run())


def test_replay_receive_delegates_followup_to_original_receive() -> None:
    from clawdchat_mcp.anon_discovery import replay_receive

    async def _run() -> None:
        calls = {"n": 0}

        async def orig_receive() -> dict[str, Any]:
            calls["n"] += 1
            return {"type": "http.disconnect", "from": "orig"}

        receive = replay_receive(b'{"jsonrpc":"2.0","method":"initialize","id":1}', orig_receive)
        first = await receive()
        assert first["type"] == "http.request"
        assert first["more_body"] is False
        assert first["body"].startswith(b'{"jsonrpc"')
        second = await receive()
        assert second == {"type": "http.disconnect", "from": "orig"}
        assert calls["n"] == 1
        third = await receive()
        assert third == {"type": "http.disconnect", "from": "orig"}
        assert calls["n"] == 2

    asyncio.run(_run())


def test_anonymous_session_upgraded_tools_call_uses_bearer_token() -> None:
    """Anonymous initialize + later Bearer tools/call must see the access token."""

    async def _run() -> None:
        from mcp.server.auth.provider import AccessToken

        from clawdchat_mcp import server as server_mod

        mcp = create_mcp_server(transport="streamable-http")
        app = mcp.streamable_http_app()
        mcp.session_manager.json_response = True
        seen: list[AccessToken | None] = []
        real_get = server_mod.get_access_token

        def _spy_get_access_token() -> AccessToken | None:
            token = real_get()
            seen.append(token)
            return token

        server_mod.get_access_token = _spy_get_access_token  # type: ignore[method-assign]
        profile = AsyncMock(return_value=TEST_PROFILE)
        try:
            async with mcp.session_manager.run():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
                    client_id, verifier, login_url = await _register_and_start_login(
                        client, oauth_state="upgrade-oauth-state"
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

                init_status, init_headers, _ = await _asgi_post(
                    app,
                    "/mcp",
                    _mcp_jsonrpc(
                        "initialize",
                        params={
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "directory-crawler", "version": "1.0"},
                        },
                    ),
                )
                assert init_status == 200
                session_id = init_headers.get("mcp-session-id")
                assert session_id

                initialized_status, _, _ = await _asgi_post(
                    app,
                    "/mcp",
                    _mcp_jsonrpc("notifications/initialized", rpc_id=None),
                    headers={"mcp-session-id": session_id},
                )
                assert initialized_status in (200, 202)

                seen.clear()
                call_status, call_headers, call_body = await _asgi_post(
                    app,
                    "/mcp",
                    _mcp_jsonrpc(
                        "tools/call",
                        params={"name": "list_my_agents", "arguments": {"action": "list"}},
                    ),
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "mcp-session-id": session_id,
                    },
                )
                assert call_status == 200, call_body
                payload = _parse_sse_or_json(call_body, call_headers.get("content-type", ""))
                assert payload is not None
                assert payload.get("error") is None
                result = payload.get("result") or {}
                assert result.get("isError") is not True
                texts = [
                    block.get("text", "")
                    for block in (result.get("content") or [])
                    if isinstance(block, dict)
                ]
                joined = "\n".join(texts)
                assert "not signed in" not in joined.lower()
                assert "Open this URL" not in joined
                assert TEST_AGENT_ID in joined
                assert TEST_AGENT_NAME in joined
                assert seen, "list_my_agents must call get_access_token()"
                token_obj = next((item for item in seen if item is not None), None)
                assert token_obj is not None
                assert token_obj.token == access_token
        finally:
            server_mod.get_access_token = real_get  # type: ignore[method-assign]

    asyncio.run(_run())
