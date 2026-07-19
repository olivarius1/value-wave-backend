#!/bin/bash
# batch_build.sh - 批量获取K线并生成估值报告
# 用法: ./batch_build.sh <配置JSON文件>
# 配置文件每行一个股票，格式：
# 代码,名称,交易所,总股本,PE最低,PE最高,PB最低,PB最高,增速,营收,净利润,毛利率,市值,行业,副标题,模型

set -e
CONFIG=$1
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$CONFIG" ]; then
  echo "用法: $0 <配置文件>"
  echo ""
  echo "配置文件每行一个股票，格式："
  echo "代码,名称,交易所,总股本,PE最低,PE最高,PB最低,PB最高,增速,营收,净利润,毛利率,市值,行业,副标题,模型"
  echo ""
  echo "示例:"
  echo "600938,中国海油,sh,475.30,6,18,0.8,3.0,0.06,3982.2,1220.82,51.17%,13671,海上油气,央企龙头,cyclical"
  exit 1
fi

while IFS=',' read -r code name exchange shares pe_min pe_max pb_min pb_max growth rev profit margin mcap industry subtitle model; do
  [ -z "$code" ] && continue
  [[ "$code" == \#* ]] && continue

  echo "=== 生成 ${name}(${code}) 报告 ==="
  python3 "${SCRIPT_DIR}/build_report.py" \
    "$code" "$name" "$exchange" "$shares" \
    "$pe_min" "$pe_max" "$pb_min" "$pb_max" "$growth" \
    "$rev" "$profit" "$margin" "$mcap" \
    "$industry" "$subtitle" "$model"

  echo ""
done < "$CONFIG"

echo "全部完成！报告位于 local_reports/ 目录。"
