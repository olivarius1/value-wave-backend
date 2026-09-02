---
name: stock-valuation-skill
description: A股估值分析Skill，自动获取东方财富年报/季报数据+腾讯财经10年K线，生成自包含HTML估值回测报告。支持8种行业模型、19种估值因子、财务报表自动校准、浅灰色面积图、全屏图表。
---

# A股估值系统 Skill

基于腾讯财经K线数据和东方财富财务报表API，自动生成单文件自包含HTML估值分析报告的独立Skill。完全独立，不依赖后端系统代码或数据库。

## 功能特性

- **8种行业估值模型**：必选消费、可选消费、科技制造、周期资源、央企基建、银行保险、地产、医药消费
- **19种估值因子**：6种基础因子（PE、PB、PEG、MA偏离、量能、波动率）+ 13种可选因子（ROE、股息率、研发费用率、毛利率稳定性、品牌溢价度、NAV折价、去化率、杠杆率、营收增速、订单增速、商品价格偏离、产能利用率、不良率）
- **财务报表自动获取**：从东方财富API自动拉取历年年报、半年报、季报，自动计算并填充ROE、毛利率稳定性、营收增速等因子
- **EPS/BPS 逐日滚动重述**：拉取东财分红送配明细，按送转/派息事件对历史 EPS/BPS 逐日重述（叠加20年财报窗口），消除5月年报切换的口径悬崖；覆盖检测在生效年缺失或送转与盈利跳变矛盾时告警
- **10年K线自动获取**：未提供K线文件时自动从腾讯财经API拉取最近10年日K线数据（不足则用最长可用数据）；历史 PE/PB 用当日不复权真实价计算
- **行业自动识别**：东财 F10 公司概况 EM2016 行业分类自动填入报告；获取失败时输出中性表述（不出现未知行业）
- **估值消化曲线**：高估档且最新报告期净利同比 ≥ 阈值（--digest-growth，默认 60%）时，按 EPS×(1+g) 等效压缩 PE/PEG 因子同口径重算全曲线，紫色虚线叠加展示、tooltip 与测算卡同步呈现
- **章节导航栏**：sticky 顶部导航（业务全景/评分模型/回测曲线/关键时点/逻辑风险），平滑滚动 + 当前章节高亮
- **可选因子权重归一化**：缺失可选因子时权重自动均分到已有因子
- **兼容旧模型**：`growth` 别名映射到 `staples`
- **自包含HTML输出**：内联ECharts库，无需外部依赖，单文件可直接在浏览器打开
- **全屏图表**：支持横屏查看、十字轴光标、固定tooltip
- **历史百分位**：20th/80th百分位参考线，tooltip显示历史百分位
- **浅灰色面积图**：收盘价以浅灰色渐变面积图作为背景展示
- **财务报表摘要卡片**：报告中展示可用年报数、近5年平均ROE、近5年平均毛利率、5年营收CAGR等
- **5级评分系统**：极度低估(80-100)、低估(70-79)、无交易价值(40-69)、高估(20-39)、极度高估(0-19)

## 文件结构

```
stock-valuation-skill/
├── SKILL.md              # Skill 描述文件（即本文件）
├── scripts/
│   ├── build_report.py        # 报告生成入口（argparse + 自动获取 + 缓存）
│   ├── report_generator.py    # 核心报告生成器（完全独立，不依赖后端）
│   ├── kline_cache.py         # K线缓存与增量更新模块
│   ├── financial_fetcher.py   # 财务报表数据获取器（东方财富API）
│   ├── scan_watchlist.py      # watchlist 快速扫描（终端打分表）
│   ├── batch_rebuild.py       # 批量重建报告（watchlist.txt 驱动）
│   ├── summary_report.py      # 估值汇总筛选报告
├── _shared/
│   └── js/
│       └── echarts.min.js     # ECharts 库（内联到报告中）
└── templates/
    ├── growth_params.md        # 各行业估值模型参数参考
    └── thinking_flow.md        # 分析思维流程
```

## 依赖

- Python 3.6+
- 网络访问（用于东方财富和腾讯财经API）

## 使用方式

### 单只报告生成

```bash
python scripts/build_report.py <股票代码> --model <模型类型>

# 示例
python scripts/build_report.py 600887 --model staples
python scripts/build_report.py 601899 --model cyclical --pe 10 35 --growth 0.12
python scripts/build_report.py 600887 --model staples --no-cache  # 强制全量刷新
```

所有参数自动获取：股票名称、交易所、总股本、PE/PB区间（10th/90th百分位）、营收、净利润、毛利率、市值、行业、预期增速。
K线数据自动缓存，第二次运行同一股票只需几秒（增量更新）。

**可选覆盖参数：**
- `--pe MIN MAX`：手动PE区间（覆盖自动计算）
- `--pb MIN MAX`：手动PB区间
- `--growth 0.12`：手动预期增速
- `--name "名称"`：手动股票名称
- `--subtitle "副标题"`：报告副标题
- `--no-cache`：强制全量刷新K线
- `--dps 1.2`：每股年分红（股息率因子历史逐日动态化）
- `--digest-growth 0.60`：估值消化曲线触发阈值（默认 0.60）
- `--roe:0.15 --rd_ratio:0.08`：可选因子（`--key:value`格式）

### 批量生成

```bash
python scripts/batch_rebuild.py                          # 全量重建 watchlist
python scripts/batch_rebuild.py --stocks 601799,600887   # 指定代码子集
python scripts/batch_rebuild.py --model tech,cyclical    # 按模型过滤
python scripts/batch_rebuild.py --dry-run                # 只打印将执行的命令
python scripts/batch_rebuild.py --summary                # 完成后刷新估值汇总筛选.html
python scripts/batch_rebuild.py --retry                  # 只重跑上次失败项（.cache/batch_failed.txt）
```

股票池唯一来源 watchlist.txt（CSV：名称,代码,模型,最后报告时间）；扫描/重建/汇总三个入口同源解析。
`python scripts/scan_watchlist.py` 为终端快速扫描（不生成报告）；`python scripts/summary_report.py` 生成汇总筛选页。

**8种模型类型**：`staples`(必选消费) / `discretionary`(可选消费) / `tech`(科技制造) / `cyclical`(周期资源) / `soe`(央企基建) / `bank`(银行保险) / `realestate`(地产) / `pharma`(医药消费)

**可选因子参数格式**（`--key:value`）：

| 因子 | 参数键 | 示例 | 适用模型 |
|------|--------|------|----------|
| ROE | `--roe` | `--roe:0.15` | soe, bank |
| 股息率 | `--div_yield` | `--div_yield:0.05` | soe, bank |
| 研发费用率 | `--rd_ratio` | `--rd_ratio:0.08` | tech |
| 毛利率稳定性 | `--margin_stability` | `--margin_stability:0.02` | staples |
| 品牌溢价度 | `--brand_premium` | `--brand_premium:2.0` | discretionary |
| 不良率 | `--npl_ratio` | `--npl_ratio:0.012` | bank |
| NAV折价 | `--nav_discount` | `--nav_discount:0.6` | realestate |
| 去化率 | `--clearance_rate` | `--clearance_rate:0.7` | realestate |
| 杠杆率 | `--leverage` | `--leverage:0.4` | realestate |
| 营收增速 | `--revenue_growth` | `--revenue_growth:0.2` | pharma |
| 订单增速 | `--order_growth` | `--order_growth:0.15` | soe |
| 商品价格偏离 | `--commodity_dev` | `--commodity_dev:-0.05` | cyclical |
| 产能利用率 | `--capacity_util` | `--capacity_util:0.75` | cyclical |

> 未指定的可选因子将自动从东方财富财务报表数据中计算填充（ROE、毛利率稳定性、营收增速）。

## 数据来源

- **腾讯财经K线API**：`http://web.ifzq.gtimg.cn/appstock/app/fqkline/get`
  - 每次最多返回500天数据，脚本自动分批获取
  - 必须带 `User-Agent` 头
- **东方财富数据中心API**：`https://datacenter.eastmoney.com/securities/api/data/v1/get`
  - RPT_F10_FINANCE_MAINFINADATA：历年年报/半年报/季报核心财务指标（20年窗口，重述与区间计算依据）
  - RPT_SHAREBONUS_DET：分红送配明细（送转/派息事件，逐日重述依据）
  - RPT_F10_BASIC_ORGINFO：F10 公司概况（EM2016 行业分类）
  - push2 域名接口间歇性拒连，不作为数据源

## 模型选择校验（重要，2026-08 复盘新增）

行业归类后**必须用财务特征二次确认**，反例清单：

- 高股息承诺（分红规划≥30%）+ 央国企 + 现金充裕 → 即使行业有周期，优先 `soe` 而非 `cyclical`
- ROE<8% 且微利 → 不适用 `soe`（soe 语义=高股息央企，因子会缺失）
- 负增速 → 禁用 `tech`/`pharma`（PEG 失真）
- 周期股必须提供 `--commodity_dev`，缺失时权重会摊到 PE 放大失真
- 盈利 regime 切换（近5年 vs 前5年净利中枢变化>100%）→ PE/PB 区间只用切换后年份或手动指定

**交付前结果校验**：近 2 年评分单一档位占比 >70%、或 PE<10+股息率>5% 却被评"高估" → 必是参数锚定问题，先修正再交付。

详细规则见 `templates/thinking_flow.md`（2.1.1 二次确认、4.3 结果校验）与 `templates/growth_params.md`（第八节 regime 校准）。

## 输出

- 报告输出到项目根目录的 `artifacts/reports/` 文件夹（JSON 数据中间件同目录树 `artifacts/json_data/`）
- 文件名格式：`{股票名称}{股票代码}-valuation.html`
- 单文件自包含（内联ECharts），可直接在浏览器打开
