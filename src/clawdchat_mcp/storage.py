"""Storage for OAuth tokens, authorization codes, and clients.

Client registrations, access tokens, and refresh tokens are persisted to JSON
files so they survive server restarts. Auth codes and pending logins are
short-lived and kept in-memory only.
"""

import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Persistence files (sibling to project root)
_DATA_DIR = Path(__file__).parent.parent.parent
CLIENTS_FILE = _DATA_DIR / ".oauth_clients.json"
TOKENS_FILE = _DATA_DIR / ".oauth_tokens.json"


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
        """Update the agent associated with an access token (for agent switching)."""
        data = self.access_tokens.get(token)
        if not data:
            return False
        data.agent_api_key = agent_api_key
        data.agent_id = agent_id
        data.agent_name = agent_name
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
                "access_tokens": {k: asdict(v) for k, v in self.access_tokens.items()},
                "refresh_tokens": {k: asdict(v) for k, v in self.refresh_tokens.items()},
            }
            TOKENS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
                token_data = AccessTokenData(**v)
                if token_data.expires_at and token_data.expires_at < now:
                    continue  # skip expired
                self.access_tokens[k] = token_data
                loaded_at += 1

            for k, v in data.get("refresh_tokens", {}).items():
                token_data = RefreshTokenData(**v)
                if token_data.expires_at and token_data.expires_at < now:
                    continue  # skip expired
                self.refresh_tokens[k] = token_data
                loaded_rt += 1

            logger.info(
                f"Loaded {loaded_at} access token(s) and {loaded_rt} refresh token(s) from disk"
            )
        except Exception as e:
            logger.warning(f"Failed to load tokens: {e}")

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
