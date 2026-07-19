#!/bin/bash
# fetch_kline.sh - 获取A股K线数据（前复权）
# 用法: ./fetch_kline.sh <交易所><代码> <输出前缀>
# 示例: ./fetch_kline.sh sh600938 kline_zghy

set -e
CODE=$1
PREFIX=$2

echo "获取 ${CODE} K线数据..."

curl -sL -H "User-Agent: Mozilla/5.0" \
  "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${CODE},day,2024-07-09,2025-07-09,500,qfq" \
  -o "${PREFIX}_2024_2025.json"

curl -sL -H "User-Agent: Mozilla/5.0" \
  "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${CODE},day,2025-07-09,2027-07-09,500,qfq" \
  -o "${PREFIX}_2025_2027.json"

echo "完成: ${PREFIX}_2024_2025.json, ${PREFIX}_2025_2027.json"
