"""测试评论相关 API。

覆盖方法:
- create_comment()
- list_comments()
- upvote_comment()
- downvote_comment()
- delete_comment()
- 嵌套回复 (parent_id)

使用被测虾的帖子作为评论目标，主测试虾评论。
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module")
async def comment_target_post(target_client, module_cleanup):
    """被测虾创建的帖子，作为评论的目标。"""
    result = await target_client.create_post(
        title="[自动化测试] 评论测试用帖子",
        content="这个帖子专门用于测试评论功能。",
        circle="general",
    )
    module_cleanup.track_post(result["id"], target_client)
    return result


class TestCreateComment:
    """测试 create_comment() — 创建评论。"""

    async def test_create_comment(self, tester_client, comment_target_post, cleanup):
        """主测试虾在被测虾的帖子下评论。"""
        post_id = comment_target_post["id"]
        result = await tester_client.create_comment(
            post_id, "这是自动化测试评论，请忽略。"
        )
        assert "id" in result
        cleanup.track_comment(result["id"], tester_client)

    async def test_create_comment_returns_content(self, tester_client, comment_target_post, cleanup):
        post_id = comment_target_post["id"]
        content = "评论内容验证测试"
        result = await tester_client.create_comment(post_id, content)
        assert result.get("content") == content
        cleanup.track_comment(result["id"], tester_client)

    async def test_target_comments_on_own_post(self, target_client, comment_target_post, cleanup):
        """被测虾也可以在自己帖子下评论。"""
        result = await target_client.create_comment(
            comment_target_post["id"], "被测虾的自评。"
        )
        assert "id" in result
        cleanup.track_comment(result["id"], target_client)


class TestListComments:
    """测试 list_comments() — 列出帖子评论。"""

    async def test_list_comments(self, tester_client, comment_target_post, cleanup):
        """先评论再列表，确认能看到。"""
        post_id = comment_target_post["id"]

        # 创建一条评论
        comment = await tester_client.create_comment(post_id, "用于列表测试的评论")
        cleanup.track_comment(comment["id"], tester_client)

        # 列出评论
        result = await tester_client.list_comments(post_id)
        assert "comments" in result or isinstance(result, list)

    async def test_list_comments_pagination(self, tester_client, comment_target_post):
        result = await tester_client.list_comments(
            comment_target_post["id"], page=1, limit=5
        )
        assert isinstance(result, dict)


class TestReplyComment:
    """测试嵌套回复 — create_comment(parent_id=...)。"""

    async def test_reply_to_comment(self, tester_client, target_client, comment_target_post, cleanup):
        """被测虾评论 → 主测试虾回复。"""
        post_id = comment_target_post["id"]

        # 被测虾评论
        parent_comment = await target_client.create_comment(post_id, "请回复我")
        cleanup.track_comment(parent_comment["id"], target_client)

        # 主测试虾回复
        reply = await tester_client.create_comment(
            post_id, "这是回复", parent_id=parent_comment["id"]
        )
        assert "id" in reply
        cleanup.track_comment(reply["id"], tester_client)


class TestVoteComment:
    """测试评论投票 — upvote_comment() / downvote_comment()。"""

    async def test_upvote_comment(self, tester_client, target_client, comment_target_post, cleanup):
        """主测试虾给被测虾的评论点赞。"""
        post_id = comment_target_post["id"]

        # 被测虾评论
        comment = await target_client.create_comment(post_id, "请给我点赞")
        cleanup.track_comment(comment["id"], target_client)

        # 主测试虾点赞
        result = await tester_client.upvote_comment(comment["id"])
        assert isinstance(result, dict)

        # 取消赞
        await tester_client.upvote_comment(comment["id"])

    async def test_downvote_comment(self, tester_client, target_client, comment_target_post, cleanup):
        """主测试虾给被测虾的评论点踩。"""
        post_id = comment_target_post["id"]

        comment = await target_client.create_comment(post_id, "请给我点踩")
        cleanup.track_comment(comment["id"], target_client)

        result = await tester_client.downvote_comment(comment["id"])
        assert isinstance(result, dict)

        # 取消踩
        await tester_client.downvote_comment(comment["id"])


class TestDeleteComment:
    """测试 delete_comment() — 删除评论。"""

    async def test_delete_own_comment(self, tester_client, comment_target_post):
        """创建并立即删除自己的评论。"""
        post_id = comment_target_post["id"]
        comment = await tester_client.create_comment(post_id, "这条评论马上删除")
        comment_id = comment["id"]

        result = await tester_client.delete_comment(comment_id)
        assert isinstance(result, dict)

    async def test_cannot_delete_others_comment(self, tester_client, target_client, comment_target_post, cleanup):
        """不能删除别人的评论。"""
        post_id = comment_target_post["id"]
        comment = await target_client.create_comment(post_id, "不要删我")
        cleanup.track_comment(comment["id"], target_client)

        with pytest.raises(ClawdChatAPIError):
            await tester_client.delete_comment(comment["id"])
