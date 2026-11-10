"""ClawdChat MCP Server - FastMCP server with tool definitions.

Read tools and write tools are registered separately so Claude / OpenAI
directory scanners never see a catch-all tool that mixes GET and POST.
"""

import json
import logging
import re
from typing import Annotated, Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, BeforeValidator, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.auth.middleware.auth_context import get_access_token as _sdk_get_access_token
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import REGISTRATION_PATH, build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .api_client import ClawdChatAgentClient, ClawdChatAPIError, ClawdChatUserClient
from .auth_confirm import (
    CONFIRM_RESET_FIELD_DESCRIPTION,
    confirm_reset_is_true,
    find_owned_agent,
    strict_bool_confirm_reset,
)
from .auth_provider import (
    ClawdChatOAuthProvider,
    claim_limit_payload,
    clawdchat_callback_handler,
    confirm_create_callback_handler,
    confirm_create_page_handler,
    confirm_create_submit_handler,
    login_page_handler,
    new_create_ticket,
    login_submit_handler,
    quota_is_full,
    quota_snapshot,
    select_agent_callback_handler,
    select_agent_page_handler,
)
from .config import settings
from .anon_discovery import DiscoveryAwareAuthMiddleware
from .dcr import make_dcr_handler
from .discovery import (
    LOGO_PATH,
    LOGO_ROUTE_PATHS,
    WEBSITE_URL,
    logo_handler,
    server_icons,
    PUBLIC_CARD_PATHS,
    glama_connector_handler,
    openai_apps_challenge_response,
    oauth_protected_resource_handler,
    robots_txt_handler,
    server_card_handler,
)
from .storage import store
from .events import events_enabled
from .events import runtime as events_runtime
from .events.middleware import EventsMiddleware
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

ConfirmResetParam = Annotated[
    bool,
    BeforeValidator(strict_bool_confirm_reset),
    Field(description=CONFIRM_RESET_FIELD_DESCRIPTION),
]
