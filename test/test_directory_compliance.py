"""Claude / OpenAI / Smithery directory compliance tests (offline, no live API)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

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
            assert card["serverInfo"]["version"]
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
