"""MCP fail-closed isolation. Never hits production."""

from __future__ import annotations

import os

import pytest

from clawdchat_mcp.e2e_isolation import (
    E2EIsolationError,
    assert_isolated_api_url,
    isolation_enabled,
)


def test_isolation_off_allows_default_url(monkeypatch):
    monkeypatch.delenv("CLAWDCHAT_E2E_ISOLATION", raising=False)
    assert isolation_enabled() is False
    assert_isolated_api_url("https://clawdchat.ai")  # no-op


def test_isolation_on_refuses_production(monkeypatch):
    monkeypatch.setenv("CLAWDCHAT_E2E_ISOLATION", "1")
    with pytest.raises(E2EIsolationError):
        assert_isolated_api_url("https://clawdchat.ai")
    with pytest.raises(E2EIsolationError):
        assert_isolated_api_url("https://clawdchat.cn/api")
    with pytest.raises(E2EIsolationError):
        assert_isolated_api_url("https://mcp.clawdchat.ai/mcp")
    with pytest.raises(E2EIsolationError):
        assert_isolated_api_url("https://example.com")
    assert_isolated_api_url("http://127.0.0.1:18081")
    assert_isolated_api_url("http://localhost:18081")


def test_agent_client_refuses_prod_when_isolated(monkeypatch):
    monkeypatch.setenv("CLAWDCHAT_E2E_ISOLATION", "true")
    from clawdchat_mcp.api_client import ClawdChatAgentClient

    with pytest.raises(E2EIsolationError):
        ClawdChatAgentClient("https://clawdchat.ai", "clawdchat_fake")
    client = ClawdChatAgentClient("http://127.0.0.1:18081", "clawdchat_fake")
    assert client.base_url.endswith("18081")
