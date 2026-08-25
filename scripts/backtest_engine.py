# -*- coding: utf-8 -*-
"""
Point-in-Time 回测评分引擎（消除未来函数）

核心思想（审计 2026-08-12 P0 正确性）：
- 年度切片：每年 5 月 1 日重算一次参数（与 A 股年报披露截止 4/30 对齐），年内恒定
- 每个切片只用"截至切片起点已披露"的数据：
    * PE/PB 区间：截至当日的不复权价 / 已生效 EPS/BPS 的 10th/90th（expanding window）
    * 基本面因子：ROE/毛利率稳定性/营收增速/净利润CAGR，只用已披露年报
- 逐日评分复用 scoring_engine.compute_daily_scores，与修复后的报告逻辑同源；
  历史 PE/PB 用不复权真实交易价（修复前复权价缩放失真）；
  无历史 EPS 时禁用"当前 EPS 反推"（no_pe_fallback），计中性分。

复用示例：
    from backtest_engine import compute_pit_scores
    daily = compute_pit_scores(qfq_kline, raw_kline, reports, pershare, weights, total_shares)
"""
import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from scoring_engine import (
    MODEL_PRESETS, resolve_active_weights, compute_daily_scores,
    _series_effective,
)

# 财务披露假设：T 年年报次年 5 月 1 日生效（A 股年报披露截止 4/30）
DISCLOSURE_MONTH = 5
# 区间样本下限（与 compute_valuation_range 一致，样本不足时不出分数）
MIN_RANGE_SAMPLES = 50
# MA/量能滚动窗口重叠天数（切片边界处保持完整历史窗口）
MA_WARMUP_DAYS = 60


def _annuals_before(reports, eff_year):
    """截至 eff_year 已披露的年报（从新到旧），reports 为 fetch_financial_reports 返回值"""
    annuals = []
    for r in reports:
        if r.get('report_type') != 'annual':
            continue
        y = int((r.get('report_date') or '')[:4] or 0)
        if y > eff_year:
            continue
        annuals.append(r)
    return annuals


def _cagr_growth(annuals, max_lookback=5):
    """净利润 CAGR（与 scan_watchlist 同口径：oldest_idx = min(len-1, 4)）"""
    if len(annuals) < 2:
        return 0.0
    latest_p = annuals[0]['net_profit']
    oldest_idx = min(len(annuals) - 1, max_lookback - 1)
    oldest_p = annuals[oldest_idx]['net_profit']
    if latest_p > 0 and oldest_p > 0 and oldest_idx > 0:
        return round((latest_p / oldest_p) ** (1.0 / oldest_idx) - 1, 4)
    return 0.0


def _pit_valuation_range(seg_end_date, raw_kline, eps_series, bps_series):
    """
    计算截至 seg_end_date 的 PE/PB 10th/90th 百分位区间（expanding window）。

    只用当日不复权收盘价 / 当日已生效 EPS/BPS，不偷看未来。
    Returns:
        dict {'pe_min','pe_max','pb_min','pb_max','pe_samples','pb_samples'} 或 None（样本不足）
    """
    pe_vals, pb_vals = [], []
    for row in raw_kline:
        d = row[0]
        if d > seg_end_date:
            break  # raw_kline 按日期升序
        close = float(row[2])
        if close <= 0:
            continue
        eps = _series_effective(eps_series, d)
        bps = _series_effective(bps_series, d)
        if eps and eps > 0:
            pe = close / eps
            if 0 < pe < 500:
                pe_vals.append(pe)
        if bps and bps > 0:
            pb = close / bps
            if 0 < pb < 50:
                pb_vals.append(pb)

    result = {'pe_samples': len(pe_vals), 'pb_samples': len(pb_vals)}
    if len(pe_vals) >= MIN_RANGE_SAMPLES:
        pe_vals.sort()
        result['pe_min'] = round(pe_vals[int(len(pe_vals) * 0.1)], 1)
        result['pe_max'] = round(pe_vals[int(len(pe_vals) * 0.9)], 1)
        if result['pe_min'] >= result['pe_max']:
            result['pe_min'], result['pe_max'] = result['pe_max'], result['pe_min']
    else:
        result['pe_min'] = result['pe_max'] = None
    if len(pb_vals) >= MIN_RANGE_SAMPLES:
        pb_vals.sort()
        result['pb_min'] = round(pb_vals[int(len(pb_vals) * 0.1)], 1)
        result['pb_max'] = round(pb_vals[int(len(pb_vals) * 0.9)], 1)
        if result['pb_min'] >= result['pb_max']:
            result['pb_min'], result['pb_max'] = result['pb_max'], result['pb_min']
    else:
        result['pb_min'] = result['pb_max'] = None

    if result['pe_min'] is None and result['pb_min'] is None:
        return None  # 样本不足：该切片不出分数
    return result


def _segment_map(dates):
    """
    按 5 月 1 日划分年度切片。

    Returns:
        list of (start_date, end_date)：闭区间，K 线首尾自动截断。
    """
    segments = []
    if not dates:
        return segments
    cur_start = dates[0]
    cur_key = None
    prev_date = dates[0]
    for d in dates:
        y = int(d[:4])
        m = int(d[5:7])
        key = y if m >= DISCLOSURE_MONTH else y - 1  # 5月1日起进入新切片
        if cur_key is not None and key != cur_key:
            segments.append((cur_start, prev_date))
            cur_start = d
        cur_key = key
        prev_date = d
    segments.append((cur_start, dates[-1]))
    return segments


def _factor_snapshot(reports, eff_year):
    """截至 eff_year 已披露年报 → 基本面因子快照 + eps_growth"""
    annuals = _annuals_before(reports, eff_year)
    if not annuals:
        return {}, 0.0
    # 只把 eff_year 及之前的报告喂给 metrics（compute_financial_metrics 自行筛选年报）
    reports_before = [r for r in reports if int((r.get('report_date') or '')[:4] or 0) <= eff_year]
    from financial_fetcher import compute_financial_metrics
    metrics = compute_financial_metrics(reports_before) or {}
    fv = {}
    if metrics.get('avg_roe'):
        fv['roe'] = metrics['avg_roe']
    if metrics.get('gross_margin_stability') is not None:
        fv['margin_stability'] = metrics['gross_margin_stability']
    if metrics.get('latest_revenue_yoy') is not None:
        fv['revenue_growth'] = metrics['latest_revenue_yoy']
    growth = _cagr_growth(annuals)
    return fv, growth


def compute_pit_scores(qfq_kline, raw_kline, reports, pershare_data,
                       model_weights, total_shares, dps=None):
    """
    计算一只股票的全历史 Point-in-Time 分数序列。

    Args:
        qfq_kline: 前复权 [[date, open, close, high, low, volume], ...] 升序（收益/MA/量能用）
        raw_kline: 不复权 [[date, open, close, high, low, volume], ...] 升序（历史PE/PB用）
        reports: fetch_financial_reports() 返回值（从新到旧）
        pershare_data: fetch_pershare_data() 返回值 [{'year','eps','bps'}, ...]
        model_weights: MODEL_PRESETS[model]['weights']
        total_shares: 总股本(亿股)
        dps: 每股年分红(元)，None 时股息率因子缺失（权重再分配）

    Returns:
        list of dict: {date, close, pe_ttm, pb, score, pe_min, pe_max, pb_min, pb_max,
                       eff_eps_year, s_<factor>} 仅含区间样本充足且有分的日期
    """
    if not qfq_kline or not raw_kline:
        return []

    dates = [r[0] for r in qfq_kline]
    raw_close = {r[0]: float(r[2]) for r in raw_kline if len(r) >= 3}
    eps_series = {d['year']: d['eps'] for d in pershare_data} or None
    bps_series = {d['year']: d['bps'] for d in pershare_data} or None

    # 全部 K 线转 dict（评分引擎格式），重叠窗口保证切片边界 MA 完整
    kline_all = []
    for r in qfq_kline:
        kline_all.append({
            'date': r[0], 'open': float(r[1]), 'close': float(r[2]),
            'high': float(r[3]), 'low': float(r[4]), 'volume': float(r[5]),
        })
    date_index = {d: i for i, d in enumerate(dates)}

    out = []
    for seg_start, seg_end in _segment_map(dates):
        # 切片参数快照（只用截至切片起点已披露的数据）：
        # 起点为 5 月 1 日 → 上年年报生效；起点在 5 月前（仅首切片）→ 前两年年报生效
        seg_y = int(seg_start[:4])
        seg_m = int(seg_start[5:7])
        eff_year = seg_y - (1 if seg_m >= DISCLOSURE_MONTH else 2)

        # 区间用截至切片起点的数据重算（expanding window，不偷看切片内未来）
        val_range = _pit_valuation_range(seg_start, raw_kline, eps_series, bps_series)
        if val_range is None:
            continue  # 区间样本不足（warmup 期），不出分数

        factor_values, growth = _factor_snapshot(reports, eff_year)
        if dps and dps > 0:
            factor_values['dividend_yield'] = dps  # 仅供缺失判断，实际用 dps/close 动态计算

        active_weights = resolve_active_weights(model_weights, factor_values)

        # 切片窗口（含 60 天重叠），评分后只保留切片内日期
        start_idx = max(0, date_index[seg_start] - MA_WARMUP_DAYS)
        end_idx = date_index[seg_end]
        seg_kline = kline_all[start_idx:end_idx + 1]

        params = {
            'pe_min': val_range['pe_min'] or 0, 'pe_max': val_range['pe_max'] or 0,
            'pb_min': val_range['pb_min'] or 0, 'pb_max': val_range['pb_max'] or 0,
            'eps_growth': growth,
            'latest_price': kline_all[-1]['close'], 'latest_pe': 0, 'latest_pb': 0,
            'total_shares': total_shares,
            'eps_series': eps_series, 'bps_series': bps_series,
            'pe_close_series': raw_close,
            'no_pe_fallback': True,
            'dps': dps,
        }
        results = compute_daily_scores(seg_kline, active_weights, factor_values, params)

        for r in results:
            if r['date'] < seg_start:
                continue
            entry = {
                'date': r['date'], 'close': r['close'],
                'pe_ttm': r['pe_ttm'], 'pb': r['pb'],
                'score': r['score'],
                'pe_min': val_range['pe_min'], 'pe_max': val_range['pe_max'],
                'pb_min': val_range['pb_min'], 'pb_max': val_range['pb_max'],
                'eff_eps_year': eff_year,
            }
            for fk in ('pe', 'pb', 'peg', 'ma', 'vol', 'vola', 'dividend_yield',
                       'roe', 'margin_stability', 'revenue_growth'):
                if f's_{fk}' in r:
                    entry[f's_{fk}'] = r[f's_{fk}']
            out.append(entry)
    return out
