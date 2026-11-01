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

    # MCP Events (ChatGPT webhook push). Default OFF.
    mcp_events_enabled: bool = False
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt subscription signing
    # secrets at rest in Redis. Required when events are enabled.
    mcp_events_secret_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    mcp_events_stream_key: str = "dm:events"
    # One consumer group + key prefix per instance (.ai / .cn run separately).
    mcp_events_instance: str = "mcp-ai"
    mcp_events_default_ttl_ms: int = 24 * 3600 * 1000
    mcp_events_max_ttl_ms: int = 7 * 24 * 3600 * 1000
    mcp_events_min_ttl_ms: int = 60 * 1000
    # Optional egress proxy used ONLY by the outbound MCP Events webhook client
    # (callback verification + event delivery). Empty = direct (default).
    # e.g. "socks5h://127.0.0.1:40000" (Cloudflare WARP proxy mode). Never logged.
    mcp_events_webhook_proxy: str = ""

    @property
    def mcp_endpoint(self) -> str:
        """Full MCP endpoint URL (resource identifier). Never the site root."""
        base = self.mcp_server_url.rstrip("/")
        if base.endswith("/mcp"):
            return base
        return f"{base}/mcp"


settings = Settings()
