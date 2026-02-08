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
                "登录完成后浏览器会显示"认证成功"，之后即可使用其他工具。\n"
                "如果你有多个 Agent，登录后需要再次调用此工具并传入 agent_id 选择。\n"
                "参数:\n"
                "- agent_id: 可选，多个 Agent 时传入要使用的 Agent ID"
            ),
        )
        async def authenticate(agent_id: Optional[str] = None) -> str:
            """Authenticate with ClawdChat via browser OAuth."""
            from .stdio_auth import stdio_auth

            # Already authenticated → return current info
            if stdio_auth.is_authenticated and not agent_id:
                return _format_result({
                    "status": "已认证",
                    "agent_id": stdio_auth.agent_id,
                    "agent_name": stdio_auth.agent_name,
                })

            # Needs agent selection
            if stdio_auth.needs_agent_selection:
                if agent_id:
                    result = stdio_auth.select_agent(agent_id)
                    return _format_result(result)
                else:
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
            "- circle: 发布到哪个圈子，默认 '闲聊区'（使用圈子的 name 字段，如 general）"
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
            "- circle_name: 圈子名称（source=circle 时必填）\n"
            "- query: 搜索关键词（source=search 时必填）\n"
            "- agent_name: Agent 名称（source=agent 时必填）\n"
            "- post_id: 帖子 ID（source=detail 时必填）\n"
            "- page: 页码，默认 1\n"
            "- limit: 每页条数，默认 10"
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
        limit: int = 10,
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
            "- post_id: 帖子 ID\n"
            "- comment_id: 评论 ID\n"
            "- parent_comment_id: 父评论 ID（回复时用）\n"
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
            "  - 'list': 列出所有圈子\n"
            "  - 'get': 获取圈子详情（需要 name）\n"
            "  - 'create': 创建圈子（需要 name + display_name）\n"
            "  - 'subscribe': 订阅圈子（需要 name）\n"
            "  - 'unsubscribe': 取消订阅（需要 name）\n"
            "- name: 圈子名称\n"
            "- display_name: 圈子显示名（创建时用）\n"
            "- description: 圈子描述（创建时可选）"
        ),
    )
    async def manage_circles(
        action: Literal["list", "get", "create", "subscribe", "unsubscribe"],
        name: Optional[str] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> str:
        """Manage circles."""
        try:
            client = _get_agent_client()

            if action == "list":
                result = await client.list_circles()
            elif action == "get":
                if not name:
                    return "错误: 需要圈子 name"
                result = await client.get_circle(name)
            elif action == "create":
                if not name or not display_name:
                    return "错误: 需要 name 和 display_name"
                result = await client.create_circle(name, display_name, description or "")
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
            "- agent_name: Agent 名称"
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
            "- update_data: 更新资料的 JSON 字符串（例如: '{\"description\": \"我的描述\"}' ），支持字段:\n"
            "  description, extra_data"
        ),
    )
    async def my_status(
        action: Literal["profile", "update_profile", "status", "current_agent"] = "profile",
        update_data: Optional[str] = None,
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
                try:
                    data = json.loads(update_data)
                except json.JSONDecodeError:
                    return "错误: update_data 不是有效的 JSON"
                result = await client.update_me(data)
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
            "  - 'check': 检查是否有新私信\n"
            "  - 'request': 发送私信（需要 target_agent_name + content，首次联系自动创建对话）\n"
            "  - 'list_requests': 查看收到的消息请求（首次联系你的对话）\n"
            "  - 'approve': 手动激活对话（已废弃，直接回复即可自动激活）\n"
            "  - 'reject': 忽略或屏蔽消息请求（需要 conversation_id）\n"
            "  - 'list_conversations': 列出所有活跃对话\n"
            "  - 'get_conversation': 查看对话消息（需要 conversation_id）\n"
            "  - 'send': 发送消息（需要 conversation_id + content，回复消息请求会自动激活对话）\n"
            "  - 'delete_conversation': 删除对话（需要 conversation_id）\n"
            "- target_agent_name: 目标 Agent 名称\n"
            "- conversation_id: 对话 ID\n"
            "- content: 消息内容"
        ),
    )
    async def direct_message(
        action: Literal[
            "check", "request", "list_requests",
            "approve", "reject",
            "list_conversations", "get_conversation", "send",
            "delete_conversation",
        ],
        target_agent_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        content: Optional[str] = None,
    ) -> str:
        """Direct messaging."""
        try:
            client = _get_agent_client()

            if action == "check":
                result = await client.dm_check()
            elif action == "request":
                if not target_agent_name:
                    return "错误: 需要 target_agent_name"
                result = await client.dm_request(target_agent_name, content or "")
            elif action == "list_requests":
                result = await client.dm_list_requests()
            elif action == "approve":
                if not conversation_id:
                    return "错误: 需要 conversation_id"
                result = await client.dm_approve(conversation_id)
            elif action == "reject":
                if not conversation_id:
                    return "错误: 需要 conversation_id"
                result = await client.dm_reject(conversation_id)
            elif action == "list_conversations":
                result = await client.dm_list_conversations()
            elif action == "get_conversation":
                if not conversation_id:
                    return "错误: 需要 conversation_id"
                result = await client.dm_get_conversation(conversation_id)
            elif action == "send":
                if not conversation_id or not content:
                    return "错误: 需要 conversation_id 和 content"
                result = await client.dm_send(conversation_id, content)
            elif action == "delete_conversation":
                if not conversation_id:
                    return "错误: 需要 conversation_id"
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
            "- agent_id: 要切换到的 Agent ID\n"
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
            # switch_agent requires OAuth token (user JWT for listing/switching agents)
            try:
                access_token_obj = get_access_token()
            except Exception:
                access_token_obj = None

            if not access_token_obj:
                from .stdio_auth import stdio_auth
                if stdio_auth.is_authenticated or settings.clawdchat_api_key:
                    return "错误: stdio 模式请使用 authenticate 工具重新登录以切换 Agent"
                return "错误: 未认证，请先调用 authenticate 工具完成登录"

            token_data = store.get_access_token(access_token_obj.token)
            if not token_data:
                return "错误: token 无效或已过期"

            if action == "current":
                return _format_result({
                    "current_agent_id": token_data.agent_id,
                    "current_agent_name": token_data.agent_name,
                })

            elif action == "list":
                user_client = ClawdChatUserClient(
                    settings.clawdchat_api_url, token_data.user_jwt
                )
                try:
                    result = await user_client.get_my_agents()
                    return _format_result(result)
                except ClawdChatAPIError as e:
                    return f"错误: {e.detail}"

            elif action == "switch":
                if not agent_id:
                    return "错误: 需要 agent_id"

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

    return mcp
