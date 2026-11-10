"""Issue #82 v2: search_mode 透传。未传时不发 mode，非法值由工具 schema 拒绝。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from clawdchat_mcp.api_client import ClawdChatAgentClient
from clawdchat_mcp.server import create_mcp_server

VALID_MODES = ("auto", "keyword", "semantic")


def _read_posts_tool():
    mcp = create_mcp_server(transport="streamable-http")
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    return tools["read_posts"]


def _search_mode_schema(tool) -> dict:
    props = (tool.inputSchema or {}).get("properties") or {}
    return props.get("search_mode") or {}


def _enum_values(schema: dict) -> set[str]:
    values: set[str] = set(schema.get("enum") or [])
    for item in schema.get("anyOf") or schema.get("oneOf") or []:
        values.update(item.get("enum") or [])
    return values


@pytest.mark.asyncio
async def test_search_omits_mode_when_none():
    client = ClawdChatAgentClient("https://example.test", "test-key")
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"results": []}

    client._request = fake_request  # type: ignore[method-assign]
    await client.search("非凡大赏", type="posts", limit=5)
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/search"
    assert captured["json"] == {"q": "非凡大赏", "type": "posts", "limit": 5}
    assert "mode" not in captured["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", VALID_MODES)
async def test_search_passes_mode_when_set(mode):
    client = ClawdChatAgentClient("https://example.test", "test-key")
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"results": []}

    client._request = fake_request  # type: ignore[method-assign]
    await client.search("非凡大赏", type="posts", limit=5, mode=mode)
    assert captured["json"]["mode"] == mode
    assert captured["json"]["q"] == "非凡大赏"


def test_read_posts_exposes_search_mode_enum():
    tool = _read_posts_tool()
    schema = json.dumps(tool.inputSchema)
    assert "search_mode" in schema
    assert "search_mode" in (tool.description or "")
    mode_schema = _search_mode_schema(tool)
    enum_values = _enum_values(mode_schema)
    assert set(VALID_MODES) <= enum_values
    assert "hybrid" not in enum_values
    required = (tool.inputSchema or {}).get("required") or []
    assert "search_mode" not in required


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", VALID_MODES)
async def test_read_posts_search_passthrough(mode):
    captured: dict = {}

    class FakeClient:
        async def search(self, q, type="all", limit=20, mode=None):
            captured.update(q=q, type=type, limit=limit, mode=mode)
            return {"results": []}

    mcp = create_mcp_server(transport="streamable-http")
    with patch("clawdchat_mcp.server._get_agent_client", return_value=FakeClient()):
        await mcp.call_tool(
            "read_posts",
            {
                "source": "search",
                "query": "ai创造者大会",
                "search_type": "posts",
                "search_mode": mode,
            },
        )
    assert captured["q"] == "ai创造者大会"
    assert captured["type"] == "posts"
    assert captured["mode"] == mode


@pytest.mark.asyncio
async def test_read_posts_search_omits_mode_when_unset():
    captured: dict = {}

    class FakeClient:
        async def search(self, q, type="all", limit=20, mode=None):
            captured.update(q=q, type=type, limit=limit, mode=mode)
            return {"results": []}

    mcp = create_mcp_server(transport="streamable-http")
    with patch("clawdchat_mcp.server._get_agent_client", return_value=FakeClient()):
        await mcp.call_tool(
            "read_posts",
            {"source": "search", "query": "非凡大赏", "search_type": "posts"},
        )
    assert captured["q"] == "非凡大赏"
    assert captured["mode"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hybrid", "bogus", "AUTO", ""])
async def test_read_posts_rejects_illegal_search_mode(bad):
    mcp = create_mcp_server(transport="streamable-http")
    with pytest.raises((ValidationError, ValueError, Exception)) as exc_info:
        await mcp.call_tool(
            "read_posts",
            {
                "source": "search",
                "query": "非凡大赏",
                "search_mode": bad,
            },
        )
    text = str(exc_info.value).lower()
    assert "search_mode" in text or "literal" in text or "enum" in text or "input" in text
