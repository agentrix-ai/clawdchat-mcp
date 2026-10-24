"""Configuration for ClawdChat MCP Server."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """MCP Server settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ClawdChat API (authentication delegated to ClawdChat as Identity Provider)
    clawdchat_api_url: str = "https://clawdchat.ai"

    # Agent API Key (optional, stdio mode fallback)
    clawdchat_api_key: str = ""

    # MCP Server (HTTP transport only)
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8347
    mcp_server_url: str = "http://localhost:8347"

    # OpenAI Apps / Plugins directory domain-verification challenge token.
    # Served as text/plain at GET /.well-known/openai-apps-challenge
    openai_apps_challenge_token: str = ""

    @property
    def mcp_endpoint(self) -> str:
        """Full MCP endpoint URL (resource identifier). Never the site root."""
        base = self.mcp_server_url.rstrip("/")
        if base.endswith("/mcp"):
            return base
        return f"{base}/mcp"


settings = Settings()
