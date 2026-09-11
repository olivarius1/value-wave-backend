#!/usr/bin/env python3
"""
仓位管理变体回测（2026-09-11）
在百分位信号(95/10)之上叠加交易约束，回答三个问题：
  ① 加仓位限制（≤10只/单只≤30%/每笔5%/每月3笔）后结果更好吗？
  ② 短历史股票伪信号（绝对分数<70 却触发 95 分位）用绝对分数门槛能修复吗？
  ③ 更合适的仓位管理（月度 Top-10 等权调仓）表现如何？

变体：
  v0_equal_free   原版等权 95/10（无仓位约束、无成本）——对照
  v0_equal_cost   原版等权 95/10（含 0.1% 单边成本）——对照
  v1_user         用户约束版：≤10只 / 单只≤30% / 每笔5%NAV / 单股单日1笔 / 每月3笔买入
  v2_abs70        v1 + 绝对分数≥70 双确认（修复短历史伪信号）
  v3_min250       v1 + 阈值样本门槛 50→250 天（次新历史不足不出信号）
  v4_top10_m      月度调仓：每月首日，买入区且分数≥70 中按分数取前10、各10%等权再平衡

共同规则：
  卖出 = 分数 ≤ p10 清仓（月度版为跌出目标池清仓）；信号滞后1日（T-1信号→T收盘执行）；
  单边成本0.1%；现金不计息；停牌日不交易（卖出信号挂起到复牌）。
  优先级（同日多候选）：按分数降序（分数=低估综合分，最便宜先买）；
  买到为止 = 现金不足一笔 / 当月3笔用完 / 10只满 / 单只30%封顶，任一闸门关上即停。

评价口径：全期净值/年化/回撤/超额 + 任意起点(季度)1y/3y超额分布 + 弱段(2015-2016/2016-2018)修复 +
  短历史买入(买入时该股历史分数样本<250天)与长历史买入的逐笔收益对比。
输出：artifacts/backtest/position_{ts}/position_results.json + 控制台汇总
"""
import bisect
import datetime
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from backtest_grid import load_data, _quarterly_first_days
from run_backtest import (load_watchlist, compute_percentile_thresholds,
                          simulate_strategy, WATCHLIST_PATH)
from market_regimes import compute_regimes, assign_regimes

COST = 0.001
HORIZONS = ((252, '1y'), (756, '3y'))
WEAK = ('2015~2016熊市', '2016~2018牛市')
OUTPUT_ROOT = os.path.join(os.path.dirname(_SCRIPT_DIR), 'artifacts', 'backtest')


def dist(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return {'n': n, 'median': round(xs[n // 2], 4), 'mean': round(sum(xs) / n, 4),
            'p10': round(xs[int(n * 0.1)], 4), 'p90': round(xs[min(n - 1, int(n * 0.9))], 4),
            'win': round(sum(1 for x in xs if x > 0) / n, 4)}


def med(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[len(xs) // 2], 4)


def curve_stats(curve):
    navs = [v for _, v in curve]
    n = len(navs)
    ratio = navs[-1] / navs[0]
    annual = ratio ** (252.0 / n) - 1 if ratio > 0 else None
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {'total_return': round(ratio - 1, 4),
            'annualized': round(annual, 4) if annual is not None else None,
            'max_drawdown': round(mdd, 4), 'days': n}


def _carry(dc):
    ds = sorted(dc)
    return ds, [dc[d] for d in ds]


class PortfolioSim:
    """现金+持股的事件驱动组合模拟器（收盘价成交，信号滞后1日）。"""

    def __init__(self, stock_daily, daily_close, thr):
        self.score_by = {c: {x['date']: x['score'] for x in daily}
                         for c, daily in stock_daily.items()}
        self.hist_dates = {c: [x['date'] for x in daily] for c, daily in stock_daily.items()}
        self.arr = {c: _carry(dc) for c, dc in daily_close.items()}
        self.dc = daily_close
        self.thr = thr

    def _env(self, wdates):
        return {'cash': 1.0, 'shares': {}, 'buys_open': {}, 'pending_sell': set(),
                'mcount': {}, 'curve': [], 'trades': [],
                'short_rets': [], 'long_rets': [], 'expo_sum': 0.0,
                'min_cash': 1.0, 'max_w': 0.0, 'm_cands': []}

    def _nav(self, st, d, cash):
        total = cash
        for c, sh in st['shares'].items():
            ds, px = self.arr[c]
            i = bisect.bisect_right(ds, d)
            if i:
                total += sh * px[i - 1]
        return total

    def _risk_probe(self, st, d, cash, nav):
        if cash < st['min_cash']:
            st['min_cash'] = cash
        if nav > 0:
            for c, sh in st['shares'].items():
                ds, px = self.arr[c]
                i = bisect.bisect_right(ds, d)
                if i:
                    w = sh * px[i - 1] / nav
                    if w > st['max_w']:
                        st['max_w'] = w

    def _close_buy(self, st, c, p):
        for bprice, bn in st['buys_open'].pop(c, []):
            r = p / bprice - 1
            (st['short_rets'] if bn < 250 else st['long_rets']).append(r)

    def _finish(self, st, wdates):
        dlast = wdates[-1]
        for c in list(st['buys_open']):
            ds, px = self.arr[c]
            i = bisect.bisect_right(ds, dlast)
            if i:
                self._close_buy(st, c, px[i - 1])
        return st

    # ---- v1/v2/v3：分笔建仓 ----
    def run_chunk(self, wdates, *, abs_gate=None, cost=COST, max_stocks=10,
                  cap=0.30, chunk=0.05, monthly_buys=3, trim_cap=False):
        st = self._env(wdates)
        cash, shares = st['cash'], st['shares']
        for i, d in enumerate(wdates):
            dprev = wdates[i - 1] if i else None
            if dprev:
                fresh_buy, fresh_sell = [], []
                for c in self.score_by:
                    s = self.score_by[c].get(dprev)
                    th = self.thr.get(c, {}).get(dprev)
                    if th and s is not None:
                        if s >= th[0] and (abs_gate is None or s >= abs_gate):
                            fresh_buy.append((s, c))
                        elif s <= th[1]:
                            fresh_sell.append(c)
                st['pending_sell'].update(fresh_sell)
                nav = self._nav(st, d, cash)
                # 先卖（清仓释放额度；停牌挂起）
                for c in sorted(st['pending_sell']):
                    if c in shares and self.dc[c].get(d) and self.dc[c][d] > 0:
                        p = self.dc[c][d]
                        val = shares[c] * p
                        cash += val * (1 - cost)
                        self._close_buy(st, c, p)
                        del shares[c]
                        st['pending_sell'].discard(c)
                        st['trades'].append([d, c, 'sell', round(val / nav, 4)])
                # 后买：分数降序，闸门=现金/月度笔数/只数/单只上限
                month = d[:7]
                used = st['mcount'].get(month, 0)
                fresh_buy.sort(reverse=True)
                nav = self._nav(st, d, cash)
                for s, c in fresh_buy:
                    if used >= monthly_buys or cash < chunk * nav:
                        break
                    if not self.dc[c].get(d) or self.dc[c][d] <= 0:
                        continue
                    held = c in shares
                    if not held and len(shares) >= max_stocks:
                        continue
                    w = shares.get(c, 0) * self.dc[c][d] / nav
                    room = cap - w
                    if room <= 0:
                        continue
                    amt = min(chunk, room) * nav
                    if amt < 0.005 * nav:
                        continue
                    p = self.dc[c][d]
                    shares[c] = shares.get(c, 0) + amt / p
                    cash -= amt
                    used += 1
                    pn = bisect.bisect_right(self.hist_dates[c], dprev)
                    st['buys_open'].setdefault(c, []).append((p, pn))
                    st['trades'].append([d, c, 'buy', round(amt / nav, 4), round(s, 1), pn])
                st['mcount'][month] = used
            # 持续封顶（v1b）：单股超 30% 每日削峰回 cap（赢家浮盈不设限会让单股漂到 65%）
            if trim_cap and shares:
                nav = self._nav(st, d, cash)
                for c in list(shares):
                    p = self.dc[c].get(d)
                    if not p or p <= 0:
                        continue
                    w = shares[c] * p / nav
                    if w > cap + 1e-9:
                        cut_sh = (w - cap) * nav / p
                        shares[c] -= cut_sh
                        cash += cut_sh * p * (1 - cost)
                        st['trades'].append([d, c, 'trim', round(w - cap, 4)])
            nav = self._nav(st, d, cash)
            self._risk_probe(st, d, cash, nav)
            st['expo_sum'] += (nav - cash) / nav if nav > 0 else 0
            st['curve'].append((d, round(nav, 6)))
        self._finish(st, wdates)
        st.update({'n_buys': sum(1 for t in st['trades'] if t[2] == 'buy'),
                   'n_sells': sum(1 for t in st['trades'] if t[2] == 'sell'),
                   'avg_expo': round(st['expo_sum'] / len(st['curve']), 4),
                   'min_cash': round(st['min_cash'], 4), 'max_w': round(st['max_w'], 4),
                   'avg_m_cands': round(sum(st['m_cands']) / len(st['m_cands']), 1) if st['m_cands'] else None})
        return st

    # ---- v4：月度 Top-10 等权再平衡 ----
    def run_monthly(self, wdates, *, abs_gate=70, top_n=10, cap=0.30, cost=COST):
        st = self._env(wdates)
        cash, shares = st['cash'], st['shares']
        for i, d in enumerate(wdates):
            dprev = wdates[i - 1] if i else None
            if dprev and d[:7] != wdates[i - 1][:7]:
                nav = self._nav(st, d, cash)
                cands = []
                for c in self.score_by:
                    if not self.dc[c].get(d) or self.dc[c][d] <= 0:
                        continue
                    s = self.score_by[c].get(dprev)
                    th = self.thr.get(c, {}).get(dprev)
                    if th and s is not None and s >= th[0] and s >= abs_gate:
                        cands.append((s, c))
                cands.sort(reverse=True)
                st['m_cands'].append(len(cands))
                k = min(top_n, len(cands))
                w_each = min(1.0 / k, cap) if k else 0.0
                tset = {c for _, c in cands[:top_n]}
                # 清仓跌出目标池的
                for c in list(shares):
                    if c not in tset and self.dc[c].get(d) and self.dc[c][d] > 0:
                        p = self.dc[c][d]
                        val = shares[c] * p
                        cash += val * (1 - cost)
                        self._close_buy(st, c, p)
                        del shares[c]
                        st['trades'].append([d, c, 'sell', round(val / nav, 4)])
                nav = self._nav(st, d, cash)
                # 目标池内再平衡到 10%（分数降序执行，现金不足跳过）
                for s, c in cands[:top_n]:
                    if not self.dc[c].get(d) or self.dc[c][d] <= 0:
                        continue
                    p = self.dc[c][d]
                    cur = shares.get(c, 0) * p
                    delta = w_each * nav - cur
                    if delta > 0.002 * nav and cash >= delta:
                        shares[c] = shares.get(c, 0) + delta / p
                        cash -= delta
                        pn = bisect.bisect_right(self.hist_dates[c], dprev)
                        st['buys_open'].setdefault(c, []).append((p, pn))
                        st['trades'].append([d, c, 'buy', round(delta / nav, 4), round(s, 1), pn])
                    elif delta < -0.002 * nav:
                        cut = -delta / p
                        shares[c] -= cut
                        cash += (-delta) * (1 - cost)
                        if shares[c] < 1e-9:
                            del shares[c]
                        st['trades'].append([d, c, 'trim', round((-delta) / nav, 4)])
            nav = self._nav(st, d, cash)
            self._risk_probe(st, d, cash, nav)
            st['expo_sum'] += (nav - cash) / nav if nav > 0 else 0
            st['curve'].append((d, round(nav, 6)))
        self._finish(st, wdates)
        st.update({'n_buys': sum(1 for t in st['trades'] if t[2] == 'buy'),
                   'n_sells': sum(1 for t in st['trades'] if t[2] == 'sell'),
                   'avg_expo': round(st['expo_sum'] / len(st['curve']), 4),
                   'min_cash': round(st['min_cash'], 4), 'max_w': round(st['max_w'], 4),
                   'avg_m_cands': round(sum(st['m_cands']) / len(st['m_cands']), 1) if st['m_cands'] else None})
        return st


def sim_bench(daily_close, wdates):
    nav, curve = 1.0, []
    for i, d in enumerate(wdates):
        if i:
            rets = []
            dp = wdates[i - 1]
            for c, dc in daily_close.items():
                if dc.get(d) and dc.get(dp) and dc[d] > 0 and dc[dp] > 0:
                    rets.append(dc[d] / dc[dp] - 1)
            if rets:
                nav *= 1 + sum(rets) / len(rets)
        curve.append((d, round(nav, 6)))
    return curve


def main():
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    stocks = load_watchlist(WATCHLIST_PATH)
    print(f'仓位管理变体回测：{len(stocks)} 只')
    stock_daily, daily_close, fut_map = load_data(stocks)
    if not stock_daily:
        print('无可用数据，退出')
        sys.exit(1)
    dates = sorted(set().union(*[set(dc.keys()) for dc in daily_close.values()]))
    regime_of = assign_regimes(dates, compute_regimes())
    thr95 = compute_percentile_thresholds(stock_daily, lo=0.10, hi=0.95)
    thr250 = compute_percentile_thresholds(stock_daily, lo=0.10, hi=0.95, min_samples=250)
    sim95 = PortfolioSim(stock_daily, daily_close, thr95)
    sim250 = PortfolioSim(stock_daily, daily_close, thr250)
    start = min(daily[0]['date'] for daily in stock_daily.values())

    variants = {
        'v1_user':    ('chunk', thr95, None, False),
        'v1b_cap30':  ('chunk', thr95, None, True),
        'v2_abs70':   ('chunk', thr95, 70, False),
        'v3_min250':  ('chunk', thr250, None, False),
        'v4_top10_m': ('monthly', thr95, 70, False),
    }

    def run_variant(name, wdates):
        fn, thr, gate, trim = variants[name]
        sim = sim95 if thr is thr95 else sim250
        if fn == 'chunk':
            return sim.run_chunk(wdates, abs_gate=gate, trim_cap=trim)
        return sim.run_monthly(wdates, abs_gate=gate)

    def run_v0(wdates, cost):
        sub_close = {c: {d: v for d, v in dc.items() if wdates[0] <= d <= wdates[-1]}
                     for c, dc in daily_close.items()}
        sub_daily = {c: [x for x in daily if wdates[0] <= x['date'] <= wdates[-1]]
                     for c, daily in stock_daily.items()}
        sub_daily = {c: v for c, v in sub_daily.items() if v}
        return simulate_strategy(sub_daily, sub_close, {}, mode='pct',
                                 pct_thr=thr95, cost=cost)

    results = {'generated_at': ts, 'stock_count': len(stock_daily),
               'params': {'cost': COST, 'max_stocks': 10, 'cap': 0.30, 'chunk': 0.05,
                          'monthly_buys': 3, 'abs_gate': 70, 'min_thr_samples_v3': 250,
                          'pct': 'buy p95 / sell p10, PIT annual slices'},
               'variants': {}}

    # ===== 全期 =====
    wdates_full = [d for d in dates if d >= start]
    bench_full = curve_stats(sim_bench(daily_close, wdates_full))
    results['bench_full'] = bench_full
    print(f"\n基准(闭眼持有等权): {bench_full['total_return']:+.0%}  年化 {bench_full['annualized']:+.1%}  "
          f"回撤 {bench_full['max_drawdown']:.1%}  区间 {wdates_full[0]}~{wdates_full[-1]}")
    for name in variants:
        r = run_variant(name, wdates_full)
        st = curve_stats(r['curve'])
        results['variants'][name] = {
            'full': st, 'n_buys': r['n_buys'], 'n_sells': r['n_sells'],
            'avg_expo': r['avg_expo'],
            'short_buy_median_ret': med(r['short_rets']), 'n_short_buys': len(r['short_rets']),
            'long_buy_median_ret': med(r['long_rets']), 'n_long_buys': len(r['long_rets'])}
        print(f"{name:<12} {st['total_return']:+7.0%}  年化 {st['annualized']:+.1%}  "
              f"回撤 {st['max_drawdown']:6.1%}  超额 {st['total_return'] - bench_full['total_return']:+6.0%}  "
              f"买入{r['n_buys']:>4}笔  平均仓位{r['avg_expo']:.0%}  单股峰值{r['max_w']:.0%}  最小现金{r['min_cash']:.0%}  "
              f"短历史买入中位 {med(r['short_rets']) if r['short_rets'] else '—'}(n={len(r['short_rets'])})  "
              f"长历史中位 {med(r['long_rets']) if r['long_rets'] else '—'}(n={len(r['long_rets'])})")
    for key, cost, tag in (('v0_equal_free', 0.0, '无成本'), ('v0_equal_cost', COST, '含成本')):
        r = run_v0(wdates_full, cost)
        st = r['strategy']
        results['variants'][key] = {'full': st}
        print(f"{key:<12} {st['total_return']:+7.0%}  年化 {st['annualized']:+.1%}  "
              f"回撤 {st['max_drawdown']:6.1%}  超额 {st['total_return'] - bench_full['total_return']:+6.0%}")

    # ===== 任意起点（季度入场 × 1y/3y），窗口集以 v0 的过滤口径为准，保证可比 =====
    entries_all = _quarterly_first_days(dates)
    windows = {'1y': [], '3y': []}
    for entry in entries_all:
        i0 = dates.index(entry)
        for h, key in HORIZONS:
            if i0 + h >= len(dates):
                continue
            wdates = dates[i0:i0 + h + 1]
            bench_total = curve_stats(sim_bench(daily_close, wdates))['total_return']
            r = run_v0(wdates, COST)
            if not r or r['strategy']['days'] < h * 0.6:
                continue
            windows[key].append((entry, wdates, bench_total, regime_of.get(entry, '?'),
                                 round(r['strategy']['total_return'] - bench_total, 4)))

    def rolling_block(per):
        blk = {}
        for key in ('1y', '3y'):
            blk[key] = dist([e['excess'] for e in per[key]])
            by_reg = {}
            for e in per[key]:
                by_reg.setdefault(e['regime'], []).append(e['excess'])
            blk[f'{key}_by_regime'] = {k: dist(v) for k, v in sorted(by_reg.items())}
        blk['weak3y'] = dist([e['excess'] for e in per['3y'] if e['regime'] in WEAK])
        return blk

    def print_rolling(name, blk):
        w = blk['weak3y'] or {'median': 0, 'win': 0, 'n': 0}
        print(f"{name:<16} 1y中位 {blk['1y']['median']:+.1%}(胜率{blk['1y']['win']:.0%},n={blk['1y']['n']})  "
              f"3y中位 {blk['3y']['median']:+.1%}(胜率{blk['3y']['win']:.0%},n={blk['3y']['n']})  "
              f"弱段3y中位 {w['median']:+.1%}(胜率{w['win']:.0%},n={w['n']})")

    per0 = {key: [{'entry': e, 'regime': reg, 'excess': exc}
                  for e, _, _, reg, exc in windows[key]] for key in ('1y', '3y')}
    blk = rolling_block(per0)
    results['variants']['v0_equal_cost']['rolling'] = blk
    print_rolling('v0_equal(含成本)', blk)

    for name in variants:
        per = {key: [] for key in ('1y', '3y')}
        for key in ('1y', '3y'):
            for entry, wdates, bench_total, reg, _ in windows[key]:
                r = run_variant(name, wdates)
                st = curve_stats(r['curve'])
                per[key].append({'entry': entry, 'regime': reg,
                                 'excess': round(st['total_return'] - bench_total, 4)})
        blk = rolling_block(per)
        results['variants'][name]['rolling'] = blk
        print_rolling(name, blk)

    out_dir = os.path.join(OUTPUT_ROOT, f'position_{ts}')
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'position_results.json'), 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'\n输出: {out_dir}/position_results.json')


if __name__ == '__main__':
    main()
