"""测试 A2A 统一消息 API（站内私信 + 外部 A2A 消息）。

A2A 端点（/a2a/...）:
- a2a_send(agent_name, message)  — 按名称发送消息
- a2a_send_to_conversation(conversation_id, message)  — 按对话 ID 发送
- a2a_inbox(unread_only)  — 统一收件箱（DM + 外部 relay）
- a2a_list_conversations(status)  — 对话列表 + 未读汇总
- a2a_get_conversation(conversation_id)  — 对话消息历史
- a2a_action(conversation_id, action)  — 对话操作（ignore/block/unblock）
- a2a_delete_conversation(conversation_id)  — 删除对话

Legacy DM aliases (dm_send, dm_list_conversations, etc.) 保留向后兼容。

流程：
1. Agent A 调用 a2a_send(AgentB名, message) → 自动创建 message_request
2. Agent B 收到消息后回复 a2a_send_to_conversation(conv_id, message) → 对话升级为 active
3. 对方未回复前发起者最多发送 5 条消息
4. 接收者可 ignore/block
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# 清理辅助
# ---------------------------------------------------------------------------

async def _try_delete_dm(client, conversation_id: str) -> bool:
    try:
        await client.a2a_delete_conversation(conversation_id)
        return True
    except ClawdChatAPIError:
        return False


async def _cleanup_existing_dm(client, other_agent_name: str) -> None:
    try:
        result = await client.a2a_list_conversations(status="all")
        for conv in result.get("conversations", []):
            other = conv.get("with_agent", {})
            if other.get("name") == other_agent_name:
                await _try_delete_dm(client, conv["conversation_id"])
    except Exception as e:
        print(f"[DM CLEANUP WARNING] 清理对话时出错: {e}")


async def _find_existing_conversation(client, other_agent_name: str) -> str | None:
    try:
        result = await client.a2a_list_conversations(status="all")
        for conv in result.get("conversations", []):
            other = conv.get("with_agent", {})
            if other.get("name") == other_agent_name:
                return conv["conversation_id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 模块级 fixture：清理双方已有对话
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def _dm_clean(tester_client, target_client, tester_info, target_info):
    await _cleanup_existing_dm(tester_client, target_info["name"])
    await _cleanup_existing_dm(target_client, tester_info["name"])
    yield
    await _cleanup_existing_dm(tester_client, target_info["name"])
    await _cleanup_existing_dm(target_client, tester_info["name"])


class TestA2aListConversations:
    """测试 a2a_list_conversations() — 列出对话 + 未读汇总。"""

    async def test_list_conversations_default(self, tester_client):
        result = await tester_client.a2a_list_conversations()
        assert isinstance(result, dict)
        assert "conversations" in result

    async def test_list_conversations_with_summary(self, tester_client):
        result = await tester_client.a2a_list_conversations()
        summary = result.get("summary", {})
        assert isinstance(summary, dict)

    async def test_list_conversations_filter_active(self, tester_client):
        result = await tester_client.a2a_list_conversations(status="active")
        assert isinstance(result, dict)
        assert "conversations" in result

    async def test_list_conversations_filter_message_request(self, tester_client):
        result = await tester_client.a2a_list_conversations(status="message_request")
        assert isinstance(result, dict)
        assert "conversations" in result


class TestA2aInbox:
    """测试 a2a_inbox() — 统一收件箱。"""

    async def test_inbox_unread_only(self, tester_client):
        """获取未读消息。"""
        result = await tester_client.a2a_inbox(unread_only=True)
        assert isinstance(result, dict)
        assert "messages" in result

    async def test_inbox_all(self, tester_client):
        """获取所有消息。"""
        result = await tester_client.a2a_inbox(unread_only=False)
        assert isinstance(result, dict)
        assert "messages" in result


class TestA2aSendBasic:
    """测试 a2a_send() 基础功能。"""

    async def test_send_missing_content(self, tester_client, target_info, _dm_clean):
        with pytest.raises((ClawdChatAPIError, Exception)):
            await tester_client.a2a_send(target_info["name"], "")

    async def test_send_to_nonexistent_agent(self, tester_client, _dm_clean):
        with pytest.raises(ClawdChatAPIError):
            await tester_client.a2a_send(
                "nonexistent_agent_zzz_99999",
                "测试消息",
            )


class TestA2aOpenFlow:
    """测试完整的开放式消息流程。

    1. 主测试虾用 a2a_send(目标名称, 消息) 发消息 → 自动创建 message_request
    2. 被测虾在 a2a_list_conversations(status=message_request) 中看到
    3. 被测虾通过 a2a_send_to_conversation(conv_id, 消息) 回复 → 对话自动升级为 active
    4. 双方自由发消息
    5. 查看对话内容
    """

    @pytest_asyncio.fixture(scope="class")
    async def dm_conv(self, tester_client, target_client, target_info, tester_info, _dm_clean):
        # 1. 主测试虾直接发消息
        send_result = await tester_client.a2a_send(
            target_info["name"],
            "你好，这是自动化测试的消息，请忽略。",
        )
        assert isinstance(send_result, dict)

        conv_id = send_result.get("data", send_result).get("conversation_id")
        if not conv_id:
            conv_id = send_result.get("conversation_id")
        if not conv_id:
            conv_id = await _find_existing_conversation(target_client, tester_info["name"])

        assert conv_id, f"无法获取 conversation_id，a2a_send 返回: {send_result}"

        # 2. 被测虾回复（自动激活对话）
        await target_client.a2a_send_to_conversation(conv_id, "收到，对话已自动激活。")

        return {"conversation_id": conv_id}

    async def test_message_request_created(self, dm_conv):
        assert dm_conv["conversation_id"]

    async def test_send_in_active_conversation(self, tester_client, dm_conv):
        """主测试虾在 active 对话中继续发送消息。"""
        result = await tester_client.a2a_send_to_conversation(
            dm_conv["conversation_id"],
            "自动化测试消息：你好！",
        )
        assert isinstance(result, dict)

    async def test_reply_in_active_conversation(self, target_client, dm_conv):
        """被测虾继续回复。"""
        result = await target_client.a2a_send_to_conversation(
            dm_conv["conversation_id"],
            "自动化测试回复：一切正常！",
        )
        assert isinstance(result, dict)

    async def test_get_conversation(self, tester_client, dm_conv):
        """查看对话内容，应包含所有消息。"""
        result = await tester_client.a2a_get_conversation(dm_conv["conversation_id"])
        assert isinstance(result, dict)
        assert result.get("status") == "active"
        assert len(result.get("messages", [])) >= 3

    async def test_list_conversations_shows_active(self, tester_client, dm_conv):
        """活跃对话应出现在对话列表中。"""
        result = await tester_client.a2a_list_conversations(status="active")
        conv_ids = [c["conversation_id"] for c in result.get("conversations", [])]
        assert dm_conv["conversation_id"] in conv_ids

    async def test_inbox_shows_messages(self, target_client, dm_conv):
        """收件箱中应有消息（可能已被标记为已读）。"""
        result = await target_client.a2a_inbox(unread_only=False)
        assert isinstance(result, dict)
        assert "messages" in result


class TestA2aMessageLimit:
    """测试消息上限 — 对方未回复前发起者最多发 5 条消息。"""

    async def test_message_limit_enforced(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        result = await tester_client.a2a_send(
            target_info["name"],
            "限流测试消息 1",
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id, f"无法获取 conversation_id: {result}"

        for i in range(2, 6):
            await tester_client.a2a_send(
                target_info["name"],
                f"限流测试消息 {i}",
            )

        with pytest.raises(ClawdChatAPIError) as exc_info:
            await tester_client.a2a_send(
                target_info["name"],
                "这条应该被拒绝",
            )
        assert exc_info.value.status_code == 429

        await _try_delete_dm(tester_client, conv_id)


class TestA2aDeleteConversation:
    """测试 a2a_delete_conversation() — 删除对话。"""

    async def test_delete_conversation_flow(self, tester_client, target_client, target_info, tester_info, _dm_clean):
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        result = await tester_client.a2a_send(
            target_info["name"],
            "用于测试删除功能的消息",
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        await target_client.a2a_send_to_conversation(conv_id, "收到")

        del_result = await tester_client.a2a_delete_conversation(conv_id)
        assert isinstance(del_result, dict)

        convs = await tester_client.a2a_list_conversations()
        conv_ids = [c["conversation_id"] for c in convs.get("conversations", [])]
        assert conv_id not in conv_ids


class TestA2aIgnore:
    """测试忽略消息请求 — a2a_action(action="ignore")。"""

    async def test_ignore_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        result = await tester_client.a2a_send(
            target_info["name"],
            "请忽略这条消息",
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        ignore_result = await target_client.a2a_action(conv_id, "ignore")
        assert isinstance(ignore_result, dict)

        result2 = await tester_client.a2a_send(
            target_info["name"],
            "忽略后再次发送",
        )
        assert isinstance(result2, dict)

        await _try_delete_dm(tester_client, conv_id)


class TestA2aBlock:
    """测试屏蔽 — a2a_action(action="block")。"""

    async def test_block_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        result = await target_client.a2a_send(
            tester_info["name"],
            "测试屏蔽功能的消息",
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        requests = await tester_client.a2a_list_conversations(status="message_request")
        request_conv_ids = [c["conversation_id"] for c in requests.get("conversations", [])]
        assert conv_id in request_conv_ids, f"应在消息请求列表中，实际列表: {request_conv_ids}"

        block_result = await tester_client.a2a_action(conv_id, "block")
        assert isinstance(block_result, dict)

        await _try_delete_dm(tester_client, conv_id)


class TestLegacyDmAliases:
    """测试 legacy DM 别名方法仍然可用（向后兼容）。"""

    async def test_dm_list_conversations(self, tester_client):
        """dm_list_conversations 应委托到 a2a_list_conversations。"""
        result = await tester_client.dm_list_conversations()
        assert isinstance(result, dict)
        assert "conversations" in result

    async def test_dm_send_by_name(self, tester_client, target_client, target_info, tester_info, _dm_clean):
        """dm_send(to=name) 应委托到 a2a_send。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        result = await tester_client.dm_send(
            "legacy DM 兼容测试",
            to=target_info["name"],
        )
        assert isinstance(result, dict)
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        if conv_id:
            await _try_delete_dm(tester_client, conv_id)
