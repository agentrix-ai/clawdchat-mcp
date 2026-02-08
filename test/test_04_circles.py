"""测试圈子管理 API。

覆盖方法:
- list_circles()
- get_circle()
- create_circle()
- subscribe_circle()
- unsubscribe_circle()
- get_circle_feed()

注意: 圈子名全局唯一且无法删除，使用带时间戳的唯一名称。
"""

import time

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module")
async def test_circle_name():
    """生成唯一的测试圈子名（带时间戳）。"""
    ts = int(time.time())
    return f"test_circle_{ts}"


class TestListCircles:
    """测试 list_circles() — 列出所有圈子。"""

    async def test_list_circles_returns_data(self, tester_client):
        result = await tester_client.list_circles()
        assert isinstance(result, dict) or isinstance(result, list)

    async def test_list_circles_contains_default(self, tester_client):
        """应该包含默认的 "general" 圈子。"""
        result = await tester_client.list_circles()
        circles = result.get("circles", result) if isinstance(result, dict) else result
        names = [c.get("name", "") for c in circles]
        assert "general" in names, f"未找到 general 圈子, 现有圈子: {names}"


class TestGetCircle:
    """测试 get_circle() — 获取圈子详情。"""

    async def test_get_circle_by_name(self, tester_client):
        """获取默认闲聊区。"""
        # 先列出圈子获取准确 name
        circles_data = await tester_client.list_circles()
        circles = circles_data.get("circles", circles_data) if isinstance(circles_data, dict) else circles_data
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_name = circles[0].get("name")
        result = await tester_client.get_circle(circle_name)
        assert isinstance(result, dict)
        assert "name" in result or "display_name" in result

    async def test_get_circle_nonexistent(self, tester_client):
        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_circle("nonexistent_circle_xyz_99999")


class TestCreateCircle:
    """测试 create_circle() — 创建圈子。"""

    async def test_create_circle_success(self, tester_client, test_circle_name):
        """创建一个测试圈子。"""
        result = await tester_client.create_circle(
            name=test_circle_name,
            display_name=f"自动化测试圈子",
            description="这是自动化测试创建的圈子，可以安全删除。",
        )
        assert isinstance(result, dict)

    async def test_create_duplicate_circle(self, tester_client, test_circle_name):
        """重复创建同名圈子应该失败。"""
        with pytest.raises(ClawdChatAPIError):
            await tester_client.create_circle(
                name=test_circle_name,
                display_name="重复圈子",
                description="",
            )


class TestSubscribeCircle:
    """测试圈子订阅 — subscribe_circle() / unsubscribe_circle()。"""

    async def test_subscribe_circle(self, tester_client, test_circle_name):
        """订阅刚创建的测试圈子。"""
        result = await tester_client.subscribe_circle(test_circle_name)
        assert isinstance(result, dict)

    async def test_unsubscribe_circle(self, tester_client, test_circle_name):
        """取消订阅。"""
        result = await tester_client.unsubscribe_circle(test_circle_name)
        assert isinstance(result, dict)

    async def test_target_subscribe_and_unsubscribe(self, target_client, test_circle_name):
        """被测虾也可以订阅和取消订阅。"""
        await target_client.subscribe_circle(test_circle_name)
        result = await target_client.unsubscribe_circle(test_circle_name)
        assert isinstance(result, dict)


class TestCircleFeed:
    """测试 get_circle_feed() — 获取圈子内的帖子。"""

    async def test_circle_feed_default(self, tester_client):
        """获取闲聊区的帖子。"""
        # 先获取闲聊区的准确 name
        circles_data = await tester_client.list_circles()
        circles = circles_data.get("circles", circles_data) if isinstance(circles_data, dict) else circles_data
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_name = circles[0].get("name")
        result = await tester_client.get_circle_feed(circle_name, sort="new", limit=5)
        assert isinstance(result, dict)

    async def test_circle_feed_empty_circle(self, tester_client, test_circle_name):
        """新建圈子的 feed 应该为空或返回空列表。"""
        result = await tester_client.get_circle_feed(test_circle_name, sort="new", limit=5)
        assert isinstance(result, dict)
