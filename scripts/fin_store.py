#!/usr/bin/env python3
"""
财务数据SQLite存储层——财报/每股指标/分红送转/行业与信息的统一存储

与 kline_store 同构的存储层（WAL + 线程本地连接 + 全局写锁），独立于 K线库文件。
数据源无关：akshare 批量、东财单股、iFinD 迁移数据统一落同一组表，读取方
（financial_fetcher 门面）不感知来源。

表结构:
  fin_reports(code, report_date, report_type, report_type_cn, revenue, net_profit,
              roe, gross_margin, revenue_yoy, profit_yoy)   报告期从新到旧由读取方排序
  fin_pershare(code, year, eps, bps)                       原值存储（含负EPS），口径过滤在门面
  fin_bonus(code, date, ratio, div)                        分红送转实施事件
  fin_info(code, total_shares)                             总股本等单值信息（营收/净利从reports派生）
  fin_industry(code, em_chain)                             EM2016 完整三级链，行业名取末段
  fin_extra(code, kind, data)                              杂项 kind 的JSON blob（如研发费用率）
  fin_freshness(code, kind, updated, source)               30天TTL刷新记录（kind=reports/pershare/bonus/info/industry/extra:rd…）
  fin_ledger(task, status, attempts, error, updated_at)    批量任务断点账本（task 如 'yjbb:20241231'/'bonus:000338'）

JSON缓存迁移: artifacts/.cache/financial/{code}_{kind}.json 一次性导入（migrate_json），
原文件保留作备份，此后读写全部走本库。
"""
import datetime
import json
import os
import sqlite3
import threading

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
CACHE_DIR = os.path.join(_SKILL_DIR, 'artifacts', '.cache')
DB_PATH = os.path.join(CACHE_DIR, 'fin_store.db')
LEGACY_FIN_DIR = os.path.join(CACHE_DIR, 'financial')

_local = threading.local()
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fin_reports (
  code TEXT NOT NULL, report_date TEXT NOT NULL,
  report_type TEXT, report_type_cn TEXT,
  revenue REAL, net_profit REAL, roe REAL, gross_margin REAL,
  revenue_yoy REAL, profit_yoy REAL,
  PRIMARY KEY (code, report_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_pershare (
  code TEXT NOT NULL, year INTEGER NOT NULL, eps REAL, bps REAL,
  PRIMARY KEY (code, year)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_bonus (
  code TEXT NOT NULL, date TEXT NOT NULL, ratio REAL, div REAL,
  PRIMARY KEY (code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_info (
  code TEXT PRIMARY KEY, total_shares REAL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_industry (
  code TEXT PRIMARY KEY, em_chain TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_extra (
  code TEXT NOT NULL, kind TEXT NOT NULL, data TEXT,
  PRIMARY KEY (code, kind)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_freshness (
  code TEXT NOT NULL, kind TEXT NOT NULL, updated TEXT NOT NULL, source TEXT,
  PRIMARY KEY (code, kind)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fin_ledger (
  task TEXT PRIMARY KEY, status TEXT, attempts INTEGER DEFAULT 0,
  error TEXT, updated_at TEXT
) WITHOUT ROWID;
"""


def _conn():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def init_db():
    _conn()


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


# ---------- fin_reports ----------

_REPORT_TYPE = {3: ('q1', '一季报'), 6: ('semi', '半年报'), 9: ('q3', '三季报'), 12: ('annual', '年报')}


def report_type_of(report_date):
    """'2024-12-31' → ('annual', '年报')"""
    rtype, rtype_cn = _REPORT_TYPE.get(int(report_date[5:7]), ('other', '其他'))
    return rtype, rtype_cn


def upsert_reports_rows(rows):
    """批量插入/覆盖（批量源按报告期写入）。rows: (code, report_date, revenue, net_profit,
    roe, gross_margin, revenue_yoy, profit_yoy)，类型字段由月份推导。"""
    conn = _conn()
    payload = []
    for code, rd, rev, np_, roe, gm, ryoy, pyoy in rows:
        rtype, rtype_cn = report_type_of(rd)
        payload.append((code, rd, rtype, rtype_cn, rev, np_, roe, gm, ryoy, pyoy))
    with _write_lock:
        conn.executemany('INSERT OR REPLACE INTO fin_reports VALUES (?,?,?,?,?,?,?,?,?,?)', payload)
        conn.commit()


def replace_reports(code, reports, updated=None, source=''):
    """整股覆写（单股源：东财兜底/迁移）。reports: 门面 schema 的 dict 列表（从新到旧）"""
    updated = updated or _today()
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM fin_reports WHERE code=?', (code,))
            conn.executemany(
                'INSERT OR REPLACE INTO fin_reports VALUES (?,?,?,?,?,?,?,?,?,?)',
                [(code, r['report_date'], r.get('report_type') or report_type_of(r['report_date'])[0],
                  r.get('report_type_cn') or report_type_of(r['report_date'])[1],
                  r.get('revenue', 0), r.get('net_profit', 0), r.get('roe', 0),
                  r.get('gross_margin', 0), r.get('revenue_yoy', 0), r.get('profit_yoy', 0))
                 for r in reports])
            conn.execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                         (code, 'reports', updated, source))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def load_reports(code):
    """读取并组装为门面 schema（从新到旧）。缺口 YoY 用本地同期环比回填（与 iFinD 路径同口径）"""
    rows = list(_conn().execute(
        'SELECT report_date, report_type, report_type_cn, revenue, net_profit, roe, '
        'gross_margin, revenue_yoy, profit_yoy FROM fin_reports WHERE code=? ORDER BY report_date DESC', (code,)))
    if not rows:
        return []
    by_period = {r[0]: r for r in rows}
    out = []
    for rd, rtype, rtype_cn, rev, np_, roe, gm, ryoy, pyoy in rows:
        cur = {'report_date': rd, 'report_type': rtype, 'report_type_cn': rtype_cn,
               'revenue': rev or 0, 'net_profit': np_ if np_ is not None else 0,
               'roe': roe or 0, 'gross_margin': gm or 0}
        prev = by_period.get(f"{int(rd[:4]) - 1}{rd[4:]}")
        # 缺失 YoY 用同期环比回填（列3=revenue, 列4=net_profit；与 iFinD 本地推导同口径）
        for key, val, prev_col, cur_val in (('revenue_yoy', ryoy, 3, cur['revenue']),
                                            ('profit_yoy', pyoy, 4, cur['net_profit'])):
            if val is not None:
                cur[key] = round(val, 4)
            elif prev is not None and prev[prev_col]:
                cur[key] = round(cur_val / prev[prev_col] - 1, 4)
            else:
                cur[key] = 0.0
        out.append(cur)
    return out


# ---------- fin_pershare ----------

def upsert_pershare_rows(rows):
    """rows: (code, year, eps, bps) 原值（含负EPS）"""
    with _write_lock:
        _conn().executemany('INSERT OR REPLACE INTO fin_pershare VALUES (?,?,?,?)', rows)
        _conn().commit()


def replace_pershare(code, pershare, updated=None, source=''):
    """整股覆写。pershare: [{'year','eps','bps'}] 从新到旧"""
    updated = updated or _today()
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM fin_pershare WHERE code=?', (code,))
            conn.executemany('INSERT OR REPLACE INTO fin_pershare VALUES (?,?,?,?)',
                             [(code, d['year'], d.get('eps', 0), d.get('bps', 0)) for d in pershare])
            conn.execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                         (code, 'pershare', updated, source))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def load_pershare(code):
    """原值读取（从新到旧），口径过滤（EPS<0 置0 保留BPS）由门面负责"""
    return [{'year': r[0], 'eps': r[1] if r[1] is not None else 0, 'bps': r[2] if r[2] is not None else 0}
            for r in _conn().execute(
                'SELECT year, eps, bps FROM fin_pershare WHERE code=? ORDER BY year DESC', (code,))]


# ---------- fin_bonus ----------

def replace_bonus(code, events, updated=None, source=''):
    """整股覆写。events: [{'date','ratio','div'}]（升序或乱序均可，读取方排序）"""
    updated = updated or _today()
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM fin_bonus WHERE code=?', (code,))
            conn.executemany('INSERT OR REPLACE INTO fin_bonus VALUES (?,?,?,?)',
                             [(code, e['date'], e.get('ratio', 0), e.get('div', 0)) for e in events])
            conn.execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                         (code, 'bonus', updated, source))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def load_bonus(code):
    return [{'date': r[0], 'ratio': r[1] or 0, 'div': r[2] or 0} for r in _conn().execute(
        'SELECT date, ratio, div FROM fin_bonus WHERE code=? ORDER BY date', (code,))]


# ---------- fin_info / fin_industry / fin_extra ----------

def set_info(code, total_shares, updated=None, source=''):
    with _write_lock:
        _conn().execute('INSERT OR REPLACE INTO fin_info VALUES (?,?)', (code, total_shares))
        _conn().execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                        (code, 'info', updated or _today(), source))
        _conn().commit()


def get_info(code):
    row = _conn().execute('SELECT total_shares FROM fin_info WHERE code=?', (code,)).fetchone()
    return {'total_shares': row[0]} if row and row[0] else {}


def set_industry(code, em_chain, updated=None, source=''):
    with _write_lock:
        _conn().execute('INSERT OR REPLACE INTO fin_industry VALUES (?,?)', (code, em_chain or ''))
        _conn().execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                        (code, 'industry', updated or _today(), source))
        _conn().commit()


def get_industry(code):
    row = _conn().execute('SELECT em_chain FROM fin_industry WHERE code=?', (code,)).fetchone()
    return row[0] if row and row[0] else ''


def set_extra(code, kind, data, updated=None, source=''):
    with _write_lock:
        _conn().execute('INSERT OR REPLACE INTO fin_extra VALUES (?,?,?)',
                        (code, kind, json.dumps(data, ensure_ascii=False)))
        _conn().execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                        (code, f'extra:{kind}', updated or _today(), source))
        _conn().commit()


def get_extra(code, kind):
    row = _conn().execute('SELECT data FROM fin_extra WHERE code=? AND kind=?', (code, kind)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------- freshness / TTL ----------

FRESH_TTL_DAYS = 30


def fresh_updated(code, kind):
    """该 (code, kind) 最近刷新日期，无记录返回 ''"""
    row = _conn().execute('SELECT updated FROM fin_freshness WHERE code=? AND kind=?', (code, kind)).fetchone()
    return row[0] if row else ''


def is_fresh(code, kind, ttl_days=FRESH_TTL_DAYS, today=None):
    updated = fresh_updated(code, kind)
    if not updated:
        return False
    try:
        d = datetime.date.fromisoformat(updated)
    except ValueError:
        return False
    today = today or datetime.date.today()
    return (today - d).days < ttl_days


def set_fresh(code, kind, source='', updated=None):
    _conn().execute('INSERT OR REPLACE INTO fin_freshness VALUES (?,?,?,?)',
                    (code, kind, updated or _today(), source))
    _conn().commit()


def has(code, kind):
    """kind 是否有数据（不看TTL）。kind: reports/pershare/bonus/info/industry/extra:xxx"""
    conn = _conn()
    if kind == 'reports':
        return conn.execute('SELECT 1 FROM fin_reports WHERE code=? LIMIT 1', (code,)).fetchone() is not None
    if kind == 'pershare':
        return conn.execute('SELECT 1 FROM fin_pershare WHERE code=? LIMIT 1', (code,)).fetchone() is not None
    if kind == 'bonus':
        return conn.execute('SELECT 1 FROM fin_bonus WHERE code=? LIMIT 1', (code,)).fetchone() is not None
    if kind == 'info':
        return conn.execute('SELECT 1 FROM fin_info WHERE code=?', (code,)).fetchone() is not None
    if kind == 'industry':
        return conn.execute('SELECT 1 FROM fin_industry WHERE code=?', (code,)).fetchone() is not None
    if kind.startswith('extra:'):
        return get_extra(code, kind[6:]) is not None
    return False


def codes_with(kind):
    """有该 kind 数据的全部代码（TTL 不管）"""
    conn = _conn()
    table = {'reports': 'fin_reports', 'pershare': 'fin_pershare', 'bonus': 'fin_bonus',
             'info': 'fin_info', 'industry': 'fin_industry'}.get(kind)
    if table:
        return [r[0] for r in conn.execute(f'SELECT DISTINCT code FROM {table}')]
    if kind.startswith('extra:'):
        return [r[0] for r in conn.execute('SELECT code FROM fin_extra WHERE kind=?', (kind[6:],))]
    return []


# ---------- fin_ledger（批量任务断点账本） ----------

def ledger_mark(task, status, error=None):
    """done/failed/pending。failed 时 attempts+1"""
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _conn()
    with _write_lock:
        conn.execute(
            'INSERT INTO fin_ledger VALUES (?,?,1,?,?) '
            'ON CONFLICT(task) DO UPDATE SET '
            'status=excluded.status,'
            'attempts=CASE WHEN excluded.status=\'failed\' THEN fin_ledger.attempts+1 ELSE fin_ledger.attempts END,'
            'error=excluded.error, updated_at=excluded.updated_at',
            (task, status, error, now))
        conn.commit()


def ledger_done_tasks():
    return {r[0] for r in _conn().execute("SELECT task FROM fin_ledger WHERE status='done'")}


def ledger_stats():
    return {f'{status}|{kind}': n for status, kind, n in _conn().execute(
        "SELECT status, substr(task,1,instr(task,':')-1), COUNT(*) FROM fin_ledger "
        "WHERE instr(task,':')>0 GROUP BY status, substr(task,1,instr(task,':')-1)")}


def ledger_failures(limit=100):
    return list(_conn().execute(
        "SELECT task, attempts, error FROM fin_ledger WHERE status='failed' ORDER BY task LIMIT ?", (limit,)))


# ---------- 存量JSON迁移 ----------

def migrate_json(verbose=True):
    """artifacts/.cache/financial/{code}_{kind}.json 导入库。
    reports/pershare20/bonus_v2 → 专表；info/industry_v1/industry/rd → info/extra。
    kind 本身可含下划线（bonus_v2/industry_v1），按已知 kind 后缀最长优先解析。
    迁移记录 freshness.updated=文件mtime（保留原30天窗口语义）。
    返回 (imported, skipped, errors)"""
    imported, skipped, errors = 0, 0, []
    if not os.path.isdir(LEGACY_FIN_DIR):
        return imported, skipped, errors
    known = ('pershare20', 'industry_v1', 'bonus_v2', 'reports', 'industry', 'info', 'rd')
    for fn in sorted(os.listdir(LEGACY_FIN_DIR)):
        if not fn.endswith('.json'):
            skipped += 1
            continue
        stem = fn[:-5]
        kind = next((k for k in known if stem.endswith('_' + k)), None)
        if kind is None:
            skipped += 1
            continue
        code = stem[:-len(kind) - 1]
        if not (code.isdigit() and len(code) == 6):
            skipped += 1
            continue
        path = os.path.join(LEGACY_FIN_DIR, fn)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            mtime = datetime.date.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d')
            if kind == 'reports' and data:
                replace_reports(code, data, updated=mtime, source='migrate')
            elif kind == 'pershare20' and data:
                replace_pershare(code, data, updated=mtime, source='migrate')
            elif kind == 'bonus_v2' and data is not None:
                replace_bonus(code, data, updated=mtime, source='migrate')
            elif kind == 'info' and data:
                set_info(code, data.get('total_shares'), updated=mtime, source='migrate')
                # 旧 info 内含营收/净利/毛利率等派生字段，落 extra 以便回溯
                set_extra(code, 'info_legacy', data, updated=mtime, source='migrate')
            elif kind == 'industry_v1' and data:
                set_industry(code, data, updated=mtime, source='migrate')   # 旧缓存只存末段
            elif kind == 'industry' and data:
                set_industry(code, data, updated=mtime, source='migrate')   # model_classifier 三级链
            elif kind == 'rd' and data:
                set_extra(code, 'rd', data, updated=mtime, source='migrate')
            else:
                skipped += 1
                continue
            imported += 1
            if verbose and imported % 200 == 0:
                print(f'  迁移进度 {imported}...', flush=True)
        except Exception as e:
            errors.append((fn, str(e)[:120]))
    return imported, skipped, errors
