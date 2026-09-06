#!/usr/bin/env python3
"""
watchlist 快速估值扫描
- 读取 watchlist.txt（CSV：名称,代码,模型,最后报告时间）中的股票
- 获取最新K线（增量缓存）
- 计算当前分数 + 历史PE/PB区间
- 输出是否低估/值得建仓的判断

因子评分与权重全部复用 scoring_engine（与报告/回测同源，无重复实现）
"""
import json
import os
import sys
import datetime
import io

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)
# 注意：stdout 编码包装不能在模块顶层做——本模块会被 batch_rebuild import，
# 二次包装会让旧 TextIOWrapper 被 GC 时关闭底层流

from kline_cache import get_kline, CACHE_DIR
from financial_fetcher import (
    fetch_pershare_data, compute_valuation_range,
    fetch_financial_reports, compute_financial_metrics, auto_fill_factors
)
from scoring_engine import (
    MODEL_PRESETS, resolve_active_weights,
    score_pe, score_pb, score_peg, score_ma_deviation,
    score_volume, score_volatility, ret_std20,
    OPTIONAL_SCORE_FUNCS,
)


def compute_latest_score(kline_data, pe, pb, price, pe_min, pe_max, pb_min, pb_max,
                         eps_growth, model_type, optional_factors=None):
    """计算最新一天的分数（因子函数与权重解析复用 scoring_engine）"""
    if not kline_data or len(kline_data) < 20:
        return None

    preset = MODEL_PRESETS.get(model_type, MODEL_PRESETS['cyclical'])
    active_weights = resolve_active_weights(preset['weights'], optional_factors or {})

    # 取最后一天数据
    last = kline_data[-1]
    close = float(last[2])
    volume = float(last[5])

    # MA20 / MA60 / VOL_MA20
    n = len(kline_data)
    ma20 = sum(float(kline_data[j][2]) for j in range(max(0, n-20), n)) / min(20, n)
    ma60 = sum(float(kline_data[j][2]) for j in range(max(0, n-60), n)) / min(60, n)
    vol_ma20 = sum(float(kline_data[j][5]) for j in range(max(0, n-20), n)) / min(20, n)

    cur_pe = pe if pe > 0 else 0
    cur_pb = pb if pb > 0 else 0

    # 逐因子评分
    total_score = 0
    for fk, w in active_weights.items():
        if w < 0.001:
            continue
        if fk == 'pe':
            s = score_pe(cur_pe, pe_min, pe_max)
        elif fk == 'pb':
            s = score_pb(cur_pb, pb_min, pb_max)
        elif fk == 'peg':
            s = score_peg(cur_pe, eps_growth)
        elif fk == 'ma':
            s = score_ma_deviation(close, ma20, ma60)
        elif fk == 'vol':
            s = score_volume(volume, vol_ma20)
        elif fk == 'vola':
            s = score_volatility(ret_std20([float(kline_data[j][2]) for j in range(max(0, n - 21), n)]))
        elif fk in OPTIONAL_SCORE_FUNCS and optional_factors and optional_factors.get(fk) is not None:
            s = OPTIONAL_SCORE_FUNCS[fk](optional_factors[fk])
        else:
            s = 50
        total_score += s * w

    return round(total_score, 1)


def get_status(score):
    """分数 → 状态标签（与 README 评分体系一致）"""
    if score >= 80: return '极度低估'
    elif score >= 70: return '低估'
    elif score >= 40: return '无交易价值'
    elif score >= 20: return '高估'
    else: return '极度高估'


def get_signal(score):
    """交易信号"""
    if score >= 80: return '★★★ 强烈建仓'
    elif score >= 70: return '★★ 分批建仓'
    elif score >= 60: return '★ 轻仓试探'
    elif score >= 40: return '— 观望'
    elif score >= 20: return '▼ 减仓'
    else: return '▼▼ 清仓'


def scan_stock(name, code, model):
    """扫描单只股票，返回结果dict"""
    exchange = 'sh' if code.startswith('6') else 'sz'
    result = {'name': name, 'code': code, 'model': model, 'score': None, 'error': None}

    try:
        # 获取K线（自动增量）
        kline_result = get_kline(code, exchange)
        kline_data = kline_result['kline']
        pe = kline_result['pe']
        pb = kline_result['pb']
        price = kline_result['price']

        if not kline_data or len(kline_data) < 20:
            result['error'] = 'K线数据不足'
            return result

        result['price'] = price
        result['pe'] = pe
        result['pb'] = pb

        # 计算PE/PB历史百分位区间
        pershare = fetch_pershare_data(code, exchange)
        val_range = compute_valuation_range(kline_data, pershare, pe, pb)
        pe_min, pe_max = val_range['pe_min'], val_range['pe_max']
        pb_min, pb_max = val_range['pb_min'], val_range['pb_max']
        result['pe_range'] = f"{pe_min}~{pe_max}"
        result['pb_range'] = f"{pb_min}~{pb_max}"

        # 获取增速
        reports = fetch_financial_reports(code, exchange, max_reports=10)
        eps_growth = 0.08
        opt_factors = {}
        if reports:
            metrics = compute_financial_metrics(reports)
            opt_factors = auto_fill_factors(opt_factors, metrics, model)
            # 从年报计算增速
            annuals = [r for r in reports if r['report_type'] == 'annual']
            if len(annuals) >= 2:
                latest_p = annuals[0]['net_profit']
                oldest_idx = min(len(annuals) - 1, 4)
                oldest_p = annuals[oldest_idx]['net_profit']
                if latest_p > 0 and oldest_p > 0 and oldest_idx > 0:
                    eps_growth = round((latest_p / oldest_p) ** (1.0 / oldest_idx) - 1, 4)
        result['growth'] = eps_growth

        # 计算最新分数
        score = compute_latest_score(
            kline_data, pe, pb, price,
            pe_min, pe_max, pb_min, pb_max,
            eps_growth, model, opt_factors
        )
        result['score'] = score
        result['status'] = get_status(score)
        result['signal'] = get_signal(score)

    except Exception as e:
        result['error'] = str(e)[:60]

    return result


def load_watchlist(path):
    """读取 watchlist：CSV 格式（名称,代码,模型,最后报告时间）"""
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('名称'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3 and parts[1].isdigit():
                rows.append((parts[0], parts[1], parts[2]))
    return rows


def main():
    # 读取watchlist
    watchlist_path = os.path.join(_SKILL_DIR, 'watchlist.txt')
    if not os.path.exists(watchlist_path):
        print("错误: watchlist.txt 不存在")
        sys.exit(1)

    rows = load_watchlist(watchlist_path)
    if not rows:
        print("错误: watchlist.txt 为空或格式无法解析")
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  Watchlist 估值扫描  |  {datetime.date.today()}  |  共{len(rows)}只")
    print(f"{'='*70}")

    results = []
    for name, code, model in rows:
        r = scan_stock(name, code, model)
        results.append(r)
        if r['score'] is not None:
            print(f" 分数={r['score']} {r['status']}")
        else:
            print(f" 失败: {r.get('error', '?')}")

    # 输出汇总表
    print(f"\n{'='*70}")
    print(f"{'股票':<8}{'代码':<8}{'价格':>7}{'PE':>7}{'PB':>6}{'分数':>6}{'状态':<8}{'信号':<12}{'模型'}")
    print(f"{'-'*70}")

    # 按分数降序排列（低估在前）
    scored = [r for r in results if r['score'] is not None]
    scored.sort(key=lambda x: -x['score'])

    for r in scored:
        price_s = f"{r.get('price', 0):.2f}" if r.get('price') else '-'
        pe_s = f"{r.get('pe', 0):.1f}" if r.get('pe') else '-'
        pb_s = f"{r.get('pb', 0):.2f}" if r.get('pb') else '-'
        print(f"{r['name']:<8}{r['code']:<8}{price_s:>7}{pe_s:>7}{pb_s:>6}"
              f"{r['score']:>6.1f}  {r['status']:<8}{r['signal']:<12}{r['model']}")

    # 失败列表
    failed = [r for r in results if r['score'] is None]
    if failed:
        print(f"\n  失败({len(failed)}只): " + ', '.join(f"{r['name']}({r.get('error','')})" for r in failed))

    # 建仓提示
    buy_candidates = [r for r in scored if r['score'] >= 70]
    print(f"\n{'─'*70}")
    if buy_candidates:
        print(f"  ◆ 低估建仓候选 ({len(buy_candidates)}只):")
        for r in buy_candidates:
            pe_r = r.get('pe_range', '?')
            pb_r = r.get('pb_range', '?')
            print(f"    {r['name']}({r['code']}) 分数{r['score']} | PE区间{pe_r} | PB区间{pb_r} | {r['signal']}")
    else:
        print(f"  ◆ 当前无低估建仓候选（所有股票分数 < 70）")

    watch_candidates = [r for r in scored if 60 <= r['score'] < 70]
    if watch_candidates:
        print(f"\n  ◆ 接近低估关注 ({len(watch_candidates)}只):")
        for r in watch_candidates:
            print(f"    {r['name']}({r['code']}) 分数{r['score']} | {r['status']}")

    print(f"{'='*70}")


if __name__ == '__main__':
    if not sys.stdout.isatty():
        # 管道/重定向场景统一 UTF-8（配合 PowerShell Console 编码）；控制台直出走 console API 不需要
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
