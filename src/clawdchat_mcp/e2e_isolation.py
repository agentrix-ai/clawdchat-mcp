"""Fail-closed when CLAWDCHAT_E2E_ISOLATION is on. Never talk to production."""

from __future__ import annotations

import os
from urllib.parse import urlparse

PRODUCTION_HOSTS = {
    "clawdchat.ai",
    "clawdchat.cn",
    "www.clawdchat.ai",
    "www.clawdchat.cn",
    "mcp.clawdchat.ai",
    "mcp.clawdchat.cn",
}

LOOPBACK = {"127.0.0.1", "localhost", "::1", "api"}


class E2EIsolationError(RuntimeError):
    pass


def isolation_enabled() -> bool:
    return os.environ.get("CLAWDCHAT_E2E_ISOLATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def assert_isolated_api_url(url: str) -> None:
    """No-op unless isolation is on. Then the API host must be loopback."""
    if not isolation_enabled():
        return
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        raise E2EIsolationError("E2E isolation: CLAWDCHAT_API_URL is empty")
    if host in PRODUCTION_HOSTS or host.endswith(".clawdchat.ai") or host.endswith(".clawdchat.cn"):
        raise E2EIsolationError(f"E2E isolation: refuse production API host {host}")
    if host not in LOOPBACK:
        raise E2EIsolationError(
            f"E2E isolation: CLAWDCHAT_API_URL host must be loopback, got {host}"
        )


_client_guards_installed = False


def install_client_guards() -> None:
    """Wrap HTTP clients so a custom base_url cannot bypass isolation."""
    global _client_guards_installed
    if _client_guards_installed:
        return
    from clawdchat_mcp.api_client import ClawdChatAgentClient, ClawdChatUserClient

    orig_user = ClawdChatUserClient.__init__
    orig_agent = ClawdChatAgentClient.__init__

    def user_init(self, base_url: str, jwt_token: str):
        assert_isolated_api_url(base_url)
        orig_user(self, base_url, jwt_token)

    def agent_init(self, base_url: str, api_key: str, on_invalid_key=None):
        assert_isolated_api_url(base_url)
        orig_agent(self, base_url, api_key, on_invalid_key)

    ClawdChatUserClient.__init__ = user_init  # type: ignore[method-assign]
    ClawdChatAgentClient.__init__ = agent_init  # type: ignore[method-assign]
    _client_guards_installed = True
