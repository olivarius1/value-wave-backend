# A股估值报告生成工具

纯算法驱动的A股估值分析工具，自动获取腾讯财经10年K线 + 东方财富财务报表数据，生成自包含HTML估值回测报告。

**无需部署后端服务、无需数据库、无需AI，本地 Python 即可运行。**

## 环境要求

- Python 3.6+ 
- 网络访问（腾讯财经 + 东方财富 API）

## 快速开始

```bash
cd D:/myLab/trader/stock-valuation-skill

# 最简用法：仅2个必填参数
python scripts/build_report.py 600887 --model staples
```

输出：`local_reports/伊利股份600887-valuation.html`（浏览器直接打开）

所有参数自动获取：股票名称、交易所、总股本、PE/PB区间（10th/90th百分位）、营收、净利润、毛利率、市值、行业、预期增速。

## 参数说明

```
python scripts/build_report.py <股票代码> --model <模型类型> [可选参数]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `code` | ✅ | 股票代码，如 `600887` |
| `--model` | ✅ | 行业估值模型，见下表 |
| `--pe MIN MAX` | | 手动PE区间（覆盖自动计算） |
| `--pb MIN MAX` | | 手动PB区间 |
| `--growth` | | 手动预期增速（默认用近5年净利润CAGR） |
| `--name` | | 手动股票名称 |
| `--subtitle` | | 报告副标题 |
| `--no-cache` | | 强制全量刷新K线（跳过缓存） |
| `--key:value` | | 可选因子，如 `--roe:0.15` |

## K线缓存与增量更新

- **首次运行**：全量获取10年K线（~30秒），缓存到 `local_reports/.cache/`
- **同日重复运行**：直接使用缓存，0次API调用
- **次日运行**：仅增量获取新数据（1次API调用，~2秒）
- `--no-cache`：强制全量刷新

## 8种行业模型

| 模型 | 名称 | 核心因子 |
|------|------|----------|
| `staples` | 必选消费 | PE + PEG + 毛利率稳定性 |
| `discretionary` | 可选消费 | PE + PEG + 品牌溢价度 |
| `tech` | 科技制造 | PEG + 研发费用率 |
| `cyclical` | 周期资源 | PE + 商品价格偏离 + 产能利用率 |
| `soe` | 央企基建 | PB + 股息率 + 订单增速 + ROE |
| `bank` | 银行保险 | PB + ROE + 股息率 + 不良率 |
| `realestate` | 地产 | NAV折价 + 去化率 + 杠杆率 |
| `pharma` | 医药消费 | PEG + 营收增速 |

## 可选因子

未手动指定的可选因子会**自动从东方财富财报数据计算填充**（ROE、毛利率稳定性、营收增速）。

| 因子 | 参数 | 示例 | 适用模型 |
|------|------|------|----------|
| ROE | `--roe:0.15` | 近10年报均值 | soe, bank |
| 股息率 | `--div_yield:0.05` | 年度股息/股价 | soe, bank |
| 研发费用率 | `--rd_ratio:0.08` | 研发/营收 | tech |
| 毛利率稳定性 | `--margin_stability:0.02` | 毛利率标准差 | staples |
| 品牌溢价度 | `--brand_premium:2.0` | PB/行业均PB | discretionary |
| 不良率 | `--npl_ratio:0.012` | 不良贷款率 | bank |
| NAV折价 | `--nav_discount:0.6` | 市值/NAV | realestate |
| 去化率 | `--clearance_rate:0.7` | 销售/推盘 | realestate |
| 杠杆率 | `--leverage:0.4` | 负债/资产 | realestate |
| 营收增速 | `--revenue_growth:0.2` | 营收同比 | pharma |
| 订单增速 | `--order_growth:0.15` | 新签/在手 | soe |
| 商品价格偏离 | `--commodity_dev:-0.05` | 现价/均价-1 | cyclical |
| 产能利用率 | `--capacity_util:0.75` | 实际/设计产能 | cyclical |

## 使用示例

### 新格式（推荐）

```bash
# 最简：自动获取一切
python scripts/build_report.py 600887 --model staples

# 手动覆盖PE区间和增速
python scripts/build_report.py 601899 --model cyclical --pe 10 35 --growth 0.12

# 带可选因子
python scripts/build_report.py 601899 --model cyclical --commodity_dev:-0.05 --capacity_util:0.85

# 强制全量刷新K线
python scripts/build_report.py 600887 --model staples --no-cache
```

### 旧格式（兼容，16+位置参数）

```bash
python scripts/build_report.py 600887 "伊利股份" sh 63.25 \
  15 35 2.0 5.0 0.08 \
  "1156.36" "115.65" "34%" "1571" \
  "乳制品龙头" "乳制品龙头估值框架与10年回测" \
  staples
```

### 批量生成

```bash
# 创建配置文件 stocks.csv
cat > stocks.csv << 'EOF'
600887,伊利股份,sh,63.25,15,35,2.0,5.0,0.08,1156.36,115.65,34%,1571,乳制品龙头,乳制品龙头估值框架,staples
601899,紫金矿业,sh,265.91,10,35,1.5,6.0,0.12,3490.8,517.77,27.7%,7666,有色金属采选,有色金属龙头估值框架,cyclical
EOF

bash scripts/batch_build.sh stocks.csv
```

## 数据来源

| 数据 | API | 说明 |
|------|-----|------|
| K线 | 腾讯财经 `web.ifzq.gtimg.cn` | 前复权日K，自动分批获取10年 |
| 财务报表 | 东方财富 `datacenter.eastmoney.com` | 年报/半年报/季报核心指标 |

## 输出说明

- 输出目录：项目根目录 `local_reports/`
- 文件名：`{股票名称}{股票代码}-valuation.html`
- 格式：单文件自包含HTML（内联ECharts），浏览器直接打开
- 内容：10年估值回测曲线、当前评分、历史百分位、财务报表摘要

## 评分体系

| 分数区间 | 状态 |
|----------|------|
| 80-100 | 极度低估 |
| 70-79 | 低估 |
| 40-69 | 无交易价值 |
| 20-39 | 高估 |
| 0-19 | 极度高估 |

## 目录结构

```
stock-valuation-skill/
├── SKILL.md                 # AI Agent 调用契约
├── README.md                # 本文件
├── scripts/
│   ├── build_report.py      # 入口脚本（argparse + 自动获取 + 缓存）
│   ├── report_generator.py  # 核心生成器（1100行，纯算法）
│   ├── kline_cache.py       # K线缓存与增量更新
│   ├── financial_fetcher.py # 东方财富财报获取 + 估值区间计算
│   ├── fetch_kline.sh       # 腾讯K线获取
│   └── batch_build.sh       # 批量构建
├── _shared/js/
│   └── echarts.min.js       # ECharts（内联到HTML）
└── templates/
    ├── growth_params.md     # 行业参数参考
    └── thinking_flow.md     # 分析思维流程
```
