#!/bin/bash

# ClawdChat MCP Server - 统一管理脚本
# 用法: ./mcp.sh {start|stop|status|restart} [mode]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=".mcp-server.pid"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/server.log"
ERROR_LOG="$LOG_DIR/error.log"

# ==================== 启动服务 ====================
start_server() {
    local TRANSPORT_MODE="${1:-streamable-http}"
    
    mkdir -p "$LOG_DIR"
    
    # 检查是否已经在运行
    if [ -f "$PID_FILE" ]; then
        local OLD_PID=$(cat "$PID_FILE")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo "❌ MCP Server 已经在运行 (PID: $OLD_PID)"
            echo "如需重启，请运行: $0 restart"
            return 1
        else
            echo "⚠️  发现过期的 PID 文件，清理中..."
            rm -f "$PID_FILE"
        fi
    fi
    
    echo "🚀 启动 ClawdChat MCP Server (模式: $TRANSPORT_MODE)..."
    
    # 后台启动服务（stdout 和 stderr 都输出到同一个日志文件）
    if [ "$TRANSPORT_MODE" = "stdio" ]; then
        nohup uv run python main.py >> "$LOG_FILE" 2>&1 &
    else
        nohup uv run python main.py --transport streamable-http >> "$LOG_FILE" 2>&1 &
    fi
    
    local SERVER_PID=$!
    echo $SERVER_PID > "$PID_FILE"
    
    sleep 2
    
    # 检查服务是否成功启动
    if ps -p "$SERVER_PID" > /dev/null 2>&1; then
        echo "✅ MCP Server 启动成功!"
        echo "   PID: $SERVER_PID"
        echo "   模式: $TRANSPORT_MODE"
        echo "   日志文件: $LOG_FILE"
        echo ""
        echo "📝 查看日志: tail -f $LOG_FILE"
        echo "🛑 停止服务: $0 stop"
        echo "📊 查看状态: $0 status"
    else
        echo "❌ 服务启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

# ==================== 停止服务 ====================
stop_server() {
    if [ ! -f "$PID_FILE" ]; then
        echo "⚠️  未找到运行中的 MCP Server"
        return 0
    fi
    
    local PID=$(cat "$PID_FILE")
    
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "⚠️  进程 $PID 不存在，清理 PID 文件..."
        rm -f "$PID_FILE"
        return 0
    fi
    
    echo "🛑 正在停止 MCP Server (PID: $PID)..."
    
    # 尝试优雅关闭
    kill "$PID"
    
    # 等待进程结束
    for i in {1..10}; do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            echo "✅ MCP Server 已停止"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done
    
    # 如果还没停止，强制杀掉
    echo "⚠️  优雅关闭超时，强制停止..."
    kill -9 "$PID" 2>/dev/null
    
    sleep 1
    
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "✅ MCP Server 已强制停止"
        rm -f "$PID_FILE"
    else
        echo "❌ 无法停止进程 $PID"
        return 1
    fi
}

# ==================== 查看状态 ====================
show_status() {
    echo "================================================"
    echo "  ClawdChat MCP Server 状态"
    echo "================================================"
    echo ""
    
    if [ ! -f "$PID_FILE" ]; then
        echo "状态: ⚫ 未运行"
        echo ""
        echo "💡 启动服务: $0 start [stdio|streamable-http]"
        return 0
    fi
    
    local PID=$(cat "$PID_FILE")
    
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "状态: ⚠️  异常 (PID 文件存在但进程不存在)"
        echo "PID 文件: $PID"
        echo ""
        echo "💡 清理并重启: $0 stop && $0 start"
        return 1
    fi
    
    # 获取进程信息
    local PROCESS_INFO=$(ps -p "$PID" -o pid,ppid,etime,rss,cmd --no-headers)
    local MEMORY=$(echo "$PROCESS_INFO" | awk '{print $4}')
    local MEMORY_MB=$((MEMORY / 1024))
    local UPTIME=$(echo "$PROCESS_INFO" | awk '{print $3}')
    
    echo "状态: 🟢 运行中"
    echo "PID: $PID"
    echo "运行时间: $UPTIME"
    echo "内存占用: ${MEMORY_MB} MB"
    echo ""
    echo "进程详情:"
    echo "$PROCESS_INFO"
    echo ""
    
    # 日志文件信息
    if [ -f "$LOG_FILE" ]; then
        local LOG_SIZE=$(du -h "$LOG_FILE" | cut -f1)
        local LOG_LINES=$(wc -l < "$LOG_FILE")
        echo "日志文件: $LOG_FILE"
        echo "  大小: $LOG_SIZE"
        echo "  行数: $LOG_LINES"
        
        # 显示最后几行日志
        if [ "$LOG_LINES" -gt 0 ]; then
            echo ""
            echo "📋 最近的日志 (最后 5 行):"
            echo "---"
            tail -n 5 "$LOG_FILE"
        fi
        echo ""
    fi
    
    echo "================================================"
    echo "📝 查看实时日志: tail -f $LOG_FILE"
    echo "🛑 停止服务: $0 stop"
    echo "🔄 重启服务: $0 restart"
    echo "================================================"
}

# ==================== 重启服务 ====================
restart_server() {
    echo "🔄 重启 MCP Server..."
    echo ""
    stop_server
    sleep 1
    start_server "$@"
}

# ==================== 显示帮助 ====================
show_help() {
    cat << EOF
ClawdChat MCP Server 管理工具

用法:
    $0 <command> [options]

命令:
    start [mode]    启动服务（后台运行）
                    mode: stdio | streamable-http (默认: streamable-http)
    
    stop            停止服务
    
    status          查看服务状态
    
    restart [mode]  重启服务
                    mode: stdio | streamable-http (默认: streamable-http)
    
    help            显示此帮助信息

示例:
    $0 start                    # 启动服务 (HTTP 模式)
    $0 start stdio              # 启动服务 (stdio 模式)
    $0 stop                     # 停止服务
    $0 status                   # 查看状态
    $0 restart                  # 重启服务
    
    tail -f logs/server.log     # 查看实时日志

EOF
}

# ==================== 主函数 ====================
main() {
    local COMMAND="${1:-help}"
    shift || true
    
    case "$COMMAND" in
        start)
            start_server "$@"
            ;;
        stop)
            stop_server
            ;;
        status)
            show_status
            ;;
        restart)
            restart_server "$@"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            echo "❌ 未知命令: $COMMAND"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

main "$@"
