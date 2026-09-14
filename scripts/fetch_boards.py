#!/usr/bin/env python3
"""
板块/指数归属批量获取（stock_board 表，30天TTL）

- 数据源: 东财"所属板块"接口（push2delay，每股一次，含 行业/概念/地域/指数成分/交易属性 全部标签）
- 分类:   与三类板块清单（行业 t:2 / 地域 t:1）求交 → industry/region；
          概念清单内名字带指数特征（HS300_/上证50_/中证500/深证100R/MSCI中国/富时罗素...）→ index；
          融资融券/沪股通/深股通/转债标的 → attr；其余 → concept
- 存储:   kline_store.db 的 stock_board(board_code, code, board_name, board_type, updated)
- 刷新:   单股 30 天内已获取则跳过（--force 强制）；全市场约 5300 只 × 0.4s ≈ 35 分钟

用法:
  python3 scripts/fetch_boards.py --codes 600887,601318   # 冒烟
  python3 scripts/fetch_boards.py                          # 增量（30天内跳过）
  python3 scripts/fetch_boards.py --force                  # 全量重刷
  python3 scripts/fetch_boards.py --status                 # 覆盖概览
"""
import argparse
import datetime
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import kline_store
import fin_store

TTL_DAYS = 30
INTERVAL = 0.35            # 全局最小调用间隔秒
_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36',
       'Referer': 'https://quote.eastmoney.com/'}
_HOSTS = ('https://push2delay.eastmoney.com', 'https://push2.eastmoney.com')

_INDEX_RE = re.compile(r'(_|HS300|上证50|上证180|上证380|中证500|中证1000|深证100|深成500|创业板综|创业板指|科创50|央视50|MSCI中国|富时罗素)$')
_ATTR_RE = re.compile(r'^(融资融券|沪股通|深股通|转债标的|科创板做市股|科创板做市商)$')


def _get(url, retries=3):
    last = None
    for i in range(retries):
        host = _HOSTS[i % len(_HOSTS)]
        try:
            with urllib.request.urlopen(urllib.request.Request(host + url, headers=_UA), timeout=20) as r:
                return json.loads(r.read().decode('utf-8', errors='replace'))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _typed_board_sets():
    """三类板块清单（行业/概念/地域）的代码集合，用于分类"""
    out = {}
    for fs, label in (('m:90+t:2', 'industry'), ('m:90+t:3', 'concept'), ('m:90+t:1', 'region')):
        codes, pn = set(), 1
        while True:
            jd = _get(f'/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={fs}&fields=f12,f14')
            d = jd.get('data') or {}
            rows = d.get('diff') or []
            if not rows:
                break
            codes.update(r['f12'] for r in rows)
            if pn * 100 >= (d.get('total') or 0):
                break
            pn += 1
            time.sleep(0.25)
        out[label] = codes
        print(f'  [{label} 清单] {len(codes)} 个板块', flush=True)
    return out


def classify(board_code, board_name, typed):
    if board_code in typed['industry']:
        return 'industry'
    if board_code in typed['region']:
        return 'region'
    if _INDEX_RE.search(board_name or ''):
        return 'index'
    if _ATTR_RE.match(board_name or ''):
        return 'attr'
    return 'concept'


def fetch_one(code, typed):
    """单股所属板块 → 分类后列表 [(board_code, board_name, board_type)]"""
    secid = ('1.' if code.startswith('6') else '0.') + code
    jd = _get(f'/api/qt/slist/get?spt=3&fltt=2&invt=2&fields=f12,f14&secid={secid}&pn=1&po=1&np=1')
    rows = ((jd.get('data') or {}).get('diff')) or []
    out = []
    for r in rows:
        bc, bn = str(r['f12']), str(r['f14'] or '')
        out.append((bc, bn, classify(bc, bn, typed)))
    return out


def _next_ok_lock(lock, next_ok, interval):
    with lock:
        dt = next_ok[0] - time.monotonic()
        if dt > 0:
            time.sleep(dt + random.uniform(0, interval * 0.2))
        next_ok[0] = time.monotonic() + interval


def cmd_fetch(args):
    uni = {s['code']: s for s in kline_store.stock_basic_all(excluded=False)}
    if args.codes:
        wanted = [c.strip() for c in args.codes.split(',') if c.strip() in uni]
    else:
        # 只刷"仍在交易"的股票：最后交易日落后最新交易日 >60 天的退市/长期停牌遗留
        # 东财已无板块数据，刷了必失败（与分数面板的退市判定同口径）
        latest = kline_store._conn().execute(
            "SELECT MAX(last_date) FROM fetch_ledger WHERE ktype='kline20h' AND status='done'"
        ).fetchone()[0] or ''
        cutoff = latest or ''
        if cutoff:
            d = datetime.date.fromisoformat(cutoff[:10]) - datetime.timedelta(days=60)
            cutoff = d.isoformat()
        last_dates = {r[0]: r[1] for r in kline_store._conn().execute(
            "SELECT code, MAX(date) FROM kline WHERE ktype IN ('kline20h','kline20r','klineh') GROUP BY code")}
        wanted = sorted(c for c in uni if (last_dates.get(c) or '') >= cutoff)
        print(f'[boards] 宇宙 {len(uni)} → 交易中 {len(wanted)}（排除 {len(uni) - len(wanted)} 只退市/长期停牌遗留）', flush=True)
    if not args.force:
        cutoff = (datetime.date.today() - datetime.timedelta(days=TTL_DAYS)).isoformat()
        wanted = [c for c in wanted if kline_store.board_last_updated(c) < cutoff]
    total = len(wanted)
    print(f'[boards] 宇宙 {len(uni)} | 待获取 {total} 只' + ('' if args.force else '（30天内已获取的跳过）'), flush=True)
    if not total:
        return

    typed = _typed_board_sets()
    lock = threading.Lock()
    next_ok = [0.0]
    stats = {'done': 0, 'fail': 0}
    q = queue.Queue()
    for c in wanted:
        q.put(c)
    t0 = time.monotonic()

    def work():
        while True:
            try:
                code = q.get_nowait()
            except queue.Empty:
                return
            try:
                _next_ok_lock(lock, next_ok, INTERVAL)
                boards = fetch_one(code, typed)
                if not boards:
                    raise RuntimeError('无板块数据')
                today = datetime.date.today().isoformat()
                kline_store.boards_replace_for_code(code, boards, today)
                fin_store.ledger_mark(f'board:{code}', 'done')
                with lock:
                    stats['done'] += 1
                    n = stats['done'] + stats['fail']
                    if n % 100 == 0 or n == total:
                        el = time.monotonic() - t0
                        eta = (total - n) * (el / n) / 60 if n else 0
                        print(f'  [进度] {n}/{total} done={stats["done"]} fail={stats["fail"]} eta={eta:.0f}m', flush=True)
            except Exception as e:
                fin_store.ledger_mark(f'board:{code}', 'failed', error=str(e)[:200])
                with lock:
                    stats['fail'] += 1
                time.sleep(random.uniform(1, 3))
            finally:
                q.task_done()

    ths = [threading.Thread(target=work, daemon=True) for _ in range(args.threads)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    print(f'[boards] 完成: done={stats["done"]} fail={stats["fail"]}', flush=True)


def cmd_status():
    conn = kline_store._conn()
    covered = conn.execute('SELECT COUNT(DISTINCT code) FROM stock_board').fetchone()[0]
    rows = conn.execute('SELECT COUNT(*) FROM stock_board').fetchone()[0]
    last = conn.execute('SELECT MAX(updated) FROM stock_board').fetchone()[0]
    print(f'== stock_board 覆盖: {covered} 只 / {rows} 条关系 | 最近刷新 {last} ==')
    for bt in ('industry', 'concept', 'region', 'index', 'attr'):
        n = conn.execute('SELECT COUNT(DISTINCT board_code) FROM stock_board WHERE board_type=?', (bt,)).fetchone()[0]
        print(f'  {bt:9}: {n} 个板块')


def main():
    ap = argparse.ArgumentParser(description='板块/指数归属批量获取')
    ap.add_argument('--codes', help='逗号分隔股票代码')
    ap.add_argument('--force', action='store_true', help='忽略30天TTL强制重刷')
    ap.add_argument('--threads', type=int, default=3)
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()
    kline_store.init_db()
    if args.status:
        cmd_status()
    else:
        cmd_fetch(args)


if __name__ == '__main__':
    main()
