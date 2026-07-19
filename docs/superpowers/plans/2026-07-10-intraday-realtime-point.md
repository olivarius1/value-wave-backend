# 盘中实时估值点 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交易时段生成报告时，将腾讯API返回的实时价格作为虚拟最后一天参与评分计算，在图表中用醒目圆点标识，收盘后自动被正式收盘数据覆盖。

**Architecture:** 方案A（内存拼接）。不修改DB schema。在 `build_report.py` 的评分计算循环之前，检测今天是否为交易日且当前时刻在DB最后一天之后，若是则将 qt 实时数据追加为虚拟K线点。图表中用 `markPoint` 标识该点。

**Tech Stack:** Python 3, ECharts (markPoint), 腾讯财经 qt 字段

---

### Task 1: db_manager.py — fetch_kline_from_api 增加 volume 和判断交易日

**Files:**
- Modify: `scripts/db_manager.py:98-103`

- [ ] **Step 1: 修改 latest 字典，增加 volume 字段，从 qt[6] 获取成交量**

将 `fetch_kline_from_api` 返回的 `latest` 字典从：
```python
latest = {
    'price': float(qt[3]) if len(qt) > 3 and qt[3] else 0,
    'pe': float(qt[39]) if len(qt) > 39 and qt[39] else 0,
    'pb': float(qt[46]) if len(qt) > 46 and qt[46] else 0,
}
```
改为：
```python
latest = {
    'price': float(qt[3]) if len(qt) > 3 and qt[3] else 0,
    'pe': float(qt[39]) if len(qt) > 39 and qt[39] else 0,
    'pb': float(qt[46]) if len(qt) > 46 and qt[46] else 0,
    'volume': float(qt[6]) if len(qt) > 6 and qt[6] else 0,
    'date': qt[30] if len(qt) > 30 and qt[30] else '',  # qt[30] 是当前日期 YYYYMMDD
}
```

- [ ] **Step 2: 验证**

Run: `python3 -c "from scripts.db_manager import fetch_kline_from_api; rows, latest = fetch_kline_from_api('sh600938', '2026-07-01', '2026-07-10'); print(latest)"`
Expected: latest 中包含 'volume' 和 'date' 键

- [ ] **Step 3: Commit**

```bash
git add scripts/db_manager.py
git commit -m "feat(db): fetch_kline_from_api 返回 volume 和 date 字段"
```

---

### Task 2: build_report.py — 判断是否需要追加盘中虚拟点

**Files:**
- Modify: `scripts/build_report.py:230-240` (DB模式数据获取完成后)

- [ ] **Step 1: 在 DB 模式数据获取完成后，追加盘中虚拟点逻辑**

在 `kline_rows = get_kline(STOCK_CODE)` 循环之前（约第232行），插入以下代码：

```python
        # ===== 盘中虚拟点检测 =====
        # 判断条件：DB中有历史数据，且 qt 返回了当天日期的实时价格
        intraday_point = None
        today_str = ''
        try:
            from datetime import datetime as _dt
            today_str = _dt.now().strftime('%Y-%m-%d')
        except:
            pass
        if latest and latest.get('date') and latest.get('price', 0) > 0:
            qt_date_raw = str(latest['date'])
            # qt 日期格式 YYYYMMDD → YYYY-MM-DD
            qt_date_formatted = f"{qt_date_raw[:4]}-{qt_date_raw[4:6]}-{qt_date_raw[6:8]}" if len(qt_date_raw) == 8 else ''
            if qt_date_formatted == today_str and kline_rows and kline_rows[-1]['date'] < today_str:
                intraday_point = {
                    'date': today_str,
                    'open': latest['price'],  # 盘中无开高低，均用实时价
                    'close': latest['price'],
                    'high': latest['price'],
                    'low': latest['price'],
                    'volume': latest.get('volume', 0),
                }
                print(f"  盘中模式: 检测到今日({today_str})实时价格 {latest['price']:.2f}")
        elif latest and latest.get('price', 0) > 0 and kline_rows:
            # qt 无日期字段时回退：如果DB最后一天 < 今天，且今天有实时价格
            if kline_rows[-1]['date'] < today_str and today_str:
                intraday_point = {
                    'date': today_str,
                    'open': latest['price'],
                    'close': latest['price'],
                    'high': latest['price'],
                    'low': latest['price'],
                    'volume': latest.get('volume', 0),
                }
                print(f"  盘中模式: 追加今日实时价格 {latest['price']:.2f}")
```

然后在 `kline_rows = get_kline(STOCK_CODE)` 循环之后追加：
```python
        # 将盘中虚拟点追加到 kline_rows
        if intraday_point:
            kline_rows.append(intraday_point)
```

- [ ] **Step 2: 验证盘中检测逻辑**

非交易时段运行时，intraday_point 应为 None。可通过临时修改 today_str 测试。
注意：此时不应运行，等 Task 3 一起验证。

- [ ] **Step 3: Commit**

```bash
git add scripts/build_report.py
git commit -m "feat: 盘中虚拟点检测与追加逻辑"
```

---

### Task 3: build_report.py — 盘中点量能因子剔除 + is_intraday 标记

**Files:**
- Modify: `scripts/build_report.py:446-484` (评分计算循环)

- [ ] **Step 1: 盘中虚拟点剔除量能因子，权重归一化**

将评分计算循环（第446-484行）整体替换为以下逻辑。核心变化：
- 检测当前行是否为盘中虚拟点（`row.get('is_intraday')`）
- 如果是盘中点，构建 `intraday_weights`（去掉 `vol` 因子），重新归一化
- 用归一化后的权重计算盘中天的分数
- 如果是收盘数据，用原始 `active_weights`

```python
# ===== 评分计算循环 =====
n = len(kline)
results = []
for i in range(n):
    row = kline[i]
    close, high, low, volume = row['close'], row['high'], row['low'], row['volume']
    ma20 = sum(kline[j]['close'] for j in range(max(0, i-19), i+1)) / min(20, i+1)
    ma60 = sum(kline[j]['close'] for j in range(max(0, i-59), i+1)) / min(60, i+1)
    vol_ma20 = sum(kline[j]['volume'] for j in range(max(0, i-19), i+1)) / min(20, i+1)
    pe_ttm = close / latest_price * latest_pe if latest_price > 0 else 0
    pb = close / latest_price * latest_pb if latest_price > 0 else 0
    mcap = close * TOTAL_SHARES

    # 盘中虚拟点：剔除量能因子（成交量不完整），权重重新归一化
    is_intraday = row.get('is_intraday', False)
    if is_intraday and 'vol' in active_weights and active_weights['vol'] > 0:
        cur_weights = {k: v for k, v in active_weights.items() if k != 'vol'}
        tw = sum(cur_weights.values())
        if tw > 0:
            cur_weights = {k: v/tw for k, v in cur_weights.items()}
    else:
        cur_weights = active_weights

    # 计算各因子得分
    factor_scores = {}
    total = 0

    for fk, w in cur_weights.items():
        if w < 0.001:
            factor_scores[fk] = 0
            continue
        if fk == 'pe':
            s = score_pe(pe_ttm)
        elif fk == 'pb':
            s = score_pb(pb)
        elif fk == 'peg':
            s = score_peg(pe_ttm)
        elif fk == 'ma':
            s = score_ma_deviation(close, ma20, ma60)
        elif fk == 'vol':
            s = score_volume(volume, vol_ma20)
        elif fk == 'vola':
            s = score_volatility(close, high, low)
        elif fk in OPTIONAL_SCORE_FUNCS and fk in factor_values and factor_values[fk] is not None:
            s = OPTIONAL_SCORE_FUNCS[fk](factor_values[fk])
        else:
            s = 50  # 缺失数据默认中性分
        factor_scores[fk] = s
        total += s * w

    total = round(total, 2)
    result_entry = {
        'date': row['date'], 'close': close, 'pe_ttm': round(pe_ttm, 2), 'pb': round(pb, 2),
        'market_cap': round(mcap, 2), 'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
        'score': total,
        'is_intraday': is_intraday,
    }
    # 只记录权重>0的因子分数
    for fk, w in cur_weights.items():
        if w > 0.001:
            result_entry[f's_{fk}'] = round(factor_scores.get(fk, 0), 1)
    results.append(result_entry)
```

- [ ] **Step 2: 验证语法**

Run: `python3 -c "import py_compile; py_compile.compile('/workspace/stock-valuation-skill/scripts/build_report.py', doraise=True)"; echo OK`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add scripts/build_report.py
git commit -m "feat: 盘中虚拟点剔除量能因子，权重归一化避免成交量不完整影响"
```

---

### Task 4: build_report.py — ECharts markPoint 标识盘中点

**Files:**
- Modify: `scripts/build_report.py` (val_data_js 生成区域，约第500行)
- Modify: `scripts/build_report.py` (内联图表 JS 估值分 series，约第818行)
- Modify: `scripts/build_report.py` (全屏图表 JS 估值分 series，约第919行)

- [ ] **Step 1: 生成 ECharts markPoint 数据**

在 `val_data_js` 生成之前，插入：

```python
# 生成 markPoint 数据（标记盘中虚拟点）
intraday_markpoint = []
for idx_r, r in enumerate(results):
    if r.get('is_intraday'):
        intraday_markpoint.append({
            'name': '盘中实时',
            'coord': [r['date'], r['score']],
            'value': f"盘中 {r['close']:.2f}元\n估值分 {r['score']}",
            'itemStyle': {'color': '#f59e0b'},
        })
        break
```

将 `intraday_markpoint` 序列化为 JS 变量插入到 `val_data_js` 中：
```python
val_data_js += f"\nvar INTRADAY_MARKPOINT = {json.dumps(intraday_markpoint, ensure_ascii=False)};"
```

- [ ] **Step 2: 在内联图表的估值分 series 中添加 markPoint**

找到内联图表中估值分 series 的 `symbol: 'none'` 行（约第818行），在其后添加：
```python
        lineStyle: {{ color: '#1a4b8c', width: 1.5 }}, itemStyle: {{ color: '#1a4b8c' }}, symbol: 'none',
        markPoint: {{ data: INTRADAY_MARKPOINT, symbol: 'circle', symbolSize: 10, label: {{ show: true, position: 'top', color: '#f59e0b', fontSize: 11, formatter: function(p) {{ return p.value; }} }} }},
```

- [ ] **Step 3: 在全屏图表的估值分 series 中添加同样 markPoint**

找到全屏图表中估值分 series 的 `symbol: 'none'` 行（约第919行），同样添加：
```python
        lineStyle: {{ color: '#60a5fa', width: 1.2 }}, itemStyle: {{ color: '#60a5fa' }}, symbol: 'none',
        markPoint: {{ data: INTRADAY_MARKPOINT, symbol: 'circle', symbolSize: 12, label: {{ show: true, position: 'top', color: '#f59e0b', fontSize: 12, formatter: function(p) {{ return p.value; }} }} }},
```

- [ ] **Step 4: 端到端验证**

```bash
python3 /workspace/stock-valuation-skill/scripts/build_report.py 603596 "伯特利" sh 8.98 12 45 1.5 5.5 0.15 "120.14" "13.09" "19.65%" "264" "汽车制动系统" "测试盘中" tech
```
Expected: 无"盘中模式"（非交易时段），报告正常，JS 语法通过。

- [ ] **Step 5: Commit**

```bash
git add scripts/build_report.py
git commit -m "feat: ECharts markPoint 标识盘中虚拟点"
```

---

### Task 4: 增量覆盖验证 — 收盘后盘中点自动消失

**Files:**
- No new file changes, only verification

- [ ] **Step 1: 验证收盘后行为**

收盘后（或DB中已有当天数据时）运行：
```bash
python3 scripts/build_report.py 603596 "伯特利" sh 8.98 12 45 1.5 5.5 0.15 "120.14" "13.09" "19.65%" "264" "汽车制动系统" "测试收盘覆盖" tech
```
Expected: 输出无"盘中模式"（因为DB最后一天已 >= 今天，或今天非交易日），intraday_markpoint 为空数组

- [ ] **Step 2: 提交 git 并更新 SKILL.md**

在 SKILL.md 中补充说明盘中模式的自动行为。