"""Issue #82 v2: search_mode 透传。mode=None 时不发该字段。"""

from __future__ import annotations

import asyncio
import json

import pytest

from clawdchat_mcp.api_client import ClawdChatAgentClient
from clawdchat_mcp.server import create_mcp_server


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
async def test_search_passes_mode_when_set():
    client = ClawdChatAgentClient("https://example.test", "test-key")
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"results": []}

    client._request = fake_request  # type: ignore[method-assign]
    await client.search("非凡大赏", type="posts", limit=5, mode="keyword")
    assert captured["json"]["mode"] == "keyword"
    assert captured["json"]["q"] == "非凡大赏"


def test_read_posts_exposes_search_mode():
    mcp = create_mcp_server(transport="streamable-http")
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    tool = tools["read_posts"]
    schema = json.dumps(tool.inputSchema)
    assert "search_mode" in schema
    assert "search_mode" in (tool.description or "")
