#!/usr/bin/env python3
"""
估值评分回测与有效性验证（审计 2026-08-12 P0：分数 vs 未来收益的唯一验证途径）

功能：
1. Point-in-Time 评分（backtest_engine，无未来函数：滚动区间 + 财务滞后）
2. IC 检验：分数 vs 未来 60/250 日收益（Spearman 秩相关，周频采样）
3. 分层回测：<40 / 40-70 / >=70 与五等分桶的未来收益
4. 策略模拟：>=70 持仓 / <40 空仓 / 40-70 保持前态（信号滞后1日），等权组合 vs 买入持有

用法：
    python run_backtest.py                       # 全量 44 只 watchlist
    python run_backtest.py --stocks 600887       # 指定股票（逗号分隔）
    python run_backtest.py --refresh-data        # 重新抓取数据（默认只用缓存）
    python run_backtest.py --start 2020-01-01    # 截断回测起点

输出（local_reports/backtest/{run_id}/）：
    meta.json / daily_scores.csv / metrics.json / curves.json / backtest_report.html
    local_reports/backtest/backtest_latest.html （固定入口，指向最新一次）
"""
import argparse
import csv
import datetime
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from kline_cache import get_kline, get_kline_raw
from financial_fetcher import fetch_pershare_data, fetch_financial_reports, fetch_stock_info
from scoring_engine import MODEL_PRESETS
from backtest_engine import compute_pit_scores, DISCLOSURE_MONTH

_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
WATCHLIST_PATH = os.path.join(_PROJECT_ROOT, 'watchlist.txt')
OUTPUT_ROOT = os.path.join(_PROJECT_ROOT, 'local_reports', 'backtest')

# ===== 验证参数 =====
FUTURE_HORIZONS = (60, 250)   # 未来收益窗口（交易日）
WEEKLY_SAMPLE = 5             # 周频采样间隔（减少重叠样本）
SCORE_BUY = 70                # 策略买入阈值（绝对分数）
SCORE_SELL = 40               # 策略卖出阈值（绝对分数）
NO_TRADE_COST = True          # 简化假设：无交易成本
# 百分位策略参数（个股历史分数分布，PIT 重算）
PCT_BUY = 0.80                # 分数处于自身历史 80th 以上买入
PCT_SELL = 0.20               # 分数处于自身历史 20th 以下卖出
PCT_MIN_SAMPLES = 50          # 阈值样本下限（与区间 MIN_RANGE_SAMPLES 一致）


def load_watchlist(path):
    """解析 watchlist.txt：名称,代码,模型,..."""
    stocks = []
    with open(path, 'r', encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('名称') or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3 and parts[1].isdigit():
                stocks.append({'name': parts[0], 'code': parts[1], 'model': parts[2]})
    return stocks


def qfq_dicts(kline_rows):
    """[[date,open,close,high,low,vol],...] → [{'date','close'},...]"""
    return [{'date': r[0], 'close': float(r[2])} for r in kline_rows]


def future_returns(daily_close, horizons):
    """
    为每只股票的每日样本附加未来 N 日收益。
    daily_close: [{'date','close'}] 升序
    Returns: {date: {'fut_60': x, 'fut_250': y}}（末端不足 None）
    """
    n = len(daily_close)
    out = {}
    for i, d in enumerate(daily_close):
        entry = {}
        for h in horizons:
            j = i + h
            if j < n and daily_close[j]['close'] > 0 and d['close'] > 0:
                entry[f'fut_{h}'] = daily_close[j]['close'] / d['close'] - 1
            else:
                entry[f'fut_{h}'] = None
        out[d['date']] = entry
    return out


# ===== 统计工具（无第三方依赖）=====

def _rank(vals):
    """平均秩（处理并列）"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    vx = sum((v - mx) ** 2 for v in x) ** 0.5
    vy = sum((v - my) ** 2 for v in y) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def spearman(x, y):
    """Spearman 秩相关；样本不足返回 None"""
    if len(x) < 5:
        return None
    return _pearson(_rank(x), _rank(y))


def weekly_sample(samples):
    """周频采样（每 WEEKLY_SAMPLE 个样本取1个），减少重叠样本对 IC 的扭曲"""
    return samples[::WEEKLY_SAMPLE]


# ===== IC / 分层 =====

def compute_ic(stock_daily, fut_map, horizons):
    """池化 IC + 按股 IC"""
    ic = {}
    for h in horizons:
        pooled_s, pooled_r = [], []
        by_stock = {}
        for code, daily in stock_daily.items():
            xs, ys = [], []
            for d in weekly_sample(daily):
                fut = fut_map[code].get(d['date'], {}).get(f'fut_{h}')
                if d.get('score') is not None and fut is not None:
                    xs.append(d['score'])
                    ys.append(fut)
                    pooled_s.append(d['score'])
                    pooled_r.append(fut)
            ic_s = spearman(xs, ys)
            if ic_s is not None:
                by_stock[code] = ic_s
        ic[f'fut_{h}'] = {
            'pooled': spearman(pooled_s, pooled_r),
            'by_stock_mean': round(sum(by_stock.values()) / len(by_stock), 4) if by_stock else None,
            'by_stock_median': round(sorted(by_stock.values())[len(by_stock) // 2], 4) if by_stock else None,
            'positive_ratio': round(sum(1 for v in by_stock.values() if v > 0) / len(by_stock), 4) if by_stock else None,
            'stock_count': len(by_stock),
            'pooled_n': len(pooled_s),
        }
    return ic


def compute_layers(stock_daily, fut_map, horizons):
    """分层回测：固定阈值桶 + 五等分桶"""
    buckets_fixed = {'<40': (0, 40), '40-70': (40, 70), '>=70': (70, 101)}
    layers = {'fixed': [], 'quintile': []}
    for h in horizons:
        # 固定阈值
        rows = {k: [] for k in buckets_fixed}
        for code, daily in stock_daily.items():
            for d in weekly_sample(daily):
                fut = fut_map[code].get(d['date'], {}).get(f'fut_{h}')
                if d.get('score') is None or fut is None:
                    continue
                for bname, (lo, hi) in buckets_fixed.items():
                    if lo <= d['score'] < hi:
                        rows[bname].append(fut)
                        break
        for bname, (lo, hi) in buckets_fixed.items():
            vals = rows[bname]
            layers['fixed'].append({
                'bucket': bname, 'n': len(vals),
                'mean_ret': round(sum(vals) / len(vals), 4) if vals else None,
                'winrate': round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None,
            })
        # 五等分
        quint = [[] for _ in range(5)]
        for code, daily in stock_daily.items():
            for d in weekly_sample(daily):
                fut = fut_map[code].get(d['date'], {}).get(f'fut_{h}')
                if d.get('score') is None or fut is None:
                    continue
                idx = min(4, int(d['score'] // 20))
                quint[idx].append(fut)
        for idx, vals in enumerate(quint):
            layers['quintile'].append({
                'bucket': f'{idx * 20}-{idx * 20 + 20}', 'n': len(vals),
                'mean_ret': round(sum(vals) / len(vals), 4) if vals else None,
                'winrate': round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None,
            })
    return layers


def _seg_key(date_str):
    """日期 → 年度切片键（5 月 1 日分界，与财务生效日对齐）"""
    y = int(date_str[:4])
    m = int(date_str[5:7])
    return y if m >= DISCLOSURE_MONTH else y - 1


def compute_percentile_thresholds(stock_daily, lo=PCT_SELL, hi=PCT_BUY, min_samples=PCT_MIN_SAMPLES):
    """
    个股历史分数百分位阈值（Point-in-Time）。

    对每只股票：按年度切片（5/1）逐片重算，只用截至切片起点的全部历史分数
    （expanding window，不偷看未来），取 hi/lo 分位作为买卖阈值。

    Returns:
        {code: {date: (p80, p20)}}，样本不足的切片无阈值（该期间不产生信号）
    """
    thr = {}
    for code, daily in stock_daily.items():
        by_seg = {}
        for d in daily:
            by_seg.setdefault(_seg_key(d['date']), []).append(d['score'])
        seg_thr = {}
        for key in sorted(by_seg):
            # expanding：累计截至本切片起点的所有历史分数
            scores = [s for k in sorted(by_seg) if k <= key for s in by_seg[k]]
            if len(scores) < min_samples:
                continue
            ss = sorted(scores)
            idx_lo = int(len(ss) * lo)
            idx_hi = min(int(len(ss) * hi) - 1, len(ss) - 1)
            seg_thr[key] = (ss[idx_hi], ss[idx_lo])  # (p80, p20)
        if seg_thr:
            thr[code] = {d['date']: seg_thr[_seg_key(d['date'])]
                         for d in daily if _seg_key(d['date']) in seg_thr}
    return thr


def _signal_fn(mode, score_by, pct_thr):
    """返回信号函数 fn(code, date) -> 'buy' / 'sell' / None（None=保持前态）"""
    if mode == 'abs':
        def abs_fn(code, date):
            s = score_by[code].get(date)
            if s is None:
                return None
            if s >= SCORE_BUY:
                return 'buy'
            if s < SCORE_SELL:
                return 'sell'
            return None
        return abs_fn

    def pct_fn(code, date):
        s = score_by[code].get(date)
        if s is None:
            return None
        thr = (pct_thr or {}).get(code, {}).get(date)
        if thr is None:
            return None  # 阈值样本不足：不产生信号
        p80, p20 = thr
        if s >= p80:
            return 'buy'
        if s <= p20:
            return 'sell'
        return None
    return pct_fn


# ===== 策略模拟 =====

def simulate_strategy(stock_daily, daily_close, fut_map, mode='abs', pct_thr=None):
    """
    等权组合策略模拟，支持两种信号口径：
    - mode='abs'：绝对分数（>=SCORE_BUY 持仓 / <SCORE_SELL 空仓）
    - mode='pct'：个股历史分数百分位（>=自身 p80 持仓 / <=自身 p20 空仓）
    其余逻辑相同：中间区保持前态；信号滞后1日（T日信号→T+1日持仓）；基准 = 全部股票等权买入持有。
    """
    # 公共日历：全部股票日期并集
    all_dates = sorted(set().union(*[set(dc.keys()) for dc in daily_close.values()]))
    # 公共起点：所有股票都有首个分数的日期之后
    first_scores = {}
    for code, daily in stock_daily.items():
        if daily:
            first_scores[code] = daily[0]['date']
    if not first_scores:
        return None
    common_start = max(first_scores.values())
    # 公共终点：最后一个分数日（时间段截断后净值曲线不应延伸到最新数据）
    common_end = max(daily[-1]['date'] for daily in stock_daily.values())
    dates = [d for d in all_dates if common_start <= d <= common_end]
    if len(dates) < 60:
        return None

    # date → score 索引（每日取当日样本；分数取当日有效值）
    score_by = {code: {d['date']: d['score'] for d in daily} for code, daily in stock_daily.items()}
    close_by = {code: dc for code, dc in daily_close.items()}
    signal = _signal_fn(mode, score_by, pct_thr)

    prev_pos = {}          # code → 昨日持仓状态
    nav_s, nav_b = 1.0, 1.0
    curve = []
    turnover_days = 0
    up_days = 0
    for i, d in enumerate(dates):
        # 组合当日收益（用 T-1 日信号决定持仓）
        day_ret_s = []
        day_ret_b = []
        for code in daily_close:
            closes = close_by[code]
            if d not in closes or i == 0:
                continue
            prev_close = closes.get(dates[i - 1])
            if not prev_close or prev_close <= 0:
                continue
            ret = closes[d] / prev_close - 1
            if ret != ret:  # NaN 防护
                continue
            day_ret_b.append(ret)
            # 持仓状态：T-1 日信号
            sig = signal(code, dates[i - 1])
            holding = prev_pos.get(code, False)
            if sig == 'buy':
                holding = True
            elif sig == 'sell':
                holding = False
            # 中间区（或阈值样本不足）：保持前态
            if holding != prev_pos.get(code, False):
                turnover_days += 1
            prev_pos[code] = holding
            if holding:
                day_ret_s.append(ret)
        if not day_ret_b:
            continue
        r_s = sum(day_ret_s) / len(day_ret_s) if day_ret_s else 0.0
        r_b = sum(day_ret_b) / len(day_ret_b)
        nav_s *= (1 + r_s)
        nav_b *= (1 + r_b)
        if r_s > 0:
            up_days += 1
        curve.append({'date': d, 'strategy': round(nav_s, 6), 'bench': round(nav_b, 6)})

    if not curve:
        return None

    # 指标
    def _stats(nav_series):
        total = nav_series[-1] / nav_series[0] - 1
        n_days = len(nav_series)
        annual = (nav_series[-1] / nav_series[0]) ** (252.0 / n_days) - 1 if n_days > 0 else 0
        peak = nav_series[0]
        mdd = 0.0
        for v in nav_series:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return {'total_return': round(total, 4), 'annualized': round(annual, 4),
                'max_drawdown': round(mdd, 4), 'days': n_days}

    strat_nav = [c['strategy'] for c in curve]
    bench_nav = [c['bench'] for c in curve]
    result = {
        'start': curve[0]['date'], 'end': curve[-1]['date'],
        'strategy': _stats(strat_nav),
        'bench': _stats(bench_nav),
        'excess_total': round(strat_nav[-1] / strat_nav[0] - bench_nav[-1] / bench_nav[0], 4),
        'winrate': round(up_days / len(curve), 4),
        'turnover_ratio': round(turnover_days / len(curve), 4),
        'curve': curve,
    }
    return result


# ===== 主流程 =====

def main():
    parser = argparse.ArgumentParser(description='估值评分回测与有效性验证')
    parser.add_argument('--watchlist', default=WATCHLIST_PATH, help='watchlist 文件路径')
    parser.add_argument('--stocks', help='仅回测指定股票代码（逗号分隔）')
    parser.add_argument('--refresh-data', action='store_true', help='重新抓取数据（默认只用缓存）')
    parser.add_argument('--start', help='回测起点（YYYY-MM-DD，截断）')
    parser.add_argument('--end', help='回测终点（YYYY-MM-DD，截断）')
    args = parser.parse_args()

    run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(OUTPUT_ROOT, run_id)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, 'run.log')
    log = open(log_path, 'w', encoding='utf-8')

    def say(msg):
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        log.write(line + '\n')
        log.flush()

    stocks = load_watchlist(args.watchlist)
    if args.stocks:
        codes = {c.strip() for c in args.stocks.split(',')}
        stocks = [s for s in stocks if s['code'] in codes]
    say(f"回测股票数: {len(stocks)} (run_id={run_id})")

    # 数据准备 + PIT 评分
    stock_daily = {}      # code → [{'date','score',...}]
    daily_close = {}      # code → {date: close}
    fut_map = {}          # code → {date: {'fut_60','fut_250'}}
    stocks_meta = []
    failed = []
    for si, st in enumerate(stocks):
        code, model = st['code'], st['model']
        exchange = 'sh' if code.startswith('6') else 'sz'
        try:
            say(f"[{si + 1}/{len(stocks)}] {st['name']}({code}) model={model}")
            qfq = get_kline(code, exchange, no_cache=args.refresh_data)['kline']
            raw = get_kline_raw(code, exchange, no_cache=args.refresh_data)
            pershare = fetch_pershare_data(code, exchange)
            reports = fetch_financial_reports(code, exchange)
            info = fetch_stock_info(code, exchange)
            if not qfq or not raw:
                say(f"  跳过: K线数据不足")
                failed.append({'code': code, 'reason': 'kline'})
                continue
            weights = MODEL_PRESETS.get(model, MODEL_PRESETS['cyclical'])['weights']
            daily = compute_pit_scores(qfq, raw, reports, pershare, weights,
                                       info.get('total_shares', 0))
            if not daily:
                say(f"  跳过: 无有效分数")
                failed.append({'code': code, 'reason': 'no_scores'})
                continue
            if args.start:
                daily = [d for d in daily if d['date'] >= args.start]
            if args.end:
                daily = [d for d in daily if d['date'] <= args.end]
            if (args.start or args.end) and not daily:
                failed.append({'code': code, 'reason': 'period_cut'})
                continue
            closes = {r[0]: float(r[2]) for r in qfq}
            fut_map[code] = future_returns(
                [{'date': d, 'close': c} for d, c in sorted(closes.items())], FUTURE_HORIZONS)
            stock_daily[code] = daily
            daily_close[code] = closes
            stocks_meta.append({'name': st['name'], 'code': code, 'model': model,
                                'first_score': daily[0]['date'], 'last_score': daily[-1]['date'],
                                'samples': len(daily)})
            say(f"  分数 {len(daily)} 天 [{daily[0]['date']} ~ {daily[-1]['date']}]")
        except Exception as e:
            say(f"  失败: {e}")
            failed.append({'code': code, 'reason': str(e)[:80]})

    say(f"成功: {len(stock_daily)} / {len(stocks)}，失败: {len(failed)}")
    if not stock_daily:
        say("无可用数据，退出")
        log.close()
        sys.exit(1)

    # 验证指标
    say("计算 IC / 分层 / 策略...")
    ic = compute_ic(stock_daily, fut_map, FUTURE_HORIZONS)
    layers = compute_layers(stock_daily, fut_map, FUTURE_HORIZONS)
    strategy = simulate_strategy(stock_daily, daily_close, fut_map, mode='abs')
    # 百分位策略：个股历史分数分布 80th/20th（PIT 重算）
    pct_thr = compute_percentile_thresholds(stock_daily)
    strategy_pct = simulate_strategy(stock_daily, daily_close, fut_map, mode='pct', pct_thr=pct_thr)
    say(f"策略(abs): {strategy and strategy['strategy']['total_return']}  策略(pct): {strategy_pct and strategy_pct['strategy']['total_return']}  基准: {strategy and strategy['bench']['total_return']}")

    # 汇总日频数据（CSV）
    csv_path = os.path.join(out_dir, 'daily_scores.csv')
    csv_meta = {s['code']: s for s in stocks_meta}
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['date', 'name', 'code', 'model', 'close', 'pe_ttm', 'pb', 'score',
                    'status', 'pe_min', 'pe_max', 'pb_min', 'pb_max', 'eff_eps_year',
                    'p80', 'p20', 'fut_60', 'fut_250'])
        for code, daily in stock_daily.items():
            m = csv_meta[code]
            code_thr = pct_thr.get(code, {})
            for d in daily:
                fut = fut_map[code].get(d['date'], {})
                thr = code_thr.get(d['date'])
                w.writerow([
                    d['date'], m['name'], code, m['model'], d['close'], d['pe_ttm'], d['pb'],
                    d['score'], status_label(d['score']),
                    d['pe_min'], d['pe_max'], d['pb_min'], d['pb_max'], d['eff_eps_year'],
                    thr[0] if thr else '', thr[1] if thr else '',
                    fut.get('fut_60', ''), fut.get('fut_250', ''),
                ])
    say(f"daily_scores.csv: {sum(len(v) for v in stock_daily.values())} 行")

    # meta.json
    meta = {
        'run_id': run_id, 'generated_at': datetime.datetime.now().isoformat(),
        'stock_count': len(stocks_meta), 'failed': failed,
        'params': {
            'future_horizons': list(FUTURE_HORIZONS), 'weekly_sample': WEEKLY_SAMPLE,
            'score_buy': SCORE_BUY, 'score_sell': SCORE_SELL,
            'no_trade_cost': NO_TRADE_COST,
            'disclosure_assumption': f'年报次年{DISCLOSURE_MONTH}月1日生效',
            'range': 'expanding 10th/90th 百分位, 年度切片重算',
            'pe_price': '不复权真实价/已生效EPS', 'no_pe_fallback': True,
            'dps_assumption': '未提供DPS，股息率因子缺失（权重再分配）',
            'signal_lag': 'T日分数→T+1日持仓',
            'pct_strategy': f'个股历史分数百分位: >=自身{PCT_BUY:.0%}买入 / <=自身{PCT_SELL:.0%}卖出, 年度切片PIT重算, 样本>={PCT_MIN_SAMPLES}',
            'period': {'start': args.start or '自动', 'end': args.end or '最新数据日'},
        },
        'models': {k: {'name': v['name'], 'weights': v['weights']}
                   for k, v in MODEL_PRESETS.items()},
        'stocks': stocks_meta,
    }
    with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    metrics = {'ic': ic, 'layers': layers, 'strategy': strategy, 'strategy_pct': strategy_pct}
    with open(os.path.join(out_dir, 'metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)

    with open(os.path.join(out_dir, 'curves.json'), 'w', encoding='utf-8') as f:
        json.dump({'strategy': strategy, 'strategy_pct': strategy_pct}, f, ensure_ascii=False, indent=1)

    # HTML 报告
    from report_builder import build_html_report
    html_path = os.path.join(out_dir, 'backtest_report.html')
    build_html_report(html_path, meta, metrics, stock_daily, fut_map, stocks_meta, pct_thr)
    say(f"报告: {html_path}")

    # 阅读说明副本（与报告同目录，链接始终有效）
    guide_src = os.path.join(_PROJECT_ROOT, 'docs', 'backtest_guide.md')
    if os.path.exists(guide_src):
        import shutil
        shutil.copyfile(guide_src, os.path.join(out_dir, 'reading_guide.md'))
        say(f"阅读说明: {os.path.join(out_dir, 'reading_guide.md')}")

    # 固定入口
    latest = os.path.join(OUTPUT_ROOT, 'backtest_latest.html')
    with open(latest, 'w', encoding='utf-8') as f:
        f.write(open(html_path, encoding='utf-8').read())
    say(f"固定入口: {latest}")

    say("完成")
    log.close()


def status_label(score):
    if score >= 80:
        return '极度低估'
    if score >= 70:
        return '低估'
    if score >= 40:
        return '中性'
    if score >= 20:
        return '高估'
    return '极度高估'


if __name__ == '__main__':
    main()
