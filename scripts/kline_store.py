#!/usr/bin/env python3
"""
K线SQLite存储层——全市场数据的统一存储（替代按股票×类型的JSON文件）

表结构:
  kline(ktype, code, date, open, close, high, low, volume)  行序与腾讯API一致: [date, open, close, high, low, volume]
  meta(ktype, code, exchange, name, pe, pb, price, qt_date, volume, updated)  对应原JSON缓存的元数据
  stock_basic(code, name, market, list_date, status, excluded, excluded_reason, updated)  全A基础信息（只拉一次）
  fetch_ledger(code, ktype, status, rows, last_date, attempts, error, updated_at)          断点续传账本

ktype 与原 JSON 后缀的对应: _kline→kline, _klineh→klineh, _kline20→kline20, _kline20h→kline20h,
  _raw_kline→raw_kline, _raw_kline20→raw_kline20, _index_kline→index_kline

并发: WAL 模式 + 全局写锁（读者不被写者阻塞；写者串行化避免 busy）
"""
import json
import os
import sqlite3
import threading

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
CACHE_DIR = os.path.join(_SKILL_DIR, 'artifacts', '.cache')
DB_PATH = os.path.join(CACHE_DIR, 'kline_store.db')

# 与 kline_cache 的后缀命名保持一致（含前导下划线）
SUFFIX_TO_KTYPE = {
    '_kline': 'kline', '_klineh': 'klineh', '_kline20': 'kline20', '_kline20h': 'kline20h',
    '_raw_kline': 'raw_kline', '_raw_kline20': 'raw_kline20', '_index_kline': 'index_kline',
    # 总收益口径（raw + 分红送转事件重建，见 build_total_return.py）
    '_kline20r': 'kline20r', '_kliner': 'kliner',
}
KTYPE_TO_SUFFIX = {v: k for k, v in SUFFIX_TO_KTYPE.items()}

_local = threading.local()
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kline (
  ktype TEXT NOT NULL, code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, close REAL, high REAL, low REAL, volume REAL,
  PRIMARY KEY (ktype, code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
  ktype TEXT NOT NULL, code TEXT NOT NULL,
  exchange TEXT, name TEXT, pe REAL, pb REAL, price REAL,
  qt_date TEXT, volume REAL, updated TEXT,
  PRIMARY KEY (ktype, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stock_basic (
  code TEXT PRIMARY KEY, name TEXT, market TEXT, list_date TEXT,
  status TEXT, excluded INTEGER DEFAULT 0, excluded_reason TEXT, updated TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetch_ledger (
  code TEXT NOT NULL, ktype TEXT NOT NULL,
  status TEXT, rows INTEGER, last_date TEXT, attempts INTEGER DEFAULT 0,
  error TEXT, updated_at TEXT,
  PRIMARY KEY (code, ktype)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS model_classify (
  code TEXT PRIMARY KEY,
  name TEXT, model TEXT NOT NULL, confidence TEXT, reasons TEXT,
  source TEXT DEFAULT 'ai', needs_review INTEGER DEFAULT 0, review_done INTEGER DEFAULT 0,
  review_note TEXT DEFAULT '', rule_model TEXT DEFAULT '',
  industry TEXT, business TEXT, holder TEXT, list_date TEXT, inputs_hash TEXT,
  batch_id TEXT, ai_engine TEXT, prompt_version TEXT,
  classified_at TEXT, updated TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS classify_batch (
  batch_id TEXT PRIMARY KEY,
  status TEXT, codes TEXT, n INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
  error TEXT, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  started_at TEXT, finished_at TEXT
) WITHOUT ROWID;
-- 全市场算分因子输入（score_factors.py 维护；供全市场扫描离线打分与规则交叉校验用）
CREATE TABLE IF NOT EXISTS score_factors (
  code TEXT PRIMARY KEY,
  n_reports INTEGER, latest_report_date TEXT,
  avg_roe REAL, avg_gross_margin REAL, gross_margin_stability REAL,
  revenue_growth_5y REAL, latest_revenue_yoy REAL, latest_profit_yoy REAL,
  roe_trend REAL, rd_ratio REAL, dps_ttm REAL, div_yield REAL,
  updated TEXT
) WITHOUT ROWID;
"""


def _conn():
    """线程本地连接（WAL、busy_timeout 30s）"""
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


# ---------- kline + meta 读写（对外形状与 kline_cache.load_cache/save_cache 一致） ----------

def load(ktype, code):
    """返回与原 JSON cache 相同结构的 dict，或 None。
    data 行 = [date, open, close, high, low, volume]（数值为 float，与原JSON字符串值 float() 后等值）"""
    conn = _conn()
    row = conn.execute('SELECT exchange, name, pe, pb, price, qt_date, volume, updated '
                       'FROM meta WHERE ktype=? AND code=?', (ktype, code)).fetchone()
    if row is None:
        return None
    exchange, name, pe, pb, price, qt_date, volume, updated = row
    data = [list(r) for r in conn.execute(
        'SELECT date, open, close, high, low, volume FROM kline WHERE ktype=? AND code=? ORDER BY date',
        (ktype, code))]
    return {
        'code': code, 'exchange': exchange or '', 'name': name or '',
        'updated': updated or '', 'last_date': data[-1][0] if data else '',
        'pe': pe or 0, 'pb': pb or 0, 'price': price or 0,
        'qt_date': qt_date or '', 'volume': volume or 0, 'data': data,
    }


def save(ktype, code, exchange, kline_data, pe=0, pb=0, price=0, name='', qt_date='', volume=0,
         updated=None, ledger_status=None, ledger_rows=None, ledger_last_date=None, ledger_error=None):
    """整股覆写（事务内 DELETE+INSERT+meta[+ledger]）。kline_data 行: [date, open, close, high, low, volume]"""
    import datetime
    updated = updated or datetime.date.today().strftime('%Y-%m-%d')
    last_date = kline_data[-1][0] if kline_data else ''
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM kline WHERE ktype=? AND code=?', (ktype, code))
            conn.executemany(
                'INSERT OR REPLACE INTO kline VALUES (?,?,?,?,?,?,?,?)',
                [(ktype, code, r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                 for r in kline_data])
            conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (ktype, code, exchange, name, pe, pb, price, qt_date, volume, updated))
            if ledger_status is not None:
                conn.execute(
                    'INSERT INTO fetch_ledger(code,ktype,status,rows,last_date,attempts,error,updated_at) '
                    'VALUES (?,?,?,?,?,'
                    'COALESCE((SELECT attempts FROM fetch_ledger WHERE code=? AND ktype=?),0)+0,'
                    '?,?) '
                    'ON CONFLICT(code,ktype) DO UPDATE SET status=excluded.status, rows=excluded.rows, '
                    'last_date=excluded.last_date, error=excluded.error, updated_at=excluded.updated_at',
                    (code, ktype, ledger_status,
                     ledger_rows if ledger_rows is not None else len(kline_data),
                     ledger_last_date if ledger_last_date is not None else last_date,
                     code, ktype, ledger_error, updated))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return last_date


def has_meta(ktype, code):
    return _conn().execute('SELECT 1 FROM meta WHERE ktype=? AND code=?', (ktype, code)).fetchone() is not None


def all_codes_with(ktype):
    return [r[0] for r in _conn().execute('SELECT code FROM meta WHERE ktype=?', (ktype,))]


# ---------- fetch_ledger ----------

def ledger_mark(code, ktype, status, rows=None, last_date=None, error=None):
    """done/failed/pending。failed 时 attempts+1"""
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _conn()
    with _write_lock:
        conn.execute(
            'INSERT INTO fetch_ledger(code,ktype,status,rows,last_date,attempts,error,updated_at) '
            'VALUES (?,?,?,?,?,1,?,?) '
            'ON CONFLICT(code,ktype) DO UPDATE SET '
            'status=excluded.status,'
            'rows=COALESCE(excluded.rows,fetch_ledger.rows),'
            'last_date=COALESCE(excluded.last_date,fetch_ledger.last_date),'
            'attempts=CASE WHEN excluded.status=\'failed\' THEN fetch_ledger.attempts+1 ELSE fetch_ledger.attempts END,'
            'error=excluded.error, updated_at=excluded.updated_at',
            (code, ktype, status, rows, last_date, error, now))
        conn.commit()


def ledger_pending(ktypes, max_attempts=None):
    """待抓取列表: 无记录的（默认待办）+ failed 且 attempts 未超上限的。返回 [(code, ktype)]"""
    conn = _conn()
    done = {(r[0], r[1]) for r in conn.execute(
        "SELECT code, ktype FROM fetch_ledger WHERE status='done'")}
    failed = {}
    if max_attempts is not None:
        failed = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT code, ktype, attempts FROM fetch_ledger WHERE status='failed'")}
    tasks = []
    for kt in ktypes:
        for code in [r[0] for r in conn.execute(
                'SELECT code FROM stock_basic WHERE excluded=0 ORDER BY code')]:
            if (code, kt) in done:
                continue
            if (code, kt) in failed and failed[(code, kt)] >= max_attempts:
                continue
            tasks.append((code, kt))
    return tasks


def ledger_failures():
    return list(_conn().execute(
        "SELECT code, ktype, attempts, error FROM fetch_ledger WHERE status='failed' ORDER BY code"))


def ledger_stats():
    return {f'{status}|{kt}': n for status, kt, n in _conn().execute(
        'SELECT status, ktype, COUNT(*) FROM fetch_ledger GROUP BY status, ktype')}


# ---------- stock_basic ----------

def stock_basic_replace(rows, updated):
    conn = _conn()
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM stock_basic')
            conn.executemany('INSERT OR REPLACE INTO stock_basic VALUES (?,?,?,?,?,?,?,?)',
                             [(r['code'], r['name'], r['market'], r['list_date'], r['status'],
                               r.get('excluded', 0), r.get('excluded_reason', ''), updated) for r in rows])
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def stock_basic_all(excluded=False):
    cond = '' if excluded else ' WHERE excluded=0'
    return [dict(zip(('code', 'name', 'market', 'list_date', 'status', 'excluded', 'excluded_reason'), r))
            for r in _conn().execute(
                'SELECT code, name, market, list_date, status, excluded, excluded_reason FROM stock_basic' + cond + ' ORDER BY code')]


def stock_basic_count():
    return _conn().execute('SELECT COUNT(*) FROM stock_basic').fetchone()[0]


def exclude_codes(codes, reason):
    """证据式排除（如窗口内无数据的退市遗留代码），不覆盖已有排除原因"""
    conn = _conn()
    with _write_lock:
        conn.executemany(
            'UPDATE stock_basic SET excluded=1, '
            'excluded_reason=CASE WHEN excluded_reason=\'\' THEN ? ELSE excluded_reason END '
            'WHERE code=? AND excluded=0',
            [(reason, c) for c in codes])
        conn.commit()


# ---------- 存量JSON迁移与校验 ----------

def parse_suffix(stem):
    """JSON 文件名主干 → (code, ktype)；最长后缀优先（_raw_kline20/_raw_kline/_index_kline
    必须先于 _kline20/_kline 匹配，否则会被短后缀截断出 '000338_raw' 这类假代码）"""
    for suf in sorted(SUFFIX_TO_KTYPE, key=len, reverse=True):
        if stem.endswith(suf):
            code = stem[:-len(suf)]
            if code.isdigit() and len(code) == 6:
                return code, SUFFIX_TO_KTYPE[suf]
    return None, None


def migrate_json(verbose=True):
    """把 artifacts/.cache/*[后缀].json 全部导入DB。返回 (imported, skipped, errors)"""
    imported, skipped, errors = 0, 0, []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.endswith('.json'):
            continue
        code, ktype = parse_suffix(fn[:-5])
        if ktype is None:
            skipped += 1
            continue
        try:
            with open(os.path.join(CACHE_DIR, fn), encoding='utf-8') as f:
                cache = json.load(f)
            data = cache.get('data') or []
            if not data:
                skipped += 1
                continue
            save(ktype, code, cache.get('exchange', ''), data,
                 cache.get('pe', 0), cache.get('pb', 0), cache.get('price', 0),
                 cache.get('name', ''), qt_date=cache.get('qt_date', ''),
                 volume=cache.get('volume', 0), updated=cache.get('updated'))
            imported += 1
            if verbose and imported % 50 == 0:
                print(f'  迁移进度 {imported}...')
        except Exception as e:
            errors.append((fn, str(e)))
    return imported, skipped, errors


def _rows_equal_json(db_data, json_data):
    """数值等值比较（JSON存字符串、DB存float，float()后必须完全一致）"""
    if len(db_data) != len(json_data):
        return False, f'行数 {len(db_data)} != {len(json_data)}'
    for rdb, rjs in zip(db_data, json_data):
        if rdb[0] != rjs[0]:
            return False, f'日期 {rdb[0]} != {rjs[0]}'
        for i in (1, 2, 3, 4, 5):
            if float(rjs[i]) != rdb[i]:
                return False, f'{rdb[0]} 列{i}: {rdb[i]} != {rjs[i]}'
    return True, ''


def verify_migration():
    """逐文件校验 DB 与 JSON 数值完全一致。返回 (checked, ok, mismatches)"""
    checked, ok, mismatches = 0, 0, []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.endswith('.json'):
            continue
        code, ktype = parse_suffix(fn[:-5])
        if ktype is None:
            continue
        try:
            with open(os.path.join(CACHE_DIR, fn), encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            continue
        data = cache.get('data') or []
        if not data:
            continue
        checked += 1
        db = load(ktype, code)
        if db is None:
            mismatches.append((fn, 'DB缺失'))
            continue
        eq, msg = _rows_equal_json(db['data'], data)
        if not eq:
            mismatches.append((fn, msg))
        else:
            ok += 1
    return checked, ok, mismatches


# ---------- 10年切片派生 ----------

def slice_last_years(rows, years, today):
    import datetime
    boundary = (today - datetime.timedelta(days=years * 365)).isoformat()
    return [r for r in rows if r[0] >= boundary]


def derive_10y_for(code, today=None):
    """由 20年序列切片生成 10年 hfq/raw 缓存（幂等）。返回 derived ktype 列表"""
    import datetime
    if today is None:
        today = datetime.date.today()
    elif isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    derived = []
    for src, dst in (('kline20h', 'klineh'), ('raw_kline20', 'raw_kline')):
        src_cache = load(src, code)
        if src_cache is None or not src_cache['data']:
            continue
        sl = slice_last_years(src_cache['data'], 10, today)
        if not sl:
            continue
        save(dst, code, src_cache['exchange'], sl,
             src_cache['pe'], src_cache['pb'], src_cache['price'], src_cache['name'],
             qt_date=src_cache['qt_date'], volume=src_cache['volume'], updated=today.strftime('%Y-%m-%d'))
        derived.append(dst)
    return derived


# ---------- model_classify（AI模型分类结果，由 ai_model_classifier.py 读写） ----------

_MC_COLS = ('code', 'name', 'model', 'confidence', 'reasons', 'source', 'needs_review',
            'review_done', 'review_note', 'rule_model', 'industry', 'business', 'holder',
            'list_date', 'inputs_hash', 'batch_id', 'ai_engine', 'prompt_version',
            'classified_at', 'updated')
_MC_DEFAULTS = {'code': '', 'name': '', 'model': '', 'confidence': '', 'reasons': '[]',
                'source': 'ai', 'needs_review': 0, 'review_done': 0, 'review_note': '',
                'rule_model': '', 'industry': '', 'business': '', 'holder': '', 'list_date': '',
                'inputs_hash': '', 'batch_id': '', 'ai_engine': '', 'prompt_version': '',
                'classified_at': '', 'updated': ''}


def _row_values(r):
    return tuple(r.get(c, _MC_DEFAULTS[c]) for c in _MC_COLS)


def model_classify_upsert(rows):
    """rows: dict列表（键=_MC_COLS，缺省列用默认值）。INSERT OR REPLACE 全列覆写。
    注意会覆写整行——部分更新场景需先 model_classify_get 读出合并后再写。"""
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _conn()
    with _write_lock:
        for r in rows:
            vals = dict(_MC_DEFAULTS)
            vals.update({k: v for k, v in r.items() if k in _MC_DEFAULTS})
            if not vals['updated']:
                vals['updated'] = now
            conn.execute(
                f'INSERT OR REPLACE INTO model_classify ({",".join(_MC_COLS)}) '
                f'VALUES ({",".join("?" * len(_MC_COLS))})',
                tuple(vals[c] for c in _MC_COLS))
        conn.commit()


def classify_batch_mark(batch_id, status, codes=None, n=None, error=None,
                        prompt_tokens=None, completion_tokens=None, started_at=None):
    """批级账本标记（每次调用 attempts+1；started_at 保留首次值）"""
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _conn()
    with _write_lock:
        conn.execute(
            'INSERT INTO classify_batch(batch_id,status,codes,n,attempts,error,prompt_tokens,'
            'completion_tokens,started_at,finished_at) VALUES (?,?,?,? ,1,?,?,?,?,?) '
            'ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status,'
            'codes=COALESCE(excluded.codes,classify_batch.codes),'
            'n=COALESCE(excluded.n,classify_batch.n),attempts=classify_batch.attempts+1,'
            'error=excluded.error,'
            'prompt_tokens=COALESCE(excluded.prompt_tokens,classify_batch.prompt_tokens),'
            'completion_tokens=COALESCE(excluded.completion_tokens,classify_batch.completion_tokens),'
            'started_at=COALESCE(classify_batch.started_at,excluded.started_at),'
            'finished_at=CASE WHEN excluded.status IN (\'done\',\'failed\') THEN excluded.finished_at '
            'ELSE classify_batch.finished_at END',
            (batch_id, status, json.dumps(codes) if codes else None, n, error,
             prompt_tokens, completion_tokens, started_at or now, now))
        conn.commit()


def model_classify_get(code=None):
    """code=None 返回全部 {code: row_dict}；否则单只 dict 或 None"""
    if code is not None:
        r = _conn().execute(
            f'SELECT {",".join(_MC_COLS)} FROM model_classify WHERE code=?', (code,)).fetchone()
        return dict(zip(_MC_COLS, r)) if r else None
    return {r[0]: dict(zip(_MC_COLS, r)) for r in _conn().execute(
        f'SELECT {",".join(_MC_COLS)} FROM model_classify')}


def classify_batch_all():
    cols = ('batch_id', 'status', 'codes', 'n', 'attempts', 'error', 'prompt_tokens',
            'completion_tokens', 'started_at', 'finished_at')
    return [dict(zip(cols, r)) for r in _conn().execute(
        'SELECT ' + ','.join(cols) + ' FROM classify_batch ORDER BY batch_id')]


# ---------- score_factors（全市场算分因子输入，由 score_factors.py 维护） ----------

_SF_COLS = ('code', 'n_reports', 'latest_report_date', 'avg_roe', 'avg_gross_margin',
            'gross_margin_stability', 'revenue_growth_5y', 'latest_revenue_yoy',
            'latest_profit_yoy', 'roe_trend', 'rd_ratio', 'dps_ttm', 'div_yield', 'updated')


def score_factors_upsert(rows):
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _conn()
    with _write_lock:
        for r in rows:
            vals = {c: r.get(c) for c in _SF_COLS}
            vals['updated'] = now
            conn.execute(
                f'INSERT OR REPLACE INTO score_factors ({",".join(_SF_COLS)}) '
                f'VALUES ({",".join("?" * len(_SF_COLS))})',
                tuple(vals[c] for c in _SF_COLS))
        conn.commit()


def score_factors_get(code=None):
    """code=None 返回全部 {code: row_dict}；否则单只 dict 或 None"""
    if code is not None:
        r = _conn().execute(
            f'SELECT {",".join(_SF_COLS)} FROM score_factors WHERE code=?', (code,)).fetchone()
        return dict(zip(_SF_COLS, r)) if r else None
    return {r[0]: dict(zip(_SF_COLS, r)) for r in _conn().execute(
        f'SELECT {",".join(_SF_COLS)} FROM score_factors')}


def meta_quote(ktype, code):
    """轻量读取最新报价元数据（不加载K线行），无则 None"""
    r = _conn().execute('SELECT pe, pb, price, qt_date FROM meta WHERE ktype=? AND code=?',
                        (ktype, code)).fetchone()
    if r is None:
        return None
    return {'pe': r[0], 'pb': r[1], 'price': r[2], 'qt_date': r[3]}
