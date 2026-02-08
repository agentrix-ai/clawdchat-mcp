"""ClawdChat MCP Server - Entry point.

Usage:
    # 通过 uvx（推荐，无需 clone）
    uvx clawdchat-mcp                                # stdio 模式（默认）
    uvx clawdchat-mcp --transport streamable-http     # HTTP 模式

    # 本地开发
    uv run python main.py
    uv run python main.py --transport streamable-http
"""

from clawdchat_mcp import main

if __name__ == "__main__":
    main()
