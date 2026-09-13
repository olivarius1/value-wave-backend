# 全市场AI模型分类模块方案（AI API批量归类8种估值模型）

> **状态:** ✅ 已执行完成（2026-09-13 凌晨）。全市场 5348/5348=100% 分类入库、零永久失败；金标准回归 41/46=89.1%（v2026-09-c）；待复核 1243 只。执行细节与待用户决策项见第 9 节执行记录。
> **范围:** 新增 `scripts/ai_model_classifier.py`；`kline_store.py` 增加 2 张表；预留统一读取接口 `resolve_model()`。报告/回测/scan 现链路零改动。
> **目标:** 用用户提供的 AI API 批量完成全市场约 5348 只股票的 8 模型归类，结果入库带时间戳，低频（半年~1年）维护。

---

## 1. 背景与现状核实

- **模型列现状**：`watchlist.txt` 46 只的"模型"列是会话内 AI 逐只分析后写入的。全市场 5348 只后此方式不可扩展——这正是本方案要替代的事。
- **已有未被使用的资产**：`scripts/model_classifier.py` 规则版分类器（财务特征打分 + 行业关键词先验），**全仓库无任何调用方**。它有两个新用途：①API 失败时的兜底；②全量跑完后的交叉校验源。
- **输入数据可行性（已验证 600887）**：东财 F10 `RPT_F10_BASIC_ORGINFO`（columns=ALL）一次调用返回：
  | 字段 | 示例值 | 用途 |
  |---|---|---|
  | `EM2016` | 食品饮料-食品-乳制品 | 三级行业（最强先验） |
  | `MAIN_BUSINESS` | 各类乳品及健康饮品的生产与销售 | 主营简述（业务性质判断核心） |
  | `ORG_PROFILE` | 公司简介长文本（截断~300字用） | 兜底补充 |
  | `ACTUAL_HOLDER` | 无 / 国务院国资委等 | soe 央企属性判定 |
  | `LISTING_DATE` / `SECURITY_TYPE` | 1996-03-12 / 上交所主板 | 次新/板块标注 |
  可经 `_cache_financial(code, 'orginfo', ...)` 缓存，全市场 5348 次一次性拉取。
- **全市场财报 PIT 未预热**（另一个战役，见 2026-09-11 计划第 6 节）→ 分类输入暂无财务特征（ROE/增速/股息率）。**soe 判定（高股息央企）证据偏弱**，处置见 D5。
- **判定规则已有成熟文本可直接做提示词素材**：`growth_params.md` 第一节模型表、`thinking_flow.md` 2.1 决策树 + 2.1.1 反例清单（中远海控 soe 化、垒知集团伪 soe、宏达股份壳化、元琛科技亏损无盈利锚）。

## 2. 方案评估（用户提案：AI API + 内置提示词 + 50只/批 + JSON入库 + 低频刷新）

**结论：方向正确，采纳。** 量化依据：

| 维度 | 估算 |
|---|---|
| 调用量 | 5348 只 ≈ **107 批**（50只/批），每批 1 次 API 调用 |
| Token 量 | 输入 ≈ 1.8M（每批系统提示词~2.5k + 每股~280），输出 ≈ 0.65M |
| 成本 | 中档模型（豆包/DeepSeek/Qwen 档）**个位数人民币**，放大 10 倍也无痛 |
| 耗时 | 每批 30~60s，3 并发 + 限频 ≈ **30~60 分钟**跑完全市场 |
| 频率 | 业务属性（行业/主营/实控人）变化极慢，**半年~1年刷新足够**；盈利换挡由引擎自动检测，不依赖重分类。新股/漏网用增量补跑，无需常驻 cron |

**需要补强的 5 点（设计里已落实）：**

1. **输入质量决定分类质量**：仅"名称+行业"不够——`soe` 要看实控人、`staples/discretionary` 边界要看主营。已验证东财一次调用全取。
2. **纯 AI 的失败模式**：JSON 格式错、幻觉、长批次后半段敷衍、批次间不一致 → schema 白名单校验 + 低温度(0~0.2) + 失败单只补跑 + raw 存档可复现。
3. **可审计可追溯**：每条结果带 `prompt_version / ai_engine / 输入快照 / batch_id`，raw 按批存档。提示词改版 = 新 version，旧结论可回溯。
4. **人工复核队列**：低置信度 / 输入缺失 / soe 类 / 行业-模型矛盾 → `needs_review=1`，导出 CSV 人工定夺后导入；`source=manual` 的结果**永不被自动刷新覆盖**。
5. **金标准回归先行**：watchlist 46 只人工标注做对照集，AI 一致率 **≥90%** 才允许全量跑；同时随机抽 50 只全市场快检（46 只是精选好公司，边缘案例覆盖不足，抽样补这个盲区）。

## 3. 需求

| # | 需求 | 验收 |
|---|---|---|
| R1 | 输入构建：每股 {名称, EM2016行业, 主营简述, 简介(截断), 实控人, 上市日期}，东财一次调用 + 缓存 | 5348 只输入快照齐备，缺主营的标记 |
| R2 | 批量引擎：50只/批、JSON mode、2~3并发+退避、重试；解析失败批内单只补跑 | raw 存档齐全；失败清单可复现 |
| R3 | 存储：kline_store.db 新增 `model_classify` + `classify_batch` 两表；raw 按批存 `artifacts/.cache/ai_classify/raw/` | 批级账本断点续传，重跑即续 |
| R4 | 校验：模型 key 白名单、confidence 枚举、批内 code 覆盖率校验 | 非法输出重试，3 次不过记失败 |
| R5 | 复核流：needs_review 自动标记 + `--export-review/--import-review` CSV + `--set` 单只手工 | manual 优先级最高且不被刷新覆盖 |
| R6 | 金标准回归：46 只对照一致率 ≥90%；抽样 50 只人工快检 | diff 报告逐只列出 |
| R7 | 读取接口：`resolve_model(code)` 统一优先级（manual > ai > rule 兜底），供后续全市场扫描/汇总消费 | 现有 scan/batch 链路零改动 |

## 4. 关键决策

### D1 AI 为主，规则分类器为兜底 + 交叉校验
2.1.1 案例（中远海控/垒知集团）证明业务性质判断超出规则能力；但规则版零成本、确定性，用作 API 失败兜底和跑完后的 `--crosscheck` 二次确认（分歧 → needs_review）。

### D2 存储放 kline_store.db（同一 SQLite，WAL），不建新库
与全市场 K 线/stock_basic 同库，一次连接全拿到；表结构：

```sql
CREATE TABLE IF NOT EXISTS model_classify (
  code TEXT PRIMARY KEY,
  name TEXT, model TEXT NOT NULL,          -- 8模型key
  confidence TEXT, reasons TEXT,           -- JSON数组，AI理由
  source TEXT DEFAULT 'ai',                -- ai / manual / rule
  needs_review INTEGER DEFAULT 0,
  review_done INTEGER DEFAULT 0,
  industry TEXT, business TEXT, holder TEXT,  -- 输入快照（审计+变化检测）
  batch_id TEXT, ai_engine TEXT, prompt_version TEXT,
  classified_at TEXT, updated TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS classify_batch (   -- 批级账本（断点续传）
  batch_id TEXT PRIMARY KEY,
  status TEXT, codes TEXT, attempts INTEGER DEFAULT 0,
  error TEXT, started_at TEXT, finished_at TEXT
) WITHOUT ROWID;
```

### D3 提示词 = 三块现成素材压缩，prompt_version 管理
系统提示词（每批共享）①8 模型定义表（growth_params 第一节：适用行业+核心特征）；②2.1 决策树；③2.1.1 反例规则（高股息央企化周期股→soe；ROE<8%微利→禁 soe；负增速→禁 tech/pharma；亏损/壳公司→低置信并说明）+ 输出 schema `{"results":[{"code","model","confidence","reasons":[]}]}` + "每只独立判断、信息不足给 low"。
用户消息 = 编号股票清单。`prompt_version` 写入每条结果，改提示词必须升版本。

### D4 输入快照入库，输入变化触发重分类提示
`industry/business/holder` 存进结果行；`--refresh` 时输入 hash 与存量不同 → 该股强制重分类（业务变更 detection），相同则可跳过（省 token）。

### D5 无财务特征时 soe 一律 needs_review
本阶段分类无 ROE/股息数据，soe 语义=高股息央企，证据弱。规则：`model=soe` → needs_review=1；后续财报 PIT 预热完成后跑 `--crosscheck`（规则分类器用真实财务特征复核），分歧项再人工。

### D6 刷新策略：手动命令，不做 cron
- 半年/一年后：`--refresh --stale-only`（只重跑 `classified_at` 超龄或输入已变的），manual 永不覆盖；
- 新股/漏网：`--all` 缺谁补谁（stock_basic 对比 model_classify 差集）；
- `--status` 随时出统计报告（各模型分布/置信度/待复核/超龄清单）。

### D7 API 配置：OpenAI 兼容格式
`artifacts/.cache/ai_credentials.json`（chmod 600，与 ths_credentials.json 同风格）：

```json
{"base_url": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "…", "model": "doubao-…", "batch_size": 50, "concurrency": 3}
```

豆包/DeepSeek/Qwen(兼容模式)/GLM 均适用；优先用 `response_format=json_object`（不支持则提示词约束 + 宽松解析兜底）。

## 5. 执行计划

| Step | 内容 | 预算 |
|---|---|---|
| 0 配置冒烟 | 写 credentials；输入构建器跑 1 批 50 只试调用，验证 JSON 遵从率与输出质量 | 0.5h |
| 1 模块开发 | `ai_model_classifier.py`：输入构建 + 批量引擎 + 校验 + 存储 + 复核 CLI 全套（`--all/--codes/--limit/--dry-run/--refresh/--stale-only/--export-review/--import-review/--set/--status/--crosscheck`） | 1天 |
| 2 金标准回归 | 46 只跑 diff：一致率 ≥90% 过关；不达标逐只裁决（改提示词升版本 or 接受 AI）+ 全市场抽样 50 只快检 | 2h |
| 3 全量执行 | 107 批 × 3 并发挂机跑完，失败批自动补 | 0.5~1h |
| 4 人工复核 | `--export-review` 导出（预计 300~600 只：低置信+soe+缺主营），人工定夺后 `--import-review` 导入 | 1~2h 人工 |
| 5 收尾 | `--status` 总报告；`resolve_model()` 落在 model_classifier.py（watchlist.txt 手工值优先级最高），供全市场扫描落地时启用；本次 scan/batch 链路零改动 | 0.5天 |

## 6. needs_review 触发规则

1. `confidence != high`；2. 主营简述缺失；3. `model=soe`（无财务证据期，见 D5）；4. 行业关键词与模型矛盾（如非金融关键词→bank、无资源关键词→cyclical）；5. AI 与规则分类器分歧（`--crosscheck`，财报预热后）。

## 7. 成本与风险

| 风险 | 对策 |
|---|---|
| JSON 解析失败/幻觉 | 白名单校验+重试+单只补跑；raw 存档可复现；3 次不过记失败不阻塞其他批 |
| 长批次后半段敷衍 | 50/批为上限，实测差则降到 30；指令"逐只独立判断"；低温度 |
| soe 误判（缺财务证据） | 强制复核 + 财报预热后 --crosscheck 二次确认 |
| API 限频/不稳定 | 退避重试 + 批级账本，中断重跑即续 |
| 模型 key 漂移（业务转型） | 输入快照 + --refresh 变化检测；半年刷新兜底 |
| 金标准偏差（46 只偏精选） | 增加全市场随机抽样 50 只人工快检补盲区 |

## 8. 后续（不在本次范围）

1. 财报 PIT 预热完成后：`--crosscheck` 用规则分类器 + 真实财务特征全量复核一次；
2. 全市场扫描/汇总落地时经 `resolve_model()` 消费本表（DB 为准，watchlist.txt 仍是观察池唯一来源，模型列可由 DB 预填）；
3. 北交所放开时 stock_basic 同步纳入分类宇宙。

---

## 9. 执行记录（2026-09-12 ~ 09-13，已完成）

### 9.1 结果总览

- **覆盖：5348/5348 = 100%**（含 2 只首轮漏网补跑），批级账本全部 done，**零永久失败**
- 模型分布：tech 1933 / cyclical 1273 / discretionary 634 / pharma 506 / soe 495 / staples 255 / bank 132 / realestate 120
- 置信度：high 2560 / medium 2036 / low 752；**待复核 1243 只**（`artifacts/model_classify_review.csv`）
- 成本：DeepSeek deepseek-flash，tokens 输入 1.81M（含上下文缓存命中~80%）/ 输出 1.69M（含重试与单只补跑），107 批 × 3 并发约 85 分钟；输入预取 5302 只约 30 分钟（2.8只/s）

### 9.2 金标准回归（提示词 v1→v3）

| 版本 | 一致率 | 主要分歧与处置 |
|---|---|---|
| v2026-09-a | 80.4%（37/46） | 自创规则"存储/光伏→cyclical"与系统约定冲突（德明利/阳光电源），tech 定义含"汽车零部件"吸走潍柴/新柴 |
| v2026-09-b | 80.4%（37/46） | 修复上述后暴露"轨交装备"误入 cyclical 示例（中国中车）、汽车零部件粗分类（星宇/伯特利） |
| v2026-09-c | **89.1%（41/46）** | 剩余 5 分歧全部灰色地带：①海油/陕煤/中石油 AI 按 thinking_flow 2.1.1"央企化高股息→soe"与 watchlist 旧标注(cyclical)冲突；②高能环境/中公高科 low 置信度边缘股 |

v3 后停止调提示词：继续凑 90% 就是对 46 只精选样本过拟合；剩余分歧全部自动 needs_review 进复核队列。

### 9.3 执行期发现与修正

1. **DeepSeek json mode 不稳**：空响应（重试即恢复）频发 + 整批 JSON 非法若干次 → 新增"整批失败重试一次 → 仍失败降级单只补跑"两级救援 + raw 响应存档 `artifacts/.cache/ai_classify/raw/`，之后零永久失败
2. **旗标校准**：初版 `confidence != high` 即复核 → 3041 只不可操作；回归证实 medium-无其他旗标与人工标注全部一致 → medium 不再单独触发，加 `--recompute-flags` 对存量重算（3041 → 1243）
3. **缓存策略验证**：静态提示词全在 system、变量全在 user 末尾，实测输入 token 缓存命中 80%+（896/1096 单批抽样）

### 9.4 待用户决策

1. **煤炭/石油央企口径**：AI 按 thinking_flow 2.1.1 规则把 中国神华/陕西煤业/中国石油/中国海油 等判 soe（watchlist 旧标注 cyclical）。全市场此类约数十只，两种口径评分差异大（cyclical 靠商品价格偏离因子、soe 靠股息率+订单增速）——需统一口径后重跑该类或改 watchlist 标注
2. **tech 占比 36%** 是否接受（新能源/电子/通信/软件全计入 tech 的宽口径）
3. **1243 只复核 CSV** 的处理节奏（可先只处理 soe 与 low 的并集，其余默认维持 AI）

### 9.5 二期：限流加固 + 算分因子入库 + 全市场交叉校验（2026-09-13）

- **限流加固**（依 api-docs.deepseek.com/zh-cn/quick_start/rate_limit）：DeepSeek 为**并发数限制**（deepseek-flash 上限 2500 并发/账号，非 TPM/RPM），超限 429 无 Retry-After 头。`call_ai` 新增：429 → 全局暂停（所有线程共享，Retry-After 优先，否则指数退避 10s×2ⁿ 封顶160s）；成功响应清零连续429计数；配置 $doc 注明上限。当前 concurrency=3 远低于上限，历史 429 零发生
- **算分因子入库**：新增 `score_factors` 表 + `scripts/score_factors.py`（--backfill/--status，断点续传30天新鲜度）。字段：compute_financial_metrics 7项（akshare 预热财报，离线）+ rd_ratio（东财研发费用率）+ dps_ttm（fin_bonus 滚动365天每股分红，口径=元/股与 build_total_return 一致，已用伊利验证 dps_ttm=1.38）+ div_yield（dps_ttm/最新价，soe≥4% 证据）
- **crosscheck 升级**：规则分类器交叉校验现在携带 rd_ratio/div_yield 真实财务证据，soe 判定可用股息率≥4% 与 ROE 验证
- **执行结果（2026-09-13）**：
  - 因子回填 5348/5348（3.2只/s，约28分钟，含东财研发费用率联网），字段覆盖率见 `score_factors.py --status`
  - **发现并修复规则分类器结构缺陷**：`_MODEL_RULES` 缺 realestate 规则集（8模型只定义7个），曾致万科→bank 等垃圾分歧；已补 `realestate` 规则（地产关键词+6/ROE中等/增速平稳）
  - **交叉校验旗标加置信度门槛**：仅规则分类器自评 confidence='high' 的分歧才触发 needs_review（不设门槛时分歧2312条/43%多为假阳性）；高置信结果无论一致与否存 rule_model 列（审计可查）
  - **AI vs 规则（带财务证据）高置信一致率 89%**（1698/1908），与金标准回归 89.1% 相互印证；高置信分歧 210 只已标 needs_review
  - **最终复核队列 1335 只**（构成：置信度low 752 / soe 495 / 行业先验矛盾~180 / 规则分歧 210，有重叠）
  - **soe 股息证据量化**：495 只 soe 中 374 只有股息数据，**仅 72 只 div_yield≥4%**（高股息语义成立，如渤海轮渡11%/建发股份8.2%/中国建筑6.2%），**302 只 <4%**（电科院/洪都航空/碧水源等股息≈0，与 soe 语义矛盾）——决策项 9.4-1 的数据基础已齐
