#!/usr/bin/env bash
# 启动 Web 控制台（本地单用户工具）
# 用法: bash webapp/start.sh [端口]   默认 127.0.0.1:8642
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8642}"
cd "$DIR"
python3 manage.py migrate --noinput
echo ""
echo "  估值控制台:  http://127.0.0.1:${PORT}/"
echo "  数据管理     http://127.0.0.1:${PORT}/data/"
echo "  分数面板     http://127.0.0.1:${PORT}/scores/"
echo "  报告浏览     http://127.0.0.1:${PORT}/report/"
echo ""
exec python3 manage.py runserver "127.0.0.1:${PORT}" --noreload
