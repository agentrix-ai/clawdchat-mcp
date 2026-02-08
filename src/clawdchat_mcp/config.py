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

    @property
    def mcp_endpoint(self) -> str:
        """Full MCP endpoint URL."""
        return f"{self.mcp_server_url}/mcp"


settings = Settings()
