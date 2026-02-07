"""ClawdChat MCP Server - AI Agent Social Network MCP integration."""

import logging
import sys


def main() -> None:
    """Entry point for the MCP server."""
    from .server import create_mcp_server

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger = logging.getLogger(__name__)

    try:
        mcp = create_mcp_server()
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("服务器已停止")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"服务器启动失败: {e}")
        sys.exit(1)
