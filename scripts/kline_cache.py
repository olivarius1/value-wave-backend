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
import urllib.request
import datetime

# 缓存目录：local_reports/.cache/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_PROJECT_ROOT = _SKILL_DIR  # 独立项目，根目录即skill目录
CACHE_DIR = os.path.join(_PROJECT_ROOT, 'local_reports', '.cache')


def _cache_path(stock_code):
    return os.path.join(CACHE_DIR, f'{stock_code}_kline.json')


def load_cache(stock_code):
    """加载本地K线缓存，返回 dict 或 None"""
    path = _cache_path(stock_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def save_cache(stock_code, exchange, kline_data, pe=0, pb=0, price=0, name=''):
    """保存K线数据到本地缓存"""
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
        'data': kline_data,
    }
    with open(_cache_path(stock_code), 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False)


def _fetch_kline_api(full_code, start_date, end_date):
    """从腾讯财经API获取一批K线数据"""
    url = (
        f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={full_code},day,{start_date},{end_date},500,qfq"
    )
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _parse_kline_response(jd, full_code):
    """解析腾讯K线API响应，返回 (kline_rows, qt_info)"""
    stock_data = jd.get('data', {}).get(full_code, {})
    kdata = stock_data.get('qfqday') or stock_data.get('day', [])
    qt = stock_data.get('qt', {}).get(full_code, [])
    return kdata, qt


def _extract_qt_info(qt):
    """从qt字段提取PE/PB/价格/名称"""
    if not qt or len(qt) < 47:
        return {'pe': 0, 'pb': 0, 'price': 0, 'name': ''}
    try:
        return {
            'name': qt[1] if len(qt) > 1 else '',
            'price': float(qt[3]) if qt[3] else 0,
            'pe': float(qt[39]) if qt[39] else 0,
            'pb': float(qt[46]) if qt[46] else 0,
        }
    except (ValueError, IndexError):
        return {'pe': 0, 'pb': 0, 'price': 0, 'name': qt[1] if len(qt) > 1 else ''}


def fetch_full(stock_code, exchange, years=10):
    """全量获取N年K线数据（6批API调用）"""
    full_code = f"{exchange}{stock_code}"
    today = datetime.date.today()
    start = today - datetime.timedelta(days=years * 365)

    # 每批约700自然日覆盖~500个交易日
    batch_days = 700
    batches = []
    current = start
    while current < today:
        end = current + datetime.timedelta(days=batch_days)
        if end > today + datetime.timedelta(days=365):
            end = today + datetime.timedelta(days=365)
        batches.append((current.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')))
        current = end

    all_kline = []
    seen_dates = set()
    qt_info = {'pe': 0, 'pb': 0, 'price': 0, 'name': ''}

    for i, (s, e) in enumerate(batches):
        try:
            jd = _fetch_kline_api(full_code, s, e)
            kdata, qt = _parse_kline_response(jd, full_code)
            if qt:
                qt_info = _extract_qt_info(qt)
            for row in kdata:
                if row[0] not in seen_dates:
                    all_kline.append(row[:6])  # [date, open, close, high, low, volume]
                    seen_dates.add(row[0])
            print(f"  K线批次{i+1}/{len(batches)}: {s}~{e} = {len(kdata)}天")
        except Exception as e_err:
            print(f"  K线批次{i+1}获取失败: {e_err}", file=sys.stderr)

    return all_kline, qt_info


def fetch_incremental(stock_code, exchange, last_date):
    """增量获取 last_date 之后的新K线数据（1次API调用）"""
    full_code = f"{exchange}{stock_code}"
    today = datetime.date.today()
    # 从 last_date 的下一天开始
    start = last_date
    end = (today + datetime.timedelta(days=30)).strftime('%Y-%m-%d')

    try:
        jd = _fetch_kline_api(full_code, start, end)
        kdata, qt = _parse_kline_response(jd, full_code)
        qt_info = _extract_qt_info(qt)
        # 过滤掉 <= last_date 的数据
        new_rows = [row[:6] for row in kdata if row[0] > last_date]
        if new_rows:
            print(f"  增量获取: {len(new_rows)}天新数据 ({new_rows[0][0]} ~ {new_rows[-1][0]})")
        else:
            print(f"  增量获取: 无新数据")
        return new_rows, qt_info
    except Exception as e:
        print(f"  增量获取失败: {e}", file=sys.stderr)
        return [], {'pe': 0, 'pb': 0, 'price': 0, 'name': ''}


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
                }
            else:
                # 增量更新
                print(f"  缓存存在({cache.get('updated')}), 增量更新...")
                new_rows, qt_info = fetch_incremental(stock_code, exchange, cache['last_date'])
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
                # 保存更新后的缓存
                save_cache(stock_code, exchange, cache['data'], pe, pb, price, name)
                return {
                    'kline': cache['data'],
                    'pe': pe, 'pb': pb, 'price': price, 'name': name,
                }

    # 全量获取
    print(f"  全量获取10年K线数据...")
    kline_data, qt_info = fetch_full(stock_code, exchange)
    if kline_data:
        save_cache(stock_code, exchange, kline_data,
                   qt_info['pe'], qt_info['pb'], qt_info['price'], qt_info['name'])
    return {
        'kline': kline_data,
        'pe': qt_info['pe'],
        'pb': qt_info['pb'],
        'price': qt_info['price'],
        'name': qt_info['name'],
    }
