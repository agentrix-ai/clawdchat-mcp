"""一次性账号初始化脚本。

自动完成：注册 Agent → 手机号认领 → 获取 API Key → 写入 test_accounts.json

用法：
    cd clawdchat-mcp
    uv run python test/setup_accounts.py [--api-url http://localhost:8081]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

# 测试账号配置
PHONE = "19521472942"
TESTER_NAME = "我是虾聊官方的测试虾"
TESTER_DESC = "ClawdChat 官方测试 Agent，用于自动化功能验证。"
TARGET_NAME = "测试虾的被测虾"
TARGET_DESC = "ClawdChat 官方测试 Agent（被动方），用于双 Agent 交互测试。"

ACCOUNTS_FILE = Path(__file__).parent / "test_accounts.json"


async def phone_login(base_url: str, phone: str) -> str:
    """手机号登录，返回 JWT token。"""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base_url}/api/v1/auth/phone/login",
            json={"phone": phone},
        )
        r.raise_for_status()
        jwt = r.cookies.get("clawdchat_token", "")
        if not jwt:
            raise RuntimeError(f"登录成功但未获取到 JWT cookie: {r.json()}")
        print(f"[OK] 手机号 {phone} 登录成功")
        return jwt


async def list_my_agents(base_url: str, jwt: str) -> list[dict]:
    """列出用户名下的所有 Agent。"""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{base_url}/api/v1/users/me/agents",
            cookies={"clawdchat_token": jwt},
        )
        r.raise_for_status()
        data = r.json()
        agents = data.get("agents", [])
        print(f"[OK] 用户名下共有 {len(agents)} 个 Agent")
        return agents


async def register_agent(base_url: str, name: str, description: str) -> dict:
    """注册新 Agent，返回包含 api_key 和 claim_token 的响应。"""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base_url}/api/v1/agents/register",
            json={"name": name, "description": description},
        )
        if r.status_code != 200:
            detail = r.json().get("detail", r.text)
            raise RuntimeError(f"注册 Agent '{name}' 失败: {detail}")
        data = r.json()
        agent_data = data.get("agent", {})
        print(f"[OK] 注册 Agent '{name}' 成功, id={agent_data.get('id')}")
        return agent_data


async def claim_agent(base_url: str, claim_token: str, phone: str, jwt: str) -> None:
    """用手机号认领 Agent。注意此接口返回 HTML。"""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base_url}/api/v1/claim/phone/claim/{claim_token}",
            json={"phone": phone},
            cookies={"clawdchat_token": jwt},
        )
        if r.status_code != 200:
            raise RuntimeError(f"认领失败 (HTTP {r.status_code}): {r.text[:200]}")
        print(f"[OK] Agent 认领成功 (claim_token={claim_token[:8]}...)")


async def get_agent_credentials(base_url: str, jwt: str, agent_id: str) -> str | None:
    """获取 Agent 的 API Key（可能为 None）。"""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{base_url}/api/v1/users/me/agents/{agent_id}/credentials",
            cookies={"clawdchat_token": jwt},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("api_key")


async def reset_agent_key(base_url: str, jwt: str, agent_id: str) -> str:
    """重置 Agent 的 API Key，返回新 Key。"""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base_url}/api/v1/users/me/agents/{agent_id}/reset-key",
            cookies={"clawdchat_token": jwt},
        )
        r.raise_for_status()
        data = r.json()
        api_key = data.get("api_key")
        if not api_key:
            raise RuntimeError(f"重置 API Key 失败: {data}")
        return api_key


async def ensure_agent(
    base_url: str,
    jwt: str,
    existing_agents: list[dict],
    name: str,
    description: str,
    phone: str,
) -> dict:
    """确保 Agent 存在并获取其 API Key。返回 {"name", "agent_id", "api_key"}。"""
    # 在已有 agents 中查找
    found = None
    for agent in existing_agents:
        if agent["name"] == name:
            found = agent
            break

    if found:
        agent_id = found["id"]
        print(f"[INFO] Agent '{name}' 已存在 (id={agent_id})")

        # 尝试获取现有 key
        api_key = await get_agent_credentials(base_url, jwt, agent_id)
        if not api_key:
            print(f"[INFO] Agent '{name}' 无 API Key，执行重置...")
            api_key = await reset_agent_key(base_url, jwt, agent_id)
        print(f"[OK] 获取 API Key 成功: {api_key[:20]}...")
        return {"name": name, "agent_id": agent_id, "api_key": api_key}

    # Agent 不存在，注册 + 认领
    print(f"[INFO] Agent '{name}' 不存在，开始注册...")
    agent_data = await register_agent(base_url, name, description)
    agent_id = agent_data["id"]
    api_key = agent_data.get("api_key", "")

    # 从 claim_url 中提取 claim_token
    claim_url = agent_data.get("claim_url", "")
    claim_token = ""
    if claim_url:
        # claim_url 格式: http://host/claim/{claim_token}
        claim_token = claim_url.rstrip("/").split("/")[-1]

    if claim_token:
        await claim_agent(base_url, claim_token, phone, jwt)
        # 认领后需要重新登录获取新 JWT（cookie 可能已更新）
        jwt_new = await phone_login(base_url, phone)
        # 如果注册时就拿到了 key，直接用；否则重置获取
        if not api_key:
            api_key = await reset_agent_key(base_url, jwt_new, agent_id)
    else:
        # 没有 claim_token（不应该出现）
        if not api_key:
            api_key = await reset_agent_key(base_url, jwt, agent_id)

    print(f"[OK] Agent '{name}' 准备就绪, api_key={api_key[:20]}...")
    return {"name": name, "agent_id": agent_id, "api_key": api_key}


async def main(api_url: str) -> None:
    """主流程：初始化两个测试账号。"""
    print(f"\n{'='*60}")
    print(f"ClawdChat 测试账号初始化")
    print(f"后端地址: {api_url}")
    print(f"{'='*60}\n")

    # 1. 手机号登录
    jwt = await phone_login(api_url, PHONE)

    # 2. 列出已有 Agents
    agents = await list_my_agents(api_url, jwt)

    # 3. 确保主测试虾
    print(f"\n--- 初始化主测试虾 ---")
    tester = await ensure_agent(api_url, jwt, agents, TESTER_NAME, TESTER_DESC, PHONE)

    # 重新登录（认领可能改变了 JWT）
    jwt = await phone_login(api_url, PHONE)
    agents = await list_my_agents(api_url, jwt)

    # 4. 确保被测虾
    print(f"\n--- 初始化被测虾 ---")
    target = await ensure_agent(api_url, jwt, agents, TARGET_NAME, TARGET_DESC, PHONE)

    # 5. 验证两个 key 都能工作
    print(f"\n--- 验证 API Key ---")
    for label, account in [("主测试虾", tester), ("被测虾", target)]:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{api_url}/api/v1/agents/me",
                headers={"Authorization": f"Bearer {account['api_key']}"},
            )
            if r.status_code == 200:
                me = r.json()
                print(f"[OK] {label} API Key 验证通过, name={me.get('name')}")
            else:
                print(f"[FAIL] {label} API Key 验证失败: HTTP {r.status_code}")
                sys.exit(1)

    # 6. 写入 JSON
    result = {
        "api_url": api_url,
        "phone": PHONE,
        "tester": tester,
        "target": target,
    }

    ACCOUNTS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[OK] 账号信息已写入 {ACCOUNTS_FILE}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n{'='*60}")
    print("初始化完成！现在可以运行测试了：uv run pytest test/ -v")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="初始化 ClawdChat 测试账号")
    parser.add_argument(
        "--api-url",
        default="http://localhost:8081",
        help="ClawdChat 后端地址 (默认: http://localhost:8081)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.api_url))
