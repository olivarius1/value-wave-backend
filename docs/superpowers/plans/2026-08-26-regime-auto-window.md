# 自动换挡检测 + 子序列分位（方案 A）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态：已被取代（2026-09-03）**。实施采用 `.qoder/plans/regime-auto-window_3dccb72f.md`（v3 方案），验收记录见该文件第七节。与本方案的主要差异：检测输入用年报净利润序列而非 EPS 逐日重述序列；只向上切窗（向下回落仅标注不切窗）；仅 PE 走窗口、PB 维持全历史（本方案原设计 PE/PB 都切）；阈值 3.0（本方案 2.0）；MANUAL_RANGES 与 pe/pb 参数恢复机制均删除，--pe/--pb 变单次逃生阀。

**Goal:** 全市场估值零人工锚点。用自动盈利换挡检测替代人工校准区间（MANUAL_RANGES），检测到换挡的股票只用换挡之后的 PE/PB 序列算分位，未换挡的用全历史（现状不变）。保留 rank 分位机制（避免满分问题），统一口径：所有股票分数都是"当前 PE/PB 在有效窗口内的百分位"，可横向比较。

**Architecture:** 方案 A（自动检测 + 子序列 rank），否决方案 B（固定 N 年窗口：隐式 regime 处理粗糙，5 年前污染仍在窗口内）、方案 C（时间衰减加权分位：污染永不归零、排序抖动、参数不可验证）。在 `scoring_engine.py` 构建分位排序数组前插入换挡检测，命中则按换挡年份过滤序列；`build_report.py` 的 meta 增加窗口标注；MANUAL_RANGES 退役（紫金交给自动检测），`--pe/--pb` 命令行保留为逃生阀。

**Tech Stack:** Python 3, EPS 年度序列（含披露滞后，无未来函数）

---

### Task 1: scoring_engine.py — 换挡检测函数

**Files:**
- Modify: `scripts/scoring_engine.py`（分位排序数组构建处之前，新增检测函数）

- [ ] **Step 1: 新增 `_detect_regime_window(eps_series)` 函数**

输入：年度 EPS 序列（已含披露滞后，键为年份）。输出：`(window_start_year or None, reason)`。

算法（顺序扫描，取最后一次换挡点）：
1. 以 5 年为一段，从最早到最晚顺序比较相邻两段的中位数（中位数优先于均值，抗亏损年/极端年）
2. 比值 > 2 或 < 0.5 → 记录换挡点（段边界年份，即后一段的起始年）
3. 窗口起点 = 最后一次换挡点；无换挡 → None（全历史）
4. 防御：任一段中位数 ≤ 0（亏损段）→ 跳过检测，返回 `(None, 'loss_period_skip')`——比值的符号/大小在亏损段参与时无意义（正÷负为负数、负÷负会误触发、分母近零时比值爆炸）
5. 样本不足：窗口内交易日 < 50 时由 Task 2 降级处理（此处只返回年份）

- [ ] **Step 2: 验证（4 个用例）**

用临时脚本（放系统临时目录，勿入工作区）验证：
- 用例 A 无换挡：EPS 三段 1.0 / 1.1 / 1.2 → 返回 `(None, 'no_switch')`
- 用例 B 一次换挡：EPS 三段 0.1 / 1.0 / 1.2 → 返回 `(第5年, 'regime_switch')`（第 2 段/第 1 段 = 10 倍触发，第 3 段/第 2 段 = 1.2 倍未触发）
- 用例 C 两次换挡（持续高增长）：EPS 三段 0.2 / 0.6 / 1.8 → 返回 `(第10年, 'regime_switch')`（取最后一次换挡点，窗口 = 近 5 年）
- 用例 D 亏损段：EPS 三段 0.1 / -0.05 / 0.3 → 返回 `(None, 'loss_period_skip')`

Expected: 四个用例全部符合预期

- [ ] **Step 3: Commit**

```bash
git add scripts/scoring_engine.py
git commit -m "feat(engine): 换挡检测函数，顺序扫描取最后一次换挡点，亏损段跳过"
```

---

### Task 2: scoring_engine.py — 窗口化分位序列

**Files:**
- Modify: `scripts/scoring_engine.py`（rank 排序数组构建处，`bisect_right` 之前）

- [ ] **Step 1: 按窗口过滤分位序列**

在构建 PE/PB 分位排序数组前调用 Task 1 的检测函数：
- `window_start` 为年份时：只保留 `date >= window_start` 的 PE/PB 值构建排序数组
- `window_start` 为 None 时：全历史（现状不变）
- 窗口内交易日 < 50：返回 `pe_pct/pb_pct = None` 并标记 `regime_unstable = True`（降级为"无法评估"，绝不回退到含污染的全历史）
- BPS 断裂（净资产骤降 >50% 且长期低位，宏达类）：不切窗口，沿用现有"评分仅参考"标注，与换挡逻辑正交

- [ ] **Step 2: 验证**

临时脚本验证：
- 构造 10 年 PE 序列 + 换挡点在第 5 年 → 分位只基于第 5 年后的数据计算
- 构造窗口样本 < 50 的序列 → pe_pct 为 None + regime_unstable 标记
- 构造无换挡序列 → 分位与改动前完全一致（回归保证）

Expected: 无换挡股票的分位与改动前逐点一致；换挡股票分位基于窗口

- [ ] **Step 3: Commit**

```bash
git add scripts/scoring_engine.py
git commit -m "feat(engine): 分位序列按换挡窗口过滤，窗口样本不足降级无法评估"
```

---

### Task 3: build_report.py — meta 标注 + MANUAL_RANGES 退役

**Files:**
- Modify: `scripts/build_report.py`（val_data meta 构建处；MANUAL_RANGES 常量与 use_rank 分支）

- [ ] **Step 1: meta 增加窗口标注**

`val_data['meta']` 增加字段（JSON 与 HTML 内嵌同步）：
- `window_start`: 换挡年份或 null
- `window_reason`: `'regime_switch'` / `'no_switch'` / `'loss_period_skip'`
- `regime_unstable`: true/false（窗口样本不足）

- [ ] **Step 2: MANUAL_RANGES 退役**

- 删除 `MANUAL_RANGES` 常量与 `use_rank_pe/use_rank_pb` 分支（紫金 601899 交给自动检测）
- 保留 `--pe/--pb` 命令行区间（逃生阀，语义：显式指定时仍走线性映射）
- 报告 HTML 在数据说明区展示窗口标注（如"分位窗口：2023 年起（检测到盈利换挡）"）

- [ ] **Step 3: 验证**

重跑紫金，检查 JSON meta：
- `window_start` 命中（紫金净利 5 年增长 20 倍，应检测到换挡，窗口约近 2-5 年）
- `pe_pct/pb_pct` 基于窗口计算
- HTML 数据说明区显示窗口标注

Expected: meta 字段齐全，紫金窗口命中

- [ ] **Step 4: Commit**

```bash
git add scripts/build_report.py
git commit -m "feat(report): meta 标注窗口信息，MANUAL_RANGES 退役交由自动检测"
```

---

### Task 4: 紫金对照验证

**Files:**
- No new file changes, only verification

- [ ] **Step 1: 对照 8.4c 结论**

重跑紫金后对比：
- 旧值：线性锚点（PE 8.3~18.0 / PB 2.35~5.1），score = 24.37（偏贵，低分）
- 新值：自动检测窗口内的 rank 分数
- 验证方向一致：新分数应仍落在偏贵区间（低分），且能解释（窗口内当前 PE 处于高位分位）

- [ ] **Step 2: 记录对照结果**

将旧值/新值/窗口年份/分位数据写入本计划的验证记录段（见文末），供审计追溯。

Expected: 新分数与 8.4c 结论方向一致；若明显矛盾（如变成高分低估），暂停并检查检测参数

---

### Task 5: 44 只全量回归

**Files:**
- No new file changes, only verification

- [ ] **Step 1: 批量重跑**

沿用 runpy 批量模式重跑全部 44 只 watchlist，确认全部成功、JSON 正常。

- [ ] **Step 2: 全量检查**

- 全部 JSON meta 含 `window_start/window_reason/regime_unstable`，无缺失
- 抽查检测命中清单：哪些股票触发换挡、窗口年份是否与 EPS 序列直观一致（抽样核对 2-3 只）
- 亏损段跳过清单：确认是实际亏损股（如周期底部亏损的股票）
- 汇总报告（估值汇总筛选.html）重新生成，个股分数与汇总一致

- [ ] **Step 3: 分数变化抽查**

抽查 3 只（星宇/阳光/招行）分数，确认无换挡股票分数与改动前一致（回归保证）、汇总 = 个股。

Expected: 无换挡股票分数不变；汇总与个股口径一致；HTML 渲染正常

---

### Task 6: 收尾

- [ ] **Step 1: 更新本计划状态**

计划文件头部标注完成状态与验证结果摘要。

- [ ] **Step 2: 提交**

```bash
git add docs/superpowers/plans/2026-08-26-regime-auto-window.md
git commit -m "docs(plan): 自动换挡检测+子序列分位实施计划完成"
```

---

## 验证记录（Task 4/5 结果，实施时填写）

| 股票 | 检测结果 | 窗口 | 旧分数 | 新分数 | 说明 |
|------|---------|------|--------|--------|------|
| 紫金矿业 | （待填） | （待填） | 24.37（线性） | （待填） | 对照 8.4c |
| 星宇股份 | （待填） | （待填） | 73.91 | （待填） | 应无换挡，分数不变 |
| 阳光电源 | （待填） | （待填） | 74.13 | （待填） | 应无换挡，分数不变 |
| 招商银行 | （待填） | （待填） | 73.01 | （待填） | 应无换挡，分数不变 |

## 风险与边界（实施时留意）

1. **高增长股窗口普遍过短**（每 5 年翻 2.5 倍以上会连续触发，窗口被压到近 5 年）：先观察 44 只里占比，若过高可加切换强度阈值（比值 > 3 才算显著换挡）——属参数调整，需记录后决定
2. **负 EPS 段**：跳过检测用全历史 + 标注（亏损期 PE 本身失真，保持简单比自作聪明安全）
3. **次新股**：沿用现有回退逻辑（全历史），不受影响
4. **分数口径变化**：换挡股分数会从线性变为 rank 语义，跨股票比较时注意 meta 标注（汇总表可显示窗口年份列）
5. **与回测引擎的关系**：backtest_engine 是独立 PIT 逻辑（expanding 区间），本计划只影响报告主流程的当前视角分位，两者不冲突；后续若需回测窗口化，另立计划
