#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场算分因子输入回填 —— 把评分引擎用到的财务因子字段批量算好存入 kline_store.db 的
score_factors 表（与 model_classify 同库），供全市场扫描离线打分与规则交叉校验使用。

字段来源:
- compute_financial_metrics(fin_store.load_reports): avg_roe/avg_gross_margin/
  gross_margin_stability/revenue_growth_5y/latest_revenue_yoy/latest_profit_yoy/roe_trend
  （akshare 全市场预热数据，离线零网络）
- rd_ratio: model_classifier.fetch_rd_ratio（东财年报研发费用/营收，30天TTL缓存；联网）
- dps_ttm: fin_bonus 滚动365天每股现金分红求和（div 口径=元/股，与 build_total_return 一致）
- div_yield: dps_ttm / 最新价（kline meta 报价快照）

用法:
  python scripts/score_factors.py --status                 # 覆盖率统计
  python scripts/score_factors.py --backfill               # 全量回填（断点续传，30天内跳过）
  python scripts/score_factors.py --backfill --offline     # 只算离线部分(不拉研发费用率)
  python scripts/score_factors.py --backfill --codes 600887,000338 --force
"""
import argparse
import datetime
import os
import random
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import kline_store
import fin_store
from financial_fetcher import compute_financial_metrics
from model_classifier import fetch_rd_ratio

FRESH_DAYS = 30


def compute_dps_ttm(code, today):
    """滚动365天每股现金分红合计（div 口径=元/股）；无分红返回 0.0"""
    boundary = (today - datetime.timedelta(days=365)).isoformat()
    return round(sum(e['div'] for e in fin_store.load_bonus(code)
                     if e['div'] and e['date'] >= boundary), 4)


def compute_row(code, market, offline=False):
    """单只股票因子行。财报缺失返回 None；离线模式下 rd_ratio 不取网。"""
    reps = fin_store.load_reports(code)
    if not reps:
        return None
    m = compute_financial_metrics(reps)
    today = datetime.date.today()
    rd_ratio = None
    if not offline:
        try:
            rd_list = fetch_rd_ratio(code, market)
            rd_ratio = rd_list[0]['rd_ratio'] if rd_list else None
        except Exception as e:
            print(f'  [rd] {code} 获取失败: {e}', file=sys.stderr)
    dps_ttm = compute_dps_ttm(code, today)
    quote = None
    for kt in ('kline20h', 'klineh', 'kline'):
        quote = kline_store.meta_quote(kt, code)
        if quote and quote.get('price'):
            break
    price = quote['price'] if quote else None
    div_yield = round(dps_ttm / price, 4) if (dps_ttm and price) else None
    return {
        'code': code,
        'n_reports': m.get('annual_reports_count') or len(reps),
        'latest_report_date': (reps[0].get('report_date') or '')[:10],
        'avg_roe': m.get('avg_roe'), 'avg_gross_margin': m.get('avg_gross_margin'),
        'gross_margin_stability': m.get('gross_margin_stability'),
        'revenue_growth_5y': m.get('revenue_growth_5y'),
        'latest_revenue_yoy': m.get('latest_revenue_yoy'),
        'latest_profit_yoy': m.get('latest_profit_yoy'),
        'roe_trend': m.get('roe_trend'),
        'rd_ratio': rd_ratio, 'dps_ttm': dps_ttm or None, 'div_yield': div_yield,
    }


def cmd_backfill(args):
    if args.codes:
        wanted = [c.strip() for c in args.codes.split(',') if c.strip()]
        uni = {s['code']: s for s in kline_store.stock_basic_all(excluded=False)}
        stocks = [uni[c] for c in wanted if c in uni]
    else:
        stocks = [s for s in kline_store.stock_basic_all(excluded=False)
                  if s.get('status') == 'listed']
    existing = kline_store.score_factors_get()
    now = datetime.datetime.now()
    cutoff = (now - datetime.timedelta(days=FRESH_DAYS)).strftime('%Y-%m-%d')

    todo = []
    for s in stocks:
        row = existing.get(s['code'])
        if row and not args.force and (row.get('updated') or '')[:10] >= cutoff:
            continue  # 30天内已回填
        todo.append(s)
    print(f'[backfill] 宇宙{len(stocks)} 已有{len(existing)} → 待回填 {len(todo)} 只'
          f'{"(offline)" if args.offline else ""}')

    t0, rows_buf, no_report = time.time(), [], []
    for i, s in enumerate(todo):
        row = compute_row(s['code'], s['market'], offline=args.offline)
        if row is None:
            no_report.append(s['code'])
            continue
        rows_buf.append(row)
        if not args.offline:
            time.sleep(random.uniform(0.12, 0.3))  # 东财研发费用率礼貌限速
        if len(rows_buf) % 50 == 0:
            kline_store.score_factors_upsert(rows_buf)
            rows_buf = []
        if (i + 1) % 200 == 0:
            rate = (i + 1) / max(time.time() - t0, 1)
            remain = (len(todo) - i - 1) / max(rate, 0.01) / 60
            print(f'  [backfill] {i + 1}/{len(todo)} ({rate:.1f}只/s, 预计还需{remain:.0f}分钟)',
                  flush=True)
    if rows_buf:
        kline_store.score_factors_upsert(rows_buf)
    total = len(kline_store.score_factors_get())
    print(f'[backfill] 完成: 本次{len(todo) - len(no_report)}, 无财报跳过{len(no_report)}, '
          f'库内共{total}只, 耗时{(time.time() - t0) / 60:.1f}分钟')
    if no_report:
        print(f'  无财报样例(前10): {no_report[:10]}')


def cmd_status(args):
    rows = kline_store.score_factors_get()
    uni = [s for s in kline_store.stock_basic_all(excluded=False)
           if s.get('status') == 'listed']
    n = len(rows)
    print(f'===== score_factors 状态 =====')
    print(f'宇宙: {len(uni)} | 已回填: {n} ({n / len(uni) * 100:.1f}%)')
    if not rows:
        return

    def cov(key):
        v = sum(1 for r in rows.values() if r.get(key) is not None)
        return f'{key}: {v} ({v / n * 100:.0f}%)'

    print('字段覆盖: ' + ', '.join(cov(k) for k in
          ('avg_roe', 'gross_margin_stability', 'revenue_growth_5y', 'rd_ratio', 'div_yield')))
    stale = sum(1 for r in rows.values()
                if (r.get('updated') or '')[:10]
                < (datetime.datetime.now() - datetime.timedelta(days=FRESH_DAYS)).strftime('%Y-%m-%d'))
    print(f'超{FRESH_DAYS}天未刷新: {stale} 只')
    soe = [r for r in kline_store.model_classify_get().values() if r['model'] == 'soe']
    if soe:
        with_dps = sum(1 for r in soe if (rows.get(r['code']) or {}).get('div_yield'))
        hi = sum(1 for r in soe if ((rows.get(r['code']) or {}).get('div_yield') or 0) >= 0.04)
        print(f'soe证据: {len(soe)}只中 div_yield可得{with_dps}, ≥4%(符合高股息语义){hi}只')


def main():
    ap = argparse.ArgumentParser(description='全市场算分因子回填')
    ap.add_argument('--backfill', action='store_true', help='回填因子（断点续传）')
    ap.add_argument('--offline', action='store_true', help='不联网取研发费用率')
    ap.add_argument('--codes', help='逗号分隔股票代码')
    ap.add_argument('--force', action='store_true', help='忽略30天新鲜度强制重算')
    ap.add_argument('--status', action='store_true', help='覆盖率统计')
    args = ap.parse_args()
    kline_store.init_db()
    if args.status:
        cmd_status(args)
    elif args.backfill:
        cmd_backfill(args)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
