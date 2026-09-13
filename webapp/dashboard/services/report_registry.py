"""报告产物登记与缓存过期判定。

产物：artifacts/reports/*{code}-valuation.html（自包含单文件）
     + artifacts/json_data/*{code}-valuation.json（数据副本）
generated_at 取自 HTML 的 mtime（构建完成即文件落盘时间），首次查询时登记进
ReportArtifact 表；重建后 mtime 变化会自动刷新记录。报告页据此决定是否按需重建。

过期判据（任一命中即 stale）：
  1. 无报告文件                      → 需生成
  2. 报告数据末日 < 该股分数序列末日   → 数据落后（主判据，最硬）
  3. generated_at 早于 TTL（默认8h）  → 时效兜底（盘中价格/分数会变）
"""
import datetime
import glob
import json
import os

from django.conf import settings
from django.utils import timezone

from ..models import ReportArtifact

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
REPORTS_DIR = os.path.join(BACKEND_DIR, 'artifacts', 'reports')
JSON_DATA_DIR = os.path.join(BACKEND_DIR, 'artifacts', 'json_data')

# JSON 摘要缓存: code -> (json mtime, {'data_last_date','model_type'})
_JSON_CACHE = {}


def ttl_hours():
    return getattr(settings, 'REPORT_TTL_HOURS', 8)


def _latest_file(pattern):
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _json_digest(code):
    """报告 JSON 的摘要（数据末日 / 模型），按 mtime 缓存，避免每次请求解析 700KB"""
    path = _latest_file(os.path.join(JSON_DATA_DIR, f'*{code}-valuation.json'))
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _JSON_CACHE.get(code)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    data = doc.get('data') or []
    meta = doc.get('meta') or {}
    digest = {
        'data_last_date': (data[-1].get('date') if data else '') or '',
        'model_type': meta.get('model_type') or '',
        'stock': meta.get('stock') or '',
        'weights': meta.get('weights') or '',
        'latest_raw_price': meta.get('latest_raw_price'),
        'score': (data[-1].get('score') if data else None),
    }
    _JSON_CACHE[code] = (mtime, digest)
    return digest


def _latest_trading_day():
    import kline_store
    return kline_store._conn().execute(
        "SELECT MAX(last_date) FROM fetch_ledger WHERE ktype='kline20h' AND status='done'"
    ).fetchone()[0] or ''


def _score_last_date(code):
    import score_market
    score_market.init_db()
    row = score_market._conn().execute(
        'SELECT last_date FROM stock_score WHERE code=?', (code,)).fetchone()
    return (row[0] if row else '') or ''


def info(code):
    """报告产物信息 + 过期判定。返回 dict：
    {exists, file_name, size_kb, generated_at(本地时间str), data_last_date, model,
     stale, stale_reason, ttl_hours, latest_trading_day}"""
    html_path = _latest_file(os.path.join(REPORTS_DIR, f'*{code}-valuation.html'))
    digest = _json_digest(code) or {}
    latest_td = _latest_trading_day()
    score_last = _score_last_date(code)

    out = {
        'exists': html_path is not None,
        'file_name': os.path.basename(html_path) if html_path else '',
        'size_kb': round(os.path.getsize(html_path) / 1024) if html_path else 0,
        'generated_at': None,
        'data_last_date': digest.get('data_last_date', ''),
        'model': digest.get('model_type', ''),
        'weights': digest.get('weights', ''),
        'latest_raw_price': digest.get('latest_raw_price'),
        'score': digest.get('score'),
        'ttl_hours': ttl_hours(),
        'latest_trading_day': latest_td,
        'stale': False,
        'stale_reason': '',
    }

    if not html_path:
        out['stale'] = True
        out['stale_reason'] = '尚无报告'
        return out

    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(html_path))
    gen_dt = timezone.make_aware(mtime) if timezone.is_naive(mtime) else mtime
    out['generated_at'] = timezone.localtime(gen_dt).strftime('%Y-%m-%d %H:%M')

    # 登记/刷新数据库记录（生成时间入库的直接来源）
    ReportArtifact.objects.update_or_create(
        code=code,
        defaults={
            'name': (digest.get('stock') or '')[:40],
            'model': out['model'][:20],
            'file_name': out['file_name'][:140],
            'size_kb': out['size_kb'],
            'data_last_date': out['data_last_date'],
            'generated_at': gen_dt,
        })

    # 过期判据：数据落后 > TTL
    if score_last and out['data_last_date'] and out['data_last_date'] < score_last:
        out['stale'] = True
        out['stale_reason'] = f"数据落后（报告至 {out['data_last_date']}，分数至 {score_last}）"
    elif latest_td and out['data_last_date'] and out['data_last_date'] < latest_td and not score_last:
        out['stale'] = True
        out['stale_reason'] = f"数据落后（报告至 {out['data_last_date']}，最新交易日 {latest_td}）"
    elif gen_dt < timezone.now() - datetime.timedelta(hours=ttl_hours()):
        out['stale'] = True
        out['stale_reason'] = f'缓存超过 {ttl_hours()} 小时'
    return out


def stale_codes():
    """已登记报告里过期的代码清单（供概览/批量刷新用）"""
    out = []
    latest = _latest_trading_day()
    if not latest:
        return out
    for rec in ReportArtifact.objects.all():
        if rec.data_last_date and rec.data_last_date < latest:
            out.append(rec.code)
    return out


_BACKFILLED = False


def backfill_registry(force=False):
    """把磁盘上已有的报告一次性登记进 DB（只取 mtime/size，不解析 JSON）。
    进程内只跑一次；之后被打开的报告会由 info() 补齐名称/模型/数据末日。"""
    global _BACKFILLED
    if _BACKFILLED and not force:
        return 0
    _BACKFILLED = True
    known = {r.code: r for r in ReportArtifact.objects.all()}
    n = 0
    for path in glob.glob(os.path.join(REPORTS_DIR, '*-valuation.html')):
        base = os.path.basename(path)
        code = base.rsplit('-valuation.html', 1)[0][-6:]
        if not (code.isdigit() and len(code) == 6):
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        gen_dt = timezone.make_aware(mtime) if timezone.is_naive(mtime) else mtime
        rec = known.get(code)
        if rec and rec.generated_at and abs((rec.generated_at - gen_dt).total_seconds()) < 1:
            continue
        ReportArtifact.objects.update_or_create(
            code=code,
            defaults={
                'file_name': base[:140],
                'size_kb': round(os.path.getsize(path) / 1024),
                'generated_at': gen_dt,
            })
        n += 1
    return n
