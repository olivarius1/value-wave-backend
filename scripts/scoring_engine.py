# -*- coding: utf-8 -*-
"""
可复用估值评分引擎（与数据获取、HTML渲染完全解耦）

设计原则：
- 所有函数显式传参，不依赖任何全局状态，可被任意脚本独立 import 复用。
- 例如复用科技股算分：
      from scoring_engine import MODEL_PRESETS, resolve_active_weights, compute_daily_scores
      weights = resolve_active_weights(MODEL_PRESETS['tech']['weights'], factor_values)
      results = compute_daily_scores(kline, weights, factor_values, params)

包含：
- MODEL_PRESETS：8种行业模型权重预设
- FACTOR_NAMES：因子中文名映射
- 全部因子评分函数（score_*）
- resolve_active_weights：缺失可选因子的权重再分配 + 归一化
- build_weights_display：权重展示字符串
- compute_daily_scores：每日估值评分序列计算（核心）
"""

# ===== 8种模型权重预设 =====
MODEL_PRESETS = {
    'staples': {
        'name': '必选消费',
        'desc': '需求刚性、业绩稳定、现金流充沛，PE+毛利率稳定性为估值锚',
        'weights_label': 'PE(28%) + PB(12%) + PEG(20%) + MA偏离(12%) + 量能(8%) + 波动率(10%) + 毛利率稳定性(10%)',
        'weights': {'pe': 0.28, 'pb': 0.12, 'peg': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.10, 'margin_stability': 0.10},
    },
    'discretionary': {
        'name': '可选消费',
        'desc': '品牌溢价显著、受消费周期影响，PEG与品牌力为核心估值锚',
        'weights_label': 'PE(22%) + PB(12%) + PEG(22%) + MA偏离(15%) + 量能(8%) + 波动率(10%) + 品牌溢价度(11%)',
        'weights': {'pe': 0.22, 'pb': 0.12, 'peg': 0.22, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'brand_premium': 0.11},
    },
    'tech': {
        'name': '科技制造',
        'desc': '高研发投入、高增速，PEG为最敏感因子，关注成长确定性',
        'weights_label': 'PE(20%) + PB(12%) + PEG(25%) + MA偏离(15%) + 量能(8%) + 波动率(10%) + 研发费用率(10%)',
        'weights': {'pe': 0.20, 'pb': 0.12, 'peg': 0.25, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'rd_ratio': 0.10},
    },
    'cyclical': {
        'name': '周期资源',
        'desc': '盈利随大宗商品价格大幅波动，需追踪商品价格位置与产能周期；股息率修正股东回报',
        'weights_label': 'PE(22.5%) + PB(10.8%) + 商品价格偏离(18%) + MA偏离(13.5%) + 量能(9%) + 波动率(9%) + 产能利用率(7.2%) + 股息率(10%)',
        'weights': {'pe': 0.225, 'pb': 0.108, 'commodity_dev': 0.18, 'ma': 0.135, 'vol': 0.09, 'vola': 0.09, 'capacity_util': 0.072, 'dividend_yield': 0.10},
    },
    'soe': {
        'name': '央企基建',
        'desc': '高股息、订单驱动、经营稳健，股息率与PB为估值核心',
        'weights_label': 'PE(15%) + PB(18%) + 股息率(20%) + MA偏离(12%) + 量能(8%) + 波动率(8%) + 订单增速(15%) + ROE(4%)',
        'weights': {'pe': 0.15, 'pb': 0.18, 'dividend_yield': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'order_growth': 0.15, 'roe': 0.04},
    },
    'bank': {
        'name': '银行保险',
        'desc': '重资产金融业态，PB+ROE为估值核心，资产质量是关键风险变量',
        'weights_label': 'PB(30%) + ROE(25%) + 股息率(15%) + 不良/偿付(12%) + MA偏离(10%) + 波动率(8%)',
        'weights': {'pe': 0.00, 'pb': 0.30, 'roe': 0.25, 'dividend_yield': 0.15, 'npl_ratio': 0.12, 'ma': 0.10, 'vola': 0.08},
    },
    'realestate': {
        'name': '地产',
        'desc': '重资产高杠杆，NAV折价与去化率决定估值中枢',
        'weights_label': 'NAV折价(25%) + PB(20%) + 去化率(20%) + MA偏离(12%) + 量能(8%) + 杠杆率(10%) + 波动率(5%)',
        'weights': {'pe': 0.00, 'pb': 0.20, 'nav_discount': 0.25, 'clearance_rate': 0.20, 'ma': 0.12, 'vol': 0.08, 'leverage': 0.10, 'vola': 0.05},
    },
    'pharma': {
        'name': '医药消费',
        'desc': '政策敏感、研发驱动，营收增速与PEG反映成长预期',
        'weights_label': 'PE(20%) + PB(10%) + PEG(25%) + MA偏离(12%) + 量能(8%) + 波动率(8%) + 营收增速(17%)',
        'weights': {'pe': 0.20, 'pb': 0.10, 'peg': 0.25, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'revenue_growth': 0.17},
    },
}

# 因子中文名映射
FACTOR_NAMES = {
    'pe': 'PE(TTM)', 'pb': 'PB', 'peg': 'PEG', 'ma': 'MA偏离度', 'vol': '量能',
    'vola': '波动率', 'commodity_dev': '商品价格偏离', 'capacity_util': '产能利用率',
    'roe': 'ROE', 'dividend_yield': '股息率', 'npl_ratio': '不良/偿付',
    'nav_discount': 'NAV折价', 'clearance_rate': '去化率', 'leverage': '杠杆率',
    'rd_ratio': '研发费用率', 'margin_stability': '毛利率稳定性', 'brand_premium': '品牌溢价度',
    'order_growth': '订单增速', 'revenue_growth': '营收增速',
}

# 可选因子列表（非价格/K线可计算，需要额外输入的因子）
OPTIONAL_FACTOR_KEYS = [
    'commodity_dev', 'capacity_util', 'roe', 'dividend_yield', 'npl_ratio',
    'nav_discount', 'clearance_rate', 'leverage', 'rd_ratio', 'margin_stability',
    'brand_premium', 'order_growth', 'revenue_growth',
]


# ===== 基础因子评分函数（价格/K线可计算，显式传参）=====

def score_pe(pe, pe_min, pe_max):
    """PE评分：PE越低分越高（相对自身历史区间）；超出区间上限时给5分而非0分，避免"超出90th"被展示为估值无意义"""
    if pe <= 0:
        return 50
    if pe_max <= pe_min:
        return 50
    p = (pe - pe_min) / (pe_max - pe_min)
    p = max(0, min(1, p))
    return max(5, (1 - p) * 100)


def score_pb(pb, pb_min, pb_max):
    """PB评分：PB越低分越高（相对自身历史区间）；超出区间上限时给5分而非0分"""
    if pb <= 0:
        return 50
    if pb_max <= pb_min:
        return 50
    p = (pb - pb_min) / (pb_max - pb_min)
    p = max(0, min(1, p))
    return max(5, (1 - p) * 100)


def score_peg(pe, eps_growth):
    """PEG评分：PEG越低分越高"""
    if pe <= 0 or eps_growth <= 0:
        return 50
    peg = pe / (eps_growth * 100)
    if peg < 0.8:
        return 95
    elif peg < 1.0:
        return 80
    elif peg < 1.2:
        return 65
    elif peg < 1.5:
        return 50
    elif peg < 2.0:
        return 35
    else:
        return 20


def score_ma_deviation(close, ma20, ma60):
    """MA偏离度评分：价格低于均线越多分越高（超卖）"""
    if ma20 <= 0:
        return 50
    dev20 = (close - ma20) / ma20 * 100
    dev60 = (close - ma60) / ma60 * 100 if ma60 > 0 else 0
    s20 = max(0, min(100, 50 - dev20 * 3))
    s60 = max(0, min(100, 50 - dev60 * 2.5))
    return s20 * 0.6 + s60 * 0.4


def score_volume(volume, vol_ma20):
    """量能评分：缩量分越高"""
    if vol_ma20 <= 0:
        return 50
    ratio = volume / vol_ma20
    if ratio < 0.5:
        return 85
    elif ratio < 0.8:
        return 70
    elif ratio < 1.2:
        return 55
    elif ratio < 1.5:
        return 40
    elif ratio < 2.0:
        return 30
    else:
        return 20


def score_volatility(close, high, low):
    """波动率评分：波动越低分越高"""
    if close <= 0:
        return 50
    vol = (high - low) / close
    if vol < 0.01:
        return 85
    elif vol < 0.02:
        return 70
    elif vol < 0.03:
        return 55
    elif vol < 0.05:
        return 40
    else:
        return 20


# ===== 可选因子评分函数 =====

def score_roe(roe):
    """ROE评分：ROE越高越好"""
    if roe <= 0:
        return 20
    if roe >= 0.25:
        return 95
    elif roe >= 0.20:
        return 85
    elif roe >= 0.15:
        return 70
    elif roe >= 0.10:
        return 55
    elif roe >= 0.05:
        return 35
    else:
        return 20


def score_dividend_yield(div_yield):
    """股息率评分：越高越好"""
    if div_yield <= 0:
        return 30
    if div_yield >= 0.08:
        return 95
    elif div_yield >= 0.06:
        return 85
    elif div_yield >= 0.04:
        return 70
    elif div_yield >= 0.03:
        return 55
    elif div_yield >= 0.02:
        return 40
    else:
        return 30


def score_rd_ratio(rd_ratio):
    """研发费用率评分：科技/医药越高越好"""
    if rd_ratio <= 0:
        return 30
    if rd_ratio >= 0.15:
        return 95
    elif rd_ratio >= 0.10:
        return 80
    elif rd_ratio >= 0.05:
        return 60
    elif rd_ratio >= 0.03:
        return 45
    else:
        return 30


def score_margin_stability(margin_stability):
    """毛利率稳定性评分：越稳定越好（输入为标准差，越小越好）"""
    if margin_stability <= 0:
        return 90
    elif margin_stability <= 0.01:
        return 80
    elif margin_stability <= 0.02:
        return 65
    elif margin_stability <= 0.05:
        return 50
    elif margin_stability <= 0.10:
        return 35
    else:
        return 20


def score_brand_premium(brand_premium):
    """品牌溢价度评分：毛利率/行业均值，越高越好"""
    if brand_premium <= 0:
        return 20
    if brand_premium >= 3.0:
        return 95
    elif brand_premium >= 2.0:
        return 85
    elif brand_premium >= 1.5:
        return 70
    elif brand_premium >= 1.0:
        return 55
    else:
        return 35


def score_npl_ratio(npl_ratio):
    """不良率评分：越低越好"""
    if npl_ratio <= 0:
        return 95
    elif npl_ratio <= 0.01:
        return 85
    elif npl_ratio <= 0.015:
        return 70
    elif npl_ratio <= 0.02:
        return 55
    elif npl_ratio <= 0.03:
        return 35
    else:
        return 20


def score_nav_discount(nav_discount):
    """NAV折价评分：P/NAV越低越好（<1为低估）"""
    if nav_discount <= 0:
        return 90
    elif nav_discount <= 0.5:
        return 95
    elif nav_discount <= 0.8:
        return 80
    elif nav_discount <= 1.0:
        return 65
    elif nav_discount <= 1.5:
        return 45
    else:
        return 25


def score_clearance_rate(clearance_rate):
    """去化率评分：越高越好"""
    if clearance_rate <= 0:
        return 20
    elif clearance_rate >= 0.80:
        return 90
    elif clearance_rate >= 0.60:
        return 75
    elif clearance_rate >= 0.40:
        return 55
    elif clearance_rate >= 0.20:
        return 35
    else:
        return 20


def score_leverage(leverage):
    """杠杆率评分：有息负债率越低越好"""
    if leverage <= 0:
        return 90
    elif leverage <= 0.30:
        return 80
    elif leverage <= 0.50:
        return 65
    elif leverage <= 0.70:
        return 45
    else:
        return 25


def score_revenue_growth(rev_growth):
    """营收增速评分：越高越好"""
    if rev_growth <= 0:
        return 30
    elif rev_growth >= 0.30:
        return 95
    elif rev_growth >= 0.20:
        return 85
    elif rev_growth >= 0.10:
        return 70
    elif rev_growth >= 0.05:
        return 55
    else:
        return 40


def score_order_growth(order_growth):
    """订单增速评分：越高越好"""
    if order_growth <= 0:
        return 30
    elif order_growth >= 0.30:
        return 95
    elif order_growth >= 0.20:
        return 85
    elif order_growth >= 0.10:
        return 70
    elif order_growth >= 0.05:
        return 55
    else:
        return 40


def score_commodity_dev(commodity_dev):
    """商品价格偏离评分：输入为偏离均值的程度（负值为低于均值=低估），越高越好"""
    if commodity_dev is None:
        return 50
    if commodity_dev <= -0.30:
        return 95
    elif commodity_dev <= -0.20:
        return 80
    elif commodity_dev <= -0.10:
        return 65
    elif commodity_dev <= 0.10:
        return 50
    elif commodity_dev <= 0.20:
        return 35
    else:
        return 20


def score_capacity_util(capacity_util):
    """产能利用率评分：越高越好"""
    if capacity_util is None:
        return 50
    if capacity_util <= 0:
        return 20
    elif capacity_util >= 0.90:
        return 95
    elif capacity_util >= 0.80:
        return 80
    elif capacity_util >= 0.70:
        return 65
    elif capacity_util >= 0.50:
        return 50
    elif capacity_util >= 0.30:
        return 35
    else:
        return 20


# 可选因子评分函数映射
OPTIONAL_SCORE_FUNCS = {
    'roe': score_roe,
    'dividend_yield': score_dividend_yield,
    'rd_ratio': score_rd_ratio,
    'margin_stability': score_margin_stability,
    'brand_premium': score_brand_premium,
    'npl_ratio': score_npl_ratio,
    'nav_discount': score_nav_discount,
    'clearance_rate': score_clearance_rate,
    'leverage': score_leverage,
    'revenue_growth': score_revenue_growth,
    'order_growth': score_order_growth,
    'commodity_dev': score_commodity_dev,
    'capacity_util': score_capacity_util,
}


# ===== 权重处理 =====

def resolve_active_weights(model_weights, factor_values):
    """
    处理缺失可选因子：若模型中某可选因子权重>0但值为None，
    则将其权重按比例均分到已有值的因子，最后归一化使总和为1。

    Args:
        model_weights: dict {factor_key: weight}（模型预设权重）
        factor_values: dict {factor_key: value}（可选因子实际值，None表示缺失）

    Returns:
        dict: 归一化后的活跃权重 {factor_key: weight}
    """
    optional_keys_set = set(OPTIONAL_FACTOR_KEYS)
    active_weights = {}
    missing_weight_sum = 0
    for fk, w in model_weights.items():
        if fk in optional_keys_set:
            if factor_values.get(fk) is not None:
                active_weights[fk] = w
            else:
                missing_weight_sum += w
        else:
            active_weights[fk] = w

    if missing_weight_sum > 0 and active_weights:
        total_active = sum(active_weights.values())
        if total_active > 0:
            for fk in active_weights:
                active_weights[fk] = active_weights[fk] + active_weights[fk] / total_active * missing_weight_sum
    else:
        active_weights = dict(model_weights)

    # 归一化（确保总和为1）
    total_w = sum(active_weights.values())
    if total_w > 0:
        for fk in active_weights:
            active_weights[fk] = active_weights[fk] / total_w
    return active_weights


def build_weights_display(active_weights):
    """根据活跃权重生成展示字符串，如 'PE(TTM)(28%) + PB(12%) + ...'"""
    parts = []
    for fk, w in sorted(active_weights.items(), key=lambda x: -x[1]):
        if w > 0.001:  # 忽略极小权重
            fname = FACTOR_NAMES.get(fk, fk)
            parts.append(f'{fname}({int(round(w * 100))}%)')
    return ' + '.join(parts)


# ===== 核心：每日评分序列 =====

def _series_effective(series, date_str):
    """按披露滞后取年度序列值：T 年年报次年 5 月 1 日生效（A 股年报披露截止 4/30）

    即 1-4 月可用最新年报为 T-2 年，5 月起为 T-1 年；目标年份缺失时只向前回退
    （绝不取未来数据，消除披露时点未来函数）。
    """
    if not series:
        return None
    y = int(date_str[:4])
    m = int(date_str[5:7])
    eff = y - 1 if m >= 5 else y - 2
    for yy in range(eff, eff - 4, -1):  # 只向前回退，绝不向后取未来
        v = series.get(yy)
        if v and v > 0:
            return v
    return None


def compute_daily_scores(kline, active_weights, factor_values, params):
    """
    计算每日估值评分序列。

    Args:
        kline: list of dict {date, open, close, high, low, volume, [is_intraday]}
        active_weights: dict {factor_key: weight}（已归一化）
        factor_values: dict {factor_key: value}（可选因子值）
        params: dict {
            'pe_min', 'pe_max', 'pb_min', 'pb_max': 历史PE/PB区间,
            'eps_growth': 预期增速(小数),
            'latest_price', 'latest_pe', 'latest_pb': 当前价/PE/PB,
            'total_shares': 总股本,
            'eps_series': 可选 dict {year: eps}，提供则用真实历史EPS计算PE（治本），
                          否则回退到“恒定当前EPS”反推（兼容旧行为）,
            'bps_series': 可选 dict {year: bps}，同上用于PB,
            'pe_close_series': 可选 dict {date: 不复权收盘价}，提供则 PE/PB 用当日真实价计算
                          （修复前复权历史价缩放失真）；缺失日期回退 kline close,
            'no_pe_fallback': 可选 bool，True 时无历史EPS/BPS的日期 PE/PB 计中性分0，
                          禁用“当前EPS反推”（回测用，消除审计#5未来函数）,
        }

    Returns:
        list of dict {date, close, pe_ttm, pb, market_cap, ma20, ma60, score, is_intraday, s_<factor>}
    """
    pe_min = params['pe_min']
    pe_max = params['pe_max']
    pb_min = params['pb_min']
    pb_max = params['pb_max']
    eps_growth = params['eps_growth']
    latest_price = params['latest_price']
    latest_pe = params['latest_pe']
    latest_pb = params['latest_pb']
    total_shares = params['total_shares']
    eps_series = params.get('eps_series')  # {year: eps} 或 None
    bps_series = params.get('bps_series')  # {year: bps} 或 None
    pe_close_series = params.get('pe_close_series')  # {date: 不复权收盘价} 或 None
    no_pe_fallback = params.get('no_pe_fallback', False)  # 回测禁用“当前EPS反推”

    n = len(kline)
    results = []
    for i in range(n):
        row = kline[i]
        close, high, low, volume = row['close'], row['high'], row['low'], row['volume']
        ma20 = sum(kline[j]['close'] for j in range(max(0, i - 19), i + 1)) / min(20, i + 1)
        ma60 = sum(kline[j]['close'] for j in range(max(0, i - 59), i + 1)) / min(60, i + 1)
        vol_ma20 = sum(kline[j]['volume'] for j in range(max(0, i - 19), i + 1)) / min(20, i + 1)

        # PE/PB：优先用真实历史EPS/BPS（按披露时点生效），否则回退到恒定当前EPS反推
        # （no_pe_fallback=True 时无历史数据计中性分，回测消除未来函数）
        hist_eps = _series_effective(eps_series, row['date'])
        hist_bps = _series_effective(bps_series, row['date'])
        # 当日真实交易价（不复权），缺失时回退前复权收盘价
        pe_price = close
        if pe_close_series:
            pe_price = pe_close_series.get(row['date']) or close

        if hist_eps and hist_eps > 0:
            pe_ttm = pe_price / hist_eps
        elif no_pe_fallback:
            pe_ttm = 0  # 缺历史EPS：PE因子计中性分，不引入未来信息
        else:
            pe_ttm = pe_price / latest_price * latest_pe if latest_price > 0 else 0

        if hist_bps and hist_bps > 0:
            pb = pe_price / hist_bps
        elif no_pe_fallback:
            pb = 0
        else:
            pb = pe_price / latest_price * latest_pb if latest_price > 0 else 0

        mcap = close * total_shares

        # 盘中虚拟点：剔除量能因子（成交量不完整），权重重新归一化
        is_intraday = row.get('is_intraday', False)
        if is_intraday and active_weights.get('vol', 0) > 0:
            cur_weights = {k: v for k, v in active_weights.items() if k != 'vol'}
            tw = sum(cur_weights.values())
            if tw > 0:
                cur_weights = {k: v / tw for k, v in cur_weights.items()}
        else:
            cur_weights = active_weights

        # 计算各因子得分
        factor_scores = {}
        total = 0
        for fk, w in cur_weights.items():
            if w < 0.001:  # 跳过权重为0的因子
                factor_scores[fk] = 0
                continue
            if fk == 'pe':
                s = score_pe(pe_ttm, pe_min, pe_max)
            elif fk == 'pb':
                s = score_pb(pb, pb_min, pb_max)
            elif fk == 'peg':
                s = score_peg(pe_ttm, eps_growth)
            elif fk == 'ma':
                s = score_ma_deviation(close, ma20, ma60)
            elif fk == 'vol':
                s = score_volume(volume, vol_ma20)
            elif fk == 'vola':
                s = score_volatility(close, high, low)
            elif fk == 'dividend_yield':
                # 动态股息率：若提供每股分红DPS，用 dps/当日收盘价 逐日计算（A2方案，2026-08）
                # 历史股价低点 → 股息率自动升高 → 高分，与估值目标同向；无DPS时退化为恒定当前股息率
                _dps = params.get('dps')
                if _dps and _dps > 0 and close > 0:
                    s = score_dividend_yield(_dps / close)
                else:
                    s = OPTIONAL_SCORE_FUNCS[fk](factor_values.get(fk) or 0)
            elif fk in OPTIONAL_SCORE_FUNCS and factor_values.get(fk) is not None:
                s = OPTIONAL_SCORE_FUNCS[fk](factor_values[fk])
            else:
                s = 50  # 缺失数据默认中性分
            factor_scores[fk] = s
            total += s * w

        total = round(total, 2)
        result_entry = {
            'date': row['date'], 'close': close, 'pe_ttm': round(pe_ttm, 2), 'pb': round(pb, 2),
            'market_cap': round(mcap, 2), 'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
            'score': total,
            'is_intraday': is_intraday,
        }
        # 只记录权重>0的因子分数
        for fk, w in cur_weights.items():
            if w > 0.001:
                result_entry[f's_{fk}'] = round(factor_scores.get(fk, 0), 1)
        results.append(result_entry)

    return results
