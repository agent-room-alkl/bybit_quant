#!/bin/bash
# 查看量化交易状态

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "  量化交易系统状态"
echo "=========================================="

# 检查PID文件
if [ -f "quant.pid" ]; then
    PID=$(cat quant.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "✅ 状态: 运行中"
        echo "   PID: $PID"
        
        # 显示运行时间
        UPTIME=$(ps -o etime= -p $PID)
        echo "   运行时间: $UPTIME"
        
        # 显示内存使用
        MEM=$(ps -o rss= -p $PID | awk '{print $1/1024 " MB"}')
        echo "   内存使用: $MEM"
    else
        echo "❌ 状态: 已停止（PID文件存在但进程不存在）"
    fi
else
    # 尝试查找进程
    PID=$(pgrep -f "python.*main.py")
    if [ -n "$PID" ]; then
        echo "✅ 状态: 运行中（无PID文件）"
        echo "   PID: $PID"
    else
        echo "❌ 状态: 未运行"
    fi
fi

echo ""
echo "最近日志 (最后10行):"
echo "------------------------------------------"
if [ -f "logs/trading.log" ]; then
    tail -10 logs/trading.log
else
    echo "无日志文件"
fi

echo ""
echo "最近错误 (最后5行):"
echo "------------------------------------------"
if [ -f "logs/error.log" ]; then
    tail -5 logs/error.log
else
    echo "无错误日志"
fi

