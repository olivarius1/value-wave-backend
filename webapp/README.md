# Web 控制台（Django）

把原脚本流水线搬到浏览器上操作：数据管理 / 分数面板 / 报告浏览 三页 +
后台任务队列。数据本体仍是既有 SQLite 库（`artifacts/.cache/kline_store.db`
与 `fin_store.db`），Web 层只是调度与展示，不复制数据。

## 启动

```bash
pip install --user django          # 首次
bash webapp/start.sh               # 默认 http://127.0.0.1:8642/
bash webapp/start.sh 9000          # 换端口
```

`start.sh` 会先自动 `migrate`（webapp 自己的 `db.sqlite3`，只存任务账本与股票分组标签；
分数在 score_store.db、K线/财务在 kline_store.db / fin_store.db，均由 scripts/ 产出），
再以 `--noreload` 起单进程服务。

## 三个页面

| 页面 | 路径 | 功能 |
|------|------|------|
| 数据管理 | `/data/` | 全市场K线/财务/模型分类/算分因子/分红事件/报告 的**新鲜度总览**（多久没更新、落后多少只），每张卡片带**更新按钮**；**重建类按钮带二次确认**（`data-confirm`，如"重建股票列表"），增量类直接执行；**单股更新**：搜索框输入 6 位代码或名称关键字 → 搜索结果行上点「更新数据」（K线增量 → 财务强制刷新 → 因子回填 → 重算评分 → 重建报告）；后台任务进度条 + 实时日志 + 取消 |
| 分数面板 | `/scores/` | 判定口径：**当前分在个股自身15年分数序列中的历史分位**。**高分（低估候选）**= 分位 ≥ 阈值（默认90%，页面可调），分数从高到低；**低分（高估警惕）**= 分位 ≤ 阈值（默认20%），分数从低到高；中间带不列出（页头不展示，调整阈值即时生效并本地记忆）。「全市场评分」按钮增量重算；点行**新开标签**进入该股报告页 |
| 报告浏览 | `/report/?code=600887` | 输入代码 → 内嵌展示自包含估值报告 HTML（`artifacts/reports/`）；没有报告时显示空态，选模型后「生成/更新报告」，完成后自动加载。模型下拉显示中文（枚举在 `stock_ops.MODEL_LABELS` 后端维护），留空 = 自动归属（watchlist人工 > AI分类） |
| 分组管理 | `/groups/` | 股票分组（标签）。内置「自选」组（首次访问从 watchlist.txt 播种，不可删）；建组/删组、搜索股票后勾选分组贴标签，**一只股票可属于多个分组**，组成员可查看/移除 |

## 后台任务设计（刻意不用 celery）

- 单 worker 串行队列：同一时刻一个任务，避免抢网络限速与 SQLite 写锁；
- 两类任务：**子进程**（原 CLI 脚本原样跑：`fetch_all_market.py` 等，stdout 逐行入日志，
  行内 `n/m` 自动解析为进度）与**函数**（Web 编排：单股更新 / 评分扫描 / K线全市场增量）；
- 幂等入队：同 kind+params 排队/运行中时重复点击直接复用；
- 取消：子进程 SIGTERM，函数任务协作式检查；
- 服务重启时遗留任务标记失败，不产生僵尸。

### 按钮与脚本的对应

| 按钮 | 执行 |
|------|------|
| 增量更新全市场 | 先把落后于最新交易日的 done 账本重置为待抓取（`fetch_all_market` 的账本语义是 done 永不重跑），再跑 `fetch_all_market.py --fetch` |
| 重建股票列表 | `fetch_all_market.py --build-list` |
| 业绩报表批量 | `fetch_all_financials.py --bulk-reports`（新报告期自动拾取） |
| 缺口补齐 | `fetch_all_financials.py --gap-reports` |
| 行业+股本 | `fetch_all_financials.py --industry-info` |
| 分类增量 | `ai_model_classifier.py --all`（需 AI API 配置） |
| 因子回填 | `score_factors.py --backfill` |
| 事件预热 | `warmup_bonus_events.py` |
| 全市场评分 | `score_market.py`（增量：K线无新数据的股票跳过；产出 score_store.db） |
| 生成报告 | `build_report.py <code> --model <m>` |

## 分数口径（系统评分器）

分数的唯一来源是 **`scripts/score_market.py`**（全市场批量评分器）：对每只上市股票，
用 scoring_engine 因子函数 + 该股模型权重（AI 分类，缺省回退周期），从数据库
（K线 / 财报 / 每股指标 / 分红 / score_factors 因子表）计算 **15年日度分数序列**
（K线或财务不足 15 年的股票按实际可得天数计算），
存入 `artifacts/.cache/score_store.db`：

- `score_series(code, date, score)` — 日度序列
- `stock_score(code, model, score, score_pct, price, pe, pb, pe_min/max, pb_min/max, …)` — 当前状态

**面板判定口径 = 个股自身历史分位（score_pct）**：当前分严格低于自身序列的比例。
高分 tab = 历史分位 ≥ 阈值（默认 90%，页面可调），低分 tab = ≤ 阈值（默认 20%）。
即"该股当前比它自己历史上 90% 的时间更被低估"，与横截面排名无关。

- 分数面板的「全市场评分」按钮 = 增量重算（K线无新数据的股票自动跳过）；
  单股更新会在第 4 步重算该股自己的序列。
- PE/PB 用真实价（raw_kline20 切15年）÷ 逐日重述 EPS/BPS（分红送转滚动重述），
  当前 PE 与历史区间同口径；MA/量/波动用收益口径序列（kline20r 优先）。
- **退市股票始终不显示**：分数序列落后最新交易日超过 60 天的视为退市/长期停牌遗留
  （约 95 只），面板永久隐藏；60 天以内的停牌股正常显示（当天临时停牌/涨跌停无成交等，
  约 10 余只），页头标注「停牌 N 只」。
- 面板可按**分组**筛选（下拉选择，与历史分位阈值、停牌过滤叠加生效）；
  分组数据存 Web 库（`StockGroup`/`StockGroupMember`），筛选选择本地记忆。
- 评分公式后续补充因子（如商品价格偏离）时只改 score_market / scoring_engine，
  面板与阈值机制不变。

## 已知边界

- 名称搜索是中文子串匹配（不含拼音首字母缩写）；代码前缀搜索无此限制。
- 财务覆盖率按"当前上市宇宙"统计；fin 库中的历史退市代码不计入分母。
- `runserver` 是开发服务器；本工具按本机单用户设计，未做鉴权，勿暴露公网。
