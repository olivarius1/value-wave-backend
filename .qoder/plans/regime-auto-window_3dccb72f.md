# 盈利换挡自动检测替代 MANUAL_RANGES（实施方案）

> v3：由 v2 评审整合版改写为叙述式实施方案。技术口径与决策内容与 v2 一致，无增删；改写目标是不依赖评审编号也能读懂每项决策的依据。评审原文见同目录 regime-auto-window_3dccb72f-review.md。

## 一、要解决的问题

全历史百分位隐含一个假设：估值区间长期平稳。盈利发生过跃迁的公司不满足这个假设——跃迁前的低 EPS 制造极端历史 PE，拉宽分位区间，评分失真。紫金矿业 5 年净利增长 20 倍，曾出现 PE/PB 双 0 分硬截断；宝丰、神火、云铝同类。

原有对策是 MANUAL_RANGES 人工校准区间。人工方式有三个解决不了的问题：全市场 5000+ 只逐只校准不可扩展；校准值取决于设计者个人判断；硬编码在源文件里，随重构容易丢失。

目标：用数据驱动的检测替代人工判断。检测到向上的盈利换挡 → PE 分位只用换挡生效之后的子序列计算；未换挡、向下回落、亏损段 → 维持全历史，并在 meta 与报告中标注原因。

## 二、方案主线

检测到向上换挡时只切 PE 分位窗口，PB 与其余因子维持现状。三处改动：

1. `scripts/scoring_engine.py`：新增检测函数 + 评分窗口参数
2. `scripts/report_generator.py`：调用检测、写 meta、渲染标注
3. `scripts/build_report.py`：删除 MANUAL_RANGES、关闭区间的参数恢复

## 三、设计决策

### 1. 检测输入用年报净利润序列，不用 EPS

来源：`fetch_financial_reports` 过滤 `report_type == 'annual'`（默认 40 期，覆盖 20 年），构建 `{int(report_date[:4]): net_profit}`，键为年报归属年。

理由：净利润总额不受送转稀释影响；直接用年报归属年，避开逐日重述序列的两个坑——逐日序列的键是日期而非年份；生效年规则下取年末值对应的是上一年年报，年份还需再偏移校正。

### 2. 只检测向上换挡，向下回落只标注

相邻段比值 `> REGIME_RATIO` 记向上换挡；比值 `< 1/REGIME_RATIO` 记 `downward_skip`，不切窗。

理由：周期股盈利回落段 EPS 下降快于股价，若切到回落段窗口，当前 PE 在窗口内分位偏高，低分恰好出现在周期底部——最该买入的位置给出最差评分。神火、云铝当前都在这个场景。全模型统一规则，不按行业开例外，口径一致优先。

### 3. 阈值 REGIME_RATIO = 3.0

5 年段中位数比值 2.0 等价于复合年化约 15%，正常成长路径即触发，误伤面太大。3.0 ≈ 年化 25%，与正常成长拉开距离。

### 4. 只有 PE 排序数组走窗口，PB 保持全历史

盈利换挡改变的是 PE 的分母。净资产是存量，只要 BPS 序列连续，PB 锚仍然可用；净资产断裂是另一个问题，已有独立处理机制，与盈利换挡互不相干。窗口内 PE 样本不足时只把 pe 因子计中性 50 分，PB 不受影响——刚换挡的股票不至于丢掉所有估值锚。

### 5. 窗口起点对齐年报披露时点

切窗条件 `date >= f'{window_start + 1}-05-01'`：T 年年报次年 5 月生效，与 `_series_effective` 口径一致。若从当年 1 月 1 日切，窗口头部几个月的 PE 分母还是上一年年报的盈利，口径断裂。

实施注意：backtest_engine 导入 scoring_engine，scoring_engine 不能反向导入 `backtest_engine.DISCLOSURE_MONTH`（循环导入）。在 scoring_engine 定义本地常量，注释标明与 backtest_engine 同源。

### 6. 检测在引擎外部，结果经参数传入

backtest_engine 复用 compute_daily_scores。检测若放在引擎内部，回测会使用回测时点尚不可知的换挡结论，即未来函数。外部检测 + `regime_window_start` 参数传入，backtest 不传 → 行为不变。

### 7. pe/pb 区间整体退出参数恢复机制

现状的问题：恢复逻辑读到上次 meta 的 manual/calibrated 区间会写回 `args.pe`。紫金的旧校准区间会被恢复 → `use_rank_pe=False` → 评分继续走旧线性区间。此时检测照常输出 window_start 标注，但评分链路根本不用它——表面验证通过，实际是新机制从未生效。

处置：恢复逻辑中 pe/pb 两个分支删除，manual 与 calibrated 都不恢复（manual 区间同样会覆盖自动结果，单独处理 calibrated 留有缺口）。dps/growth/subtitle/因子分支保留——它们与估值区间无关，云铝 DPS 恢复不受影响。

逻辑层关闭优于修改旧数据：可重复执行，不依赖某一次性的数据清理。旧 meta 的 calibrated 标记自然失效。param_source 的 pe/pb 仍记录来源（manual/auto），只作溯源展示，不参与恢复。

### 8. 已知代价：--pe/--pb 变为单次参数

删除 MANUAL_RANGES 且关闭区间恢复后，命令行区间只在当次报告生效，batch_rebuild 批量重跑即回到自动区间。这是有意的取舍：个别股票自动检测长期出错时，正确动作是修检测规则或参数（触发清单 → 人工标注 → 调整），不是重新给这只股票挂一个持久的人工区间——那等于把 MANUAL_RANGES 换个存放位置。

### 9. 验收用事实核查，不预设分数落点

旧人工锚的线性分数与窗口内 rank 分位是两种口径，拿旧分数当新方法的及格线是循环论证。验收改为三件事：窗口年份与净利序列人工核对一致；无换挡股分数逐点不变；每只触发股票标注真跃迁还是误判。

### 10. 分段规则：首年对齐，尾部残段不参与

按序列首年起每 5 年一段；最后不满 5 年的残段中位数不稳，不参与触发判定。代价：最近 1-4 年内新发生的跃迁要等残段攒满 5 年才可检测。方向保守——宁可窗口多含旧数据，也不错切。

### 11. 两项明确暂缓

- 扣非净利：数据源未确认提供扣非字段。报表净利含一次性损益（大额减值、处置收益）可能制造伪换挡，先靠触发清单人工标注拦截，风险表承认该噪声。
- 景气高位的分位偏低：高增长股盈利兑现期 EPS 增速高于股价增速，窗口内 PE 分布持续压缩，当前分位天然偏低，汇总表顶部会聚集景气高位的股票。这是 rank 分位语义的固有局限，改动前就存在，本次不引入也不修复；以汇总表分位窗口列透明呈现 + 已有消化曲线应对，跨股票的横截面修复需要时另立方案。

## 四、实施步骤

### 步骤 1：scoring_engine.py — 检测函数

常量：`REGIME_SEGMENT_YEARS = 5`、`REGIME_RATIO = 3.0`、`REGIME_MIN_DAYS = 50`。

新增公开函数 `detect_regime_window(profit_series)`，供 report_generator 与后续全市场扫描复用：

- 输入 `{归属年: 净利润}`，docstring 注明必须传年报净利序列
- 首年对齐按 5 年分段取中位数（抗单年极端值），相邻段中位数比值 `> REGIME_RATIO` 记向上换挡点（后段起始年），取最后一次
- 比值 `< 1/REGIME_RATIO` → `(None, 'downward_skip')`；任一段中位数 ≤ 0 → `(None, 'loss_period_skip')`；段数 < 2 → `(None, 'insufficient_history')`；无换挡 → `(None, 'no_switch')`
- 尾部残段（不足 5 年）不参与判定

验证（临时脚本放系统临时目录，不入工作区）六个用例：无换挡；单次向上（比值 >3 触发）；连续向上取最后一次；周期回落不触发且返回 downward_skip；亏损段跳过；段数不足。

### 步骤 2：scoring_engine.py — 窗口参数

- compute_daily_scores 的 params 新增可选键 `regime_window_start`（年份或 None，默认全历史）
- rank 预收集段（L582-593）：仅 `_pe_hist` 收集条件追加 `date >= f'{window_start + 1}-05-01'`；`_pb_hist` 不动
- 窗口内 PE 样本 `< REGIME_MIN_DAYS`：pe 因子计中性 50 分、`_pe_pct` 置 None；pb 因子不受影响
- unstable 由引擎单点判定：新增可选参数 `regime_info_out`（dict），函数内填充 `{'window_start', 'unstable', 'window_days'}`；现有调用方不传则不填充，不改现有函数签名，backtest_engine（L228）与消化曲线（L253）零影响
- 回归保证：不传新键时行为与现状逐点一致

### 步骤 3：report_generator.py — 接入检测与标注

- 净利序列：`fetch_financial_reports` 的结果已在 build_report 获取，经 config 传入。实施时先确认 config 现有字段避免重复拉取；`net_profit` 确认为归母口径，非归母则在 meta 注明口径
- L215-217 之后调用 `detect_regime_window`；`_score_params` 加 `'regime_window_start'`；调用 compute_daily_scores 时传 `regime_info_out` 取 unstable
- meta（L276-283）加三字段：`window_start`（年份或 null）、`window_reason`（regime_switch / no_switch / downward_skip / loss_period_skip / insufficient_history / no_data）、`regime_unstable`
- HTML Section 02 参数表格（L595-599 附近）下方按 reason 分文案：
  - regime_switch：分位窗口 {start+1} 年 5 月起（检测到盈利跃迁）
  - downward_skip：检测到盈利回落段，未切换分位窗口（全历史口径）
  - loss_period_skip：历史含亏损段，PE 分位口径可靠性受限（醒目警示样式）
  - 其余 reason 不显示
- 消化曲线 `_dig_params = dict(_score_params)` 自动继承窗口，无额外改动

### 步骤 4：build_report.py — 入口改动

- 删除 `MANUAL_RANGES` 常量与校准分支（L142-151 / L161-166）：区间决策简化为 `args.pe` 显式指定 → 线性映射（逃生阀，语义不变）；否则 auto + use_rank_pe=True。use_rank 判断简化为 `args.pe is None` / `args.pb is None`
- 恢复逻辑（L100/L104）：删除 pe/pb 两个恢复分支，其余分支保留
- param_source（L255-265）：pe/pb 枚举收敛为 manual/auto，仅溯源展示
- 旧 meta 的 calibrated 标记自然失效，不做数据迁移
- L15-16 注释改为指向自动换挡检测

### 步骤 5：验证

- 步骤 1 的六个单元用例全过
- 机制事实核查（取代旧版紫金分数对照）：紫金/宝丰/神火/云铝重跑，人工核对 window_start 与年报净利跃迁年份一致、window_reason 正确、HTML 标注渲染正确；不预设分数落点，分数只需窗口内 PE 分位可解释
- 对照组覆盖四类场景：
  - 向上跃迁：紫金 601899、宝丰 600989
  - 周期回落：神火 000933、云铝 000807（预期 downward_skip 或窗口含回落段，核对评分可解释）
  - 亏损段：中远海控 601919（预期 loss_period_skip + 警示标注；此前 74% 交易日误判高估的现状会被显式标出）
  - 高增长真伪触发：中际旭创 300308、新易盛 300502、德明利 001309 任选 2 只，窗口与净利序列人工对照，标注真跃迁/误判
  - 互不影响检查：宏达股份 600331 的 PB 行为不变（净资产断裂走既有机制）
- 回归保证：星宇 601799、阳光电源 300274、招商银行 600036 分数与改前逐点一致；backtest 抽查一只，回测分数零变化
- 46 只全量：`python scripts/batch_rebuild.py --summary`；触发清单逐只标注真伪，记录回计划文档；云铝 `[恢复]` 行仍在（dps/growth/subtitle/因子）
- summary_report.py 表格加分位窗口列（window_start 非空时显示年份，约 5 行改动）

### 步骤 6：文档与提交

- README.md / SKILL.md：删 MANUAL_RANGES 描述，写入换挡窗口机制（只向上触发、仅 PE、披露对齐切窗）
- .qoder/commands/report-zx.md：校验清单第 4 条替换为 meta.window_reason 与触发清单一致 + 云铝 dps=0.6997 恢复正常 + loss_period_skip 股票有警示标注
- docs/superpowers/plans/2026-08-26-regime-auto-window.md：标注被本方案取代及差异点
- 更新长期记忆（param_source 机制演进 + 换挡窗口机制）
- 分步提交：步骤 1+2（engine）→ 步骤 3+4（报告链路）→ 步骤 5 通过后 → 步骤 6（文档）

## 五、不采用的做法

1. 参数外置 JSON 配置：仍是人工校准，只把硬编码换了位置，主观性问题原样保留
2. 固定 N 年窗口：隐式处理粗糙，5 年前的污染仍在窗口内
3. 时间衰减加权分位：污染永不归零、排序抖动、参数不可验证
4. 引擎内自动检测：向 PIT 回测引入未来函数，且需改函数签名
5. MANUAL_RANGES 保留为最高优先层：--pe/--pb 已是逃生阀，常量层不删则人工锚点仍在
6. 旧 JSON 数据迁移（清除三只校准股的区间字段）：一次性数据操作不可重复，且 manual 区间同样会覆盖自动结果；逻辑层关闭恢复覆盖全部来源
7. 向下换挡对称切窗：周期底部给出最差评分（见决策 2）
8. PB 跟随换挡窗口：会让刚换挡的股票失去唯一可用的估值锚（见决策 4）

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 向上换挡股在景气高位分位偏低（rank 语义固有） | 汇总表分位窗口列透明呈现；消化曲线对高估档+高增速股已有防御；横截面修复另立方案 |
| 报表净利含一次性损益制造伪换挡 | 触发清单逐只人工标注真伪；扣非字段确认可用后升级 |
| 亏损段股票（中远海控模式）仍用全历史 | HTML 醒目警示 + meta reason；剔除亏损段比较的进阶处理列入后续迭代 |
| 向下回落股窗口含高位段，评分偏严 | 窗口已剔除更早期的低 EPS 污染；downward_skip 标注可见，解读时结合 |
| 高增长股连续触发压短窗口 | 阈值 3.0 起步；触发清单标注真伪后再评估调参 |
| PIT 回测被污染 | 检测在引擎外、参数传入；回测不传新键，逐点回归验证 |
| 最近 1-4 年新跃迁检测不到（残段不参与判定） | 保守方向的已知代价；窗口年份在 meta/HTML 呈现，解读时可知 |

---

## 七、实施与验收记录（2026-09-03）

### 代码改动落点

- `scoring_engine.py`：常量 `REGIME_SEGMENT_YEARS=5 / REGIME_RATIO=3.0 / REGIME_MIN_DAYS=50 / REGIME_DISCLOSURE_MONTH=5`（本地常量，与 backtest_engine.DISCLOSURE_MONTH 同源，避免循环导入）；`detect_regime_window(profit_series)`；`compute_daily_scores` 新增 params 键 `regime_window_start` + keyword 参数 `regime_info_out`
- `report_generator.py`：复用 `config.financial_reports` 构建年报净利序列（归母 PARENTNETPROFIT 口径）；检测调用；meta 三字段 `window_start / window_reason / regime_unstable`；HTML 参数区三分支标注（regime_switch 蓝 / downward_skip 灰 / loss_period_skip 红警示）；消化曲线经 `dict(_score_params)` 自动继承窗口
- `build_report.py`：删 MANUAL_RANGES；恢复逻辑删 pe/pb 分支（dps/growth/subtitle/因子保留）；--pe/--pb 为单次逃生阀；param_source 的 pe/pb 收敛为 manual/auto
- `summary_report.py`：低估区/高估区表加「PE分位窗口」列

### 验证结果（全部通过）

| 验证项 | 结果 |
|---|---|
| 单元六用例 + compute_daily_scores 窗口行为 4 组 | 22 项断言全过（临时脚本已删） |
| 机制核查 11 只重跑 | window_start 与年报净利序列人工核对全部吻合 |
| 新旧引擎同输入回归 | 星宇/招行/宏达 7373 天 score/pe_pct/pb_pct 0 不一致 |
| PIT 回测对比 | 紫金/神火 4529 天分数/区间 0 不一致 |
| HTML 渲染 | 紫金/神火/云铝显示「分位窗口：2022 年 5 月起」；宝丰/中远海控/星宇无标注，6/6 正确 |
| 46 只全量 `batch_rebuild.py --summary` | 46/46 成功；云铝 `[恢复]` 行保留 dps/growth/subtitle/因子（无 pe/pb） |

初次回归对比中星宇/招行/宏达出现 ±0.01 漂移，归因为 K 线新增 1 个交易日使 rank 分母 2442→2443（既有行为）；用 git HEAD 旧引擎同输入重比 0 不一致，排除本次改动影响。紫金/云铝旧报告 pb 为 manual 校准区间（线性口径 pb_pct=None），新报告 pb 走 auto rank，符合决策 7「旧校准区间不再恢复」的预期。

### 触发清单真伪标注（46 只，2026-09-03）

分布：no_switch 25 / regime_switch 13 / insufficient_history 6 / loss_period_skip 2。

regime_switch 13 只全部为 2021 段（window_start=2021，分位窗口 2022 年 5 月起），逐只与年报序列人工核对，**全部真跃迁**（2016-2020 段 → 2021-2025 段净利中位数，亿元）：

| 股票 | 前段→后段中位数 | 比值 | 判定 |
|---|---|---|---|
| 中国石油 601857 | 227.9 → 1573.0 | 6.9x | 真跃迁 |
| 中际旭创 300308 | 5.1 → 21.7 | 4.3x | 真跃迁 |
| 云天化 600096 | 1.5 → 51.6 | 34x | 真跃迁（2016 单年亏损，段中位数>0） |
| 云铝股份 000807 | 5.0 → 44.1 | 8.8x | 真跃迁 |
| 亿纬锂能 300014 | 5.7 → 40.5 | 7.1x | 真跃迁 |
| 华能国际 600011 | 17.4 → 84.5 | 4.9x | 真跃迁（2021/2022 亏损年在窗口段，段中位数>0） |
| 川恒股份 002895 | 1.3 → 7.7 | 5.9x | 真跃迁 |
| 新易盛 300502 | 1.1 → 9.0 | 8.2x | 真跃迁 |
| 神火股份 000933 | 3.6 → 43.1 | 12x | 真跃迁 |
| 紫金矿业 601899 | 40.9 → 211.2 | 5.2x | 真跃迁 |
| 铜陵有色 000630 | 7.0 → 28.1 | 4.0x | 真跃迁 |
| 阳光电源 300274 | 8.9 → 94.4 | 10.6x | 真跃迁 |
| 香农芯创 300475 | 0.7 → 3.1 | 4.4x | 真跃迁 |

loss_period_skip 2 只判定正确：文科股份 002775（2021-2025 段中位数 -2.9，持续亏损）、盐湖股份 000792（2016-2020 段中位数 -34.5）。

insufficient_history 6 只：粤电力A / 德明利 / 新柴股份 / 中国海油 / 伯特利 / 元琛科技（数据源年报不足 2 个完整段）。

宝丰能源 600989：37.0→63.4（1.7x）未达 3.0 阈值，no_switch，正确不触发。宏达股份 600331 no_switch，PB 行为不变（净资产断裂走既有机制）。

### 与计划预期的偏差（3 项）

1. **中远海控 601919（watchlist 外对照组）**：预期 loss_period_skip + 警示标注；实际 insufficient_history——datacenter API 对该股仅返回 3 份年报（2023-2025），不足 2 个完整段。数据源覆盖问题，非机制 bug；insufficient_history 不在 HTML 标注分支，PE 区间 2.4~34.8 仍被旧低盈利期拉宽且无警示。待数据源恢复后复核。
2. **阳光电源回归组假设被数据推翻**：计划预期其无换挡（分数逐点一致），实际 2021 段真跃迁（8.9→94.4，10.6x），检测正确；回归验证改用新旧引擎同输入对比替代。
3. **神火/云铝对照预期**「downward_skip 或窗口含回落段」：实际 regime_switch 2021 段（后一情形）——段中位数比较的是 2016-2020 vs 2021-2025，近期盈利回落不影响段间跃迁判定；窗口已剔除更早期低 EPS 污染，回落段在窗口内属风险表已知项。
