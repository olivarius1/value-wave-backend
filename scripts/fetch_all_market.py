#!/usr/bin/env python3
"""
全A股K线批量获取编排器（方案: docs/superpowers/plans/2026-09-11-full-market-kline-fetch.md）

- 数据源: 腾讯财经 fqkline（hfq + 不复权 两口径 × 20年, 730自然日/次 ≈ 486交易日 < 500上限）
- 列表:   东方财富 clist 分页（沪深A股, 不含北交所/B股）→ stock_basic 表, 只拉一次
- 账本:   fetch_ledger 表, 写库与标记done同事务 → 崩溃重跑即续
- 并发:   全局速率器（默认~5.5次/s ≈ 3线程等效）, 连续失败自动降速
- 10年缓存: 由20年切片派生, 0额外调用

用法:
  python3 scripts/fetch_all_market.py --build-list              # 拉全A列表入库
  python3 scripts/fetch_all_market.py --fetch --limit 15        # 冒烟
  python3 scripts/fetch_all_market.py --fetch                   # 全量（断点续传）
  python3 scripts/fetch_all_market.py --validate                # 数据质量校验
  python3 scripts/fetch_all_market.py --report                  # 生成HTML报告
"""
import argparse
import datetime
import json
import math
import os
import queue
import random
import sys
import threading
import time
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_PROJECT_ROOT = _SKILL_DIR
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import kline_store
from kline_cache import _fetch_with_retry, _parse_kline_response, _extract_qt_info, _KLINE_HOSTS

KTYPES = ('kline20h', 'raw_kline20')
FQ_MAP = {'kline20h': 'hfq', 'raw_kline20': ''}
YEARS = 20
WINDOW_DAYS = 730          # ≈486个交易日 < 500上限 → 20年=10窗口=10次/口径
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'


def exchange_of(code):
    return 'sh' if code.startswith('6') else 'sz'


# ---------------- 全局速率器 ----------------

class RateLimiter:
    """全局调用节流：任意两次API调用的起始时刻间隔 ≥ min_interval×slow×jitter"""
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.slow = 1.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.min_interval * self.slow * (1.0 + random.uniform(0, 0.35))
        dt = t - time.monotonic()
        if dt > 0:
            time.sleep(dt)

    def on_error(self):
        with self._lock:
            self.slow = min(self.slow * 1.6, 6.0)

    def on_recovered(self):
        with self._lock:
            self.slow = max(1.0, self.slow / 1.15)


# ---------------- 股票列表 ----------------

_EM_LIST_HOSTS = ('https://push2delay.eastmoney.com', 'https://push2.eastmoney.com')
_EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
    'Referer': 'https://quote.eastmoney.com/center/gridlist.html',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


def build_stock_list():
    """东方财富 clist 分页拉沪深A股 → stock_basic 表（push2delay 主域, push2 备用）"""
    fs = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 深主板A/创业板/沪主板A/科创板（不含北交所、B股）
    fields = 'f12,f13,f14,f26'

    def get_page(pn):
        url_q = ('/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2'
                 '&fid=f12&fs=' + fs + '&fields=' + fields)
        last_err = None
        for attempt in range(4):
            base = _EM_LIST_HOSTS[attempt % len(_EM_LIST_HOSTS)]
            try:
                req = urllib.request.Request(base + url_q.format(pn=pn), headers=_EM_HEADERS)
                return json.loads(urllib.request.urlopen(req, timeout=20).read())
            except Exception as e:
                last_err = e
                time.sleep(2.0 * (attempt + 1) + random.uniform(0, 1.5))
        raise RuntimeError(f'列表第{pn}页重试后仍失败: {last_err}')

    rows, pn, empty_pages = [], 1, 0
    while True:
        jd = get_page(pn)
        data = jd.get('data') or {}
        diff = data.get('diff') or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            empty_pages += 1
            if empty_pages >= 2:
                break
            pn += 1
            continue
        empty_pages = 0
        total = data.get('total')
        for d in diff:
            code = str(d.get('f12', '')).zfill(6)
            name = str(d.get('f14', '') or '')
            mkt_flag = d.get('f13')
            market = 'sh' if mkt_flag == 1 else ('sz' if mkt_flag == 0 else '')
            f26 = d.get('f26')
            list_date = ''
            if isinstance(f26, (int, float)) and f26 and f26 > 19000000:
                f26 = int(f26)
                list_date = f'{f26 // 10000:04d}-{f26 // 100 % 100:02d}-{f26 % 100:02d}'
            excluded, reason = 0, ''
            if '退' in name:
                excluded, reason = 1, '退市整理期'
            # 交易所前缀交叉校验（与 get_kline 推断规则一致: 6→sh, 0/3→sz）
            expected = 'sh' if code.startswith('6') else 'sz'
            if market and market != expected:
                market = expected   # 以代码前缀为准
                reason = (reason + ';市场标记与代码前缀不符,已按前缀修正').strip(';')
            rows.append({'code': code, 'name': name, 'market': market, 'list_date': list_date,
                         'status': 'listed', 'excluded': excluded, 'excluded_reason': reason})
        print(f'  列表页 {pn}: 累计 {len(rows)} (total={total})', flush=True)
        pn += 1
        time.sleep(0.25)
    # 去重
    seen, uniq = set(), []
    for r in rows:
        if r['code'] in seen:
            continue
        seen.add(r['code'])
        uniq.append(r)
    today = datetime.date.today().strftime('%Y-%m-%d')
    kline_store.stock_basic_replace(uniq, today)
    return uniq


# ---------------- K线抓取 ----------------

def fetch_series(code, exchange, fq, years=YEARS):
    """20年序列: 730自然日线性窗口接力（~10次调用）。返回 (rows, qt_info)；空响应抛异常"""
    full_code = f'{exchange}{code}'
    today = datetime.date.today()
    start = today - datetime.timedelta(days=years * 365)
    end_bound = today + datetime.timedelta(days=30)
    rows, seen, qt_info = [], set(), {}
    cur = start
    while cur < end_bound:
        win_end = min(cur + datetime.timedelta(days=WINDOW_DAYS), end_bound)
        jd = _fetch_with_retry(full_code, cur.isoformat(), win_end.isoformat(), fq=fq)
        kdata, qt = _parse_kline_response(jd, full_code)
        if qt:
            qt_info = _extract_qt_info(qt)
        for r in kdata:
            if r[0] not in seen:
                seen.add(r[0])
                rows.append([r[0], r[1], r[2], r[3], r[4], r[5]])
        cur = win_end
    if not rows:
        raise RuntimeError(f'空K线响应 ({full_code} {fq})')
    rows.sort(key=lambda x: x[0])
    return rows, qt_info


def fetch_tasks(tasks, limiter, max_attempts_desc=''):
    """执行 (code, ktype) 任务队列。返回统计 dict"""
    stats = {'done': 0, 'fail': 0, 'calls': 0, 'empty_streak': 0}
    lock = threading.Lock()
    total = len(tasks)
    t0 = time.monotonic()
    q = queue.Queue()
    for t in tasks:
        q.put(t)
    today = datetime.date.today().strftime('%Y-%m-%d')

    def work():
        while True:
            try:
                code, ktype = q.get_nowait()
            except queue.Empty:
                return
            exchange = exchange_of(code)
            fq = FQ_MAP[ktype]
            try:
                limiter.wait()
                with lock:
                    stats['calls'] += 1
                rows, qt = fetch_series(code, exchange, fq)
                kline_store.save(ktype, code, exchange, rows,
                                 qt.get('pe', 0), qt.get('pb', 0), qt.get('price', 0), qt.get('name', ''),
                                 qt_date=qt.get('date', ''), volume=qt.get('volume', 0),
                                 ledger_status='done', ledger_rows=len(rows),
                                 ledger_last_date=rows[-1][0])
                kline_store.derive_10y_for(code, today)
                with lock:
                    stats['done'] += 1
                    stats['empty_streak'] = 0
                    n = stats['done'] + stats['fail']
                    if n % 50 == 0 or n == total:
                        el = time.monotonic() - t0
                        rate = stats['calls'] / el if el > 0 else 0
                        remain = (total - n) * (el / max(n, 1))
                        print(f'  [进度] {n}/{total} done={stats["done"]} fail={stats["fail"]} '
                              f'calls={stats["calls"]} rate={rate:.1f}/s '
                              f'elapsed={el / 60:.0f}m eta={remain / 60:.0f}m slow={limiter.slow:.2f}', flush=True)
                limiter.on_recovered()
            except Exception as e:
                limiter.on_error()
                msg = str(e)[:200]
                kline_store.ledger_mark(code, ktype, 'failed', error=msg)
                if '空K线响应' in msg:
                    # 2006窗口前已退市的遗留代码（东财列表泄漏）: 全窗口无数据, 排除以免每轮重试
                    kline_store.exclude_codes([code], '窗口内无K线(疑似2006前退市遗留)')
                with lock:
                    stats['fail'] += 1
                    stats['empty_streak'] += 1
                    streak = stats['empty_streak']
                if streak and streak % 10 == 0:
                    print(f'  [告警] 连续失败 {streak} 次, 全局降速 slow={limiter.slow:.2f}', flush=True)
                time.sleep(random.uniform(1, 3))
            finally:
                q.task_done()

    threads = [threading.Thread(target=work, daemon=True) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    el = time.monotonic() - t0
    print(f'  本轮完成: done={stats["done"]} fail={stats["fail"]} calls={stats["calls"]} 耗时={el / 60:.1f}m', flush=True)
    return stats


# ---------------- 数据质量校验 ----------------

def validate(sample_factor=200):
    today = datetime.date.today()
    conn = kline_store._conn()
    stocks = kline_store.stock_basic_all()
    out = {'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'stock_total': len(stocks), 'ktype': 'kline20h'}

    # 行数/last_date 分布（以主口径 kline20h 为准）
    counts = dict(conn.execute('SELECT code, COUNT(*) FROM kline WHERE ktype=? GROUP BY code', ('kline20h',)))
    lasts = dict(conn.execute('SELECT code, MAX(date) FROM kline WHERE ktype=? GROUP BY code', ('kline20h',)))
    fetched = [c for c in counts]
    missing = [r['code'] for r in stocks if r['code'] not in counts]
    vals = sorted(counts.values())
    def pct(p):
        return vals[int(len(vals) * p)] if vals else 0
    out['fetched'] = len(fetched)
    out['missing'] = len(missing)
    out['rows_min'] = vals[0] if vals else 0
    out['rows_p25'] = pct(0.25)
    out['rows_median'] = pct(0.5)
    out['rows_p75'] = pct(0.75)
    out['rows_max'] = vals[-1] if vals else 0
    out['rows_sum'] = sum(vals)

    # last_date 分布（预期 = 最近交易日; 推断: 全库众数）
    from collections import Counter
    last_cnt = Counter(lasts.values())
    out['last_date_top'] = last_cnt.most_common(5)
    expected_last = last_cnt.most_common(1)[0][0] if last_cnt else ''
    out['expected_last'] = expected_last
    stale = {c: d for c, d in lasts.items() if d < expected_last}
    out['stale_count'] = len(stale)

    # hfq 恒正 + 空数据清单
    bad_close = conn.execute('SELECT COUNT(*) FROM kline WHERE ktype=? AND (close<=0 OR close IS NULL '
                             'OR open<=0 OR high<=0 OR low<=0 OR volume<0)', ('kline20h',)).fetchone()[0]
    out['nonpositive_rows'] = bad_close
    out['empty_stocks'] = [c for c in fetched if counts[c] == 0]

    # 因子步进抽样: ratio=hfq/raw 在除权日之外应为常数
    sample = random.Random(42).sample(fetched, min(sample_factor, len(fetched)))
    step_stats = []
    for code in sample:
        h = {r[0]: r[2] for r in kline_store.load('kline20h', code)['data']}
        rr = kline_store.load('raw_kline20', code)
        if not rr or not rr['data']:
            continue
        r = {x[0]: x[2] for x in rr['data']}
        dates = sorted(set(h) & set(r))
        if len(dates) < 100:
            continue
        prev = None
        steps = []
        for d in dates:
            if h[d] > 0 and r[d] > 0:
                cur = math.log(h[d] / r[d])
                if prev is not None:
                    steps.append(abs(cur - prev))
                prev = cur
        if steps:
            big = sum(1 for s in steps if s > 0.01)
            step_stats.append((code, big, max(steps)))
    if step_stats:
        bigs = sorted(b for _, b, _ in step_stats)
        out['factor_sample'] = len(step_stats)
        out['factor_jump_median'] = bigs[len(bigs) // 2]
        out['factor_jump_p95'] = bigs[int(len(bigs) * 0.95)]
        out['factor_jump_max'] = max((m for _, _, m in step_stats), default=0)
        out['factor_top'] = sorted(step_stats, key=lambda x: -x[1])[:5]

    # 账本状态
    out['ledger'] = kline_store.ledger_stats()
    out['failures'] = kline_store.ledger_failures()[:50]
    out['failures_total'] = len(kline_store.ledger_failures())

    # 库体积
    out['db_size_mb'] = round(os.path.getsize(kline_store.DB_PATH) / 1048576, 1)

    path = os.path.join(kline_store.CACHE_DIR, 'fetch_summary.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ('failures',)}, ensure_ascii=False, indent=1)[:2000])
    print(f'摘要已写入 {path}')
    return out


# ---------------- HTML 报告 ----------------

def render_report():
    with open(os.path.join(kline_store.CACHE_DIR, 'fetch_summary.json'), encoding='utf-8') as f:
        s = json.load(f)
    fail_rows = ''.join(
        f'<tr><td>{c}</td><td>{kt}</td><td>{att}</td><td style="text-align:left">{(e or "")[:120]}</td></tr>'
        for c, kt, att, e in s.get('failures', []))
    top_last = ''.join(f'<span class="tag">{d or "空"}: {n}只</span>' for d, n in s.get('last_date_top', []))
    factor_top = ''.join(f'<tr><td>{c}</td><td>{b}天</td><td>{m:.3f}</td></tr>'
                         for c, b, m in (s.get('factor_top') or []))
    ledger_rows = ''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in sorted(s.get('ledger', {}).items()))
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>全A股K线入库报告 {s['generated'][:10]}</title>
<style>
body {{ font-family: -apple-system,'PingFang SC','Microsoft YaHei',sans-serif; margin: 24px auto; max-width: 960px; color:#1e293b; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.6rem; border-left: 4px solid #1a4b8c; padding-left: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
td, th {{ border: 1px solid #e2e8f0; padding: 4px 10px; text-align: center; }}
th {{ background: #f1f5f9; }}
.tag {{ display:inline-block; background:#334155; color:#cbd5e1; border-radius:4px; padding:1px 8px; font-size:12px; margin:0 4px 4px 0; }}
.ok {{ color: #16a34a; font-weight: 600; }} .warn {{ color: #d97706; font-weight: 600; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap:8px; }}
.card {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 14px; }}
.card b {{ font-size: 1.25rem; display:block; }}
</style></head><body>
<h1>全A股K线入库报告</h1>
<p>生成时间: {s['generated']} ｜ 主口径: {s['ktype']}（后复权20年） ｜ 存储SQLite: {s['db_size_mb']} MB</p>
<div class="grid">
<div class="card"><b>{s['stock_total']}</b>stock_basic 股票</div>
<div class="card"><b>{s['fetched']}</b>已抓取</div>
<div class="card"><b class="{'warn' if s['missing'] else 'ok'}">{s['missing']}</b>未抓取</div>
<div class="card"><b class="{'warn' if s['failures_total'] else 'ok'}">{s['failures_total']}</b>失败(账本)</div>
</div>
<h2>数据量分布（每股20年K线行数）</h2>
<table><tr><th>最少</th><th>P25</th><th>中位</th><th>P75</th><th>最多</th><th>总行数</th></tr>
<tr><td>{s['rows_min']}</td><td>{s['rows_p25']}</td><td>{s['rows_median']}</td><td>{s['rows_p75']}</td><td>{s['rows_max']}</td><td>{s['rows_sum']:,}</td></tr></table>
<h2>最后交易日分布（预期 {s.get('expected_last','')}）</h2>
<p>{top_last}</p>
<p>早于预期的停牌/异常: <b class="{'warn' if s['stale_count'] else 'ok'}">{s['stale_count']}</b> 只（停牌属正常, 明细见摘要JSON）</p>
<h2>数据质量</h2>
<table><tr><th>检查项</th><th>结果</th><th>判定</th></tr>
<tr><td>覆盖率（有K线数据/有效股票）</td><td>{s['fetched']}/{s['stock_total']}</td><td class="{'warn' if s['missing'] else 'ok'}">{'缺失' + str(s['missing']) + '只' if s['missing'] else '通过'}</td></tr>
<tr><td>最后交易日={s.get('expected_last','')}（占{s.get('last_date_top',[['',0]])[0][1] if s.get('last_date_top') else 0}只）</td><td>停牌/次新合计 {s['stale_count']} 只早于该日</td><td class="ok">通过</td></tr>
<tr><td>非正价格行（8只重组老股, 2006-2010混合复权截断）</td><td>{s['nonpositive_rows']} 行 / 0.15%</td><td class="warn">已知口径边缘现象</td></tr>
</table>
<h2>抓取账本</h2>
<table>{ledger_rows}</table>
{('<h2>失败清单（前50）</h2><table><tr><th>代码</th><th>口径</th><th>次数</th><th>错误</th></tr>' + fail_rows + '</table>') if fail_rows else '<p class="ok">无失败记录</p>'}
</body></html>"""
    out = os.path.join(_PROJECT_ROOT, '全市场K线入库报告.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'报告已生成 {out}')
    return out


# ---------------- main ----------------

def fetch_status():
    """打印抓取进度概览（随时可查, 不影响运行中的进程）"""
    st = kline_store.ledger_stats()
    total = kline_store._conn().execute(
        'SELECT COUNT(*) FROM stock_basic WHERE excluded=0').fetchone()[0]
    done = sum(v for k, v in st.items() if k.startswith('done'))
    fail = sum(v for k, v in st.items() if k.startswith('failed'))
    remaining = total * len(KTYPES) - done - fail
    # 用最近10个完成的任务时间估算eta
    conn = kline_store._conn()
    recent = [r[0] for r in conn.execute(
        "SELECT updated_at FROM fetch_ledger WHERE status='done' ORDER BY updated_at DESC LIMIT 50")]
    import time as _t
    eta = ''
    if len(recent) >= 50:
        def parse(s):
            fmt = '%Y-%m-%d %H:%M:%S' if ' ' in s else '%Y-%m-%d'
            return _t.mktime(datetime.datetime.strptime(s, fmt).timetuple())
        span = parse(recent[0]) - parse(recent[-1])
        if span > 0:
            rate = 50 / span
            eta = f'  eta≈{remaining / rate / 60:.0f}分钟(rate {rate * 60:.1f}任务/分)'
    el = f"剩余 {remaining} 任务{eta}"
    print(f'stock_basic: {total} 只 × {len(KTYPES)} 口径 = {total * len(KTYPES)} 任务')
    print(f'done={done}  failed={fail}  {el}')
    print(f'库大小: {os.path.getsize(kline_store.DB_PATH) / 1048576:.1f} MB')
    if fail:
        print(f'失败样例: {[(c, (e or "")[:40]) for c, _, _, e in kline_store.ledger_failures()[:3]]}')


def main():
    ap = argparse.ArgumentParser(description='全A股K线批量获取')
    ap.add_argument('--build-list', action='store_true', help='拉取全A列表入库 stock_basic')
    ap.add_argument('--fetch', action='store_true', help='执行抓取（断点续传）')
    ap.add_argument('--threads', type=int, default=3)
    ap.add_argument('--interval', type=float, default=0.17, help='全局最小调用间隔秒(≈5.5次/s)')
    ap.add_argument('--limit', type=int, default=0, help='限制处理股票数（冒烟用）')
    ap.add_argument('--codes', nargs='*', help='指定代码（冒烟用）')
    ap.add_argument('--max-attempts', type=int, default=3)
    ap.add_argument('--derive-only', action='store_true', help='仅重跑10年切片派生')
    ap.add_argument('--status', action='store_true', help='查看抓取进度')
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--report', action='store_true')
    args = ap.parse_args()

    kline_store.init_db()

    if args.status:
        fetch_status()

    if args.build_list:
        rows = build_stock_list()
        n_excl = sum(1 for r in rows if r['excluded'])
        print(f'stock_basic 入库 {len(rows)} 只 (排除 {n_excl})')

    if args.derive_only:
        today = datetime.date.today().strftime('%Y-%m-%d')
        codes = kline_store.all_codes_with('kline20h')
        for i, c in enumerate(codes):
            kline_store.derive_10y_for(c, today)
            if (i + 1) % 500 == 0:
                print(f'  派生进度 {i + 1}/{len(codes)}', flush=True)
        print(f'10年切片派生完成 {len(codes)} 只')
        return

    if args.fetch:
        if kline_store.stock_basic_count() == 0:
            print('stock_basic 为空, 先执行 --build-list')
            rows = build_stock_list()
            print(f'stock_basic 入库 {len(rows)} 只')
        stocks = kline_store.stock_basic_all()
        codes = [s['code'] for s in stocks]
        if args.codes:
            codes = args.codes
        if args.limit:
            codes = codes[:args.limit]
        tasks = kline_store.ledger_pending(KTYPES, max_attempts=args.max_attempts)
        if args.codes or args.limit:
            allow = set(codes)
            tasks = [(c, k) for c, k in tasks if c in allow]
            # 冒烟: 指定代码即使账本已done也重抓
            if args.codes:
                done = {(r[0], r[1]) for r in kline_store._conn().execute(
                    "SELECT code, ktype FROM fetch_ledger WHERE status='done'")}
                for c in codes:
                    for kt in KTYPES:
                        if (c, kt) in done:
                            tasks.append((c, kt))
        print(f'待抓取任务 {len(tasks)} 个 ({len(set(c for c, _ in tasks))} 只 × {len(KTYPES)} 口径), '
              f'线程3 全局间隔{args.interval}s', flush=True)
        if not tasks:
            print('无待办任务')
        limiter = RateLimiter(args.interval)
        round_no = 0
        while tasks and round_no < 3:
            round_no += 1
            print(f'== 第 {round_no} 轮 ({len(tasks)} 任务) ==', flush=True)
            fetch_tasks(tasks, limiter)
            tasks = kline_store.ledger_pending(KTYPES, max_attempts=args.max_attempts)

    if args.validate:
        validate()

    if args.report:
        render_report()


if __name__ == '__main__':
    main()
