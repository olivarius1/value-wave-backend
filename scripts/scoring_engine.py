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

from bisect import bisect_right
from datetime import datetime as _dt, timedelta as _td
from statistics import median as _median

# ===== 8种模型权重预设 =====
# 2026-09 因子审计：剔除量能(vol, IC≈0)与波动率(vola, IC为负)两技术因子——
# 回测 250 日 pooled IC：量能+0.004 纯噪声、波动率-0.12 方向相反（见 .qoder/plans 因子审计记录），
# 权重按比例摊回估值/基本面因子；ma 保留（60日 IC=0.04，唯一有短周期择时信息的技术因子）。
MODEL_PRESETS = {
    'staples': {
        'name': '必选消费',
        'desc': '需求刚性、业绩稳定、现金流充沛，PE+毛利率稳定性为估值锚',
        'weights_label': 'PE(34.2%) + PB(14.6%) + PEG(24.4%) + MA偏离(14.6%) + 毛利率稳定性(12.2%)',
        'weights': {'pe': 0.342, 'pb': 0.146, 'peg': 0.244, 'ma': 0.146, 'margin_stability': 0.122},
    },
    'discretionary': {
        'name': '可选消费',
        'desc': '品牌溢价显著、受消费周期影响，PEG与品牌力为核心估值锚',
        'weights_label': 'PE(26.9%) + PB(14.6%) + PEG(26.8%) + MA偏离(18.3%) + 品牌溢价度(13.4%)',
        'weights': {'pe': 0.269, 'pb': 0.146, 'peg': 0.268, 'ma': 0.183, 'brand_premium': 0.134},
    },
    'tech': {
        'name': '科技制造',
        'desc': '高研发投入、高增速，PEG为最敏感因子，关注成长确定性',
        'weights_label': 'PE(24.4%) + PB(14.6%) + PEG(30.5%) + MA偏离(18.3%) + 研发费用率(12.2%)',
        'weights': {'pe': 0.244, 'pb': 0.146, 'peg': 0.305, 'ma': 0.183, 'rd_ratio': 0.122},
    },
    'cyclical': {
        'name': '周期资源',
        'desc': '盈利随大宗商品价格大幅波动，需追踪商品价格位置与产能周期；股息率修正股东回报',
        'weights_label': 'PE(30.1%) + PB(14.4%) + 商品价格偏离(24.1%) + MA偏离(18%) + 股息率(13.4%)',
        'weights': {'pe': 0.301, 'pb': 0.144, 'commodity_dev': 0.241, 'ma': 0.18, 'dividend_yield': 0.134},
    },
    'soe': {
        'name': '央企基建',
        'desc': '高股息、订单驱动、经营稳健，股息率与PB为估值核心',
        'weights_label': 'PE(18.7%) + PB(22.5%) + 股息率(25.1%) + MA偏离(15%) + 订单增速(18.7%)',
        'weights': {'pe': 0.187, 'pb': 0.225, 'dividend_yield': 0.251, 'ma': 0.15, 'order_growth': 0.187},
    },
    'bank': {
        'name': '银行保险',
        'desc': '重资产金融业态，PB+ROE为估值核心，资产质量是关键风险变量',
        'weights_label': 'PB(32.6%) + ROE(27.2%) + 股息率(16.3%) + 不良/偿付(13%) + MA偏离(10.9%)',
        'weights': {'pb': 0.326, 'roe': 0.272, 'dividend_yield': 0.163, 'npl_ratio': 0.13, 'ma': 0.109},
    },
    'realestate': {
        'name': '地产',
        'desc': '重资产高杠杆，NAV折价与去化率决定估值中枢',
        'weights_label': 'NAV折价(28.7%) + PB(23%) + 去化率(23%) + MA偏离(13.8%) + 杠杆率(11.5%)',
        'weights': {'pb': 0.23, 'nav_discount': 0.287, 'clearance_rate': 0.23, 'ma': 0.138, 'leverage': 0.115},
    },
    'pharma': {
        'name': '医药消费',
        'desc': '政策敏感、研发驱动，营收增速与PEG反映成长预期',
        'weights_label': 'PE(23.8%) + PB(11.9%) + PEG(29.8%) + MA偏离(14.3%) + 营收增速(20.2%)',
        'weights': {'pe': 0.238, 'pb': 0.119, 'peg': 0.298, 'ma': 0.143, 'revenue_growth': 0.202},
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


def score_pe_rank(percentile):
    """PE历史百分位rank评分：percentile 0%(历史最便宜)->100分，100%(最贵)->5分。
    替代固定区间线性映射：极值不截断、永不触顶，保持区分度（解决“股价更便宜但分数不变”饱和）"""
    return max(5, (1 - percentile) * 100)


def score_pb_rank(percentile):
    """PB历史百分位rank评分：percentile 0%(历史最便宜)->100分，100%(最贵)->5分"""
    return max(5, (1 - percentile) * 100)


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


def ret_std20(closes):
    """近20日日收益率总体标准差（需21个收盘价）；样本不足或含非正值返回 None（中性）"""
    if len(closes) < 21 or any(c <= 0 for c in closes):
        return None
    rets = [closes[j] / closes[j - 1] - 1 for j in range(1, len(closes))]
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5


def score_volatility(ret_std20):
    """波动率评分：20日滚动日收益率标准差，波动越低分越高。
    （2026-09审计：旧版用单日振幅(high-low)/close，噪声过大，非真实波动率口径）"""
    if ret_std20 is None:
        return 50
    if ret_std20 < 0.01:
        return 85
    elif ret_std20 < 0.02:
        return 70
    elif ret_std20 < 0.03:
        return 55
    elif ret_std20 < 0.05:
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

    series 兼容两种键：
    - {year: value} 年份键：按生效年回退查表（原始年报口径）
    - {'YYYY-MM-DD': value} 日期键：build_adjusted_series 逐日重述序列
      （送转/派息滚动重述），直接查表，缺失日期（停牌等）向前回退最近可用值
    """
    if not series:
        return None
    if isinstance(next(iter(series)), str) and '-' in next(iter(series)):
        d0 = _dt.strptime(date_str, '%Y-%m-%d')
        for k in range(31):
            v = series.get((d0 - _td(days=k)).strftime('%Y-%m-%d'))
            if v is not None and v > 0:
                return v
        return None
    y = int(date_str[:4])
    m = int(date_str[5:7])
    eff = y - 1 if m >= 5 else y - 2
    for yy in range(eff, eff - 4, -1):  # 只向前回退，绝不向后取未来
        v = series.get(yy)
        if v and v > 0:
            return v
    return None


# ===== 盈利换挡检测（regime window，方案见 .qoder/plans/regime-auto-window_3dccb72f.md）=====
REGIME_SEGMENT_YEARS = 5   # 按序列首年对齐每5年一段取中位数（抗单年极端值）
REGIME_RATIO = 3.0         # 相邻段中位数比值阈值：>3 记向上换挡（≈年化25%，与正常成长拉开距离）
REGIME_MIN_DAYS = 50       # 换挡窗口内PE样本下限，不足时pe因子计中性分
# 披露生效月份与 backtest_engine.DISCLOSURE_MONTH 同源（A股年报披露截止4/30，T年年报次年5月生效）。
# 此处定义本地常量而不反向 import backtest_engine：它会 import scoring_engine，反向导入成环
REGIME_DISCLOSURE_MONTH = 5


def detect_regime_window(profit_series):
    """年报净利润序列的向上盈利换挡检测（供 report_generator 与后续全市场扫描复用）

    必须传年报净利润序列 {归属年: 净利润}：fetch_financial_reports 结果过滤
    report_type=='annual'，键取 report_date 前4位。净利润总额不受送转稀释影响，
    且直接用归属年，避开逐日重述序列的日期键与生效年偏移问题。

    规则：按序列首年对齐每 REGIME_SEGMENT_YEARS 年一段取段中位数；尾部不满一段的残段
    不参与判定（保守方向：宁可窗口多含旧数据，也不错切）；相邻段中位数比值
    > REGIME_RATIO 记向上换挡点（后段起始年，取最后一次）；< 1/REGIME_RATIO 记向下回落，
    只标注不切窗（周期股回落段切窗会使周期底部拿到最差评分）；任一段中位数 <= 0
    视为含亏损段，比值比较失真，跳过判定。

    Returns:
        (window_start, reason): window_start 为换挡段起始年（int）或 None；
        reason ∈ {regime_switch, no_switch, downward_skip, loss_period_skip,
                   insufficient_history, no_data}
    """
    if not profit_series:
        return None, 'no_data'
    years = sorted(profit_series)
    first, last = years[0], years[-1]
    n_full = (last - first + 1) // REGIME_SEGMENT_YEARS  # 完整段数（尾部残段不计）
    if n_full < 2:
        return None, 'insufficient_history'
    # 各完整段中位数；段内缺年用现有样本，整段无样本记 None（跳过涉及它的相邻比较）
    seg_medians = []
    for i in range(n_full):
        s = first + i * REGIME_SEGMENT_YEARS
        vals = [profit_series[y] for y in range(s, s + REGIME_SEGMENT_YEARS) if y in profit_series]
        seg_medians.append((s, _median(vals) if vals else None))
    if any(m is not None and m <= 0 for _, m in seg_medians):
        return None, 'loss_period_skip'
    switch_years, downward = [], False
    for i in range(1, len(seg_medians)):
        _, m_prev = seg_medians[i - 1]
        s_cur, m_cur = seg_medians[i]
        if not m_prev or not m_cur:
            continue
        ratio = m_cur / m_prev
        if ratio < 1.0 / REGIME_RATIO:
            downward = True
        elif ratio > REGIME_RATIO:
            switch_years.append(s_cur)
    if downward:
        return None, 'downward_skip'
    if switch_years:
        return switch_years[-1], 'regime_switch'
    return None, 'no_switch'


def compute_daily_scores(kline, active_weights, factor_values, params, regime_info_out=None):
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
            'use_rank_pe': 可选 bool，True 时 PE 因子用历史百分位rank映射（同口径序列），
                          False 用固定区间线性映射（手动区间时保留），
            'use_rank_pb': 可选 bool，同上用于PB,
            'no_pe_fallback': 可选 bool，True 时无历史EPS/BPS的日期 PE/PB 计中性分0，
                          禁用“当前EPS反推”（回测用，消除审计#5未来函数）,
            'regime_window_start': 可选 int，盈利换挡生效年份（detect_regime_window 返回）。提供且
                          use_rank_pe 时，PE 分位只用 {start+1}-05-01 之后的子序列（对齐年报披露
                          生效时点）；窗口样本 < REGIME_MIN_DAYS 时 pe 因子计中性50分。PB 不受影响。
                          不传时行为与无窗口版本逐点一致（回测不传，保持PIT纯净）,
        }
        regime_info_out: 可选 dict，传入则填充 {'window_start', 'unstable', 'window_days'}
                （unstable=窗口样本不足致pe中性）。默认 None 不填充，现有调用方零影响。

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
    use_rank_pe = params.get('use_rank_pe', False)  # PE因子：历史百分位rank映射
    use_rank_pb = params.get('use_rank_pb', False)  # PB因子：历史百分位rank映射
    regime_window_start = params.get('regime_window_start')  # 盈利换挡窗口起始年或 None

    def _calc_pe_pb(date_str, close):
        """当日真实PE/PB：披露滞后EPS/BPS + 不复权真实价（rank预收集与主循环共用同一口径）"""
        h_eps = _series_effective(eps_series, date_str)
        h_bps = _series_effective(bps_series, date_str)
        p_price = close
        if pe_close_series:
            p_price = pe_close_series.get(date_str) or close
        if h_eps and h_eps > 0:
            _pe = p_price / h_eps
        elif no_pe_fallback:
            _pe = 0  # 缺历史EPS：PE因子计中性分，不引入未来信息
        else:
            _pe = p_price / latest_price * latest_pe if latest_price > 0 else 0
        if h_bps and h_bps > 0:
            _pb = p_price / h_bps
        elif no_pe_fallback:
            _pb = 0
        else:
            _pb = p_price / latest_price * latest_pb if latest_price > 0 else 0
        return _pe, _pb

    # rank 百分位预收集：全历史序列排序，主循环对每个日期二分求百分位
    # （与主循环同口径计算，避免区间口径不一致；解决固定区间截断导致的触顶饱和）
    _pe_rank_sorted = _pb_rank_sorted = None
    # PE 分位换挡窗口：仅 PE 排序数组走窗口（盈利换挡改变 PE 的分母，净资产是存量，
    # PB 锚仍可用）；切窗对齐年报披露时点（window_start 年年报次年 5 月生效，
    # 与 _series_effective 同口径，避免窗口头部几个月 PE 分母还是旧年报盈利的口径断裂）
    _pe_window_from = None
    if regime_window_start is not None:
        _pe_window_from = f'{regime_window_start + 1}-{REGIME_DISCLOSURE_MONTH:02d}-01'
    _pe_hist = []
    if use_rank_pe or use_rank_pb:
        _pb_hist = []
        for _r in kline:
            _pe, _pb = _calc_pe_pb(_r['date'], _r['close'])
            if use_rank_pe and _pe > 0 and (_pe_window_from is None or _r['date'] >= _pe_window_from):
                _pe_hist.append(_pe)
            if use_rank_pb and _pb > 0:
                _pb_hist.append(_pb)
        if use_rank_pe and _pe_hist:
            _pe_rank_sorted = sorted(_pe_hist)
        if use_rank_pb and _pb_hist:
            _pb_rank_sorted = sorted(_pb_hist)
    # 窗口内 PE 样本不足：rank 不可用，pe 因子计中性分、pe_pct 置 None（PB 不受影响，
    # 刚换挡的股票不至于丢掉所有估值锚）；不传窗口时保持旧行为（回归保证）
    _pe_window_insufficient = use_rank_pe and _pe_window_from is not None and len(_pe_hist) < REGIME_MIN_DAYS
    if _pe_window_insufficient:
        _pe_rank_sorted = None
    if regime_info_out is not None:
        regime_info_out['window_start'] = regime_window_start
        regime_info_out['unstable'] = bool(_pe_window_insufficient)
        regime_info_out['window_days'] = len(_pe_hist) if use_rank_pe else 0

    n = len(kline)
    results = []
    for i in range(n):
        row = kline[i]
        close, high, low, volume = row['close'], row['high'], row['low'], row['volume']
        ma20 = sum(kline[j]['close'] for j in range(max(0, i - 19), i + 1)) / min(20, i + 1)
        ma60 = sum(kline[j]['close'] for j in range(max(0, i - 59), i + 1)) / min(60, i + 1)
        vol_ma20 = sum(kline[j]['volume'] for j in range(max(0, i - 19), i + 1)) / min(20, i + 1)
        # 波动率：近20日日收益率总体标准差（样本不足计中性，不给噪声分）
        vola20 = ret_std20([kline[j]['close'] for j in range(max(0, i - 20), i + 1)])

        # PE/PB：优先用真实历史EPS/BPS（按披露时点生效），否则回退到恒定当前EPS反推
        # （no_pe_fallback=True 时无历史数据计中性分，回测消除未来函数）
        pe_ttm, pb = _calc_pe_pb(row['date'], close)

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
                if use_rank_pe and _pe_window_insufficient:
                    s = 50  # 换挡窗口内PE样本不足：中性分，不让刚换挡的股票丢掉PB以外的估值锚
                elif use_rank_pe and _pe_rank_sorted and pe_ttm > 0:
                    s = score_pe_rank(bisect_right(_pe_rank_sorted, pe_ttm) / len(_pe_rank_sorted))
                else:
                    s = score_pe(pe_ttm, pe_min, pe_max)
            elif fk == 'pb':
                if use_rank_pb and _pb_rank_sorted and pb > 0:
                    s = score_pb_rank(bisect_right(_pb_rank_sorted, pb) / len(_pb_rank_sorted))
                else:
                    s = score_pb(pb, pb_min, pb_max)
            elif fk == 'peg':
                s = score_peg(pe_ttm, eps_growth)
            elif fk == 'ma':
                s = score_ma_deviation(close, ma20, ma60)
            elif fk == 'vol':
                s = score_volume(volume, vol_ma20)
            elif fk == 'vola':
                s = score_volatility(vola20)
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
        # 当前PE/PB的历史百分位（rank模式），避免固定区间映射在极值区触顶饱和（满分无区分度）
        _pe_pct = round(bisect_right(_pe_rank_sorted, pe_ttm) / len(_pe_rank_sorted) * 100) if (use_rank_pe and _pe_rank_sorted and pe_ttm > 0) else None
        _pb_pct = round(bisect_right(_pb_rank_sorted, pb) / len(_pb_rank_sorted) * 100) if (use_rank_pb and _pb_rank_sorted and pb > 0) else None
        result_entry = {
            'date': row['date'], 'close': close, 'pe_ttm': round(pe_ttm, 2), 'pb': round(pb, 2),
            'market_cap': round(mcap, 2), 'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
            'score': total, 'pe_pct': _pe_pct, 'pb_pct': _pb_pct,
            'is_intraday': is_intraday,
        }
        # 只记录权重>0的因子分数
        for fk, w in cur_weights.items():
            if w > 0.001:
                result_entry[f's_{fk}'] = round(factor_scores.get(fk, 0), 1)
        results.append(result_entry)

    return results
