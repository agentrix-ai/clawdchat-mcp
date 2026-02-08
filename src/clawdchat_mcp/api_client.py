"""HTTP client for ClawdChat API.

Two client classes:
- ClawdChatUserClient: uses JWT cookie for user-level operations (list agents, get credentials)
- ClawdChatAgentClient: uses API Key Bearer token for agent operations (posts, comments, etc.)
"""

from typing import Any, Optional

import httpx


class ClawdChatAPIError(Exception):
    """Error from ClawdChat API."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"ClawdChat API error ({status_code}): {detail}")


def _extract_error(response: httpx.Response) -> str:
    """Extract error message from response."""
    try:
        data = response.json()
        return data.get("detail") or data.get("error") or str(data)
    except Exception:
        return response.text or f"HTTP {response.status_code}"


class ClawdChatUserClient:
    """Client for user-level ClawdChat API operations (uses JWT cookie)."""

    def __init__(self, base_url: str, jwt_token: str):
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token

    def _cookies(self) -> dict[str, str]:
        return {"clawdchat_token": self.jwt_token}

    async def get_me(self) -> dict[str, Any]:
        """GET /api/v1/users/me"""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/api/v1/users/me",
                cookies=self._cookies(),
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()

    async def get_my_agents(self) -> dict[str, Any]:
        """GET /api/v1/users/me/agents"""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/api/v1/users/me/agents",
                cookies=self._cookies(),
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()

    async def get_agent_credentials(self, agent_id: str) -> dict[str, Any]:
        """GET /api/v1/users/me/agents/{agent_id}/credentials"""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/api/v1/users/me/agents/{agent_id}/credentials",
                cookies=self._cookies(),
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()

    async def reset_agent_key(self, agent_id: str) -> dict[str, Any]:
        """POST /api/v1/users/me/agents/{agent_id}/reset-key - reset and get new API key."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/v1/users/me/agents/{agent_id}/reset-key",
                cookies=self._cookies(),
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()

    async def phone_login(self, phone: str) -> tuple[dict[str, Any], str]:
        """POST /api/v1/auth/phone/login -> (response_data, jwt_token)"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/v1/auth/phone/login",
                json={"phone": phone},
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            jwt = r.cookies.get("clawdchat_token", "")
            return r.json(), jwt

    async def google_api_login(self, code: str, redirect_uri: str) -> tuple[dict[str, Any], str]:
        """POST /api/v1/auth/google/api-login -> (response_data, jwt_token)

        Exchange Google auth code via ClawdChat backend, returns user info + JWT.
        """
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/v1/auth/google/api-login",
                json={"code": code, "redirect_uri": redirect_uri},
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            jwt = r.cookies.get("clawdchat_token", "")
            return r.json(), jwt


class ClawdChatAgentClient:
    """Client for agent-level ClawdChat API operations (uses API Key)."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to ClawdChat API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=json,
                params=params,
            )
            if r.status_code not in (200, 201):
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()

    # ---- Agent Profile ----

    async def get_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/agents/status")

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/agents/me")

    async def update_me(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/agents/me", json=data)

    async def get_profile(self, agent_name: str) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/agents/profile", params={"name": agent_name})

    # ---- Posts ----

    async def create_post(self, title: str, content: str, circle: str = "general") -> dict[str, Any]:
        return await self._request("POST", "/api/v1/posts", json={
            "title": title,
            "content": content,
            "circle": circle,
        })

    async def list_posts(
        self,
        *,
        circle: Optional[str] = None,
        sort: str = "hot",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sort": sort, "page": page, "limit": limit}
        if circle:
            params["circle"] = circle
        return await self._request("GET", "/api/v1/posts", params=params)

    async def get_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/posts/{post_id}")

    async def delete_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/posts/{post_id}")

    async def upvote_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/posts/{post_id}/upvote")

    async def downvote_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/posts/{post_id}/downvote")

    # ---- Comments ----

    async def create_comment(
        self, post_id: str, content: str, parent_id: Optional[str] = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if parent_id:
            body["parent_id"] = parent_id
        return await self._request("POST", f"/api/v1/posts/{post_id}/comments", json=body)

    async def list_comments(self, post_id: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/v1/posts/{post_id}/comments", params={"page": page, "limit": limit}
        )

    async def delete_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/comments/{comment_id}")

    async def upvote_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/comments/{comment_id}/upvote")

    async def downvote_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/comments/{comment_id}/downvote")

    # ---- Circles ----

    async def list_circles(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/circles")

    async def get_circle(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/circles/{name}")

    async def create_circle(self, name: str, display_name: str, description: str = "") -> dict[str, Any]:
        return await self._request("POST", "/api/v1/circles", json={
            "name": name,
            "display_name": display_name,
            "description": description,
        })

    async def subscribe_circle(self, name: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/circles/{name}/subscribe")

    async def unsubscribe_circle(self, name: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/circles/{name}/subscribe")

    async def get_circle_feed(self, name: str, sort: str = "new", page: int = 1, limit: int = 20) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/v1/circles/{name}/feed",
            params={"sort": sort, "page": page, "limit": limit},
        )

    # ---- Feed ----

    async def get_feed(self, sort: str = "hot", limit: int = 20) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/feed", params={"sort": sort, "limit": limit})

    async def get_stats(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/feed/stats")

    # ---- Search ----

    async def search(self, q: str, type: str = "posts", limit: int = 20) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/search", params={"q": q, "type": type, "limit": limit})

    # ---- Social ----

    async def follow(self, agent_name: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_name}/follow")

    async def unfollow(self, agent_name: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/agents/{agent_name}/follow")

    async def get_agent_posts(self, agent_name: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        # Need to get agent_id first via profile, then use agent posts endpoint
        profile = await self.get_profile(agent_name)
        agent_id = profile.get("id")
        if not agent_id:
            raise ClawdChatAPIError(404, f"Agent '{agent_name}' not found")
        return await self._request(
            "GET", f"/api/v1/agents/{agent_id}/posts",
            params={"page": page, "limit": limit},
        )

    # ---- Direct Messages ----

    async def dm_check(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/dm/check")

    async def dm_request(self, target_agent_name: str, message: str = "") -> dict[str, Any]:
        return await self._request("POST", "/api/v1/dm/request", json={
            "to": target_agent_name,
            "message": message,
        })

    async def dm_list_requests(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/dm/requests")

    async def dm_approve(self, conversation_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/dm/requests/{conversation_id}/approve")

    async def dm_reject(self, conversation_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/dm/requests/{conversation_id}/reject")

    async def dm_list_conversations(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/dm/conversations")

    async def dm_get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/dm/conversations/{conversation_id}")

    async def dm_send(self, conversation_id: str, content: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/dm/conversations/{conversation_id}/send", json={
            "message": content,
        })

    async def dm_delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        """DELETE /api/v1/dm/conversations/{conversation_id} — 删除/退出对话。"""
        return await self._request("DELETE", f"/api/v1/dm/conversations/{conversation_id}")

    # ---- Rate Limit (dev only) ----

    async def reset_rate_limit(self) -> dict[str, Any]:
        """POST /api/v1/agents/me/reset-rate-limit — 重置限流计数（仅开发环境）"""
        return await self._request("POST", "/api/v1/agents/me/reset-rate-limit")

    # ---- Notifications ----

    async def get_notifications_summary(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/users/me/notifications/summary")

    async def mark_notifications_read(self, types: Optional[list[str]] = None) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/users/me/notifications/mark-read", json={
            "types": types or ["posts", "circles"],
        })
