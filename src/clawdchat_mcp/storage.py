"""Storage for OAuth tokens, authorization codes, and clients.

Client registrations, access tokens, and refresh tokens are persisted to JSON
files so they survive server restarts. Auth codes and pending logins are
short-lived and kept in-memory only.

Secrets at rest: ``agent_api_key`` and ``user_jwt`` are Fernet-encrypted in the
tokens file (``enc:v1:`` prefix). The key comes from ``MCP_TOKEN_ENCRYPTION_KEY``;
without it a random key is generated once into ``.oauth_token_key`` (0600).
Legacy plaintext values still load and are re-encrypted on the next save.
"""

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Persistence files (sibling to project root)
_DATA_DIR = Path(__file__).parent.parent.parent
CLIENTS_FILE = _DATA_DIR / ".oauth_clients.json"
TOKENS_FILE = _DATA_DIR / ".oauth_tokens.json"
TOKEN_KEY_FILE = _DATA_DIR / ".oauth_token_key"

_ENC_PREFIX = "enc:v1:"
_SECRET_FIELDS = ("agent_api_key", "user_jwt")


def _load_fernet() -> Fernet:
    from .config import settings

    key = (settings.mcp_token_encryption_key or "").strip()
    if not key:
        if TOKEN_KEY_FILE.exists():
            key = TOKEN_KEY_FILE.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode()
            TOKEN_KEY_FILE.write_text(key, encoding="utf-8")
            try:
                os.chmod(TOKEN_KEY_FILE, 0o600)
            except OSError:
                pass
            logger.warning(
                "MCP_TOKEN_ENCRYPTION_KEY not set; generated %s for token encryption",
                TOKEN_KEY_FILE.name,
            )
    return Fernet(key.encode())


def _encrypt_secrets(record: dict, fernet: Fernet) -> dict:
    out = dict(record)
    for name in _SECRET_FIELDS:
        value = out.get(name)
        if value and not str(value).startswith(_ENC_PREFIX):
            out[name] = _ENC_PREFIX + fernet.encrypt(str(value).encode()).decode()
    return out


def _decrypt_secrets(record: dict, fernet: Fernet) -> dict:
    out = dict(record)
    for name in _SECRET_FIELDS:
        value = out.get(name)
        if isinstance(value, str) and value.startswith(_ENC_PREFIX):
            try:
                out[name] = fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
            except InvalidToken:
                logger.warning("Could not decrypt stored %s (key changed?); dropping it", name)
                out[name] = ""
    return out


@dataclass
class AuthCodeData:
    """Data associated with an authorization code."""
    code: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: float
    # ClawdChat-specific
    agent_api_key: str
    agent_id: str
    agent_name: str
    user_jwt: str
    resource: Optional[str] = None
    # 主人在授权页上选了「新建」：允许 switch_agent(create) 的次数（用一次扣一次）
    create_grants: int = 0


@dataclass
class AccessTokenData:
    """Data associated with an access token."""
    token: str
    client_id: str
    scopes: list[str]
    expires_at: Optional[int]
    # ClawdChat-specific
    agent_api_key: str
    agent_id: str
    agent_name: str
    user_jwt: str
    resource: Optional[str] = None
    # Refresh token issued together with this access token (keeps bindings in sync)
    refresh_token: Optional[str] = None
    create_grants: int = 0


@dataclass
class RefreshTokenData:
    """Data associated with a refresh token."""
    token: str
    client_id: str
    scopes: list[str]
    expires_at: Optional[int]
    # ClawdChat-specific
    agent_api_key: str
    agent_id: str
    agent_name: str
    user_jwt: str
    create_grants: int = 0


@dataclass
class OAuthClientData:
    """Registered OAuth client."""
    client_id: str
    client_secret: Optional[str]
    redirect_uris: list[str]
    client_name: Optional[str] = None
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = field(default_factory=lambda: ["code"])
    token_endpoint_auth_method: str = "none"
    scope: Optional[str] = None  # Space-separated scopes, e.g. "agent"
    client_id_issued_at: Optional[int] = None
    client_secret_expires_at: Optional[int] = None


@dataclass
class PendingLogin:
    """Pending login state during OAuth authorize flow."""
    state: str  # Internal state for login flow
    oauth_state: str  # Original OAuth state from MCP client (must be returned in redirect)
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    resource: Optional[str] = None
    # After login:
    user_jwt: Optional[str] = None
    user_info: Optional[dict] = None
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


CREATE_TICKET_TTL = 15 * 60


class TokenStore:
    """Storage for all OAuth-related data.

    Persisted to disk (survive restarts): clients, access tokens, refresh tokens.
    In-memory only (short-lived): auth codes, pending logins.
    """

    def __init__(self):
        self.auth_codes: dict[str, AuthCodeData] = {}
        self.access_tokens: dict[str, AccessTokenData] = {}
        self.refresh_tokens: dict[str, RefreshTokenData] = {}
        self.clients: dict[str, OAuthClientData] = {}
        self.pending_logins: dict[str, PendingLogin] = {}  # keyed by state
        self.create_tickets: dict[str, dict] = {}  # keyed by ticket
        self._fernet = _load_fernet()
        self._load_clients()
        self._load_tokens()

    # ---- Auth Codes (in-memory, short-lived) ----

    def store_auth_code(self, data: AuthCodeData) -> None:
        self.auth_codes[data.code] = data

    def get_auth_code(self, code: str) -> Optional[AuthCodeData]:
        data = self.auth_codes.get(code)
        if data and data.expires_at < time.time():
            del self.auth_codes[code]
            return None
        return data

    def consume_auth_code(self, code: str) -> Optional[AuthCodeData]:
        return self.auth_codes.pop(code, None)

    # ---- Access Tokens (persisted) ----

    def store_access_token(self, data: AccessTokenData) -> None:
        self.access_tokens[data.token] = data
        self._save_tokens()

    def get_access_token(self, token: str) -> Optional[AccessTokenData]:
        data = self.access_tokens.get(token)
        if data and data.expires_at and data.expires_at < time.time():
            del self.access_tokens[token]
            self._save_tokens()
            return None
        return data

    def revoke_access_token(self, token: str) -> None:
        if self.access_tokens.pop(token, None) is not None:
            self._save_tokens()

    def update_access_token_agent(
        self, token: str, agent_api_key: str, agent_id: str, agent_name: str
    ) -> bool:
        """Bind an access token (and its refresh token) to an agent.

        The refresh token is updated too, so a later token refresh keeps the
        agent the user switched to instead of reverting to the original one.
        """
        data = self.access_tokens.get(token)
        if not data:
            return False
        data.agent_api_key = agent_api_key
        data.agent_id = agent_id
        data.agent_name = agent_name
        rt = self.refresh_tokens.get(data.refresh_token or "")
        if rt is not None:
            rt.agent_api_key = agent_api_key
            rt.agent_id = agent_id
            rt.agent_name = agent_name
        self._save_tokens()
        return True

    def update_user_jwt(self, token: str, user_jwt: str) -> bool:
        """Store a renewed ClawdChat user JWT on an access token and its refresh token."""
        data = self.access_tokens.get(token)
        if not data:
            return False
        data.user_jwt = user_jwt
        rt = self.refresh_tokens.get(data.refresh_token or "")
        if rt is not None:
            rt.user_jwt = user_jwt
        self._save_tokens()
        return True

    # ---- Refresh Tokens (persisted) ----

    def store_refresh_token(self, data: RefreshTokenData) -> None:
        self.refresh_tokens[data.token] = data
        self._save_tokens()

    def get_refresh_token(self, token: str) -> Optional[RefreshTokenData]:
        data = self.refresh_tokens.get(token)
        if data and data.expires_at and data.expires_at < time.time():
            del self.refresh_tokens[token]
            self._save_tokens()
            return None
        return data

    def revoke_refresh_token(self, token: str) -> None:
        if self.refresh_tokens.pop(token, None) is not None:
            self._save_tokens()

    # ---- Clients (persisted) ----

    def store_client(self, data: OAuthClientData) -> None:
        self.clients[data.client_id] = data
        self._save_clients()

    def get_client(self, client_id: str) -> Optional[OAuthClientData]:
        return self.clients.get(client_id)

    def _save_clients(self) -> None:
        """Persist client registrations to JSON file."""
        try:
            data = {cid: asdict(c) for cid, c in self.clients.items()}
            CLIENTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save clients: {e}")

    def _load_clients(self) -> None:
        """Load persisted client registrations from JSON file."""
        if not CLIENTS_FILE.exists():
            return
        try:
            data = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
            for cid, cdata in data.items():
                self.clients[cid] = OAuthClientData(**cdata)
            logger.info(f"Loaded {len(self.clients)} persisted OAuth client(s)")
        except Exception as e:
            logger.warning(f"Failed to load clients: {e}")

    # ---- Tokens persistence ----

    def _save_tokens(self) -> None:
        """Persist access tokens and refresh tokens to JSON file."""
        try:
            data = {
                "access_tokens": {
                    k: _encrypt_secrets(asdict(v), self._fernet)
                    for k, v in self.access_tokens.items()
                },
                "refresh_tokens": {
                    k: _encrypt_secrets(asdict(v), self._fernet)
                    for k, v in self.refresh_tokens.items()
                },
            }
            TOKENS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                os.chmod(TOKENS_FILE, 0o600)
            except OSError:
                pass
        except Exception as e:
            logger.warning(f"Failed to save tokens: {e}")

    def _load_tokens(self) -> None:
        """Load persisted tokens from JSON file, discarding expired ones."""
        if not TOKENS_FILE.exists():
            return
        try:
            data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            now = time.time()
            loaded_at = loaded_rt = 0

            for k, v in data.get("access_tokens", {}).items():
                token_data = AccessTokenData(**_decrypt_secrets(v, self._fernet))
                if token_data.expires_at and token_data.expires_at < now:
                    continue  # skip expired
                self.access_tokens[k] = token_data
                loaded_at += 1

            for k, v in data.get("refresh_tokens", {}).items():
                token_data = RefreshTokenData(**_decrypt_secrets(v, self._fernet))
                if token_data.expires_at and token_data.expires_at < now:
                    continue  # skip expired
                self.refresh_tokens[k] = token_data
                loaded_rt += 1

            logger.info(
                f"Loaded {loaded_at} access token(s) and {loaded_rt} refresh token(s) from disk"
            )
        except Exception as e:
            logger.warning(f"Failed to load tokens: {e}")

    # ---- Create grants (owner approved creating an Agent) ----

    def change_create_grants(self, token: str, delta: int) -> bool:
        """Add / use a create grant on an access token and its refresh token."""
        data = self.access_tokens.get(token)
        if not data:
            return False
        data.create_grants = max(0, (data.create_grants or 0) + delta)
        rt = self.refresh_tokens.get(data.refresh_token or "")
        if rt is not None:
            rt.create_grants = data.create_grants
        self._save_tokens()
        return True

    # ---- Create confirmations (in-memory, short-lived) ----

    def store_create_ticket(self, ticket: str, data: dict) -> None:
        data.setdefault("created_at", time.time())
        self.create_tickets[ticket] = data

    def get_create_ticket(self, ticket: str) -> Optional[dict]:
        data = self.create_tickets.get(ticket)
        if data and (time.time() - data["created_at"]) > CREATE_TICKET_TTL:
            del self.create_tickets[ticket]
            return None
        return data

    def consume_create_ticket(self, ticket: str) -> Optional[dict]:
        """Mark a ticket used so a later GET can say "already used" instead of "expired"."""
        data = self.get_create_ticket(ticket)
        if not data or data.get("used"):
            return None
        data["used"] = True
        data["used_at"] = time.time()
        return dict(data)

    # ---- Pending Logins (in-memory, short-lived) ----

    def store_pending_login(self, data: PendingLogin) -> None:
        self.pending_logins[data.state] = data

    def get_pending_login(self, state: str) -> Optional[PendingLogin]:
        data = self.pending_logins.get(state)
        # Expire after 10 minutes
        if data and (time.time() - data.created_at) > 600:
            del self.pending_logins[state]
            return None
        return data

    def consume_pending_login(self, state: str) -> Optional[PendingLogin]:
        return self.pending_logins.pop(state, None)

    # ---- Utilities ----

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_code() -> str:
        return secrets.token_urlsafe(32)

    def cleanup_expired(self) -> None:
        """Remove expired entries and persist changes."""
        now = time.time()
        self.auth_codes = {
            k: v for k, v in self.auth_codes.items() if v.expires_at > now
        }
        old_at_count = len(self.access_tokens)
        old_rt_count = len(self.refresh_tokens)
        self.access_tokens = {
            k: v for k, v in self.access_tokens.items()
            if not v.expires_at or v.expires_at > now
        }
        self.refresh_tokens = {
            k: v for k, v in self.refresh_tokens.items()
            if not v.expires_at or v.expires_at > now
        }
        self.pending_logins = {
            k: v for k, v in self.pending_logins.items()
            if (now - v.created_at) < 600
        }
        # Persist if any tokens were cleaned up
        if len(self.access_tokens) != old_at_count or len(self.refresh_tokens) != old_rt_count:
            self._save_tokens()


# Global store instance
store = TokenStore()
