"""MCP Events (draft SEP-3415, ChatGPT webhook delivery) for ClawdChat.

v1 ships one event: ``dm.message_received``. Gated by ``MCP_EVENTS_ENABLED``.
See https://developers.openai.com/plugins/build/mcp-events
"""

from ..config import settings


def events_enabled() -> bool:
    return bool(settings.mcp_events_enabled and settings.mcp_events_secret_key)
