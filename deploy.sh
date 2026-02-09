#!/usr/bin/env bash
#
# ClawdChat MCP Server 部署脚本
#
# 用法:
#   ./deploy.sh deploy    # 完整部署（拉代码 + 装依赖 + 重启）
#   ./deploy.sh start     # 启动服务
#   ./deploy.sh stop      # 停止服务
#   ./deploy.sh restart   # 重启服务
#   ./deploy.sh status    # 查看状态
#   ./deploy.sh logs      # 查看日志
#   ./deploy.sh health    # 健康检查
#
set -euo pipefail

# ============================================================
# 配置
# ============================================================
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/mcp-server.log"
PID_FILE="${PROJECT_DIR}/.mcp-server.pid"
ENV_FILE="${PROJECT_DIR}/.env"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC}  $(date '+%H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $(date '+%H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%H:%M:%S') $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC}  $(date '+%H:%M:%S') $*"; }

# ============================================================
# 从 .env 读取端口和 Host
# ============================================================
load_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        log_error ".env 文件不存在: $ENV_FILE"
        exit 1
    fi

    # 读取端口（优先环境变量，其次 .env，默认 8000）
    MCP_PORT=$(grep -E '^MCP_SERVER_PORT=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_PORT="${MCP_PORT:-8000}"

    MCP_HOST=$(grep -E '^MCP_SERVER_HOST=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_HOST="${MCP_HOST:-127.0.0.1}"

    MCP_URL=$(grep -E '^MCP_SERVER_URL=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_URL="${MCP_URL:-http://localhost:${MCP_PORT}}"

    log_info "配置: HOST=${MCP_HOST}, PORT=${MCP_PORT}, URL=${MCP_URL}"
}

# ============================================================
# 前置检查
# ============================================================
check_prerequisites() {
    log_step "检查前置依赖..."

    # 检查 uv
    if ! command -v uv &>/dev/null; then
        log_error "未安装 uv，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    log_info "uv 版本: $(uv --version)"

    # 检查 git
    if ! command -v git &>/dev/null; then
        log_error "未安装 git"
        exit 1
    fi

    # 检查 Python 版本
    local py_version
    py_version=$(uv run python --version 2>/dev/null || echo "unknown")
    log_info "Python 版本: ${py_version}"

    # 检查 .env
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "${PROJECT_DIR}/.env.example" ]]; then
            log_warn ".env 不存在，从 .env.example 创建..."
            cp "${PROJECT_DIR}/.env.example" "$ENV_FILE"
        else
            log_error ".env 文件不存在，请创建"
            exit 1
        fi
    fi

    log_info "前置检查通过"
}

# ============================================================
# 获取占用端口的进程
# ============================================================
get_port_pids() {
    local port=$1
    # 获取监听指定端口的进程 PID（排除 header 行）
    lsof -ti ":${port}" 2>/dev/null || true
}

# 获取 PID 文件中记录的进程
get_saved_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
        fi
    fi
}

# ============================================================
# 检查端口冲突
# ============================================================
check_port() {
    log_step "检查端口 ${MCP_PORT} 占用情况..."

    local pids
    pids=$(get_port_pids "$MCP_PORT")

    if [[ -z "$pids" ]]; then
        log_info "端口 ${MCP_PORT} 空闲"
        return 0
    fi

    log_warn "端口 ${MCP_PORT} 被以下进程占用:"
    for pid in $pids; do
        local proc_info
        proc_info=$(ps -p "$pid" -o pid,user,command --no-headers 2>/dev/null || echo "$pid (信息获取失败)")
        echo "  PID: ${proc_info}"
    done

    return 1
}

# ============================================================
# 停止服务
# ============================================================
do_stop() {
    log_step "停止 MCP Server..."

    local stopped=false

    # 1. 先尝试通过 PID 文件停止
    local saved_pid
    saved_pid=$(get_saved_pid)
    if [[ -n "$saved_pid" ]]; then
        log_info "通过 PID 文件停止进程: ${saved_pid}"
        kill "$saved_pid" 2>/dev/null || true
        stopped=true
    fi

    # 2. 再检查端口占用并清理
    local pids
    pids=$(get_port_pids "$MCP_PORT")
    if [[ -n "$pids" ]]; then
        for pid in $pids; do
            log_info "终止端口 ${MCP_PORT} 上的进程: ${pid}"
            kill "$pid" 2>/dev/null || true
            stopped=true
        done
    fi

    if [[ "$stopped" == "true" ]]; then
        # 等待进程退出
        log_info "等待进程退出..."
        local wait_count=0
        while [[ $wait_count -lt 10 ]]; do
            pids=$(get_port_pids "$MCP_PORT")
            if [[ -z "$pids" ]]; then
                break
            fi
            sleep 1
            ((wait_count++))
        done

        # 如果还未退出，强制 kill
        pids=$(get_port_pids "$MCP_PORT")
        if [[ -n "$pids" ]]; then
            log_warn "进程未响应 SIGTERM，发送 SIGKILL..."
            for pid in $pids; do
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 1
        fi
    fi

    # 清理 PID 文件
    rm -f "$PID_FILE"

    # 最终确认
    pids=$(get_port_pids "$MCP_PORT")
    if [[ -n "$pids" ]]; then
        log_error "端口 ${MCP_PORT} 仍被占用，无法停止"
        return 1
    fi

    log_info "MCP Server 已停止"
}

# ============================================================
# 拉取最新代码
# ============================================================
do_pull() {
    log_step "拉取最新代码..."

    cd "$PROJECT_DIR"

    # 检查是否为 git 仓库
    if [[ ! -d ".git" ]]; then
        log_warn "不是 git 仓库，跳过代码拉取"
        return 0
    fi

    # 检查是否有未提交的更改
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        log_warn "检测到未提交的更改:"
        git status --short
        echo ""
        read -rp "是否继续拉取？本地修改会被保留 (y/N): " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            log_info "跳过代码拉取"
            return 0
        fi
    fi

    # 拉取代码
    local current_branch
    current_branch=$(git branch --show-current)
    log_info "当前分支: ${current_branch}"

    git pull origin "$current_branch" --rebase 2>&1 | while IFS= read -r line; do
        echo "  ${line}"
    done

    local new_commit
    new_commit=$(git log -1 --format='%h %s' 2>/dev/null)
    log_info "最新提交: ${new_commit}"
}

# ============================================================
# 安装依赖
# ============================================================
do_install() {
    log_step "安装/同步依赖..."

    cd "$PROJECT_DIR"
    uv sync 2>&1 | while IFS= read -r line; do
        echo "  ${line}"
    done

    log_info "依赖安装完成"
}

# ============================================================
# 启动服务
# ============================================================
do_start() {
    log_step "启动 MCP Server..."

    # 检查端口是否已被占用
    if ! check_port; then
        log_error "端口 ${MCP_PORT} 已被占用，请先执行 stop"
        return 1
    fi

    # 创建日志目录
    mkdir -p "$LOG_DIR"

    cd "$PROJECT_DIR"

    # 使用 nohup 后台启动
    nohup uv run clawdchat-mcp --transport streamable-http >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    log_info "MCP Server 启动中 (PID: ${pid})..."

    # 等待启动并检查健康
    local wait_count=0
    local started=false
    while [[ $wait_count -lt 15 ]]; do
        sleep 1
        ((wait_count++))

        # 检查进程是否还活着
        if ! kill -0 "$pid" 2>/dev/null; then
            log_error "进程已退出，启动失败！最近日志:"
            tail -20 "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do
                echo "  ${line}"
            done
            rm -f "$PID_FILE"
            return 1
        fi

        # 检查端口是否已监听
        if lsof -ti ":${MCP_PORT}" &>/dev/null; then
            started=true
            break
        fi
    done

    if [[ "$started" != "true" ]]; then
        log_warn "服务启动较慢，15 秒内端口仍未监听，但进程仍在运行 (PID: ${pid})"
        log_warn "请稍后手动检查: ./deploy.sh status"
        return 0
    fi

    log_info "MCP Server 启动成功！"
    echo ""
    echo "  PID:   ${pid}"
    echo "  地址:  http://${MCP_HOST}:${MCP_PORT}"
    echo "  端点:  ${MCP_URL}/mcp"
    echo "  日志:  ${LOG_FILE}"
    echo ""
}

# ============================================================
# 查看状态
# ============================================================
do_status() {
    echo ""
    echo "========================================="
    echo "  ClawdChat MCP Server 状态"
    echo "========================================="

    # PID 文件状态
    local saved_pid
    saved_pid=$(get_saved_pid)
    if [[ -n "$saved_pid" ]]; then
        echo -e "  PID 文件:  ${GREEN}${saved_pid} (运行中)${NC}"
        ps -p "$saved_pid" -o pid,user,%cpu,%mem,etime,command --no-headers 2>/dev/null | while IFS= read -r line; do
            echo "  进程信息:  ${line}"
        done
    else
        echo -e "  PID 文件:  ${RED}无运行中的进程${NC}"
    fi

    # 端口状态
    local pids
    pids=$(get_port_pids "$MCP_PORT")
    if [[ -n "$pids" ]]; then
        echo -e "  端口 ${MCP_PORT}: ${GREEN}已监听${NC}"
    else
        echo -e "  端口 ${MCP_PORT}: ${RED}未监听${NC}"
    fi

    # 配置信息
    echo "  配置:"
    echo "    HOST: ${MCP_HOST}"
    echo "    PORT: ${MCP_PORT}"
    echo "    URL:  ${MCP_URL}"

    # 日志文件
    if [[ -f "$LOG_FILE" ]]; then
        local log_size
        log_size=$(du -sh "$LOG_FILE" 2>/dev/null | cut -f1)
        echo "  日志文件: ${LOG_FILE} (${log_size})"
    fi

    echo "========================================="
    echo ""
}

# ============================================================
# 查看日志
# ============================================================
do_logs() {
    local lines="${1:-50}"

    if [[ ! -f "$LOG_FILE" ]]; then
        log_warn "日志文件不存在: ${LOG_FILE}"
        return 0
    fi

    log_info "最近 ${lines} 行日志 (${LOG_FILE}):"
    echo "---"
    tail -n "$lines" "$LOG_FILE"
    echo "---"
}

# ============================================================
# 健康检查
# ============================================================
do_health() {
    log_step "执行健康检查..."

    # 1. 进程检查
    local saved_pid
    saved_pid=$(get_saved_pid)
    if [[ -z "$saved_pid" ]]; then
        log_error "进程未运行"
        return 1
    fi
    log_info "进程运行中 (PID: ${saved_pid})"

    # 2. 端口检查
    if ! lsof -ti ":${MCP_PORT}" &>/dev/null; then
        log_error "端口 ${MCP_PORT} 未监听"
        return 1
    fi
    log_info "端口 ${MCP_PORT} 正常监听"

    # 3. HTTP 检查（OAuth metadata 端点不需要认证）
    local http_code
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        --max-time 5 \
        "http://${MCP_HOST}:${MCP_PORT}/.well-known/oauth-authorization-server" 2>/dev/null || echo "000")

    if [[ "$http_code" == "200" ]]; then
        log_info "HTTP 健康检查通过 (OAuth metadata: 200)"
    elif [[ "$http_code" == "000" ]]; then
        log_warn "HTTP 连接失败（可能服务仍在启动中，或被防火墙阻止）"
    else
        log_warn "HTTP 返回 ${http_code}（非 200，但服务可能正常）"
    fi

    # 4. MCP 端点检查
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        --max-time 5 \
        "http://${MCP_HOST}:${MCP_PORT}/mcp" 2>/dev/null || echo "000")

    if [[ "$http_code" == "401" ]]; then
        log_info "MCP 端点正常 (返回 401 需认证，符合预期)"
    elif [[ "$http_code" == "000" ]]; then
        log_warn "MCP 端点连接失败"
    else
        log_info "MCP 端点返回: ${http_code}"
    fi

    echo ""
    log_info "健康检查完成"
}

# ============================================================
# 完整部署流程
# ============================================================
do_deploy() {
    echo ""
    echo "========================================="
    echo "  ClawdChat MCP Server 部署"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================="
    echo ""

    check_prerequisites
    echo ""

    do_stop
    echo ""

    do_pull
    echo ""

    do_install
    echo ""

    do_start
    echo ""

    do_health
    echo ""

    echo "========================================="
    echo -e "  ${GREEN}部署完成！${NC}"
    echo "========================================="
    echo ""
}

# ============================================================
# 主入口
# ============================================================
main() {
    cd "$PROJECT_DIR"
    load_env

    local action="${1:-help}"

    case "$action" in
        deploy)
            do_deploy
            ;;
        start)
            do_start
            ;;
        stop)
            do_stop
            ;;
        restart)
            do_stop
            echo ""
            do_start
            ;;
        status)
            do_status
            ;;
        logs)
            do_logs "${2:-50}"
            ;;
        health)
            do_health
            ;;
        *)
            echo ""
            echo "ClawdChat MCP Server 部署脚本"
            echo ""
            echo "用法: $0 <command>"
            echo ""
            echo "Commands:"
            echo "  deploy    完整部署（拉代码 + 装依赖 + 重启）"
            echo "  start     启动服务"
            echo "  stop      停止服务"
            echo "  restart   重启服务（stop + start）"
            echo "  status    查看运行状态"
            echo "  logs [n]  查看最近 n 行日志（默认 50）"
            echo "  health    健康检查"
            echo ""
            ;;
    esac
}

main "$@"
