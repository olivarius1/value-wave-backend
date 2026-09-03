#!/usr/bin/env python3
"""
估值汇总报告生成器（数据中间件模式）
- 股票池唯一来源 watchlist.txt
- 数据来源：各股报告 JSON（artifacts/json_data/*-valuation.json），与个股报告同一份数据，
  分数口径绝对一致（rank百分位映射、不复权PE、披露滞后等全部沿用，无重复算分）
- 计算每只股票当前分数在历史中的百分位
- 筛选百分位 > 85%（低估区，分数处于历史高位）或 < 40%（高估区，分数处于历史低位）
- 输出单页HTML汇总表；无网络调用，秒级完成
"""
import csv
import datetime
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)

# ===== 股票池：唯一来源 watchlist.txt（名称,代码,模型,最后报告时间） =====

def _load_watchlist():
    """读取 watchlist.txt 作为唯一股票池，与批量重跑/scan_watchlist 同源，避免硬编码漂移"""
    stocks = {}
    path = os.path.join(_SKILL_DIR, 'watchlist.txt')
    with open(path, encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for row in reader:
            if len(row) >= 3 and row[0].strip():
                stocks[row[1].strip()] = (row[0].strip(), row[2].strip())
    return stocks


ALL_STOCKS = _load_watchlist()

# ===== 模型中文名（仅展示用，权重定义在 scoring_engine.MODEL_PRESETS） =====
MODEL_NAMES = {
    'staples': '必选消费', 'discretionary': '可选消费', 'tech': '科技制造',
    'cyclical': '周期资源', 'soe': '央企基建', 'bank': '银行保险',
    'realestate': '地产', 'pharma': '医药消费',
}


def analyze_stock(code, name, model):
    """从个股报告 JSON 读取结果，口径与个股报告绝对一致（同一份数据，无重复算分）"""
    json_path = os.path.join(_SKILL_DIR, 'artifacts', 'json_data', f'{name}{code}-valuation.json')
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    meta, entries = data.get('meta', {}), data.get('data') or []
    if not entries:
        return None
    scores = [r['score'] for r in entries if r.get('score') is not None]
    if not scores:
        return None
    latest = entries[-1]
    latest_score = latest.get('score')
    if latest_score is None:
        return None
    # 百分位：当前分在历史中的位置
    below = sum(1 for s in scores if s <= latest_score)
    percentile = round(below / len(scores) * 100, 1)

    return {
        'code': code, 'name': name, 'model': model,
        'price': latest.get('close') or 0,
        'pe': latest.get('pe_ttm') or 0,
        'pb': latest.get('pb') or 0,
        'score': latest_score,
        'percentile': percentile,
        'score_min': round(min(scores), 1),
        'score_max': round(max(scores), 1),
        'score_avg': round(sum(scores)/len(scores), 1),
        'pe_range': f"{meta.get('pe_min')}~{meta.get('pe_max')}",
        'pb_range': f"{meta.get('pb_min')}~{meta.get('pb_max')}",
        # 盈利换挡窗口：非空表示 PE 分位只用该年份 5 月起的子序列（透明呈现景气高位分位偏低的成因）
        'window': f"{meta.get('window_start') + 1}年5月起" if meta.get('window_start') else '-',
        'days': len(scores),
        'last_date': latest.get('date', ''),
    }


def generate_html(results, output_path):
    """生成汇总HTML"""
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    # 分两组：百分位 = 当前分数在历史中的位置；分数高 = 低估（便宜）
    # 百分位>85%（分数处于历史高位，相对历史更便宜）→ 低估区；百分位<40%（分数处于历史低位，相对历史更贵）→ 高估区
    undervalued = sorted([r for r in results if r['percentile'] > 85], key=lambda x: -x['percentile'])
    overvalued = sorted([r for r in results if r['percentile'] < 40], key=lambda x: x['percentile'])

    def make_rows(items):
        rows = ''
        for r in items:
            pctl = r['percentile']
            # 徽章直接表达估值状态（分数高=低估）：高分位=低估（好），低分位=高估（坏）
            if pctl > 95:
                badge = '<span class="badge badge-green">极度低估</span>'
            elif pctl > 85:
                badge = '<span class="badge badge-blue">低估</span>'
            elif pctl < 20:
                badge = '<span class="badge badge-red">极度高估</span>'
            elif pctl < 40:
                badge = '<span class="badge badge-orange">高估</span>'
            else:
                badge = '<span class="badge badge-gray">中性</span>'
            rows += f'''<tr>
  <td class="stock"><b>{r['name']}</b><span class="code">{r['code']}</span></td>
  <td class="pctl">{pctl:.0f}% {badge}</td>
  <td class="num score">{r['score']:.1f}</td>
  <td class="num">{r['pe']:.1f}</td>
  <td class="num">{r['pb']:.2f}</td>
  <td class="num">{r['price']:.2f}</td>
  <td>{MODEL_NAMES.get(r['model'], r['model'])}</td>
  <td class="num">{r['score_avg']:.1f}</td>
  <td class="num">{r['score_min']:.1f}~{r['score_max']:.1f}</td>
  <td class="num">{r['pe_range']}</td>
  <td class="num">{r['pb_range']}</td>
  <td class="num">{r['window']}</td>
</tr>\n'''
        return rows

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>估值汇总筛选报告</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background:#f8f9fa; padding:24px; color:#1a1a2e; }}
h1 {{ font-size:22px; margin-bottom:4px; }}
.meta {{ color:#666; font-size:13px; margin-bottom:20px; }}
h2 {{ font-size:16px; margin:24px 0 10px; padding-left:10px; border-left:3px solid #4361ee; }}
h2.over {{ border-left-color:#e63946; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:24px; }}
th {{ background:#f1f3f5; font-size:12px; padding:8px 10px; white-space:nowrap; text-align:right; }}
th:first-child {{ text-align:left; }}
td {{ padding:8px 10px; border-top:1px solid #eee; font-size:13px; text-align:right; }}
td.stock {{ text-align:left; white-space:nowrap; }}
tr:hover {{ background:#f8f9ff; }}
.num {{ font-variant-numeric:tabular-nums; }}
.score {{ font-weight:700; font-size:15px; }}
.pctl {{ font-weight:600; white-space:nowrap; }}
.code {{ color:#999; font-size:11px; margin-left:6px; }}
.badge {{ display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; font-weight:600; }}
.badge-green {{ background:#d3f9d8; color:#2b8a3e; }}
.badge-blue {{ background:#d0ebff; color:#1971c2; }}
.badge-orange {{ background:#fff3bf; color:#e67700; }}
.badge-gray {{ background:#f1f3f5; color:#666; }}
.badge-red {{ background:#ffe3e3; color:#c92a2a; }}
.summary {{ background:#fff; border-radius:8px; padding:16px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); display:flex; gap:32px; flex-wrap:wrap; }}
.stat {{ text-align:center; }}
.stat .val {{ font-size:24px; font-weight:700; }}
.stat .lbl {{ font-size:12px; color:#666; }}
.empty {{ color:#999; padding:20px; text-align:center; }}
</style>
</head>
<body>
<h1>估值汇总筛选报告</h1>
<div class="meta">生成时间: {now} | 数据截至: {results[0]['last_date'] if results else '?'} | 覆盖: {len(results)}只股票 | 数据来源: 个股报告JSON（口径一致）</div>

<div class="summary">
  <div class="stat"><div class="val">{len(results)}</div><div class="lbl">已计算股票</div></div>
  <div class="stat"><div class="val" style="color:#2b8a3e">{len(undervalued)}</div><div class="lbl">百分位&gt;85% (低估区)</div></div>
  <div class="stat"><div class="val" style="color:#e63946">{len(overvalued)}</div><div class="lbl">百分位&lt;40% (高估区)</div></div>
  <div class="stat"><div class="val">{len(results)-len(undervalued)-len(overvalued)}</div><div class="lbl">中间区域</div></div>
</div>

<h2>低估区 — 百分位 &gt; 85%（分数处于历史高位，当前相对历史低估/便宜）</h2>
{'<table><tr><th>股票</th><th>百分位</th><th>分数</th><th>PE</th><th>PB</th><th>价格</th><th>模型</th><th>历史均值</th><th>历史范围</th><th>PE区间</th><th>PB区间</th><th>PE分位窗口</th></tr>' + make_rows(undervalued) + '</table>' if undervalued else '<div class="empty">当前无低估股票</div>'}

<h2 class="over">高估区 — 百分位 &lt; 40%（分数处于历史低位，当前相对历史高估/贵）</h2>
{'<table><tr><th>股票</th><th>百分位</th><th>分数</th><th>PE</th><th>PB</th><th>价格</th><th>模型</th><th>历史均值</th><th>历史范围</th><th>PE区间</th><th>PB区间</th><th>PE分位窗口</th></tr>' + make_rows(overvalued) + '</table>' if overvalued else '<div class="empty">当前无高估股票</div>'}

<div class="meta" style="margin-top:32px;border-top:1px solid #eee;padding-top:12px;">
  百分位含义: 当前分数在N年历史得分序列中的排位（分数高=低估）。85%表示当前分数高于历史85%的交易日——分数偏高，相对历史更便宜（低估）；30%表示当前分数仅高于历史30%的交易日——分数偏低，相对历史更贵（高估）。<br>
  分数含义: 0-100分，越高越低估。80+极度低估 / 70-80低估 / 40-70中性 / 20-40高估 / 0-20极度高估。
</div>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path


def main():
    print(f"{'='*60}")
    print(f"  估值汇总筛选报告")
    print(f"  筛选条件: 百分位 > 85% (低估) 或 < 40% (高估)")
    print(f"{'='*60}")

    results = []
    for code, (name, model) in sorted(ALL_STOCKS.items()):
        print(f"  读取 {name}({code})...", end='', flush=True)
        try:
            r = analyze_stock(code, name, model)
            if r:
                results.append(r)
                print(f" 分数={r['score']:.1f} 百分位={r['percentile']:.0f}%")
            else:
                print(" 无报告JSON（先运行 build_report 生成）")
        except Exception as e:
            print(f" 失败: {e}")

    # 生成HTML
    output = os.path.join(_SKILL_DIR, 'artifacts', 'reports', '估值汇总筛选.html')
    generate_html(results, output)

    # 控制台摘要（分数高=低估：高分位→低估区，低分位→高估区）
    undervalued = [r for r in results if r['percentile'] > 85]
    overvalued = [r for r in results if r['percentile'] < 40]
    print(f"\n{'='*60}")
    print(f"  已分析: {len(results)}只 | 低估区: {len(undervalued)}只 | 高估区: {len(overvalued)}只")
    if undervalued:
        print(f"\n  ◆ 低估区 (百分位>85%):")
        for r in sorted(undervalued, key=lambda x: -x['percentile']):
            print(f"    {r['name']}({r['code']}) 分数{r['score']:.1f} 百分位{r['percentile']:.0f}%")
    if overvalued:
        print(f"\n  ◆ 高估区 (百分位<40%):")
        for r in sorted(overvalued, key=lambda x: x['percentile']):
            print(f"    {r['name']}({r['code']}) 分数{r['score']:.1f} 百分位{r['percentile']:.0f}%")
    print(f"\n  报告: {output}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
