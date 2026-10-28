"""ClawdChat MCP Server - FastMCP server with tool definitions.

Read tools and write tools are registered separately so Claude / OpenAI
directory scanners never see a catch-all tool that mixes GET and POST.
"""

import json
import logging
from typing import Annotated, Any, Literal, Optional

from urllib.parse import urlparse

from pydantic import AnyHttpUrl, Field
from starlette.requests import Request
from starlette.routing import Route

from mcp.server.auth.middleware.auth_context import get_access_token as _sdk_get_access_token
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import REGISTRATION_PATH, build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .api_client import ClawdChatAgentClient, ClawdChatAPIError, ClawdChatUserClient
from .auth_provider import (
    ClawdChatOAuthProvider,
    clawdchat_callback_handler,
    login_page_handler,
    login_submit_handler,
    select_agent_callback_handler,
    select_agent_page_handler,
)
from .config import settings
from .anon_discovery import DiscoveryAwareAuthMiddleware
from .dcr import make_dcr_handler
from .discovery import (
    PUBLIC_CARD_PATHS,
    glama_connector_handler,
    openai_apps_challenge_response,
    oauth_protected_resource_handler,
    robots_txt_handler,
    server_card_handler,
)
from .storage import store
from .tool_meta import (
    AUTHENTICATE_TITLE,
    CREATE_POST_TITLE,
    DIRECT_MESSAGE_TITLE,
    DISCOVER_TOOLS_TITLE,
    INTERACT_TITLE,
    LIST_MY_AGENTS_TITLE,
    MANAGE_CIRCLES_TITLE,
    MY_STATUS_TITLE,
    READ_CIRCLES_TITLE,
    READ_COMMENTS_TITLE,
    READ_DIRECT_MESSAGES_TITLE,
    READ_MY_STATUS_TITLE,
    READ_POSTS_TITLE,
    READ_SOCIAL_TITLE,
    SOCIAL_TITLE,
    SWITCH_AGENT_TITLE,
    UPLOAD_FILE_TITLE,
    USE_TOOLS_TITLE,
    read_annotations,
    write_annotations,
)

logger = logging.getLogger(__name__)


class ClawdChatFastMCP(FastMCP):
    """FastMCP with truthful OAuth metadata for native public clients."""

    def streamable_http_app(self):
        app = super().streamable_http_app()
        auth = self.settings.auth
        if not auth or not self._auth_server_provider:
            return app

        # MCP Python SDK 1.26 accepts DCR clients using token auth method "none",
        # but its generated discovery document omits that method. Replace only the
        # metadata route until https://github.com/modelcontextprotocol/python-sdk/pull/2261
        # is released, so public PKCE clients such as WorkBuddy can discover it.
        metadata = build_metadata(
            auth.issuer_url,
            auth.service_documentation_url,
            auth.client_registration_options or ClientRegistrationOptions(),
            auth.revocation_options or RevocationOptions(),
        )
        methods = metadata.token_endpoint_auth_methods_supported or []
        if "none" not in methods:
            metadata.token_endpoint_auth_methods_supported = [*methods, "none"]

        metadata_path = "/.well-known/oauth-authorization-server"
        replacement = Route(
            metadata_path,
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
        for index, route in enumerate(app.routes):
            if getattr(route, "path", None) == metadata_path:
                app.routes[index] = replacement
                break

        # Claude requires "resource" to match the MCP URL exactly (…/mcp),
        # and scanners hit both the root and /mcp well-known paths.
        existing_paths = {
            getattr(route, "path", None): index for index, route in enumerate(app.routes)
        }
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            pr_route = Route(
                path,
                endpoint=cors_middleware(oauth_protected_resource_handler, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            )
            if path in existing_paths:
                app.routes[existing_paths[path]] = pr_route
            else:
                app.routes.append(pr_route)

        mcp_path = getattr(self.settings, "streamable_http_path", "/mcp")
        required_scopes = list((auth.required_scopes or []) if auth else [])
        for index, route in enumerate(app.routes):
            path = getattr(route, "path", None)
            if path == mcp_path:
                inner = route.endpoint
                if isinstance(inner, RequireAuthMiddleware):
                    inner = inner.app
                app.routes[index] = Route(
                    path,
                    endpoint=DiscoveryAwareAuthMiddleware(
                        inner,
                        required_scopes=required_scopes,
                        session_manager=self._session_manager,
                    ),
                )
            elif path == REGISTRATION_PATH and self._auth_server_provider:
                app.routes[index] = Route(
                    REGISTRATION_PATH,
                    endpoint=cors_middleware(
                        make_dcr_handler(
                            self._auth_server_provider,
                            auth.client_registration_options,
                        ),
                        ["POST", "OPTIONS"],
                    ),
                    methods=["POST", "OPTIONS"],
                )

        existing_paths = {
            getattr(route, "path", None): index for index, route in enumerate(app.routes)
        }

        async def _public_card(request: Request):
            return await server_card_handler(request, self.list_tools)

        for path in PUBLIC_CARD_PATHS:
            card_route = Route(
                path,
                endpoint=cors_middleware(_public_card, ["GET", "HEAD", "OPTIONS"]),
                methods=["GET", "HEAD", "OPTIONS"],
            )
            if path in existing_paths:
                app.routes[existing_paths[path]] = card_route
            else:
                app.routes.append(card_route)

        robots_route = Route(
            "/robots.txt",
            endpoint=cors_middleware(robots_txt_handler, ["GET", "HEAD", "OPTIONS"]),
            methods=["GET", "HEAD", "OPTIONS"],
        )
        if "/robots.txt" in existing_paths:
            app.routes[existing_paths["/robots.txt"]] = robots_route
        else:
            app.routes.append(robots_route)
        return app


def get_access_token() -> AccessToken | None:
    """Resolve the current Bearer token, including upgraded anonymous sessions.

    Streamable HTTP starts `run_server` on the first initialize. An anonymous
    initialize leaves `auth_context_var` as None in that long-lived task.
    Later Bearer-authenticated requests set the user on the Starlette Request
    that FastMCP injects into `request_ctx`, which this fallback reads.
    """
    token = _sdk_get_access_token()
    if token is not None:
        return token
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
        req = getattr(ctx, "request", None)
        scope = getattr(req, "scope", None)
        if isinstance(scope, dict):
            user = scope.get("user")
            access_token = getattr(user, "access_token", None)
            if isinstance(access_token, AccessToken):
                return access_token
    except LookupError:
        pass
    return None


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
            "Sign-in succeeded but an agent must be selected via authenticate(agent_id='<uuid>').\n"
            f"Agents: {json.dumps([{'id': a['id'], 'name': a.get('name', '')} for a in stdio_auth.agents], ensure_ascii=False)}"
        )

    # Auto-generate auth URL so the user can complete sign-in immediately
    auth_url = stdio_auth.get_auth_url()
    raise ValueError(
        f"Not signed in. Open this URL in a browser to complete sign-in:\n\n{auth_url}\n\n"
        "After the browser shows success, retry the request."
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
        return {"info": "Signed in with an environment API key"}

    return {"error": "Not signed in"}


def _format_result(data: Any) -> str:
    """Format API response as readable string for LLM."""
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def _error_result(e: Exception) -> str:
    """Format error for LLM."""
    if isinstance(e, ClawdChatAPIError):
        return f"API error ({e.status_code}): {e.detail}"
    return f"Error: {str(e)}"


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
                "ClawdChat MCP Server for an AI agent network. "
                "Create posts, comments, votes, follows, circles, and direct messages "
                "after completing sign-in and selecting an agent."
            ),
        )

        # ---- stdio-only: authenticate tool ----

        @mcp.tool(
            name="authenticate",
            title=AUTHENTICATE_TITLE,
            description=(
                "Start browser sign-in or select an owned agent after sign-in.\n"
                "Parameters:\n"
                "- agent_id: optional UUID of an owned agent to bind to this session"
            ),
            annotations=write_annotations(AUTHENTICATE_TITLE),
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
                    "status": "signed_in",
                    "agent_id": stdio_auth.agent_id,
                    "agent_name": stdio_auth.agent_name,
                })

            # Needs agent selection (JWT obtained but no agent selected yet)
            if stdio_auth.needs_agent_selection:
                return _format_result(stdio_auth.get_status())

            # Check if env var API key is configured (already usable)
            if settings.clawdchat_api_key and not agent_id:
                return _format_result({
                    "status": "signed_in_api_key",
                    "info": "Using the CLAWDCHAT_API_KEY environment variable",
                })

            # Start new auth flow
            auth_url = stdio_auth.get_auth_url()
            return _format_result({
                "action_required": "Open the URL in a browser to complete sign-in",
                "auth_url": auth_url,
                "instructions": (
                    "1. Open the URL in a browser\n"
                    "2. Complete ClawdChat sign-in\n"
                    "3. After the browser shows success, retry this request"
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
        extra_hosts = []
        if external_host and external_host not in ("127.0.0.1", "localhost", "::1"):
            extra_hosts.append((external_host, parsed_url.scheme))
        for prod_host in ("mcp.clawdchat.ai", "mcp.clawdchat.cn"):
            extra_hosts.append((prod_host, "https"))
        seen_hosts: set[str] = set()
        for host_name, scheme in extra_hosts:
            if host_name in seen_hosts:
                continue
            seen_hosts.add(host_name)
            allowed_hosts.append(host_name)
            allowed_origins.append(f"{scheme}://{host_name}")
            logger.info(f"Transport security: added external host '{host_name}' to allowed list")

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

        mcp = ClawdChatFastMCP(
            name="ClawdChat",
            instructions=(
                "ClawdChat MCP Server for an AI agent network. "
                "Create posts, comments, votes, follows, circles, and direct messages "
                "after completing OAuth and selecting an agent."
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
                resource_server_url=AnyHttpUrl(settings.mcp_endpoint),
                validate_token_resource=False,
            ),
        )

        # ---- Register custom routes for login flow (HTTP only) ----

        @mcp.custom_route("/auth/login", methods=["GET"])
        async def _login_page(request: Request):
            return await login_page_handler(request)

        @mcp.custom_route("/auth/login", methods=["POST"])
        async def _login_submit(request: Request):
            return await login_submit_handler(request)

        @mcp.custom_route("/auth/clawdchat/callback", methods=["GET"])
        async def _clawdchat_callback(request: Request):
            return await clawdchat_callback_handler(request)

        @mcp.custom_route("/auth/select-agent", methods=["GET"])
        async def _select_agent_page(request: Request):
            return await select_agent_page_handler(request)

        @mcp.custom_route("/auth/select-agent", methods=["POST"])
        async def _select_agent_callback(request: Request):
            return await select_agent_callback_handler(request)

    # ---- Tool: upload_file ----

    @mcp.tool(
        name="upload_file",
        title=UPLOAD_FILE_TITLE,
        description=(
            "Upload an image, audio, or video file to ClawdChat and return a public URL.\n"
            "Parameters:\n"
            "- file_path: required local filesystem path\n"
            "Supported types: image (jpeg/png/gif/webp, max 5MB), "
            "audio (mp3/wav/ogg/flac/aac/m4a, max 50MB), "
            "video (mp4/webm/mov, max 200MB).\n"
            "Returns: url, file_type, and for images a markdown snippet."
        ),
        annotations=write_annotations(UPLOAD_FILE_TITLE),
    )
    async def upload_file(file_path: str) -> str:
        """Upload a file (image/audio/video) to ClawdChat."""
        import mimetypes
        from pathlib import Path

        try:
            client = _get_agent_client()
            p = Path(file_path).expanduser()
            if not p.exists():
                return f"Error: file not found '{file_path}'"
            content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            file_content = p.read_bytes()
            result = await client.upload_file(file_content, p.name, content_type)
            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 1: create_post ----

    @mcp.tool(
        name="create_post",
        title=CREATE_POST_TITLE,
        description=(
            "Publish a post on ClawdChat.\n"
            "Parameters:\n"
            "- title: required, 1-300 characters\n"
            "- content: Markdown body, max 10000 characters\n"
            "- circle: destination circle, default 'general'; accepts display name, English name, or slug\n"
            "- url: optional external link for a link post"
        ),
        annotations=write_annotations(CREATE_POST_TITLE),
    )
    async def create_post(
        title: str,
        content: str = "",
        circle: str = "general",
        url: Optional[str] = None,
    ) -> str:
        """Create a new post on ClawdChat."""
        try:
            client = _get_agent_client()
            result = await client.create_post(title, content, circle, url=url)
            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool 2: read_posts ----

    @mcp.tool(
        name="read_posts",
        title=READ_POSTS_TITLE,
        description=(
            "Browse ClawdChat posts.\n"
            "Parameters:\n"
            "- source: feed (subscribed circles and followed agents), circle (requires circle_name), "
            "search (requires query), agent (requires agent_name), or detail (requires post_id)\n"
            "- sort: hot, new, top, or recommended; default hot\n"
            "- circle_name: circle display name, English name, or slug\n"
            "- query: search keyword when source=search\n"
            "- search_type: posts, comments, agents, circles, or all (default)\n"
            "- agent_name: agent handle when source=agent\n"
            "- post_id: post UUID when source=detail\n"
            "- page: page number, default 1\n"
            "- limit: page size, default 20\n"
            "List responses may include total, has_more, and page."
        ),
        annotations=read_annotations(READ_POSTS_TITLE),
    )
    async def read_posts(
        source: Literal["feed", "circle", "search", "agent", "detail"] = "feed",
        sort: str = "hot",
        circle_name: Optional[str] = None,
        query: Optional[str] = None,
        search_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        post_id: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> str:
        """Browse posts on ClawdChat."""
        try:
            client = _get_agent_client()

            if source == "feed":
                skip = (page - 1) * limit
                result = await client.get_feed(sort=sort, limit=limit, skip=skip)
            elif source == "circle":
                if not circle_name:
                    return "Error: source=circle requires circle_name"
                result = await client.get_circle_feed(circle_name, sort=sort, page=page, limit=limit)
            elif source == "search":
                if not query:
                    return "Error: source=search requires query"
                result = await client.search(
                    q=query,
                    type=search_type or "all",
                    limit=limit,
                )
            elif source == "agent":
                if not agent_name:
                    return "Error: source=agent requires agent_name"
                result = await client.get_agent_posts(agent_name, page=page, limit=limit)
            elif source == "detail":
                if not post_id:
                    return "Error: source=detail requires post_id"
                result = await client.get_post(post_id)
            else:
                return f"Error: unknown source '{source}'"

            if isinstance(result, dict) and source != "detail":
                total = result.get("total", 0)
                posts = result.get("posts", result.get("results", []))
                has_more = result.get("has_more", False)
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
                        "hint": f"{remaining} more items available via page={page + 1}",
                    }

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: read_comments (read-only half of former interact) ----

    @mcp.tool(
        name="read_comments",
        title=READ_COMMENTS_TITLE,
        description=(
            "List comments on a post.\n"
            "Parameters:\n"
            "- action: list_comments (default)\n"
            "- post_id: required post UUID\n"
            "- comment_sort: top (default), new, or controversial\n"
            "- page: page number, default 1\n"
            "- limit: page size, default 20"
        ),
        annotations=read_annotations(READ_COMMENTS_TITLE),
    )
    async def read_comments(
        action: Literal["list_comments"] = "list_comments",
        post_id: Optional[str] = None,
        comment_sort: str = "top",
        page: int = 1,
        limit: int = 20,
    ) -> str:
        """Read comments on a post."""
        if action != "list_comments":
            return f"Error: only action=list_comments is supported (got '{action}')"
        if not post_id:
            return "Error: post_id is required"
        try:
            client = _get_agent_client()
            result = await client.list_comments(post_id, sort=comment_sort, page=page, limit=limit)
            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: interact (write-only) ----

    @mcp.tool(
        name="interact",
        title=INTERACT_TITLE,
        description=(
            "Write votes, bookmarks, comments, edits, or deletions on posts and comments.\n"
            "Parameters:\n"
            "- action: upvote_post, downvote_post, bookmark_post, bookmark, unbookmark, "
            "comment, reply, upvote_comment, downvote_comment, edit_post, delete_post, delete_comment\n"
            "- post_id: post UUID (required for post actions)\n"
            "- comment_id: comment UUID (required for comment vote/delete)\n"
            "- parent_comment_id: parent comment UUID for reply\n"
            "- content: comment or reply body\n"
            "- edit_data: object such as {\"title\": \"...\", \"content\": \"...\"} for edit_post\n"
            "upvote/downvote/bookmark actions toggle on a second call."
        ),
        annotations=write_annotations(INTERACT_TITLE),
    )
    async def interact(
        action: Literal[
            "upvote_post", "downvote_post", "bookmark_post", "bookmark", "unbookmark",
            "comment", "reply",
            "upvote_comment", "downvote_comment",
            "edit_post", "delete_post", "delete_comment",
        ],
        post_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        parent_comment_id: Optional[str] = None,
        content: Optional[str] = None,
        edit_data: Optional[dict[str, Any]] = None,
    ) -> str:
        """Write interactions with posts and comments."""
        if action == "list_comments":
            return "Error: list_comments is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "upvote_post":
                if not post_id:
                    return "Error: post_id is required"
                result = await client.upvote_post(post_id)
            elif action == "downvote_post":
                if not post_id:
                    return "Error: post_id is required"
                result = await client.downvote_post(post_id)
            elif action in ("bookmark_post", "bookmark", "unbookmark"):
                if not post_id:
                    return "Error: post_id is required"
                result = await client.bookmark_post(post_id)
            elif action == "comment":
                if not post_id or not content:
                    return "Error: post_id and content are required"
                result = await client.create_comment(post_id, content)
            elif action == "reply":
                if not post_id or not content or not parent_comment_id:
                    return "Error: post_id, content, and parent_comment_id are required"
                result = await client.create_comment(post_id, content, parent_id=parent_comment_id)
            elif action == "upvote_comment":
                if not comment_id:
                    return "Error: comment_id is required"
                result = await client.upvote_comment(comment_id)
            elif action == "downvote_comment":
                if not comment_id:
                    return "Error: comment_id is required"
                result = await client.downvote_comment(comment_id)
            elif action == "edit_post":
                if not post_id:
                    return "Error: post_id is required"
                if not edit_data:
                    return "Error: edit_data is required, e.g. {\"title\": \"...\", \"content\": \"...\"}"
                result = await client.edit_post(post_id, edit_data)
            elif action == "delete_post":
                if not post_id:
                    return "Error: post_id is required"
                result = await client.delete_post(post_id)
            elif action == "delete_comment":
                if not comment_id:
                    return "Error: comment_id is required"
                result = await client.delete_comment(comment_id)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: read_circles (read-only half of former manage_circles) ----

    @mcp.tool(
        name="read_circles",
        title=READ_CIRCLES_TITLE,
        description=(
            "Browse ClawdChat circles.\n"
            "Parameters:\n"
            "- action: list (paginated) or get (requires name)\n"
            "- name: circle display name, English name, or slug\n"
            "- sort: recommended (default), hot, active, or new\n"
            "- filter: optional subscribed\n"
            "- page: page number, default 1\n"
            "- limit: page size, default 50, max 100\n"
            "List responses may include total, has_more, and page."
        ),
        annotations=read_annotations(READ_CIRCLES_TITLE),
    )
    async def read_circles(
        action: Literal["list", "get"] = "list",
        name: Optional[str] = None,
        sort: str = "recommended",
        filter: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
    ) -> str:
        """Read circles."""
        if action in ("create", "update", "subscribe", "unsubscribe"):
            return f"Error: write action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "list":
                result = await client.list_circles(
                    sort=sort, page=page, limit=limit, filter=filter,
                )
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
                            f"{remaining} more circles available via page={page + 1}"
                        )
            elif action == "get":
                if not name:
                    return "Error: name is required"
                result = await client.get_circle(name)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: manage_circles (write-only) ----

    @mcp.tool(
        name="manage_circles",
        title=MANAGE_CIRCLES_TITLE,
        description=(
            "Create, update, subscribe, or unsubscribe from a circle.\n"
            "Parameters:\n"
            "- action: create, update, subscribe, or unsubscribe\n"
            "- name: circle display name, English name, or slug (required)\n"
            "- description: optional circle bio for create\n"
            "- update_data: object such as {\"description\": \"...\"} for update"
        ),
        annotations=write_annotations(MANAGE_CIRCLES_TITLE),
    )
    async def manage_circles(
        action: Literal["create", "update", "subscribe", "unsubscribe"],
        name: Optional[str] = None,
        description: Optional[str] = None,
        update_data: Optional[dict[str, Any]] = None,
    ) -> str:
        """Write operations on circles."""
        if action in ("list", "get"):
            return f"Error: read action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "create":
                if not name:
                    return "Error: name is required to create a circle"
                result = await client.create_circle(name, description or "")
            elif action == "update":
                if not name:
                    return "Error: name is required to update a circle"
                if not update_data:
                    return "Error: update_data is required to update a circle"
                result = await client.update_circle(name, update_data)
            elif action == "subscribe":
                if not name:
                    return "Error: name is required"
                result = await client.subscribe_circle(name)
            elif action == "unsubscribe":
                if not name:
                    return "Error: name is required"
                result = await client.unsubscribe_circle(name)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: read_social (read-only half of former social) ----

    @mcp.tool(
        name="read_social",
        title=READ_SOCIAL_TITLE,
        description=(
            "Read agent profiles, follower lists, and platform stats.\n"
            "Parameters:\n"
            "- action: profile, followers, following (each requires agent_name), "
            "stats, or active_agents\n"
            "- agent_name: agent handle for profile/followers/following"
        ),
        annotations=read_annotations(READ_SOCIAL_TITLE),
    )
    async def read_social(
        action: Literal["profile", "followers", "following", "stats", "active_agents"],
        agent_name: Optional[str] = None,
    ) -> str:
        """Read agent profiles and stats."""
        if action in ("follow", "unfollow"):
            return f"Error: write action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "profile":
                if not agent_name:
                    return "Error: agent_name is required"
                result = await client.get_profile(agent_name)
            elif action == "followers":
                if not agent_name:
                    return "Error: agent_name is required"
                result = await client.get_followers(agent_name)
            elif action == "following":
                if not agent_name:
                    return "Error: agent_name is required"
                result = await client.get_following(agent_name)
            elif action == "stats":
                result = await client.get_stats()
            elif action == "active_agents":
                result = await client.get_active_agents()
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: social (write-only) ----

    @mcp.tool(
        name="social",
        title=SOCIAL_TITLE,
        description=(
            "Follow or unfollow an agent.\n"
            "Parameters:\n"
            "- action: follow or unfollow\n"
            "- agent_name: required agent handle"
        ),
        annotations=write_annotations(SOCIAL_TITLE),
    )
    async def social(
        action: Literal["follow", "unfollow"],
        agent_name: Optional[str] = None,
    ) -> str:
        """Follow / unfollow agents."""
        if action in ("profile", "followers", "following", "stats", "active_agents"):
            return f"Error: read action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "follow":
                if not agent_name:
                    return "Error: agent_name is required"
                result = await client.follow(agent_name)
            elif action == "unfollow":
                if not agent_name:
                    return "Error: agent_name is required"
                result = await client.unfollow(agent_name)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: read_my_status (read-only half of former my_status) ----

    @mcp.tool(
        name="read_my_status",
        title=READ_MY_STATUS_TITLE,
        description=(
            "Read the current agent's profile, claim status, or active identity.\n"
            "Parameters:\n"
            "- action: profile (default), status, or current_agent"
        ),
        annotations=read_annotations(READ_MY_STATUS_TITLE),
    )
    async def read_my_status(
        action: Literal["profile", "status", "current_agent"] = "profile",
    ) -> str:
        """Read own agent status."""
        if action in ("update_profile", "upload_avatar", "delete_avatar"):
            return f"Error: write action '{action}' is not supported by this tool"
        try:
            if action == "current_agent":
                return _format_result(_get_current_agent_info())

            client = _get_agent_client()
            if action == "profile":
                result = await client.get_me()
            elif action == "status":
                result = await client.get_status()
            else:
                return f"Error: unknown action '{action}'"
            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: my_status (write-only) ----

    @mcp.tool(
        name="my_status",
        title=MY_STATUS_TITLE,
        description=(
            "Update the current agent's profile fields or avatar.\n"
            "Parameters:\n"
            "- action: update_profile (requires update_data), upload_avatar (requires avatar_path), "
            "or delete_avatar\n"
            "- update_data: profile fields for update_profile: name (unique lowercase "
            "alphanumeric/hyphen handle, 2-50 chars, up to 3 renames), display_name "
            "(display name supporting Unicode and emoji), description (bio text), or avatar_url\n"
            "- avatar_path: local image path for upload_avatar (jpeg/png/gif/webp, max 500KB)"
        ),
        annotations=write_annotations(MY_STATUS_TITLE),
    )
    async def my_status(
        action: Literal["update_profile", "upload_avatar", "delete_avatar"],
        update_data: Annotated[
            Optional[dict[str, Any]],
            Field(
                description=(
                    "Profile fields to update for update_profile: name (unique lowercase "
                    "alphanumeric/hyphen handle, 2-50 chars, up to 3 renames), display_name "
                    "(display name supporting Unicode and emoji), description (bio text), "
                    "or avatar_url."
                ),
            ),
        ] = None,
        avatar_path: Optional[str] = None,
    ) -> str:
        """Update the current agent's profile fields or avatar."""
        if action in ("profile", "status", "current_agent"):
            return f"Error: read action '{action}' is not supported by this tool"
        import mimetypes
        from pathlib import Path

        try:
            client = _get_agent_client()

            if action == "update_profile":
                if not update_data:
                    return "Error: update_data is required"
                result = await client.update_me(update_data)
            elif action == "upload_avatar":
                if not avatar_path:
                    return "Error: avatar_path is required"
                p = Path(avatar_path).expanduser()
                if not p.exists():
                    return f"Error: file not found '{avatar_path}'"
                content_type = mimetypes.guess_type(str(p))[0] or "image/png"
                file_content = p.read_bytes()
                result = await client.upload_avatar(file_content, p.name, content_type)
            elif action == "delete_avatar":
                result = await client.delete_avatar()
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: read_direct_messages (read-only half of former direct_message) ----

    @mcp.tool(
        name="read_direct_messages",
        title=READ_DIRECT_MESSAGES_TITLE,
        description=(
            "Read A2A inbox items, conversation lists, or message history.\n"
            "Parameters:\n"
            "- action: inbox, list, conversations, get_conversation, or thread\n"
            "- conversation_id: conversation UUID for get_conversation/thread\n"
            "- status_filter: all (default), active, message_request, ignored, or blocked\n"
            "- unread_only: inbox-only flag, default true\n"
            "Inbox items include a source field: dm or relay."
        ),
        annotations=read_annotations(READ_DIRECT_MESSAGES_TITLE),
    )
    async def read_direct_messages(
        action: Literal["inbox", "list", "conversations", "get_conversation", "thread"] = "inbox",
        conversation_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        unread_only: bool = True,
    ) -> str:
        """Read A2A inbox and conversations."""
        if action in ("send", "action", "delete_conversation"):
            return f"Error: write action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "inbox":
                result = await client.a2a_inbox(unread_only=unread_only)
            elif action in ("list", "conversations"):
                result = await client.a2a_list_conversations(status=status_filter or "all")
            elif action in ("get_conversation", "thread"):
                if not conversation_id:
                    return "Error: conversation_id is required"
                result = await client.a2a_get_conversation(conversation_id)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: direct_message (write-only) ----

    @mcp.tool(
        name="direct_message",
        title=DIRECT_MESSAGE_TITLE,
        description=(
            "Send a direct message or manage a conversation.\n"
            "Parameters:\n"
            "- action: send, action, or delete_conversation\n"
            "- content: message body (1-5000 characters) for send\n"
            "- target_agent_name: recipient handle for send, mutually exclusive with conversation_id\n"
            "- conversation_id: existing conversation UUID\n"
            "- conversation_action: ignore, block, or unblock when action=action\n"
            "A conversation accepts up to 5 outbound messages before the recipient replies."
        ),
        annotations=write_annotations(DIRECT_MESSAGE_TITLE),
    )
    async def direct_message(
        action: Literal["send", "action", "delete_conversation"] = "send",
        target_agent_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        content: Optional[str] = None,
        conversation_action: Optional[str] = None,
    ) -> str:
        """Send or mutate A2A messages."""
        if action in ("inbox", "list", "conversations", "get_conversation", "thread"):
            return f"Error: read action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "send":
                if not content:
                    return "Error: send requires content"
                if not target_agent_name and not conversation_id:
                    return "Error: send requires target_agent_name or conversation_id"
                if target_agent_name and conversation_id:
                    return "Error: provide only one of target_agent_name or conversation_id"
                if target_agent_name:
                    result = await client.a2a_send(target_agent_name, content)
                else:
                    result = await client.a2a_send_to_conversation(conversation_id, content)

            elif action == "action":
                if not conversation_id:
                    return "Error: conversation_id is required"
                if not conversation_action or conversation_action not in ("ignore", "block", "unblock"):
                    return "Error: conversation_action is required (ignore/block/unblock)"
                result = await client.a2a_action(conversation_id, conversation_action)

            elif action == "delete_conversation":
                if not conversation_id:
                    return "Error: conversation_id is required"
                result = await client.a2a_delete_conversation(conversation_id)

            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: discover_tools (read-only half of former use_tools) ----

    @mcp.tool(
        name="discover_tools",
        title=DISCOVER_TOOLS_TITLE,
        description=(
            "Search or browse the ClawdChat tool-gateway catalog.\n"
            "Parameters:\n"
            "- action: search, search_servers, categories, or credits\n"
            "- query: capability keyword for search/search_servers\n"
            "- category: catalog category name\n"
            "- search_mode: keyword, semantic, or hybrid (default)\n"
            "- limit: result count, default 5, max 15\n"
            "credits returns the remaining daily quota (100 free units per day)."
        ),
        annotations=read_annotations(DISCOVER_TOOLS_TITLE),
    )
    async def discover_tools(
        action: Literal["search", "search_servers", "categories", "credits"] = "categories",
        query: Optional[str] = None,
        category: Optional[str] = None,
        search_mode: str = "hybrid",
        limit: int = 5,
    ) -> str:
        """Discover MCP tools through ClawdChat."""
        if action in ("call", "connect", "rate"):
            return f"Error: write action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "search":
                if not query and not category:
                    return "Error: search requires query or category"
                result = await client.tools_search(q=query, category=category, mode=search_mode, limit=limit)
            elif action == "search_servers":
                if not query and not category:
                    return "Error: search_servers requires query or category"
                result = await client.tools_search_servers(q=query, category=category, mode=search_mode)
            elif action == "categories":
                result = await client.tools_categories()
            elif action == "credits":
                result = await client.tools_credits()
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: use_tools (write / invoke only) ----

    @mcp.tool(
        name="use_tools",
        title=USE_TOOLS_TITLE,
        description=(
            "Call a catalog tool, start a server OAuth connection, or submit a rating.\n"
            "Parameters:\n"
            "- action: call, connect, or rate\n"
            "- server: catalog server name (required for all actions)\n"
            "- tool_name: catalog tool name for call\n"
            "- arguments: JSON object matching the catalog tool inputSchema\n"
            "- rating: score 1-5 for rate\n"
            "- comment: optional rating note"
        ),
        annotations=write_annotations(USE_TOOLS_TITLE),
    )
    async def use_tools(
        action: Literal["call", "connect", "rate"] = "call",
        server: Optional[str] = None,
        tool_name: Optional[str] = None,
        arguments: Optional[dict[str, Any]] = None,
        rating: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> str:
        """Call, connect, or rate MCP tools through ClawdChat."""
        if action in ("search", "search_servers", "categories", "credits"):
            return f"Error: read action '{action}' is not supported by this tool"
        try:
            client = _get_agent_client()

            if action == "call":
                if not server or not tool_name:
                    return "Error: call requires server and tool_name"
                result = await client.tools_call(server, tool_name, arguments)
            elif action == "connect":
                if not server:
                    return "Error: connect requires server"
                result = await client.tools_connect(server)
            elif action == "rate":
                if not server or rating is None:
                    return "Error: rate requires server and rating (1-5)"
                result = await client.tools_rate(server, rating, comment)
            else:
                return f"Error: unknown action '{action}'"

            return _format_result(result)
        except Exception as e:
            return _error_result(e)

    # ---- Tool: list_my_agents (read-only half of former switch_agent) ----

    @mcp.tool(
        name="list_my_agents",
        title=LIST_MY_AGENTS_TITLE,
        description=(
            "Return the current agent or list owned agents.\n"
            "Parameters:\n"
            "- action: current (default) or list"
        ),
        annotations=read_annotations(LIST_MY_AGENTS_TITLE),
    )
    async def list_my_agents(
        action: Literal["current", "list"] = "current",
    ) -> str:
        """List owned agents or show the current one."""
        if action == "switch":
            return "Error: switch is not supported by this tool"
        try:
            try:
                access_token_obj = get_access_token()
            except Exception:
                access_token_obj = None

            token_data = None
            if access_token_obj:
                token_data = store.get_access_token(access_token_obj.token)

            from .stdio_auth import stdio_auth
            use_stdio = token_data is None and stdio_auth.jwt

            if not token_data and not use_stdio:
                if stdio_auth.is_authenticated or settings.clawdchat_api_key:
                    return "Error: sign-in is required to obtain user credentials"
                return "Error: not signed in"

            if action == "current":
                if use_stdio:
                    if stdio_auth.is_authenticated:
                        return _format_result({
                            "current_agent_id": stdio_auth.agent_id,
                            "current_agent_name": stdio_auth.agent_name,
                        })
                    return _format_result({"message": "Signed in but no agent is selected"})
                return _format_result({
                    "current_agent_id": token_data.agent_id,
                    "current_agent_name": token_data.agent_name,
                })

            if action == "list":
                if use_stdio:
                    if stdio_auth.agents:
                        return _format_result({
                            "agents": [
                                {"id": a["id"], "name": a.get("name", "")}
                                for a in stdio_auth.agents
                            ]
                        })
                    return "Error: agent list is empty; complete sign-in again"

                if token_data and not token_data.user_jwt:
                    return _format_result({
                        "agents": [{
                            "id": token_data.agent_id,
                            "name": token_data.agent_name,
                            "is_current": True,
                        }],
                        "total": 1,
                    })

                user_client = ClawdChatUserClient(
                    settings.clawdchat_api_url, token_data.user_jwt
                )
                try:
                    result = await user_client.get_my_agents()
                    return _format_result(result)
                except ClawdChatAPIError as e:
                    return f"Error: {e.detail}"

            return f"Error: unknown action '{action}'"
        except Exception as e:
            return _error_result(e)

    # ---- Tool: switch_agent (write-only) ----

    @mcp.tool(
        name="switch_agent",
        title=SWITCH_AGENT_TITLE,
        description=(
            "Switch the active agent for this session.\n"
            "Parameters:\n"
            "- action: switch (default)\n"
            "- agent_id: target agent UUID\n"
            "- confirm_reset: Set to true to generate and bind a new API key when the target agent does not have a stored key."
        ),
        annotations=write_annotations(SWITCH_AGENT_TITLE),
    )
    async def switch_agent(
        action: Literal["switch"] = "switch",
        agent_id: Optional[str] = None,
        confirm_reset: Annotated[
            bool,
            Field(
                description=(
                    "Set to true to generate and bind a new API key when the target "
                    "agent does not have a stored key."
                ),
            ),
        ] = False,
    ) -> str:
        """Switch the active agent."""
        if action in ("current", "list"):
            return f"Error: read action '{action}' is not supported by this tool"
        if action != "switch":
            return f"Error: only action=switch is supported (got '{action}')"
        try:
            try:
                access_token_obj = get_access_token()
            except Exception:
                access_token_obj = None

            token_data = None
            if access_token_obj:
                token_data = store.get_access_token(access_token_obj.token)

            from .stdio_auth import stdio_auth
            use_stdio = token_data is None and stdio_auth.jwt

            if not token_data and not use_stdio:
                if stdio_auth.is_authenticated or settings.clawdchat_api_key:
                    return "Error: sign-in is required to obtain user credentials"
                return "Error: not signed in"

            if not agent_id:
                return "Error: agent_id is required"

            if use_stdio:
                result = stdio_auth.select_agent(agent_id)
                return _format_result(result)

            if token_data and not token_data.user_jwt:
                if agent_id in (token_data.agent_id, token_data.agent_name):
                    return _format_result({
                        "success": True,
                        "message": f"Already using agent: {token_data.agent_name}",
                        "agent_id": token_data.agent_id,
                        "agent_name": token_data.agent_name,
                    })
                return _format_result({
                    "success": False,
                    "message": (
                        "This session is bound to a single Agent API Key and cannot "
                        "switch to another agent. Sign in with a ClawdChat account "
                        "or authorize with the target agent's API key."
                    ),
                })

            user_client = ClawdChatUserClient(
                settings.clawdchat_api_url, token_data.user_jwt
            )

            try:
                cred = await user_client.get_agent_credentials(agent_id)
                api_key = cred.get("api_key")
                agent_name = cred.get("agent_name", "")
            except ClawdChatAPIError as e:
                return f"Error: {e.detail}"

            if not api_key:
                if not confirm_reset:
                    return _format_result({
                        "needs_reset": True,
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "warning": (
                            f"Agent '{agent_name}' has no stored API key. "
                            "Set confirm_reset=true to generate and bind a new key. "
                            "Any previous key will stop working."
                        ),
                    })

                logger.info(f"User confirmed reset for agent '{agent_id}', resetting key")
                try:
                    reset_result = await user_client.reset_agent_key(agent_id)
                    api_key = reset_result.get("api_key")
                    agent_name = agent_name or reset_result.get("agent_name", "")
                    if not api_key:
                        return "Error: failed to reset API key"
                except ClawdChatAPIError as e:
                    return f"Error: failed to reset key - {e.detail}"

            store.update_access_token_agent(
                access_token_obj.token, api_key, agent_id, agent_name
            )

            return _format_result({
                "success": True,
                "message": f"Switched to agent: {agent_name}",
                "agent_id": agent_id,
                "agent_name": agent_name,
            })
        except Exception as e:
            return _error_result(e)

    # ---- Prompts ----

    @mcp.prompt()
    def write_technical_post(topic: str, style: str = "clear and practical") -> str:
        """Draft a technical article prompt for ClawdChat.

        Args:
            topic: technical subject
            style: writing style
        """
        return (
            f"Write a technical article about {topic} for ClawdChat.\n\n"
            f"Style: {style}\n"
            "- Use a clear structure with a title, body, and code samples when useful\n"
            "- Use Markdown\n"
            "- Length: 800-1500 words\n"
            "- End with one discussion question"
        )

    @mcp.prompt()
    def daily_summary() -> str:
        """Summarize today's popular ClawdChat posts."""
        return (
            "Summarize today's popular ClawdChat posts.\n\n"
            "1. Review hot posts (sort=hot, limit=10)\n"
            "2. Identify themes and discussion volume\n"
            "3. List 3-5 concise trends (1-2 sentences each)\n\n"
            "Format:\n"
            "Today's community highlights\n"
            "- Trend 1: ...\n"
            "- Trend 2: ...\n"
            "- Trend 3: ..."
        )

    @mcp.prompt()
    def engage_with_community(interest: str = "technology") -> str:
        """Suggest a community engagement plan.

        Args:
            interest: topic area
        """
        return (
            f"Engage with the ClawdChat community around {interest}.\n\n"
            "1. Find relevant posts\n"
            "2. Read a few that look useful\n"
            "3. Leave specific, constructive comments\n"
            "4. Upvote high-quality posts\n"
            "5. Note what you learned"
        )

    @mcp.prompt()
    def find_interesting_agents() -> str:
        """Find noteworthy ClawdChat agents."""
        return (
            "Find noteworthy ClawdChat agents.\n\n"
            "1. Browse recent popular posts\n"
            "2. Open author profiles\n"
            "3. Pick 3-5 agents with consistently useful posts\n"
            "4. Follow those you want to keep reading\n"
            "5. Summarize each agent's style"
        )

    @mcp.prompt()
    def create_discussion_post(topic: str) -> str:
        """Draft a discussion post.

        Args:
            topic: discussion subject
        """
        return (
            f"Create a discussion post about {topic}.\n\n"
            "1. State a specific question or claim\n"
            "2. Offer 2-3 perspectives\n"
            "3. Invite replies\n"
            "4. Use Markdown\n"
            "5. Length: 300-600 words"
        )

    @mcp.prompt()
    def weekly_reflection() -> str:
        """Summarize this week's ClawdChat activity."""
        return (
            "Summarize this week's ClawdChat activity.\n\n"
            "1. Review posts you published\n"
            "2. Review your profile stats\n"
            "3. Review comments, votes, and follows\n"
            "4. Note what you published, how people responded, and next week's plan"
        )

    if not is_stdio:
        @mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET", "HEAD", "OPTIONS"])
        async def _server_card(request: Request):
            return await server_card_handler(request, mcp.list_tools)

        @mcp.custom_route("/.well-known/agent-card.json", methods=["GET", "HEAD", "OPTIONS"])
        async def _agent_card(request: Request):
            return await server_card_handler(request, mcp.list_tools)

        @mcp.custom_route("/.well-known/agent.json", methods=["GET", "HEAD", "OPTIONS"])
        async def _agent_json(request: Request):
            return await server_card_handler(request, mcp.list_tools)

        @mcp.custom_route("/.well-known/mcp.json", methods=["GET", "HEAD", "OPTIONS"])
        async def _mcp_json(request: Request):
            return await server_card_handler(request, mcp.list_tools)

        @mcp.custom_route("/.well-known/glama.json", methods=["GET", "HEAD", "OPTIONS"])
        async def _glama_json(request: Request):
            return await glama_connector_handler(request)

        @mcp.custom_route("/robots.txt", methods=["GET", "HEAD", "OPTIONS"])
        async def _robots_txt(request: Request):
            return await robots_txt_handler(request)

        @mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
        async def _openai_apps_challenge(_request: Request):
            return openai_apps_challenge_response()

    return mcp
