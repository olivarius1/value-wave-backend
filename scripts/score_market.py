#!/usr/bin/env python3
"""
全市场批量评分器 —— 系统统一分数来源（DB → DB，零报告依赖）

对每只上市股票用 scoring_engine 的因子函数 + MODEL_PRESETS 权重（模型取
model_classify，缺省回退 cyclical）计算日度分数序列（默认回看 15 年，
K线/财务数据不足 15 年的按实际可得天数计算）：

  数据源（全部读库，离线）:
    kline_store  kline20r/kline20h（收益/MA/量/波动，20年表内切15年）
                 raw_kline20（真实价，PE/PB 序列）
    fin_store    fin_reports（PEG 增速/可选因子）、fin_pershare+fin_bonus
                 （build_adjusted_series 逐日重述 EPS/BPS → 真实 PE/PB）
    score_factors 表（rd_ratio / div_yield）

  输出 score_store.db:
    score_series(code, date, score)   日度分数序列（历史分位的分布基础）
    stock_score(code, model, score, score_pct, price, pe, pb, pe_min/max,
                pb_min/max, n_days, last_date, computed_at)   每股当前状态

  score_pct = 当前分严格低于自身序列的比例×100 —— 分数面板高分(≥阈值)/低分(≤阈值)
  的判定口径（默认 90/20，阈值在面板上可调）。

用法:
  python3 scripts/score_market.py --codes 600887,000338 --force   # 冒烟
  python3 scripts/score_market.py                                  # 增量（K线无新数据跳过）
  python3 scripts/score_market.py --force                          # 全量重算
  python3 scripts/score_market.py --status                         # 覆盖概览
"""
import argparse
import datetime
import os
import sqlite3
import sys
import threading

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import kline_store
import fin_store
from financial_fetcher import (
    compute_financial_metrics, auto_fill_factors, build_adjusted_series,
)
from scoring_engine import (
    MODEL_PRESETS, resolve_active_weights, OPTIONAL_SCORE_FUNCS,
    score_pe, score_pb, score_peg, score_ma_deviation,
    score_volume, score_volatility, ret_std20,
)

SCORE_DB_PATH = os.path.join(kline_store.CACHE_DIR, 'score_store.db')
YEARS = 15   # 分数序列回看年数（K线/财务不足则按实际可得天数）
_local = threading.local()
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS score_series (
  code TEXT NOT NULL, date TEXT NOT NULL, score REAL,
  PRIMARY KEY (code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stock_score (
  code TEXT PRIMARY KEY, name TEXT, model TEXT,
  score REAL, score_pct REAL, price REAL, pe REAL, pb REAL,
  pe_min REAL, pe_max REAL, pb_min REAL, pb_max REAL,
  n_days INTEGER, last_date TEXT, computed_at TEXT
) WITHOUT ROWID;
"""


def _ensure_columns():
    """老库补列（暂无；保留为后续加列的迁移入口）"""
    conn = _conn()
    cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_score)')}
    for col, ddl in ():
        if col not in cols:
            conn.execute(f'ALTER TABLE stock_score ADD COLUMN {ddl}')
    conn.commit()


def _conn():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        os.makedirs(os.path.dirname(SCORE_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(SCORE_DB_PATH, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def init_db():
    _conn()
    _ensure_columns()


# ---------------- 单股序列计算 ----------------

def _slice_years(rows):
    """20年表内切最近 YEARS 年（与 kline_store.slice_last_years 同口径）；不足则返回全部"""
    boundary = (datetime.date.today() - datetime.timedelta(days=YEARS * 365)).isoformat()
    return [r for r in rows if r[0] >= boundary]


def _pick_hfq(code):
    """MA/量/波动用收益口径序列：kline20r（总收益）优先，回退 kline20h（行情hfq）；
    再回退存量10年 klineh。返回 [(date, close, volume)] 或 None"""
    for kt in ('kline20r', 'kline20h'):
        c = kline_store.load(kt, code)
        if c and c.get('data'):
            return [(r[0], float(r[2]), float(r[5])) for r in _slice_years(c['data'])]
    c = kline_store.load('klineh', code)
    if c and c.get('data'):
        return [(r[0], float(r[2]), float(r[5])) for r in c['data']]
    return None


def _pick_raw(code):
    """真实价序列（PE/PB 分母必须当日真实价）: raw_kline20 切15年, 回退存量 raw_kline"""
    c = kline_store.load('raw_kline20', code)
    if c and c.get('data'):
        return [(r[0], float(r[2])) for r in _slice_years(c['data'])]
    c = kline_store.load('raw_kline', code)
    if c and c.get('data'):
        return [(r[0], float(r[2])) for r in c['data']]
    return None


def _eps_growth(reports):
    """近5年年报净利润CAGR（与 scan_watchlist 同式）"""
    annuals = [r for r in reports if r['report_type'] == 'annual']
    growth = 0.08
    if len(annuals) >= 2:
        latest_p = annuals[0]['net_profit']
        oldest_idx = min(len(annuals) - 1, 4)
        oldest_p = annuals[oldest_idx]['net_profit']
        if latest_p > 0 and oldest_p > 0 and oldest_idx > 0:
            growth = round((latest_p / oldest_p) ** (1.0 / oldest_idx) - 1, 4)
    return growth


def _optional_factors(code, model):
    """可选因子: 财报指标 auto_fill + score_factors 表(rd_ratio/div_yield)"""
    optional = {}
    reports = fin_store.load_reports(code)
    if reports:
        optional = auto_fill_factors({}, compute_financial_metrics(reports), model)
        growth = _eps_growth(reports)
    else:
        growth = 0.08
    sf = kline_store.score_factors_get(code)
    if sf:
        if sf.get('rd_ratio') is not None:
            optional['rd_ratio'] = sf['rd_ratio']
        if sf.get('div_yield') is not None:
            optional['dividend_yield'] = sf['div_yield']
    return optional, growth


def compute_stock_series(code, model=None, name=''):
    """单股日度分数序列（回看 YEARS 年）。
    返回 dict(rows=[(date, score)], summary={...})；数据不足返回 None（reason）"""
    model = model or (kline_store.model_classify_get(code).get('model') if kline_store.model_classify_get(code) else None) or 'cyclical'
    preset = MODEL_PRESETS.get(model, MODEL_PRESETS['cyclical'])

    hfq = _pick_hfq(code)
    raw = _pick_raw(code)
    if not hfq or len(hfq) < 60:
        return None, 'K线不足'
    if not raw or len(raw) < 60:
        return None, '真实价K线不足'

    optional, eps_growth = _optional_factors(code, model)
    weights = resolve_active_weights(preset['weights'], optional)

    # 真实 PE/PB 逐日序列（EPS/BPS 逐日重述，除权日自然连续）
    pershare = fin_store.load_pershare(code)
    bonus = fin_store.load_bonus(code)
    raw_dates = [r[0] for r in raw]
    adj = build_adjusted_series(pershare, bonus, raw_dates) if pershare else None
    pe_by_day, pb_by_day = {}, {}
    for i, (d, close) in enumerate(raw):
        eps = adj['eps'].get(d) if adj else None
        bps = adj['bps'].get(d) if adj else None
        if eps and close > 0:
            pe = close / eps
            if 0 < pe < 500:
                pe_by_day[d] = pe
        if bps and bps > 0:
            pb = close / bps
            if 0 < pb < 50:
                pb_by_day[d] = pb

    if len(pe_by_day) >= 50:
        pes = sorted(pe_by_day.values())
        pe_min, pe_max = pes[int(len(pes) * 0.1)], pes[int(len(pes) * 0.9)]
    else:
        pe_min = pe_max = None
    if len(pb_by_day) >= 50:
        pbs = sorted(pb_by_day.values())
        pb_min, pb_max = pbs[int(len(pbs) * 0.1)], pbs[int(len(pbs) * 0.9)]
    else:
        pb_min = pb_max = None

    # hfq 收盘/量 按日期对齐
    hfq_map = {d: (c, v) for d, c, v in hfq}
    closes = [c for _, c, _ in hfq]

    def ma(vals, i, n):
        lo = max(0, i - n + 1)
        window = vals[lo:i + 1]
        return sum(window) / len(window)

    rows = []
    for i, (d, close_raw) in enumerate(raw):
        if d not in hfq_map:
            continue
        close_hfq, vol = hfq_map[d]
        total = 0.0
        for fk, w in weights.items():
            if w < 0.001:
                continue
            if fk == 'pe':
                s = score_pe(pe_by_day.get(d, 0), pe_min, pe_max) if pe_min else 50
            elif fk == 'pb':
                s = score_pb(pb_by_day.get(d, 0), pb_min, pb_max) if pb_min else 50
            elif fk == 'peg':
                s = score_peg(pe_by_day.get(d, 0), eps_growth)
            elif fk == 'ma':
                s = score_ma_deviation(close_hfq, ma(closes, i, 20), ma(closes, i, 60))
            elif fk == 'vol':
                s = score_volume(vol, ma([v for _, _, v in hfq[max(0, i - 19):i + 1]], len(hfq[max(0, i - 19):i + 1]) - 1, 20))
            elif fk == 'vola':
                s = score_volatility(ret_std20(closes[max(0, i - 20):i + 1]))
            elif fk in OPTIONAL_SCORE_FUNCS and optional.get(fk) is not None:
                s = OPTIONAL_SCORE_FUNCS[fk](optional[fk])
            else:
                s = 50
            total += s * w
        rows.append((d, round(total, 2)))

    if len(rows) < 60:
        return None, '对齐后天数不足'

    score = rows[-1][1]
    below = sum(1 for _, s in rows if s < score)
    last_raw_close = raw[-1][1]
    summary = {
        'model': model,
        'score': score,
        'score_pct': round(below / len(rows) * 100, 1),
        'price': last_raw_close,
        'pe': pe_by_day.get(raw_dates[-1]),
        'pb': pb_by_day.get(raw_dates[-1]),
        'pe_min': round(pe_min, 1) if pe_min else None,
        'pe_max': round(pe_max, 1) if pe_max else None,
        'pb_min': round(pb_min, 1) if pb_min else None,
        'pb_max': round(pb_max, 1) if pb_max else None,
        'n_days': len(rows),
        'last_date': rows[-1][0],
    }
    return rows, summary


# ---------------- 入库 ----------------

def _save(code, name, rows, summary, computed_at):
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM score_series WHERE code=?', (code,))
            conn.executemany('INSERT OR REPLACE INTO score_series VALUES (?,?,?)',
                             [(code, d, s) for d, s in rows])
            conn.execute(
                'INSERT OR REPLACE INTO stock_score (code,name,model,score,score_pct,price,'
                'pe,pb,pe_min,pe_max,pb_min,pb_max,n_days,last_date,computed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (code, name, summary['model'], summary['score'], summary['score_pct'],
                 summary['price'], summary['pe'], summary['pb'],
                 summary['pe_min'], summary['pe_max'], summary['pb_min'], summary['pb_max'],
                 summary['n_days'], summary['last_date'], computed_at))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def score_stocks(codes=None, force=False):
    """评分入库。codes=None 全市场（增量: K线无新数据跳过）。返回 (done, skipped, failed)"""
    uni = {s['code']: s for s in kline_store.stock_basic_all(excluded=False)}
    if codes:
        todo = [uni[c] for c in codes if c in uni]
    else:
        todo = [s for s in uni.values() if s.get('status') == 'listed']
    existing_last = {}
    if not force:
        conn = _conn()
        existing_last = {r[0]: r[1] for r in conn.execute('SELECT code, last_date FROM stock_score')}
    conn = _conn()
    kline_last = {r[0]: r[1] for r in kline_store._conn().execute(
        "SELECT code, MAX(date) FROM kline WHERE ktype IN ('kline20r','kline20h','klineh') GROUP BY code")}

    done = skipped = failed = 0
    today = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    t0 = datetime.datetime.now()
    for i, s in enumerate(todo, 1):
        code = s['code']
        if not force and existing_last.get(code) and kline_last.get(code, '') <= existing_last[code]:
            skipped += 1
            continue
        try:
            rows, summary = compute_stock_series(code, name=s['name'])
            if rows is None:
                failed += 1
                if failed <= 10:
                    print(f'  [跳过] {code} {s["name"]}: {summary}', flush=True)
            else:
                _save(code, s['name'], rows, summary, today)
                done += 1
        except Exception as e:
            failed += 1
            print(f'  [失败] {code} {s["name"]}: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
        if i % 25 == 0 or i == len(todo):
            el = (datetime.datetime.now() - t0).total_seconds()
            eta = (len(todo) - i) * (el / i) / 60 if i else 0
            print(f'  [进度] {i}/{len(todo)} done={done} skip={skipped} fail={failed} '
                  f'eta={eta:.0f}m', flush=True)
    return done, skipped, failed


def status():
    conn = _conn()
    n = conn.execute('SELECT COUNT(*) FROM stock_score').fetchone()[0]
    uni = kline_store.stock_basic_count()
    last = conn.execute('SELECT MAX(computed_at), MAX(last_date) FROM stock_score').fetchone()
    hi = conn.execute('SELECT COUNT(*) FROM stock_score WHERE score_pct>=90').fetchone()[0]
    lo = conn.execute('SELECT COUNT(*) FROM stock_score WHERE score_pct<=20').fetchone()[0]
    print(f'== stock_score: {n}/{uni} | 最近计算 {last[0]} | 序列至 {last[1]} ==')
    print(f'== 历史分位 ≥90%: {hi} 只 | ≤20%: {lo} 只 ==')
    series = conn.execute('SELECT COUNT(*) FROM score_series').fetchone()[0]
    print(f'== score_series 行数: {series} ==')


def main():
    ap = argparse.ArgumentParser(description='全市场批量评分（历史分位口径）')
    ap.add_argument('--codes', help='逗号分隔股票代码')
    ap.add_argument('--force', action='store_true', help='忽略增量跳过，全量重算')
    ap.add_argument('--status', action='store_true', help='覆盖概览')
    args = ap.parse_args()
    init_db()
    if args.status:
        status()
        return
    codes = [c.strip() for c in args.codes.split(',')] if args.codes else None
    done, skipped, failed = score_stocks(codes=codes, force=args.force)
    print(f'评分完成: done={done} skipped={skipped} failed={failed}')


if __name__ == '__main__':
    main()
