#!/usr/bin/env python3
"""
全A股财务数据批量获取编排器（方案: docs/superpowers/plans/2026-09-12-full-market-financial-fetch.md）
姊妹版: fetch_all_market.py（K线），复用其账本/限速/冒烟-全量-校验三段式结构。

数据源（iFinD 额度已耗尽，全程不使用）:
  ① akshare stock_yjbb_em  东财业绩报表·按报告期全市场批量——一次调用覆盖全市场，
     字段与 reports/pershare schema 一一对应；2005Q1~今 87 个报告期，主力通道
  ② 东财数据中心 datacenter.eastmoney.com  逐股兜底（缺口补齐/行业链/总股本，公开接口无额度）
  ③ akshare stock_fhps_detail_em  分红送转逐股（经 financial_fetcher 门面自动降级东财）
     ——亦可用 warmup_bonus_events.py 单独跑，两者同走门面同一份库

存储: fin_store.db (SQLite)，读取一律走 financial_fetcher 门面（30天TTL）。

用法:
  python3 scripts/fetch_all_financials.py --bulk-reports --limit 3   # 冒烟(前3期)
  python3 scripts/fetch_all_financials.py --bulk-reports             # 业绩报表批量(断点续传)
  python3 scripts/fetch_all_financials.py --gap-reports              # 无报告股票东财逐股补齐
  python3 scripts/fetch_all_financials.py --industry-info            # 行业链+总股本逐股
  python3 scripts/fetch_all_financials.py --status                   # 进度概览
  python3 scripts/fetch_all_financials.py --validate                 # 质量校验+摘要JSON
"""
import argparse
import datetime
import json
import os
import queue
import random
import sys
import threading
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import fin_store
import kline_store
import financial_fetcher
from ths_fetcher import _periods   # 与回测口径同源: 2005Q1 起至最近已结束季度


BULK_INTERVAL = 0.6        # yjbb 批量调用间隔（87次/全期）
YJBB_COLUMNS = {
    '股票代码': 'code',
    '每股收益': 'eps',
    '营业总收入-营业总收入': 'revenue',
    '营业总收入-同比增长': 'revenue_yoy',
    '净利润-净利润': 'net_profit',
    '净利润-同比增长': 'profit_yoy',
    '每股净资产': 'bps',
    '净资产收益率': 'roe',
    '销售毛利率': 'gross_margin',
}


def _num(v, scale=1.0, ndigits=None):
    """NaN/None → None；数值缩放取整"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:   # NaN
        return None
    return round(f * scale, ndigits) if ndigits is not None else f * scale


# ---------------- ① 业绩报表批量（akshare yjbb，全市场×全报告期） ----------------

def bulk_reports(limit=0, codes_filter=None):
    """按报告期批量入库。断点账本 task='yjbb:YYYYMMDD'。返回 (done, failed)"""
    import akshare as ak
    periods = _periods()
    if limit:
        periods = periods[:limit]
    done_tasks = fin_store.ledger_done_tasks()
    if codes_filter:
        allow = set(codes_filter)
    else:
        allow = None
    today = datetime.date.today().strftime('%Y-%m-%d')
    done, failed = 0, 0
    for period in periods:
        task = f"yjbb:{period.strftime('%Y%m%d')}"
        if task in done_tasks:
            done += 1
            continue
        df = None
        for attempt in range(1, 4):
            try:
                df = ak.stock_yjbb_em(date=period.strftime('%Y%m%d'))
                break
            except Exception as e:
                print(f'  {task} 第{attempt}次失败: {str(e)[:120]}', file=sys.stderr, flush=True)
                time.sleep(3.0 * attempt)
        if df is None or df.empty:
            fin_store.ledger_mark(task, 'failed', error='empty response after retries')
            failed += 1
            continue
        rows, pershare_rows, touched = [], [], []
        annual = period.month == 12
        for _, r in df.iterrows():
            code = str(r.get('股票代码') or '').zfill(6)
            if not (code.isdigit() and len(code) == 6):
                continue
            if allow is not None and code not in allow:
                continue
            rev = _num(r.get('营业总收入-营业总收入'), 1e-8, 2)
            np_ = _num(r.get('净利润-净利润'), 1e-8, 2)
            if rev is None and np_ is None:
                continue   # 未披露有效数据
            ryoy = _num(r.get('营业总收入-同比增长'), 1e-2, 4)
            pyoy = _num(r.get('净利润-同比增长'), 1e-2, 4)
            rows.append((code, period.isoformat(),
                         rev, np_,
                         _num(r.get('净资产收益率'), 1e-2, 4),
                         _num(r.get('销售毛利率'), 1e-2, 4),
                         ryoy, pyoy))
            if annual:
                pershare_rows.append((code, period.year,
                                      _num(r.get('每股收益')),
                                      _num(r.get('每股净资产'))))
            touched.append(code)
        fin_store.upsert_reports_rows(rows)
        if pershare_rows:
            fin_store.upsert_pershare_rows(pershare_rows)
        # 刷新度批量置位（executemany 单事务）
        conn = fin_store._conn()
        with fin_store._write_lock:
            conn.executemany('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                             [(c, 'reports', today, 'akshare:yjbb') for c in touched])
            if annual:
                conn.executemany('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                                 [(c, 'pershare', today, 'akshare:yjbb') for c in touched])
            conn.commit()
        fin_store.ledger_mark(task, 'done')
        done += 1
        print(f'  {task}: {len(rows)} 行 reports, {len(pershare_rows)} 行 pershare '
              f'({done + failed}/{len(periods)})', flush=True)
        time.sleep(BULK_INTERVAL)
    print(f'批量完成: done={done} failed={failed}')
    return done, failed


# ---------------- 逐股阶段通用执行器（缺口补齐/行业信息） ----------------

def run_per_stock(codes, task_prefix, worker, threads=3, interval=0.3, label=''):
    """逐股任务队列 + 全局限速 + fin_ledger 断点。worker(code)->None，异常记 failed"""
    stats = {'done': 0, 'fail': 0}
    lock = threading.Lock()
    q = queue.Queue()
    for c in codes:
        q.put(c)
    total = len(codes)
    t0 = time.monotonic()
    next_ok = [0.0]

    def work():
        while True:
            try:
                code = q.get_nowait()
            except queue.Empty:
                return
            try:
                with lock:
                    dt = next_ok[0] - time.monotonic()
                if dt > 0:
                    time.sleep(dt + random.uniform(0, interval * 0.2))
                with lock:
                    next_ok[0] = time.monotonic() + interval
                worker(code)
                fin_store.ledger_mark(f'{task_prefix}:{code}', 'done')
                with lock:
                    stats['done'] += 1
                    n = stats['done'] + stats['fail']
                    if n % 100 == 0 or n == total:
                        el = time.monotonic() - t0
                        eta = (total - n) * (el / max(n, 1)) / 60
                        print(f'  [{label}] {n}/{total} done={stats["done"]} fail={stats["fail"]} '
                              f'eta={eta:.0f}m', flush=True)
            except Exception as e:
                fin_store.ledger_mark(f'{task_prefix}:{code}', 'failed', error=str(e)[:200])
                with lock:
                    stats['fail'] += 1
                time.sleep(random.uniform(1, 3))
            finally:
                q.task_done()

    ths = [threading.Thread(target=work, daemon=True) for _ in range(threads)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    print(f'  [{label}] 完成: done={stats["done"]} fail={stats["fail"]}', flush=True)
    return stats


def gap_reports(threads=3, interval=0.3, limit=0):
    """无 reports 数据的股票 → 东财单股兜底（facade 自动写库）。task='reports:CODE'"""
    all_codes = [s['code'] for s in kline_store.stock_basic_all()]
    have = set(fin_store.codes_with('reports'))
    failed_tasks = {t.split(':', 1)[1] for t, _, _ in fin_store.ledger_failures(10 ** 9)
                    if t.startswith('reports:')}
    # 账本 failed 不重复自旋（attempts 由重跑 --gap-reports 间隔控制），只补无数据且未失败的
    codes = [c for c in all_codes if c not in have and c not in failed_tasks]
    if limit:
        codes = codes[:limit]
    print(f'缺口补齐: {len(codes)} 只（全市场 {len(all_codes)}，已有 reports {len(have)}）')

    def worker(code):
        exch = 'sh' if code.startswith('6') else 'sz'
        data = financial_fetcher.fetch_financial_reports(code, exch, max_reports=80)
        if not data:
            raise RuntimeError('东财无财报数据')

    return run_per_stock(codes, 'reports', worker, threads, interval, 'gap-reports')


def industry_info(threads=3, interval=0.3, limit=0):
    """行业链（EM2016 三级）+ 总股本。task='info:CODE'"""
    all_codes = [s['code'] for s in kline_store.stock_basic_all()]
    have_ind = set(fin_store.codes_with('industry'))
    have_info = set(fin_store.codes_with('info'))
    codes = [c for c in all_codes if c not in have_ind or c not in have_info]
    if limit:
        codes = codes[:limit]
    print(f'行业+总股本: {len(codes)} 只（已有 industry {len(have_ind)}, info {len(have_info)}）')

    def worker(code):
        exch = 'sh' if code.startswith('6') else 'sz'
        chain = financial_fetcher.fetch_industry_chain(code, exch)
        if not chain:
            raise RuntimeError('东财无行业数据')
        ts = financial_fetcher._fetch_total_shares(code, exch)
        if ts:
            fin_store.set_info(code, ts, source='east')

    return run_per_stock(codes, 'info', worker, threads, interval, 'industry-info')


# ---------------- 进度概览与质量校验 ----------------

def status():
    conn = fin_store._conn()
    print('== fin_store 覆盖 ==')
    for kind in ('reports', 'pershare', 'bonus', 'info', 'industry'):
        n = len(fin_store.codes_with(kind))
        fresh = conn.execute('SELECT COUNT(*) FROM fin_freshness WHERE kind=? AND updated>=?',
                             (kind, (datetime.date.today() - datetime.timedelta(days=30)).isoformat())).fetchone()[0]
        print(f'  {kind}: {n} 只 (30天内刷新 {fresh})')
    periods_all = len(_periods())
    yjbb_done = sum(1 for t in fin_store.ledger_done_tasks() if t.startswith('yjbb:'))
    print(f'== yjbb 报告期: {yjbb_done}/{periods_all} ==')
    print(f'== fin_ledger 失败: {len(fin_store.ledger_failures(10 ** 9))} ==')
    size = os.path.getsize(fin_store.DB_PATH) / 1048576 if os.path.exists(fin_store.DB_PATH) else 0
    print(f'== 库大小: {size:.1f} MB ==')


def validate():
    out = {'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    stocks = kline_store.stock_basic_all()
    all_codes = {s['code'] for s in stocks}
    conn = fin_store._conn()
    out['stock_total'] = len(all_codes)
    for kind in ('reports', 'pershare', 'bonus', 'info', 'industry'):
        have = set(fin_store.codes_with(kind))
        out[f'{kind}_coverage'] = len(have & all_codes)
        out[f'{kind}_missing'] = sorted(all_codes - have)[:50]
        out[f'{kind}_missing_n'] = len(all_codes - have)

    # 年报覆盖分布（2005起最多22份年报）
    annual_counts = dict(conn.execute(
        "SELECT code, COUNT(*) FROM fin_reports WHERE report_type='annual' GROUP BY code"))
    vals = sorted(annual_counts.get(c, 0) for c in all_codes)
    def pct(p):
        return vals[int(len(vals) * p)] if vals else 0
    out['annual_count_median'] = pct(0.5)
    out['annual_count_p10'] = pct(0.1)
    out['annual_ge18'] = sum(1 for v in vals if v >= 18)
    out['annual_le3'] = sum(1 for v in vals if v <= 3)

    # 有效性: 最新年报营收>0 占比、roe>0 占比
    latest = conn.execute(
        "SELECT f.code, f.revenue, f.net_profit, f.roe, f.gross_margin FROM fin_reports f "
        "JOIN (SELECT code, MAX(report_date) AS md FROM fin_reports "
        "      WHERE report_type='annual' GROUP BY code) t "
        "ON f.code=t.code AND f.report_date=t.md WHERE f.report_type='annual'").fetchall()
    out['annual_latest_n'] = len(latest)
    out['annual_latest_rev_pos'] = sum(1 for r in latest if (r[1] or 0) > 0)
    out['annual_latest_roe_pos'] = sum(1 for r in latest if (r[3] or 0) > 0)
    out['annual_latest_gm_pos'] = sum(1 for r in latest if (r[4] or 0) > 0)

    # 抽样目检: 3只知名股票最新年报
    sample = {}
    for code, name in (('600887', '伊利'), ('600519', '茅台'), ('601318', '平安')):
        reps = fin_store.load_reports(code)
        if reps:
            ann = [r for r in reps if r['report_type'] == 'annual']
            sample[code] = {'name': name, 'latest_annual': ann[0] if ann else None,
                            'reports_total': len(reps)}
    out['sample'] = sample

    # 账本
    out['ledger_stats'] = fin_store.ledger_stats()
    out['failures'] = fin_store.ledger_failures(50)
    out['db_size_mb'] = round(os.path.getsize(fin_store.DB_PATH) / 1048576, 1)

    path = os.path.join(fin_store.CACHE_DIR, 'fin_fetch_summary.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    brief = {k: v for k, v in out.items() if k not in ('failures',) and not k.endswith('_missing')}
    print(json.dumps(brief, ensure_ascii=False, indent=1)[:3000])
    print(f'摘要已写入 {path}')
    return out


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description='全A股财务数据批量获取')
    ap.add_argument('--bulk-reports', action='store_true', help='akshare业绩报表按报告期批量入库')
    ap.add_argument('--gap-reports', action='store_true', help='无报告股票东财逐股补齐')
    ap.add_argument('--industry-info', action='store_true', help='行业链+总股本逐股')
    ap.add_argument('--threads', type=int, default=3)
    ap.add_argument('--interval', type=float, default=0.3, help='逐股全局最小间隔秒')
    ap.add_argument('--limit', type=int, default=0, help='限制处理数量（冒烟用）')
    ap.add_argument('--codes', nargs='*', help='指定代码（冒烟用，配合--bulk-reports过滤）')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--validate', action='store_true')
    args = ap.parse_args()

    fin_store.init_db()
    if args.status:
        status()
    if args.bulk_reports:
        bulk_reports(limit=args.limit, codes_filter=args.codes)
    if args.gap_reports:
        gap_reports(args.threads, args.interval, args.limit)
    if args.industry_info:
        industry_info(args.threads, args.interval, args.limit)
    if args.validate:
        validate()
    if not any((args.bulk_reports, args.gap_reports, args.industry_info,
                args.status, args.validate)):
        ap.print_help()


if __name__ == '__main__':
    main()
