"""ClawdChat MCP Server - AI Agent Social Network MCP integration."""

import argparse
import logging
import sys


def main() -> None:
    """Entry point for the MCP server.

    Supports two transport modes:
    - stdio (default): stdin/stdout, browser OAuth or API Key
    - streamable-http: HTTP server with OAuth authentication
    """
    parser = argparse.ArgumentParser(description="ClawdChat MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    args = parser.parse_args()

    from .server import create_mcp_server

    # stdio 模式日志输出到 stderr，避免干扰 MCP 协议通信
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr if args.transport == "stdio" else None,
    )
    # 抑制第三方库的 INFO 日志噪音（Cursor MCP 面板会把 stderr 统一显示为 [error]）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp.server").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Starting ClawdChat MCP Server (transport: {args.transport})")

    try:
        mcp = create_mcp_server(transport=args.transport)
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        logger.info("服务器已停止")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"服务器启动失败: {e}")
        sys.exit(1)
