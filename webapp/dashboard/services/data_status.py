"""数据管理页的状态聚合 —— 直接读既有 SQLite 库，零网络请求。

只 import kline_store / fin_store（纯 stdlib 模块），避免把 akshare 等重依赖
拉进 Web 请求路径。所有"多久没更新"的口径：
- K线: meta(kline20h).updated / last_date 与全市场最新交易日的差距
- 财务: fin_freshness(kind).updated 与 30 天 TTL
- 模型: model_classify.classified_at / classify_batch.finished_at
- 因子: score_factors.updated 与 30 天 TTL
"""
import datetime
import glob
import os

import kline_store
import fin_store

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), 'artifacts', 'reports')

FRESH_DAYS = 30


def _date(s):
    try:
        return datetime.date.fromisoformat((s or '')[:10])
    except ValueError:
        return None


def _days_ago(s):
    d = _date(s)
    if not d:
        return None
    return (datetime.date.today() - d).days


def _kline_status():
    conn = kline_store._conn()
    listed = conn.execute(
        "SELECT COUNT(*) FROM stock_basic WHERE excluded=0 AND status='listed'").fetchone()[0]
    covered = conn.execute("SELECT COUNT(*) FROM meta WHERE ktype='kline20h'").fetchone()[0]
    # 新鲜度走 fetch_ledger（批量账本含 last_date，毫秒级；kline 表 6400 万行扫描太慢）
    latest_trading_day = conn.execute(
        "SELECT MAX(last_date) FROM fetch_ledger WHERE ktype='kline20h' AND status='done'"
    ).fetchone()[0] or ''
    stale = 0
    if latest_trading_day:
        stale = conn.execute(
            "SELECT COUNT(*) FROM fetch_ledger WHERE ktype='kline20h' AND status='done' "
            "AND last_date < ?", (latest_trading_day,)).fetchone()[0]
    updated_max = conn.execute(
        "SELECT MAX(updated) FROM meta WHERE ktype='kline20h'").fetchone()[0] or ''
    ledger = kline_store.ledger_stats()
    failed = sum(v for k, v in ledger.items() if k.startswith('failed|'))
    return {
        'listed': listed,
        'covered': covered,
        'no_data': max(listed - covered, 0),
        'latest_trading_day': latest_trading_day,
        'stale': stale,
        'last_updated': updated_max,
        'last_updated_ago': _days_ago(updated_max),
        'ledger_failed': failed,
    }


def _fin_status():
    conn = fin_store._conn()
    cutoff = (datetime.date.today() - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    # 口径收敛：覆盖率按"当前上市宇宙"统计（fin 库含大量历史退市代码，直接计数会大于上市家数）
    listed_set = {r[0] for r in kline_store._conn().execute(
        "SELECT code FROM stock_basic WHERE excluded=0 AND status='listed'")}
    out = {}
    for kind, label in (('reports', '财报'), ('pershare', '每股指标'),
                        ('bonus', '分红送转'), ('info', '总股本'), ('industry', '行业链')):
        have = set(fin_store.codes_with(kind))
        have_listed = have & listed_set
        n = len(have_listed)
        fresh = sum(
            1 for c, u in conn.execute('SELECT code, updated FROM fin_freshness WHERE kind=?',
                                       (kind,)).fetchall()
            if c in have_listed and (u or '') >= cutoff)
        last = conn.execute('SELECT MAX(updated) FROM fin_freshness WHERE kind=?',
                            (kind,)).fetchone()[0] or ''
        out[kind] = {'label': label, 'covered': n, 'fresh': fresh, 'last': last,
                     'last_ago': _days_ago(last)}
    reports_n = conn.execute('SELECT COUNT(DISTINCT code) FROM fin_reports').fetchone()[0]
    failures = len(fin_store.ledger_failures(10 ** 9))
    ledger = fin_store.ledger_stats()
    yjbb_done = sum(v for k, v in ledger.items() if k.startswith('done|yjbb'))
    yjbb_failed = sum(v for k, v in ledger.items() if k.startswith('failed|yjbb'))
    out['reports']['have_data'] = reports_n
    out['yjbb_done'] = yjbb_done
    out['yjbb_failed'] = yjbb_failed
    out['ledger_failed'] = failures
    out['listed'] = len(listed_set)
    return out


def _classify_status():
    conn = kline_store._conn()
    n = conn.execute('SELECT COUNT(*) FROM model_classify').fetchone()[0]
    needs_review = conn.execute(
        'SELECT COUNT(*) FROM model_classify WHERE needs_review=1 AND review_done=0').fetchone()[0]
    last = conn.execute('SELECT MAX(classified_at) FROM model_classify').fetchone()[0] or ''
    batch = conn.execute(
        "SELECT batch_id, status, finished_at FROM classify_batch ORDER BY batch_id DESC LIMIT 1").fetchone()
    return {
        'covered': n,
        'needs_review': needs_review,
        'last_classified': last,
        'last_ago': _days_ago(last),
        'last_batch': {'id': batch[0], 'status': batch[1], 'finished_at': batch[2]} if batch else None,
    }


def _factors_status():
    conn = kline_store._conn()
    n = conn.execute('SELECT COUNT(*) FROM score_factors').fetchone()[0]
    last = conn.execute('SELECT MAX(updated) FROM score_factors').fetchone()[0] or ''
    cutoff = (datetime.date.today() - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    stale = conn.execute('SELECT COUNT(*) FROM score_factors WHERE updated < ?',
                         (cutoff,)).fetchone()[0]
    return {'covered': n, 'last': last, 'last_ago': _days_ago(last), 'stale': stale}


def _reports_status():
    files = glob.glob(os.path.join(REPORTS_DIR, '*-valuation.html'))
    latest_mtime = max((os.path.getmtime(f) for f in files), default=0)
    latest = (datetime.datetime.fromtimestamp(latest_mtime).strftime('%Y-%m-%d %H:%M')
              if latest_mtime else '')
    ago = ((datetime.datetime.now() - datetime.datetime.fromtimestamp(latest_mtime)).days
           if latest_mtime else None)
    return {'count': len(files), 'latest': latest, 'latest_ago_days': ago}


def _boards_status():
    conn = kline_store._conn()
    covered = conn.execute('SELECT COUNT(DISTINCT code) FROM stock_board').fetchone()[0]
    relations = conn.execute('SELECT COUNT(*) FROM stock_board').fetchone()[0]
    index_boards = conn.execute(
        "SELECT COUNT(DISTINCT board_code) FROM stock_board WHERE board_type='index'").fetchone()[0]
    last = conn.execute('SELECT MAX(updated) FROM stock_board').fetchone()[0] or ''
    listed = conn.execute(
        "SELECT COUNT(*) FROM stock_basic WHERE excluded=0 AND status='listed'").fetchone()[0]
    return {'covered': covered, 'relations': relations, 'index_boards': index_boards,
            'listed': listed, 'last': last, 'last_ago': _days_ago(last)}


def status_json():
    return {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'kline': _kline_status(),
        'finance': _fin_status(),
        'classify': _classify_status(),
        'factors': _factors_status(),
        'boards': _boards_status(),
        'reports': _reports_status(),
    }
