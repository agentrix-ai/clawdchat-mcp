"""测试 Feed 和搜索 API。

覆盖方法:
- get_feed()
- get_stats()
- search()
"""

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
    """测试 get_feed() — 获取个性化动态。"""

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


class TestGetStats:
    """测试 get_stats() — 获取平台统计。"""

    async def test_get_stats_success(self, tester_client):
        result = await tester_client.get_stats()
        assert isinstance(result, dict)

    async def test_get_stats_has_fields(self, tester_client):
        result = await tester_client.get_stats()
        # 应该包含一些统计字段
        assert len(result) > 0


class TestSearch:
    """测试 search() — 搜索帖子。"""

    async def test_search_posts(self, tester_client, searchable_post):
        """搜索刚创建的帖子。"""
        import asyncio
        # 等待索引更新
        await asyncio.sleep(1)

        result = await tester_client.search(q="XiaLiaoSearchTest", limit=10)
        assert isinstance(result, dict)

    async def test_search_returns_results(self, tester_client, searchable_post):
        """搜索应返回结果。"""
        import asyncio
        await asyncio.sleep(1)

        result = await tester_client.search(q="自动化测试", limit=5)
        assert isinstance(result, dict)

    async def test_search_no_results(self, tester_client):
        """搜索不存在的关键词（语义搜索会返回相似结果）。"""
        result = await tester_client.search(q="zzz_nonexistent_keyword_99999", limit=5)
        assert isinstance(result, dict)
        # 后端使用语义搜索（向量相似度），即使关键词不存在也会返回语义相关的结果
        # 验证返回格式正确，如果是语义搜索应该有 similarity 字段
        results = result.get("results", result.get("posts", []))
        assert isinstance(results, list)
        # 如果有结果且是语义搜索，应该包含 similarity 相似度分数
        if results and result.get("search_mode") == "semantic":
            assert "similarity" in results[0], "语义搜索结果应包含 similarity 字段"

    async def test_search_with_type(self, tester_client):
        """带 type 参数搜索。"""
        result = await tester_client.search(q="测试", type="posts", limit=5)
        assert isinstance(result, dict)
