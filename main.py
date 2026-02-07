"""ClawdChat MCP Server - Entry point.

Usage:
    uv run main.py
"""

import logging

from clawdchat_mcp.server import create_mcp_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

mcp = create_mcp_server()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
