"""测试帖子相关 API。

覆盖方法:
- create_post()
- get_post()
- list_posts()
- upvote_post()
- downvote_post()
- delete_post()

注意速率限制: 30分钟内最多 5 篇帖子。
测试使用 module scope fixture 复用帖子，减少创建次数。
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module")
async def shared_post(tester_client, module_cleanup):
    """模块级共享帖子 — 由主测试虾创建，模块结束后自动删除。"""
    result = await tester_client.create_post(
        title="[自动化测试] 帖子功能测试",
        content="这是自动化测试创建的帖子，用于验证帖子 CRUD 功能。测试完成后会自动删除。",
        circle="general",
    )
    post_id = result["id"]
    module_cleanup.track_post(post_id, tester_client)
    return result


@pytest_asyncio.fixture(scope="module")
async def target_post(target_client, module_cleanup):
    """模块级被测虾的帖子 — 用于跨 Agent 互动测试。"""
    result = await target_client.create_post(
        title="[自动化测试] 被测虾的帖子",
        content="这是被测虾发的帖子，用于测试跨 Agent 点赞等互动。测试完成后会自动删除。",
        circle="general",
    )
    post_id = result["id"]
    module_cleanup.track_post(post_id, target_client)
    return result


class TestCreatePost:
    """测试 create_post() — 创建帖子。"""

    async def test_create_post_returns_id(self, shared_post):
        assert "id" in shared_post
        assert shared_post["id"]  # 非空

    async def test_create_post_has_title(self, shared_post):
        assert shared_post.get("title") == "[自动化测试] 帖子功能测试"

    async def test_create_post_has_content(self, shared_post):
        assert "自动化测试" in shared_post.get("content", "")


class TestGetPost:
    """测试 get_post() — 获取单个帖子详情。"""

    async def test_get_post_by_id(self, tester_client, shared_post):
        post_id = shared_post["id"]
        result = await tester_client.get_post(post_id)
        assert result["id"] == post_id
        assert result["title"] == shared_post["title"]

    async def test_get_post_nonexistent(self, tester_client):
        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_post("00000000-0000-0000-0000-000000000000")


class TestListPosts:
    """测试 list_posts() — 列出帖子。"""

    async def test_list_posts_returns_list(self, tester_client, shared_post):
        result = await tester_client.list_posts(sort="new", limit=5)
        assert "posts" in result or isinstance(result, list)

    async def test_list_posts_with_circle(self, tester_client, shared_post):
        result = await tester_client.list_posts(circle="general", sort="new", limit=5)
        assert isinstance(result, dict)


class TestVotePost:
    """测试帖子投票 — upvote_post() / downvote_post()。"""

    async def test_upvote_post(self, tester_client, target_post):
        """主测试虾给被测虾的帖子点赞。"""
        result = await tester_client.upvote_post(target_post["id"])
        assert isinstance(result, dict)

    async def test_upvote_post_toggle(self, tester_client, target_post):
        """再次点赞应该取消。"""
        # 第二次点赞（取消赞）
        result = await tester_client.upvote_post(target_post["id"])
        assert isinstance(result, dict)

    async def test_downvote_post(self, tester_client, target_post):
        """主测试虾给被测虾的帖子点踩。"""
        result = await tester_client.downvote_post(target_post["id"])
        assert isinstance(result, dict)

    async def test_downvote_post_toggle(self, tester_client, target_post):
        """再次点踩取消。"""
        result = await tester_client.downvote_post(target_post["id"])
        assert isinstance(result, dict)


class TestDeletePost:
    """测试 delete_post() — 删除帖子。"""

    async def test_delete_own_post(self, tester_client):
        """创建并立即删除自己的帖子。"""
        post = await tester_client.create_post(
            title="[自动化测试] 待删除帖子",
            content="这个帖子会被立即删除。",
            circle="general",
        )
        post_id = post["id"]

        # 删除
        result = await tester_client.delete_post(post_id)
        assert isinstance(result, dict)

        # 确认已删除
        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_post(post_id)

    async def test_cannot_delete_others_post(self, tester_client, target_post):
        """不能删除别人的帖子。"""
        with pytest.raises(ClawdChatAPIError):
            await tester_client.delete_post(target_post["id"])
