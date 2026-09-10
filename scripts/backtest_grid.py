#!/usr/bin/env python3
"""
百分位策略参数网格搜索（rolling-entry 任意起点检验）

问题：相对百分位策略（分数处于个股自身历史 hi 分位以上买入 / lo 分位以下卖出）
的 hi/lo 取多少最好？候选：买入 {0.80, 0.90, 0.95} × 卖出 {0.30, 0.20, 0.10}，共 9 组合。

评价口径（2026-09 与用户确认）：
- 主指标 rolling-entry：以历史上每个季度首个交易日为入场点，模拟"从该点起执行
  策略 1 年 / 3 年"，统计相对基准（等权买入持有）的超额收益分布——
  入场时点任意，不依赖"段首入场"假设；每个入场点按当时牛熊段打标签。
- 辅助：全期模拟（成本 0 / 0.1% 两档）、换手率。
- 分段信号诊断（IC）与组合无关（组合只改交易规则），见 run_backtest 的 regimes 段。

用法：python scripts/backtest_grid.py [--stocks 600887,...]
输出：artifacts/backtest/grid_{ts}/grid_results.json + grid_report.html + 控制台结论
"""
import argparse
import datetime
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from run_backtest import (
    load_watchlist, future_returns, compute_percentile_thresholds, simulate_strategy,
    FUTURE_HORIZONS, OUTPUT_ROOT, BACKTEST_YEARS, WATCHLIST_PATH,
)
from kline_cache import get_kline, get_kline_raw
from financial_fetcher import fetch_pershare_data, fetch_financial_reports, fetch_stock_info
from scoring_engine import MODEL_PRESETS
from backtest_engine import compute_pit_scores
from market_regimes import compute_regimes, assign_regimes

BUY_GRID = (0.80, 0.90, 0.95)
SELL_GRID = (0.30, 0.20, 0.10)
HORIZONS_YEARS = (1, 3)       # rolling-entry 持有期（年）
ENTRY_FREQ_MONTHS = 3         # 入场点采样：每季度首日
COST_PER_FLIP = 0.001         # 成本敏感性：单边 0.1%


def load_data(stocks, say=print):
    """PIT 分数 + 收益数据（每只股票只算一次，9 组合复用）"""
    stock_daily, daily_close, fut_map = {}, {}, {}
    for si, st in enumerate(stocks, 1):
        code, model = st['code'], st['model']
        exchange = 'sh' if code.startswith('6') else 'sz'
        try:
            qfq = get_kline(code, exchange, years=BACKTEST_YEARS, fq='hfq')['kline']
            raw = get_kline_raw(code, exchange, years=BACKTEST_YEARS)
            if not qfq or not raw:
                say(f"[{si}] {code} K线不足，跳过")
                continue
            pershare = fetch_pershare_data(code, exchange)
            reports = fetch_financial_reports(code, exchange)
            info = fetch_stock_info(code, exchange)
            weights = MODEL_PRESETS.get(model, MODEL_PRESETS['cyclical'])['weights']
            daily = compute_pit_scores(qfq, raw, reports, pershare, weights,
                                       info.get('total_shares', 0))
            if not daily:
                say(f"[{si}] {code} 无有效分数，跳过")
                continue
            closes = {r[0]: float(r[2]) for r in qfq}
            stock_daily[code] = daily
            daily_close[code] = closes
            fut_map[code] = future_returns(
                [{'date': d, 'close': c} for d, c in sorted(closes.items())], FUTURE_HORIZONS)
            say(f"[{si}/{len(stocks)}] {st['name']}({code}) 分数 {len(daily)} 天 [{daily[0]['date']}~]")
        except Exception as e:
            say(f"[{si}] {code} 失败: {e}")
    return stock_daily, daily_close, fut_map


def _quarterly_first_days(dates):
    """每个季度首个交易日"""
    out, seen = [], set()
    for d in dates:
        q = f"{d[:4]}Q{(int(d[5:7]) - 1) // 3 + 1}"
        if q not in seen:
            seen.add(q)
            out.append(d)
    return out


def _window_subset(stock_daily, daily_close, start, end):
    """截取 [start, end] 窗口（simulate_strategy 以窗口首日为入场日）"""
    sub_daily = {c: [d for d in daily if start <= d['date'] <= end]
                 for c, daily in stock_daily.items()}
    sub_daily = {c: v for c, v in sub_daily.items() if v}
    sub_close = {c: {d: v for d, v in dc.items() if start <= d <= end}
                 for c, dc in daily_close.items()}
    return sub_daily, sub_close


def rolling_entry(stock_daily, daily_close, pct_thr, horizon_years, regime_of, dates):
    """任意起点检验：每季度入场，持有 N 年，返回 (策略结果, 基准结果) 列表"""
    horizon_days = int(horizon_years * 252)
    results = []
    entries = _quarterly_first_days(dates)
    for entry in entries:
        # 按交易日数近似截断持有期
        i0 = dates.index(entry)
        if i0 + horizon_days >= len(dates):
            continue  # 窗口不足，跳过（越接近数据末日的入场点少测长持有期）
        end = dates[i0 + horizon_days]
        sub_daily, sub_close = _window_subset(stock_daily, daily_close, entry, end)
        if not sub_daily:
            continue
        res = simulate_strategy(sub_daily, sub_close, {}, mode='pct', pct_thr=pct_thr,
                                cost=COST_PER_FLIP)
        if not res or res['strategy']['days'] < horizon_days * 0.6:
            continue
        results.append({
            'entry': entry, 'regime': regime_of.get(entry, 'pre_index'),
            'strategy': res['strategy']['total_return'],
            'bench': res['bench']['total_return'],
            'excess': round(res['excess_total'], 4),
        })
    return results


def _dist_stats(entries):
    """超额收益分布统计"""
    if not entries:
        return None
    xs = sorted(e['excess'] for e in entries)
    n = len(xs)
    return {
        'n': n,
        'median': round(xs[n // 2], 4),
        'mean': round(sum(xs) / n, 4),
        'p10': round(xs[int(n * 0.1)], 4),
        'p90': round(xs[min(n - 1, int(n * 0.9))], 4),
        'positive_share': round(sum(1 for x in xs if x > 0) / n, 4),
    }


def main():
    parser = argparse.ArgumentParser(description='百分位策略参数网格搜索')
    parser.add_argument('--stocks', help='仅指定股票代码（逗号分隔）')
    args = parser.parse_args()
    stocks = load_watchlist(WATCHLIST_PATH)
    if args.stocks:
        keep = {c.strip() for c in args.stocks.split(',')}
        stocks = [s for s in stocks if s['code'] in keep]
    print(f'网格搜索 {len(stocks)} 只，{len(BUY_GRID)}×{len(SELL_GRID)} 组合')

    stock_daily, daily_close, fut_map = load_data(stocks)
    if not stock_daily:
        print('无可用数据，退出')
        sys.exit(1)
    dates = sorted(set().union(*[set(dc.keys()) for dc in daily_close.values()]))
    regimes = compute_regimes()
    regime_of = assign_regimes(dates, regimes)

    combos = [(hi, lo) for hi in BUY_GRID for lo in SELL_GRID]
    grid = {}
    for hi, lo in combos:
        key = f'buy{int(hi*100)}_sell{int(lo*100)}'
        thr = compute_percentile_thresholds(stock_daily, lo=lo, hi=hi)
        full = simulate_strategy(stock_daily, daily_close, fut_map, mode='pct', pct_thr=thr)
        full_cost = simulate_strategy(stock_daily, daily_close, fut_map, mode='pct',
                                      pct_thr=thr, cost=COST_PER_FLIP)
        entry = {}
        by_regime = {}
        for hy in HORIZONS_YEARS:
            rs = rolling_entry(stock_daily, daily_close, thr, hy, regime_of, dates)
            entry[f'{hy}y'] = _dist_stats(rs)
            # 按入场时点的牛熊段分组
            groups = {}
            for r in rs:
                groups.setdefault(r['regime'], []).append(r['excess'])
            by_regime[f'{hy}y'] = {k: _dist_stats([{'excess': v} for v in vs])
                                   for k, vs in sorted(groups.items())}
        grid[key] = {
            'buy': hi, 'sell': lo,
            'full': {k: full[k] for k in ('start', 'end', 'strategy', 'bench',
                                          'excess_total', 'winrate', 'turnover_ratio')} if full else None,
            'full_cost': {'strategy': full_cost['strategy']} if full_cost else None,
            'rolling': entry,
            'rolling_by_regime': by_regime,
        }
        med3 = entry.get('3y') or {}
        print(f"  {key}: 全期 {full['strategy']['total_return']:+.0%} "
              f"(含成本 {full_cost['strategy']['total_return']:+.0%}) "
              f"3y任意起点中位超额 {med3.get('median', 0):+.1%} "
              f"胜率 {med3.get('positive_share', 0):.0%} (n={med3.get('n', 0)})")

    # ===== 推荐判定：3y 任意起点中位超额为主 + 胜率 + 全期回撤（多准则中位排名）====
    def _rank_stats():
        keys = list(grid)
        metrics_per = {}
        for k in keys:
            r3 = grid[k]['rolling'].get('3y') or {}
            metrics_per[k] = {
                'med3': r3.get('median') or -9,
                'win3': r3.get('positive_share') or 0,
                'mdd': (grid[k]['full'] or {}).get('strategy', {}).get('max_drawdown') or -9,
            }
        ranks = {k: [] for k in keys}
        for metric in ('med3', 'win3', 'mdd'):
            ordered = sorted(keys, key=lambda k: -metrics_per[k][metric])
            for i, k in enumerate(ordered):
                ranks[k].append(i + 1)
        return {k: sum(v) / len(v) for k, v in ranks.items()}

    rank_avg = _rank_stats()
    best = min(rank_avg, key=rank_avg.get)
    print('\n=== 推荐排名（3y中位超额/胜率/回撤 三准则平均名次，越小越好）===')
    for k in sorted(rank_avg, key=rank_avg.get):
        print(f"  {k}: 平均名次 {rank_avg[k]:.1f}")
    print(f"\n推荐组合: {best}（注意：9 选 1 的最优存在多重比较高估，结论看排名稳定性而非单点数字）")

    # ===== 输出 =====
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(OUTPUT_ROOT, f'grid_{ts}')
    os.makedirs(out_dir, exist_ok=True)
    results = {
        'generated_at': ts,
        'params': {'buy_grid': list(BUY_GRID), 'sell_grid': list(SELL_GRID),
                   'horizons_years': list(HORIZONS_YEARS), 'cost_per_flip': COST_PER_FLIP,
                   'stock_count': len(stock_daily), 'regimes': regimes},
        'grid': grid,
        'recommendation': {'best': best, 'rank_avg': rank_avg},
    }
    with open(os.path.join(out_dir, 'grid_results.json'), 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    _build_html(os.path.join(out_dir, 'grid_report.html'), results, grid)
    latest = os.path.join(OUTPUT_ROOT, 'grid_latest.html')
    with open(latest, 'w', encoding='utf-8') as f:
        f.write(open(os.path.join(out_dir, 'grid_report.html'), encoding='utf-8').read())
    print(f'\n输出: {out_dir}/grid_results.json')
    print(f'固定入口: {latest}')


def _build_html(path, results, grid):
    """轻量网格报告：汇总表 + 3y 任意起点分组热力表"""
    import html as _html
    echarts = ''
    epath = os.path.join(os.path.dirname(_SCRIPT_DIR), '_shared', 'js', 'echarts.min.js')
    if os.path.exists(epath):
        echarts = f'<script>{open(epath, encoding="utf-8").read()}</script>'

    keys = sorted(grid, key=lambda k: -((grid[k]['rolling'].get('3y') or {}).get('median') or -9))
    rows = []
    for k in keys:
        g = grid[k]
        f3 = g['rolling'].get('3y') or {}
        f1 = g['rolling'].get('1y') or {}
        full = (g['full'] or {}).get('strategy', {})
        rows.append(f"<tr><td>{_html.escape(k)}</td>"
                    f"<td>{(g['full'] or {}).get('excess_total', 0):+.1%}</td>"
                    f"<td>{full.get('max_drawdown', 0):.1%}</td>"
                    f"<td>{(g['full_cost'] or {}).get('strategy', {}).get('total_return', 0):+.1%}</td>"
                    f"<td>{(f1.get('median') or 0):+.1%}</td>"
                    f"<td>{(f3.get('median') or 0):+.1%}</td>"
                    f"<td>{(f3.get('positive_share') or 0):.0%}</td>"
                    f"<td>{f3.get('n', 0)}</td></tr>")
    table = ('<table><tr><th>组合</th><th>全期超额</th><th>全期回撤</th><th>全期(含成本)</th>'
             '<th>1y中位超额</th><th>3y中位超额</th><th>3y胜率</th><th>3y入场点数</th></tr>'
             + ''.join(rows) + '</table>')

    # 分环境热力表（3y 中位超额）
    regime_labels = sorted({r['label'] for r in results['params']['regimes']})
    heat_rows = []
    for k in keys:
        cells = []
        by_regime = grid[k]['rolling_by_regime'].get('3y', {})
        for rl in regime_labels:
            st = by_regime.get(rl)
            v = st['median'] if st else None
            color = ''
            if v is not None:
                color = '#2b8a3e' if v > 0 else '#c92a2a'
            cells.append(f'<td style="color:{color}">{"—" if v is None else f"{v:+.1%}"}</td>')
        heat_rows.append(f'<tr><td>{_html.escape(k)}</td>{"".join(cells)}</tr>')
    heat = ('<table><tr><th>组合＼入场时点所属段</th>'
            + ''.join(f'<th>{_html.escape(rl)}</th>' for rl in regime_labels) + '</tr>'
            + ''.join(heat_rows) + '</table>')

    best = results['recommendation']['best']
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>百分位策略网格搜索</title><style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#ddd}}
h1{{font-size:20px}} table{{border-collapse:collapse;margin:12px 0;font-size:13px}}
td,th{{border:1px solid #333;padding:4px 10px;text-align:right}}
th{{background:#1a1a1a}} .note{{color:#888;font-size:12px;max-width:900px}}
</style></head><body>
<h1>百分位策略网格搜索（{results['params']['stock_count']} 只，买入 {'/'.join(str(int(b*100)) for b in results['params']['buy_grid'])} × 卖出 {'/'.join(str(int(s*100)) for s in results['params']['sell_grid'])}）</h1>
<p class="note">主指标为 rolling-entry 任意起点检验：历史上每个季度首个交易日入场、持有 1/3 年、含 {results['params']['cost_per_flip']:.1%} 单边成本，
统计相对基准（等权买入持有）的超额收益分布。分段收益的"段首入场"假设不用于评价（见 market_regimes.py 用途边界）。
推荐组合：<b>{_html.escape(best)}</b>（9 选 1 存在多重比较高估，重排名稳定性）。</p>
{table}
<h2 style="font-size:16px">3y 中位超额 × 入场时点所属牛熊段</h2>
{heat}
<p class="note">生成于 {results['generated_at']} | 结果明细 grid_results.json</p>
</body></html>"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
