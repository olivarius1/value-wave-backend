---
description: 生成 watchlist 全量估值报告与汇总筛选
---
生成 watchlist 里所有股票的估值报告并生成汇总报告（数据中间件 JSON 同步更新，最后报告时间列自动同步）。

## 执行步骤

1. 核对任务范围：`python scripts/batch_rebuild.py --dry-run`，确认股票数、代码、模型与 watchlist 一致
2. 财报缓存预热（iFinD 主源，后台运行并轮询；缓存 30 天内有效可跳过）：
   `python scripts/ths_fetcher.py`
3. 全量重建 + 刷新汇总（后台运行并轮询输出直到完成，勿截断输出）：
   `python scripts/batch_rebuild.py --summary`
4. 失败处理：末尾失败行 → `python scripts/batch_rebuild.py --retry --summary`，直到无失败
5. 结果校验（用 Python 读 JSON，勿用文本工具逐行匹配输出）：
   - 46 只 JSON/HTML 齐全，meta.period 末尾 = 最新交易日
   - meta.window_reason 分布合理（2026-09-06 iFinD 基准：regime_switch 15 只全部 window_start=2021 / loss_period_skip 3 / no_switch 28，insufficient_history 应为 0——iFinD 年报含上市前历史），regime_switch 名单见 .qoder/plans/factor-audit-ifind_20260906.md
   - loss_period_skip 股票（文科股份/盐湖股份/元琛科技）HTML 有红色警示标注
   - 云铝 dps=0.6997 恢复正常（[恢复] 行保留 dps/growth/subtitle/因子，不含 pe/pb）
   - 财报缓存抽查：老股年报数应 ≥10（iFinD），出现"近2年报均值"字样 = 缓存被截断污染，删 `artifacts/.cache/financial/*_reports.json` 重跑预热
6. watchlist 时间列由 batch_rebuild 自动同步；有改动才提交（git add watchlist.txt）
7. 汇报：数据日期、低/高估区名单（解析 artifacts/reports/估值汇总筛选.html）

## 参数口径（build_report 自动维护，禁止手工拼参数补跑单只）

显式 CLI 参数 > 自动计算（rank 分位评分 + 盈利换挡窗口检测）

- PE/PB：不传 --pe/--pb 走自动分位评分；盈利换挡检测（年报净利 5 年段中位数比值>3，仅向上）命中时 PE 分位只用换挡后子序列（披露对齐次年 5 月起），PB 维持全历史
- --pe/--pb 为单次逃生阀：仅当次报告走线性映射，不做持久化恢复（人工区间会覆盖自动窗口）
- meta.param_source 记录参数来源（manual/auto），仅溯源展示不参与恢复；dps/growth/subtitle/因子等人工项下次无参运行时自动恢复
- 手工补跑单只会绕过来源标记且可能与其他报告口径不一致

## 注意事项

- artifacts/ 不入库；只提交 watchlist.txt / 代码 / 文档改动
- 评分与上次显著漂移 = 参数口径问题，先查 [恢复] 行、meta.window_reason 与 param_source，勿直接交付
