#!/usr/bin/env python3
"""
因子级有效性与引擎版本对比分析（复用 run_backtest / backtest_engine，只读缓存）

维度：
1. 因子级 IC：单因子分数 s_<fk> vs 未来 60/250 日收益的 Spearman 秩相关，
   回答"哪些因子在贡献信号、哪些在贡献噪声"。
2. 引擎对比：审计前(单日振幅波动率+8因子权重) vs 现行(20日滚动std+7因子)，
   用同一份 PIT 因子分数重组装总分，量化审计改动的边际贡献。

用法：
    python scripts/factor_analysis.py            # 全量 watchlist
    python scripts/factor_analysis.py 600887     # 指定股票

输出: artifacts/backtest/factor_analysis.json + 控制台摘要
"""
import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from run_backtest import (
    load_watchlist, future_returns, spearman, weekly_sample,
    FUTURE_HORIZONS, WEEKLY_SAMPLE, OUTPUT_ROOT, BACKTEST_YEARS,
)
from kline_cache import get_kline, get_kline_raw
from financial_fetcher import fetch_pershare_data, fetch_financial_reports, fetch_stock_info
from scoring_engine import MODEL_PRESETS, score_volatility as score_volatility_new
from backtest_engine import compute_pit_scores

# 引擎对比基准：cf26193（7因子：含20日滚动std波动率+量能）。
# 更早的单日振幅口径已从引擎移除，其 IC 已在审计记录中留档（IC250=0.1084，重建口径见审计文档）。
_OLD_WEIGHTS = {
    'staples': {'pe': 0.28, 'pb': 0.12, 'peg': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.10, 'margin_stability': 0.10},
    'discretionary': {'pe': 0.22, 'pb': 0.12, 'peg': 0.22, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'brand_premium': 0.11},
    'tech': {'pe': 0.20, 'pb': 0.12, 'peg': 0.25, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'rd_ratio': 0.10},
    'cyclical': {'pe': 0.243, 'pb': 0.116, 'commodity_dev': 0.194, 'ma': 0.145, 'vol': 0.097, 'vola': 0.097, 'dividend_yield': 0.108},
    'soe': {'pe': 0.156, 'pb': 0.188, 'dividend_yield': 0.209, 'ma': 0.125, 'vol': 0.083, 'vola': 0.083, 'order_growth': 0.156},
    'bank': {'pb': 0.30, 'roe': 0.25, 'dividend_yield': 0.15, 'npl_ratio': 0.12, 'ma': 0.10, 'vola': 0.08},
    'realestate': {'pb': 0.20, 'nav_discount': 0.25, 'clearance_rate': 0.20, 'ma': 0.12, 'vol': 0.08, 'leverage': 0.10, 'vola': 0.05},
    'pharma': {'pe': 0.20, 'pb': 0.10, 'peg': 0.25, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'revenue_growth': 0.17},
}


def load_data(stocks):
    data = {}
    for st in stocks:
        code, model = st['code'], st['model']
        exchange = 'sh' if code.startswith('6') else 'sz'
        qfq = get_kline(code, exchange, years=BACKTEST_YEARS, fq='hfq')['kline']
        raw = get_kline_raw(code, exchange)
        if not qfq or not raw:
            continue
        pershare = fetch_pershare_data(code, exchange)
        reports = fetch_financial_reports(code, exchange)
        info = fetch_stock_info(code, exchange)
        weights = MODEL_PRESETS.get(model, MODEL_PRESETS['cyclical'])['weights']
        daily = compute_pit_scores(qfq, raw, reports, pershare, weights,
                                   info.get('total_shares', 0))
        if not daily:
            continue
        closes = {r[0]: float(r[2]) for r in qfq}
        fut = future_returns([{'date': d, 'close': c} for d, c in sorted(closes.items())],
                             FUTURE_HORIZONS)
        data[code] = {'model': model, 'daily': daily, 'fut': fut, 'closes': closes,
                      'inputs': (qfq, raw, reports, pershare, info.get('total_shares', 0))}
    return data


def _sampled_pairs(samples, fut, factor_key=None, total_key='score'):
    """周频采样的 (因子值, 未来收益) 对；factor_key=None 用总分"""
    out = []
    for s in weekly_sample(samples):
        f = fut.get(s['date'])
        if not f:
            continue
        x = s.get(factor_key) if factor_key else s.get(total_key)
        if x is None:
            continue
        for h in FUTURE_HORIZONS:
            if f.get(f'fut_{h}') is not None:
                out.append((h, x, f[f'fut_{h}']))
    return out


def factor_ic(data):
    """单因子 pooled IC：{factor: {horizon: {'ic','n'}}}"""
    factors = set()
    for d in data.values():
        for s in d['daily']:
            factors.update(k[2:] for k in s if k.startswith('s_'))
    result = {}
    for fk in sorted(factors):
        pairs = []
        for d in data.values():
            pairs += _sampled_pairs(d['daily'], d['fut'], factor_key=f's_{fk}')
        per_h = {}
        for h in FUTURE_HORIZONS:
            xs = [x for hh, x, _ in pairs if hh == h]
            ys = [y for hh, _, y in pairs if hh == h]
            if len(xs) >= 100:
                per_h[str(h)] = {'ic': round(spearman(xs, ys), 4), 'n': len(xs)}
        if per_h:
            result[fk] = per_h
    return result


def _pooled_ic(scores_by_code, data, horizon):
    """跨股票池化的总分 IC（周频采样）"""
    xs, ys = [], []
    for code, scores in scores_by_code.items():
        fut = data[code]['fut']
        samples = [{'date': dt, 'score': sc} for dt, sc in sorted(scores.items())]
        pairs = _sampled_pairs(samples, fut, total_key='score')
        xs += [x for h, x, _ in pairs if h == horizon]
        ys += [y for h, _, y in pairs if h == horizon]
    return round(spearman(xs, ys), 4) if len(xs) >= 100 else None, len(xs)


def compare_engines(data):
    """现行5因子 vs cf26193七因子(含滚动std波动率/量能) 的 pooled IC 对比"""
    current = {c: {s['date']: s['score'] for s in d['daily']} for c, d in data.items()}
    prev = {}
    for code, d in data.items():
        qfq, raw, reports, pershare, shares = d['inputs']
        weights = _OLD_WEIGHTS.get(d['model']) or MODEL_PRESETS[d['model']]['weights']
        daily = compute_pit_scores(qfq, raw, reports, pershare, weights, shares)
        prev[code] = {s['date']: s['score'] for s in daily}
    result = {}
    for label, scores in (('current_5factor', current), ('prev_7factor', prev)):
        ic250, n = _pooled_ic(scores, data, 250)
        ic60, _ = _pooled_ic(scores, data, 60)
        result[label] = {'ic_60': ic60, 'ic_250': ic250, 'n': n}
    return result


def main():
    parser = argparse.ArgumentParser(description='因子级IC与引擎版本对比')
    parser.add_argument('codes', nargs='*', help='仅分析指定代码')
    args = parser.parse_args()
    stocks = load_watchlist(os.path.join(os.path.dirname(_SCRIPT_DIR), 'watchlist.txt'))
    if args.codes:
        keep = set(args.codes)
        stocks = [s for s in stocks if s['code'] in keep]
    print(f'分析 {len(stocks)} 只...')
    data = load_data(stocks)
    print(f'有效 {len(data)} 只')

    fic = factor_ic(data)
    print('\n=== 因子级 pooled IC ===')
    for fk, per_h in sorted(fic.items(), key=lambda kv: -abs(kv[1].get('250', {}).get('ic', 0))):
        h60 = per_h.get('60', {})
        h250 = per_h.get('250', {})
        print(f"  {fk:16s} IC60={h60.get('ic', '-')!s:>7}  IC250={h250.get('ic', '-')!s:>7}  n250={h250.get('n', '-')}")

    eng = compare_engines(data)
    print('\n=== 引擎对比 (pooled IC) ===')
    for label, v in eng.items():
        print(f"  {label:18s} IC60={v['ic_60']}  IC250={v['ic_250']}  n={v['n']}")

    out = os.path.join(OUTPUT_ROOT, 'factor_analysis.json')
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'factor_ic': fic, 'engine_compare': eng}, f, ensure_ascii=False, indent=1)
    print(f'\n写入 {out}')


if __name__ == '__main__':
    main()
