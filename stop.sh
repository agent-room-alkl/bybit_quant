#!/bin/bash
# 停止量化交易脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f "quant.pid" ]; then
    PID=$(cat quant.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "正在停止进程 PID: $PID"
        kill $PID
        sleep 2
        if ps -p $PID > /dev/null 2>&1; then
            echo "进程未响应，强制终止..."
            kill -9 $PID
        fi
        echo "✅ 程序已停止"
    else
        echo "进程 $PID 不存在"
    fi
    rm -f quant.pid
else
    # 尝试查找进程
    PID=$(pgrep -f "python.*main.py")
    if [ -n "$PID" ]; then
        echo "找到进程 PID: $PID，正在停止..."
        kill $PID
        echo "✅ 程序已停止"
    else
        echo "未找到运行中的程序"
    fi
fi

