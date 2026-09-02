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

输出：`artifacts/reports/伊利股份600887-valuation.html`（浏览器直接打开，JSON 中间件在 `artifacts/json_data/`）

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

## 盘中实时模式

交易时段（9:30-15:00）运行 `build_report.py` 时，若实时接口返回了当日价格且 K 线最后一天早于今天，会自动追加一个**盘中虚拟点**参与评分：

- 实时价（无开高低，均用现价）作为虚拟最后一天，图表中以橙色圆点标记 **“盘中实时”**
- 盘中成交量不完整，该点自动**剔除量能因子**，其余因子权重重新归一化
- 盘中点仅内存拼接，**不写入缓存**；收盘后正式 K 线包含当日，盘中点自动消失

```bash
# 盘中运行示例（自动检测，无需额外参数）
python scripts/build_report.py 601919 --model soe --dps 1.00
```

## K线缓存与增量更新

- **首次运行**：全量获取10年K线（~30秒），缓存到 `artifacts/.cache/`
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

### 批量生成

```bash
# 全量重建 watchlist（股票池唯一来源 watchlist.txt：名称,代码,模型,最后报告时间）
python scripts/batch_rebuild.py

# 指定代码子集 / 按模型过滤
python scripts/batch_rebuild.py --stocks 601799,600887
python scripts/batch_rebuild.py --model tech,cyclical

# 只打印将执行的命令（不实际生成）
python scripts/batch_rebuild.py --dry-run

# 完成后刷新估值汇总筛选.html
python scripts/batch_rebuild.py --summary

# 只重跑上次失败的股票（失败清单自动记录在 artifacts/.cache/batch_failed.txt）
python scripts/batch_rebuild.py --retry
```

## 数据来源

| 数据 | API | 说明 |
|------|-----|------|
| K线 | 腾讯财经 `web.ifzq.gtimg.cn` | 前复权日K，自动分批获取10年 |
| 财务报表 | 东方财富 `datacenter.eastmoney.com` | 年报/半年报/季报核心指标 |

## 输出说明

- 输出目录：`artifacts/`（可重建产物，与代码分离，不入库）
  - `artifacts/reports/`：HTML 报告，文件名 `{股票名称}{股票代码}-valuation.html`
  - `artifacts/json_data/`：同名 JSON 数据中间件（VALUATION_DATA 同源落盘，供汇总筛选与外部工具消费，HTML 不依赖它）
  - `artifacts/.cache/`：K线/财务缓存、batch_failed.txt
- 格式：单文件自包含HTML（内联ECharts），浏览器直接打开
- 内容：10年估值回测曲线、当前评分、历史百分位、财务报表摘要

## 回测系统（分数-未来收益验证）

审计发现：报告中的历史分数曲线含未来信息（全局区间+最新基本面因子），不能当作历史验证。
回测系统以 **Point-in-Time 口径**（无未来函数）重算历史分数，验证"分数是否预示未来收益"，
这是判断估值标准是否合理的唯一途径。

### 运行

```bash
cd D:/myLab/trader/stock-valuation-skill

# 全量 44 只 watchlist（首次运行自动抓取不复权K线+财务数据并缓存）
python scripts/run_backtest.py

# 指定股票 / 时间段（起止均支持，避开特定行情段）/ 强制刷新数据
python scripts/run_backtest.py --stocks 600887,601899
python scripts/run_backtest.py --start 2018-01-01 --end 2022-12-31
python scripts/run_backtest.py --refresh-data

# 本地 Web 服务：页面表单提交 → 自动重跑 → 跳转最新报告（更换时间段/股票子集）
python scripts/backtest_web.py        # 打开 http://127.0.0.1:8643
```

### 阅读说明

报告内每节附简短解读；完整版见 `docs/backtest_guide.md`（每次运行自动复制为 `{run_id}/reading_guide.md`），
包含：分层回测 / 五等分桶 / IC（池化 vs 按股）/ 周频采样等术语表、各图表解读要点、常见误区与口径假设清单。

### 输出（artifacts/backtest/）

- `backtest_latest.html`：固定入口，浏览器直接打开（最新一次回测的可视化报告）
- `{run_id}/meta.json`：全部参数与口径（复现依据）
- `{run_id}/daily_scores.csv`：每只股票每日的 Point-in-Time 分数、PE/PB、区间、状态、p80/p20 阈值、未来 60/250 日收益
- `{run_id}/metrics.json`：IC 检验、分层回测、两种策略模拟（绝对分数 / 个股历史百分位）全部指标
- `{run_id}/curves.json`：策略A（绝对分数）vs 策略B（个股百分位）vs 基准净值曲线
- `{run_id}/reading_guide.md`：回测报告阅读说明副本
- `{run_id}/run.log`：运行日志

每次运行生成独立 `run_id`（时间戳）目录，数据不变时重复运行结果一致（可随时复现）。

### 三项验证

1. **IC 检验**：分数与未来 60/250 日收益的 Spearman 秩相关（周频采样减少重叠样本），
   输出池化 IC、按股 IC 均值/中位数、IC>0 股票占比
2. **分层回测**：分数桶 <40 / 40-70 / >=70 与五等分桶的未来平均收益、胜率，检查单调性
3. **策略模拟**（两种信号口径，净值曲线三条线对照）：
   - **策略A 绝对分数**：分数 >=70 持仓 / <40 空仓 / 中间保持前态
   - **策略B 个股历史百分位**：分数 >= 自身历史 p80 持仓 / <= p20 空仓（PIT 重算，样本 >=50）
   - 等权组合 vs 买入持有基准（信号滞后 1 日、无交易成本）

### 口径假设（消除未来函数）

| 项 | 口径 |
|----|------|
| PE/PB 区间 | expanding window 10th/90th 百分位，每年 5 月 1 日重算（只用截至当日数据） |
| 财务数据 | T 年年报次年 5 月 1 日生效（A 股披露截止 4/30），因子只用已披露年报 |
| 历史 PE/PB | 不复权真实交易价 ÷ 已生效 EPS/BPS（前复权价随除权缩放会失真） |
| 缺历史财务 | PE/PB 因子计中性分，禁止"当前 EPS 反推" |
| 收益 | 前复权收盘价（含分红除权效应），不另计现金分红与交易成本 |
| 股息率因子 | 未提供 DPS 时缺失，权重自动再分配 |

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
│   ├── report_generator.py  # 核心生成器（HTML报告）
│   ├── scoring_engine.py    # 可复用评分引擎（8模型权重+因子评分）
│   ├── model_classifier.py  # 基本面特征模型分类器
│   ├── kline_cache.py       # K线缓存（前复权 + 不复权）与增量更新
│   ├── financial_fetcher.py # 东方财富财报获取（带本地缓存）+ 估值区间计算
│   ├── backtest_engine.py   # Point-in-Time 回测评分（无未来函数）
│   ├── run_backtest.py      # 回测入口：IC/分层/策略模拟 + 输出
│   ├── report_builder.py    # 回测 HTML 报告生成
│   ├── scan_watchlist.py    # watchlist 扫描
│   ├── batch_rebuild.py     # 批量重建（watchlist.txt 驱动，支持子集/过滤/dry-run/retry）
│   ├── summary_report.py    # 估值汇总筛选报告
├── artifacts/                  # 输出产物（不入库，可重建）
│   ├── reports/                # HTML 估值报告（含估值汇总筛选.html）
│   ├── json_data/              # 数据中间件 JSON（同源落盘，汇总/外部工具消费）
│   ├── backtest/               # 回测输出（每 run 独立目录）
│   └── .cache/                 # K线/财务缓存、batch_failed.txt
├── _shared/js/
│   └── echarts.min.js       # ECharts（内联到HTML）
└── templates/
    ├── growth_params.md     # 行业参数参考
    └── thinking_flow.md     # 分析思维流程
```
