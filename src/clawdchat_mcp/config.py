"""Configuration for ClawdChat MCP Server."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """MCP Server settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ClawdChat API
    clawdchat_api_url: str = "http://localhost:8081"

    # MCP Server
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8000
    mcp_server_url: str = "http://localhost:8000"

    # Google OAuth (共享 ClawdChat 的 Google OAuth App)
    google_client_id: str = ""
    google_client_secret: str = ""

    @property
    def mcp_endpoint(self) -> str:
        """Full MCP endpoint URL."""
        return f"{self.mcp_server_url}/mcp"

    @property
    def google_redirect_uri(self) -> str:
        """Google OAuth callback URL for MCP server."""
        return f"{self.mcp_server_url}/auth/google/callback"

    @property
    def google_enabled(self) -> bool:
        """Whether Google OAuth is configured."""
        return bool(self.google_client_id and self.google_client_secret)


settings = Settings()
