"""OAuth authentication for stdio mode.

Uses a temporary local HTTP server to receive OAuth callbacks,
allowing stdio-mode MCP clients to authenticate via browser.

Flow:
1. authenticate tool → start temp HTTP server → return auth URL
2. User opens URL in browser → completes login on ClawdChat
3. ClawdChat redirects to temp server → callback exchanges code for JWT
4. 0 agents: error page (no default Agent is created)
5. 1 or many agents: confirmation page — login JWT is not Agent authorization
6. Missing stored key requires confirm_reset=true (boolean) before reset-key
7. Subsequent tool calls use the bound agent API key in this process
"""

import json
import logging
import secrets
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .auth_confirm import (
    AGENT_NOT_OWNED,
    ConfirmResetTypeError,
    confirm_reset_from_mapping,
    find_owned_agent,
    parse_confirm_reset,
)
from .config import settings
from .discovery import ICON_URL

logger = logging.getLogger(__name__)


def _stdio_is_cn() -> bool:
    return "clawdchat.cn" in (settings.clawdchat_api_url or "").lower()


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_result_page(title: str, message: str, error: bool = False) -> str:
    """Build a simple result page (success/error)."""
    color = "#dc2626" if error else "#22c55e"
    icon = "&#10060;" if error else "&#9989;"
    return (
        f"<html><head><meta charset='utf-8'><title>ClawdChat MCP</title></head>"
        f"<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        f"text-align:center;padding-top:80px;background:#f0f2f5;'>"
        f"<div style='background:white;max-width:480px;margin:0 auto;padding:48px 32px;"
        f"border-radius:12px;border:1px solid #e5e7eb;'>"
        f"<h1 style='color:{color};font-size:22px;'>{icon} {title}</h1>"
        f"<p style='color:#6b7280;margin-top:16px;font-size:14px;line-height:1.6;'>"
        f"{message}</p></div></body></html>"
    )


def _render_selection_page(agents: list) -> str:
    from .auth_provider import render_select_agent_html

    return render_select_agent_html(
        state="",
        agents=agents,
        is_cn=_stdio_is_cn(),
        select_url="/select",
        logo_src=ICON_URL,
    )


class StdioAuthManager:
    """Manages OAuth authentication for stdio mode via browser.

    Login always stops at the Agent confirmation page. A stored key is bound
    only after an explicit select POST; missing keys rotate only when
    confirm_reset is the boolean True.
    """

    def __init__(self):
        self.jwt: str = ""
        self.api_key: str = ""
        self.agent_id: str = ""
        self.agent_name: str = ""
        self.agents: list = []
        self._server: Optional[HTTPServer] = None
        self._port: int = 0
        self._auth_complete = threading.Event()
        self._error: str = ""

    @property
    def is_authenticated(self) -> bool:
        """Has a valid agent API key bound for this process."""
        return bool(self.api_key)

    @property
    def needs_agent_selection(self) -> bool:
        """JWT obtained but this round has not confirmed an Agent yet."""
        return bool(self.jwt) and not self.api_key and len(self.agents) >= 1

    def get_auth_url(self) -> str:
        """Start a temp HTTP server and return the auth URL for user to open.

        The temp server handles the full auth flow:
        - /callback: receives OAuth redirect, exchanges code for JWT
        - /select: shows agent confirmation page and processes selection
        """
        self._shutdown_server()
        self._auth_complete.clear()
        self._error = ""

        self._port = _find_free_port()
        callback_url = f"http://127.0.0.1:{self._port}/callback"

        state = secrets.token_urlsafe(16)
        auth_url = (
            f"{settings.clawdchat_api_url}/api/v1/auth/external/authorize?"
            + urlencode({"callback_url": callback_url, "state": state})
        )

        manager = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)

                if parsed.path == "/callback":
                    self._handle_callback(parsed)
                elif parsed.path == "/select":
                    self._handle_select_page()
                else:
                    self.send_error(404)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path == "/select":
                    self._handle_select_submit()
                else:
                    self.send_error(404)

            def _handle_callback(self, parsed):
                """Exchange code for JWT, then always show the confirmation page."""
                params = parse_qs(parsed.query)
                code = params.get("code", [""])[0]

                if not code:
                    self._send_html(_build_result_page("Sign-in failed", "Missing code parameter", error=True))
                    manager._error = "Callback is missing the code parameter"
                    manager._auth_complete.set()
                    return

                try:
                    manager._do_auth_exchange(code)
                except Exception as e:
                    logger.exception("Stdio auth exchange failed")
                    self._send_html(_build_result_page("Sign-in failed", str(e), error=True))
                    manager._error = str(e)
                    manager._auth_complete.set()
                    return

                if not manager.agents:
                    self._send_html(_build_result_page(
                        "No Agent",
                        "No claimed agents found. Claim an agent on ClawdChat first.",
                        error=True,
                    ))
                    manager._error = "No claimed agents found. Claim an agent on ClawdChat first."
                    manager._auth_complete.set()
                    return

                # 1 or many: confirmation page. JWT is not Agent authorization.
                self._send_html(_render_selection_page(manager.agents))

            def _handle_select_page(self):
                """GET /select - Show agent confirmation page."""
                if not manager.agents:
                    self._send_html(_build_result_page("Error", "Agent list is empty", error=True))
                    return
                self._send_html(_render_selection_page(manager.agents))

            def _handle_select_submit(self):
                """POST /select - Process explicit Agent confirmation."""
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)

                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "Invalid request body"}, 400)
                    return

                if not isinstance(data, dict):
                    self._send_json({"error": "Invalid request body"}, 400)
                    return

                try:
                    confirm_reset = confirm_reset_from_mapping(data)
                except ConfirmResetTypeError as e:
                    self._send_json({"error": str(e)}, 422)
                    return

                agent_id = data.get("agent_id", "")
                if not agent_id:
                    self._send_json({"error": "Missing agent_id"}, 400)
                    return

                result = manager.select_agent(agent_id, confirm_reset=confirm_reset)
                if result.get("status") == "authenticated":
                    self._send_json({
                        "success": True,
                        "status": "authenticated",
                        "agent_name": result.get("agent_name", ""),
                    })
                    manager._auth_complete.set()
                elif result.get("needs_reset"):
                    self._send_json(result)
                elif "not in the owned" in str(result.get("error", "")).lower() or result.get("error") == AGENT_NOT_OWNED:
                    self._send_json(result, 403)
                else:
                    self._send_json({"error": result.get("error", "Selection failed")}, 400)

            def _send_html(self, content: str):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))

            def _send_json(self, data: dict, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

            def log_message(self, format, *args):
                pass  # Suppress default HTTP server logs

        self._server = HTTPServer(("127.0.0.1", self._port), CallbackHandler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

        logger.info(f"Stdio auth: temp callback server on port {self._port}")
        return auth_url

    def _do_auth_exchange(self, code: str):
        """Exchange auth code for JWT and fetch agents. Never auto-select."""
        api_url = settings.clawdchat_api_url

        r = httpx.post(
            f"{api_url}/api/v1/auth/external/token",
            json={"code": code},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Failed to obtain token: {r.text}")

        data = r.json()
        self.jwt = data.get("jwt", "")
        if not self.jwt:
            raise RuntimeError("Server did not return a JWT")

        r = httpx.get(
            f"{api_url}/api/v1/users/me/agents",
            cookies={"clawdchat_token": self.jwt},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Failed to fetch agent list: {r.text}")

        self.agents = r.json().get("agents", [])
        if not self.agents:
            raise RuntimeError("No claimed agents found. Claim an agent on ClawdChat first.")

        logger.info(f"Stdio auth: login ok, {len(self.agents)} agent(s); waiting for confirmation")

    def _get_stored_key(self, agent_id: str) -> Optional[str]:
        api_url = settings.clawdchat_api_url
        r = httpx.get(
            f"{api_url}/api/v1/users/me/agents/{agent_id}/credentials",
            cookies={"clawdchat_token": self.jwt},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Failed to fetch credentials: {r.text}")
        return r.json().get("api_key") or None

    def _reset_key(self, agent_id: str) -> Optional[str]:
        api_url = settings.clawdchat_api_url
        r = httpx.post(
            f"{api_url}/api/v1/users/me/agents/{agent_id}/reset-key",
            cookies={"clawdchat_token": self.jwt},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Failed to reset API key: {r.text}")
        return r.json().get("api_key") or None

    def _bind_agent(self, agent_id: str, agent_name: str, api_key: str) -> None:
        self.api_key = api_key
        self.agent_id = agent_id
        self.agent_name = agent_name
        logger.info(f"Stdio auth: selected agent '{agent_name}' ({agent_id})")

    def select_agent(self, agent_id: str, confirm_reset: Any = False) -> dict:
        """Bind an owned agent after explicit confirmation.

        Does not rotate or deliver a key unless the target has a stored key,
        or confirm_reset is the boolean True. Cancel / omit / false / string
        truthy leave the current session binding unchanged.
        """
        try:
            confirmed = (
                False
                if confirm_reset is False
                else parse_confirm_reset(confirm_reset, present=True)
            )
        except ConfirmResetTypeError as e:
            return {"error": str(e)}

        agent = find_owned_agent(self.agents, agent_id)
        if not agent:
            return {"error": f"Agent ID '{agent_id}' is not in the owned agent list"}

        owned_id = str(agent.get("id") or agent_id)
        owned_name = str(agent.get("name") or "")

        try:
            api_key = self._get_stored_key(owned_id)
            if api_key:
                self._bind_agent(owned_id, owned_name, api_key)
                return {
                    "status": "authenticated",
                    "message": f"Selected agent '{self.agent_name}'",
                    "agent_id": self.agent_id,
                    "agent_name": self.agent_name,
                }

            if not confirmed:
                return {
                    "needs_reset": True,
                    "agent_id": owned_id,
                    "agent_name": owned_name,
                    "message": (
                        f"Agent '{owned_name}' has no stored API key. "
                        "Set confirm_reset=true to generate and bind a new key. "
                        "Any previous key will stop working."
                    ),
                    "warning": (
                        f"Agent '{owned_name}' has no stored API key. "
                        "Set confirm_reset=true to generate and bind a new key. "
                        "Any previous key will stop working."
                    ),
                }

            api_key = self._reset_key(owned_id)
            if not api_key:
                return {"error": f"Unable to obtain an API key for agent '{owned_name}'"}
            self._bind_agent(owned_id, owned_name, api_key)
            return {
                "status": "authenticated",
                "message": f"Selected agent '{self.agent_name}'",
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_status(self) -> dict:
        """Get current auth status."""
        if self.is_authenticated:
            return {
                "status": "authenticated",
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
            }
        elif self.needs_agent_selection:
            return {
                "status": "needs_selection",
                "message": "Sign-in succeeded; select an agent to continue",
                "agents": [
                    {"id": a["id"], "name": a.get("name", "")}
                    for a in self.agents
                ],
            }
        elif self._error:
            return {"status": "error", "error": self._error}
        elif self._auth_complete.is_set():
            return {"status": "error", "error": "Sign-in failed; start a new sign-in flow"}
        else:
            return {"status": "not_authenticated"}

    def _shutdown_server(self):
        """Shutdown the temporary callback server."""
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            self._server = None


# Global instance for stdio mode
stdio_auth = StdioAuthManager()
