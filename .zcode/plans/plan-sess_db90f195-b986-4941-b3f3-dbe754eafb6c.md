# 回测改进计划：跨牛熊多周期验证 + 百分位策略网格搜索

## 目标
1. 回测覆盖 2008~2026 多轮牛熊（2008 危机、2015 牛顶与崩塌、2018 熊、2019-2021 结构牛、2022-2024 熊、2025-2026），消除"2021-2026 单一牛市样本期"的缺陷
2. 网格验证百分位策略：买入阈值 {0.80, 0.90, 0.95} × 卖出阈值 {0.30, 0.20, 0.10} 共 9 组合，全期 + 分牛熊段对比，回答"哪一组最好"（以分段稳健性排名防单段过拟合，不只看总收益）

## 改动清单

### A. 数据回溯扩展（一次性预热，报告链路零影响）
- `kline_cache.py`：`get_kline/get_kline_raw` 增加可选 `years` 参数（默认 10，`build_report`/`scan_watchlist` 等现有调用不变）；`years>10` 时缓存文件用独立后缀 `_kline20`，与报告的 10 年缓存隔离（避免报告的 PE/PB 10 年分位窗口被拉长的历史污染）
- `ths_fetcher.py`：`_PERIOD_START_YEAR` 2016→2005（保证 2008-2015 各切片的基本面因子 ROE/毛利率/增速成分一致，避免"早期切片因子缺失"与"牛熊效应"混淆）；东财兜底 `max_reports` 40→80 同步
- 迁移：删除 `artifacts/.cache/financial/*_reports.json` 重预热（约 640 次 iFinD 调用，~8 分钟）；46 只 K 线按 `_kline20` 后缀拉 20 年（约 2200 次腾讯调用，一次性）

### B. PIT 修复（探查发现的 bug）
- `run_backtest.compute_percentile_thresholds`（L230）：切片阈值 `k <= key` → `k < key`，消除"当前切片自身未来分数计入自身阈值"的切片内前视；重跑基线记录数字变化

### C. 牛熊分段模块 `scripts/market_regimes.py`（新）
- 拉上证指数 `sh000001` 日线（独立缓存 `index_sh000001.json`，避免与 sz000001 平安银行的缓存键冲突）
- 月度收盘 + 20% 回撤/恢复规则自动划分牛熊段（bear: 自滚动高点回撤≥20%；bull: 自低点恢复≥20%；其余震荡），段表落盘可人工修订；提供 `assign_regimes(dates) -> {date: regime_label}` 供复用

### D. `run_backtest.py` 增强
- 回测数据源改用 20 年 K 线（`years=20`）
- `simulate_strategy` 改"扩张式宇宙"：去掉 `common_start=全体股票都有分数` 的齐步走（当前被元琛拖到 2023-05），改为每日"有分数的股票才入池、无分数不持仓"，使组合模拟真正覆盖 2008-2026（个股分数起点各不相同，自然滚动加入）
- metrics.json 增加 `regimes` 段：分段 pooled IC、分层收益（复用 `compute_ic/compute_layers` 按段子集计算）

### E. 网格搜索 `scripts/backtest_grid.py`（新，可复用 run_backtest 的数据加载与策略函数）
- PIT 分数只算一次（重 `compute_pit_scores` 结果复用），9 组合各跑 `compute_percentile_thresholds(lo,hi)` + `simulate_strategy(mode='pct')`（纯内存，秒级）
- 每组输出：全期 + 各牛熊段的 累计/年化/最大回撤/胜率/换手；含 0.1%/次翻转的成本敏感性（组合间换手差异大，净收益排名可能与毛收益不同）
- 最优判定：全期收益 + 各段排名中位数 + 回撤，输出推荐组合与"多重比较偏差"提示（9 选 1 的最优天然高估）
- 产出：`artifacts/backtest/grid_{ts}/grid_results.json` + `grid_report.html`（9 曲线净值图 + 汇总表 + 分段热力表），固定入口 `grid_latest.html`

### F. 文档与提交
- `factor-audit-ifind_20260906.md` 追加"多周期验证与网格搜索"节（含幸存者偏差的诚实披露：用今天的 watchlist 回测 2008-2015 会高估组合收益，个股级 IC/分层仍有效）
- `templates/backtest_explained.md` 补充分段回测与网格搜索的通俗解读；README 回测段同步
- 按阶段 commit+push：A 数据扩展 → B+C+D 回测框架 → E 网格 → F 文档

## 执行顺序
1. A 数据扩展与预热（后台跑，验证缓存后缀隔离与首分日 ~2008-05）
2. B PIT 修复 + 基线重跑（记录影响）
3. C+D 牛熊分段 + 扩张式组合 + 分段 IC/分层
4. E 网格搜索 + 报告
5. F 结论与文档，逐阶段提交

## 验收标准
- 回测覆盖 ≥3 个完整牛熊转换；分段 IC 均为正且 ≥2 个不同性质段（牛/熊）都成立
- 9 组合有全期+分段对比表，推荐组合有分段稳健性依据
- 报告链路（build_report 分数）回归验证零漂移（缓存隔离生效）
