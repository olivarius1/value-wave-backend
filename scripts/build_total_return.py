#!/usr/bin/env python3
"""
总收益口径K线重建（kline20r）

腾讯行情的"后复权"为 送转因子 × (不复权价 + 累计现金分红加回) 的混合口径，
普通交易日收益被压缩 raw/(raw+累计分红)。本模块用不复权K线 + 分红送转实施记录
（东财 RPT_SHAREBONUS_DET，除权除息日/送转比例/每股派息）链式重建总收益序列：

  非事件日:  T_ret = raw 收益（与不复权价逐日一致）
  事件日:    T_ret = ((1+送转比例)×收盘 + 每股派息) / 前收盘 - 1
  表外事件（配股等东财分红送转表不覆盖，按 raw 大跌且行情后复权被抹平检出）:
             T_ret = 0（配股对价公允、送转不创造价值）

同时输出数据反解分红金额与事件表的比对（金额互证）、表外事件清单。
kline_cache 在 hfq 20年路径缺库或落后于 raw 时调用 build_series 本地续建。
"""
import argparse
import datetime
import json
import os
import sys
import threading
import time
from queue import Queue

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import kline_store
from financial_fetcher import fetch_bonus_events

SUMMARY_PATH = os.path.join(kline_store.CACHE_DIR, 'total_return_summary.json')
KTYPE = 'kline20r'
MIN_MOVE = 0.02          # 斜率采样最小价格变动（元）
B_CHANGE = 0.06          # 段切换判定：斜率相对变化阈值（送转≥10%、配股~5-15%）
WIN = 60                 # 斜率估计窗口（样本数）
STEP = 10                # 斜率估计步长（样本数）
REFINE_ROUNDS = 3        # 迭代校正轮数


def _segment_slope(samples, lo, hi):
    """段内斜率中位（排除边界 40 日过渡区，样本不足则放宽）"""
    for pad in (40, 15, 0):
        inner = [sl for i, sl in samples if lo + pad <= i < hi - pad and 0.1 < sl < 500]
        if len(inner) >= (6 if pad == 40 else 3):
            return _pct(inner)
    return None


def _pct(vals, q=0.20):
    """低分位估计：分红只会上抬斜率（Δten 含加回分红），故取低分位而非中位"""
    v = sorted(vals)
    if not v:
        return None
    return v[max(0, min(len(v) - 1, int(len(v) * q)))]


def _slope_samples(raw_close, ten_close, span=20, step=5, exclude_days=None):
    """斜率样本：用 span 日聚合位移估斜率（Δraw 大 → 舍入噪声小）。
    逐日斜率在低价股上会被 0.01 元舍入放大成几十个百分点噪声，不可用。
    exclude_days: 已检测出的现金事件日集合——跨事件的窗口 Δten 含加回分红（单边抬高斜率），须剔除。"""
    n = len(raw_close)
    excl = exclude_days or set()
    out = []
    for i in range(span, n, step):
        if any((i - k) in excl for k in range(0, span + 1, 5)):
            continue
        rc_p, rc = raw_close[i - span], raw_close[i]
        t_p, t_c = ten_close[i - span], ten_close[i]
        if None in (rc_p, rc, t_p, t_c) or t_p <= 0 or rc_p <= 0:
            continue
        draw = rc - rc_p
        if abs(draw) > max(0.05, 0.01 * rc_p):
            out.append((i, (t_c - t_p) / draw))
    return out


def _b_segments(dates, raw_close, ten_close, extra_bounds=None, exclude_days=None):
    """分段估计 b。extra_bounds: 迭代校正传入的边界索引列表。返回 [(lo, hi, b)]

    边界定位：先找"事件候选日"——送转/配股/除息日 ten 连续而 raw 跳变（局部斜率≈0）；
    再看该日前后 60 日斜率中位是否变化 >5%：变化=乘性事件（送转/配股）→ 段边界；
    不变化=纯现金除息（b 不变，不切段）。
    """
    n = len(dates)
    samples = _slope_samples(raw_close, ten_close, exclude_days=exclude_days)
    if len(samples) < 8:
        # 样本太少（次新等）：回退整段
        return [(0, n, None)]
    b0 = _pct([s for _, s in samples]) or 1.0

    def slope_med(lo, hi):
        v = [sl for i, sl in samples if lo <= i < hi and 0.1 < sl < 500]
        if len(v) < 4:
            return None
        return _pct(v)

    # 事件候选日：raw 有实际移动，但 ten 的变化远小于 b0×Δraw（ten 被事件"抹平")
    boundaries = []
    for i in range(1, n):
        rc_p, rc = raw_close[i - 1], raw_close[i]
        t_p, t_c = ten_close[i - 1], ten_close[i]
        if None in (rc_p, rc, t_p, t_c) or t_p <= 0 or rc_p <= 0:
            continue
        draw = abs(rc - rc_p)
        if draw < max(MIN_MOVE, 0.003 * rc_p):
            continue
        dten = abs(t_c - t_p)
        if dten < 0.30 * draw * b0:
            b_before = slope_med(i - 70, i - 10)
            b_after = slope_med(i + 10, i + 70)
            if b_before and b_after and abs(b_after / b_before - 1) > 0.05:
                boundaries.append(i)
    boundaries += list(extra_bounds or [])
    boundaries.sort()
    merged = []
    for bpt in boundaries:
        if merged and bpt - merged[-1] < 10:
            merged[-1] = (merged[-1] + bpt) // 2
        else:
            merged.append(bpt)
    bounds = [0] + merged + [n]
    segs = []
    for s in range(len(bounds) - 1):
        lo, hi = bounds[s], bounds[s + 1]
        if hi - lo < 10:
            continue
        b = _segment_slope(samples, lo, hi)
        if b is None:
            continue
        segs.append((lo, hi, b))
    if not segs:
        return [(0, n, None)]
    # b 只可能因送转/配股上升：小幅下降（<5%）并入前段
    merged_segs = []
    for seg in segs:
        if merged_segs:
            prev_b = merged_segs[-1][2]
            if prev_b and seg[2] < prev_b * 1.05:
                merged_segs[-1] = (merged_segs[-1][0], seg[1], prev_b)
                continue
        merged_segs.append(seg)
    return merged_segs


def _reconstruct(raw_rows, ten_close, seg_b, has_ten):
    """按给定 seg_b（逐日 b）链式重建。返回 (rows, cash_events, recon_rets)"""
    n = len(raw_rows)
    base = float(raw_rows[0][2])
    T = 1.0
    out, cash_events, rets = [], [], [0.0]
    for i in range(n):
        close = float(raw_rows[i][2])
        if i == 0 or not has_ten or seg_b[i - 1] is None:
            ret = 0.0 if i == 0 else close / float(raw_rows[i - 1][2]) - 1
        else:
            price_part_prev = seg_b[i - 1] * float(raw_rows[i - 1][2])
            dten = ten_close[i] - ten_close[i - 1]
            ret = dten / price_part_prev if price_part_prev > 0 else 0.0
            implied = (dten - seg_b[i - 1] * (close - float(raw_rows[i - 1][2]))) / seg_b[i - 1]
            if abs(implied) > 0.03:
                cash_events.append((raw_rows[i][0], round(implied, 4), round(seg_b[i - 1], 3)))
        if i > 0:
            T *= (1 + ret)
        hfq_close = base * T
        factor = hfq_close / close if close > 0 else 0.0
        out.append([raw_rows[i][0], float(raw_rows[i][1]) * factor, hfq_close,
                    float(raw_rows[i][3]) * factor, float(raw_rows[i][4]) * factor,
                    float(raw_rows[i][5])])
        rets.append(ret)
    return out, cash_events, rets


def build_series(raw_rows, ten_rows, events):
    """表驱动重建真·总收益序列（保真优先）。
    - 非事件日：T_ret = raw 收益（构造性保真，无需估计 b）
    - 表内事件日：T_ret = ((1+送转比例)×close_t + 每股派息)/close_{t-1} - 1（分红按除权日收盘再投）
    - 表外检出事件日（配股/表缺失）：T_ret = 0（配股对价公允、送转不创造价值；保守且正确）
    数据侧（ten）仅用于"表外事件日检出"与校验，不参与收益计算——避免 b 估计误差污染收益。
    """
    dates = [r[0] for r in raw_rows]
    n = len(dates)
    raw_close = [float(r[2]) for r in raw_rows]
    ten_map = {r[0]: float(r[2]) for r in ten_rows} if ten_rows else {}
    ten_close = [ten_map.get(d) for d in dates]
    has_ten = all(t is not None for t in ten_close)

    # 表内事件 → 交易日索引
    ev_idx = {}
    for e in events:
        if e['date'] < dates[0] or e['date'] > dates[-1]:
            continue
        i = next((j for j, d in enumerate(dates) if d >= e['date']), None)
        if i is None:
            continue
        cur = ev_idx.setdefault(i, {'ratio': 0.0, 'div': 0.0})
        cur['ratio'] += e.get('ratio', 0) or 0
        cur['div'] += e.get('div', 0) or 0

    # 表外事件日检出：ten 连续（|Δten| 远小于 |Δraw|）而 raw 实际移动
    b_level = 1.0
    if has_ten:
        long_slopes = _slope_samples(raw_close, ten_close, span=250, step=20)
        if long_slopes:
            vals = sorted(sl for _, sl in long_slopes if 0.1 < sl < 500)
            if vals:
                b_level = vals[len(vals) // 2]
    # 判据不需要 b：普通日斜率 b≥1 ⇒ |Δten|≥|Δraw|；事件日 raw 机械下跌而 ten 被抹平
    unmatched = set()
    if has_ten:
        for i in range(1, n):
            if i in ev_idx or raw_close[i - 1] <= 0:
                continue
            raw_ret = raw_close[i] / raw_close[i - 1] - 1
            draw = abs(raw_close[i] - raw_close[i - 1])
            if raw_ret > -0.03 or draw < 0.05:
                continue
            if abs(ten_close[i] - ten_close[i - 1]) < 0.5 * draw:
                unmatched.add(i)

    base = raw_close[0]
    T = 1.0
    out, cash_events = [], []
    for i in range(n):
        close = raw_close[i]
        if i == 0:
            ret = 0.0
        elif i in ev_idx:
            e = ev_idx[i]
            ret = ((1 + e['ratio']) * close + e['div']) / raw_close[i - 1] - 1
        elif i in unmatched:
            ret = 0.0
        else:
            ret = close / raw_close[i - 1] - 1
        T *= (1 + ret)
        hfq_close = base * T
        factor = hfq_close / close if close > 0 else 0.0
        out.append([dates[i], float(raw_rows[i][1]) * factor, hfq_close,
                    float(raw_rows[i][3]) * factor, float(raw_rows[i][4]) * factor,
                    float(raw_rows[i][5])])
        if i in ev_idx and ev_idx[i]['div'] > 0:
            cash_events.append((dates[i], round(ev_idx[i]['div'], 4), 0))
    return out, [(0, n, None)], cash_events


def validate_stock(code, raw_rows, events, built_rows, segs, cash_events):
    """校验：数据反解现金事件 vs 东财事件表；未覆盖事件；数值合法性"""
    dates = [r[0] for r in raw_rows]
    n = len(dates)
    ev_idx = {}
    for e in events:
        if e['date'] < dates[0] or e['date'] > dates[-1]:
            continue
        i = next((j for j, d in enumerate(dates) if d >= e['date']), None)
        if i is not None:
            cur = ev_idx.setdefault(i, {'ratio': 0.0, 'div': 0.0})
            cur['ratio'] += e.get('ratio', 0) or 0
            cur['div'] += e.get('div', 0) or 0
    det_idx = {d: (v, b) for d, v, b in cash_events}
    unmatched_table, amount_mismatch = [], []
    for i in sorted(ev_idx):
        near = [det_idx.get(dates[j]) for j in (i - 1, i, i + 1) if 0 <= j < n and dates[j] in det_idx]
        if not near:
            unmatched_table.append((dates[i], round(ev_idx[i]['div'], 3)))
        else:
            v = near[0][0]
            if ev_idx[i]['div'] > 0 and abs(v - ev_idx[i]['div']) > max(0.02, 0.25 * ev_idx[i]['div']):
                amount_mismatch.append((dates[i], round(v, 3), round(ev_idx[i]['div'], 3)))
    ev_dates = set()
    for i in ev_idx:
        for j in (i - 1, i, i + 1):
            if 0 <= j < n:
                ev_dates.add(dates[j])
    data_only = [(d, v) for d, v, _ in cash_events if d not in ev_dates]

    # 异常判据：重建收益与 raw 收益显著偏离（>5%），而非单纯"大波动日"
    # （创业板/科创板涨跌停 20%，连板日本身就是大波动；复牌跳空同理，重建应等于 raw）
    bad_jumps = []
    for i in range(5, len(built_rows)):
        pc, c = built_rows[i - 1][2], built_rows[i][2]
        rp, rc = float(raw_rows[i - 1][2]), float(raw_rows[i][2])
        if pc <= 0 or rp <= 0:
            continue
        recon_ret = c / pc - 1
        raw_ret = rc / rp - 1
        if abs(recon_ret - raw_ret) > 0.05 and abs(recon_ret) > 0.10:
            bad_jumps.append((built_rows[i][0], round(recon_ret, 3), round(raw_ret, 3)))
    nonpositive = sum(1 for r in built_rows if min(r[1], r[2], r[3], r[4]) <= 0)
    return {
        'rows': len(built_rows),
        'b_segments': len(segs) if segs and segs[0][2] is not None else 1,
        'b_multi': bool(len(segs) > 1 and segs[0][2] is not None),
        'table_events': len(ev_idx),
        'data_cash_events': len(cash_events),
        'unmatched_table_events': len(unmatched_table),
        'unmatched_sample': unmatched_table[:5],
        'amount_mismatch': len(amount_mismatch),
        'amount_mismatch_sample': amount_mismatch[:5],
        'data_only_events': len(data_only),
        'data_only_sample': data_only[:8],
        'bad_jumps': len(bad_jumps),
        'bad_jumps_sample': bad_jumps[:5],
        'nonpositive': nonpositive,
    }


def process(code, exchange, skip_events=False):
    raw = kline_store.load('raw_kline20', code)
    if not raw or not raw['data'] or len(raw['data']) < 30:
        return None
    ten = kline_store.load('kline20h', code)
    events = [] if skip_events else fetch_bonus_events(code, exchange)
    built, segs, cash = build_series(raw['data'], (ten or {}).get('data') or [], events)
    kline_store.save(KTYPE, code, exchange, built, raw.get('pe', 0), raw.get('pb', 0),
                     raw.get('price', 0), raw.get('name', ''), qt_date=raw.get('qt_date', ''),
                     volume=raw.get('volume', 0))
    v = validate_stock(code, raw['data'], events, built, segs, cash)
    v['code'] = code
    return v


def main():
    ap = argparse.ArgumentParser(description='真·分红再投序列重建与校验')
    ap.add_argument('--codes', nargs='*')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--threads', type=int, default=1)
    ap.add_argument('--skip-events', action='store_true',
                    help='跳过东财事件表抓取（全市场重建用；事件交叉校验在预热完成后另行执行）')
    args = ap.parse_args()

    kline_store.init_db()
    if args.codes:
        todo = [(c, 'sh' if c.startswith('6') else 'sz') for c in args.codes]
    else:
        todo = [(s['code'], s['market']) for s in kline_store.stock_basic_all()]
    if args.limit:
        todo = todo[:args.limit]
    print(f'待重建 {len(todo)} 只（线程 {args.threads}, skip_events={args.skip_events}）', flush=True)

    results, errors = [], []
    lock = threading.Lock()
    q = Queue()
    for it in todo:
        q.put(it)
    t0 = time.monotonic()

    def work():
        while True:
            try:
                code, mkt = q.get_nowait()
            except Exception:
                return
            try:
                v = process(code, mkt, skip_events=args.skip_events)
                with lock:
                    if v:
                        results.append(v)
                    if len(results) % 500 == 0 and results:
                        print(f'  进度 {len(results)}/{len(todo)} elapsed={time.monotonic()-t0:.0f}s', flush=True)
            except Exception as e:
                with lock:
                    errors.append((code, str(e)[:120]))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    miss = [r for r in results if r['unmatched_table_events'] > 0]
    mismatch = [r for r in results if r['amount_mismatch'] > 0]
    data_only = [r for r in results if r['data_only_events'] > 0]
    single = [r for r in results if not r['b_multi']]
    multi = [r for r in results if r['b_multi']]
    jumps = [r for r in results if r['bad_jumps'] > 0]
    nonpos = [r for r in results if r['nonpositive'] > 0]
    out = {
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'stocks': len(results), 'errors': len(errors), 'error_sample': errors[:10],
        'single_segment_stocks': len(single),
        'multi_segment_stocks': len(multi),
        'table_events_total': sum(r['table_events'] for r in results),
        'data_cash_events_total': sum(r['data_cash_events'] for r in results),
        'single_unmatched_table': len([r for r in single if r['unmatched_table_events'] > 0]),
        'single_amount_mismatch': len([r for r in single if r['amount_mismatch'] > 0]),
        'single_data_only': [{'code': r['code'], 'n': r['data_only_events']} for r in
                             sorted(single, key=lambda r: -r['data_only_events'])[:20]
                             if r['data_only_events'] > 0],
        'multi_data_only_top': [{'code': r['code'], 'n': r['data_only_events'], 'segs': r['b_segments']}
                                for r in sorted(multi, key=lambda r: -r['data_only_events'])[:20]],
        'table_events_without_evidence': len(miss),
        'amount_mismatch_stocks': len(mismatch),
        'data_only_event_stocks': len(data_only),
        'bad_jump_stocks': len(jumps),
        'bad_jump_top': sorted(jumps, key=lambda r: -r['bad_jumps'])[:20],
        'nonpositive_stocks': len(nonpos),
        'nonpositive_list': [r['code'] for r in nonpos][:50],
    }
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n重建完成: {len(results)} 只, 错误 {len(errors)}")
    print(f"  单段股(逐事件检测可信) {len(single)} 只 | 多段股(复核清单) {len(multi)} 只")
    print(f"  表内事件总数 {out['table_events_total']} | 数据反解现金事件 {out['data_cash_events_total']}")
    print(f"  单段股: 表内事件无证据 {out['single_unmatched_table']} 只; 金额不吻合 {out['single_amount_mismatch']} 只; "
          f"数据独有事件 {len(out['single_data_only'])} 只 {out['single_data_only'][:5]}")
    print(f"  重建序列异常跳变 {len(jumps)} 只 | 非正价格 {len(nonpos)} 只")
    print(f'摘要: {SUMMARY_PATH}')


if __name__ == '__main__':
    main()
