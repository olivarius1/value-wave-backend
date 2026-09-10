#!/usr/bin/env python3
"""
市场牛熊分段（上证指数月收盘回撤规则）

用途边界（2026-09 与用户确认）：
- 分段只用于"信号诊断"——每段单独计算 IC/分层（逐日观测，不涉及入场时点），
  以及给 rolling-entry 任意起点检验打环境标签；
- 不得把"段首入场的段收益"当作真实投资体验（真实入场时点任意）。

分段规则（月度收盘，规避日内噪声）：
- 熊市：自滚动高点回撤 >= BEAR_DD（20%）
- 牛市：自低点恢复 >= RECOVER_GAIN（20%）
- 首段从指数数据起点计，末段开放至数据末日；短于 MIN_MONTHS 的段并入前段

指数取 sh000001（上证指数），独立缓存 index_sh000001.json——
不用 get_kline('000001') 以避免与 sz000001（平安银行）的股票缓存键冲突。

CLI: python3 scripts/market_regimes.py   # 打印段表
"""
import bisect
import datetime
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_CACHE_PATH = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'index_sh000001.json')

BEAR_DD = 0.20        # 自滚动高点回撤阈值 → 熊市
RECOVER_GAIN = 0.20   # 自低点恢复阈值 → 牛市
MIN_MONTHS = 3        # 段最短月数（过短并入前段）
INDEX_YEARS = 25      # 指数回溯年数（覆盖 2001 至今，未来自动延伸）
_KIND_CN = {'bull': '牛市', 'bear': '熊市'}


def _load_index(no_cache=False):
    """上证指数日线 [[date, close], ...] 升序，独立缓存。
    走 fetch_full 分块（API 单次上限 500 条，长区间只回最近 500 天）"""
    from kline_cache import fetch_full, load_cache, save_cache
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if not no_cache:
        cache = load_cache('000001', suffix='_index_kline')
        if cache and cache.get('data'):
            if cache.get('updated') == today_str:
                return [[r[0], float(r[2])] for r in cache['data']]
            # 指数无除权，直接增量追加
            from kline_cache import fetch_incremental
            new_rows, _, _ = fetch_incremental('000001', 'sh', cache['last_date'], fq='')
            if new_rows:
                cache['data'].extend(new_rows)
                save_cache('000001', 'sh', cache['data'], 0, 0, 0, '上证指数',
                           suffix='_index_kline')
            return [[r[0], float(r[2])] for r in cache['data']]
    kline_data, _ = fetch_full('000001', 'sh', years=INDEX_YEARS, fq='')
    if kline_data:
        save_cache('000001', 'sh', kline_data, 0, 0, 0, '上证指数', suffix='_index_kline')
    return [[r[0], float(r[2])] for r in kline_data]


def compute_regimes(index_kline=None, bear_dd=BEAR_DD, recover_gain=RECOVER_GAIN,
                    min_months=MIN_MONTHS):
    """
    月度收盘状态机划段。

    Returns:
        list of {'label', 'kind', 'start', 'end'}（start/end 为月度边界日，升序，
        末段 end = 数据末日）
    """
    if index_kline is None:
        index_kline = _load_index()
    if not index_kline:
        return []
    # 月度收盘（同月取最后一条）
    monthly = {}
    for d, c in index_kline:
        monthly[d[:7]] = c
    months = sorted(monthly)
    closes = [monthly[m] for m in months]

    segments = []          # (start_idx, end_idx, kind)
    state, seg_start = 'bull', 0
    peak, trough = closes[0], closes[0]
    peak_i, trough_i = 0, 0
    for i, c in enumerate(closes):
        if state == 'bull':
            if c > peak:
                peak, peak_i = c, i
            elif c <= peak * (1 - bear_dd):
                segments.append((seg_start, peak_i, 'bull'))
                state, seg_start = 'bear', peak_i + 1
                trough, trough_i = c, i
        else:
            if c < trough:
                trough, trough_i = c, i
            elif c >= trough * (1 + recover_gain):
                segments.append((seg_start, trough_i, 'bear'))
                state, seg_start = 'bull', trough_i + 1
                peak, peak_i = c, i
    segments.append((seg_start, len(closes) - 1, state))

    # 短段并入前段（末段开放不并；首段过短并入后段）
    merged = []
    for seg_i, seg in enumerate(segments):
        start_i, end_i, kind = seg
        if (end_i - start_i + 1) < min_months and seg is not segments[-1]:
            if merged:
                merged[-1][1] = end_i
            else:
                continue  # 首段过短：丢弃，后段起点自然提前
            continue
        merged.append(list(seg))

    out = []
    for start_i, end_i, kind in merged:
        s, e = months[start_i], months[end_i]
        y0, y1 = s[:4], e[:4]
        year_part = y0 if y0 == y1 else f'{y0}~{y1}'
        out.append({'label': f'{year_part}{_KIND_CN[kind]}', 'kind': kind,
                    'start': f'{s}-01', 'end': f'{e}-31', 'start_month': s, 'end_month': e})
    return out


def assign_regimes(dates, regimes=None):
    """日期列表 → {date: label}；早于首段的日期标 'pre_index'（不应出现，兜底）"""
    if regimes is None:
        regimes = compute_regimes()
    starts = [r['start'] for r in regimes]
    out = {}
    for d in dates:
        i = bisect.bisect_right(starts, d) - 1
        out[d] = regimes[i]['label'] if i >= 0 else 'pre_index'
    return out


def main():
    regimes = compute_regimes()
    print(f'上证指数分段（规则: 回撤{BEAR_DD:.0%}入熊 / 恢复{RECOVER_GAIN:.0%}入牛, 最短{MIN_MONTHS}个月）')
    for r in regimes:
        print(f"  {r['start'][:7]} ~ {r['end'][:7]}  {r['label']}")


if __name__ == '__main__':
    main()
