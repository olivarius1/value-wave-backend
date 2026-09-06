#!/usr/bin/env python3
"""
K线数据缓存与增量更新模块
- 首次运行：全量获取10年K线并缓存
- 后续运行：仅获取缓存最后日期之后的新数据
- 同日重复运行：直接使用缓存，0次API调用
"""
import json
import os
import sys
import time
import urllib.request
import datetime

# 缓存目录：artifacts/.cache/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_PROJECT_ROOT = _SKILL_DIR  # 独立项目，根目录即skill目录
CACHE_DIR = os.path.join(_PROJECT_ROOT, 'artifacts', '.cache')


def _cache_path(stock_code, suffix='_kline'):
    return os.path.join(CACHE_DIR, f'{stock_code}{suffix}.json')


def load_cache(stock_code, suffix='_kline'):
    """加载本地K线缓存，返回 dict 或 None（suffix 指定缓存类型）"""
    path = _cache_path(stock_code, suffix)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def save_cache(stock_code, exchange, kline_data, pe=0, pb=0, price=0, name='', suffix='_kline', qt_date='', volume=0):
    """保存K线数据到本地缓存（suffix 指定缓存类型）"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    last_date = kline_data[-1][0] if kline_data else ''
    cache = {
        'code': stock_code,
        'exchange': exchange,
        'name': name,
        'updated': datetime.date.today().strftime('%Y-%m-%d'),
        'last_date': last_date,
        'pe': pe,
        'pb': pb,
        'price': price,
        'qt_date': qt_date,
        'volume': volume,
        'data': kline_data,
    }
    with open(_cache_path(stock_code, suffix), 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False)


# 同一接口的备用域名，主域故障（如整体501）时依次切换
_KLINE_HOSTS = (
    'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
    'http://ifzq.gtimg.cn/appstock/app/fqkline/get',
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get',
)


def _fetch_kline_api(full_code, start_date, end_date, fq='qfq'):
    """从腾讯财经API获取一批K线数据（fq='' 时不复权，主域失败自动切换备用域名）"""
    query = f"param={full_code},day,{start_date},{end_date},500,{fq}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    last_err = None
    for base in _KLINE_HOSTS:
        req = urllib.request.Request(f"{base}?{query}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
    raise last_err


def _parse_kline_response(jd, full_code):
    """解析腾讯K线API响应，返回 (kline_rows, qt_info)"""
    stock_data = jd.get('data', {}).get(full_code, {})
    kdata = stock_data.get('qfqday') or stock_data.get('day', [])
    qt = stock_data.get('qt', {}).get(full_code, [])
    return kdata, qt


def _extract_qt_info(qt):
    """从qt字段提取PE/PB/价格/名称/日期/成交量"""
    if not qt or len(qt) < 47:
        return {'pe': 0, 'pb': 0, 'price': 0, 'name': '', 'date': '', 'volume': 0}
    try:
        raw_date = qt[30] if len(qt) > 30 and qt[30] else ''
        # qt[30] 形如 20260820 或 20260820130756（盘中带时间），取前8位
        date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else ''
        return {
            'name': qt[1] if len(qt) > 1 else '',
            'price': float(qt[3]) if qt[3] else 0,
            'pe': float(qt[39]) if qt[39] else 0,
            'pb': float(qt[46]) if qt[46] else 0,
            'date': date_str,
            'volume': float(qt[6]) if len(qt) > 6 and qt[6] else 0,
        }
    except (ValueError, IndexError):
        return {'pe': 0, 'pb': 0, 'price': 0, 'name': qt[1] if len(qt) > 1 else '', 'date': '', 'volume': 0}


def _fetch_with_retry(full_code, start_date, end_date, retries=3, backoff=2, fq='qfq'):
    """带重试的K线API调用（指数退避）"""
    for attempt in range(1, retries + 1):
        try:
            jd = _fetch_kline_api(full_code, start_date, end_date, fq=fq)
            return jd
        except Exception as e:
            if attempt < retries:
                wait = backoff ** attempt
                print(f"    重试{attempt}/{retries-1} ({wait}s后): {e}", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
    return None


def fetch_full(stock_code, exchange, years=10, chunk_years=3, fq='qfq'):
    """
    全量获取N年K线数据（分块+重试）
    - 按 chunk_years 年为一块（默认3年），进度按块报告
    - 每块内部按700天API调用（腾讯API单次上限500条）
    - 每次API调用失败自动重试3次（指数退避）
    - fq: 复权方式 'qfq'/'hfq'/''（空=不复权）
    """
    full_code = f"{exchange}{stock_code}"
    today = datetime.date.today()
    start = today - datetime.timedelta(days=years * 365)

    # 按 chunk_years 年划分大块
    chunk_days = chunk_years * 365
    chunks = []
    current = start
    while current < today:
        chunk_end = current + datetime.timedelta(days=chunk_days)
        if chunk_end > today + datetime.timedelta(days=30):
            chunk_end = today + datetime.timedelta(days=30)
        chunks.append((current, chunk_end))
        current = chunk_end

    all_kline = []
    seen_dates = set()
    qt_info = {'pe': 0, 'pb': 0, 'price': 0, 'name': ''}

    # API单次请求覆盖~700自然日（≤500交易日）
    api_batch_days = 700

    for ci, (chunk_start, chunk_end) in enumerate(chunks):
        chunk_count = 0
        # 块内按700天细分API调用
        cur = chunk_start
        while cur < chunk_end:
            api_end = cur + datetime.timedelta(days=api_batch_days)
            if api_end > chunk_end:
                api_end = chunk_end
            s = cur.strftime('%Y-%m-%d')
            e = api_end.strftime('%Y-%m-%d')
            try:
                jd = _fetch_with_retry(full_code, s, e, fq=fq)
                kdata, qt = _parse_kline_response(jd, full_code)
                if qt:
                    qt_info = _extract_qt_info(qt)
                for row in kdata:
                    if row[0] not in seen_dates:
                        all_kline.append(row[:6])
                        seen_dates.add(row[0])
                        chunk_count += 1
            except Exception as e_err:
                print(f"  块{ci+1} 子批{s}~{e} 最终失败: {e_err}", file=sys.stderr)
            cur = api_end

        cs = chunk_start.strftime('%Y-%m')
        ce = chunk_end.strftime('%Y-%m')
        print(f"  K线块{ci+1}/{len(chunks)}: {cs}~{ce} = {chunk_count}天")

    return all_kline, qt_info


def fetch_incremental(stock_code, exchange, last_date, fq='qfq', check_days=30, cached_close=None):
    """增量获取 last_date 之后的新K线数据（1次API调用，带重试）

    Args:
        check_days: 基准对比回看天数（默认30天，覆盖近期除权）
        cached_close: 可选 dict {date: close}，传入后对比接口最新复权基准下重叠日期的价格，
                      用于检测除权（前复权价以最新价为基准，除权后历史价整体重算）

    Returns:
        (new_rows, qt_info, base_shift_pct)
        base_shift_pct: 重叠日期最大价格差异比例（%），无重叠或未传 cached_close 时为 0
    """
    full_code = f"{exchange}{stock_code}"
    today = datetime.date.today()
    # 从 last_date 的前 check_days 天开始（覆盖除权发生在缓存建立后任意时点的场景）
    start = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=check_days)).isoformat()
    end = (today + datetime.timedelta(days=30)).strftime('%Y-%m-%d')

    try:
        jd = _fetch_with_retry(full_code, start, end, fq=fq)
        kdata, qt = _parse_kline_response(jd, full_code)
        qt_info = _extract_qt_info(qt)
        # 重叠日期最大价格差异（%）：接口当前基准 vs 缓存，差异>0.5% 视为除权
        base_shift_pct = 0.0
        if cached_close:
            _max_diff = 0.0
            for _row in kdata:
                _c = float(_row[2])
                _cc = cached_close.get(_row[0])
                if _cc:
                    _d = abs(_c - _cc) / _cc * 100
                    if _d > _max_diff:
                        _max_diff = _d
            base_shift_pct = _max_diff
        # 过滤掉 <= last_date 的数据
        new_rows = [row[:6] for row in kdata if row[0] > last_date]
        if new_rows:
            print(f"  增量获取: {len(new_rows)}天新数据 ({new_rows[0][0]} ~ {new_rows[-1][0]})")
        else:
            print(f"  增量获取: 无新数据")
        return new_rows, qt_info, base_shift_pct
    except Exception as e:
        print(f"  增量获取失败: {e}", file=sys.stderr)
        return [], {'pe': 0, 'pb': 0, 'price': 0, 'name': ''}, 0


def get_kline(stock_code, exchange=None, no_cache=False):
    """
    统一入口：获取K线数据（自动缓存+增量）

    Args:
        stock_code: 股票代码
        exchange: 交易所 (sh/sz)，None则自动推断
        no_cache: 强制全量刷新

    Returns:
        dict: {
            'kline': [[date, open, close, high, low, volume], ...],
            'pe': float, 'pb': float, 'price': float, 'name': str
        }
    """
    # 自动推断交易所
    if not exchange:
        exchange = 'sh' if stock_code.startswith('6') else 'sz'

    today_str = datetime.date.today().strftime('%Y-%m-%d')

    # 尝试使用缓存
    if not no_cache:
        cache = load_cache(stock_code)
        if cache and cache.get('data'):
            if cache.get('updated') == today_str:
                # 今天已更新，直接使用
                print(f"  缓存命中: {len(cache['data'])}天 (更新于今日)")
                return {
                    'kline': cache['data'],
                    'pe': cache.get('pe', 0),
                    'pb': cache.get('pb', 0),
                    'price': cache.get('price', 0),
                    'name': cache.get('name', ''),
                    'qt_date': cache.get('qt_date', ''),
                    'volume': cache.get('volume', 0),
                }
            else:
                # 增量更新
                print(f"  缓存存在({cache.get('updated')}), 增量更新...")
                _cached_close = {r[0]: float(r[2]) for r in cache['data']}
                new_rows, qt_info, base_shift = fetch_incremental(
                    stock_code, exchange, cache['last_date'], cached_close=_cached_close)
                # 除权检测：接口当前复权基准 vs 缓存重叠日期最大偏移>0.5% 视为除权除息
                # （前复权价以最新价为基准，除权后历史价整体重算；增量拼接会导致除权日前后断层，须全量重拉）
                if base_shift > 0.5:
                    print(f"  检测到除权除息(复权基准偏移 {base_shift:.2f}%)，全量重拉K线...")
                    kline_data, qt_info = fetch_full(stock_code, exchange)
                    if kline_data:
                        save_cache(stock_code, exchange, kline_data,
                                   qt_info['pe'], qt_info['pb'], qt_info['price'], qt_info['name'],
                                   qt_date=qt_info.get('date', ''), volume=qt_info.get('volume', 0))
                    return {
                        'kline': kline_data,
                        'pe': qt_info['pe'], 'pb': qt_info['pb'], 'price': qt_info['price'],
                        'name': qt_info['name'], 'qt_date': qt_info.get('date', ''),
                        'volume': qt_info.get('volume', 0),
                    }
                if new_rows:
                    # 合并去重
                    existing_dates = {row[0] for row in cache['data']}
                    for row in new_rows:
                        if row[0] not in existing_dates:
                            cache['data'].append(row)
                # 更新qt信息（增量API也返回最新qt）
                pe = qt_info['pe'] or cache.get('pe', 0)
                pb = qt_info['pb'] or cache.get('pb', 0)
                price = qt_info['price'] or cache.get('price', 0)
                name = qt_info['name'] or cache.get('name', '')
                qt_date = qt_info.get('date', '') or cache.get('qt_date', '')
                volume = qt_info.get('volume', 0) or cache.get('volume', 0)
                # 保存更新后的缓存
                save_cache(stock_code, exchange, cache['data'], pe, pb, price, name, qt_date=qt_date, volume=volume)
                return {
                    'kline': cache['data'],
                    'pe': pe, 'pb': pb, 'price': price, 'name': name,
                    'qt_date': qt_date, 'volume': volume,
                }

    # 全量获取
    print(f"  全量获取10年K线数据...")
    kline_data, qt_info = fetch_full(stock_code, exchange)
    if kline_data:
        save_cache(stock_code, exchange, kline_data,
                   qt_info['pe'], qt_info['pb'], qt_info['price'], qt_info['name'],
                   qt_date=qt_info.get('date', ''), volume=qt_info.get('volume', 0))
    return {
        'kline': kline_data,
        'pe': qt_info['pe'],
        'pb': qt_info['pb'],
        'price': qt_info['price'],
        'name': qt_info['name'],
        'qt_date': qt_info.get('date', ''),
        'volume': qt_info.get('volume', 0),
    }


def get_kline_raw(stock_code, exchange=None, no_cache=False):
    """
    获取不复权K线（独立缓存 {code}_raw_kline.json）

    用途：历史 PE/PB 必须用当日真实交易价（前复权价会随最新除权整体缩放，
    导致历史 PE 失真）。收益/MA/量能因子仍用前复权（qfq 缓存）。

    Returns:
        [[date, open, close, high, low, volume], ...] 或 []
    """
    if not exchange:
        exchange = 'sh' if stock_code.startswith('6') else 'sz'

    today_str = datetime.date.today().strftime('%Y-%m-%d')

    if not no_cache:
        cache = load_cache(stock_code, suffix='_raw_kline')
        if cache and cache.get('data'):
            if cache.get('updated') == today_str:
                print(f"  不复权缓存命中: {len(cache['data'])}天 (更新于今日)")
                return cache['data']
            # 增量更新不复权数据
            print(f"  不复权缓存存在({cache.get('updated')}), 增量更新...")
            new_rows, qt_info, _api_last = fetch_incremental(stock_code, exchange, cache['last_date'], fq='')
            if new_rows:
                existing_dates = {row[0] for row in cache['data']}
                for row in new_rows:
                    if row[0] not in existing_dates:
                        cache['data'].append(row)
            save_cache(stock_code, exchange, cache['data'],
                       cache.get('pe', 0), cache.get('pb', 0),
                       cache.get('price', 0), cache.get('name', ''), suffix='_raw_kline')
            cache['data'].sort(key=lambda r: r[0])
            return cache['data']

    print(f"  全量获取10年不复权K线数据...")
    kline_data, qt_info = fetch_full(stock_code, exchange, fq='')
    if kline_data:
        save_cache(stock_code, exchange, kline_data,
                   qt_info['pe'], qt_info['pb'], qt_info['price'], qt_info['name'], suffix='_raw_kline')
    return kline_data
