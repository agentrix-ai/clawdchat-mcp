"""OAuth discovery metadata compatibility tests."""

import asyncio

import httpx

from clawdchat_mcp.server import create_mcp_server


def test_authorization_server_metadata_advertises_public_clients() -> None:
    """WorkBuddy-style native clients must be able to discover DCR + PKCE support."""

    async def fetch_metadata() -> dict:
        app = create_mcp_server(transport="streamable-http").streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            response = await client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        return response.json()

    metadata = asyncio.run(fetch_metadata())

    assert metadata["registration_endpoint"] == "http://localhost:8347/register"
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert "none" in metadata["token_endpoint_auth_methods_supported"]


def test_protected_resource_metadata_uses_mcp_path() -> None:
    """RFC 9728 resource must be the Streamable HTTP endpoint, not the site root."""

    async def fetch_resource() -> dict:
        app = create_mcp_server(transport="streamable-http").streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8347") as client:
            response = await client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        return response.json()

    metadata = asyncio.run(fetch_resource())
    assert metadata["resource"] == "http://localhost:8347/mcp"
