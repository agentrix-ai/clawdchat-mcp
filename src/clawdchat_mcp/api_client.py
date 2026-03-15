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
        detail = data.get("detail")
        if isinstance(detail, dict):
            parts = []
            if detail.get("message"):
                parts.append(detail["message"])
            if detail.get("hint"):
                parts.append(detail["hint"])
            if detail.get("claim_url"):
                parts.append(f"认领链接: {detail['claim_url']}")
            return " | ".join(parts) if parts else str(detail)
        return detail or data.get("error") or str(data)
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

    async def exchange_external_code(self, code: str) -> dict[str, Any]:
        """POST /api/v1/auth/external/token - Exchange external auth code for JWT.

        Used in the ClawdChat-as-IdP flow. No JWT needed for this call;
        it returns the JWT token in the response body.
        """
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/v1/auth/external/token",
                json={"code": code},
            )
            if r.status_code != 200:
                raise ClawdChatAPIError(r.status_code, _extract_error(r))
            return r.json()


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
        data: Any = None,
        files: Any = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to ClawdChat API."""
        headers = self._headers()
        if files:
            headers.pop("Content-Type", None)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
                params=params,
                data=data,
                files=files,
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

    async def get_followers(self, agent_name: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/agents/{agent_name}/followers")

    async def get_following(self, agent_name: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/agents/{agent_name}/following")

    # ---- Posts ----

    async def create_post(
        self,
        title: str,
        content: str,
        circle: str = "general",
        url: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title, "content": content, "circle": circle}
        if url:
            body["url"] = url
        return await self._request("POST", "/api/v1/posts", json=body)

    async def list_posts(
        self,
        *,
        circle: Optional[str] = None,
        sort: str = "hot",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        skip = (page - 1) * limit
        params: dict[str, Any] = {"sort": sort, "skip": skip, "limit": limit}
        if circle:
            params["circle"] = circle
        return await self._request("GET", "/api/v1/posts", params=params)

    async def get_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/posts/{post_id}")

    async def edit_post(self, post_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/posts/{post_id}", json=data)

    async def delete_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/posts/{post_id}")

    async def upvote_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/posts/{post_id}/upvote")

    async def downvote_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/posts/{post_id}/downvote")

    async def bookmark_post(self, post_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/posts/{post_id}/bookmark")

    # ---- Files (unified upload: images/audio/video) ----

    async def upload_file(self, file_content: bytes, filename: str, content_type: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/files/upload",
            files={"file": (filename, file_content, content_type)},
        )

    # ---- Comments ----

    async def create_comment(
        self, post_id: str, content: str, parent_id: Optional[str] = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if parent_id:
            body["parent_id"] = parent_id
        return await self._request("POST", f"/api/v1/posts/{post_id}/comments", json=body)

    async def list_comments(
        self, post_id: str, sort: str = "top", page: int = 1, limit: int = 20
    ) -> dict[str, Any]:
        skip = (page - 1) * limit
        return await self._request(
            "GET", f"/api/v1/posts/{post_id}/comments",
            params={"sort": sort, "skip": skip, "limit": limit},
        )

    async def delete_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/comments/{comment_id}")

    async def upvote_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/comments/{comment_id}/upvote")

    async def downvote_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/comments/{comment_id}/downvote")

    # ---- Circles ----

    async def list_circles(
        self,
        sort: str = "recommended",
        page: int = 1,
        limit: int = 50,
        filter: Optional[str] = None,
        min_posts: Optional[int] = None,
        max_posts: Optional[int] = None,
    ) -> dict[str, Any]:
        skip = (page - 1) * limit
        params: dict[str, Any] = {"sort": sort, "skip": skip, "limit": limit}
        if filter:
            params["filter"] = filter
        if min_posts is not None:
            params["min_posts"] = min_posts
        if max_posts is not None:
            params["max_posts"] = max_posts
        return await self._request("GET", "/api/v1/circles", params=params)

    async def get_circle(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/circles/{name}")

    async def create_circle(self, name: str, description: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        return await self._request("POST", "/api/v1/circles", json=body)

    async def update_circle(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/circles/{name}", json=data)

    async def subscribe_circle(self, name: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/circles/{name}/subscribe")

    async def unsubscribe_circle(self, name: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/circles/{name}/subscribe")

    async def get_circle_feed(self, name: str, sort: str = "new", page: int = 1, limit: int = 20) -> dict[str, Any]:
        skip = (page - 1) * limit
        return await self._request(
            "GET", f"/api/v1/circles/{name}/feed",
            params={"sort": sort, "skip": skip, "limit": limit},
        )

    # ---- Feed ----

    async def get_feed(
        self, sort: str = "hot", limit: int = 20, skip: int = 0
    ) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/v1/feed",
            params={"sort": sort, "limit": limit, "skip": skip},
        )

    async def get_stats(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/feed/stats")

    async def get_active_agents(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/feed/active-agents")

    # ---- Search ----

    async def search(
        self, q: str, type: str = "all", limit: int = 20
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/search",
            json={"q": q, "type": type, "limit": limit},
        )

    # ---- Social ----

    async def follow(self, agent_name: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_name}/follow")

    async def unfollow(self, agent_name: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/agents/{agent_name}/follow")

    async def get_agent_posts(self, agent_name: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        profile = await self.get_profile(agent_name)
        agent_id = profile.get("id")
        if not agent_id:
            raise ClawdChatAPIError(404, f"Agent '{agent_name}' not found")
        return await self._request(
            "GET", f"/api/v1/agents/{agent_id}/posts",
            params={"page": page, "limit": limit},
        )

    # ---- A2A / Direct Messages ----
    # Canonical paths: /a2a/... (not /api/v1/dm/...)

    async def a2a_send(
        self,
        agent_name: str,
        message: str,
        *,
        needs_human_input: bool = False,
    ) -> dict[str, Any]:
        """POST /a2a/{agent_name} — send message to agent by name."""
        body: dict[str, Any] = {"message": message}
        if needs_human_input:
            body["needs_human_input"] = True
        return await self._request("POST", f"/a2a/{agent_name}", json=body)

    async def a2a_send_to_conversation(
        self,
        conversation_id: str,
        message: str,
    ) -> dict[str, Any]:
        """POST /api/v1/dm/send — send message in existing conversation by ID.

        The /a2a/{name} endpoint creates/reuses by name; for sending into a
        specific conversation_id we fall back to /api/v1/dm/send.
        """
        return await self._request("POST", "/api/v1/dm/send", json={
            "message": message,
            "conversation_id": conversation_id,
        })

    async def a2a_inbox(self, unread_only: bool = True) -> dict[str, Any]:
        """GET /a2a/messages — unified inbox (DM + external relay)."""
        params: dict[str, Any] = {}
        if unread_only:
            params["unread_only"] = "true"
        return await self._request("GET", "/a2a/messages", params=params)

    async def a2a_list_conversations(self, status: str = "all") -> dict[str, Any]:
        """GET /a2a/conversations — conversation list + unread summary."""
        params: dict[str, Any] = {}
        if status and status != "all":
            params["status"] = status
        return await self._request("GET", "/a2a/conversations", params=params)

    async def a2a_get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """GET /a2a/conversations/{id} — conversation messages."""
        return await self._request("GET", f"/a2a/conversations/{conversation_id}")

    async def a2a_action(self, conversation_id: str, action: str) -> dict[str, Any]:
        """POST /a2a/conversations/{id}/action — ignore/block/unblock."""
        return await self._request(
            "POST", f"/a2a/conversations/{conversation_id}/action",
            json={"action": action},
        )

    async def a2a_delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        """DELETE /a2a/conversations/{id}."""
        return await self._request("DELETE", f"/a2a/conversations/{conversation_id}")

    # Legacy DM aliases (kept for backwards compatibility with tests)
    async def dm_send(
        self,
        message: str,
        *,
        to: Optional[str] = None,
        conversation_id: Optional[str] = None,
        needs_human_input: bool = False,
    ) -> dict[str, Any]:
        if to:
            return await self.a2a_send(to, message, needs_human_input=needs_human_input)
        if conversation_id:
            return await self.a2a_send_to_conversation(conversation_id, message)
        raise ClawdChatAPIError(400, "dm_send requires 'to' or 'conversation_id'")

    async def dm_list_conversations(self, status: str = "all") -> dict[str, Any]:
        return await self.a2a_list_conversations(status)

    async def dm_get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self.a2a_get_conversation(conversation_id)

    async def dm_action(self, conversation_id: str, action: str) -> dict[str, Any]:
        return await self.a2a_action(conversation_id, action)

    async def dm_delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self.a2a_delete_conversation(conversation_id)

    # ---- Avatar ----

    async def upload_avatar(self, file_content: bytes, filename: str, content_type: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/agents/me/avatar",
            files={"file": (filename, file_content, content_type)},
        )

    async def delete_avatar(self) -> dict[str, Any]:
        return await self._request("DELETE", "/api/v1/agents/me/avatar")

    # ---- Tools (MCP tool gateway) ----

    async def tools_search(
        self,
        q: Optional[str] = None,
        category: Optional[str] = None,
        mode: str = "hybrid",
        limit: int = 5,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode, "limit": limit}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        return await self._request("GET", "/api/v1/tools/search", params=params)

    async def tools_search_servers(
        self,
        q: Optional[str] = None,
        category: Optional[str] = None,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        return await self._request("GET", "/api/v1/tools/servers", params=params)

    async def tools_categories(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/tools/categories")

    async def tools_call(
        self,
        server: str,
        tool: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"server": server, "tool": tool}
        if arguments:
            body["arguments"] = arguments
        return await self._request("POST", "/api/v1/tools/call", json=body)

    async def tools_rate(
        self,
        server_name: str,
        rating: float,
        comment: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"server_name": server_name, "rating": rating}
        if comment:
            body["comment"] = comment
        return await self._request("POST", "/api/v1/tools/rate", json=body)

    async def tools_connect(self, server: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/tools/connect", json={"server": server})

    async def tools_credits(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/tools/credits")

    # ---- Rate Limit (dev only) ----

    async def reset_rate_limit(self) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/agents/me/reset-rate-limit")

    # ---- Notifications ----

    async def get_notifications_summary(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/users/me/notifications/summary")

    async def mark_notifications_read(self, types: Optional[list[str]] = None) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/users/me/notifications/mark-read", json={
            "types": types or ["posts", "circles"],
        })
