"""Official-client OAuth redirect URI allowlist (Claude + ChatGPT / Codex).

DCR still accepts arbitrary client-provided URIs so existing MCP clients keep
working. Authorize-time validation additionally permits the official Claude
and ChatGPT callback patterns, including localhost loopback ports and
ChatGPT connector callback IDs.
"""

from __future__ import annotations

import re
from pydantic import AnyUrl

from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull

CLAUDE_CALLBACK_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

CHATGPT_PLATFORM_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_CONNECTOR_OAUTH_PREFIX = "https://chatgpt.com/connector/oauth/"

# http://localhost:<port>/callback and http://127.0.0.1:<port>/callback
# (port optional so http://localhost/callback also works).
_LOOPBACK_CALLBACK_RE = re.compile(
    r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?/callback/?$",
    re.IGNORECASE,
)
_CHATGPT_CONNECTOR_RE = re.compile(
    r"^https://chatgpt\.com/connector/oauth/[^/?#]+/?$",
    re.IGNORECASE,
)
_CHATGPT_CONNECTOR_WILDCARD = "https://chatgpt.com/connector/oauth/*"


def normalize_redirect_uri(uri: str) -> str:
    """Strip whitespace and a single trailing slash (except for root paths)."""
    value = (uri or "").strip()
    if len(value) > 1 and value.endswith("/"):
        return value[:-1]
    return value


def is_allowed_oauth_redirect_uri(uri: str) -> bool:
    """Return True if uri is an official Claude / ChatGPT / loopback callback."""
    value = normalize_redirect_uri(uri)
    if value in CLAUDE_CALLBACK_URIS or value == CHATGPT_PLATFORM_REDIRECT:
        return True
    if value == _CHATGPT_CONNECTOR_WILDCARD.rstrip("/") or value == _CHATGPT_CONNECTOR_WILDCARD:
        return True
    if _LOOPBACK_CALLBACK_RE.match(value):
        return True
    if _CHATGPT_CONNECTOR_RE.match(value):
        return True
    return False


def describe_allowed_oauth_redirects() -> list[str]:
    """Human-readable list of allowlisted callback patterns (for tests / docs)."""
    return [
        *CLAUDE_CALLBACK_URIS,
        "http://localhost:<port>/callback",
        "http://127.0.0.1:<port>/callback",
        CHATGPT_PLATFORM_REDIRECT,
        f"{CHATGPT_CONNECTOR_OAUTH_PREFIX}*",
    ]


class AllowlistedOAuthClient(OAuthClientInformationFull):
    """OAuth client that also accepts official Claude / ChatGPT callback URIs."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and is_allowed_oauth_redirect_uri(str(redirect_uri)):
            return redirect_uri
        try:
            return super().validate_redirect_uri(redirect_uri)
        except InvalidRedirectUriError:
            if redirect_uri is not None and is_allowed_oauth_redirect_uri(str(redirect_uri)):
                return redirect_uri
            raise
