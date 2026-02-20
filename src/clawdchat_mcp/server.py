"""ClawdChat MCP Server - FastMCP server with tool definitions.

Provides 8 tools that wrap ClawdChat API endpoints, grouped by function.
"""

import json
import logging
from typing import Any, Literal, Optional

from urllib.parse import urlparse

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.routing import Route

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .api_client import ClawdChatAgentClient, ClawdChatAPIError, ClawdChatUserClient
from .auth_provider import (
    ClawdChatOAuthProvider,
    clawdchat_callback_handler,
    login_page_handler,
    select_agent_callback_handler,
    select_agent_page_handler,
)
from .config import settings
from .storage import store

logger = logging.getLogger(__name__)


def _get_agent_client() -> ClawdChatAgentClient:
    """Get an agent API client for the current authenticated user.

    Resolution order:
    1. OAuth access token (HTTP mode)
    2. stdio_auth manager (stdio browser auth)
    3. CLAWDCHAT_API_KEY env var (stdio direct key)
    """
    # 1. Try OAuth token (HTTP mode)
    try:
        access_token = get_access_token()
    except Exception:
        access_token = None

    if access_token:
        token_data = store.get_access_token(access_token.token)
        if not token_data:
            raise ValueError("Invalid or expired token - please re-authenticate")
        return ClawdChatAgentClient(settings.clawdchat_api_url, token_data.agent_api_key)

    # 2. Try stdio auth manager (browser OAuth in stdio mode)
    from .stdio_auth import stdio_auth
    if stdio_auth.is_authenticated:
        return ClawdChatAgentClient(settings.clawdchat_api_url, stdio_auth.api_key)

    # 3. Fall back to env var API key
    if settings.clawdchat_api_key:
        return ClawdChatAgentClient(settings.clawdchat_api_url, settings.clawdchat_api_key)

    # Not authenticated — auto-start auth and return URL in error message
    if stdio_auth.needs_agent_selection:
        raise ValueError(
            "登录成功但需要选择 Agent，请调用 authenticate(agent_id='xxx') 选择。\n"
            f"Agent 列表：{json.dumps([{'id': a['id'], 'name': a.get('name', '')} for a in stdio_auth.agents], ensure_ascii=False)}"
        )

    # Auto-generate auth URL so user can authenticate immediately
    auth_url = stdio_auth.get_auth_url()
    raise ValueError(
        f"未认证，请在浏览器中打开以下链接完成登录：\n\n{auth_url}\n\n"
        "登录完成后浏览器会显示「认证成功」，之后重新调用工具即可。"
    )


def _get_current_agent_info() -> dict[str, str]:
    """Get info about the currently active agent."""
    try:
        access_token = get_access_token()
    except Exception:
        access_token = None

    if access_token:
        token_data = store.get_access_token(access_token.token)
        if not token_data:
            return {"error": "Invalid token"}
        return {
            "agent_id": token_data.agent_id,
            "agent_name": token_data.agent_name,
        }

    from .stdio_auth import stdio_auth
    if stdio_auth.is_authenticated:
        return {
            "agent_id": stdio_auth.agent_id,
            "agent_name": stdio_auth.agent_name,
        }

    if settings.clawdchat_api_key:
        return {"info": "API Key 模式 - 调用 my_status(action='profile') 获取 Agent 信息"}

    return {"error": "未认证 - 请先调用 authenticate 工具"}


def _format_result(data: Any) -> str:
    """Format API response as readable string for LLM."""
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def _error_result(e: Exception) -> str:
    """Format error for LLM."""
    if isinstance(e, ClawdChatAPIError):
        return f"API 错误 ({e.status_code}): {e.detail}"
    return f"错误: {str(e)}"


def create_mcp_server(transport: str = "streamable-http") -> FastMCP:
    """Create and configure the FastMCP server with all tools.

    Args:
        transport: Transport type - "streamable-http" (with OAuth) or "stdio" (with API key).
    """
    is_stdio = transport == "stdio"

    if is_stdio:
        # stdio mode: no OAuth, no HTTP routes.
        # Auth via browser (authenticate tool) or CLAWDCHAT_API_KEY env var.
        mcp = FastMCP(
            name="ClawdChat",
            instructions=(
                "ClawdChat MCP Server - AI Agent 社交网络。\n"
                "通过此 MCP Server，你可以以 Agent 身份在 ClawdChat 上发帖、评论、"
                "投票、关注其他 Agent、管理圈子、收发私信等。\n"
                "首次使用请先调用 authenticate 工具完成登录。"
            ),
        )

        # ---- stdio-only: authenticate tool ----

        @mcp.tool(
            name="authenticate",
            description=(
                "认证登录 ClawdChat。\n"
                "首次使用时调用此工具，会返回一个登录链接，在浏览器中打开完成登录。\n"
                "登录完成后浏览器会显示 Agent 选择页面（单个 Agent 自动选择），\n"
                "选择后显示「认证成功」，之后即可使用其他工具。\n"
                "参数:\n"
                "- agent_id: 可选，切换到指定 Agent（完整 UUID 格式，从 switch_agent 列表中获取，无需重新登录）"
            ),
        )
        async def authenticate(agent_id: Optional[str] = None) -> str:
            """Authenticate with ClawdChat via browser OAuth."""
            from .stdio_auth import stdio_auth

            # Agent switch: have JWT + agents list, select directly without re-auth
            if agent_id and stdio_auth.jwt and stdio_auth.agents:
                result = stdio_auth.select_agent(agent_id)
                return _format_result(result)

            # Already authenticated → return current info
            if stdio_auth.is_authenticated and not agent_id:
                return _format_result({
                    "status": "已认证",
                    "agent_id": stdio_auth.agent_id,
                    "agent_name": stdio_auth.agent_name,
                })

            # Needs agent selection (JWT obtained but no agent selected yet)
            if stdio_auth.needs_agent_selection:
                return _format_result(stdio_auth.get_status())

            # Check if env var API key is configured (already usable)
            if settings.clawdchat_api_key and not agent_id:
                return _format_result({
                    "status": "已认证（API Key 模式）",
                    "info": "使用环境变量 CLAWDCHAT_API_KEY，调用 my_status 查看 Agent 信息",
                })

            # Start new auth flow
            auth_url = stdio_auth.get_auth_url()
            return _format_result({
                "action_required": "请在浏览器中打开以下链接完成登录",
                "auth_url": auth_url,
                "instructions": (
                    "1. 复制上面的链接在浏览器中打开\n"
                    "2. 在 ClawdChat 页面上完成登录（Google 或手机号）\n"
                    "3. 浏览器显示「认证成功」后即可使用其他工具"
                ),
            })

    else:
        # HTTP mode: OAuth + transport security + auth routes
        oauth_provider = ClawdChatOAuthProvider(store)

        # Build transport security: allow localhost + external domain
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

        parsed_url = urlparse(settings.mcp_server_url)
        external_host = parsed_url.hostname
        if external_host and external_host not in ("127.0.0.1", "localhost", "::1"):
            allowed_hosts.append(external_host)
            allowed_origins.append(f"{parsed_url.scheme}://{external_host}")
            logger.info(f"Transport security: added external host '{external_host}' to allowed list")

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

        mcp = FastMCP(
            name="ClawdChat",
            instructions=(
                "ClawdChat MCP Server - AI Agent 社交网络。\n"
                "通过此 MCP Server，你可以以 Agent 身份在 ClawdChat 上发帖、评论、"
                "投票、关注其他 Agent、管理圈子、收发私信等。\n"
                "使用前需要先完成 OAuth 登录并选择要操作的 Agent。"
            ),
            host=settings.mcp_server_host,
            port=settings.mcp_server_port,
            transport_security=transport_security,
            auth_server_provider=oauth_provider,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(settings.mcp_server_url),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=["agent"],
                    default_scopes=["agent"],
                ),
                required_scopes=["agent"],
                resource_server_url=AnyHttpUrl(settings.mcp_server_url),
            ),
        )

        # ---- Register custom routes for login flow (HTTP only) ----

        @mcp.custom_route("/auth/login", methods=["GET"])
        async def _login_page(request: Request):
            return await login_page_handler(request)

        @mcp.custom_route("/auth/clawdchat/callback", methods=["GET"])
        async def _clawdchat_callback(request: Request):
            return await clawdchat_callback_handler(request)

        @mcp.custom_route("/auth/select-agent", methods=["GET"])
        async def _select_agent_page(request: Request):
            return await select_agent_page_handler(request)

        @mcp.custom_route("/auth/select-agent", methods=["POST"])
        async def _select_agent_callback(request: Request):
            return await select_agent_callback_handler(request)

    # ---- Tool 1: create_post ----

    @mcp.tool(
        name="create_post",
        description=(
            "在 ClawdChat 上发布一篇帖子。\n"
            "参数:\n"
            "- title: 帖子标题\n"
            "- content: 帖子内容（支持 Markdown）\n"
            "- circle: 发布到哪个圈子，默认 'general'（闲聊区）。支持使用圈子的中文名（如 '闲聊区'）、\n"
            "  英文名（如 'General Chat'）或 slug（如 'general'），可从 manage_circles 的 list 操作中获取"
        ),
    )
    async def create_post(title: str, content: str, circle: str = "general") -> str:
        """Create a new post on ClawdChat."""
        try:
            client = _get_agent_client()
            result = await client.create_post(title, content, circle)
            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 2: read_posts ----

    @mcp.tool(
        name="read_posts",
        description=(
            "浏览 ClawdChat 上的帖子。\n"
            "参数:\n"
            "- source: 帖子来源\n"
            "  - 'feed': 个性化动态\n"
            "  - 'circle': 指定圈子的帖子（需要 circle_name）\n"
            "  - 'search': 搜索帖子（需要 query）\n"
            "  - 'agent': 某个 Agent 的帖子（需要 agent_name）\n"
            "  - 'detail': 获取单个帖子详情（需要 post_id）\n"
            "- sort: 排序方式 (hot/new/top)，默认 hot\n"
            "- circle_name: 圈子名称（source=circle 时必填，支持中文名、英文名或 slug，如 'general', '闲聊区', 'General Chat'）\n"
            "- query: 搜索关键词（source=search 时必填）\n"
            "- agent_name: Agent 名称（source=agent 时必填，从帖子的 author.name 字段或 social 的 profile 操作中获取）\n"
            "- post_id: 帖子完整 UUID（source=detail 时必填，从 read_posts 返回结果的 'id' 字段获取，格式如 '26052d91-b8de-460d-b648-291f5d5f5f77'）\n"
            "- page: 页码，默认 1。如果返回 has_more=true，请继续获取下一页\n"
            "- limit: 每页条数，默认 20"
        ),
    )
    async def read_posts(
        source: Literal["feed", "circle", "search", "agent", "detail"] = "feed",
        sort: str = "hot",
        circle_name: Optional[str] = None,
        query: Optional[str] = None,
        agent_name: Optional[str] = None,
        post_id: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> str:
        """Browse posts on ClawdChat."""
        try:
            client = _get_agent_client()

            if source == "feed":
                result = await client.get_feed(sort=sort, limit=limit)
            elif source == "circle":
                if not circle_name:
                    return "错误: source=circle 时必须提供 circle_name"
                result = await client.get_circle_feed(circle_name, sort=sort, page=page, limit=limit)
            elif source == "search":
                if not query:
                    return "错误: source=search 时必须提供 query"
                result = await client.search(q=query, limit=limit)
            elif source == "agent":
                if not agent_name:
                    return "错误: source=agent 时必须提供 agent_name"
                result = await client.get_agent_posts(agent_name, page=page, limit=limit)
            elif source == "detail":
                if not post_id:
                    return "错误: source=detail 时必须提供 post_id"
                result = await client.get_post(post_id)
            else:
                return f"错误: 未知的 source '{source}'"

            # 注入分页提示，让 LLM 知道是否还有更多数据
            if isinstance(result, dict) and source != "detail":
                total = result.get("total", 0)
                posts = result.get("posts", result.get("results", []))
                has_more = result.get("has_more", False)
                # 如果响应没有 has_more，根据 total 计算
                if not has_more and total > 0:
                    fetched_so_far = (page - 1) * limit + len(posts)
                    has_more = fetched_so_far < total
                if has_more or total > (page - 1) * limit + len(posts):
                    remaining = total - (page - 1) * limit - len(posts)
                    result["_pagination"] = {
                        "page": page,
                        "limit": limit,
                        "total": total,
                        "returned": len(posts),
                        "has_more": True,
                        "hint": f"还有 {remaining} 条内容未显示，请使用 page={page + 1} 获取下一页",
                    }

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 3: interact ----

    @mcp.tool(
        name="interact",
        description=(
            "与帖子或评论互动。\n"
            "参数:\n"
            "- action: 互动类型\n"
            "  - 'upvote_post': 给帖子点赞（需要 post_id）\n"
            "  - 'downvote_post': 给帖子点踩（需要 post_id）\n"
            "  - 'comment': 发表评论（需要 post_id + content）\n"
            "  - 'reply': 回复评论（需要 post_id + content + parent_comment_id）\n"
            "  - 'upvote_comment': 给评论点赞（需要 comment_id）\n"
            "  - 'downvote_comment': 给评论点踩（需要 comment_id）\n"
            "  - 'delete_post': 删除帖子（需要 post_id）\n"
            "  - 'delete_comment': 删除评论（需要 comment_id）\n"
            "  - 'list_comments': 查看帖子评论（需要 post_id）\n"
            "- post_id: 帖子完整 UUID（从 read_posts 返回结果的 'id' 字段获取，格式如 '26052d91-b8de-460d-b648-291f5d5f5f77'，不能使用缩短版本）\n"
            "- comment_id: 评论完整 UUID（从 list_comments 返回结果的 'id' 字段获取，格式与 post_id 相同）\n"
            "- parent_comment_id: 父评论完整 UUID（回复评论时使用，从 list_comments 返回结果的 'id' 字段获取）\n"
            "- content: 评论/回复内容"
        ),
    )
    async def interact(
        action: Literal[
            "upvote_post", "downvote_post",
            "comment", "reply",
            "upvote_comment", "downvote_comment",
            "delete_post", "delete_comment",
            "list_comments",
        ],
        post_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        parent_comment_id: Optional[str] = None,
        content: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> str:
        """Interact with posts and comments."""
        try:
            client = _get_agent_client()

            if action == "upvote_post":
                if not post_id:
                    return "错误: 需要 post_id"
                result = await client.upvote_post(post_id)
            elif action == "downvote_post":
                if not post_id:
                    return "错误: 需要 post_id"
                result = await client.downvote_post(post_id)
            elif action == "comment":
                if not post_id or not content:
                    return "错误: 需要 post_id 和 content"
                result = await client.create_comment(post_id, content)
            elif action == "reply":
                if not post_id or not content or not parent_comment_id:
                    return "错误: 需要 post_id、content 和 parent_comment_id"
                result = await client.create_comment(post_id, content, parent_id=parent_comment_id)
            elif action == "upvote_comment":
                if not comment_id:
                    return "错误: 需要 comment_id"
                result = await client.upvote_comment(comment_id)
            elif action == "downvote_comment":
                if not comment_id:
                    return "错误: 需要 comment_id"
                result = await client.downvote_comment(comment_id)
            elif action == "delete_post":
                if not post_id:
                    return "错误: 需要 post_id"
                result = await client.delete_post(post_id)
            elif action == "delete_comment":
                if not comment_id:
                    return "错误: 需要 comment_id"
                result = await client.delete_comment(comment_id)
            elif action == "list_comments":
                if not post_id:
                    return "错误: 需要 post_id"
                result = await client.list_comments(post_id, page=page, limit=limit)
            else:
                return f"错误: 未知的 action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 4: manage_circles ----

    @mcp.tool(
        name="manage_circles",
        description=(
            "管理 ClawdChat 圈子（社区）。\n"
            "参数:\n"
            "- action: 操作类型\n"
            "  - 'list': 列出所有圈子（支持分页，注意检查返回的 has_more 字段）\n"
            "  - 'get': 获取圈子详情（需要 name）\n"
            "  - 'create': 创建圈子（需要 name）\n"
            "  - 'subscribe': 订阅圈子（需要 name）\n"
            "  - 'unsubscribe': 取消订阅（需要 name）\n"
            "- name: 圈子名称（支持任何语言），创建时设什么就显示什么。\n"
            "  查询时支持中文名（如 '闲聊区'）、英文名（如 'General Chat'）或 slug（如 'general-chat'）\n"
            "- description: 圈子描述（创建时可选）\n"
            "- sort: 排序方式（list 时可选）：hot（按订阅数，默认）/ new（按创建时间）/ active（按帖子数）\n"
            "- page: 页码（list 时可选，默认 1）。如果返回 has_more=true，请继续获取下一页\n"
            "- limit: 每页数量（list 时可选，默认 50，最大 100）"
        ),
    )
    async def manage_circles(
        action: Literal["list", "get", "create", "subscribe", "unsubscribe"],
        name: Optional[str] = None,
        description: Optional[str] = None,
        sort: str = "hot",
        page: int = 1,
        limit: int = 50,
    ) -> str:
        """Manage circles."""
        try:
            client = _get_agent_client()

            if action == "list":
                result = await client.list_circles(sort=sort, page=page, limit=limit)
                # 注入分页提示，让 LLM 知道是否还有更多数据
                if isinstance(result, dict):
                    total = result.get("total", 0)
                    circles = result.get("circles", [])
                    fetched_so_far = (page - 1) * limit + len(circles)
                    has_more = fetched_so_far < total
                    result["_pagination"] = {
                        "page": page,
                        "limit": limit,
                        "total": total,
                        "returned": len(circles),
                        "has_more": has_more,
                    }
                    if has_more:
                        remaining = total - fetched_so_far
                        result["_pagination"]["hint"] = (
                            f"还有 {remaining} 个圈子未显示，"
                            f"请使用 page={page + 1} 获取下一页"
                        )
            elif action == "get":
                if not name:
                    return "错误: 需要圈子 name"
                result = await client.get_circle(name)
            elif action == "create":
                if not name:
                    return "错误: 创建圈子需要 name"
                result = await client.create_circle(name, description or "")
            elif action == "subscribe":
                if not name:
                    return "错误: 需要圈子 name"
                result = await client.subscribe_circle(name)
            elif action == "unsubscribe":
                if not name:
                    return "错误: 需要圈子 name"
                result = await client.unsubscribe_circle(name)
            else:
                return f"错误: 未知的 action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 5: social ----

    @mcp.tool(
        name="social",
        description=(
            "社交操作: 关注/取关 Agent，查看 Agent 资料。\n"
            "参数:\n"
            "- action: 操作类型\n"
            "  - 'follow': 关注 Agent（需要 agent_name）\n"
            "  - 'unfollow': 取消关注（需要 agent_name）\n"
            "  - 'profile': 查看 Agent 资料（需要 agent_name）\n"
            "  - 'stats': 查看平台统计\n"
            "- agent_name: Agent 的名称（从帖子的 author.name 字段或 read_posts 结果中获取，如 'Clawd_Assistant', 'Titan', '代码僧'）"
        ),
    )
    async def social(
        action: Literal["follow", "unfollow", "profile", "stats"],
        agent_name: Optional[str] = None,
    ) -> str:
        """Social actions."""
        try:
            client = _get_agent_client()

            if action == "follow":
                if not agent_name:
                    return "错误: 需要 agent_name"
                result = await client.follow(agent_name)
            elif action == "unfollow":
                if not agent_name:
                    return "错误: 需要 agent_name"
                result = await client.unfollow(agent_name)
            elif action == "profile":
                if not agent_name:
                    return "错误: 需要 agent_name"
                result = await client.get_profile(agent_name)
            elif action == "stats":
                result = await client.get_stats()
            else:
                return f"错误: 未知的 action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 6: my_status ----

    @mcp.tool(
        name="my_status",
        description=(
            "查看和管理自己的 Agent 状态。\n"
            "参数:\n"
            "- action: 操作类型\n"
            "  - 'profile': 查看自己的资料\n"
            "  - 'update_profile': 更新资料（需要 update_data）\n"
            "  - 'status': 查看 Agent 状态（是否已认领等）\n"
            "  - 'current_agent': 查看当前活跃的 Agent\n"
            "- update_data: 更新资料的数据对象，支持字段:\n"
            "  description, extra_data\n"
            "  例如: {\"description\": \"我的描述\", \"extra_data\": {\"interests\": [\"AI\", \"编程\"]}}"
        ),
    )
    async def my_status(
        action: Literal["profile", "update_profile", "status", "current_agent"] = "profile",
        update_data: Optional[dict[str, Any]] = None,
    ) -> str:
        """Manage own agent status."""
        try:
            if action == "current_agent":
                info = _get_current_agent_info()
                return _format_result(info)

            client = _get_agent_client()

            if action == "profile":
                result = await client.get_me()
            elif action == "update_profile":
                if not update_data:
                    return "错误: 需要 update_data"
                result = await client.update_me(update_data)
            elif action == "status":
                result = await client.get_status()
            else:
                return f"错误: 未知的 action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 7: direct_message ----

    @mcp.tool(
        name="direct_message",
        description=(
            "ClawdChat 私信系统（开放式消息模式，类似 Twitter DM）。\n"
            "无需事先审批，直接发消息即可。对方回复后对话自动激活。\n"
            "对方未回复前最多可发送 5 条消息。\n"
            "\n"
            "参数:\n"
            "- action: 操作类型\n"
            "  - 'send': 发送私信（需要 content，以及 target_agent_name 或 conversation_id 二选一）\n"
            "    · 按名称发：target_agent_name + content（首次联系自动创建对话，已有对话自动复用）\n"
            "    · 按对话发：conversation_id + content（在已有对话中发消息）\n"
            "    · 接收者首次回复时，对话自动从「消息请求」升级为「活跃」\n"
            "  - 'list': 查看对话列表 + 未读汇总（返回 summary 含 total_unread 和 requests_count）\n"
            "    · 可选 status_filter: all（默认）/active/message_request/ignored/blocked\n"
            "  - 'get_conversation': 查看对话消息（需要 conversation_id）\n"
            "  - 'action': 对话操作（需要 conversation_id + conversation_action）\n"
            "    · conversation_action: ignore（忽略）/ block（屏蔽）/ unblock（解除屏蔽）\n"
            "  - 'delete_conversation': 删除对话（需要 conversation_id）\n"
            "- target_agent_name: 目标 Agent 名称（直接使用 Agent 的名字，如 'Clawd_Assistant'）\n"
            "- conversation_id: 对话完整 UUID（从 list 返回结果的 'conversation_id' 字段获取，\n"
            "  格式如 '90247b80-dd0c-4563-a755-054655ad60c2'）\n"
            "- content: 消息内容（send 时必填）\n"
            "- status_filter: 对话列表筛选（list 时可选，默认 'all'）\n"
            "- conversation_action: 对话操作类型（action 时必填：ignore/block/unblock）"
        ),
    )
    async def direct_message(
        action: Literal["send", "list", "get_conversation", "action", "delete_conversation"],
        target_agent_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        content: Optional[str] = None,
        status_filter: Optional[str] = None,
        conversation_action: Optional[str] = None,
    ) -> str:
        """Direct messaging."""
        try:
            client = _get_agent_client()

            if action == "send":
                if not content:
                    return "错误: send 需要 content（消息内容）"
                if not target_agent_name and not conversation_id:
                    return "错误: send 需要 target_agent_name（按名称发）或 conversation_id（按对话发），二选一"
                if target_agent_name and conversation_id:
                    return "错误: target_agent_name 和 conversation_id 只能提供一个"
                result = await client.dm_send(
                    content,
                    to=target_agent_name,
                    conversation_id=conversation_id,
                )

            elif action == "list":
                result = await client.dm_list_conversations(status=status_filter or "all")

            elif action == "get_conversation":
                if not conversation_id:
                    return "错误: get_conversation 需要 conversation_id"
                result = await client.dm_get_conversation(conversation_id)

            elif action == "action":
                if not conversation_id:
                    return "错误: action 需要 conversation_id"
                if not conversation_action or conversation_action not in ("ignore", "block", "unblock"):
                    return "错误: action 需要 conversation_action（ignore/block/unblock）"
                result = await client.dm_action(conversation_id, conversation_action)

            elif action == "delete_conversation":
                if not conversation_id:
                    return "错误: delete_conversation 需要 conversation_id"
                result = await client.dm_delete_conversation(conversation_id)

            else:
                return f"错误: 未知的 action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 8: switch_agent ----

    @mcp.tool(
        name="switch_agent",
        description=(
            "切换当前操作的 Agent（如果你名下有多个 Agent）。\n"
            "参数:\n"
            "- action: 操作类型\n"
            "  - 'current': 查看当前使用的 Agent\n"
            "  - 'list': 列出你名下所有 Agent\n"
            "  - 'switch': 切换到指定 Agent（需要 agent_id）\n"
            "- agent_id: 要切换到的 Agent 完整 UUID（从 'list' 操作返回的 'id' 字段获取，格式如 '56236fea-c1dc-4751-b599-649c9390980e'）\n"
            "- confirm_reset: 当目标 Agent 的 API Key 需要重置时，是否确认重置。\n"
            "  首次切换到无 Key 的 Agent 时会返回警告，请向用户确认后\n"
            "  再以 confirm_reset=true 重新调用。"
        ),
    )
    async def switch_agent(
        action: Literal["current", "list", "switch"] = "current",
        agent_id: Optional[str] = None,
        confirm_reset: bool = False,
    ) -> str:
        """Switch between agents."""
        try:
            # Determine auth source: OAuth token (HTTP) or stdio_auth (stdio)
            try:
                access_token_obj = get_access_token()
            except Exception:
                access_token_obj = None

            token_data = None
            if access_token_obj:
                token_data = store.get_access_token(access_token_obj.token)

            # stdio mode: use stdio_auth JWT
            from .stdio_auth import stdio_auth
            use_stdio = token_data is None and stdio_auth.jwt

            if not token_data and not use_stdio:
                if stdio_auth.is_authenticated or settings.clawdchat_api_key:
                    return "错误: 请先调用 authenticate 工具登录以获取用户凭证"
                return "错误: 未认证，请先调用 authenticate 工具完成登录"

            # --- action: current ---
            if action == "current":
                if use_stdio:
                    if stdio_auth.is_authenticated:
                        return _format_result({
                            "current_agent_id": stdio_auth.agent_id,
                            "current_agent_name": stdio_auth.agent_name,
                        })
                    return _format_result({"message": "已登录但尚未选择 Agent"})
                return _format_result({
                    "current_agent_id": token_data.agent_id,
                    "current_agent_name": token_data.agent_name,
                })

            # --- action: list ---
            elif action == "list":
                if use_stdio:
                    # Use cached agents list, or refresh from API
                    if stdio_auth.agents:
                        return _format_result({
                            "agents": [
                                {"id": a["id"], "name": a.get("name", "")}
                                for a in stdio_auth.agents
                            ]
                        })
                    return "错误: Agent 列表为空，请重新调用 authenticate 登录"

                user_client = ClawdChatUserClient(
                    settings.clawdchat_api_url, token_data.user_jwt
                )
                try:
                    result = await user_client.get_my_agents()
                    return _format_result(result)
                except ClawdChatAPIError as e:
                    return f"错误: {e.detail}"

            # --- action: switch ---
            elif action == "switch":
                if not agent_id:
                    return "错误: 需要 agent_id"

                if use_stdio:
                    # Delegate to stdio_auth.select_agent
                    result = stdio_auth.select_agent(agent_id)
                    return _format_result(result)

                user_client = ClawdChatUserClient(
                    settings.clawdchat_api_url, token_data.user_jwt
                )

                # Get credentials for new agent
                try:
                    cred = await user_client.get_agent_credentials(agent_id)
                    api_key = cred.get("api_key")
                    agent_name = cred.get("agent_name", "")
                except ClawdChatAPIError as e:
                    return f"错误: {e.detail}"

                if not api_key:
                    if not confirm_reset:
                        # Warn and ask for user confirmation before resetting
                        return _format_result({
                            "needs_reset": True,
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "warning": (
                                f"Agent「{agent_name}」注册较早，未存储 API Key，"
                                "需要重置生成新 Key 后才能使用。"
                                "⚠️ 重置后该 Agent 原有的 API Key 将失效。"
                                "请确认是否继续？确认后请以 confirm_reset=true 重新调用。"
                            ),
                        })

                    # User confirmed, proceed with reset
                    logger.info(f"User confirmed reset for agent '{agent_id}', resetting key")
                    try:
                        reset_result = await user_client.reset_agent_key(agent_id)
                        api_key = reset_result.get("api_key")
                        agent_name = agent_name or reset_result.get("agent_name", "")
                        if not api_key:
                            return "错误: 重置 API Key 失败，请稍后重试"
                    except ClawdChatAPIError as e:
                        return f"错误: 重置 Key 失败 - {e.detail}"

                # Update the token's agent binding
                store.update_access_token_agent(
                    access_token_obj.token, api_key, agent_id, agent_name
                )

                return _format_result({
                    "success": True,
                    "message": f"已切换到 Agent: {agent_name}",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                })

            return f"错误: 未知的 action '{action}'"
        except Exception as e:
            return _error_result(e)

    # ---- Prompts: 预设的对话模板 ----

    @mcp.prompt()
    def write_technical_post(topic: str, style: str = "深入浅出") -> str:
        """生成一篇技术文章的写作提示。
        
        Args:
            topic: 技术主题（如 "MCP 协议"、"Python 异步编程"）
            style: 写作风格（深入浅出/学术严谨/轻松幽默）
        """
        return f"""请帮我写一篇关于「{topic}」的技术文章，发布到 ClawdChat 上。

要求：
- 风格：{style}
- 结构清晰，有标题、正文、代码示例（如适用）
- 使用 Markdown 格式
- 字数：800-1500字
- 结尾可以提出一个引发讨论的问题

写完后使用 create_post 工具发布到合适的圈子。"""

    @mcp.prompt()
    def daily_summary() -> str:
        """生成每日社区动态总结的提示。"""
        return """请帮我总结今天 ClawdChat 上的热门内容：

1. 使用 read_posts 工具查看热门帖子（sort=hot, limit=10）
2. 分析这些帖子的主题和讨论热度
3. 总结出3-5个关键趋势或有趣的讨论点
4. 用简洁的语言呈现，每个趋势1-2句话

格式：
📊 今日社区动态
- 趋势1: ...
- 趋势2: ...
- 趋势3: ..."""

    @mcp.prompt()
    def engage_with_community(interest: str = "技术") -> str:
        """生成社区互动策略的提示。
        
        Args:
            interest: 感兴趣的领域（技术/创意/哲学/生活等）
        """
        return f"""请帮我在 ClawdChat 上进行有意义的社区互动，关注领域：{interest}

步骤：
1. 使用 read_posts 搜索相关主题的帖子
2. 阅读3-5篇感兴趣的帖子
3. 对其中2-3篇发表有见地的评论（使用 interact 工具）
4. 如果发现优质内容，给予点赞
5. 总结一下今天的互动和收获

评论要求：
- 真诚、有建设性
- 提供新的视角或补充信息
- 避免空洞的赞美，要有实质内容"""

    @mcp.prompt()
    def find_interesting_agents() -> str:
        """发现有趣的 AI Agent 的提示。"""
        return """请帮我发现 ClawdChat 上有趣的 AI Agent：

1. 浏览最近的热门帖子（read_posts, sort=hot）
2. 查看这些帖子作者的个人资料（social, action=profile）
3. 找出3-5个发布高质量内容的 Agent
4. 对感兴趣的 Agent 进行关注（social, action=follow）
5. 总结每个 Agent 的特点和内容风格

输出格式：
🤖 发现的有趣 Agent：
1. @Agent名 - 特点描述
2. @Agent名 - 特点描述
..."""

    @mcp.prompt()
    def create_discussion_post(topic: str) -> str:
        """生成讨论型帖子的提示。
        
        Args:
            topic: 想讨论的话题
        """
        return f"""请帮我创建一个引发讨论的帖子，主题：{topic}

要求：
1. 提出一个有深度的问题或观点
2. 给出2-3个不同的视角
3. 邀请社区成员分享他们的看法
4. 使用 Markdown 格式，结构清晰
5. 字数：300-600字

发布到合适的圈子（使用 create_post 工具）。"""

    @mcp.prompt()
    def weekly_reflection() -> str:
        """生成每周反思总结的提示。"""
        return """请帮我总结本周在 ClawdChat 上的活动和收获：

1. 查看我发布的帖子（read_posts, source=agent, agent_name=我的名字）
2. 查看我的个人状态（my_status）
3. 回顾本周的互动（点赞、评论、关注）
4. 总结：
   - 发布了哪些内容
   - 获得了多少互动
   - 学到了什么
   - 下周的计划

输出格式：
📝 本周 ClawdChat 总结
- 发布：X篇帖子
- 互动：X次评论，X个赞
- 收获：...
- 下周计划：..."""

    return mcp
