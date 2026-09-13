#!/usr/bin/env python3
"""
全市场分红送转事件表预热（东财 RPT_SHAREBONUS_DET）

用途：为"真·分红再投后复权"口径重建（build_total_return.py）准备事件数据。
- 复用 financial_fetcher.fetch_bonus_events（含30天本地缓存，失败不写缓存）
- 自有账本 bonus_ledger.json 支持断点续传（done/empty/failed）
- 限速：全局最小间隔 + jitter，3线程；失败指数退避重试

用法:
  python3 scripts/warmup_bonus_events.py --codes 000338 600036   # 冒烟
  python3 scripts/warmup_bonus_events.py                         # 全市场
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
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import kline_store
import fin_store
from financial_fetcher import fetch_bonus_events

LEDGER_PATH = os.path.join(kline_store.CACHE_DIR, 'bonus_ledger.json')
_lock = threading.Lock()


def load_ledger():
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_ledger(ledger):
    tmp = LEDGER_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False)
    os.replace(tmp, LEDGER_PATH)


def cache_exists(code):
    return fin_store.has(code, 'bonus')


def main():
    ap = argparse.ArgumentParser(description='分红送转事件表批量预热')
    ap.add_argument('--threads', type=int, default=3)
    ap.add_argument('--interval', type=float, default=0.28, help='全局最小调用间隔秒')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--codes', nargs='*')
    ap.add_argument('--retry-rounds', type=int, default=3)
    args = ap.parse_args()

    kline_store.init_db()
    ledger = load_ledger()
    if args.codes:
        todo = [(c, 'sh' if c.startswith('6') else 'sz') for c in args.codes]
    else:
        stocks = kline_store.stock_basic_all()
        todo = [(s['code'], s['market']) for s in stocks]
    if args.limit:
        todo = todo[:args.limit]
    # 断点续传：跳过已 done；有缓存文件的视为 done（除非 ledger 标记 failed）
    pending = []
    for code, mkt in todo:
        st = ledger.get(code, {})
        if st.get('status') == 'done':
            continue
        if st.get('status') != 'failed' and cache_exists(code):
            ledger[code] = {'status': 'done', 'reason': 'cache'}
            continue
        pending.append((code, mkt))
    print(f'总 {len(todo)} 只，待处理 {len(pending)} 只（账本已跳过 {len(todo) - len(pending)}）', flush=True)

    next_slot = [0.0]

    def wait_slot():
        with _lock:
            now = time.monotonic()
            t = max(now, next_slot[0])
            next_slot[0] = t + args.interval * (1 + random.uniform(0, 0.4))
        dt = t - time.monotonic()
        if dt > 0:
            time.sleep(dt)

    q = queue.Queue()
    for item in pending:
        q.put(item)
    stats = {'done': 0, 'empty': 0, 'failed': 0}
    total = len(pending)

    def work():
        while True:
            try:
                code, mkt = q.get_nowait()
            except queue.Empty:
                return
            try:
                wait_slot()
                events = fetch_bonus_events(code, mkt)
                with _lock:
                    if events:
                        ledger[code] = {'status': 'done', 'events': len(events),
                                        'first': events[0]['date'], 'last': events[-1]['date']}
                        stats['done'] += 1
                    else:
                        # 可能真的无分红，也可能接口失败（fetch_bonus_events 不区分）——标记 empty 待交叉校验
                        ledger[code] = {'status': 'empty'}
                        stats['empty'] += 1
                    n = stats['done'] + stats['empty'] + stats['failed']
                    if n % 100 == 0 or n == total:
                        print(f'  [进度] {n}/{total} 有事件={stats["done"]} 空={stats["empty"]} 失败={stats["failed"]}', flush=True)
            except Exception as e:
                with _lock:
                    prev = ledger.get(code, {})
                    attempts = prev.get('attempts', 0) + 1
                    ledger[code] = {'status': 'failed', 'attempts': attempts, 'error': str(e)[:120]}
                    stats['failed'] += 1
                time.sleep(random.uniform(1, 3))
            finally:
                q.task_done()

    for rnd in range(args.retry_rounds):
        if q.qsize() == 0 and rnd > 0:
            break
        if rnd > 0:
            # 重试 failed（attempts<3）
            retry = [(c, m) for c, m in pending
                     if ledger.get(c, {}).get('status') == 'failed'
                     and ledger.get(c, {}).get('attempts', 0) < 3]
            if not retry:
                break
            print(f'== 重试轮 {rnd}: {len(retry)} 只 ==', flush=True)
            stats = {'done': 0, 'empty': 0, 'failed': 0}
            total = len(retry)
            for it in retry:
                q.put(it)
        threads = [threading.Thread(target=work, daemon=True) for _ in range(args.threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        save_ledger(ledger)
        print(f'== 第 {rnd + 1} 轮完成: 有事件={stats["done"]} 空={stats["empty"]} 失败={stats["failed"]} ==', flush=True)

    save_ledger(ledger)
    done = sum(1 for v in ledger.values() if v.get('status') == 'done')
    empty = sum(1 for v in ledger.values() if v.get('status') == 'empty')
    failed = sum(1 for v in ledger.values() if v.get('status') == 'failed')
    print(f'预热完成: 有分红事件 {done} 只, 空 {empty} 只, 失败 {failed} 只')


if __name__ == '__main__':
    main()
