#!/bin/bash
# 量化交易启动脚本

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 创建必要目录
mkdir -p logs data

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到python3"
    exit 1
fi

# 检查依赖
echo "检查依赖..."
pip3 install -q -r requirements.txt

# 检查配置文件
if [ ! -f "config.json" ]; then
    echo "错误: 未找到config.json配置文件"
    echo "请复制 config.example.json 为 config.json 并配置API密钥"
    exit 1
fi

echo "=========================================="
echo "  Bybit 量化交易系统"
echo "  访问地址: http://localhost:8000"
echo "=========================================="
echo ""

# 使用nohup后台运行，防止终端关闭后进程结束
nohup python3 -u main.py >> logs/stdout.log 2>> logs/stderr.log &
PID=$!
echo $PID > quant.pid

echo "✅ 程序已启动，PID: $PID"
echo "日志文件: logs/stdout.log, logs/stderr.log"
echo ""
echo "常用命令:"
echo "  查看日志: tail -f logs/stdout.log"
echo "  停止程序: ./stop.sh 或 kill $PID"

