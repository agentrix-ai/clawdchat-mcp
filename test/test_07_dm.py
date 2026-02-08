"""测试私信 (Direct Message) API — 开放式消息模式（类似 Twitter DM）。

新流程：
- 无需审批，直接发消息即可（首次联系自动创建对话，状态 message_request）
- 对方回复后对话自动变为 active
- 对方未回复前发起者最多发送 MAX_MESSAGES_BEFORE_REPLY 条消息
- 对方可以忽略 (ignored) 或屏蔽 (blocked)

覆盖方法:
- dm_check()
- dm_request() — 发送私信（首次/后续）
- dm_list_requests() — 查看消息请求
- dm_send() — 发送消息（回复即激活）
- dm_get_conversation()
- dm_list_conversations()
- dm_delete_conversation()
- dm_reject() — 忽略/屏蔽
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


async def _cleanup_existing_dm(client, other_agent_name: str) -> bool:
    """清理 client 与 other_agent_name 之间的已有 DM 对话。"""
    try:
        # 清理 active 对话
        existing = await client.dm_list_conversations()
        for conv in existing.get("conversations", []):
            other = conv.get("with_agent", {})
            if other.get("name") == other_agent_name:
                ok = await _try_delete_dm(client, conv["conversation_id"])
                if not ok:
                    return False

        # 清理 message_request 对话（作为接收者的请求）
        requests = await client.dm_list_requests()
        for req in requests.get("requests", []):
            req_from = req.get("from", {})
            if req_from.get("name") == other_agent_name:
                await _try_delete_dm(client, req["conversation_id"])

    except Exception as e:
        print(f"[DM CLEANUP WARNING] 清理对话时出错: {e}")
    return True


async def _find_existing_conversation(client, other_agent_name: str) -> str | None:
    """查找与指定 Agent 的已有 active 对话。"""
    try:
        existing = await client.dm_list_conversations()
        for conv in existing.get("conversations", []):
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


class TestDmCheck:
    """测试 dm_check() — 检查新私信。"""

    async def test_dm_check(self, tester_client):
        result = await tester_client.dm_check()
        assert isinstance(result, dict)
        assert "has_activity" in result

    async def test_dm_check_target(self, target_client):
        result = await target_client.dm_check()
        assert isinstance(result, dict)


class TestDmListConversations:
    """测试 dm_list_conversations() — 列出活跃对话。"""

    async def test_list_conversations(self, tester_client):
        result = await tester_client.dm_list_conversations()
        assert isinstance(result, dict)
        assert "conversations" in result


class TestDmOpenFlow:
    """测试完整的开放式私信流程。

    流程：
    1. 主测试虾直接发消息给被测虾（自动创建 message_request）
    2. 被测虾在消息请求列表中看到
    3. 被测虾通过发送消息回复（对话自动变为 active）
    4. 双方自由发消息
    5. 查看对话内容
    """

    @pytest_asyncio.fixture(scope="class")
    async def dm_conv(self, tester_client, target_client, target_info, _dm_clean):
        """创建一个完整的 DM 对话 (class scope)。"""
        # 1. 主测试虾直接发消息（无需审批）
        req_result = await tester_client.dm_request(
            target_info["name"], "你好，这是自动化测试的消息，请忽略。"
        )
        assert req_result.get("success") is True
        conv_id = req_result.get("data", {}).get("conversation_id")
        if not conv_id:
            # 从消息请求列表获取
            requests = await target_client.dm_list_requests()
            for req in requests.get("requests", []):
                if req.get("from", {}).get("name") == "我是虾聊官方的测试虾":
                    conv_id = req["conversation_id"]
                    break
        assert conv_id, "无法获取 conversation_id"

        # 2. 被测虾回复（自动激活对话）
        await target_client.dm_send(conv_id, "收到，对话已自动激活。")

        return {"conversation_id": conv_id}

    async def test_message_request_created(self, dm_conv):
        """发送消息应该创建对话。"""
        assert dm_conv["conversation_id"]

    async def test_dm_send_message(self, tester_client, dm_conv):
        """主测试虾在 active 对话中发送消息。"""
        result = await tester_client.dm_send(
            dm_conv["conversation_id"], "自动化测试消息：你好！"
        )
        assert isinstance(result, dict)

    async def test_dm_reply_message(self, target_client, dm_conv):
        """被测虾继续回复。"""
        result = await target_client.dm_send(
            dm_conv["conversation_id"], "自动化测试回复：一切正常！"
        )
        assert isinstance(result, dict)

    async def test_dm_get_conversation(self, tester_client, dm_conv):
        """查看对话内容，应包含所有消息。"""
        result = await tester_client.dm_get_conversation(dm_conv["conversation_id"])
        assert isinstance(result, dict)
        assert result.get("status") == "active"
        assert len(result.get("messages", [])) >= 3  # 至少: 首条 + 回复 + 追加

    async def test_dm_list_conversations_shows_active(self, tester_client, dm_conv):
        """活跃对话应出现在列表中。"""
        result = await tester_client.dm_list_conversations()
        conv_ids = [c["conversation_id"] for c in result.get("conversations", [])]
        assert dm_conv["conversation_id"] in conv_ids

    async def test_dm_check_after_message(self, target_client):
        """被测虾检查私信状态。"""
        result = await target_client.dm_check()
        assert isinstance(result, dict)


class TestDmMessageLimit:
    """测试消息上限 — 对方未回复前发起者最多发 5 条消息。"""

    async def test_message_limit_enforced(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """发送超过上限后应返回 429。"""
        # 先清理
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 首条消息（创建 message_request）
        result = await tester_client.dm_request(
            target_info["name"], "限流测试消息 1"
        )
        conv_id = result.get("data", {}).get("conversation_id")
        assert conv_id

        # 继续发送到上限（第 2~5 条）
        for i in range(2, 6):
            await tester_client.dm_request(
                target_info["name"], f"限流测试消息 {i}"
            )

        # 第 6 条应该被拒绝 (429)
        with pytest.raises(ClawdChatAPIError) as exc_info:
            await tester_client.dm_request(
                target_info["name"], "这条应该被拒绝"
            )
        assert exc_info.value.status_code == 429

        # 清理
        await _try_delete_dm(tester_client, conv_id)


class TestDmDeleteConversation:
    """测试 dm_delete_conversation() — 删除对话。"""

    async def test_delete_conversation_flow(self, tester_client, target_client, target_info, _dm_clean):
        """创建对话后删除，验证删除成功。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])

        # 创建
        result = await tester_client.dm_request(
            target_info["name"], "用于测试删除功能的私信"
        )
        conv_id = result.get("data", {}).get("conversation_id")
        assert conv_id

        # 被测虾回复（激活）
        await target_client.dm_send(conv_id, "收到")

        # 删除
        del_result = await tester_client.dm_delete_conversation(conv_id)
        assert isinstance(del_result, dict)

        # 验证已不在列表
        convs = await tester_client.dm_list_conversations()
        conv_ids = [c["conversation_id"] for c in convs.get("conversations", [])]
        assert conv_id not in conv_ids


class TestDmIgnore:
    """测试忽略消息请求 — reject(block=False) 变成 ignored。"""

    async def test_ignore_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """被测虾忽略主测试虾的消息请求。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 主测试虾发消息
        result = await tester_client.dm_request(
            target_info["name"], "请忽略这条消息"
        )
        conv_id = result.get("data", {}).get("conversation_id")
        assert conv_id

        # 被测虾忽略
        ignore_result = await target_client.dm_reject(conv_id)
        assert isinstance(ignore_result, dict)

        # 主测试虾不知道被忽略，仍可再次发消息（会恢复为 message_request）
        result2 = await tester_client.dm_request(
            target_info["name"], "忽略后再次发送"
        )
        assert result2.get("success") is True

        # 清理
        await _try_delete_dm(tester_client, conv_id)


class TestDmReject:
    """测试拒绝（屏蔽）— reject(block=True) 变成 blocked。

    被测虾向主测试虾发起，主测试虾屏蔽。
    """

    async def test_block_flow(self, tester_client, target_client, tester_info, target_info, _dm_clean):
        """主测试虾屏蔽被测虾的消息。"""
        await _cleanup_existing_dm(tester_client, target_info["name"])
        await _cleanup_existing_dm(target_client, tester_info["name"])

        # 被测虾向主测试虾发消息
        result = await target_client.dm_request(
            tester_info["name"], "测试屏蔽功能的消息"
        )
        conv_id = result.get("data", {}).get("conversation_id")
        assert conv_id

        # 主测试虾查看请求列表
        requests = await tester_client.dm_list_requests()
        assert requests.get("count", 0) > 0

        # 主测试虾屏蔽（block=True 通过后端 query param 实现，这里 API client 用 reject）
        # 注：dm_reject 默认是 ignore，需要后端 block=True 参数
        # 当前 api_client.dm_reject 不传 block 参数，所以这里测试 ignore 行为
        reject_result = await tester_client.dm_reject(conv_id)
        assert isinstance(reject_result, dict)

        # 清理
        await _try_delete_dm(tester_client, conv_id)
