---
name: stock-valuation-skill
description: A股估值分析Skill，自动获取同花顺iFinD年报/季报数据（东财兜底）+腾讯财经10年K线，生成自包含HTML估值回测报告。支持8种行业模型（每模型5因子，≤7硬约束）、盈利换挡自动检测、浅灰色面积图、全屏图表。
---

# A股估值系统 Skill

基于腾讯财经K线数据和同花顺iFinD财务报表API（东财自动兜底），自动生成单文件自包含HTML估值分析报告的独立Skill。完全独立，不依赖后端系统代码或数据库。

## 功能特性

- **8种行业估值模型**：必选消费、可选消费、科技制造、周期资源、央企基建、银行保险、地产、医药消费（每模型 5 个因子，硬约束 ≤7，2026-09 审计后）
- **估值因子**：4种基础因子（PE、PB、PEG、MA偏离）+ 13种可选因子（ROE、股息率、研发费用率、毛利率稳定性、品牌溢价度、NAV折价、去化率、杠杆率、营收增速、订单增速、商品价格偏离、产能利用率、不良率）；量能/波动率因子经回测 IC 审计后已从预设移除（函数保留）
- **财务报表自动获取**：从 iFinD PIT时点指标自动拉取历年年报、半年报、季报（含上市前历史；东财为自动兜底），自动计算并填充ROE（近5年报均值）、毛利率稳定性、营收增速等因子
- **EPS/BPS 逐日滚动重述**：拉取东财分红送配明细，按送转/派息事件对历史 EPS/BPS 逐日重述（叠加20年财报窗口），消除5月年报切换的口径悬崖；覆盖检测在生效年缺失或送转与盈利跳变矛盾时告警
- **盈利换挡自动检测**：年报净利润按首年对齐每 5 年一段取中位数，相邻段比值 >3 判定向上换挡；命中时 PE 分位只用换挡生效后子序列（对齐年报披露次年 5 月起），PB 与其余因子维持全历史；向下回落/亏损段/历史不足仅标注不切窗（meta.window_reason，亏损段报告中有醒目警示）
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
│   ├── financial_fetcher.py   # 财务报表数据获取器（iFinD主源+东财兜底）
│   ├── ths_fetcher.py         # iFinD 财报预热CLI（PIT时点指标批量拉取）
│   ├── factor_analysis.py     # 因子级IC与引擎版本对比
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
- 网络访问（用于iFinD/东财和腾讯财经API，iFinD需 `artifacts/.cache/ths_credentials.json` 凭证）

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
| 股息率 | `--dps`（推荐，逐日动态）或 `--dividend_yield`（恒定兜底） | `--dps 0.80` | soe, bank, cyclical |
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
| 产能利用率 | `--capacity_util` | `--capacity_util:0.75` | （当前预设不使用，键保留兼容） |

> 未指定的可选因子将自动从 iFinD 财报数据中计算填充（ROE 近5年报均值、毛利率稳定性、营收增速）。

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
- 盈利换挡由引擎自动检测（年报净利 5 年段中位数，相邻段比值 >3 只向上切窗）：PE 分位只用换挡后子序列（披露对齐次年 5 月起），PB 维持全历史；向下回落/亏损段仅标注不切窗，无需人工指定区间

**交付前结果校验**：近 2 年评分单一档位占比 >70%、或 PE<10+股息率>5% 却被评"高估" → 必是参数锚定问题，先修正再交付。

详细规则见 `templates/thinking_flow.md`（2.1.1 二次确认、4.3 结果校验）与 `templates/growth_params.md`（第八节盈利换挡）。

## 回测结果解读沟通规范（2026-09-11 用户提问复盘，聊天解读与报告正文同适用）

复盘发现：用户连环追问的根源是"术语未定义、同名多口径、数字无口径标签、修辞偏离数据"。规则：

1. **术语首次出现就地一句话解释**，禁止裸用：选股端/策略端、等权基准、IC、Spearman、周频采样、分层、单调性、网格搜索、任意起点(rolling-entry)、超额、中位数、PIT/未来函数、多重比较、幸存者偏差。自造词优先用大白话替代（如第一次出现用"闭眼持有"解释等权基准）。
2. **同名词多口径，第一次就列表区分**：①三个"胜率"——选股胜率(单只样本N日后上涨概率)/持有期胜率(任意起点N年跑赢基准概率)/日胜率(组合单日上涨天数占比)；②两个"百分位"——分数内部 PE/PB 区间的 10th/90th vs 策略买卖线的 95/10；③"样本数 n"是(股票×日期)观测次数，不是天数也不是股票数。
3. **数字必须带口径标签**："+28.1%"要说明是超额的百分点差不是收益率；中位数=排队取中间、代表典型体验、抗极端值；注明 全期/滚动1y3y、含成本/不含成本、20年回测口径/10年报告口径。
4. **相对指标≠绝对收益**：IC/超额只保证相对排序/相对差距，高IC≠赚钱（反例：2010-2012熊市 IC+0.310 但 ≥70 桶均值 -1.5%）。每个相对结论后跟一句适用边界。
5. **修辞口号必须先过数据复核**："熊市空仓+低估满仓"曾被实测证伪（全期最高仓位仅54%，空仓只在泡沫顶部涌现，2018顶仅减到37%）。可用版本："顶部凭百分位清仓、主跌段低仓位、跌透后重新建仓"。
6. **统计概念默认读者无量化背景**：各配一句大白话+一个3-5行小例子（IC=方向对不对、Spearman=只比排名抗极端值、周频采样=相邻日样本重叠会虚高、中位数=不被极端值带偏）。已有术语表在 docs/backtest_guide.md，解读时复用其语言。
7. **先答问题本身，再给机制**：问"哪些股票"先给股票表；问"能不能做到"先给能/不能+关键数字，机制解释放后面。
8. **回测前先亮方案**（对象/数据/口径/检验项清单），跑完按方案顺序汇报，让每个数字都能对回方案的某一项。

## 输出

- 报告输出到项目根目录的 `artifacts/reports/` 文件夹（JSON 数据中间件同目录树 `artifacts/json_data/`）
- 文件名格式：`{股票名称}{股票代码}-valuation.html`
- 单文件自包含（内联ECharts），可直接在浏览器打开
