# 全A股财务数据批量获取方案（akshare 换源 + 存储统一）

> **状态:** ✅ 已执行完成（2026-09-13 01:41）。yjbb 86期 + 缺口补齐128只 + 行业/总股本5298只 全部 **0 失败**；覆盖 reports/pershare/info/industry **5348/5348**，bonus 5121只有事件 + 227只确认无分红记录；年报中位15份（最多22）、最新年报营收>0占98.6%；库 73.8MB、fin_ledger 0 失败。新旧源对账 240条（48只×近5年报）**235条偏差≤1%**，唯一离群 600061 为安信信托→国投资本重组重述口径差异（东财=最新合并口径，iFinD 旧缓存=重组前口径，新源对新估值更准确）。摘要: `artifacts/.cache/fin_fetch_summary.json`。
> **范围:** 新增 fin_store.py / fetch_all_financials.py；重构 financial_fetcher.py（数据源链）；model_classifier.py / warmup_bonus_events.py 小改；SKILL.md 数据源章节更新。ths_fetcher.py 保持原样（iFinD 额度恢复后可一键回归）。
> **目标:** iFinD 额度耗尽的前提下，今晚完成全A约5348只的历年财报/每股指标/分红送转/行业/总股本全量入库；13个下游调用方零改动。

---

## 1. 背景

### 1.1 触发事件

- **iFinD 额度耗尽**。原架构中 iFinD 是财报主源（`financial_fetcher._load_ths_reports`），每次缓存过期都先打 iFinD：单股预热 = 87个报告期 × 4指标 ≈ 350次 THS_BD 调用，每次失败还要3次重试——全市场跑一遍等于先烧掉50万次无效调用再降级，必须摘除。
- **K线已于前夜全量入库**（5348只/1474万行/3.7GB，fetch_all_market.py + kline_store.py），但**财报只有 watchlist 48只**有缓存（散JSON），全市场回测与全市场评分缺基本面底座。
- 用户要求：切换渠道（akshare），先评估解耦程度，不满足则重构。

### 1.2 解耦评估结论（重构依据）

原 `financial_fetcher.py` 是"门面 + 上帝模块"混合体，评估如下：

| 问题 | 证据 | 判定 |
|---|---|---|
| 数据源选择硬编码在函数内部 | `fetch_financial_reports._load()` 闭包内先 iFinD 后东财，换源必须改函数体 | ❌ 不满足 |
| 死源无开关 | iFinD 失败靠异常降级，无渠道级 kill switch | ❌ 不满足 |
| 财报存储与K线存储双标准 | K线已迁 kline_store.db(SQLite+账本)，财报还是 ~5300 个散JSON（30天TTL靠文件mtime） | ❌ 不满足 |
| 行业取数三处重复 | financial_fetcher（存末段 industry_v1）/ model_classifier（存三级链 industry）各自实现+各建缓存键 | ❌ 不满足 |
| 下游耦合 | 13个调用方全部经 `financial_fetcher` 公开函数取数，纯计算（评分指标/估值区间/EPS-BPS重述）与IO同模块但无交叉依赖 | ✅ 门面成立 |
| 私有成员外泄 | warmup_bonus_events / model_classifier 直接 import `_FIN_CACHE_DIR` / `_cache_financial` | ⚠️ 兼容保留 |

**结论：门面 API 健康可保留（下游零改动），但内部"源-存储-计算"三层揉在一起，需重构内部结构。**

## 2. 目的

1. 全市场财报/每股指标/分红送转/行业链/总股本今晚全量入库（约5348只，2005Q1起87个报告期）；
2. 财报存储升级为 fin_store.db（与 kline_store 同构：WAL、线程本地连接、写锁、断点账本、JSON一次性迁移）；
3. 数据源可配置可降级：env `FIN_SOURCES`（默认 `akshare,east`），iFinD 退化为显式 opt-in；
4. 13个下游调用方（build_report/scan_watchlist/run_backtest/backtest_grid/factor_analysis/model_classifier/report_generator/build_total_return…）**零改动**。

## 3. 关键决策

### D1 主源=akshare 东财业绩报表（按报告期批量），而非逐股

`ak.stock_yjbb_em(date='20051231')` 一次调用返回该报告期**全市场**的 营收/归母净利/同比/ROE/毛利率/EPS/每股净资产——字段与现有 reports+pershare20 schema 一一对应。2005Q1~2026Q2 共 **87 次调用**完成全市场×22年，对比逐股方案（5348只×20年）约2个数量级的调用量差。实测 2005 年老数据完整（1843只）。已退市/北交所代码的数据照收入库（未来幸存者偏差分析可复用）。

### D2 每股指标(pershare)只取年报期

fin_pershare 主键 (code, year)，仅 yjbb 年报行入库（EPS/每股净资产原值，含负EPS）；口径过滤（eps≤0置0、bps≤0置0、全≤0丢行）保持在门面读取侧，与东财路径历史行为一致。

### D3 YoY 缺口本地回填

yjbb 提供的同比缺失（如2005首年）时，读取侧用库内同期环比自动回填——与 iFinD 路径"本地推导YoY"同口径，保证 compute_financial_metrics 的 revenue_yoy/profit_yoy 有值。

### D4 iFinD 摘除为显式 opt-in

`FIN_SOURCES` 环境变量控制源链（默认 `akshare,east`）。reports 链只认 `ths/east`，bonus 链只认 `akshare/east`。ths_fetcher.py 一行未改，额度恢复后 `FIN_SOURCES=east,ths` 即回到旧主次序。

### D5 fetch_stock_info 派生字段改为现算

旧实现把营收/净利/毛利率/CAGR 与总股本一起冻结在 info 缓存里，与 reports 更新节奏脱节。新实现：联网只取总股本（1次东财调用），其余从 fin_store reports 派生——消灭口径漂移。

### D6 行业链唯一入库点

`financial_fetcher.fetch_industry_chain()`（EM2016 完整三级链）入库 fin_industry；`fetch_industry()` 取末段保持报告展示契约；model_classifier 改为委托调用，删除其重复实现与私有缓存键（'industry' / 'industry_v1' 两套旧缓存迁移后统一）。

### D7 防截断回归（冒烟中发现）

旧代码读缓存不截断（iFinD 86期全量）；新代码若 `[:max_reports]` 默认截80期会砍掉回测最需要的2005-2006。已改为 **max_reports/max_years=0（默认）不截断，显式传值才截断**（scan 的近10期等调用点行为不变）。

### D8 bonus 空列表语义

akshare 返回空 df = 确认无分红（入库+置新鲜，避免重复拉）；东财返回空可能是接口故障（不置新鲜，保持旧行为下次重拉）。

## 4. 存储结构（fin_store.db，独立于 kline_store.db）

| 表 | 主键 | 内容 |
|---|---|---|
| fin_reports | (code, report_date) | report_type/营收/归母/ROE/毛利率/同比（亿、小数） |
| fin_pershare | (code, year) | 年报 EPS/BPS 原值 |
| fin_bonus | (code, date) | 分红送转实施事件 (ratio, div) |
| fin_info | code | 总股本(亿股) |
| fin_industry | code | EM2016 完整三级链 |
| fin_extra | (code, kind) | 杂项JSON blob（rd研发费用率、info_legacy等） |
| fin_freshness | (code, kind) | 30天TTL刷新记录（kind含 extra:xxx） |
| fin_ledger | task | 断点账本：`yjbb:YYYYMMDD` / `reports:CODE` / `info:CODE` |

存量JSON（5313个文件）已 `fin_store.migrate_json()` 一次性迁入（imported=5313 skipped=0），抽样600887 三类数据逐字段一致；原文件保留作备份。**注意：build_total_return.py（旧代码进程）仍在写 bonus JSON，其退出后需再跑一次迁移收尾。**

## 5. 执行阶段（fetch_all_financials.py）

| 阶段 | 命令 | 调用量 | 预估 |
|---|---|---|---|
| ① 等待+收尾迁移 | 等 build_total_return 退出后 migrate_json | 0网络 | — |
| ② 业绩报表批量 | `--bulk-reports` | 87次 akshare | ~15min |
| ③ 缺口补齐 | `--gap-reports` | 每缺失股1-2次东财 | ~10-30min |
| ④ 行业+总股本 | `--industry-info` | 每股1-2次东财 | ~30min |
| ⑤ 质量校验 | `--validate` | 0网络 | 1min |

bonus 阶段复用 `warmup_bonus_events.py`（本次由在跑的 build_total_return 覆盖大部分+迁移收尾，之后残量交给 warmup 补）。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| yjbb 缺失长期停牌/慢披露股票的个别报告期 | 阶段③逐股东财兜底补齐；validate 输出 missing 清单 |
| 东财对批量端点限频 | 87次×0.6s间隔+3次退避重试+账本断点续传；逐股阶段3线程×0.3s全局限速（与既有脚本同级） |
| yjbb 数值为"最新重述口径"（非披露时点PIT） | 与原东财兜底路径同口径，不劣化；iFinD PIT 语义仅在显式启用 ths 时存在——回测对比时注意 48 只 watchlist 的新旧源差异由 validate 抽样对账暴露 |
| 行业链关键词打分依赖三级链 | EM2016 完整链入库；旧 industry_v1（只存末段）迁移后在30天TTL内自愈为完整链 |
| 与在跑进程冲突 | fin_store.db 独立于旧进程写的 JSON；执行链先等 build_total_return 退出 |

## 7. 验收标准

- [ ] yjbb 87期全部 done，fin_ledger 无 failed
- [ ] reports 覆盖 ≥ 5348 只（stock_basic excluded=0），年报中位数 ≥ 20份
- [ ] 最新年报营收>0 占比 ≥ 95%，ROE>0 占比 ≥ 70%（银行/亏损股拉低属正常）
- [ ] 抽样 600887/600519/601318 最新年报数值与已知量级吻合
- [ ] watchlist 48只新源 vs 旧iFinD缓存营收偏差 < 1%
- [ ] `build_report.py 600887 --model staples` 与 `scan_watchlist.py` 回归正常
