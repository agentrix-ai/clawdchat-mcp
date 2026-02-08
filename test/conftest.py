"""pytest fixtures — 加载测试账号、创建 client 实例、清限流、管理资源清理。

使用前须先运行 setup_accounts.py 初始化账号。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# 将项目 src 加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clawdchat_mcp.api_client import ClawdChatAgentClient, ClawdChatAPIError

ACCOUNTS_FILE = Path(__file__).parent / "test_accounts.json"


# ---------------------------------------------------------------------------
# 限流清理（通过后端 API）
# ---------------------------------------------------------------------------

async def _clear_rate_limits(api_url: str, api_keys: list[str]) -> None:
    """通过后端 API 清除测试 Agent 的限流计数。"""
    for api_key in api_keys:
        try:
            client = ClawdChatAgentClient(api_url, api_key)
            await client.reset_rate_limit()
        except ClawdChatAPIError as e:
            if e.status_code == 403:
                print(f"[SETUP WARNING] 限流重置接口仅在开发环境可用: {e.detail}")
            else:
                print(f"[SETUP WARNING] 清除限流失败: {e}")
        except Exception as e:
            print(f"[SETUP WARNING] 清除限流失败: {e}")


# ---------------------------------------------------------------------------
# 帖子创建辅助（处理速率限制）
# ---------------------------------------------------------------------------

async def safe_create_post(
    client: ClawdChatAgentClient,
    title: str,
    content: str,
    circle: str = "general",
) -> dict:
    """创建帖子，如果遇到速率限制则 skip。"""
    try:
        return await client.create_post(title, content, circle)
    except ClawdChatAPIError as e:
        if e.status_code == 429:
            pytest.skip(f"发帖频率超限，跳过: {e.detail}")
        raise


# ---------------------------------------------------------------------------
# 账号加载
# ---------------------------------------------------------------------------

def _load_accounts() -> dict:
    """加载 test_accounts.json，不存在时跳过整个测试。"""
    if not ACCOUNTS_FILE.exists():
        pytest.skip(
            f"测试账号文件不存在: {ACCOUNTS_FILE}\n"
            "请先运行: uv run python test/setup_accounts.py"
        )
    data = json.loads(ACCOUNTS_FILE.read_text())
    # 支持通过环境变量覆盖 API URL
    api_url = os.environ.get("CLAWDCHAT_API_URL", data.get("api_url", "http://localhost:8081"))
    data["api_url"] = api_url
    return data


# ---------------------------------------------------------------------------
# Session scope: 账号数据（整个测试会话只读一次）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def accounts() -> dict:
    """测试账号信息 (session scope)。"""
    return _load_accounts()


@pytest.fixture(scope="session")
def api_url(accounts) -> str:
    return accounts["api_url"]


@pytest.fixture(scope="session")
def tester_info(accounts) -> dict:
    """主测试虾的账号信息: name, agent_id, api_key。"""
    return accounts["tester"]


@pytest.fixture(scope="session")
def target_info(accounts) -> dict:
    """被测虾的账号信息: name, agent_id, api_key。"""
    return accounts["target"]


# ---------------------------------------------------------------------------
# Session scope: 测试前清限流
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _clear_rate_limits_before_session(accounts):
    """测试会话开始前，自动清除两个 Agent 的限流计数。"""
    api_url = os.environ.get("CLAWDCHAT_API_URL", accounts.get("api_url", "http://localhost:8081"))
    api_keys = [
        accounts["tester"]["api_key"],
        accounts["target"]["api_key"],
    ]
    await _clear_rate_limits(api_url, api_keys)
    print("[SETUP] 限流计数已清除")


# ---------------------------------------------------------------------------
# Session scope: API Client 实例
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tester_client(api_url, tester_info) -> ClawdChatAgentClient:
    """主测试虾的 ClawdChatAgentClient。"""
    return ClawdChatAgentClient(api_url, tester_info["api_key"])


@pytest.fixture(scope="session")
def target_client(api_url, target_info) -> ClawdChatAgentClient:
    """被测虾的 ClawdChatAgentClient。"""
    return ClawdChatAgentClient(api_url, target_info["api_key"])


# ---------------------------------------------------------------------------
# 清理追踪器
# ---------------------------------------------------------------------------

class CleanupTracker:
    """追踪测试中创建的资源，在 teardown 时逆序清理。"""

    def __init__(self):
        self._items: list[tuple[str, str, ClawdChatAgentClient]] = []

    def track_post(self, post_id: str, client: ClawdChatAgentClient):
        self._items.append(("post", post_id, client))

    def track_comment(self, comment_id: str, client: ClawdChatAgentClient):
        self._items.append(("comment", comment_id, client))

    def track_follow(self, agent_name: str, client: ClawdChatAgentClient):
        self._items.append(("follow", agent_name, client))

    def track_subscription(self, circle_name: str, client: ClawdChatAgentClient):
        self._items.append(("subscription", circle_name, client))

    def track_dm_conversation(self, conversation_id: str, client: ClawdChatAgentClient):
        self._items.append(("dm_conversation", conversation_id, client))

    async def cleanup_all(self):
        """逆序清理所有追踪的资源。"""
        for resource_type, resource_id, client in reversed(self._items):
            try:
                if resource_type == "comment":
                    await client.delete_comment(resource_id)
                elif resource_type == "post":
                    await client.delete_post(resource_id)
                elif resource_type == "follow":
                    await client.unfollow(resource_id)
                elif resource_type == "subscription":
                    await client.unsubscribe_circle(resource_id)
                elif resource_type == "dm_conversation":
                    await client.dm_delete_conversation(resource_id)
            except Exception as e:
                # 清理失败不影响测试结果，仅打印警告
                print(f"[CLEANUP WARNING] 清理 {resource_type} '{resource_id}' 失败: {e}")
        self._items.clear()


@pytest_asyncio.fixture
async def cleanup(tester_client):
    """函数级别的清理追踪器，每个测试函数结束后自动清理。"""
    tracker = CleanupTracker()
    yield tracker
    await tracker.cleanup_all()


@pytest_asyncio.fixture(scope="module")
async def module_cleanup(tester_client):
    """模块级别的清理追踪器，每个测试模块结束后自动清理。"""
    tracker = CleanupTracker()
    yield tracker
    await tracker.cleanup_all()
