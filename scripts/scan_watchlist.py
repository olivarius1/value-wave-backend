#!/usr/bin/env python3
"""
watchlist 快速估值扫描
- 读取 watchlist.txt 中的股票
- 获取最新K线（增量缓存）
- 计算当前分数 + 历史PE/PB区间
- 输出是否低估/值得建仓的判断
"""
import json
import os
import sys
import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)

from kline_cache import get_kline, CACHE_DIR
from financial_fetcher import (
    fetch_pershare_data, compute_valuation_range,
    fetch_financial_reports, compute_financial_metrics, auto_fill_factors
)

# ===== watchlist 股票映射 (名称 → 代码, 模型) =====
WATCHLIST_MAP = {
    '中国海油': ('600938', 'cyclical'),
    '紫金矿业': ('601899', 'cyclical'),
    '川恒股份': ('002895', 'cyclical'),
    '万华化学': ('600309', 'cyclical'),
    '星宇股份': ('601799', 'discretionary'),
    '云铝股份': ('000807', 'cyclical'),
    '云天化':   ('600096', 'cyclical'),
    '中国中车': ('601766', 'soe'),
    '中国建筑': ('601668', 'soe'),
    '招商银行': ('600036', 'bank'),
    '中国平安': ('601318', 'bank'),
    '香农芯创': ('300475', 'tech'),
    '世运电路': ('603920', 'tech'),
    '东山精密': ('002384', 'tech'),
    '国投资本': ('600061', 'bank'),
    '紫金银行': ('601860', 'bank'),
    '文科股份': ('002775', 'soe'),
    '恒力石化': ('600346', 'cyclical'),
    '盐湖股份': ('000792', 'cyclical'),
    '通源石油': ('300164', 'cyclical'),
    '国元证券': ('000728', 'bank'),
    '铜陵有色': ('000630', 'cyclical'),
    '中国神华': ('601088', 'soe'),
    '华能国际': ('600011', 'soe'),
    '伊利股份': ('600887', 'staples'),
    '中国石油': ('601857', 'cyclical'),
    '陕西煤业': ('601225', 'cyclical'),
    '华丽家族': ('600503', 'realestate'),
    '长城证券': ('002939', 'bank'),
    '亿纬锂能': ('300014', 'tech'),
    '新柴股份': ('301032', 'cyclical'),
    '潍柴动力': ('000338', 'cyclical'),
    '阳光电源': ('300274', 'tech'),
    '高能环境': ('603588', 'soe'),
    '晨光股份': ('603899', 'staples'),
    '德明利':   ('001309', 'tech'),
    '中际旭创': ('300308', 'tech'),
    '新易盛':   ('300502', 'tech'),
    '胜宏科技': ('300476', 'tech'),
    '神火股份': ('000933', 'cyclical'),
    '中公高科': ('603860', 'tech'),
    '宏达股份': ('600331', 'cyclical'),
    '元琛科技': ('688659', 'tech'),
    '宝丰能源': ('600989', 'cyclical'),
}

# ===== 8种模型权重 (与 report_generator.py 一致) =====
MODEL_WEIGHTS = {
    'staples':       {'pe': 0.28, 'pb': 0.12, 'peg': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.10, 'margin_stability': 0.10},
    'discretionary': {'pe': 0.22, 'pb': 0.12, 'peg': 0.22, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'brand_premium': 0.11},
    'tech':          {'pe': 0.20, 'pb': 0.12, 'peg': 0.25, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'rd_ratio': 0.10},
    'cyclical':      {'pe': 0.25, 'pb': 0.12, 'commodity_dev': 0.20, 'ma': 0.15, 'vol': 0.10, 'vola': 0.10, 'capacity_util': 0.08},
    'soe':           {'pe': 0.15, 'pb': 0.18, 'dividend_yield': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'order_growth': 0.15, 'roe': 0.04},
    'bank':          {'pe': 0.00, 'pb': 0.30, 'roe': 0.25, 'dividend_yield': 0.15, 'npl_ratio': 0.12, 'ma': 0.10, 'vola': 0.08},
    'realestate':    {'pe': 0.00, 'pb': 0.20, 'nav_discount': 0.25, 'clearance_rate': 0.20, 'ma': 0.12, 'vol': 0.08, 'leverage': 0.10, 'vola': 0.05},
    'pharma':        {'pe': 0.20, 'pb': 0.10, 'peg': 0.25, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'revenue_growth': 0.17},
}

OPTIONAL_FACTOR_KEYS = [
    'commodity_dev', 'capacity_util', 'roe', 'dividend_yield', 'npl_ratio',
    'nav_discount', 'clearance_rate', 'leverage', 'rd_ratio',
    'margin_stability', 'brand_premium', 'order_growth', 'revenue_growth',
]


# ===== 评分函数 =====
def score_pe(pe, pe_min, pe_max):
    if pe <= 0: return 50
    p = max(0, min(1, (pe - pe_min) / (pe_max - pe_min)))
    return (1 - p) * 100

def score_pb(pb, pb_min, pb_max):
    if pb <= 0: return 50
    p = max(0, min(1, (pb - pb_min) / (pb_max - pb_min)))
    return (1 - p) * 100

def score_peg(pe, eps_growth):
    if pe <= 0 or eps_growth <= 0: return 50
    peg = pe / (eps_growth * 100)
    if peg < 0.8: return 95
    elif peg < 1.0: return 80
    elif peg < 1.2: return 65
    elif peg < 1.5: return 50
    elif peg < 2.0: return 35
    else: return 20

def score_ma(close, ma20, ma60):
    if ma20 <= 0: return 50
    dev20 = (close - ma20) / ma20 * 100
    dev60 = (close - ma60) / ma60 * 100 if ma60 > 0 else 0
    s20 = max(0, min(100, 50 - dev20 * 3))
    s60 = max(0, min(100, 50 - dev60 * 2.5))
    return s20 * 0.6 + s60 * 0.4

def score_volume(volume, vol_ma20):
    if vol_ma20 <= 0: return 50
    ratio = volume / vol_ma20
    if ratio < 0.5: return 85
    elif ratio < 0.8: return 70
    elif ratio < 1.2: return 55
    elif ratio < 1.5: return 40
    elif ratio < 2.0: return 30
    else: return 20

def score_volatility(close, high, low):
    if close <= 0: return 50
    vol = (high - low) / close
    if vol < 0.01: return 85
    elif vol < 0.02: return 70
    elif vol < 0.03: return 55
    elif vol < 0.05: return 40
    else: return 20

def score_roe(roe):
    if roe <= 0: return 20
    if roe >= 0.25: return 95
    elif roe >= 0.20: return 85
    elif roe >= 0.15: return 70
    elif roe >= 0.10: return 55
    elif roe >= 0.05: return 35
    else: return 20

def score_dividend_yield(dy):
    if dy <= 0: return 30
    if dy >= 0.08: return 95
    elif dy >= 0.06: return 85
    elif dy >= 0.04: return 70
    elif dy >= 0.03: return 55
    elif dy >= 0.02: return 40
    else: return 30

def score_optional(key, val):
    """可选因子统一评分"""
    funcs = {
        'roe': score_roe,
        'dividend_yield': score_dividend_yield,
    }
    if key in funcs:
        return funcs[key](val)
    # 其余可选因子缺失时返回中性分
    return 50


def compute_latest_score(kline_data, pe, pb, price, pe_min, pe_max, pb_min, pb_max,
                         eps_growth, model_type, optional_factors=None):
    """计算最新一天的分数"""
    if not kline_data or len(kline_data) < 20:
        return None

    weights = dict(MODEL_WEIGHTS.get(model_type, MODEL_WEIGHTS['cyclical']))
    opt_factors = optional_factors or {}

    # 处理缺失可选因子权重重分配
    active_weights = {}
    missing_w = 0
    for fk, w in weights.items():
        if fk in OPTIONAL_FACTOR_KEYS:
            if opt_factors.get(fk) is not None:
                active_weights[fk] = w
            else:
                missing_w += w
        else:
            active_weights[fk] = w
    if missing_w > 0 and active_weights:
        tw = sum(active_weights.values())
        if tw > 0:
            for fk in active_weights:
                active_weights[fk] += active_weights[fk] / tw * missing_w
    total_w = sum(active_weights.values())
    if total_w > 0:
        active_weights = {k: v / total_w for k, v in active_weights.items()}

    # 取最后一天数据
    last = kline_data[-1]
    close = float(last[2])
    high = float(last[3])
    low = float(last[4])
    volume = float(last[5])

    # MA20 / MA60 / VOL_MA20
    n = len(kline_data)
    ma20 = sum(float(kline_data[j][2]) for j in range(max(0, n-20), n)) / min(20, n)
    ma60 = sum(float(kline_data[j][2]) for j in range(max(0, n-60), n)) / min(60, n)
    vol_ma20 = sum(float(kline_data[j][5]) for j in range(max(0, n-20), n)) / min(20, n)

    # 当前PE/PB (用最新qt值)
    cur_pe = pe if pe > 0 else 0
    cur_pb = pb if pb > 0 else 0

    # 逐因子评分
    total_score = 0
    for fk, w in active_weights.items():
        if w < 0.001:
            continue
        if fk == 'pe':
            s = score_pe(cur_pe, pe_min, pe_max)
        elif fk == 'pb':
            s = score_pb(cur_pb, pb_min, pb_max)
        elif fk == 'peg':
            s = score_peg(cur_pe, eps_growth)
        elif fk == 'ma':
            s = score_ma(close, ma20, ma60)
        elif fk == 'vol':
            s = score_volume(volume, vol_ma20)
        elif fk == 'vola':
            s = score_volatility(close, high, low)
        elif fk in OPTIONAL_FACTOR_KEYS and opt_factors.get(fk) is not None:
            s = score_optional(fk, opt_factors[fk])
        else:
            s = 50
        total_score += s * w

    return round(total_score, 1)


def get_status(score):
    """分数 → 状态标签"""
    if score >= 80: return '极度低估'
    elif score >= 70: return '低估'
    elif score >= 40: return '中性'
    elif score >= 20: return '高估'
    else: return '极度高估'


def get_signal(score):
    """交易信号"""
    if score >= 80: return '★★★ 强烈建仓'
    elif score >= 70: return '★★ 分批建仓'
    elif score >= 60: return '★ 轻仓试探'
    elif score >= 40: return '— 观望'
    elif score >= 20: return '▼ 减仓'
    else: return '▼▼ 清仓'


def scan_stock(name, code, model):
    """扫描单只股票，返回结果dict"""
    exchange = 'sh' if code.startswith('6') else 'sz'
    result = {'name': name, 'code': code, 'model': model, 'score': None, 'error': None}

    try:
        # 获取K线（自动增量）
        kline_result = get_kline(code, exchange)
        kline_data = kline_result['kline']
        pe = kline_result['pe']
        pb = kline_result['pb']
        price = kline_result['price']

        if not kline_data or len(kline_data) < 20:
            result['error'] = 'K线数据不足'
            return result

        result['price'] = price
        result['pe'] = pe
        result['pb'] = pb

        # 计算PE/PB历史百分位区间
        pershare = fetch_pershare_data(code, exchange)
        val_range = compute_valuation_range(kline_data, pershare, pe, pb)
        pe_min, pe_max = val_range['pe_min'], val_range['pe_max']
        pb_min, pb_max = val_range['pb_min'], val_range['pb_max']
        result['pe_range'] = f"{pe_min}~{pe_max}"
        result['pb_range'] = f"{pb_min}~{pb_max}"

        # 获取增速
        reports = fetch_financial_reports(code, exchange, max_reports=10)
        eps_growth = 0.08
        opt_factors = {}
        if reports:
            metrics = compute_financial_metrics(reports)
            opt_factors = auto_fill_factors(opt_factors, metrics, model)
            # 从年报计算增速
            annuals = [r for r in reports if r['report_type'] == 'annual']
            if len(annuals) >= 2:
                latest_p = annuals[0]['net_profit']
                oldest_idx = min(len(annuals) - 1, 4)
                oldest_p = annuals[oldest_idx]['net_profit']
                if latest_p > 0 and oldest_p > 0 and oldest_idx > 0:
                    eps_growth = round((latest_p / oldest_p) ** (1.0 / oldest_idx) - 1, 4)
        result['growth'] = eps_growth

        # 计算最新分数
        score = compute_latest_score(
            kline_data, pe, pb, price,
            pe_min, pe_max, pb_min, pb_max,
            eps_growth, model, opt_factors
        )
        result['score'] = score
        result['status'] = get_status(score)
        result['signal'] = get_signal(score)

    except Exception as e:
        result['error'] = str(e)[:60]

    return result


def load_watchlist(path):
    """读取 watchlist：新格式 CSV（名称,代码,模型,最后报告时间）；兼容旧格式（仅名称，查内置映射）"""
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('名称'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3 and parts[1].isdigit():
                rows.append((parts[0], parts[1], parts[2]))
            elif parts[0] in WATCHLIST_MAP:
                code, model = WATCHLIST_MAP[parts[0]]
                rows.append((parts[0], code, model))
    return rows


def main():
    # 读取watchlist（新格式：名称,代码,模型,最后报告时间）
    watchlist_path = os.path.join(_SKILL_DIR, 'watchlist.txt')
    if not os.path.exists(watchlist_path):
        print("错误: watchlist.txt 不存在")
        sys.exit(1)

    rows = load_watchlist(watchlist_path)
    if not rows:
        print("错误: watchlist.txt 为空或格式无法解析")
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  Watchlist 估值扫描  |  {datetime.date.today()}  |  共{len(rows)}只")
    print(f"{'='*70}")

    results = []
    for name, code, model in rows:
        r = scan_stock(name, code, model)
        results.append(r)
        if r['score'] is not None:
            print(f" 分数={r['score']} {r['status']}")
        else:
            print(f" 失败: {r.get('error', '?')}")

    # 输出汇总表
    print(f"\n{'='*70}")
    print(f"{'\u80a1\u7968':<8}{'\u4ee3\u7801':<8}{'\u4ef7\u683c':>7}{'PE':>7}{'PB':>6}{'\u5206\u6570':>6}{'\u72b6\u6001':<8}{'\u4fe1\u53f7':<12}{'\u6a21\u578b'}")
    print(f"{'-'*70}")

    # 按分数降序排列（低估在前）
    scored = [r for r in results if r['score'] is not None]
    scored.sort(key=lambda x: -x['score'])

    for r in scored:
        price_s = f"{r.get('price', 0):.2f}" if r.get('price') else '-'
        pe_s = f"{r.get('pe', 0):.1f}" if r.get('pe') else '-'
        pb_s = f"{r.get('pb', 0):.2f}" if r.get('pb') else '-'
        print(f"{r['name']:<8}{r['code']:<8}{price_s:>7}{pe_s:>7}{pb_s:>6}"
              f"{r['score']:>6.1f}  {r['status']:<8}{r['signal']:<12}{r['model']}")

    # 失败列表
    failed = [r for r in results if r['score'] is None]
    if failed:
        print(f"\n  失败({len(failed)}只): " + ', '.join(f"{r['name']}({r.get('error','')})" for r in failed))

    # 建仓提示
    buy_candidates = [r for r in scored if r['score'] >= 70]
    print(f"\n{'─'*70}")
    if buy_candidates:
        print(f"  ◆ 低估建仓候选 ({len(buy_candidates)}只):")
        for r in buy_candidates:
            pe_r = r.get('pe_range', '?')
            pb_r = r.get('pb_range', '?')
            print(f"    {r['name']}({r['code']}) 分数{r['score']} | PE区间{pe_r} | PB区间{pb_r} | {r['signal']}")
    else:
        print(f"  ◆ 当前无低估建仓候选（所有股票分数 < 70）")

    watch_candidates = [r for r in scored if 60 <= r['score'] < 70]
    if watch_candidates:
        print(f"\n  ◆ 接近低估关注 ({len(watch_candidates)}只):")
        for r in watch_candidates:
            print(f"    {r['name']}({r['code']}) 分数{r['score']} | {r['status']}")

    print(f"{'='*70}")


if __name__ == '__main__':
    main()
