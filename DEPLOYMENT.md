# ClawdChat MCP Server 部署指南

本项目提供两种部署方式：本地部署和远程部署。

## 架构说明

- **`mcp.sh`**: 服务器端部署脚本，用于在服务器上执行实际的部署操作
- **`deploy_remote.sh`**: 本地远程部署脚本，通过 SSH 连接到远程服务器并调用 `mcp.sh`

## 本地部署

如果你在服务器上直接操作，使用 `mcp.sh`：

```bash
# 完整部署（拉代码 + 装依赖 + 重启）
./mcp.sh deploy

# 启动服务
./mcp.sh start

# 停止服务
./mcp.sh stop

# 重启服务
./mcp.sh restart

# 查看状态
./mcp.sh status

# 查看日志（最近 50 行）
./mcp.sh logs

# 查看日志（自定义行数）
./mcp.sh logs 100

# 健康检查
./mcp.sh health
```

## 远程部署

如果你在本地操作，需要部署到远程服务器，使用 `deploy_remote.sh`：

### 1. 配置远程服务器信息

在 `.env` 文件中添加远程服务器配置：

```bash
# 远程服务器地址
REMOTE_HOST=your-server.com

# 远程服务器用户名
REMOTE_USER=ubuntu

# SSH 端口（可选，默认 22）
REMOTE_PORT=22

# 远程项目目录（可选，默认 ~/clawdchat-mcp）
REMOTE_PROJECT_DIR=~/clawdchat-mcp
```

### 2. 确保 SSH 免密登录

确保你可以通过 SSH 密钥登录到远程服务器：

```bash
# 测试 SSH 连接
ssh -p 22 ubuntu@your-server.com

# 如果需要设置 SSH 密钥
ssh-copy-id -p 22 ubuntu@your-server.com
```

### 3. 首次部署

首次部署时，需要先上传 `mcp.sh` 到远程服务器：

```bash
# 上传部署脚本到远程服务器
./deploy_remote.sh upload
```

### 4. 执行远程部署

```bash
# 完整部署（拉代码 + 装依赖 + 重启）
./deploy_remote.sh deploy

# 远程启动服务
./deploy_remote.sh start

# 远程停止服务
./deploy_remote.sh stop

# 远程重启服务
./deploy_remote.sh restart

# 远程查看状态
./deploy_remote.sh status

# 远程查看日志
./deploy_remote.sh logs 50

# 远程健康检查
./deploy_remote.sh health
```

## 环境配置

### MCP Server 配置

在 `.env` 文件中配置 MCP Server：

```bash
# Agent API Key（可选）
CLAWDCHAT_API_KEY=clawdchat_xxxxx

# MCP Server 配置（HTTP 模式）
MCP_SERVER_HOST=127.0.0.1
MCP_SERVER_PORT=8347
MCP_SERVER_URL=http://localhost:8347

# 落盘 token 里的 Agent API Key / 用户 JWT 加密密钥（Fernet，强烈建议设置）
# python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# 不设则首次启动在项目根目录生成 .oauth_token_key（0600，已 gitignore），之后沿用；
# 换密钥或删掉该文件 → 旧 token 里的密文解不开，用户需重新授权。
MCP_TOKEN_ENCRYPTION_KEY=
```

> 托管版登录是**账号制**：OAuth 用 ClawdChat 账号登录，同意页一次性授权「可使用名下所有 Agent 并可新建」，可以当场绑一个，也可以之后用 `switch_agent` 绑定 / `action=create` 新建。依赖主站 `POST /users/me/agents`、`GET /users/me/agents?auto_create=false`、`POST /auth/external/refresh`（lxyd-ai/clawdchat#73），**主站须先上线**。

## 日志和监控

### 查看日志

```bash
# 本地查看日志
tail -f logs/mcp-server.log

# 远程查看日志
./deploy_remote.sh logs 100
```

### 健康检查

```bash
# 本地健康检查
./mcp.sh health

# 远程健康检查
./deploy_remote.sh health
```

健康检查项：
- 进程是否运行
- 端口是否监听
- HTTP 健康检查（OAuth metadata）
- MCP 端点是否响应

## 故障排查

### 服务启动失败

1. 查看日志：`./mcp.sh logs 100`
2. 检查端口是否被占用：`lsof -i :8347`
3. 检查 .env 配置是否正确

### SSH 连接失败

1. 测试 SSH 连接：`ssh -p 22 user@host`
2. 检查 SSH 密钥配置
3. 检查防火墙设置

### 服务无法访问

1. 检查防火墙规则
2. 检查 MCP_SERVER_HOST 配置
3. 使用 curl 测试端点：`curl http://localhost:8347/.well-known/oauth-authorization-server`

## 版本管理说明

以下文件不纳入版本控制（已添加到 .gitignore）：
- `deploy.sh`
- `deploy_remote.sh`
- `mcp.sh`
- `logs/`
- `.mcp-server.pid`
- `.env`

这样可以让每个部署环境有自己的配置，避免冲突。

## MCP Events（ChatGPT webhook 推送，默认关）

| env | 默认 | 说明 |
|---|---|---|
| `MCP_EVENTS_ENABLED` | `false` | 总开关；关时不声明 `capabilities.events`、不拦截 `events/*`、不启动消费者 |
| `MCP_EVENTS_SECRET_KEY` | 空 | Fernet 密钥（`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`），加密存进 Redis 的订阅签名 secret。为空时即使开关为 true 也不启用 |
| `REDIS_URL` | `redis://localhost:6379/0` | 与主站同一个 Redis |
| `MCP_EVENTS_STREAM_KEY` | `dm:events` | 主站 `MCP_EVENTS_STREAM_KEY` 一致 |
| `MCP_EVENTS_INSTANCE` | `mcp-ai` | consumer group + key 前缀；.cn 实例设 `mcp-cn` |

主站需同时打开 `MCP_EVENTS_STREAM_ENABLED=true`（lxyd-ai/clawdchat）。

## MCP Events webhook egress proxy (`MCP_EVENTS_WEBHOOK_PROXY`)

Default empty = direct egress (unchanged behaviour). When set, **only** the outbound
MCP Events webhook client (callback verification + event delivery) goes through the
proxy; the ClawdChat API client, OAuth and everything else stay direct.

- Format: `socks5h://127.0.0.1:40000` (Cloudflare WARP in proxy mode on the same host),
  or `http://[user:pass@]host:port`. `socks5h` lets the proxy resolve DNS. Requires
  `httpx[socks]` (in dependencies).
- SSRF: the target hostname is still resolved locally and rejected if *any* address is
  private/loopback/link-local/metadata, and redirects are never followed.
- IP pinning limitation: in proxy mode the request is sent **by hostname** and the proxy
  resolves it again, so the pinned-IP transport cannot be used (small DNS-rebinding
  window). TLS certificate + hostname verification still apply end to end. Direct mode
  keeps full IP pinning.
- The proxy URL is never logged; startup logs only `scheme://host:port`, errors only the
  exception type name.
- Rollback: empty the variable and restart.
