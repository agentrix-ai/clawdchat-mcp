#!/usr/bin/env bash
#
# ClawdChat MCP Server 部署脚本
#
# 用法:
#   ./mcp.sh deploy    # 完整部署（拉代码 + 装依赖 + 重启）
#   ./mcp.sh start     # 启动服务
#   ./mcp.sh stop      # 停止服务
#   ./mcp.sh restart   # 重启服务
#   ./mcp.sh status    # 查看状态
#   ./mcp.sh logs      # 查看日志
#   ./mcp.sh health    # 健康检查
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
SYSTEMD_SERVICE="clawdchat-mcp"

has_systemd() {
    systemctl list-unit-files "${SYSTEMD_SERVICE}.service" &>/dev/null 2>&1
}

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

    MCP_PORT=$(grep -E '^MCP_SERVER_PORT=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_PORT="${MCP_PORT:-8000}"

    MCP_HOST=$(grep -E '^MCP_SERVER_HOST=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_HOST="${MCP_HOST:-127.0.0.1}"

    MCP_URL=$(grep -E '^MCP_SERVER_URL=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
    MCP_URL="${MCP_URL:-http://localhost:${MCP_PORT}}"

    log_info "配置: HOST=${MCP_HOST}, PORT=${MCP_PORT}, URL=${MCP_URL}"
}

# ============================================================
# 端口检测（ss 优先，lsof 兜底）
# ============================================================
is_port_listening() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tln 2>/dev/null | grep -q ":${port} " && return 0
    fi
    if command -v lsof &>/dev/null; then
        lsof -ti ":${port}" &>/dev/null && return 0
    fi
    if command -v netstat &>/dev/null; then
        netstat -tlnp 2>/dev/null | grep -q ":${port} " && return 0
    fi
    return 1
}

get_port_pids() {
    local port=$1
    local pids=""
    if command -v ss &>/dev/null; then
        pids=$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | sort -u || true)
    fi
    if [[ -z "$pids" ]] && command -v lsof &>/dev/null; then
        pids=$(lsof -ti ":${port}" 2>/dev/null || true)
    fi
    if [[ -z "$pids" ]] && command -v fuser &>/dev/null; then
        pids=$(fuser "${port}/tcp" 2>/dev/null || true)
    fi
    echo "$pids"
}

# ============================================================
# PID 管理
# ============================================================
get_saved_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
        fi
    fi
}

get_all_related_pids() {
    local pids=""

    local saved_pid
    saved_pid=$(get_saved_pid)
    if [[ -n "$saved_pid" ]]; then
        pids="$saved_pid"
        local children
        children=$(pgrep -P "$saved_pid" 2>/dev/null || true)
        if [[ -n "$children" ]]; then
            pids="$pids $children"
        fi
    fi

    local port_pids
    port_pids=$(get_port_pids "$MCP_PORT")
    if [[ -n "$port_pids" ]]; then
        pids="$pids $port_pids"
    fi

    echo "$pids" | tr ' ' '\n' | sort -u | tr '\n' ' '
}

# ============================================================
# 前置检查
# ============================================================
check_prerequisites() {
    log_step "检查前置依赖..."

    if ! command -v uv &>/dev/null; then
        log_error "未安装 uv，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    log_info "uv 版本: $(uv --version)"

    if ! command -v git &>/dev/null; then
        log_error "未安装 git"
        exit 1
    fi

    local py_version
    py_version=$(uv run python --version 2>/dev/null || echo "unknown")
    log_info "Python 版本: ${py_version}"

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
# 检查端口冲突
# ============================================================
check_port() {
    log_step "检查端口 ${MCP_PORT} 占用情况..."

    if ! is_port_listening "$MCP_PORT"; then
        log_info "端口 ${MCP_PORT} 空闲"
        return 0
    fi

    local pids
    pids=$(get_port_pids "$MCP_PORT")
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

    if has_systemd; then
        systemctl stop "${SYSTEMD_SERVICE}" 2>/dev/null || true
        sleep 1
        if ! is_port_listening "$MCP_PORT"; then
            rm -f "$PID_FILE"
            log_info "MCP Server 已停止 (systemd)"
            return 0
        fi
        log_warn "systemd stop 后端口仍在，回退到手动停止..."
    fi

    local all_pids
    all_pids=$(get_all_related_pids)

    if [[ -z "$all_pids" ]]; then
        log_info "MCP Server 已停止"
        rm -f "$PID_FILE"
        return 0
    fi

    for pid in $all_pids; do
        if kill -0 "$pid" 2>/dev/null; then
            log_info "终止进程: ${pid}"
            kill "$pid" 2>/dev/null || true
        fi
    done

    log_info "等待进程退出..."
    local wait_count=0
    while [[ $wait_count -lt 10 ]]; do
        if ! is_port_listening "$MCP_PORT"; then
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done

    if is_port_listening "$MCP_PORT"; then
        log_warn "进程未响应 SIGTERM，强制终止..."
        local remaining_pids
        remaining_pids=$(get_port_pids "$MCP_PORT")
        for pid in $remaining_pids; do
            kill -9 "$pid" 2>/dev/null || true
        done
        if command -v fuser &>/dev/null; then
            fuser -k "${MCP_PORT}/tcp" 2>/dev/null || true
        fi
        sleep 2
    fi

    rm -f "$PID_FILE"

    if is_port_listening "$MCP_PORT"; then
        log_error "端口 ${MCP_PORT} 仍被占用，无法停止"
        local stubborn_pids
        stubborn_pids=$(get_port_pids "$MCP_PORT")
        for pid in $stubborn_pids; do
            log_error "  残留进程: $(ps -p "$pid" -o pid,user,command --no-headers 2>/dev/null || echo "$pid")"
        done
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

    if [[ ! -d ".git" ]]; then
        log_warn "不是 git 仓库，跳过代码拉取"
        return 0
    fi

    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        log_warn "检测到未提交的更改，stash 后拉取..."
        git stash 2>&1 | while IFS= read -r line; do echo "  ${line}"; done
    fi

    local current_branch
    current_branch=$(git branch --show-current)
    log_info "当前分支: ${current_branch}"

    git fetch origin "$current_branch" 2>&1 | while IFS= read -r line; do echo "  ${line}"; done
    git reset --hard "origin/${current_branch}" 2>&1 | while IFS= read -r line; do echo "  ${line}"; done

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

    if is_port_listening "$MCP_PORT"; then
        log_warn "端口 ${MCP_PORT} 已被占用，尝试先停止..."
        do_stop
        if is_port_listening "$MCP_PORT"; then
            log_error "端口 ${MCP_PORT} 仍被占用，启动失败"
            return 1
        fi
    fi

    mkdir -p "$LOG_DIR"
    cd "$PROJECT_DIR"

    if has_systemd; then
        systemctl start "${SYSTEMD_SERVICE}"
        sleep 3

        if is_port_listening "$MCP_PORT"; then
            local real_pid
            real_pid=$(get_port_pids "$MCP_PORT" | head -1)
            echo "${real_pid}" > "$PID_FILE"
            log_info "MCP Server 启动成功！(systemd, 崩溃自动重启)"
            echo ""
            echo "  PID:   ${real_pid}"
            echo "  地址:  http://${MCP_HOST}:${MCP_PORT}"
            echo "  端点:  ${MCP_URL}/mcp"
            echo "  日志:  ${LOG_FILE}"
            echo "  管理:  systemctl {status|restart|stop} ${SYSTEMD_SERVICE}"
            echo ""
            return 0
        else
            log_error "systemd 启动后端口未监听，最近日志:"
            journalctl -u "${SYSTEMD_SERVICE}" --no-pager -n 10 2>/dev/null | while IFS= read -r line; do
                echo "  ${line}"
            done
            return 1
        fi
    fi

    nohup uv run clawdchat-mcp --transport streamable-http >> "$LOG_FILE" 2>&1 &
    local wrapper_pid=$!

    sleep 2

    local server_pid=""
    server_pid=$(get_port_pids "$MCP_PORT" | head -1)
    if [[ -z "$server_pid" ]]; then
        local child_pids
        child_pids=$(pgrep -P "$wrapper_pid" 2>/dev/null || true)
        if [[ -n "$child_pids" ]]; then
            server_pid=$(echo "$child_pids" | head -1)
        fi
    fi

    local track_pid="${server_pid:-$wrapper_pid}"
    echo "$track_pid" > "$PID_FILE"
    log_info "MCP Server 启动中 (PID: ${track_pid}, wrapper: ${wrapper_pid})..."
    log_warn "未检测到 systemd service，使用 nohup 模式（无崩溃自动重启）"

    local wait_count=0
    local started=false
    while [[ $wait_count -lt 20 ]]; do
        sleep 1
        wait_count=$((wait_count + 1))

        if is_port_listening "$MCP_PORT"; then
            started=true
            local real_pid
            real_pid=$(get_port_pids "$MCP_PORT" | head -1)
            if [[ -n "$real_pid" ]] && [[ "$real_pid" != "$track_pid" ]]; then
                echo "$real_pid" > "$PID_FILE"
                track_pid="$real_pid"
            fi
            break
        fi

        if [[ -n "$server_pid" ]] && ! kill -0 "$server_pid" 2>/dev/null; then
            if ! kill -0 "$wrapper_pid" 2>/dev/null; then
                log_error "进程已退出，启动失败！最近日志:"
                tail -20 "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do
                    echo "  ${line}"
                done
                rm -f "$PID_FILE"
                return 1
            fi
        fi
    done

    if [[ "$started" != "true" ]]; then
        log_error "启动超时（20秒），端口 ${MCP_PORT} 仍未监听"
        log_error "最近日志:"
        tail -10 "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do
            echo "  ${line}"
        done
        rm -f "$PID_FILE"
        return 1
    fi

    log_info "MCP Server 启动成功！"
    echo ""
    echo "  PID:   ${track_pid}"
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

    if is_port_listening "$MCP_PORT"; then
        echo -e "  端口 ${MCP_PORT}: ${GREEN}已监听${NC}"
        local port_pids
        port_pids=$(get_port_pids "$MCP_PORT")
        if [[ -n "$port_pids" ]]; then
            for pid in $port_pids; do
                echo "    实际监听进程: $(ps -p "$pid" -o pid,command --no-headers 2>/dev/null || echo "$pid")"
            done
        fi
    else
        echo -e "  端口 ${MCP_PORT}: ${RED}未监听${NC}"
    fi

    echo "  配置:"
    echo "    HOST: ${MCP_HOST}"
    echo "    PORT: ${MCP_PORT}"
    echo "    URL:  ${MCP_URL}"

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

    local healthy=true

    # 1. 端口检查（最可靠的指标）
    if is_port_listening "$MCP_PORT"; then
        local port_pid
        port_pid=$(get_port_pids "$MCP_PORT" | head -1)
        log_info "端口 ${MCP_PORT} 正常监听 (PID: ${port_pid:-unknown})"

        # 自动修复 PID 文件
        if [[ -n "$port_pid" ]]; then
            local saved_pid
            saved_pid=$(get_saved_pid)
            if [[ "$saved_pid" != "$port_pid" ]]; then
                echo "$port_pid" > "$PID_FILE"
                log_info "已更新 PID 文件: ${saved_pid:-空} → ${port_pid}"
            fi
        fi
    else
        log_error "端口 ${MCP_PORT} 未监听"
        healthy=false
    fi

    # 2. HTTP 检查
    if [[ "$healthy" == "true" ]]; then
        local http_code
        http_code=$(curl -s -o /dev/null -w '%{http_code}' \
            --max-time 5 \
            "http://${MCP_HOST}:${MCP_PORT}/.well-known/oauth-authorization-server" 2>/dev/null || echo "000")

        if [[ "$http_code" == "200" ]]; then
            log_info "HTTP 健康检查通过 (OAuth metadata: 200)"
        elif [[ "$http_code" == "000" ]]; then
            log_warn "HTTP 连接失败（可能服务仍在启动中）"
        else
            log_warn "HTTP 返回 ${http_code}（非 200，但服务可能正常）"
        fi

        http_code=$(curl -s -o /dev/null -w '%{http_code}' \
            --max-time 5 \
            "http://${MCP_HOST}:${MCP_PORT}/mcp" 2>/dev/null || echo "000")

        if [[ "$http_code" == "401" ]]; then
            log_info "MCP 端点正常 (返回 401 需认证，符合预期)"
        elif [[ "$http_code" == "000" ]]; then
            log_warn "MCP 端点连接失败"
            healthy=false
        else
            log_info "MCP 端点返回: ${http_code}"
        fi
    fi

    echo ""
    if [[ "$healthy" == "true" ]]; then
        log_info "健康检查通过 ✓"
        return 0
    else
        log_error "健康检查失败 ✗"
        return 1
    fi
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
