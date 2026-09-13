"""股票级操作：搜索 / 单股数据更新 / 分数读取。

评分的唯一数据源 = score_market.py 产出的 score_store.db
（score_series 日度序列 + stock_score 当前状态，判定口径 = 当前分在自身
历史序列中的分位）。旧的 watchlist 扫描快照 / 零散报告 JSON 不参与面板。
网络与重活全部发生在后台任务线程（services.jobs），本模块函数只被 worker
调用或作为轻量查询被视图调用。
"""
import datetime
import glob
import json
import os
import unicodedata

import kline_store
import fin_store

from .jobs import JobCancelled

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
SCRIPTS_DIR = os.path.join(BACKEND_DIR, 'scripts')
REPORTS_DIR = os.path.join(BACKEND_DIR, 'artifacts', 'reports')
JSON_DATA_DIR = os.path.join(BACKEND_DIR, 'artifacts', 'json_data')
WATCHLIST_PATH = os.path.join(BACKEND_DIR, 'watchlist.txt')

MODEL_CHOICES = ['staples', 'discretionary', 'tech', 'cyclical',
                 'soe', 'bank', 'realestate', 'pharma', 'growth']

# 模型中文名枚举（唯一来源：scoring_engine.MODEL_PRESETS[].name；growth 无 preset，按周期回退）
MODEL_LABELS = {
    'staples': '必选消费',
    'discretionary': '可选消费',
    'tech': '科技制造',
    'cyclical': '周期资源',
    'soe': '央企基建',
    'bank': '银行保险',
    'realestate': '地产',
    'pharma': '医药消费',
    'growth': '成长(按周期算)',
}


def model_label(model):
    return MODEL_LABELS.get(model, model or '')


def normalize_model(raw):
    """中英文均可接受 → 英文值；未知返回 None"""
    if raw in (None, ''):
        return None
    raw = str(raw).strip()
    if raw in MODEL_CHOICES:
        return raw
    for en, cn in MODEL_LABELS.items():
        if raw == cn:
            return en
    return None


# ---------- watchlist / 名称 / 模型 ----------

def load_watchlist():
    """watchlist.txt (CSV: 名称,代码,模型,最后报告时间) → {code: {'name','model'}}"""
    out = {}
    if not os.path.exists(WATCHLIST_PATH):
        return out
    with open(WATCHLIST_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('名称'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3 and parts[1].isdigit():
                out[parts[1]] = {'name': parts[0], 'model': parts[2]}
    return out


def resolve_model(code):
    """股票 → (model, source)。watchlist 优先，其次 AI 分类，未知返回 ('','')"""
    wl = load_watchlist().get(code)
    if wl:
        return wl['model'], 'watchlist'
    mc = kline_store.model_classify_get(code)
    if mc and mc.get('model'):
        return mc['model'], mc.get('source') or 'ai'
    return '', ''


def exchange_of(code):
    return 'sh' if code.startswith('6') else 'sz'


def stock_name(code, wl=None, names=None):
    wl = wl if wl is not None else load_watchlist()
    if code in wl:
        return wl[code]['name']
    if names is None:
        row = kline_store._conn().execute(
            'SELECT name FROM stock_basic WHERE code=?', (code,)).fetchone()
        names = {code: row[0] if row else ''}
    name = names.get(code) or ''
    if not name:
        mc = kline_store.model_classify_get(code)
        name = (mc or {}).get('name') or ''
    return unicodedata.normalize('NFKC', name).strip() or code


# ---------- 分数读取（score_store.db，评分器产出） ----------

_SCORE_COLS = ('code', 'name', 'model', 'score', 'score_pct', 'price', 'pe', 'pb',
               'pe_min', 'pe_max', 'pb_min', 'pb_max', 'n_days', 'last_date', 'computed_at')

# 分数序列落后最新交易日超过该天数 → 视为退市/长期停牌遗留，面板始终隐藏；
# 60天内视为停牌（当天临时停牌/涨跌停无成交等），正常显示
DELISTED_LAG_DAYS = 60


def all_stock_scores():
    """分数面板全量行（stock_score 全表），按分数降序。status/signal 由分数映射。
    delisted = 序列落后最新交易日超过 DELISTED_LAG_DAYS（退市/长期停牌遗留），面板始终隐藏。"""
    import score_market
    score_market.init_db()
    from scan_watchlist import get_status, get_signal
    conn = score_market._conn()
    wl = load_watchlist()
    names = {r[0]: r[1] for r in kline_store._conn().execute(
        'SELECT code, name FROM stock_basic')}
    latest = kline_store._conn().execute(
        "SELECT MAX(last_date) FROM fetch_ledger WHERE ktype='kline20h' AND status='done'"
    ).fetchone()[0] or ''
    latest_d = datetime.date.fromisoformat(latest[:10]) if latest else None
    rows = []
    for r in conn.execute(f'SELECT {",".join(_SCORE_COLS)} FROM stock_score'):
        d = dict(zip(_SCORE_COLS, r))
        code = d['code']
        d['name'] = unicodedata.normalize('NFKC', names.get(code) or d['name'] or code).strip()
        d['in_watchlist'] = code in wl
        d['model_label'] = model_label(d['model'])
        d['status'] = get_status(d['score']) if d['score'] is not None else ''
        d['signal'] = get_signal(d['score']) if d['score'] is not None else ''
        delisted = False
        if latest_d and d['last_date']:
            try:
                lag = (latest_d - datetime.date.fromisoformat(d['last_date'][:10])).days
                delisted = lag > DELISTED_LAG_DAYS
            except ValueError:
                delisted = True
        d['delisted'] = delisted
        rows.append(d)
    rows.sort(key=lambda x: -(x['score'] or 0))
    return rows, latest



# ---------- 搜索 ----------

def search_stocks(q, limit=20):
    """代码前缀 / 名称子串匹配（不区分大小写）。结果附数据新鲜度与分数。
    watchlist 命中排最前，其余按代码升序。"""
    qs = unicodedata.normalize('NFKC', (q or '').strip())
    if not qs:
        return []
    conn = kline_store._conn()
    like = f'%{qs}%'
    rows = conn.execute(
        'SELECT code, name FROM stock_basic WHERE excluded=0 AND (code LIKE ? OR name LIKE ?) '
        'ORDER BY code LIMIT 60', (f'{qs}%', like)).fetchall()
    wl = load_watchlist()
    import score_market
    score_market.init_db()
    score_conn = score_market._conn()
    out = []
    for code, name in rows:
        name_n = unicodedata.normalize('NFKC', name or '')
        out.append(_enrich(score_conn, wl, code, name_n))
    out.sort(key=lambda r: (0 if r['in_watchlist'] else 1, r['code']))
    return out[:limit]


def _enrich(score_conn, wl, code, name):
    model, source = resolve_model(code)
    last_date = _kline_last_date(code)
    fin_updated = fin_store.fresh_updated(code, 'reports')
    r = score_conn.execute(
        'SELECT score, score_pct, last_date FROM stock_score WHERE code=?', (code,)).fetchone()
    return {
        'code': code, 'name': name or code, 'model': model, 'model_source': source,
        'in_watchlist': code in wl,
        'kline_last_date': last_date,
        'fin_reports_updated': fin_updated,
        'score': r[0] if r else None,
        'score_pct': r[1] if r else None,
        'scored_last_date': r[2] if r else '',
    }


# ---------- 报告文件定位（报告浏览页仍基于自包含HTML） ----------

def find_report_file(code):
    files = glob.glob(os.path.join(REPORTS_DIR, f'*{code}-valuation.html'))
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    return {
        'path': path,
        'filename': os.path.basename(path),
        'mtime': datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M'),
        'size_kb': round(os.path.getsize(path) / 1024),
    }


def find_meta_json(code):
    files = glob.glob(os.path.join(JSON_DATA_DIR, f'*{code}-valuation.json'))
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------- 单股数据更新（函数任务） ----------
# 脚本函数的 print 已由执行器统一重定向到任务日志（jobs._JobWriter）

def _kline_last_date(code):
    conn = kline_store._conn()
    for kt in ('klineh', 'kline20r', 'kline20h'):
        row = conn.execute('SELECT MAX(date) FROM kline WHERE ktype=? AND code=?',
                           (kt, code)).fetchone()
        if row and row[0]:
            return row[0]
    return ''


def update_single_stock(proxy, code):
    """单股全量刷新：K线 → 财务(强制) → 因子 → 评分(重算自身序列) → 报告重建(如有模型)"""
    from kline_cache import get_kline, get_kline_raw
    import financial_fetcher as ff

    code = str(code).strip()
    if not (code.isdigit() and len(code) == 6):
        raise ValueError(f'股票代码格式不正确: {code}')
    ex = exchange_of(code)
    name = stock_name(code)
    model, model_source = resolve_model(code)
    proxy.log(f'== 单股更新 {name}({code}) 模型={model or "未指定"} ==')

    steps = 6
    # 1. K线（先不复权后复权：总收益口径重建依赖最新 raw 序列）
    proxy.progress(1, steps, '更新K线')
    proxy.log('[1/6] 更新不复权K线（20年，增量）...')
    raw = get_kline_raw(code, ex, years=20)
    proxy.log(f'  raw_kline20: {len(raw)} 天, 最后日期 {raw[-1][0] if raw else "无"}')
    proxy.log('[1/6] 更新复权K线（20年后复权/总收益口径，增量）...')
    hk = get_kline(code, ex, fq='hfq', years=20)
    proxy.log(f'  kline20h/kline20r: {len(hk["kline"])} 天, 最后日期 '
              f'{hk["kline"][-1][0] if hk["kline"] else "无"}')
    # fetch_ledger 与批量口径对齐（数据管理页的新鲜度统计走账本）
    today = datetime.date.today().strftime('%Y-%m-%d')
    if raw:
        kline_store.ledger_mark(code, 'raw_kline20', 'done', rows=len(raw), last_date=raw[-1][0])
    if hk.get('kline'):
        kline_store.ledger_mark(code, 'kline20h', 'done', rows=len(hk['kline']),
                                last_date=hk['kline'][-1][0])
    kline_store._conn().execute(
        "UPDATE meta SET updated=? WHERE ktype IN ('kline20h','raw_kline20') AND code=?",
        (today, code))
    kline_store._conn().commit()
    if proxy.cancelled():
        raise JobCancelled()
    proxy.log(f'  现价 {hk.get("price")} PE {hk.get("pe")} PB {hk.get("pb")} '
              f'(行情日期 {hk.get("qt_date", "")})')

    # 2. 财务（删除 freshness 强制按 TTL 重取）
    proxy.progress(2, steps, '更新财务数据')
    proxy.log('[2/6] 强制刷新财务数据（财报/每股/分红/股本/行业）...')
    conn = fin_store._conn()
    with fin_store._write_lock:
        conn.execute('DELETE FROM fin_freshness WHERE code=?', (code,))
        conn.commit()
    reports = ff.fetch_financial_reports(code, ex)
    proxy.log(f'  财报: {len(reports)} 期 (最新 {reports[0]["report_date"] if reports else "无"})')
    pershare = ff.fetch_pershare_data(code, ex)
    proxy.log(f'  每股指标: {len(pershare)} 年')
    bonus = ff.fetch_bonus_events(code, ex)
    proxy.log(f'  分红送转事件: {len(bonus)} 次')
    info = ff.fetch_stock_info(code, ex)
    proxy.log(f'  总股本 {info.get("total_shares", "-")} 行业 {info.get("industry", "-")}')
    if not info.get('industry'):
        ind = ff.fetch_industry(code, ex)
        if ind:
            proxy.log(f'  行业补拉: {ind}')
    if proxy.cancelled():
        raise JobCancelled()

    # 3. 因子行
    proxy.progress(3, steps, '回填算分因子')
    proxy.log('[3/6] 回填 score_factors 因子行...')
    import score_factors as sf
    row = sf.compute_row(code, ex, offline=False)
    if row is None:
        proxy.log('  无财报数据，跳过因子行')
    else:
        kline_store.score_factors_upsert([row])
        proxy.log(f'  avg_roe={row.get("avg_roe")} 毛利率={row.get("avg_gross_margin")} '
                  f'股息率={row.get("div_yield")}')
    if proxy.cancelled():
        raise JobCancelled()

    # 4. 评分（重算该股自身序列 + 历史分位，score_store.db）
    proxy.progress(4, steps, '重算评分序列')
    proxy.log('[4/6] 重算评分序列（score_market）...')
    import score_market
    score_market.init_db()
    done, _skipped, failed = score_market.score_stocks(codes=[code], force=True)
    row = score_market._conn().execute(
        'SELECT score, score_pct, n_days, last_date FROM stock_score WHERE code=?',
        (code,)).fetchone()
    if row:
        proxy.log(f'  当前分 {row[0]} | 自身历史分位 {row[1]}% | 序列 {row[2]} 天至 {row[3]}')
    elif failed:
        proxy.log('  评分失败（数据不足，详见上方日志）')
    if proxy.cancelled():
        raise JobCancelled()

    # 5. 报告重建（有模型才跑）
    if model:
        proxy.progress(5, steps, '重建估值报告')
        proxy.log(f'[5/6] 重建估值报告 (build_report {code} --model {model})...')
        from .jobs import run_subprocess_logged
        try:
            run_subprocess_logged(proxy, ['build_report.py', code, '--model', model],
                                  _indent='  ')
            rep = find_report_file(code)
            proxy.log(f'  报告已生成: {rep["filename"]}' if rep else '  报告文件未找到')
        except JobCancelled:
            raise
        except RuntimeError as e:
            proxy.log(f'  [警告] 报告生成失败({e})，评分与数据已更新，可稍后在报告页单独重试')
    else:
        proxy.progress(5, steps, '跳过报告重建（无模型归属）')
        proxy.log('[5/6] 无模型归属，跳过报告重建（可在报告页选择模型生成）')

    proxy.progress(6, steps, '完成')
    proxy.log('[6/6] 单股更新完成')


# ---------- 全市场K线增量（先重置落后账本再抓取） ----------

def kline_refresh(proxy):
    """全市场K线增量：先把落后于最新交易日的 done 账本重置为待抓取，再跑 --fetch。
    （fetch_all_market 的账本语义是 done 永不重跑，停牌/缺新数据的股票会一直落后）"""
    from .jobs import run_subprocess_logged

    conn = kline_store._conn()
    latest = conn.execute(
        "SELECT MAX(last_date) FROM fetch_ledger WHERE ktype='kline20h' AND status='done'"
    ).fetchone()[0]
    if latest:
        stale = conn.execute(
            "SELECT code, ktype FROM fetch_ledger WHERE status='done' AND last_date<? "
            "AND ktype IN ('kline20h','raw_kline20')", (latest,)).fetchall()
        if stale:
            with kline_store._write_lock:
                conn.executemany(
                    "UPDATE fetch_ledger SET status='pending' WHERE code=? AND ktype=?", stale)
                conn.commit()
        proxy.log(f'账本重置: {len(stale)} 条落后任务 → 待抓取 (最新交易日 {latest})')
    proxy.log('启动 fetch_all_market --fetch（断点续传）...')
    run_subprocess_logged(proxy, ['fetch_all_market.py', '--fetch'])
    proxy.log('全市场K线增量更新完成')
