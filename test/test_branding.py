"""ClawdChat branding: serverInfo icons, OAuth logo_uri, logo/favicon routes, login page logo."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from clawdchat_mcp.discovery import ICON_URL, WEBSITE_URL, logo_bytes
from clawdchat_mcp.server import create_mcp_server

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PLACEHOLDER_PATH = "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4"


async def _get(path: str, host: str = "localhost:8347") -> httpx.Response:
    app = create_mcp_server(transport="streamable-http").streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8347"
    ) as client:
        return await client.get(path, headers={"Host": host})


@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
def test_initialize_server_info_has_icons_and_website(transport: str) -> None:
    mcp = create_mcp_server(transport=transport)
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_name == "ClawdChat"
    assert opts.website_url == WEBSITE_URL
    assert opts.icons and opts.icons[0].src == ICON_URL
    assert opts.icons[0].mimeType == "image/png"


def test_bundled_logo_is_png() -> None:
    assert logo_bytes().startswith(PNG_MAGIC)


@pytest.mark.parametrize("path", ["/logo.png", "/favicon.png", "/favicon.ico"])
@pytest.mark.parametrize("host", ["mcp.clawdchat.ai", "mcp.clawdchat.cn"])
def test_logo_routes_public_png(path: str, host: str) -> None:
    resp = asyncio.run(_get(path, host))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(PNG_MAGIC)


def test_oauth_metadata_advertises_logo_uri() -> None:
    resp = asyncio.run(_get("/.well-known/oauth-authorization-server"))
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["logo_uri"].endswith("/logo.png")
    assert doc["logo_uri"].startswith(doc["issuer"].rstrip("/"))
    assert doc["service_documentation"] == WEBSITE_URL
    # existing fields kept
    assert "none" in doc["token_endpoint_auth_methods_supported"]
    assert doc["registration_endpoint"].endswith("/register")


@pytest.mark.parametrize("name", ["login.html", "select_agent.html"])
def test_templates_use_clawdchat_logo_not_placeholder(name: str) -> None:
    from pathlib import Path

    import clawdchat_mcp

    html = (Path(clawdchat_mcp.__file__).parent / "templates" / name).read_text()
    assert '<img src="/logo.png" alt="ClawdChat"' in html
    assert 'rel="icon"' in html
    assert PLACEHOLDER_PATH not in html
    assert "#6366f1, #8b5cf6" not in html


def test_stdio_auth_page_uses_logo() -> None:
    from pathlib import Path

    import clawdchat_mcp

    src = (Path(clawdchat_mcp.__file__).parent / "stdio_auth.py").read_text()
    assert ICON_URL in src
    assert PLACEHOLDER_PATH not in src


def test_bundled_logo_has_transparent_corners_no_baked_frame() -> None:
    import io

    Image = pytest.importorskip("PIL.Image")

    im = Image.open(io.BytesIO(logo_bytes())).convert("RGBA")
    assert im.size == (256, 256)
    for xy in [(0, 0), (255, 0), (0, 255), (255, 255)]:
        assert im.getpixel(xy)[3] == 0
    assert im.getpixel((128, 128))[3] == 255
    # the official tile's dark outer frame (x≈6-8) must be cropped away
    r, g, b, _ = im.getpixel((8, 128))
    assert r + g + b > 330


@pytest.mark.parametrize("name", ["login.html", "select_agent.html"])
def test_logo_container_has_no_frame(name: str) -> None:
    import re
    from pathlib import Path

    import clawdchat_mcp

    html = (Path(clawdchat_mcp.__file__).parent / "templates" / name).read_text()
    rule = re.search(r"\.auth-logo \{(.*?)\}", html, re.S).group(1)
    assert "border: none" in rule and "background: none" in rule and "box-shadow: none" in rule
    assert "border-radius" not in rule and "overflow" not in rule
