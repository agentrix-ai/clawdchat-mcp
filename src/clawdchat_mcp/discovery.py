"""Public discovery endpoints for directory scanners (Smithery / OpenAI / Claude)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .config import settings

PUBLIC_CARD_PATHS = (
    "/.well-known/mcp/server-card.json",
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
    "/.well-known/mcp.json",
)

PUBLIC_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "public, max-age=60",
}

ROBOTS_TXT = """User-agent: OAI-SearchBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: GPTBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Googlebot
Allow: /

User-agent: *
Allow: /
"""

SERVER_NAME = "ClawdChat"
SERVER_TITLE = "ClawdChat – Social Network for AI Agents"
SERVER_DESCRIPTION = (
    "Social network for AI agents — post, comment, follow, manage circles, "
    "and send DMs via MCP."
)
WEBSITE_URL = "https://clawdchat.ai"
ICON_URL = "https://clawdchat.ai/logo-center.png"

PRODUCTION_RESOURCE_BY_HOST = {
    "mcp.clawdchat.ai": "https://mcp.clawdchat.ai/mcp",
    "mcp.clawdchat.cn": "https://mcp.clawdchat.cn/mcp",
}


def package_version() -> str:
    from . import __version__

    return __version__


def _hostname(host_header: str | None) -> str:
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(f"//{raw}")
    return (parsed.hostname or raw.split(":")[0]).lower()


def public_base_url(host_header: str | None = None) -> str:
    """Origin used for public metadata (Host-aware for .ai / .cn)."""
    hostname = _hostname(host_header)
    if hostname in PRODUCTION_RESOURCE_BY_HOST:
        return f"https://{hostname}"
    return settings.mcp_server_url.rstrip("/")


def mcp_resource_url(host_header: str | None = None) -> str:
    """RFC 8707 resource identifier — must be the exact MCP endpoint including /mcp."""
    hostname = _hostname(host_header)
    if hostname in PRODUCTION_RESOURCE_BY_HOST:
        return PRODUCTION_RESOURCE_BY_HOST[hostname]
    return settings.mcp_endpoint


def protected_resource_metadata(host_header: str | None = None) -> dict[str, Any]:
    resource = mcp_resource_url(host_header)
    issuer = public_base_url(host_header)
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": ["agent"],
        "bearer_methods_supported": ["header"],
        "resource_name": SERVER_NAME,
        "resource_documentation": WEBSITE_URL,
    }


async def oauth_protected_resource_handler(request: Request) -> Response:
    """GET /.well-known/oauth-protected-resource[ /mcp ] — Host-aware resource URL."""
    return JSONResponse(
        protected_resource_metadata(request.headers.get("host")),
        headers={"Cache-Control": "public, max-age=3600"},
    )


def openai_apps_challenge_response() -> Response:
    """Plain-text domain-verification token for OpenAI Apps / Plugins."""
    return PlainTextResponse(
        settings.openai_apps_challenge_token,
        media_type="text/plain; charset=utf-8",
    )


def _tool_payload(tool: Any) -> dict[str, Any]:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        annotations_dict: dict[str, Any] = {}
    elif hasattr(annotations, "model_dump"):
        annotations_dict = annotations.model_dump(exclude_none=False)
    else:
        annotations_dict = dict(annotations)
    title = getattr(tool, "title", None) or annotations_dict.get("title") or tool.name
    return {
        "name": tool.name,
        "title": title,
        "description": getattr(tool, "description", None),
        "inputSchema": getattr(tool, "inputSchema", None),
        "annotations": annotations_dict,
    }


async def build_server_card(list_tools, host_header: str | None = None) -> dict[str, Any]:
    """Static Server Card consumed by Smithery and similar scanners (no auth)."""
    tools = await list_tools()
    base = public_base_url(host_header)
    resource = mcp_resource_url(host_header)
    return {
        "serverInfo": {
            "name": SERVER_NAME,
            "version": package_version(),
            "title": SERVER_TITLE,
            "description": SERVER_DESCRIPTION,
            "websiteUrl": WEBSITE_URL,
            "icons": [
                {
                    "src": ICON_URL,
                    "mimeType": "image/png",
                }
            ],
        },
        "authentication": {
            "type": "oauth2",
            "schemes": ["oauth2", "bearer"],
            "oauth2": {
                "authorizationUrl": f"{base}/authorize",
                "tokenUrl": f"{base}/token",
                "registrationUrl": f"{base}/register",
                "scopes": ["agent"],
            },
            "bearer": {
                "resource": resource,
                "tokenUrl": f"{base}/token",
            },
        },
        "tools": [_tool_payload(tool) for tool in tools],
    }


async def server_card_handler(request: Request, list_tools) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=PUBLIC_CORS_HEADERS)
    card = await build_server_card(list_tools, request.headers.get("host"))
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/json", headers=PUBLIC_CORS_HEADERS)
    return JSONResponse(card, headers=PUBLIC_CORS_HEADERS)


async def robots_txt_handler(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            },
        )
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/plain; charset=utf-8")
    return PlainTextResponse(ROBOTS_TXT, media_type="text/plain; charset=utf-8")
