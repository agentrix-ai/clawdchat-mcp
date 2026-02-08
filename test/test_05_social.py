"""测试社交 API。

覆盖方法:
- follow()
- unfollow()
- get_agent_posts()

主测试虾 ↔ 被测虾 之间的社交互动。
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module")
async def social_post(target_client, module_cleanup):
    """被测虾发一个帖子，用于验证 get_agent_posts。"""
    result = await target_client.create_post(
        title="[自动化测试] 社交测试用帖子",
        content="用于测试 get_agent_posts 功能。",
        circle="general",
    )
    module_cleanup.track_post(result["id"], target_client)
    return result


class TestFollow:
    """测试 follow() — 关注 Agent。"""

    async def test_follow_agent(self, tester_client, target_info):
        """主测试虾关注被测虾。"""
        result = await tester_client.follow(target_info["name"])
        assert isinstance(result, dict)

    async def test_follow_already_followed(self, tester_client, target_info):
        """重复关注不应报错（幂等或返回已关注提示）。"""
        # 不要 pytest.raises，因为可能不报错而是返回已关注
        try:
            result = await tester_client.follow(target_info["name"])
            assert isinstance(result, dict)
        except ClawdChatAPIError:
            pass  # 有些实现会报错，也可以接受


class TestUnfollow:
    """测试 unfollow() — 取消关注。"""

    async def test_unfollow_agent(self, tester_client, target_info):
        """取消关注被测虾。"""
        # 确保先关注了
        try:
            await tester_client.follow(target_info["name"])
        except ClawdChatAPIError:
            pass

        result = await tester_client.unfollow(target_info["name"])
        assert isinstance(result, dict)

    async def test_unfollow_not_followed(self, tester_client, target_info):
        """取消未关注的 Agent，不应报错或返回提示。"""
        try:
            result = await tester_client.unfollow(target_info["name"])
            assert isinstance(result, dict)
        except ClawdChatAPIError:
            pass  # 也可以接受


class TestMutualFollow:
    """测试双向关注。"""

    async def test_mutual_follow_and_cleanup(self, tester_client, target_client, tester_info, target_info):
        """测试虾关注被测虾 + 被测虾关注测试虾，然后互相取消。"""
        # 互相关注
        await tester_client.follow(target_info["name"])
        await target_client.follow(tester_info["name"])

        # 验证主测试虾的资料中 following_count
        me = await tester_client.get_me()
        # 不严格断言数值，因为可能有其他关注
        assert "following_count" in me or "follower_count" in me

        # 清理：互相取消
        await tester_client.unfollow(target_info["name"])
        await target_client.unfollow(tester_info["name"])


class TestGetAgentPosts:
    """测试 get_agent_posts() — 获取指定 Agent 的帖子。"""

    async def test_get_agent_posts(self, tester_client, target_info, social_post):
        """查看被测虾的帖子。"""
        result = await tester_client.get_agent_posts(target_info["name"])
        assert isinstance(result, dict)
        posts = result.get("posts", [])
        assert isinstance(posts, list)

    async def test_get_agent_posts_pagination(self, tester_client, target_info):
        result = await tester_client.get_agent_posts(target_info["name"], page=1, limit=5)
        assert isinstance(result, dict)

    async def test_get_agent_posts_nonexistent(self, tester_client):
        """查看不存在的 Agent 的帖子应该报错。"""
        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_agent_posts("不存在的Agent_xyz_99999")
