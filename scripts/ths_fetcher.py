#!/usr/bin/env python3
"""
同花顺iFinD财报数据源——财报链路的主数据源（东财降级为兜底）

为什么用 PIT 时点指标而非 THS_DR 专题报表:
  p00209(业绩快报)为自愿披露，watchlist 抽测 5 只仅 1 只有数据，覆盖度不合格；
  ths_*_pit_stock 全覆盖，且查询日锚定后数值不随后续披露变动，天然满足回测 PIT 语义。

指标与口径（查询日 = 运行当日，与缓存冻结窗口一致）:
  ths_np_atoopc_pit_stock      归母净利润(元)   → 换挡检测/盈利动能核心
  ths_revenue_pit_stock        营业收入(元)     → 营收同比本地推导
  ths_operating_cost_pit_stock 营业成本(元)     → 毛利率 = 1 - 成本/收入
  ths_roe_stock                ROE(%) 按报告期   → 与东财 ROEJQ 同为披露口径

产出: artifacts/.cache/financial/{code}_reports.json
  schema 与 financial_fetcher 东财版逐字段一致，build_report 的 30 天缓存直接复用。

用法:
  python3 scripts/ths_fetcher.py                # 预热 watchlist 全部财报缓存（batch 前跑一次）
  python3 scripts/ths_fetcher.py 000807 600887  # 指定代码
登录: artifacts/.cache/ths_credentials.json (gitignored): {"username": "...", "password": "..."}
"""
import argparse
import datetime
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_FIN_CACHE_DIR = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'financial')
_CREDS_PATH = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'ths_credentials.json')
_WATCHLIST = os.path.join(_SKILL_DIR, 'watchlist.txt')

# 历史起点与东财 max_reports=40(约10年季报) 对齐
_PERIOD_START_YEAR = 2016
_PIT_INDICATORS = ('ths_np_atoopc_pit_stock', 'ths_revenue_pit_stock', 'ths_operating_cost_pit_stock')
_ROE_INDICATOR = 'ths_roe_stock'
_CHUNK = 25          # 单次调用最大股票数
_CALL_SLEEP = 0.15   # 调用间隔，避开接口频率限制


def _login():
    """登录 iFinD（进程内一次），返回 iFinDPy 模块"""
    from iFinDPy import THS_iFinDLogin, THS_GetErrorInfo
    with open(_CREDS_PATH, encoding='utf-8') as f:
        creds = json.load(f)
    code = THS_iFinDLogin(creds['username'], creds['password'])
    if code != 0:
        raise RuntimeError(f'iFinD登录失败 err={code}: {THS_GetErrorInfo(code)}')
    return sys.modules['iFinDPy']


def _periods(today=None):
    """报告期列表: 2016Q1 起至最近一个已结束季度，升序"""
    today = today or datetime.date.today()
    periods = []
    for y in range(_PERIOD_START_YEAR, today.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = datetime.date(y, m, d)
            if p <= today:
                periods.append(p)
    return periods


def _thsscodes(codes):
    return [f"{c}.{'SH' if c.startswith('6') else 'SZ'}" for c in codes]


def _bd_retry(thspy, codes_str, indicator, param, retries=3):
    """单指标批量查询，dict/object 两种返回归一化为 (errorcode, dataframe|None, errmsg)"""
    for attempt in range(1, retries + 1):
        r = thspy.THS_BD(codes_str, indicator, param)
        ec = r.get('errorcode') if isinstance(r, dict) else r.errorcode
        data = None if isinstance(r, dict) else r.data
        msg = r.get('errmsg', '') if isinstance(r, dict) else getattr(r, 'errmsg', '')
        if ec == 0:
            return 0, data, ''
        if attempt < retries:
            time.sleep(attempt * 2)
    return ec, data, msg


def fetch_watchlist_reports(codes, anchor_date=None):
    """拉取财报并写入缓存。返回 {code:年报数}；任何批量调用重试后仍失败则抛异常（宁可不写缓存，不写残缓存）"""
    thspy = _login()
    anchor = (anchor_date or datetime.date.today()).strftime('%Y-%m-%d')
    periods = _periods(anchor_date and datetime.date.fromisoformat(anchor_date) or None)
    scodes = _thsscodes(codes)

    # rows[code][period] = {指标: 值}；指标按 (period, indicator) 批量
    rows = {c: {} for c in codes}
    total_calls = 0
    for chunk_start in range(0, len(scodes), _CHUNK):
        chunk = scodes[chunk_start:chunk_start + _CHUNK]
        orig = codes[chunk_start:chunk_start + _CHUNK]
        for period in periods:
            pstr = period.strftime('%Y%m%d')
            for ind in _PIT_INDICATORS:
                ec, df, msg = _bd_retry(thspy, ','.join(chunk), ind, f'{anchor},{pstr},1')
                total_calls += 1
                if ec != 0:
                    raise RuntimeError(f'{ind}@{pstr} 批量查询失败 err={ec} {msg}')
                for _, row in df.iterrows():
                    code = row['thscode'].split('.')[0]
                    rows[code].setdefault(period, {})[ind] = row[ind]
                time.sleep(_CALL_SLEEP)
            ec, df, msg = _bd_retry(thspy, ','.join(chunk), _ROE_INDICATOR, period.strftime('%Y-%m-%d'))
            total_calls += 1
            if ec != 0:
                raise RuntimeError(f'{_ROE_INDICATOR}@{pstr} 批量查询失败 err={ec} {msg}')
            for _, row in df.iterrows():
                code = row['thscode'].split('.')[0]
                rows[code].setdefault(period, {})[_ROE_INDICATOR] = row[_ROE_INDICATOR]
            time.sleep(_CALL_SLEEP)
        print(f'  进度 {min(chunk_start + _CHUNK, len(codes))}/{len(codes)} 只', flush=True)

    result = {}
    for code in codes:
        reports = _build_reports(code, rows[code], periods)
        _save_cache(code, reports)
        n_annual = len([r for r in reports if r['report_type'] == 'annual'])
        result[code] = n_annual
    print(f'  API调用 {total_calls} 次')
    return result


def _build_reports(code, period_rows, periods):
    """组装为东财 schema 的报告列表（按报告期从新到旧），YoY 与去年同期比较"""
    _TYPE = {3: ('q1', '一季报'), 6: ('semi', '半年报'), 9: ('q3', '三季报'), 12: ('annual', '年报')}
    by_period = {}
    for p in periods:
        vals = period_rows.get(p, {})
        np_, rev, cost, roe = (vals.get(i) for i in (*_PIT_INDICATORS, _ROE_INDICATOR))
        if np_ is None and rev is None:
            continue  # 未披露/未上市
        rtype, rtype_cn = _TYPE[p.month]
        rev_f = float(rev) if rev is not None else 0.0
        cost_f = float(cost) if cost is not None else 0.0
        by_period[p] = {
            'report_date': p.isoformat(),
            'report_type': rtype,
            'report_type_cn': rtype_cn,
            'revenue': round(rev_f / 1e8, 2),
            'net_profit': round(float(np_) / 1e8, 2) if np_ is not None else 0.0,
            'roe': round(float(roe) / 100, 4) if roe is not None else 0.0,
            'gross_margin': round(1 - cost_f / rev_f, 4) if rev_f and cost_f else 0.0,
        }
    reports = []
    for p in sorted(by_period, reverse=True):
        cur = by_period[p]
        prev = by_period.get(datetime.date(p.year - 1, p.month, p.day))
        for field in ('revenue', 'net_profit'):
            key = 'profit_yoy' if field == 'net_profit' else 'revenue_yoy'
            if prev and prev[field]:
                cur[key] = round(cur[field] / prev[field] - 1, 4)
            else:
                cur[key] = 0.0
        reports.append(cur)
    return reports


def _save_cache(code, reports):
    os.makedirs(_FIN_CACHE_DIR, exist_ok=True)
    path = os.path.join(_FIN_CACHE_DIR, f'{code}_reports.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(reports, f, ensure_ascii=False)


def watchlist_codes():
    codes = []
    with open(_WATCHLIST, encoding='utf-8') as f:
        for line in f.read().splitlines()[1:]:
            parts = line.split(',')
            if len(parts) >= 2 and parts[1].strip():
                codes.append(parts[1].strip())
    return codes


def main():
    parser = argparse.ArgumentParser(description='iFinD财报缓存预热')
    parser.add_argument('codes', nargs='*', help='股票代码，缺省为 watchlist 全部')
    args = parser.parse_args()
    codes = args.codes or watchlist_codes()
    print(f'共 {len(codes)} 只待拉取')
    counts = fetch_watchlist_reports(codes)
    short = {c: n for c, n in counts.items() if n < 8}
    print(f'完成: 年报数<8 的 {len(short)} 只{": " + str(short) if short else "（无）"}')


if __name__ == '__main__':
    main()
