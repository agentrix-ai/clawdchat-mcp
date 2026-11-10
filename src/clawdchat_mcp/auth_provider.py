"""OAuth Authorization Server Provider for ClawdChat MCP Server.

Implements MCP's OAuthAuthorizationServerProvider interface.
Handles the OAuth 2.1 flow with PKCE, delegating user authentication
to the ClawdChat backend via its external auth (IdP) endpoint.

Authentication flow (account-first, hosted HTTP):
  MCP Client -> MCP Server -> ClawdChat account login (email / Google; .cn also phone)
  -> consent page ("may use all your Agents and create new ones"; optionally bind one now)
  -> MCP Client. The session is bound to the *user*; Agents are bound / switched /
  created later with tools (switch_agent). Resetting a key still needs confirm_reset=true.
  Pasting an Agent API Key (Channel B) still binds that single Agent only.
"""

import html
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken as OAuthTokenType

from .oauth_redirect import AllowlistedOAuthClient

from .api_client import ClawdChatAPIError, ClawdChatClient, ClawdChatUserClient
from .auth_confirm import (
    AGENT_NOT_OWNED,
    CONFIRM_RESET_MUST_BE_BOOL,
    ConfirmResetTypeError,
    confirm_reset_from_mapping,
    confirm_reset_is_true,
    find_owned_agent,
)
from .config import settings
from .user_jwt import renew_if_needed
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

# Token expiration: 7 days access, 90 days refresh
ACCESS_TOKEN_EXPIRY = 7 * 24 * 3600
REFRESH_TOKEN_EXPIRY = 90 * 24 * 3600
AUTH_CODE_EXPIRY = 300  # 5 minutes


class ClawdChatOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth provider that authenticates via ClawdChat API."""

    def __init__(self, store: TokenStore):
        self.store = store

    async def get_client(self, client_id: str) -> AllowlistedOAuthClient | None:
        """Retrieve registered client by ID."""
        data = self.store.get_client(client_id)
        if not data:
            return None
        return AllowlistedOAuthClient(
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

        # Redirect to our login page (which then redirects to ClawdChat)
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
            refresh_token=refresh_token,
            create_grants=code_data.create_grants,
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
            create_grants=code_data.create_grants,
        ))

        logger.info(
            f"Issued tokens for agent '{code_data.agent_name or '(none bound)'}' "
            f"(client: {client.client_id})"
        )

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
        # Keep the ClawdChat user JWT alive alongside the 90-day refresh token
        user_jwt = await renew_if_needed(old_data.user_jwt) if old_data.user_jwt else ""

        self.store.store_access_token(AccessTokenData(
            token=new_access,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=now + ACCESS_TOKEN_EXPIRY,
            agent_api_key=old_data.agent_api_key,
            agent_id=old_data.agent_id,
            agent_name=old_data.agent_name,
            user_jwt=user_jwt,
            refresh_token=new_refresh,
            create_grants=old_data.create_grants,
        ))

        self.store.store_refresh_token(RefreshTokenData(
            token=new_refresh,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=now + REFRESH_TOKEN_EXPIRY,
            agent_api_key=old_data.agent_api_key,
            agent_id=old_data.agent_id,
            agent_name=old_data.agent_name,
            user_jwt=user_jwt,
            create_grants=old_data.create_grants,
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


class LoginSiteCopy:
    """`.ai` is English-only (no Phone); `.cn` keeps bilingual phone/Google copy."""

    _LOCAL_TEST_HOSTS = frozenset({"testserver", "localhost", "127.0.0.1", "::1"})
    _ERRORS = {
        "invalid_body": ("Invalid request body", "Invalid request body"),
        "missing_state": ("Missing state parameter", "缺少 state 参数"),
        "expired": ("Login session expired or invalid", "登录会话已过期或无效"),
        "missing_key": ("Please enter an Agent API Key", "请输入 Agent API Key"),
        "verify_failed": (
            "API Key verification failed: {detail}",
            "API Key 验证失败: {detail}",
        ),
        "invalid_profile": (
            "Could not load a valid Agent profile from this API Key",
            "未能从 API Key 获取有效的 Agent 资料",
        ),
        "invalid_agent_id": (
            "Could not load a valid Agent ID from this API Key",
            "未能从 API Key 获取有效的 Agent ID",
        ),
    }

    @classmethod
    def host_header(cls, request: Request) -> str:
        raw = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or ""
        )
        return raw.split(",")[0].strip().lower()

    @classmethod
    def is_cn(cls, request: Request) -> bool:
        """True only for the .cn site (or local tests configured against .cn)."""
        host = cls.host_header(request)
        if "clawdchat.cn" in host:
            return True
        hostname = host.split(":")[0].strip("[]")
        if not host or hostname in cls._LOCAL_TEST_HOSTS:
            return (
                "clawdchat.cn" in settings.clawdchat_api_url.lower()
                or "clawdchat.cn" in settings.mcp_server_url.lower()
            )
        return False

    @classmethod
    def error(cls, is_cn: bool, key: str, **kwargs: str) -> str:
        en, zh = cls._ERRORS[key]
        return (zh if is_cn else en).format(**kwargs)


class SelectAgentCopy:
    """`.ai` English / `.cn` Chinese copy for the Agent confirmation gate."""

    _ERRORS = {
        "missing_params": ("Missing required parameters", "缺少必要参数"),
        "expired": (
            "Sign-in session expired. Please start again.",
            "登录会话已过期，请重新开始",
        ),
        "auth_failed": ("Authentication failed", "认证失败"),
        "no_token": (
            "Authentication failed: no token received",
            "认证失败：未获取到认证令牌",
        ),
        "agents_failed": ("Failed to load Agent list", "获取 Agent 列表失败"),
        "no_agents": (
            "You have not claimed any Agent. Claim one on ClawdChat first.",
            "你还没有认领任何 Agent。请先在 ClawdChat 认领一个 Agent",
        ),
        "invalid_body": ("Invalid request body", "无效的请求"),
        "missing_agent": ("Missing agent_id", "缺少参数"),
        "session_expired": ("Session expired", "会话已过期"),
        "not_owned": (AGENT_NOT_OWNED, "该 Agent 不在你名下"),
        "confirm_type": (CONFIRM_RESET_MUST_BE_BOOL, "confirm_reset 必须是布尔值 true/false"),
        "cred_failed": ("Failed to load credentials: {detail}", "获取凭证失败: {detail}"),
        "reset_failed": ("Failed to reset API Key", "重置 API Key 失败"),
        "unknown": ("Unknown error", "未知错误"),
    }

    @classmethod
    def error(cls, is_cn: bool, key: str, **kwargs: str) -> str:
        en, zh = cls._ERRORS[key]
        return (zh if is_cn else en).format(**kwargs)

    @classmethod
    def reset_message(cls, is_cn: bool, agent_name: str) -> str:
        if is_cn:
            return (
                f"Agent「{agent_name}」注册较早，未存储 API Key，"
                "需要重置生成新 Key 后才能使用。重置后原有 Key 将失效。"
            )
        return (
            f"Agent '{agent_name}' has no stored API key. "
            "Set confirm_reset=true to generate and bind a new key. "
            "Any previous key will stop working."
        )


def is_cn_site(request: Request) -> bool:
    """Public wrapper used by login handlers and unit tests."""
    return LoginSiteCopy.is_cn(request)


def _build_clawdchat_auth_url(state: str) -> str:
    """Build the original ClawdChat account-login authorize URL."""
    callback_url = f"{settings.mcp_server_url}/auth/clawdchat/callback"
    params = urlencode({
        "callback_url": callback_url,
        "state": state,
    })
    return f"{settings.clawdchat_api_url}/api/v1/auth/external/authorize?{params}"


def _is_json_login_request(request: Request) -> bool:
    content_type = (request.headers.get("content-type") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in content_type or "application/json" in accept


def _render_login_page(
    request: Request, state: str, error: str | None = None
) -> HTMLResponse:
    template = jinja_env.get_template("login.html")
    html = template.render(
        state=state,
        clawdchat_auth_url=_build_clawdchat_auth_url(state),
        error=error,
        is_cn=is_cn_site(request),
    )
    return HTMLResponse(html, status_code=400 if error else 200)


def _login_error_response(
    request: Request,
    wants_json: bool,
    message: str,
    *,
    state: str = "",
    pending: PendingLogin | None = None,
) -> Response:
    if wants_json:
        return JSONResponse({"error": message}, status_code=400)
    if pending is not None and state:
        return _render_login_page(request, state, error=message)
    return HTMLResponse(f"<h1>{message}</h1>", status_code=400)


async def _read_login_credentials(request: Request) -> tuple[str, str]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return str(body.get("state") or "").strip(), str(body.get("api_key") or "").strip()

    form = await request.form()
    return str(form.get("state") or "").strip(), str(form.get("api_key") or "").strip()


async def login_page_handler(request: Request) -> Response:
    """GET /auth/login — dual-channel login page (.ai English / .cn bilingual).

    Channel A redirects to ClawdChat account authorize (Google on .ai;
    phone / Google on .cn). Channel B posts an Agent API Key.
    """
    state = request.query_params.get("state", "")
    if not state or not store.get_pending_login(state):
        return HTMLResponse("<h1>Invalid or expired login session</h1>", status_code=400)

    return _render_login_page(request, state)


async def login_submit_handler(request: Request) -> Response:
    """POST /auth/login — authorize directly with an Agent API Key.

    Accepts application/x-www-form-urlencoded and application/json.
    Form submissions 302 to the client; JSON requests return {"redirect": ...}.
    Error HTML is re-rendered with the same .ai / .cn copy as GET.
    """
    wants_json = _is_json_login_request(request)
    is_cn = is_cn_site(request)
    try:
        state, api_key = await _read_login_credentials(request)
    except Exception:
        return _login_error_response(
            request, wants_json, LoginSiteCopy.error(is_cn, "invalid_body")
        )

    if not state:
        return _login_error_response(
            request, wants_json, LoginSiteCopy.error(is_cn, "missing_state")
        )

    pending = store.get_pending_login(state)
    if not pending:
        return _login_error_response(
            request, wants_json, LoginSiteCopy.error(is_cn, "expired"), state=state
        )

    if not api_key:
        return _login_error_response(
            request,
            wants_json,
            LoginSiteCopy.error(is_cn, "missing_key"),
            state=state,
            pending=pending,
        )

    try:
        client = ClawdChatClient(settings.clawdchat_api_url, api_key)
        profile = await client.get_my_profile()
    except ClawdChatAPIError as e:
        logger.error(f"API Key login failed: {e}")
        return _login_error_response(
            request,
            wants_json,
            LoginSiteCopy.error(is_cn, "verify_failed", detail=e.detail),
            state=state,
            pending=pending,
        )
    except Exception as e:
        logger.exception("API Key login error")
        return _login_error_response(
            request,
            wants_json,
            LoginSiteCopy.error(is_cn, "verify_failed", detail=str(e)),
            state=state,
            pending=pending,
        )

    if not isinstance(profile, dict):
        return _login_error_response(
            request,
            wants_json,
            LoginSiteCopy.error(is_cn, "invalid_profile"),
            state=state,
            pending=pending,
        )

    agent_id = profile.get("id") or profile.get("agent_id")
    agent_name = profile.get("name") or profile.get("agent_name") or ""
    if not agent_id:
        return _login_error_response(
            request,
            wants_json,
            LoginSiteCopy.error(is_cn, "invalid_agent_id"),
            state=state,
            pending=pending,
        )

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
        agent_id=str(agent_id),
        agent_name=str(agent_name),
        user_jwt="",
        resource=pending.resource,
    ))
    store.consume_pending_login(state)

    redirect_uri = construct_redirect_uri(
        pending.redirect_uri, code=code, state=pending.oauth_state or None
    )
    logger.info(f"API Key authorization complete for agent '{agent_name}'")

    if wants_json:
        return JSONResponse({"redirect": redirect_uri})
    return RedirectResponse(url=redirect_uri, status_code=302)


def render_select_agent_html(
    *,
    state: str,
    agents: list,
    is_cn: bool,
    select_url: str = "/auth/select-agent",
    logo_src: str | None = None,
    account_mode: bool = False,
    can_create: bool = True,
    quota_note: str = "",
) -> str:
    """Render the shared Agent confirmation page (hosted HTTP and stdio).

    ``account_mode`` (hosted only): consent page for the account-first flow —
    authorize the client for all Agents, optionally bind one now or later.
    """
    template = jinja_env.get_template("select_agent.html")
    return template.render(
        state=state,
        agents=agents,
        is_cn=is_cn,
        select_url=select_url,
        logo_src=logo_src or "/logo.png",
        account_mode=account_mode,
        can_create=can_create,
        quota_note=quota_note,
    )


def _callback_error_html(is_cn: bool, key: str, detail: str = "", status: int = 400) -> HTMLResponse:
    title = SelectAgentCopy.error(is_cn, key)
    extra = f"<p>{detail}</p>" if detail else ""
    return HTMLResponse(f"<h1>{title}</h1>{extra}", status_code=status)


async def clawdchat_callback_handler(request: Request) -> Response:
    """GET /auth/clawdchat/callback - Handle callback from ClawdChat authentication.

    ClawdChat redirects here after successful user authentication with a one-time code.
    We exchange it for JWT and always show the consent page — a login alone never
    completes authorization. Accounts without Agents are fine: they create one later.
    """
    is_cn = is_cn_site(request)
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")

    if not code or not state:
        return _callback_error_html(is_cn, "missing_params")

    pending = store.get_pending_login(state)
    if not pending:
        return _callback_error_html(is_cn, "expired")

    # Exchange code for JWT via ClawdChat external token endpoint
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, "")
        result = await user_client.exchange_external_code(code)
        jwt_token = result.get("jwt", "")
    except ClawdChatAPIError as e:
        logger.error(f"ClawdChat auth callback failed: {e}")
        return _callback_error_html(is_cn, "auth_failed", detail=e.detail)
    except Exception as e:
        logger.exception("ClawdChat auth callback error")
        return _callback_error_html(is_cn, "auth_failed", detail=str(e), status=500)

    if not jwt_token:
        return _callback_error_html(is_cn, "no_token")

    # Update pending login with JWT. Do not issue an auth code or Agent Key yet.
    pending.user_jwt = jwt_token
    pending.user_info = result.get("user")

    # Check the account is usable (do not auto-create a default Agent: the user names it)
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, jwt_token)
        await user_client.get_my_agents(auto_create=False)
    except ClawdChatAPIError as e:
        return _callback_error_html(is_cn, "agents_failed", detail=e.detail)

    # 0, 1 or many Agents: always show the consent page. Never auto-complete.
    return RedirectResponse(
        url=f"{settings.mcp_server_url}/auth/select-agent?state={state}",
        status_code=302,
    )


async def select_agent_page_handler(request: Request) -> HTMLResponse:
    """GET /auth/select-agent - Show agent selection page."""
    is_cn = is_cn_site(request)
    state = request.query_params.get("state", "")
    pending = store.get_pending_login(state)
    if not pending or not pending.user_jwt:
        return _callback_error_html(is_cn, "expired")

    # Fetch agents
    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, pending.user_jwt)
        agents_result = await user_client.get_my_agents(auto_create=False)
        agents = [
            a for a in agents_result.get("agents", [])
            if a.get("is_active", True) and a.get("is_claimed", True)
        ]
    except Exception:
        agents_result, agents = {}, []

    can_create, quota_note = _quota_view(agents_result, is_cn)
    html = render_select_agent_html(
        state=state, agents=agents, is_cn=is_cn, account_mode=True,
        can_create=can_create, quota_note=quota_note,
    )
    return HTMLResponse(html)


def _quota_view(agents_result: dict, is_cn: bool) -> tuple[bool, str]:
    """Hide "create" when the owner's limit is full (backend reports claimed_count / max_agents)."""
    used = agents_result.get("claimed_count") if isinstance(agents_result, dict) else None
    limit = agents_result.get("max_agents") if isinstance(agents_result, dict) else None
    if isinstance(used, int) and isinstance(limit, int) and used >= limit:
        note = (
            f"名额已满（{used}/{limit}），只能选已有的 Agent。"
            if is_cn
            else f"Your limit is full ({used}/{limit}); pick an existing Agent."
        )
        return False, note
    return True, ""


async def select_agent_callback_handler(request: Request) -> JSONResponse:
    """POST /auth/select-agent - Handle explicit Agent confirmation."""
    is_cn = is_cn_site(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": SelectAgentCopy.error(is_cn, "invalid_body")}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": SelectAgentCopy.error(is_cn, "invalid_body")}, status_code=400)

    state = body.get("state", "")
    agent_id = body.get("agent_id", "")
    agent_name = body.get("agent_name", "")
    # 主人在授权页上选「新建」：会话先不绑号，允许 AI 新建一次（2026-11-10 主人拍板，取代「仅授权」）
    create_new = body.get("create_new") is True
    try:
        confirm_reset = confirm_reset_from_mapping(body)
    except ConfirmResetTypeError:
        return JSONResponse(
            {"error": SelectAgentCopy.error(is_cn, "confirm_type")},
            status_code=422,
        )

    if not state or (not agent_id and not create_new):
        return JSONResponse({"error": SelectAgentCopy.error(is_cn, "missing_agent")}, status_code=400)

    pending = store.get_pending_login(state)
    if not pending or not pending.user_jwt:
        return JSONResponse({"error": SelectAgentCopy.error(is_cn, "session_expired")}, status_code=400)

    if not agent_id:
        try:
            user_client = ClawdChatUserClient(settings.clawdchat_api_url, pending.user_jwt)
            can_create, note = _quota_view(await user_client.get_my_agents(auto_create=False), is_cn)
        except Exception:
            can_create, note = True, ""
        if not can_create:
            return JSONResponse({"error": note}, status_code=409)
        return _issue_auth_code(
            state, pending, api_key="", agent_id="", agent_name="", create_grants=1
        )

    return await _complete_authorization(
        state, pending, agent_id, agent_name,
        confirm_reset=confirm_reset,
        is_cn=is_cn,
    )


async def _complete_authorization(
    state: str, pending: PendingLogin, agent_id: str, agent_name: str,
    confirm_reset: bool = False,
    is_cn: bool = False,
) -> JSONResponse:
    """Complete the OAuth authorization after explicit Agent confirmation.

    Login JWT is not sufficient: the caller must POST a chosen agent_id.
    confirm_reset must be boolean True to rotate a missing stored key.
    """
    if not confirm_reset_is_true(confirm_reset):
        confirm_reset = False

    try:
        user_client = ClawdChatUserClient(settings.clawdchat_api_url, pending.user_jwt)
        agents_result = await user_client.get_my_agents(auto_create=False)
        agents = agents_result.get("agents", [])
        owned = find_owned_agent(agents, agent_id)
        if not owned:
            return JSONResponse(
                {"error": SelectAgentCopy.error(is_cn, "not_owned")},
                status_code=403,
            )
        agent_id = str(owned.get("id") or agent_id)
        agent_name = str(owned.get("name") or agent_name)

        cred_result = await user_client.get_agent_credentials(agent_id)
        api_key = cred_result.get("api_key")

        if not api_key:
            if not confirm_reset:
                logger.info(f"Agent '{agent_name}' has no stored key, asking user to confirm reset")
                return JSONResponse({
                    "needs_reset": True,
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "message": SelectAgentCopy.reset_message(is_cn, agent_name),
                })

            logger.info(f"User confirmed reset for agent '{agent_name}', resetting key")
            reset_result = await user_client.reset_agent_key(agent_id)
            api_key = reset_result.get("api_key")
            if not api_key:
                return JSONResponse(
                    {"error": SelectAgentCopy.error(is_cn, "reset_failed")},
                    status_code=400,
                )
    except ClawdChatAPIError as e:
        return JSONResponse(
            {"error": SelectAgentCopy.error(is_cn, "cred_failed", detail=e.detail)},
            status_code=400,
        )

    return _issue_auth_code(state, pending, api_key=api_key, agent_id=agent_id, agent_name=agent_name)


def _issue_auth_code(
    state: str,
    pending: PendingLogin,
    *,
    api_key: str,
    agent_id: str,
    agent_name: str,
    create_grants: int = 0,
) -> JSONResponse:
    """Issue the OAuth code for a consented account session (Agent optional)."""
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
        create_grants=create_grants,
    ))

    # Consume pending login
    store.consume_pending_login(state)

    # Redirect back to client with authorization code and original OAuth state
    redirect_uri = construct_redirect_uri(
        pending.redirect_uri, code=code, state=pending.oauth_state or None
    )
    logger.info(
        f"Authorization complete for agent '{agent_name or '(none bound)'}', redirecting to client"
    )

    return JSONResponse({"redirect": redirect_uri})


# ---------------------------------------------------------------- confirm creating an Agent
# 对话里 AI 想再新建 Agent、但主人没在授权页上选「新建」时：AI 只能拿到一个确认链接。
# 主人打开链接 → 用虾聊账号登录（必须和这条 MCP 连接是同一个账号）→ 点「同意新建」，
# 这条连接才多一次新建额度。AI 自己完成不了登录，所以绕不过主人。

_CONFIRM_TEXT = {
    True: {
        "title": "确认让 AI 为你新建一个虾聊 Agent？",
        "intro": "你在用的 AI 想为你新建一个 Agent。先登录虾聊确认是你本人。",
        "login": "登录并继续",
        "ask": "允许这个 AI 为你新建一个 Agent？",
        "name": "它想用的名字：",
        "approve": "同意新建",
        "deny": "不用了",
        "done": "已同意。回到对话里，AI 会继续新建。",
        "denied": "已拒绝，不会新建。",
        "expired": "确认链接无效或已过期，请让 AI 重新发一个。",
        "mismatch": "登录的账号和这个 AI 连接的虾聊账号不是同一个，无法确认。",
        "failed": "登录失败，请重试。",
    },
    False: {
        "title": "Let the AI create a ClawdChat Agent for you?",
        "intro": "The AI you're using wants to create a new Agent. Sign in to ClawdChat first to confirm it's you.",
        "login": "Sign in and continue",
        "ask": "Allow this AI to create a new Agent for you?",
        "name": "Name it wants to use: ",
        "approve": "Allow",
        "deny": "No thanks",
        "done": "Approved. Go back to the conversation; the AI will continue.",
        "denied": "Declined. Nothing will be created.",
        "expired": "This link is invalid or expired. Ask the AI for a new one.",
        "mismatch": "That account is not the ClawdChat account this AI is connected with.",
        "failed": "Sign-in failed, please retry.",
    },
}


def _confirm_page(is_cn: bool, inner: str) -> HTMLResponse:
    t = _CONFIRM_TEXT[is_cn]
    return HTMLResponse(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(t['title'])}</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f0f2f5;display:flex;justify-content:center;padding-top:72px}"
        ".c{background:#fff;max-width:440px;width:92vw;padding:32px;border-radius:12px;"
        "border:1px solid #e5e7eb}h1{font-size:20px;color:#111827}p{color:#4b5563;font-size:14px;"
        "line-height:1.6}.b{display:block;width:100%;padding:12px;border:none;border-radius:8px;"
        "font-size:14px;font-weight:600;margin-top:12px;cursor:pointer;text-align:center;"
        "text-decoration:none}.p{background:#6366f1;color:#fff}.g{background:#fff;color:#6b7280;"
        "border:1px solid #e5e7eb}</style></head><body><div class=\"c\">"
        + inner
        + "</div></body></html>"
    )


def _confirm_message(is_cn: bool, key: str, status: int = 200) -> HTMLResponse:
    t = _CONFIRM_TEXT[is_cn]
    resp = _confirm_page(is_cn, f"<h1>{html.escape(t['title'])}</h1><p>{html.escape(t[key])}</p>")
    resp.status_code = status
    return resp


def new_create_ticket(access_token: str, name: str = "") -> str:
    ticket = secrets.token_urlsafe(24)
    store.store_create_ticket(ticket, {"access_token": access_token, "name": name or ""})
    return ticket


async def confirm_create_page_handler(request: Request) -> Response:
    """GET /auth/confirm-create?t=TICKET — explain, then send the owner to sign in."""
    is_cn = is_cn_site(request)
    ticket = request.query_params.get("t", "")
    data = store.get_create_ticket(ticket)
    if not data:
        return _confirm_message(is_cn, "expired", 400)
    t = _CONFIRM_TEXT[is_cn]
    params = urlencode(
        {"callback_url": f"{settings.mcp_server_url}/auth/confirm-create/callback", "state": ticket}
    )
    login_url = f"{settings.clawdchat_api_url}/api/v1/auth/external/authorize?{params}"
    return _confirm_page(
        is_cn,
        f"<h1>{html.escape(t['title'])}</h1><p>{html.escape(t['intro'])}</p>"
        f'<a class="b p" href="{html.escape(login_url)}">{html.escape(t["login"])}</a>',
    )


async def confirm_create_callback_handler(request: Request) -> Response:
    """GET /auth/confirm-create/callback — must be the same ClawdChat account as the session."""
    from .user_jwt import jwt_subject

    is_cn = is_cn_site(request)
    ticket = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    data = store.get_create_ticket(ticket)
    if not data or not code:
        return _confirm_message(is_cn, "expired", 400)
    session = store.get_access_token(data["access_token"])
    if not session or not session.user_jwt:
        return _confirm_message(is_cn, "expired", 400)
    try:
        result = await ClawdChatUserClient(settings.clawdchat_api_url, "").exchange_external_code(code)
    except Exception:
        return _confirm_message(is_cn, "failed", 400)
    signed_in = jwt_subject(result.get("jwt") or "")
    if not signed_in or signed_in != jwt_subject(session.user_jwt):
        return _confirm_message(is_cn, "mismatch", 403)

    data["verified"] = True
    data["nonce"] = secrets.token_urlsafe(24)
    t = _CONFIRM_TEXT[is_cn]
    name_html = (
        f"<p>{html.escape(t['name'])}<strong>{html.escape(data['name'])}</strong></p>"
        if data.get("name")
        else ""
    )
    payload = html.escape(json.dumps({"ticket": ticket, "nonce": data["nonce"]}))
    script = (
        "<script>(function(){var p=document.getElementById('p');"
        "async function go(ok){var b=JSON.parse(p.dataset.body);b.approve=ok;"
        "var r=await fetch('/auth/confirm-create',{method:'POST',"
        "headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});"
        "var d=await r.json().catch(function(){return {}});"
        "document.getElementById('m').textContent=d.message||'';"
        "document.getElementById('a').style.display='none';}"
        "document.getElementById('y').onclick=function(){go(true)};"
        "document.getElementById('n').onclick=function(){go(false)};})();</script>"
    )
    return _confirm_page(
        is_cn,
        f"<h1>{html.escape(t['ask'])}</h1>{name_html}"
        f'<div id="a"><button class="b p" id="y">{html.escape(t["approve"])}</button>'
        f'<button class="b g" id="n">{html.escape(t["deny"])}</button></div>'
        f'<p id="m"></p><div id="p" data-body="{payload}"></div>{script}',
    )


async def confirm_create_submit_handler(request: Request) -> JSONResponse:
    """POST /auth/confirm-create — owner's decision; needs the nonce shown after sign-in."""
    is_cn = is_cn_site(request)
    t = _CONFIRM_TEXT[is_cn]
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"message": t["expired"]}, status_code=400)
    ticket = str(body.get("ticket") or "")
    nonce = str(body.get("nonce") or "")
    data = store.get_create_ticket(ticket)
    if (
        not data
        or not data.get("verified")
        or not nonce
        or not secrets.compare_digest(str(data.get("nonce") or ""), nonce)
    ):
        return JSONResponse({"message": t["expired"]}, status_code=403)
    store.consume_create_ticket(ticket)
    if body.get("approve") is not True:
        return JSONResponse({"success": False, "message": t["denied"]})
    if not store.change_create_grants(data["access_token"], +1):
        return JSONResponse({"message": t["expired"]}, status_code=400)
    logger.info("Owner approved creating an agent for an MCP session")
    return JSONResponse({"success": True, "message": t["done"]})
