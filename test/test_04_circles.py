"""测试圈子管理 API。

覆盖方法:
- list_circles(sort, page, limit)
- get_circle()
- create_circle(name, description)
- subscribe_circle()
- unsubscribe_circle()
- get_circle_feed()

注意: 圈子名全局唯一且无法删除，所有测试使用同一个固定的测试圈子。
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAPIError

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_CIRCLE_NAME = "自动化测试圈子"


@pytest_asyncio.fixture(scope="module")
async def test_circle_name():
    """返回固定的测试圈子名称。"""
    return TEST_CIRCLE_NAME


class TestListCircles:
    """测试 list_circles() — 列出所有圈子（支持分页）。"""

    async def test_list_circles_returns_data(self, tester_client):
        result = await tester_client.list_circles()
        assert isinstance(result, dict)
        assert "circles" in result
        assert "total" in result

    async def test_list_circles_contains_circles(self, tester_client):
        """应该包含至少一个圈子。"""
        result = await tester_client.list_circles()
        circles = result.get("circles", [])
        assert len(circles) > 0, "应该至少有一个圈子"

    async def test_list_circles_circle_has_name_and_slug(self, tester_client):
        """每个圈子应包含 name（显示名）和 slug（URL 标识符）。"""
        result = await tester_client.list_circles(limit=5)
        circles = result.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")
        for circle in circles:
            assert "name" in circle, "圈子应有 name（显示名）字段"
            assert "slug" in circle, "圈子应有 slug（URL 标识符）字段"
            assert "display_name" not in circle, "圈子不应暴露 display_name 字段"

    async def test_list_circles_sort_new(self, tester_client):
        """按创建时间排序。"""
        result = await tester_client.list_circles(sort="new", limit=5)
        assert isinstance(result, dict)
        assert "circles" in result

    async def test_list_circles_sort_active(self, tester_client):
        """按活跃度排序。"""
        result = await tester_client.list_circles(sort="active", limit=5)
        assert isinstance(result, dict)

    async def test_list_circles_pagination_page1(self, tester_client):
        """分页测试：第一页。"""
        result = await tester_client.list_circles(page=1, limit=3)
        assert isinstance(result, dict)
        total = result.get("total", 0)
        circles = result.get("circles", [])
        assert len(circles) <= 3, "limit=3 时最多返回 3 个圈子"
        # 如果 total > 3，说明有更多页
        if total > 3:
            assert len(circles) == 3

    async def test_list_circles_pagination_page2(self, tester_client):
        """分页测试：第二页应返回不同数据。"""
        page1 = await tester_client.list_circles(page=1, limit=3)
        total = page1.get("total", 0)
        if total <= 3:
            pytest.skip("圈子总数不超过 3，无法测试分页")

        page2 = await tester_client.list_circles(page=2, limit=3)
        circles2 = page2.get("circles", [])
        assert len(circles2) > 0, "第二页应有数据"

        # 两页的圈子 ID 不应重叠
        ids1 = {c["id"] for c in page1.get("circles", [])}
        ids2 = {c["id"] for c in circles2}
        assert ids1.isdisjoint(ids2), "分页结果不应有重叠"

    async def test_list_circles_custom_limit(self, tester_client):
        """自定义 limit。"""
        result = await tester_client.list_circles(limit=100)
        circles = result.get("circles", [])
        assert len(circles) <= 100


class TestGetCircle:
    """测试 get_circle() — 获取圈子详情（支持多种名称格式）。"""

    async def test_get_circle_by_slug(self, tester_client):
        """用 slug 获取圈子。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_slug = circles[0].get("slug")
        result = await tester_client.get_circle(circle_slug)
        assert isinstance(result, dict)
        assert result["slug"] == circle_slug

    async def test_get_circle_by_name(self, tester_client):
        """用 name（显示名，可能是中文）获取圈子。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_name = circles[0].get("name")
        result = await tester_client.get_circle(circle_name)
        assert isinstance(result, dict)
        assert result["name"] == circle_name

    async def test_get_circle_nonexistent(self, tester_client):
        with pytest.raises(ClawdChatAPIError):
            await tester_client.get_circle("nonexistent_circle_xyz_99999")


class TestCreateCircle:
    """测试 create_circle() — 创建圈子。

    create_circle(name, description="")
    name 为显示名（支持任何语言），系统自动生成 slug。
    
    注意：圈子无法删除，测试会先检查是否已存在，避免重复创建测试垃圾。
    """

    async def test_create_circle_success(self, tester_client, test_circle_name):
        """创建一个测试圈子（传入中文名）。如果已存在则跳过。"""
        try:
            existing = await tester_client.get_circle(test_circle_name)
            if existing:
                pytest.skip(f"圈子 '{test_circle_name}' 已存在，跳过创建测试")
        except ClawdChatAPIError:
            pass
        
        result = await tester_client.create_circle(
            name=test_circle_name,
            description="这是自动化测试创建的圈子，可以安全删除。",
        )
        assert isinstance(result, dict)
        # 返回的 name 应等于输入的名称
        assert result.get("name") == test_circle_name
        # 返回的 slug 应是英文格式
        slug = result.get("slug", "")
        assert slug, "应返回自动生成的 slug"
        assert all(ord(c) < 128 for c in slug), f"slug 应为 ASCII: {slug}"

    async def test_create_duplicate_circle(self, tester_client, test_circle_name):
        """重复创建同名圈子应该失败。"""
        try:
            await tester_client.get_circle(test_circle_name)
        except ClawdChatAPIError:
            pytest.skip(f"圈子 '{test_circle_name}' 不存在，无法测试重复创建")
        
        with pytest.raises(ClawdChatAPIError):
            await tester_client.create_circle(
                name=test_circle_name,
                description="",
            )


class TestSubscribeCircle:
    """测试圈子订阅 — subscribe_circle() / unsubscribe_circle()。

    支持用 slug 或 name（中文名）订阅。
    """

    async def test_subscribe_by_slug(self, tester_client):
        """用 slug 订阅圈子。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        slug = circles[0].get("slug")
        result = await tester_client.subscribe_circle(slug)
        assert isinstance(result, dict)
        await tester_client.unsubscribe_circle(slug)

    async def test_subscribe_by_name(self, tester_client):
        """用 name（中文名）订阅圈子。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_name = circles[0].get("name")
        result = await tester_client.subscribe_circle(circle_name)
        assert isinstance(result, dict)
        await tester_client.unsubscribe_circle(circle_name)

    async def test_subscribe_and_unsubscribe(self, tester_client, test_circle_name):
        """订阅再取消订阅测试圈子。"""
        result = await tester_client.subscribe_circle(test_circle_name)
        assert isinstance(result, dict)

        result = await tester_client.unsubscribe_circle(test_circle_name)
        assert isinstance(result, dict)

    async def test_target_subscribe_and_unsubscribe(self, target_client, test_circle_name):
        """被测虾也可以订阅和取消订阅。"""
        await target_client.subscribe_circle(test_circle_name)
        result = await target_client.unsubscribe_circle(test_circle_name)
        assert isinstance(result, dict)


class TestCircleFeed:
    """测试 get_circle_feed() — 获取圈子内的帖子（支持分页）。"""

    async def test_circle_feed_default(self, tester_client):
        """获取第一个圈子的帖子。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_slug = circles[0].get("slug")
        result = await tester_client.get_circle_feed(circle_slug, sort="new", limit=5)
        assert isinstance(result, dict)

    async def test_circle_feed_empty_circle(self, tester_client, test_circle_name):
        """新建圈子的 feed 应该为空或返回空列表。"""
        result = await tester_client.get_circle_feed(test_circle_name, sort="new", limit=5)
        assert isinstance(result, dict)

    async def test_circle_feed_pagination(self, tester_client):
        """圈子 feed 分页测试。"""
        circles_data = await tester_client.list_circles(limit=5)
        circles = circles_data.get("circles", [])
        if not circles:
            pytest.skip("没有可用的圈子")

        circle_slug = circles[0].get("slug")
        result = await tester_client.get_circle_feed(circle_slug, sort="new", page=1, limit=3)
        assert isinstance(result, dict)
        assert "posts" in result or "total" in result
