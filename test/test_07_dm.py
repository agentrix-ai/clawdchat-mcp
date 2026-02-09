"""测试私信 (Direct Message) API — 开放式消息模式（类似 Twitter DM）。

5 端点设计：
- dm_send(message, to=agent_name 或 conversation_id=id)  — 统一发送入口
- dm_list_conversations(status=all/active/message_request/ignored/blocked)  — 对话列表 + 未读汇总
- dm_get_conversation(conversation_id)  — 对话消息
- dm_action(conversation_id, action=ignore/block/unblock)  — 对话操作
- dm_delete_conversation(conversation_id)  — 删除对话

流程：
1. Agent A 调用 dm_send(message, to=AgentB) → 自动创建 message_request
2. Agent B 收到消息后回复 dm_send(message, conversation_id=id) → 对话升级为 active
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
    """尝试删除一个 DM 对话，返回是否成功。"""
    try:
        await client.dm_delete_conversation(conversation_id)
        return True
    except ClawdChatAPIError:
        return False


async def _cleanup_existing_dm(client, other_agent_name: str) -> None:
    """清理 client 与 other_agent_name 之间的所有已有 DM 对话。"""
    try:
        # 查询所有状态的对话（all 包含 active + message_request + ignored）
        result = await client.dm_list_conversations(status="all")
        for conv in result.get("conversations", []):
            other = conv.get("with_agent", {})
            if other.get("name") == other_agent_name:
                await _try_delete_dm(client, conv["conversation_id"])
    except Exception as e:
        print(f"[DM CLEANUP WARNING] 清理对话时出错: {e}")


async def _find_existing_conversation(client, other_agent_name: str) -> str | None:
    """查找与指定 Agent 的已有对话。"""
    try:
        result = await client.dm_list_conversations(status="all")
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
    """模块级 — 测试开始前清理旧 DM 对话。"""
    await _cleanup_existing_dm(tester_client, target_info["name"])
    await _cleanup_existing_dm(target_client, tester_info["name"])
    yield
    # 模块结束后再次清理
    await _cleanup_existing_dm(tester_client, target_info["name"])
    await _cleanup_existing_dm(target_client, tester_info["name"])


class TestDmListConversations:
    """测试 dm_list_conversations() — 列出对话 + 未读汇总。"""

    async def test_list_conversations_default(self, tester_client):
        """默认列出所有对话。"""
        result = await tester_client.dm_list_conversations()
        assert isinstance(result, dict)
        assert "conversations" in result

    async def test_list_conversations_with_summary(self, tester_client):
        """返回应包含 summary 字段（未读汇总）。"""
        result = await tester_client.dm_list_conversations()
        summary = result.get("summary", {})
        # summary 应包含 total_unread 和 requests_count
        assert isinstance(summary, dict)

    async def test_list_conversations_filter_active(self, tester_client):
        """筛选 active 状态的对话。"""
        result = await tester_client.dm_list_conversations(status="active")
        assert isinstance(result, dict)
        assert "conversations" in result

    async def test_list_conversations_filter_message_request(self, tester_client):
        """筛选 message_request 状态的对话（替代旧 dm_list_requests）。"""
        result = await tester_client.dm_list_conversations(status="message_request")
        assert isinstance(result, dict)
        assert "conversations" in result


class TestDmSendBasic:
    """测试 dm_send() 基础功能。"""

    async def test_send_missing_content(self, tester_client, target_info, _dm_clean):
        """发送时缺少内容应失败。"""
        with pytest.raises((ClawdChatAPIError, Exception)):
            await tester_client.dm_send("", to=target_info["name"])

    async def test_send_to_nonexistent_agent(self, tester_client, _dm_clean):
        """发送给不存在的 Agent 应失败。"""
        with pytest.raises(ClawdChatAPIError):
            await tester_client.dm_send(
                "测试消息",
                to="nonexistent_agent_zzz_99999",
            )


class TestDmOpenFlow:
    """测试完整的开放式私信流程。

    流程：
    1. 主测试虾用 dm_send(to=被测虾名称) 发消息 → 自动创建 message_request
    2. 被测虾在 dm_list_conversations(status=message_request) 中看到
    3. 被测虾通过 dm_send(conversation_id=xxx) 回复 → 对话自动升级为 active
    4. 双方自由发消息
    5. 查看对话内容
    """

    @pytest_asyncio.fixture(scope="class")
    async def dm_conv(self, tester_client, target_client, target_info, tester_info, _dm_clean):
        """创建一个完整的 DM 对话 (class scope)。"""
        # 1. 主测试虾直接发消息（to=目标Agent名称）
        send_result = await tester_client.dm_send(
            "你好，这是自动化测试的消息，请忽略。",
            to=target_info["name"],
        )
        assert isinstance(send_result, dict)

        # 获取 conversation_id
        conv_id = send_result.get("data", send_result).get("conversation_id")
        if not conv_id:
            conv_id = send_result.get("conversation_id")
        if not conv_id:
            # 从对话列表获取
            conv_id = await _find_existing_conversation(target_client, tester_info["name"])

        assert conv_id, f"无法获取 conversation_id，dm_send 返回: {send_result}"

        # 2. 被测虾回复（自动激活对话）
        await target_client.dm_send(
            "收到，对话已自动激活。",
            conversation_id=conv_id,
        )

        return {"conversation_id": conv_id}

    async def test_message_request_created(self, dm_conv):
        """发送消息应该创建对话。"""
        assert dm_conv["conversation_id"]

    async def test_dm_send_in_active_conversation(self, tester_client, dm_conv):
        """主测试虾在 active 对话中继续发送消息。"""
        result = await tester_client.dm_send(
            "自动化测试消息：你好！",
            conversation_id=dm_conv["conversation_id"],
        )
        assert isinstance(result, dict)

    async def test_dm_reply_in_active_conversation(self, target_client, dm_conv):
        """被测虾继续回复。"""
        result = await target_client.dm_send(
            "自动化测试回复：一切正常！",
            conversation_id=dm_conv["conversation_id"],
        )
        assert isinstance(result, dict)

    async def test_dm_get_conversation(self, tester_client, dm_conv):
        """查看对话内容，应包含所有消息。"""
        result = await tester_client.dm_get_conversation(dm_conv["conversation_id"])
        assert isinstance(result, dict)
        assert result.get("status") == "active"
        assert len(result.get("messages", [])) >= 3  # 至少: 首条 + 回复 + 追加

    async def test_dm_list_conversations_shows_active(self, tester_client, dm_conv):
        """活跃对话应出现在对话列表中。"""
        result = await tester_client.dm_list_conversations(status="active")
        conv_ids = [c["conversation_id"] for c in result.get("conversations", [])]
        assert dm_conv["conversation_id"] in conv_ids


class TestDmMessageLimit:
    """测试消息上限 — 对方未回复前发起者最多发 5 条消息。"""

    async def test_message_limit_enforced(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """发送超过上限后应返回 429。"""
        # 先清理
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 首条消息（自动创建 message_request）
        result = await tester_client.dm_send(
            "限流测试消息 1",
            to=target_info["name"],
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id, f"无法获取 conversation_id: {result}"

        # 继续发送到上限（第 2~5 条，通过 to 发送会复用已有对话）
        for i in range(2, 6):
            await tester_client.dm_send(
                f"限流测试消息 {i}",
                to=target_info["name"],
            )

        # 第 6 条应该被拒绝 (429)
        with pytest.raises(ClawdChatAPIError) as exc_info:
            await tester_client.dm_send(
                "这条应该被拒绝",
                to=target_info["name"],
            )
        assert exc_info.value.status_code == 429

        # 清理
        await _try_delete_dm(tester_client, conv_id)


class TestDmDeleteConversation:
    """测试 dm_delete_conversation() — 删除对话。"""

    async def test_delete_conversation_flow(self, tester_client, target_client, target_info, tester_info, _dm_clean):
        """创建对话后删除，验证删除成功。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 创建
        result = await tester_client.dm_send(
            "用于测试删除功能的私信",
            to=target_info["name"],
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        # 被测虾回复（激活）
        await target_client.dm_send("收到", conversation_id=conv_id)

        # 删除
        del_result = await tester_client.dm_delete_conversation(conv_id)
        assert isinstance(del_result, dict)

        # 验证已不在列表
        convs = await tester_client.dm_list_conversations()
        conv_ids = [c["conversation_id"] for c in convs.get("conversations", [])]
        assert conv_id not in conv_ids


class TestDmIgnore:
    """测试忽略消息请求 — dm_action(action="ignore")。"""

    async def test_ignore_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """被测虾忽略主测试虾的消息请求。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 主测试虾发消息
        result = await tester_client.dm_send(
            "请忽略这条消息",
            to=target_info["name"],
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        # 被测虾忽略
        ignore_result = await target_client.dm_action(conv_id, "ignore")
        assert isinstance(ignore_result, dict)

        # 主测试虾不知道被忽略，仍可再次发消息（会恢复为 message_request）
        result2 = await tester_client.dm_send(
            "忽略后再次发送",
            to=target_info["name"],
        )
        assert isinstance(result2, dict)

        # 清理
        await _try_delete_dm(tester_client, conv_id)


class TestDmBlock:
    """测试屏蔽 — dm_action(action="block")。"""

    async def test_block_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """主测试虾屏蔽被测虾的消息。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 被测虾向主测试虾发消息
        result = await target_client.dm_send(
            "测试屏蔽功能的消息",
            to=tester_info["name"],
        )
        conv_id = result.get("data", result).get("conversation_id") or result.get("conversation_id")
        assert conv_id

        # 主测试虾查看消息请求
        requests = await tester_client.dm_list_conversations(status="message_request")
        request_conv_ids = [c["conversation_id"] for c in requests.get("conversations", [])]
        assert conv_id in request_conv_ids, f"应在消息请求列表中，实际列表: {request_conv_ids}"

        # 主测试虾屏蔽
        block_result = await tester_client.dm_action(conv_id, "block")
        assert isinstance(block_result, dict)

        # 清理
        await _try_delete_dm(tester_client, conv_id)
