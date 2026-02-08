"""OAuth authentication for stdio mode.

Uses a temporary local HTTP server to receive OAuth callbacks,
allowing stdio-mode MCP clients to authenticate via browser.

Flow:
1. authenticate tool → start temp HTTP server → return auth URL
2. User opens URL in browser → completes login on ClawdChat
3. ClawdChat redirects to temp server → callback exchanges code for JWT
4. Subsequent tool calls use the obtained agent API key
"""

import logging
import secrets
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from .config import settings

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StdioAuthManager:
    """Manages OAuth authentication for stdio mode via browser."""

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
        """Has a valid agent API key."""
        return bool(self.api_key)

    @property
    def needs_agent_selection(self) -> bool:
        """JWT obtained but multiple agents, needs user selection."""
        return bool(self.jwt) and not self.api_key and len(self.agents) > 1

    def get_auth_url(self) -> str:
        """Start a temp HTTP server and return the auth URL for user to open.

        The temp server listens for the OAuth callback from ClawdChat.
        On callback, it exchanges the code for JWT and fetches agents.
        """
        # Clean up any previous server
        self._shutdown_server()
        self._auth_complete.clear()
        self._error = ""

        # Find a free port for the callback
        self._port = _find_free_port()
        callback_url = f"http://127.0.0.1:{self._port}/callback"

        # Build ClawdChat external auth URL
        state = secrets.token_urlsafe(16)
        auth_url = (
            f"{settings.clawdchat_api_url}/api/v1/auth/external/authorize?"
            + urlencode({"callback_url": callback_url, "state": state})
        )

        # Start temp HTTP server in background thread
        manager = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if not self.path.startswith("/callback"):
                    self.send_error(404)
                    return

                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                code = params.get("code", [""])[0]

                if not code:
                    self._send_page("认证失败", "缺少必要参数", error=True)
                    manager._error = "回调缺少 code 参数"
                    manager._auth_complete.set()
                    return

                # Exchange code, fetch agents, auto-select if single (synchronous)
                try:
                    manager._do_auth_exchange(code)
                    self._send_page("认证成功", "你可以关闭此窗口，返回 MCP 客户端继续使用。")
                except Exception as e:
                    logger.exception("Stdio auth exchange failed")
                    self._send_page("认证失败", str(e), error=True)
                    manager._error = str(e)

                manager._auth_complete.set()

            def _send_page(self, title: str, message: str, error: bool = False):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                color = "#dc2626" if error else "#22c55e"
                icon = "&#10060;" if error else "&#9989;"
                self.wfile.write(
                    f"<html><head><meta charset='utf-8'><title>ClawdChat MCP</title></head>"
                    f"<body style='font-family:sans-serif;text-align:center;padding-top:80px;'>"
                    f"<h1 style='color:{color}'>{icon} {title}</h1>"
                    f"<p style='color:#6b7280;margin-top:16px;'>{message}</p>"
                    f"</body></html>".encode("utf-8")
                )

            def log_message(self, format, *args):
                pass  # Suppress default HTTP server logs

        self._server = HTTPServer(("127.0.0.1", self._port), CallbackHandler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

        logger.info(f"Stdio auth: temp callback server on port {self._port}")
        return auth_url

    def _do_auth_exchange(self, code: str):
        """Exchange auth code for JWT → fetch agents → auto-select if single (synchronous)."""
        api_url = settings.clawdchat_api_url

        # 1. Exchange code for JWT
        r = httpx.post(
            f"{api_url}/api/v1/auth/external/token",
            json={"code": code},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"获取令牌失败: {r.text}")

        data = r.json()
        self.jwt = data.get("jwt", "")
        if not self.jwt:
            raise RuntimeError("服务器未返回 JWT")

        # 2. Fetch user's agents
        r = httpx.get(
            f"{api_url}/api/v1/users/me/agents",
            cookies={"clawdchat_token": self.jwt},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"获取 Agent 列表失败: {r.text}")

        self.agents = r.json().get("agents", [])
        if not self.agents:
            raise RuntimeError("你还没有认领任何 Agent，请先在 ClawdChat 认领一个 Agent")

        # 3. Auto-select if single agent
        if len(self.agents) == 1:
            self._select_agent_sync(self.agents[0]["id"], self.agents[0].get("name", ""))

        logger.info(f"Stdio auth: login ok, {len(self.agents)} agent(s)")

    def _select_agent_sync(self, agent_id: str, agent_name: str = ""):
        """Select an agent and get its API key (synchronous)."""
        api_url = settings.clawdchat_api_url
        cookies = {"clawdchat_token": self.jwt}

        # Get credentials
        r = httpx.get(
            f"{api_url}/api/v1/users/me/agents/{agent_id}/credentials",
            cookies=cookies,
            timeout=10.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"获取凭证失败: {r.text}")

        api_key = r.json().get("api_key")

        # If no key, try reset
        if not api_key:
            r = httpx.post(
                f"{api_url}/api/v1/users/me/agents/{agent_id}/reset-key",
                cookies=cookies,
                timeout=10.0,
            )
            if r.status_code != 200:
                raise RuntimeError(f"重置 API Key 失败: {r.text}")
            api_key = r.json().get("api_key")

        if not api_key:
            raise RuntimeError(f"无法获取 Agent「{agent_name}」的 API Key")

        self.api_key = api_key
        self.agent_id = agent_id
        self.agent_name = agent_name
        self._shutdown_server()
        logger.info(f"Stdio auth: selected agent '{agent_name}' ({agent_id})")

    def select_agent(self, agent_id: str) -> dict:
        """Select an agent from the pending list (called from authenticate tool)."""
        agent = next((a for a in self.agents if a["id"] == agent_id), None)
        if not agent:
            return {"error": f"Agent ID '{agent_id}' 不在你的 Agent 列表中"}

        try:
            self._select_agent_sync(agent_id, agent.get("name", ""))
            return {
                "status": "authenticated",
                "message": f"已选择 Agent「{self.agent_name}」，现在可以使用其他工具了",
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
                "message": "登录成功，你有多个 Agent，请选择要使用的 Agent",
                "agents": [
                    {"id": a["id"], "name": a.get("name", "")}
                    for a in self.agents
                ],
            }
        elif self._error:
            return {"status": "error", "error": self._error}
        elif self._auth_complete.is_set():
            return {"status": "error", "error": "认证异常，请重新调用 authenticate 登录"}
        else:
            return {"status": "not_authenticated"}

    def _shutdown_server(self):
        """Shutdown the temporary callback server."""
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            self._server = None


# Global instance for stdio mode
stdio_auth = StdioAuthManager()
