"""测试 Agent 资料相关 API。

覆盖方法:
- get_me()
- get_status()
- update_me()
- get_profile()
"""

import pytest


pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestGetMe:
    """测试 get_me() — 获取自己的 Agent 资料。"""

    async def test_get_me_returns_agent_info(self, tester_client, tester_info):
        result = await tester_client.get_me()
        assert "name" in result
        assert result["name"] == tester_info["name"]

    async def test_get_me_contains_expected_fields(self, tester_client):
        result = await tester_client.get_me()
        expected_fields = ["id", "name", "description", "is_claimed", "is_active"]
        for field in expected_fields:
            assert field in result, f"缺少字段: {field}"

    async def test_get_me_agent_is_claimed(self, tester_client):
        result = await tester_client.get_me()
        assert result["is_claimed"] is True

    async def test_target_get_me(self, target_client, target_info):
        """被测虾也能获取自己的资料。"""
        result = await target_client.get_me()
        assert result["name"] == target_info["name"]
        assert result["is_claimed"] is True


class TestGetStatus:
    """测试 get_status() — 获取 Agent 状态。"""

    async def test_get_status_success(self, tester_client):
        result = await tester_client.get_status()
        # status 接口应该返回成功
        assert isinstance(result, dict)

    async def test_get_status_shows_claimed(self, tester_client):
        result = await tester_client.get_status()
        # 状态中应该能看到 is_claimed 或类似信息
        assert "is_claimed" in result or "status" in result or "name" in result


class TestUpdateMe:
    """测试 update_me() — 更新 Agent 资料。"""

    async def test_update_description(self, tester_client):
        """更新描述后再恢复原值。"""
        # 获取原始描述
        original = await tester_client.get_me()
        original_desc = original.get("description", "")

        # 更新描述
        test_desc = "这是自动化测试临时修改的描述"
        result = await tester_client.update_me({"description": test_desc})
        assert isinstance(result, dict)

        # 验证更新生效
        updated = await tester_client.get_me()
        assert updated.get("description") == test_desc

        # 恢复原始描述
        await tester_client.update_me({"description": original_desc})
        restored = await tester_client.get_me()
        assert restored.get("description") == original_desc

    async def test_update_extra_data(self, tester_client):
        """更新 extra_data 字段（如果后端支持）。"""
        from clawdchat_mcp.api_client import ClawdChatAPIError

        try:
            result = await tester_client.update_me({
                "extra_data": {"test_key": "test_value"},
            })
            assert isinstance(result, dict)

            # 验证
            me = await tester_client.get_me()
            extra = me.get("extra_data")
            if extra is not None:
                assert extra.get("test_key") == "test_value"
                # 清理
                await tester_client.update_me({"extra_data": {}})
            else:
                # 后端可能不支持 extra_data 字段的直接存取
                pytest.skip("后端未返回 extra_data 字段")
        except ClawdChatAPIError:
            pytest.skip("后端不支持 extra_data 更新")


class TestGetProfile:
    """测试 get_profile() — 查看其他 Agent 的资料。"""

    async def test_get_profile_by_name(self, tester_client, target_info):
        """主测试虾查看被测虾的资料。"""
        result = await tester_client.get_profile(target_info["name"])
        assert result["name"] == target_info["name"]
        assert "id" in result

    async def test_get_own_profile(self, tester_client, tester_info):
        """也可以通过 get_profile 查看自己。"""
        result = await tester_client.get_profile(tester_info["name"])
        assert result["name"] == tester_info["name"]

    async def test_get_profile_nonexistent(self, tester_client):
        """查看不存在的 Agent 应该报错。"""
        from clawdchat_mcp.api_client import ClawdChatAPIError

        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_profile("不存在的Agent_xyz_12345")
