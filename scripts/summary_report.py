#!/usr/bin/env python3
"""
估值汇总报告生成器
- 扫描 watchlist.txt（唯一股票池，batch_growth 为历史中间产物已移除）逐只分析
- 计算每只股票当前分数在历史中的百分位
- 筛选百分位 > 85%（低估区，分数处于历史高位）或 < 40%（高估区，分数处于历史低位）
- 输出单页HTML汇总表
"""
import csv
import json
import os
import sys
import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)

from kline_cache import get_kline, CACHE_DIR, load_cache
from financial_fetcher import (
    fetch_pershare_data, compute_valuation_range,
    fetch_financial_reports, compute_financial_metrics, auto_fill_factors
)

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

# ===== 模型权重 =====
MODEL_NAMES = {
    'staples': '必选消费', 'discretionary': '可选消费', 'tech': '科技制造',
    'cyclical': '周期资源', 'soe': '央企基建', 'bank': '银行保险',
    'realestate': '地产', 'pharma': '医药消费',
}
MODEL_WEIGHTS = {
    'staples':       {'pe': 0.28, 'pb': 0.12, 'peg': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.10, 'margin_stability': 0.10},
    'discretionary': {'pe': 0.22, 'pb': 0.12, 'peg': 0.22, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'brand_premium': 0.11},
    'tech':          {'pe': 0.20, 'pb': 0.12, 'peg': 0.25, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'rd_ratio': 0.10},
    'cyclical':      {'pe': 0.25, 'pb': 0.12, 'commodity_dev': 0.20, 'ma': 0.15, 'vol': 0.10, 'vola': 0.10, 'capacity_util': 0.08},
    'soe':           {'pe': 0.15, 'pb': 0.18, 'dividend_yield': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'order_growth': 0.15, 'roe': 0.04},
    'bank':          {'pe': 0.00, 'pb': 0.30, 'roe': 0.25, 'dividend_yield': 0.15, 'npl_ratio': 0.12, 'ma': 0.10, 'vola': 0.08},
    'realestate':    {'pe': 0.00, 'pb': 0.20, 'nav_discount': 0.25, 'clearance_rate': 0.20, 'ma': 0.12, 'vol': 0.08, 'leverage': 0.10, 'vola': 0.05},
    'pharma':        {'pe': 0.20, 'pb': 0.10, 'peg': 0.25, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'revenue_growth': 0.17},
}
OPTIONAL_KEYS = ['commodity_dev','capacity_util','roe','dividend_yield','npl_ratio',
                 'nav_discount','clearance_rate','leverage','rd_ratio',
                 'margin_stability','brand_premium','order_growth','revenue_growth']


# ===== 评分函数 =====
def score_pe(pe, pe_min, pe_max):
    if pe <= 0: return 50
    return (1 - max(0, min(1, (pe - pe_min) / (pe_max - pe_min)))) * 100

def score_pb(pb, pb_min, pb_max):
    if pb <= 0: return 50
    return (1 - max(0, min(1, (pb - pb_min) / (pb_max - pb_min)))) * 100

def score_peg(pe, growth):
    if pe <= 0 or growth <= 0: return 50
    peg = pe / (growth * 100)
    if peg < 0.8: return 95
    elif peg < 1.0: return 80
    elif peg < 1.2: return 65
    elif peg < 1.5: return 50
    elif peg < 2.0: return 35
    else: return 20

def score_ma(close, ma20, ma60):
    if ma20 <= 0: return 50
    d20 = (close - ma20) / ma20 * 100
    d60 = (close - ma60) / ma60 * 100 if ma60 > 0 else 0
    return max(0, min(100, 50 - d20*3)) * 0.6 + max(0, min(100, 50 - d60*2.5)) * 0.4

def score_vol(volume, vol_ma20):
    if vol_ma20 <= 0: return 50
    r = volume / vol_ma20
    if r < 0.5: return 85
    elif r < 0.8: return 70
    elif r < 1.2: return 55
    elif r < 1.5: return 40
    elif r < 2.0: return 30
    else: return 20

def score_vola(close, high, low):
    if close <= 0: return 50
    v = (high - low) / close
    if v < 0.01: return 85
    elif v < 0.02: return 70
    elif v < 0.03: return 55
    elif v < 0.05: return 40
    else: return 20


def compute_score_series(kline_data, pe, pb, price, pe_min, pe_max, pb_min, pb_max,
                         eps_growth, model_type, opt_factors):
    """计算整条K线的分数序列，返回 [score, ...]"""
    weights = dict(MODEL_WEIGHTS.get(model_type, MODEL_WEIGHTS['cyclical']))
    # 权重重分配
    active_w = {}
    missing_w = 0
    for fk, w in weights.items():
        if fk in OPTIONAL_KEYS:
            if opt_factors.get(fk) is not None:
                active_w[fk] = w
            else:
                missing_w += w
        else:
            active_w[fk] = w
    if missing_w > 0 and active_w:
        tw = sum(active_w.values())
        if tw > 0:
            for fk in active_w:
                active_w[fk] += active_w[fk] / tw * missing_w
    total_w = sum(active_w.values())
    if total_w > 0:
        active_w = {k: v/total_w for k, v in active_w.items()}

    n = len(kline_data)
    scores = []
    for i in range(n):
        row = kline_data[i]
        close = float(row[2])
        high = float(row[3])
        low = float(row[4])
        volume = float(row[5])
        # MA
        ma20 = sum(float(kline_data[j][2]) for j in range(max(0,i-19), i+1)) / min(20, i+1)
        ma60 = sum(float(kline_data[j][2]) for j in range(max(0,i-59), i+1)) / min(60, i+1)
        vol_ma20 = sum(float(kline_data[j][5]) for j in range(max(0,i-19), i+1)) / min(20, i+1)
        # 历史PE/PB推算
        cur_pe = close / price * pe if price > 0 else 0
        cur_pb = close / price * pb if price > 0 else 0

        total = 0
        for fk, w in active_w.items():
            if w < 0.001: continue
            if fk == 'pe': s = score_pe(cur_pe, pe_min, pe_max)
            elif fk == 'pb': s = score_pb(cur_pb, pb_min, pb_max)
            elif fk == 'peg': s = score_peg(cur_pe, eps_growth)
            elif fk == 'ma': s = score_ma(close, ma20, ma60)
            elif fk == 'vol': s = score_vol(volume, vol_ma20)
            elif fk == 'vola': s = score_vola(close, high, low)
            else: s = 50  # 可选因子缺失=中性
            total += s * w
        scores.append(round(total, 2))
    return scores


def analyze_stock(code, name, model):
    """分析单只股票，返回结果dict或None（增量刷新缓存以确保最新数据）"""
    exchange = 'sh' if code.startswith('6') else 'sz'
    # 增量刷新K线缓存：今日已更新则0次API调用，否则仅1次增量调用
    fresh = get_kline(code, exchange)
    kline_data = fresh.get('kline') or []
    if len(kline_data) < 60:
        return None
    pe = fresh.get('pe', 0)
    pb = fresh.get('pb', 0)
    price = fresh.get('price', 0)

    # PE/PB区间
    pershare = fetch_pershare_data(code, exchange)
    val_range = compute_valuation_range(kline_data, pershare, pe, pb)
    pe_min, pe_max = val_range['pe_min'], val_range['pe_max']
    pb_min, pb_max = val_range['pb_min'], val_range['pb_max']

    # 增速 + 可选因子
    eps_growth = 0.08
    opt_factors = {}
    reports = fetch_financial_reports(code, exchange, max_reports=10)
    if reports:
        metrics = compute_financial_metrics(reports)
        opt_factors = auto_fill_factors(opt_factors, metrics, model)
        annuals = [r for r in reports if r['report_type'] == 'annual']
        if len(annuals) >= 2:
            lp = annuals[0]['net_profit']
            oi = min(len(annuals)-1, 4)
            op = annuals[oi]['net_profit']
            if lp > 0 and op > 0 and oi > 0:
                eps_growth = round((lp/op)**(1.0/oi) - 1, 4)

    # 计算全序列分数
    scores = compute_score_series(kline_data, pe, pb, price,
                                  pe_min, pe_max, pb_min, pb_max,
                                  eps_growth, model, opt_factors)
    if not scores:
        return None

    latest_score = scores[-1]
    # 百分位：当前分在历史中的位置
    below = sum(1 for s in scores if s <= latest_score)
    percentile = round(below / len(scores) * 100, 1)

    return {
        'code': code, 'name': name, 'model': model,
        'price': price, 'pe': pe, 'pb': pb,
        'score': latest_score,
        'percentile': percentile,
        'score_min': round(min(scores), 1),
        'score_max': round(max(scores), 1),
        'score_avg': round(sum(scores)/len(scores), 1),
        'pe_range': f"{pe_min}~{pe_max}",
        'pb_range': f"{pb_min}~{pb_max}",
        'days': len(scores),
        'last_date': kline_data[-1][0],
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
<div class="meta">生成时间: {now} | 数据截至: {results[0]['last_date'] if results else '?'} | 覆盖: {len(results)}只股票</div>

<div class="summary">
  <div class="stat"><div class="val">{len(results)}</div><div class="lbl">已计算股票</div></div>
  <div class="stat"><div class="val" style="color:#2b8a3e">{len(undervalued)}</div><div class="lbl">百分位&gt;85% (低估区)</div></div>
  <div class="stat"><div class="val" style="color:#e63946">{len(overvalued)}</div><div class="lbl">百分位&lt;40% (高估区)</div></div>
  <div class="stat"><div class="val">{len(results)-len(undervalued)-len(overvalued)}</div><div class="lbl">中间区域</div></div>
</div>

<h2>低估区 — 百分位 &gt; 85%（分数处于历史高位，当前相对历史低估/便宜）</h2>
{'<table><tr><th>股票</th><th>百分位</th><th>分数</th><th>PE</th><th>PB</th><th>价格</th><th>模型</th><th>历史均值</th><th>历史范围</th><th>PE区间</th><th>PB区间</th></tr>' + make_rows(undervalued) + '</table>' if undervalued else '<div class="empty">当前无低估股票</div>'}

<h2 class="over">高估区 — 百分位 &lt; 40%（分数处于历史低位，当前相对历史高估/贵）</h2>
{'<table><tr><th>股票</th><th>百分位</th><th>分数</th><th>PE</th><th>PB</th><th>价格</th><th>模型</th><th>历史均值</th><th>历史范围</th><th>PE区间</th><th>PB区间</th></tr>' + make_rows(overvalued) + '</table>' if overvalued else '<div class="empty">当前无高估股票</div>'}

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
        cache = load_cache(code)
        if not cache or not cache.get('data'):
            continue
        print(f"  分析 {name}({code})...", end='', flush=True)
        try:
            r = analyze_stock(code, name, model)
            if r:
                results.append(r)
                print(f" 分数={r['score']:.1f} 百分位={r['percentile']:.0f}%")
            else:
                print(" 数据不足")
        except Exception as e:
            print(f" 失败: {e}")

    # 生成HTML
    output = os.path.join(_SKILL_DIR, 'local_reports', '估值汇总筛选.html')
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
