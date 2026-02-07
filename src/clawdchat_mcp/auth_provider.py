"""OAuth Authorization Server Provider for ClawdChat MCP Server.

Implements MCP's OAuthAuthorizationServerProvider interface.
Handles the OAuth 2.1 flow with PKCE, delegating user authentication
to the ClawdChat backend API.
"""

import logging
import time
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken as OAuthTokenType

from .api_client import ClawdChatAPIError, ClawdChatUserClient
from .config import settings
from .storage import (
    AccessTokenData,
    AuthCodeData,
    OAuthClientData,
    PendingLogin,
    RefreshTokenData,
    TokenStore,
    store,
)

logger = logging.getLogger(__name__)

# Load Jinja2 templates
TEMPLATES_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

# Token expiration: 1 hour access, 7 days refresh
ACCESS_TOKEN_EXPIRY = 3600
REFRESH_TOKEN_EXPIRY = 7 * 24 * 3600
AUTH_CODE_EXPIRY = 300  # 5 minutes


class ClawdChatOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth provider that authenticates via ClawdChat API."""

    def __init__(self, store: TokenStore):
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Retrieve registered client by ID."""
        data = self.store.get_client(client_id)
        if not data:
            return None
        return OAuthClientInformationFull(
            client_id=data.client_id,
            client_secret=data.client_secret,
            client_id_issued_at=data.client_id_issued_at,
            client_secret_expires_at=data.client_secret_expires_at,
            redirect_uris=[AnyUrl(u) for u in data.redirect_uris],
            client_name=data.client_name,
            grant_types=data.grant_types,
            response_types=data.response_types,
            token_endpoint_auth_method=data.token_endpoint_auth_method,
            scope=data.scope,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new OAuth client (dynamic registration)."""
        if not client_info.redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect_uri is required",
            )

        self.store.store_client(OAuthClientData(
            client_id=client_info.client_id,
            client_secret=client_info.client_secret,
            client_id_issued_at=client_info.client_id_issued_at,
            client_secret_expires_at=client_info.client_secret_expires_at,
            redirect_uris=[str(u) for u in client_info.redirect_uris],
            client_name=client_info.client_name,
            grant_types=client_info.grant_types or ["authorization_code", "refresh_token"],
            response_types=client_info.response_types or ["code"],
            token_endpoint_auth_method=client_info.token_endpoint_auth_method or "none",
            scope=client_info.scope,
        ))
        logger.info(f"Registered OAuth client: {client_info.client_id}")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Start authorization flow -> redirect to login page."""
        # Generate a unique state for this login attempt (internal use)
        state = self.store.generate_token()

        # Store the pending login, preserving the original OAuth state from the MCP client
        self.store.store_pending_login(PendingLogin(
            state=state,
            oauth_state=params.state or "",
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=params.scopes or ["agent"],
            resource=params.resource,
        ))

        # Redirect to our login page
        return f"{settings.mcp_server_url}/auth/login?state={state}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Load an authorization code."""
        data = self.store.get_auth_code(authorization_code)
        if not data:
            return None
        if data.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=data.code,
            client_id=data.client_id,
            redirect_uri=AnyUrl(data.redirect_uri),
            redirect_uri_provided_explicitly=data.redirect_uri_provided_explicitly,
            code_challenge=data.code_challenge,
            scopes=data.scopes,
            expires_at=data.expires_at,
            resource=data.resource,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthTokenType:
        """Exchange authorization code for tokens."""
        # Consume the code (one-time use)
        code_data = self.store.consume_auth_code(authorization_code.code)
        if not code_data:
            raise TokenError(error="invalid_grant", error_description="Authorization code not found or expired")

        # Generate tokens
        access_token = self.store.generate_token()
        refresh_token = self.store.generate_token()
        now = int(time.time())

        # Store access token
        self.store.store_access_token(AccessTokenData(
            token=access_token,
            client_id=client.client_id,
            scopes=code_data.scopes,
            expires_at=now + ACCESS_TOKEN_EXPIRY,
            agent_api_key=code_data.agent_api_key,
            agent_id=code_data.agent_id,
            agent_name=code_data.agent_name,
            user_jwt=code_data.user_jwt,
            resource=code_data.resource,
        ))

        # Store refresh token
        self.store.store_refresh_token(RefreshTokenData(
            token=refresh_token,
            client_id=client.client_id,
            scopes=code_data.scopes,
            expires_at=now + REFRESH_TOKEN_EXPIRY,
            agent_api_key=code_data.agent_api_key,
            agent_id=code_data.agent_id,
            agent_name=code_data.agent_name,
            user_jwt=code_data.user_jwt,
        ))

        logger.info(f"Issued tokens for agent '{code_data.agent_name}' (client: {client.client_id})")

        return OAuthTokenType(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY,
            refresh_token=refresh_token,
            scope=" ".join(code_data.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Load a refresh token."""
        data = self.store.get_refresh_token(refresh_token)
        if not data:
            return None
        if data.client_id != client.client_id:
            return None
        return RefreshToken(
            token=data.token,
            client_id=data.client_id,
            scopes=data.scopes,
            expires_at=data.expires_at,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthTokenType:
        """Exchange refresh token for new tokens."""
        old_data = self.store.get_refresh_token(refresh_token.token)
        if not old_data:
            raise TokenError(error="invalid_grant", error_description="Refresh token not found or expired")

        # Revoke old refresh token
        self.store.revoke_refresh_token(refresh_token.token)

        # Generate new tokens
        new_access = self.store.generate_token()
        new_refresh = self.store.generate_token()
        now = int(time.time())

        use_scopes = scopes if scopes else old_data.scopes

        self.store.store_access_token(AccessTokenData(
            token=new_access,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=now + ACCESS_TOKEN_EXPIRY,
            agent_api_key=old_data.agent_api_key,
            agent_id=old_data.agent_id,
            agent_name=old_data.agent_name,
            user_jwt=old_data.user_jwt,
        ))

        self.store.store_refresh_token(RefreshTokenData(
            token=new_refresh,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=now + REFRESH_TOKEN_EXPIRY,
            agent_api_key=old_data.agent_api_key,
            agent_id=old_data.agent_id,
            agent_name=old_data.agent_name,
            user_jwt=old_data.user_jwt,
        ))

        return OAuthTokenType(
            access_token=new_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY,
            refresh_token=new_refresh,
            scope=" ".join(use_scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify and load an access token."""
        data = self.store.get_access_token(token)
        if not data:
            return None
        return AccessToken(
            token=data.token,
            client_id=data.client_id,
            scopes=data.scopes,
            expires_at=data.expires_at,
            resource=data.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token."""
        if isinstance(token, AccessToken):
            self.store.revoke_access_token(token.token)
        elif isinstance(token, RefreshToken):
            self.store.revoke_refresh_token(token.token)


# ---- HTTP handlers for login/agent-selection pages ----


async def login_page_handler(request: Request) -> HTMLResponse:
    """GET /auth/login - Show login page."""
    state = request.query_params.get("state", "")
    if not state or not store.get_pending_login(state):
        return HTMLResponse("<h1>Invalid or expired login session</h1>", status_code=400)

    # Build Google auth URL if configured
    google_auth_url = ""
    if settings.google_enabled:
        google_auth_url = build_google_auth_url(state)

    template = jinja_env.get_template("login.html")
    html = template.render(
        state=state,
        google_enabled=settings.google_enabled,
        google_auth_url=google_auth_url,
    )
    return HTMLResponse(html)


async def login_callback_handler(request: Request) -> JSONResponse:
    """POST /auth/login/callback - Handle phone login."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    phone = body.get("phone", "").strip()
    state = body.get("state", "")

    if not phone or not state:
        return JSONResponse({"error": "缺少手机号或 state"}, status_code=400)

    pending = store.get_pending_login(state)
    if not pending:
        return JSONResponse({"error": "登录会话已过期，请重新开始"}, status_code=400)

    # Authenticate via ClawdChat API
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, "")
        login_result, jwt_token = await user_client.phone_login(phone)
    except ClawdChatAPIError as e:
        return JSONResponse({"error": f"登录失败: {e.detail}"}, status_code=400)
    except Exception as e:
        logger.exception("Login error")
        return JSONResponse({"error": f"登录失败: {str(e)}"}, status_code=500)

    if not jwt_token:
        return JSONResponse({"error": "登录失败: 未获取到认证令牌"}, status_code=400)

    # Update pending login with user JWT
    pending.user_jwt = jwt_token
    pending.user_info = login_result.get("user")

    # Fetch user's agents
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, jwt_token)
        agents_result = await user_client.get_my_agents()
        agents = agents_result.get("agents", [])
    except ClawdChatAPIError as e:
        return JSONResponse({"error": f"获取 Agent 列表失败: {e.detail}"}, status_code=400)

    if not agents:
        return JSONResponse({
            "error": "你还没有认领任何 Agent，请先在 ClawdChat 认领一个 Agent"
        }, status_code=400)

    if len(agents) == 1:
        # Auto-select the only agent
        agent = agents[0]
        return await _complete_authorization(state, pending, agent["id"], agent["name"])

    # Multiple agents -> redirect to selection page
    return JSONResponse({"redirect": f"/auth/select-agent?state={state}"})


async def select_agent_page_handler(request: Request) -> HTMLResponse:
    """GET /auth/select-agent - Show agent selection page."""
    state = request.query_params.get("state", "")
    pending = store.get_pending_login(state)
    if not pending or not pending.user_jwt:
        return HTMLResponse("<h1>Invalid or expired session</h1>", status_code=400)

    # Fetch agents
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, pending.user_jwt)
        agents_result = await user_client.get_my_agents()
        agents = agents_result.get("agents", [])
    except Exception:
        agents = []

    template = jinja_env.get_template("select_agent.html")
    html = template.render(state=state, agents=agents)
    return HTMLResponse(html)


async def select_agent_callback_handler(request: Request) -> JSONResponse:
    """POST /auth/select-agent - Handle agent selection."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    state = body.get("state", "")
    agent_id = body.get("agent_id", "")
    agent_name = body.get("agent_name", "")
    confirm_reset = body.get("confirm_reset", False)

    if not state or not agent_id:
        return JSONResponse({"error": "缺少参数"}, status_code=400)

    pending = store.get_pending_login(state)
    if not pending or not pending.user_jwt:
        return JSONResponse({"error": "会话已过期"}, status_code=400)

    return await _complete_authorization(state, pending, agent_id, agent_name, confirm_reset=confirm_reset)


async def _complete_authorization(
    state: str, pending: PendingLogin, agent_id: str, agent_name: str,
    confirm_reset: bool = False,
) -> JSONResponse:
    """Complete the OAuth authorization: get agent credentials, issue auth code, redirect."""
    # Get agent's API key
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, pending.user_jwt)
        cred_result = await user_client.get_agent_credentials(agent_id)
        api_key = cred_result.get("api_key")

        if not api_key:
            if not confirm_reset:
                # Ask user to confirm before resetting
                logger.info(f"Agent '{agent_name}' has no stored key, asking user to confirm reset")
                return JSONResponse({
                    "needs_reset": True,
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "message": (
                        f"Agent「{agent_name}」注册较早，未存储 API Key，"
                        "需要重置生成新 Key 后才能使用。重置后原有 Key 将失效。"
                    ),
                })

            # User confirmed, proceed with reset
            logger.info(f"User confirmed reset for agent '{agent_name}', resetting key")
            reset_result = await user_client.reset_agent_key(agent_id)
            api_key = reset_result.get("api_key")
            if not api_key:
                return JSONResponse({"error": "重置 API Key 失败"}, status_code=400)
    except ClawdChatAPIError as e:
        return JSONResponse({"error": f"获取凭证失败: {e.detail}"}, status_code=400)

    # Generate authorization code
    code = store.generate_code()
    store.store_auth_code(AuthCodeData(
        code=code,
        client_id=pending.client_id,
        redirect_uri=pending.redirect_uri,
        redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
        code_challenge=pending.code_challenge,
        scopes=pending.scopes,
        expires_at=time.time() + AUTH_CODE_EXPIRY,
        agent_api_key=api_key,
        agent_id=agent_id,
        agent_name=agent_name,
        user_jwt=pending.user_jwt,
        resource=pending.resource,
    ))

    # Consume pending login
    store.consume_pending_login(state)

    # Redirect back to client with authorization code and original OAuth state
    redirect_uri = construct_redirect_uri(
        pending.redirect_uri, code=code, state=pending.oauth_state or None
    )
    logger.info(f"Authorization complete for agent '{agent_name}', redirecting to client")

    return JSONResponse({"redirect": redirect_uri})


# ---- Google OAuth handlers ----

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def build_google_auth_url(mcp_oauth_state: str) -> str:
    """Build Google OAuth authorization URL.

    Uses the same Google OAuth App as ClawdChat (shared client_id).
    The MCP server's redirect_uri must be registered in Google Console.
    """
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "email profile",
        "access_type": "offline",
        "state": mcp_oauth_state,  # pass through MCP internal state
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def google_callback_handler(request: Request) -> HTMLResponse:
    """GET /auth/google/callback - Handle Google OAuth callback.

    Google redirects here after user authorizes. We then:
    1. Exchange code via ClawdChat's /api/v1/auth/google/api-login
    2. Get JWT token for this user
    3. Continue MCP OAuth flow (fetch agents, select agent, etc.)
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            f"<h1>Google 登录失败</h1><p>{error}</p>",
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            "<h1>缺少必要参数</h1>",
            status_code=400,
        )

    pending = store.get_pending_login(state)
    if not pending:
        return HTMLResponse(
            "<h1>登录会话已过期，请重新开始</h1>",
            status_code=400,
        )

    # Exchange code via ClawdChat backend
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, "")
        login_result, jwt_token = await user_client.google_api_login(
            code=code,
            redirect_uri=settings.google_redirect_uri,
        )
    except ClawdChatAPIError as e:
        logger.error(f"Google login via ClawdChat failed: {e}")
        return HTMLResponse(
            f"<h1>Google 登录失败</h1><p>{e.detail}</p>",
            status_code=400,
        )
    except Exception as e:
        logger.exception("Google login error")
        return HTMLResponse(
            f"<h1>Google 登录失败</h1><p>{str(e)}</p>",
            status_code=500,
        )

    if not jwt_token:
        return HTMLResponse(
            "<h1>Google 登录失败</h1><p>未获取到认证令牌</p>",
            status_code=400,
        )

    # Update pending login with JWT
    pending.user_jwt = jwt_token
    pending.user_info = login_result.get("user")

    # Fetch user's agents
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, jwt_token)
        agents_result = await user_client.get_my_agents()
        agents = agents_result.get("agents", [])
    except ClawdChatAPIError as e:
        return HTMLResponse(
            f"<h1>获取 Agent 列表失败</h1><p>{e.detail}</p>",
            status_code=400,
        )

    if not agents:
        return HTMLResponse(
            "<h1>你还没有认领任何 Agent</h1>"
            "<p>请先在 ClawdChat 认领一个 Agent</p>",
            status_code=400,
        )

    if len(agents) == 1:
        # Auto-select the only agent
        agent = agents[0]
        result = await _complete_authorization(state, pending, agent["id"], agent["name"])
        # _complete_authorization returns JSONResponse; for browser redirect we need to extract
        data = result.body.decode()
        import json as _json
        result_data = _json.loads(data)
        if result_data.get("redirect"):
            return HTMLResponse(
                f'<html><head><meta http-equiv="refresh" content="0;url={result_data["redirect"]}" />'
                f'</head><body>正在跳转...</body></html>'
            )
        elif result_data.get("needs_reset"):
            # Redirect to select-agent page for confirmation
            return RedirectResponse(
                url=f"{settings.mcp_server_url}/auth/select-agent?state={state}",
                status_code=302,
            )
        else:
            return HTMLResponse(
                f"<h1>错误</h1><p>{result_data.get('error', '未知错误')}</p>",
                status_code=400,
            )

    # Multiple agents -> redirect to select-agent page
    return RedirectResponse(
        url=f"{settings.mcp_server_url}/auth/select-agent?state={state}",
        status_code=302,
    )
