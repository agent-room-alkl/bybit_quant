#!/bin/bash
# 重启量化交易脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "正在重启..."
./stop.sh
sleep 2
./start.sh

