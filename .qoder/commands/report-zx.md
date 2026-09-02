---
description: 生成 watchlist 全量估值报告与汇总筛选
---
生成 watchlist 里所有股票的估值报告并生成汇总报告（数据中间件 JSON 同步更新，最后报告时间列自动同步）。

## 执行步骤

1. 核对任务范围：`python scripts/batch_rebuild.py --dry-run`，确认股票数、代码、模型与 watchlist 一致
2. 全量重建 + 刷新汇总（后台运行，用 GetTerminalOutput 轮询，勿截断输出）：
   `python scripts/batch_rebuild.py --summary`
3. 失败处理：末尾失败行 → `python scripts/batch_rebuild.py --retry --summary`，直到无失败
4. 结果校验（用 Python 读 JSON，勿用 PowerShell Select-String）：
   - 46 只 JSON/HTML 齐全，meta.period 末尾 = 最新交易日
   - 校准股区间未漂移：紫金 PE 8.3~18 / 神火 PE 4.8~16 / 宝丰 PE 8~20、PB 1.5~5 / 云铝 PE 9~22、PB 1.6~3.2、DPS 0.6997
   - 输出含 [恢复] 行（云铝等人工参数自动恢复）或上述股 meta.param_source = manual/calibrated
5. watchlist 时间列由 batch_rebuild 自动同步；有改动才提交（git add watchlist.txt）
6. 汇报：数据日期、低/高估区名单（解析 artifacts/reports/估值汇总筛选.html）

## 参数口径（build_report 自动维护，禁止手工拼参数补跑单只）

显式 CLI 参数 > 内置校准 MANUAL_RANGES > meta.param_source 恢复（仅上次人工项）> 自动计算

- MANUAL_RANGES：紫金/神火/宝丰（盈利 regime 校准区间，命中走线性映射）
- meta.param_source：记录每参数来源（manual/calibrated/auto），人工项下次无参运行时自动恢复
- 手工补跑单只会绕过来源标记且可能与其他报告口径不一致

## 注意事项

- artifacts/ 不入库；只提交 watchlist.txt / 代码 / 文档改动
- 评分与上次显著漂移 = 参数口径问题，先查 [恢复] 行与校准值，勿直接交付
