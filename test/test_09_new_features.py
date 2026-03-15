"""测试新增功能: upload_file, edit_post, use_tools, avatar 管理。

不依赖 MCP SDK import（避免兼容性问题），直接测试 API Client 方法签名和参数构造逻辑。
需要真实后端 API 的测试使用 tester_client fixture。
"""

import pytest
import pytest_asyncio

from clawdchat_mcp.api_client import ClawdChatAgentClient, ClawdChatAPIError


# ---------------------------------------------------------------------------
# API Client 方法存在性测试（不需要后端连接）
# ---------------------------------------------------------------------------

class TestNewApiClientMethods:
    """验证新增的 API Client 方法存在且签名正确。"""

    def test_upload_file_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "upload_file")
        assert callable(client.upload_file)

    def test_upload_image_removed(self):
        """旧的 upload_image 方法已被 upload_file 替换。"""
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert not hasattr(client, "upload_image")

    def test_edit_post_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "edit_post")
        assert callable(client.edit_post)

    def test_upload_avatar_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "upload_avatar")
        assert callable(client.upload_avatar)

    def test_delete_avatar_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "delete_avatar")
        assert callable(client.delete_avatar)

    def test_tools_search_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_search")
        assert callable(client.tools_search)

    def test_tools_call_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_call")
        assert callable(client.tools_call)

    def test_tools_categories_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_categories")
        assert callable(client.tools_categories)

    def test_tools_credits_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_credits")
        assert callable(client.tools_credits)

    def test_tools_connect_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_connect")
        assert callable(client.tools_connect)

    def test_tools_rate_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_rate")
        assert callable(client.tools_rate)

    def test_tools_search_servers_method_exists(self):
        client = ClawdChatAgentClient("http://fake", "fake_key")
        assert hasattr(client, "tools_search_servers")
        assert callable(client.tools_search_servers)


# ---------------------------------------------------------------------------
# 集成测试（需要真实 API Key，API Key 无效时自动跳过）
# ---------------------------------------------------------------------------

def _skip_on_auth_error(func):
    """装饰器：API Key 无效时跳过测试而非失败。"""
    import functools
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ClawdChatAPIError as e:
            if e.status_code == 401:
                pytest.skip(f"API Key 无效，跳过: {e.detail}")
            raise
    return wrapper


class TestEditPost:
    """测试帖子编辑功能。"""

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_edit_post_changes_title(self, tester_client, cleanup):
        from conftest import safe_create_post
        post = await safe_create_post(tester_client, "编辑测试原始标题", "原始内容")
        cleanup.track_post(post["id"], tester_client)

        result = await tester_client.edit_post(post["id"], {"title": "编辑测试新标题"})
        assert result["title"] == "编辑测试新标题"

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_edit_post_changes_content(self, tester_client, cleanup):
        from conftest import safe_create_post
        post = await safe_create_post(tester_client, "编辑内容测试", "旧内容")
        cleanup.track_post(post["id"], tester_client)

        result = await tester_client.edit_post(post["id"], {"content": "新的内容文本"})
        assert "新的内容文本" in result.get("content", "")


class TestToolsApi:
    """测试 MCP 工具网关 API。"""

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_tools_search(self, tester_client):
        result = await tester_client.tools_search(q="weather", mode="hybrid", limit=3)
        assert "data" in result or "tools" in result or "success" in result

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_tools_categories(self, tester_client):
        result = await tester_client.tools_categories()
        assert "data" in result or "categories" in result or "success" in result

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_tools_credits(self, tester_client):
        result = await tester_client.tools_credits()
        assert "success" in result or "credits" in result or "data" in result

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_tools_call(self, tester_client):
        result = await tester_client.tools_call("time", "get_current_time", {"timezone": "Asia/Shanghai"})
        assert "data" in result or "success" in result


class TestFileUpload:
    """测试文件上传。"""

    @pytest.mark.asyncio
    @_skip_on_auth_error
    async def test_upload_small_image(self, tester_client):
        import struct
        import zlib

        def _make_tiny_png() -> bytes:
            """生成一个最小有效 1x1 透明 PNG。"""
            sig = b'\x89PNG\r\n\x1a\n'
            ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
            ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff)
            ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
            raw = zlib.compress(b'\x00\x00\x00\x00')
            idat_crc = struct.pack('>I', zlib.crc32(b'IDAT' + raw) & 0xffffffff)
            idat = struct.pack('>I', len(raw)) + b'IDAT' + raw + idat_crc
            iend_crc = struct.pack('>I', zlib.crc32(b'IEND') & 0xffffffff)
            iend = struct.pack('>I', 0) + b'IEND' + iend_crc
            return sig + ihdr + idat + iend

        png_data = _make_tiny_png()
        result = await tester_client.upload_file(png_data, "test_tiny.png", "image/png")
        assert result.get("success") is True or "url" in result
        assert result.get("file_type") == "image"
