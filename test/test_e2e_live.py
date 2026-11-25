"""Optional live MCP client against an isolated ClawdChat API.

Hosted MCP OAuth is NOT accepted here (no dedicated OAuth session).
This only exercises ClawdChatAgentClient HTTP against loopback when the
main-site harness has already created a synthetic agent key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from clawdchat_mcp.e2e_isolation import isolation_enabled

SEED = Path(os.environ.get("CLAWDCHAT_E2E_SEED", ""))


def test_live_mcp_client_against_isolated_api_or_declare_untested():
    if not isolation_enabled():
        pytest.skip("CLAWDCHAT_E2E_ISOLATION not set")
    api = os.environ.get("CLAWDCHAT_API_URL", "http://127.0.0.1:18081")
    try:
        health = httpx.get(f"{api.rstrip('/')}/health", timeout=5).json()
    except httpx.HTTPError as exc:
        pytest.skip(f"isolated API not up: {exc}")
    if not health.get("e2e", {}).get("isolation"):
        pytest.fail("CLAWDCHAT_API_URL is not an isolated E2E process")
    key = os.environ.get("CLAWDCHAT_E2E_AGENT_KEY", "")
    if not key and SEED and SEED.exists():
        key = json.loads(SEED.read_text()).get("agent_key") or ""
    if not key:
        Path("/tmp/clawdchat-mcp-e2e-untested.json").write_text(
            json.dumps(
                {
                    "hosted_mcp_oauth": "untested — no dedicated OAuth session",
                    "agent_client_live": "skipped — no synthetic agent key in env",
                    "api": api,
                    "isolation": True,
                },
                indent=2,
            )
            + "\n"
        )
        pytest.skip("MCP-only OAuth untested; no CLAWDCHAT_E2E_AGENT_KEY for HTTP client")
    from clawdchat_mcp.api_client import ClawdChatAgentClient
    import asyncio

    async def _status():
        client = ClawdChatAgentClient(api, key)
        return await client.get_status()

    body = asyncio.run(_status())
    assert body
