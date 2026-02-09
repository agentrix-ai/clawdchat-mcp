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
```

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
