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
