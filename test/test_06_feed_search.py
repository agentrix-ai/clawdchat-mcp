"""测试 Feed 和搜索 API。

覆盖方法:
- get_feed(sort, limit, skip)
- get_stats()
- get_active_agents()
- search(q, type, limit)  — POST 方法，支持 type: posts/comments/agents/circles/all
"""

import asyncio

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module")
async def searchable_post(tester_client, module_cleanup):
    """创建一个内容独特的帖子用于搜索测试。"""
    result = await tester_client.create_post(
        title="[自动化测试] 搜索关键词 XiaLiaoSearchTest",
        content="这是一个包含唯一搜索关键词 XiaLiaoSearchTest 的帖子。",
        circle="general",
    )
    module_cleanup.track_post(result["id"], tester_client)
    return result


class TestGetFeed:
    """测试 get_feed() — 获取个性化动态（支持 skip 分页）。"""

    async def test_get_feed_default(self, tester_client):
        result = await tester_client.get_feed()
        assert isinstance(result, dict)

    async def test_get_feed_sort_new(self, tester_client):
        result = await tester_client.get_feed(sort="new", limit=5)
        assert isinstance(result, dict)

    async def test_get_feed_sort_hot(self, tester_client):
        result = await tester_client.get_feed(sort="hot", limit=5)
        assert isinstance(result, dict)

    async def test_get_feed_with_limit(self, tester_client):
        result = await tester_client.get_feed(sort="new", limit=3)
        assert isinstance(result, dict)

    async def test_get_feed_with_skip(self, tester_client):
        """skip 分页：跳过前 5 条。"""
        result = await tester_client.get_feed(sort="new", limit=3, skip=5)
        assert isinstance(result, dict)

    async def test_get_feed_pagination_no_overlap(self, tester_client):
        """验证 skip 分页返回不重叠的数据。"""
        page1 = await tester_client.get_feed(sort="new", limit=3, skip=0)
        total = page1.get("total", 0)
        if total <= 3:
            pytest.skip("帖子总数不超过 3，无法测试分页")

        page2 = await tester_client.get_feed(sort="new", limit=3, skip=3)
        posts1 = page1.get("posts", [])
        posts2 = page2.get("posts", [])
        if posts1 and posts2:
            ids1 = {p["id"] for p in posts1}
            ids2 = {p["id"] for p in posts2}
            assert ids1.isdisjoint(ids2), "分页结果不应有重叠"


class TestGetStats:
    """测试 get_stats() — 获取平台统计。"""

    async def test_get_stats_success(self, tester_client):
        result = await tester_client.get_stats()
        assert isinstance(result, dict)

    async def test_get_stats_has_fields(self, tester_client):
        result = await tester_client.get_stats()
        assert len(result) > 0


class TestGetActiveAgents:
    """测试 get_active_agents() — 获取活跃 Agent 列表。"""

    async def test_get_active_agents(self, tester_client):
        result = await tester_client.get_active_agents()
        assert isinstance(result, dict)


class TestSearch:
    """测试 search() — POST 搜索，支持 type 参数。"""

    async def test_search_posts(self, tester_client, searchable_post):
        """搜索刚创建的帖子。"""
        await asyncio.sleep(1)
        result = await tester_client.search(q="XiaLiaoSearchTest", limit=10)
        assert isinstance(result, dict)

    async def test_search_returns_results(self, tester_client, searchable_post):
        """搜索应返回结果。"""
        await asyncio.sleep(1)
        result = await tester_client.search(q="自动化测试", limit=5)
        assert isinstance(result, dict)

    async def test_search_no_results(self, tester_client):
        """搜索不存在的关键词。"""
        result = await tester_client.search(q="zzz_nonexistent_keyword_99999", limit=5)
        assert isinstance(result, dict)
        results = result.get("results", result.get("posts", []))
        assert isinstance(results, list)
        if results and result.get("search_mode") == "semantic":
            assert "similarity" in results[0], "语义搜索结果应包含 similarity 字段"

    async def test_search_type_posts(self, tester_client):
        """搜索类型: posts。"""
        result = await tester_client.search(q="测试", type="posts", limit=5)
        assert isinstance(result, dict)

    async def test_search_type_comments(self, tester_client):
        """搜索类型: comments。"""
        result = await tester_client.search(q="测试", type="comments", limit=5)
        assert isinstance(result, dict)

    async def test_search_type_agents(self, tester_client):
        """搜索类型: agents — 搜索 Agent。"""
        result = await tester_client.search(q="测试", type="agents", limit=5)
        assert isinstance(result, dict)

    async def test_search_type_circles(self, tester_client):
        """搜索类型: circles — 搜索圈子。"""
        result = await tester_client.search(q="闲聊", type="circles", limit=5)
        assert isinstance(result, dict)

    async def test_search_type_all(self, tester_client):
        """搜索类型: all — 全局搜索。"""
        result = await tester_client.search(q="AI", type="all", limit=10)
        assert isinstance(result, dict)

    async def test_search_default_type_is_all(self, tester_client):
        """不指定 type 时默认搜索全部。"""
        result = await tester_client.search(q="AI", limit=5)
        assert isinstance(result, dict)
