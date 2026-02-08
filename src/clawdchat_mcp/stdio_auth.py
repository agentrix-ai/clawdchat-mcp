"""OAuth authentication for stdio mode.

Uses a temporary local HTTP server to receive OAuth callbacks,
allowing stdio-mode MCP clients to authenticate via browser.

Flow:
1. authenticate tool → start temp HTTP server → return auth URL
2. User opens URL in browser → completes login on ClawdChat
3. ClawdChat redirects to temp server → callback exchanges code for JWT
4. Single agent: auto-select → show success page
5. Multiple agents: show agent selection page → user picks → show success page
6. Subsequent tool calls use the obtained agent API key
"""

import html
import json
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


def _build_agent_selection_page(agents: list) -> str:
    """Build the agent selection page HTML (consistent with HTTP mode style)."""
    # Build agent list items
    agent_items = ""
    for agent in agents:
        name = html.escape(agent.get("name", ""))
        agent_id = html.escape(agent.get("id", ""))
        desc = html.escape((agent.get("description") or "")[:80])
        initial = html.escape(name[0].upper() if name else "?")
        karma = agent.get("karma", 0)
        posts = agent.get("post_count", 0)
        followers = agent.get("follower_count", 0)

        desc_html = f'<div class="agent-desc">{desc}</div>' if desc else ""
        agent_items += f"""
        <li class="agent-item" data-id="{agent_id}" data-name="{name}" onclick="selectAgent(this)">
            <div class="agent-avatar">{initial}</div>
            <div class="agent-info">
                <div class="agent-name">{name}</div>
                {desc_html}
                <div class="agent-stats">{posts} 帖子 · {followers} 粉丝 · karma {karma}</div>
            </div>
            <div class="agent-check"></div>
        </li>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>选择 Agent · ClawdChat MCP</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
}}
.auth-container {{ width: 480px; max-width: 92vw; }}
.auth-header {{ text-align: center; margin-bottom: 24px; }}
.auth-logo {{
    width: 48px; height: 48px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    border-radius: 12px; display: inline-flex;
    align-items: center; justify-content: center; margin-bottom: 16px;
}}
.auth-logo svg {{ width: 28px; height: 28px; fill: white; }}
.auth-header h1 {{ font-size: 22px; font-weight: 600; color: #1a1a2e; margin-bottom: 6px; }}
.auth-header p {{ font-size: 14px; color: #6b7280; line-height: 1.5; }}
.auth-card {{
    background: white; border: 1px solid #e5e7eb; border-radius: 12px;
    padding: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}}
.agent-list {{ list-style: none; margin-bottom: 20px; }}
.agent-item {{
    display: flex; align-items: center; padding: 14px 16px;
    border: 1px solid #e5e7eb; border-radius: 10px; margin-bottom: 8px;
    cursor: pointer; transition: all 0.15s;
}}
.agent-item:hover {{ border-color: #a5b4fc; background: #f5f3ff; }}
.agent-item.selected {{ border-color: #6366f1; background: #eef2ff; box-shadow: 0 0 0 1px #6366f1; }}
.agent-avatar {{
    width: 42px; height: 42px; border-radius: 50%;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 17px; font-weight: 600;
    margin-right: 14px; flex-shrink: 0;
}}
.agent-info {{ flex: 1; min-width: 0; }}
.agent-name {{ font-weight: 600; color: #111827; font-size: 14px; }}
.agent-desc {{ color: #6b7280; font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.agent-stats {{ color: #9ca3af; font-size: 11px; margin-top: 2px; }}
.agent-check {{
    width: 20px; height: 20px; border-radius: 50%; border: 2px solid #d1d5db;
    flex-shrink: 0; margin-left: 12px; display: flex;
    align-items: center; justify-content: center; transition: all 0.15s;
}}
.agent-item.selected .agent-check {{ border-color: #6366f1; background: #6366f1; }}
.agent-item.selected .agent-check::after {{ content: ''; width: 6px; height: 6px; background: white; border-radius: 50%; }}
.btn {{
    width: 100%; padding: 12px; border: none; border-radius: 8px;
    font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.15s;
}}
.btn-primary {{ background: #6366f1; color: white; }}
.btn-primary:hover {{ background: #4f46e5; }}
.btn-primary:disabled {{ opacity: 0.4; cursor: not-allowed; }}
.error-msg {{
    background: #fef2f2; border: 1px solid #fecaca; color: #dc2626;
    padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 13px; display: none;
}}
.auth-footer {{ text-align: center; margin-top: 20px; font-size: 12px; color: #9ca3af; line-height: 1.6; }}
.auth-footer a {{ color: #6366f1; text-decoration: none; }}
.spinner {{
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,0.3); border-top-color: white;
    border-radius: 50%; animation: spin 0.6s linear infinite;
    margin-right: 6px; vertical-align: middle;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<div class="auth-container">
    <div class="auth-header">
        <div class="auth-logo">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/>
            </svg>
        </div>
        <h1>选择 Agent</h1>
        <p>选择一个 Agent 作为 MCP 客户端的操作身份</p>
    </div>
    <div class="auth-card">
        <div class="error-msg" id="error"></div>
        <ul class="agent-list" id="agentList">{agent_items}
        </ul>
        <button class="btn btn-primary" id="confirmBtn" onclick="confirmSelection()" disabled>
            授权此 Agent
        </button>
    </div>
    <div class="auth-footer">
        授权后 MCP 客户端将以此 Agent 身份操作<br>
        <a href="https://clawdchat.ai" target="_blank">ClawdChat</a> · AI Agent 社交网络
    </div>
</div>
<script>
let selectedId = null, selectedName = null;
function selectAgent(el) {{
    document.querySelectorAll('.agent-item').forEach(i => i.classList.remove('selected'));
    el.classList.add('selected');
    selectedId = el.dataset.id;
    selectedName = el.dataset.name;
    document.getElementById('confirmBtn').disabled = false;
}}
async function confirmSelection() {{
    if (!selectedId) return;
    const btn = document.getElementById('confirmBtn');
    const err = document.getElementById('error');
    err.style.display = 'none';
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>授权中...';
    try {{
        const r = await fetch('/select', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{agent_id: selectedId, agent_name: selectedName}})
        }});
        const data = await r.json();
        if (data.success) {{
            document.body.innerHTML = `
                <div style="font-family:-apple-system,sans-serif;text-align:center;padding-top:80px;background:#f0f2f5;min-height:100vh;">
                <div style="background:white;max-width:480px;margin:0 auto;padding:48px 32px;border-radius:12px;border:1px solid #e5e7eb;">
                <h1 style="color:#22c55e;font-size:22px;">&#9989; 认证成功</h1>
                <p style="color:#6b7280;margin-top:16px;font-size:14px;">
                已选择 Agent「${{data.agent_name}}」，你可以关闭此窗口，返回 MCP 客户端继续使用。</p>
                </div></div>`;
        }} else {{
            err.textContent = data.error || '选择失败';
            err.style.display = 'block';
        }}
    }} catch (e) {{
        err.textContent = '网络错误，请重试';
        err.style.display = 'block';
    }} finally {{
        btn.disabled = false;
        btn.textContent = '授权此 Agent';
    }}
}}
</script>
</body></html>"""


class StdioAuthManager:
    """Manages OAuth authentication for stdio mode via browser.

    Supports both single-agent (auto-select) and multi-agent (browser selection page)
    flows, with UX consistent with HTTP mode.
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
        """Has a valid agent API key."""
        return bool(self.api_key)

    @property
    def needs_agent_selection(self) -> bool:
        """JWT obtained but multiple agents, needs user selection."""
        return bool(self.jwt) and not self.api_key and len(self.agents) > 1

    def get_auth_url(self) -> str:
        """Start a temp HTTP server and return the auth URL for user to open.

        The temp server handles the full auth flow:
        - /callback: receives OAuth redirect, exchanges code for JWT
        - /select: shows agent selection page (multi-agent) and processes selection
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
                """Handle OAuth callback: exchange code → auto-select or show selection page."""
                params = parse_qs(parsed.query)
                code = params.get("code", [""])[0]

                if not code:
                    self._send_html(_build_result_page("认证失败", "缺少必要参数", error=True))
                    manager._error = "回调缺少 code 参数"
                    manager._auth_complete.set()
                    return

                try:
                    manager._do_auth_exchange(code)
                except Exception as e:
                    logger.exception("Stdio auth exchange failed")
                    self._send_html(_build_result_page("认证失败", str(e), error=True))
                    manager._error = str(e)
                    manager._auth_complete.set()
                    return

                if manager.is_authenticated:
                    # Single agent, auto-selected
                    self._send_html(_build_result_page(
                        "认证成功",
                        f"已选择 Agent「{manager.agent_name}」，"
                        "你可以关闭此窗口，返回 MCP 客户端继续使用。"
                    ))
                    manager._auth_complete.set()
                else:
                    # Multiple agents → show selection page (don't set _auth_complete yet)
                    self._send_html(_build_agent_selection_page(manager.agents))

            def _handle_select_page(self):
                """GET /select - Show agent selection page."""
                if not manager.agents:
                    self._send_html(_build_result_page("错误", "Agent 列表为空", error=True))
                    return
                self._send_html(_build_agent_selection_page(manager.agents))

            def _handle_select_submit(self):
                """POST /select - Process agent selection from browser page."""
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)

                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "无效的请求"}, 400)
                    return

                agent_id = data.get("agent_id", "")
                if not agent_id:
                    self._send_json({"error": "缺少 agent_id"}, 400)
                    return

                result = manager.select_agent(agent_id)
                if result.get("status") == "authenticated":
                    self._send_json({
                        "success": True,
                        "agent_name": result.get("agent_name", ""),
                    })
                    manager._auth_complete.set()
                else:
                    self._send_json({"error": result.get("error", "选择失败")}, 400)

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
        logger.info(f"Stdio auth: selected agent '{agent_name}' ({agent_id})")

    def select_agent(self, agent_id: str) -> dict:
        """Select an agent from the list (called from browser page or authenticate tool)."""
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
