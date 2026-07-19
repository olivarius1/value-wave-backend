#!/usr/bin/env python3
"""
通用估值报告生成器
基于现有报告结构，生成新的自包含HTML报告
支持8种估值模型：staples/discretionary/tech/cyclical/soe/bank/realestate/pharma
"""
import json, math, os, sys, re

# ===== 8种模型权重预设 =====
MODEL_PRESETS = {
    'staples': {
        'name': '必选消费',
        'weights_label': 'PE(28%) + PB(12%) + PEG(20%) + MA偏离(12%) + 量能(8%) + 波动率(10%) + 毛利率稳定性(10%)',
        'weights': {'pe': 0.28, 'pb': 0.12, 'peg': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.10, 'margin_stability': 0.10},
    },
    'discretionary': {
        'name': '可选消费',
        'weights_label': 'PE(22%) + PB(12%) + PEG(22%) + MA偏离(15%) + 量能(8%) + 波动率(10%) + 品牌溢价度(11%)',
        'weights': {'pe': 0.22, 'pb': 0.12, 'peg': 0.22, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'brand_premium': 0.11},
    },
    'tech': {
        'name': '科技制造',
        'weights_label': 'PE(20%) + PB(12%) + PEG(25%) + MA偏离(15%) + 量能(8%) + 波动率(10%) + 研发费用率(10%)',
        'weights': {'pe': 0.20, 'pb': 0.12, 'peg': 0.25, 'ma': 0.15, 'vol': 0.08, 'vola': 0.10, 'rd_ratio': 0.10},
    },
    'cyclical': {
        'name': '周期资源',
        'weights_label': 'PE(25%) + PB(12%) + 商品价格偏离(20%) + MA偏离(15%) + 量能(10%) + 波动率(10%) + 产能利用率(8%)',
        'weights': {'pe': 0.25, 'pb': 0.12, 'commodity_dev': 0.20, 'ma': 0.15, 'vol': 0.10, 'vola': 0.10, 'capacity_util': 0.08},
    },
    'soe': {
        'name': '央企基建',
        'weights_label': 'PE(15%) + PB(18%) + 股息率(20%) + MA偏离(12%) + 量能(8%) + 波动率(8%) + 订单增速(15%) + ROE(4%)',
        'weights': {'pe': 0.15, 'pb': 0.18, 'dividend_yield': 0.20, 'ma': 0.12, 'vol': 0.08, 'vola': 0.08, 'order_growth': 0.15, 'roe': 0.04},
    },
    'bank': {
        'name': '银行保险',
        'weights_label': 'PB(30%) + ROE(25%) + 股息率(15%) + 不良/偿付(12%) + MA偏离(10%) + 波动率(8%)',
        'weights': {'pe': 0.00, 'pb': 0.30, 'roe': 0.25, 'dividend_yield': 0.15, 'npl_ratio': 0.12, 'ma': 0.10, 'vola': 0.08},
    },
    'realestate': {
        'name': '地产',
        'weights_label': 'NAV折价(25%) + PB(20%) + 去化率(20%) + MA偏离(12%) + 量能(8%) + 杠杆率(10%) + 波动率(5%)',
        'weights': {'pe': 0.00, 'pb': 0.20, 'nav_discount': 0.25, 'clearance_rate': 0.20, 'ma': 0.12, 'vol': 0.08, 'leverage': 0.10, 'vola': 0.05},
    },
    'pharma': {
        'name': '医药消费',
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

# ===== 配置区 =====
# 支持两种模式：
# 1. 新格式：build_report.py 设置 _REPORT_CONFIG dict 后 exec 本文件
# 2. 旧格式：直接从 sys.argv 解析 16+ 个位置参数

_cfg = globals().get('_REPORT_CONFIG')
if _cfg is not None:
    # 新模式：从 config dict 读取
    STOCK_CODE = _cfg['code']
    STOCK_NAME = _cfg['name']
    EXCHANGE = _cfg['exchange']
    TOTAL_SHARES = _cfg['total_shares']
    PE_MIN = _cfg['pe_min']
    PE_MAX = _cfg['pe_max']
    PB_MIN = _cfg['pb_min']
    PB_MAX = _cfg['pb_max']
    EPS_GROWTH = _cfg['eps_growth']
    REVENUE = str(_cfg['revenue'])
    NET_PROFIT = str(_cfg['net_profit'])
    GROSS_MARGIN = str(_cfg['gross_margin'])
    MARKET_CAP = str(_cfg['market_cap'])
    INDUSTRY = _cfg['industry']
    SUBTITLE = _cfg['subtitle']
    MODEL_TYPE = _cfg['model']
    optional_factors = _cfg.get('optional_factors', {})
    KLINE_FILES = _cfg.get('kline_files', [])
    # 新模式可能直接提供K线数据（从缓存）
    _KLINE_DATA_FROM_CACHE = _cfg.get('kline_data')  # [[date,o,c,h,l,v],...]
    _QT_PE = _cfg.get('qt_pe', 0)
    _QT_PB = _cfg.get('qt_pb', 0)
    _QT_PRICE = _cfg.get('qt_price', 0)
else:
    # 旧模式：从 sys.argv 解析
    STOCK_CODE = sys.argv[1]
    STOCK_NAME = sys.argv[2]
    EXCHANGE = sys.argv[3]
    TOTAL_SHARES = float(sys.argv[4])
    PE_MIN = float(sys.argv[5])
    PE_MAX = float(sys.argv[6])
    PB_MIN = float(sys.argv[7])
    PB_MAX = float(sys.argv[8])
    EPS_GROWTH = float(sys.argv[9])
    REVENUE = sys.argv[10]
    NET_PROFIT = sys.argv[11]
    GROSS_MARGIN = sys.argv[12]
    MARKET_CAP = sys.argv[13]
    INDUSTRY = sys.argv[14]
    SUBTITLE = sys.argv[15]
    MODEL_TYPE = sys.argv[16]
    # 解析剩余参数
    optional_factors = {}
    kline_args = []
    i = 17
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg.startswith('--'):
            factor_arg = arg[2:]
            if ':' in factor_arg:
                fkey, fval = factor_arg.split(':', 1)
                try:
                    optional_factors[fkey] = float(fval)
                except ValueError:
                    pass
        else:
            kline_args.append(arg)
        i += 1
    KLINE_FILES = kline_args
    _KLINE_DATA_FROM_CACHE = None
    _QT_PE = 0
    _QT_PB = 0
    _QT_PRICE = 0

# 兼容旧参数：growth -> staples
if MODEL_TYPE == 'growth':
    MODEL_TYPE = 'staples'

# 提前初始化 factor_values（供财务报表模块使用）
factor_values = dict(optional_factors)

# ===== 财务报表数据获取 & 自动填充因子 =====
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_financial_metrics = {}
try:
    from financial_fetcher import fetch_financial_reports, compute_financial_metrics, auto_fill_factors
    print(f"  获取{STOCK_NAME}财务报表数据...")
    _reports = fetch_financial_reports(STOCK_CODE, EXCHANGE)
    if _reports:
        _financial_metrics = compute_financial_metrics(_reports)
        print(f"  获取到 {_financial_metrics.get('annual_reports_count', 0)} 份年报")
        # 用财务数据自动填充用户未指定的可选因子
        optional_factors = auto_fill_factors(optional_factors, _financial_metrics, MODEL_TYPE)
        # 同步到 factor_values
        for fk, fv in optional_factors.items():
            if fv is not None:
                factor_values[fk] = fv
except ImportError:
    print("  [info] financial_fetcher未找到，跳过财务报表自动分析")
except Exception as e:
    print(f"  [warn] 财务报表获取失败: {e}")

# 输出文件（基于skill scripts目录，输出到 skill reports/ 或项目 local_reports/）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_PROJECT_ROOT = _SKILL_DIR  # 独立项目，根目录即skill目录
_OUTPUT_DIR = os.path.join(_PROJECT_ROOT, 'local_reports')
os.makedirs(_OUTPUT_DIR, exist_ok=True)
OUTPUT = os.path.join(_OUTPUT_DIR, f'{STOCK_NAME}{STOCK_CODE}-valuation.html')

# 根据模型类型获取权重预设
if MODEL_TYPE not in MODEL_PRESETS:
    print(f"  警告: 未知模型类型 '{MODEL_TYPE}'，默认使用 staples")
    MODEL_TYPE = 'staples'

preset = MODEL_PRESETS[MODEL_TYPE]
MODEL_NAME = preset['name']
WEIGHTS_LABEL = preset['weights_label']
model_weights = dict(preset['weights'])  # 复制一份

# 合并可选因子到权重中（可选因子暂无预设值，需要用户提供）
# 检查模型中权重>0的因子哪些需要可选参数但用户提供了
for fkey, fval in optional_factors.items():
    if fkey in model_weights and model_weights[fkey] > 0:
        # 用户覆盖了某个可选因子的原始值，记录下来
        pass

# 可选因子列表（非价格/K线可计算，需要额外输入的因子）
OPTIONAL_FACTOR_KEYS = [
    'commodity_dev', 'capacity_util', 'roe', 'dividend_yield', 'npl_ratio',
    'nav_discount', 'clearance_rate', 'leverage', 'rd_ratio', 'margin_stability',
    'brand_premium', 'order_growth', 'revenue_growth',
]

# 合并 factor_values（保留财务数据自动填充的值）
for fk in OPTIONAL_FACTOR_KEYS:
    if fk in optional_factors and optional_factors[fk] is not None:
        factor_values[fk] = optional_factors[fk]
    elif fk not in factor_values:
        factor_values[fk] = None  # 标记为缺失

# 处理缺失的可选因子：如果模型中该因子权重>0但值为None，则将其权重均分到已有值的因子
_optional_keys_set = set(OPTIONAL_FACTOR_KEYS)
active_weights = {}
missing_weight_sum = 0
for fk, w in model_weights.items():
    if fk in _optional_keys_set:
        if factor_values.get(fk) is not None:
            active_weights[fk] = w
        else:
            missing_weight_sum += w
    else:
        active_weights[fk] = w

if missing_weight_sum > 0 and active_weights:
    # 将缺失因子的权重按比例均分到已有因子
    total_active = sum(active_weights.values())
    if total_active > 0:
        for fk in active_weights:
            active_weights[fk] = active_weights[fk] + active_weights[fk] / total_active * missing_weight_sum
else:
    active_weights = model_weights

# 归一化权重（确保总和为1）
total_w = sum(active_weights.values())
if total_w > 0:
    for fk in active_weights:
        active_weights[fk] = active_weights[fk] / total_w

# 生成用于显示的权重标签（动态）
weight_parts = []
for fk, w in sorted(active_weights.items(), key=lambda x: -x[1]):
    if w > 0.001:  # 忽略极小权重
        fname = FACTOR_NAMES.get(fk, fk)
        weight_parts.append(f'{fname}({int(round(w*100))}%)')
WEIGHTS_DISPLAY = ' + '.join(weight_parts)

# ===== K线数据获取 =====
# 支持三种模式：
# 1. 缓存模式：build_report.py 已通过 kline_cache 获取数据
# 2. JSON文件模式：直接传入K线JSON文件
# 3. 10年自动获取模式：未传K线文件时自动拉取10年数据

full_code = EXCHANGE + STOCK_CODE
all_kline = []
seen_dates = set()
latest_pe = 0
latest_pb = 0
latest_price = 0
_has_intraday = False

if _KLINE_DATA_FROM_CACHE:
    # 缓存模式：直接使用已获取的K线数据
    all_kline = _KLINE_DATA_FROM_CACHE
    latest_pe = _QT_PE
    latest_pb = _QT_PB
    latest_price = _QT_PRICE
    print(f"  使用缓存K线数据: {len(all_kline)}天")

else:
    # 非缓存模式：从文件获取或自动拉取
    if not KLINE_FILES:
        try:
            from financial_fetcher import fetch_kline_batches, generate_kline_batches
            print(f"  未提供K线文件，自动获取最近10年K线数据...")
            _batches = generate_kline_batches(STOCK_CODE, EXCHANGE, years=10)
            _kline_files = fetch_kline_batches(STOCK_CODE, EXCHANGE, _batches, _OUTPUT_DIR)
            if _kline_files:
                KLINE_FILES = _kline_files
                print(f"  获取到 {len(KLINE_FILES)} 批K线数据")
        except ImportError:
            print("  [info] financial_fetcher未找到，跳过K线自动获取")
        except Exception as e:
            print(f"  [warn] K线自动获取失败: {e}")

    if KLINE_FILES:
        # JSON文件模式（原有逻辑）
        for kf in KLINE_FILES:
            with open(kf) as f:
                jd = json.load(f)
            stock_data = jd['data'][full_code]
            if 'qfqday' in stock_data:
                kdata = stock_data['qfqday']
            elif 'day' in stock_data:
                kdata = stock_data['day']
            else:
                raise KeyError(f"K线数据缺少 qfqday/day 字段，可用键: {list(stock_data.keys())}")
            qt = jd['data'][full_code]['qt'][full_code]
            for row in kdata:
                if row[0] not in seen_dates:
                    all_kline.append(row)
                    seen_dates.add(row[0])
        latest_pe = float(qt[39]) if qt[39] else 0
        latest_pb = float(qt[46]) if qt[46] else 0
        latest_price = float(qt[3])
    else:
        print("错误: 未提供K线JSON文件且自动获取失败")
        print("请检查网络连接或手动提供K线文件")
        sys.exit(1)

if not latest_pe:
    latest_pe = latest_price  # fallback
if not latest_pb:
    latest_pb = 1.0

print(f"{STOCK_NAME}({STOCK_CODE}): {len(all_kline)}天 | 价{latest_price} PE{latest_pe:.1f} PB{latest_pb:.1f}")

# ===== 数据处理 =====
kline = []
for r in all_kline:
    entry = {'date': r[0], 'open': float(r[1]), 'close': float(r[2]), 'high': float(r[3]), 'low': float(r[4]), 'volume': float(r[5])}
    kline.append(entry)
# 标记盘中虚拟点
if _has_intraday and kline:
    kline[-1]['is_intraday'] = True

# ===== 基础因子评分函数（价格/K线可计算） =====

def score_pe(pe):
    if pe <= 0: return 50
    p = (pe - PE_MIN) / (PE_MAX - PE_MIN)
    p = max(0, min(1, p))
    return (1 - p) * 100

def score_pb(pb):
    if pb <= 0: return 50
    p = (pb - PB_MIN) / (PB_MAX - PB_MIN)
    p = max(0, min(1, p))
    return (1 - p) * 100

def score_peg(pe):
    if pe <= 0 or EPS_GROWTH <= 0: return 50
    peg = pe / (EPS_GROWTH * 100)
    if peg < 0.8: return 95
    elif peg < 1.0: return 80
    elif peg < 1.2: return 65
    elif peg < 1.5: return 50
    elif peg < 2.0: return 35
    else: return 20

def score_ma_deviation(close, ma20, ma60):
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

# ===== 可选因子评分函数 =====

def score_roe(roe):
    """ROE评分：ROE越高越好"""
    if roe <= 0: return 20
    if roe >= 0.25: return 95
    elif roe >= 0.20: return 85
    elif roe >= 0.15: return 70
    elif roe >= 0.10: return 55
    elif roe >= 0.05: return 35
    else: return 20

def score_dividend_yield(div_yield):
    """股息率评分：越高越好"""
    if div_yield <= 0: return 30
    if div_yield >= 0.08: return 95
    elif div_yield >= 0.06: return 85
    elif div_yield >= 0.04: return 70
    elif div_yield >= 0.03: return 55
    elif div_yield >= 0.02: return 40
    else: return 30

def score_rd_ratio(rd_ratio):
    """研发费用率评分：科技/医药越高越好"""
    if rd_ratio <= 0: return 30
    if rd_ratio >= 0.15: return 95
    elif rd_ratio >= 0.10: return 80
    elif rd_ratio >= 0.05: return 60
    elif rd_ratio >= 0.03: return 45
    else: return 30

def score_margin_stability(margin_stability):
    """毛利率稳定性评分：越稳定越好（输入为标准差，越小越好）"""
    if margin_stability <= 0: return 90
    elif margin_stability <= 0.01: return 80
    elif margin_stability <= 0.02: return 65
    elif margin_stability <= 0.05: return 50
    elif margin_stability <= 0.10: return 35
    else: return 20

def score_brand_premium(brand_premium):
    """品牌溢价度评分：毛利率/行业均值，越高越好"""
    if brand_premium <= 0: return 20
    if brand_premium >= 3.0: return 95
    elif brand_premium >= 2.0: return 85
    elif brand_premium >= 1.5: return 70
    elif brand_premium >= 1.0: return 55
    else: return 35

def score_npl_ratio(npl_ratio):
    """不良率评分：越低越好"""
    if npl_ratio <= 0: return 95
    elif npl_ratio <= 0.01: return 85
    elif npl_ratio <= 0.015: return 70
    elif npl_ratio <= 0.02: return 55
    elif npl_ratio <= 0.03: return 35
    else: return 20

def score_nav_discount(nav_discount):
    """NAV折价评分：P/NAV越低越好（<1为低估）"""
    if nav_discount <= 0: return 90
    elif nav_discount <= 0.5: return 95
    elif nav_discount <= 0.8: return 80
    elif nav_discount <= 1.0: return 65
    elif nav_discount <= 1.5: return 45
    else: return 25

def score_clearance_rate(clearance_rate):
    """去化率评分：越高越好"""
    if clearance_rate <= 0: return 20
    elif clearance_rate >= 0.80: return 90
    elif clearance_rate >= 0.60: return 75
    elif clearance_rate >= 0.40: return 55
    elif clearance_rate >= 0.20: return 35
    else: return 20

def score_leverage(leverage):
    """杠杆率评分：有息负债率越低越好"""
    if leverage <= 0: return 90
    elif leverage <= 0.30: return 80
    elif leverage <= 0.50: return 65
    elif leverage <= 0.70: return 45
    else: return 25

def score_revenue_growth(rev_growth):
    """营收增速评分：越高越好"""
    if rev_growth <= 0: return 30
    elif rev_growth >= 0.30: return 95
    elif rev_growth >= 0.20: return 85
    elif rev_growth >= 0.10: return 70
    elif rev_growth >= 0.05: return 55
    else: return 40

def score_order_growth(order_growth):
    """订单增速评分：越高越好"""
    if order_growth <= 0: return 30
    elif order_growth >= 0.30: return 95
    elif order_growth >= 0.20: return 85
    elif order_growth >= 0.10: return 70
    elif order_growth >= 0.05: return 55
    else: return 40

def score_commodity_dev(commodity_dev):
    """商品价格偏离评分：输入为偏离均值的程度（负值为低于均值=低估），越高越好"""
    if commodity_dev is None: return 50
    if commodity_dev <= -0.30: return 95
    elif commodity_dev <= -0.20: return 80
    elif commodity_dev <= -0.10: return 65
    elif commodity_dev <= 0.10: return 50
    elif commodity_dev <= 0.20: return 35
    else: return 20

def score_capacity_util(capacity_util):
    """产能利用率评分：越高越好"""
    if capacity_util is None: return 50
    if capacity_util <= 0: return 20
    elif capacity_util >= 0.90: return 95
    elif capacity_util >= 0.80: return 80
    elif capacity_util >= 0.70: return 65
    elif capacity_util >= 0.50: return 50
    elif capacity_util >= 0.30: return 35
    else: return 20

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

# ===== 评分计算循环 =====
n = len(kline)
results = []
for i in range(n):
    row = kline[i]
    close, high, low, volume = row['close'], row['high'], row['low'], row['volume']
    ma20 = sum(kline[j]['close'] for j in range(max(0, i-19), i+1)) / min(20, i+1)
    ma60 = sum(kline[j]['close'] for j in range(max(0, i-59), i+1)) / min(60, i+1)
    vol_ma20 = sum(kline[j]['volume'] for j in range(max(0, i-19), i+1)) / min(20, i+1)
    pe_ttm = close / latest_price * latest_pe if latest_price > 0 else 0
    pb = close / latest_price * latest_pb if latest_price > 0 else 0
    mcap = close * TOTAL_SHARES

    # 盘中虚拟点：剔除量能因子（成交量不完整），权重重新归一化
    is_intraday = row.get('is_intraday', False)
    if is_intraday and 'vol' in active_weights and active_weights['vol'] > 0:
        cur_weights = {k: v for k, v in active_weights.items() if k != 'vol'}
        tw = sum(cur_weights.values())
        if tw > 0:
            cur_weights = {k: v/tw for k, v in cur_weights.items()}
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
            s = score_pe(pe_ttm)
        elif fk == 'pb':
            s = score_pb(pb)
        elif fk == 'peg':
            s = score_peg(pe_ttm)
        elif fk == 'ma':
            s = score_ma_deviation(close, ma20, ma60)
        elif fk == 'vol':
            s = score_volume(volume, vol_ma20)
        elif fk == 'vola':
            s = score_volatility(close, high, low)
        elif fk in OPTIONAL_SCORE_FUNCS and fk in factor_values and factor_values[fk] is not None:
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

scores = [r['score'] for r in results]
print(f"  评分: 均值{sum(scores)/len(scores):.1f} 最低{min(scores):.1f} 最高{max(scores):.1f} 最新{scores[-1]:.1f}")
print(f"  区间: {kline[0]['date']} ~ {kline[-1]['date']}")

# 生成数据JS
# 生成 markPoint 数据（标记盘中虚拟点）
intraday_markpoint = []
for idx_r, r in enumerate(results):
    if r.get('is_intraday'):
        intraday_markpoint.append({
            'name': '盘中实时',
            'coord': [r['date'], r['score']],
            'value': "盘中 %.2f元\\n估值分 %s" % (r['close'], r['score']),
            'itemStyle': {'color': '#f59e0b'},
        })
        break

val_data_js = 'var VALUATION_DATA = ' + json.dumps({
    'meta': {'stock': f'{STOCK_NAME}({STOCK_CODE})', 'period': f"{kline[0]['date']} ~ {kline[-1]['date']}", 'total_days': len(results), 'weights': WEIGHTS_DISPLAY, 'description': '分数0-100，越高代表越被低估',
             'code': STOCK_CODE, 'exchange': EXCHANGE, 'model_type': MODEL_TYPE,
             'pe_min': PE_MIN, 'pe_max': PE_MAX, 'pb_min': PB_MIN, 'pb_max': PB_MAX, 'eps_growth': EPS_GROWTH,
             'total_shares': TOTAL_SHARES},
    'data': results
}, ensure_ascii=False, separators=(',', ':')) + ';'

# 读取ECharts
echarts_path = os.path.join(_SKILL_DIR, '_shared', 'js', 'echarts.min.js')
if not os.path.exists(echarts_path):
    print(f"错误: ECharts库不存在: {echarts_path}")
    sys.exit(1)
with open(echarts_path, 'r', encoding='utf-8') as f:
    echarts_js = f.read()

val_data_js += f"\nvar INTRADAY_MARKPOINT = {json.dumps(intraday_markpoint, ensure_ascii=False)};"

period_label = f"{kline[0]['date'][:4]}.{kline[0]['date'][5:7]} - {kline[-1]['date'][:4]}.{kline[-1]['date'][5:7]}"

# 预计算weights_display用于HTML（不能在f-string中用反斜杠）
weights_display_lines = WEIGHTS_DISPLAY.replace(' + ', '\n             + ')

# ===== 确定评分状态 =====
latest = results[-1]
if latest['score'] >= 80: status_text, status_class = '极度低估', 'fs-score-high'
elif latest['score'] >= 70: status_text, status_class = '低估', 'fs-score-high'
elif latest['score'] >= 60: status_text, status_class = '中性偏低', 'fs-score-mid'
elif latest['score'] >= 40: status_text, status_class = '中性偏高', 'fs-score-mid'
elif latest['score'] >= 20: status_text, status_class = '高估', 'fs-score-low'
else: status_text, status_class = '极度高估', 'fs-score-low'

# 关键日期
key_dates = []
import datetime
base = datetime.date(2024, 10, 8)
for i in range(8):
    d = base + datetime.timedelta(days=i * 90)
    key_dates.append(d.strftime('%Y-%m-%d'))
key_dates_str = ', '.join(f"'{d}'" for d in key_dates)

# 生成动态因子参数表格HTML行
factor_table_rows = []
# 基础因子的说明
factor_descriptions = {
    'pe': f'当前{latest_pe:.1f}倍',
    'pb': f'当前{latest_pb:.2f}倍',
    'peg': f'PEG={round(latest_pe/(EPS_GROWTH*100),2) if latest_pe > 0 and EPS_GROWTH > 0 else "N/A"}',
    'ma': 'MA20 + MA60',
    'vol': '相对20日均量',
    'vola': '日振幅',
    'commodity_dev': f'偏离{factor_values.get("commodity_dev", "N/A")}',
    'capacity_util': f'利用率{factor_values.get("capacity_util", "N/A")}',
    'roe': f'ROE={factor_values.get("roe", "N/A")}',
    'dividend_yield': f'股息率={factor_values.get("dividend_yield", "N/A")}',
    'npl_ratio': f'不良率={factor_values.get("npl_ratio", "N/A")}',
    'nav_discount': f'P/NAV={factor_values.get("nav_discount", "N/A")}',
    'clearance_rate': f'去化率={factor_values.get("clearance_rate", "N/A")}',
    'leverage': f'负债率={factor_values.get("leverage", "N/A")}',
    'rd_ratio': f'研发率={factor_values.get("rd_ratio", "N/A")}',
    'margin_stability': f'标准差={factor_values.get("margin_stability", "N/A")}',
    'brand_premium': f'溢价倍数={factor_values.get("brand_premium", "N/A")}',
    'order_growth': f'增速={factor_values.get("order_growth", "N/A")}',
    'revenue_growth': f'增速={factor_values.get("revenue_growth", "N/A")}',
}

for fk, w in sorted(active_weights.items(), key=lambda x: -x[1]):
    if w < 0.001:
        continue
    fname = FACTOR_NAMES.get(fk, fk)
    w_pct = int(round(w * 100))
    desc = factor_descriptions.get(fk, '-')
    factor_table_rows.append(
        f'      <tr><td>{fname}</td><td>{w_pct}%</td><td>{desc}</td><td>权重>{w_pct}%的因子</td></tr>'
    )
factor_table_html = '\n'.join(factor_table_rows)

# 生成财务报表摘要HTML
_financial_summary_html = ''
if _financial_metrics:
    _fm = _financial_metrics
    _fin_cards = []
    if 'annual_reports_count' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["annual_reports_count"]}</div><div class="label">可用年报数</div></div>')
    if 'avg_roe' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["avg_roe"]:.1%}</div><div class="label">近5年平均ROE</div></div>')
    if 'avg_gross_margin' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["avg_gross_margin"]:.1%}</div><div class="label">近5年平均毛利率</div></div>')
    if 'revenue_growth_5y' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["revenue_growth_5y"]:.1%}</div><div class="label">5年营收CAGR</div></div>')
    if 'latest_revenue_yoy' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["latest_revenue_yoy"]:.1%}</div><div class="label">最新年报营收同比</div></div>')
    if 'latest_profit_yoy' in _fm:
        _fin_cards.append(f'<div class="metric-card"><div class="number">{_fm["latest_profit_yoy"]:.1%}</div><div class="label">最新年报净利润同比</div></div>')
    if _fin_cards:
        _cards_joined = '\n    '.join(_fin_cards)
        _financial_summary_html = f'''
  <h3>历年财务报表核心指标（自动获取）</h3>
  <div class="metric-grid">
    {_cards_joined}
  </div>'''

# 计算有效因子数量
num_active_factors = sum(1 for w in active_weights.values() if w > 0.001)

# ===== 生成HTML =====
html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{STOCK_NAME}（{STOCK_CODE}）估值系统设计报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: 'Noto Sans CJK SC', 'WenQuanYi Micro Hei', 'PingFang SC', 'Microsoft YaHei', 'Helvetica Neue', Arial, sans-serif;
  font-size: 16px; line-height: 1.75; color: #1a1a1a; background: #fafaf9;
}}
.container {{ max-width: 960px; margin: 0 auto; padding: 0 1.5rem; }}
.report-header {{
  background: linear-gradient(135deg, #0d2137 0%, #1a4b8c 50%, #1a3a5c 100%);
  color: #fff; padding: 5rem 1.5rem 4rem; text-align: center; position: relative; overflow: hidden;
}}
.report-header::after {{ content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 4px; background: linear-gradient(90deg, #c75b2a, #e8a040, #c75b2a); }}
.report-header h1 {{ font-family: Georgia, 'Noto Serif CJK SC', serif; font-size: 2.4rem; font-weight: 400; letter-spacing: 0.04em; margin-bottom: 1rem; }}
.report-header .subtitle {{ font-size: 1.1rem; color: rgba(255,255,255,0.7); letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 0.5rem; }}
.report-header .meta {{ font-size: 0.85rem; color: rgba(255,255,255,0.5); margin-top: 2rem; }}
section {{ padding: 3rem 0; }}
section + section {{ border-top: 1px solid #d4d0c8; }}
h2 {{ font-family: Georgia, 'Noto Serif CJK SC', serif; font-size: 1.8rem; font-weight: 400; color: #1a4b8c; margin-bottom: 0.5rem; }}
h2.section-num {{ font-size: 1rem; color: #c75b2a; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 0.25rem; font-family: 'Noto Sans CJK SC', sans-serif; font-weight: 700; }}
h3 {{ font-size: 1.2rem; font-weight: 700; margin: 2rem 0 0.75rem; color: #1a1a1a; }}
h4 {{ font-size: 1rem; font-weight: 700; margin: 1.5rem 0 0.5rem; color: #1a4b8c; }}
p {{ margin-bottom: 1rem; color: #1a1a1a; }}
mark.key {{ background: none; color: #1a4b8c; font-weight: 600; }}
.table-wrap {{ overflow-x: auto; overflow-y: auto; max-height: 600px; margin: 1.5rem 0; border: 1px solid #d4d0c8; border-radius: 6px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
thead th {{ background: #1a4b8c; color: #fff; font-weight: 600; text-align: left; padding: 0.7rem 1rem; font-size: 0.8rem; position: sticky; top: 0; z-index: 1; }}
tbody td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #d4d0c8; }}
tbody tr:nth-child(even) {{ background: #f0eeeb; }}
.callout {{ border-left: 4px solid #1a4b8c; background: rgba(26,75,140,0.05); padding: 1.25rem 1.5rem; margin: 1.5rem 0; border-radius: 0 6px 6px 0; }}
.callout.warn {{ border-left-color: #c75b2a; background: rgba(199,91,42,0.06); }}
.callout.success {{ border-left-color: #2d7d46; background: rgba(45,125,70,0.05); }}
.metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 1.5rem 0; }}
.metric-card {{ background: #fff; border: 1px solid #d4d0c8; border-radius: 8px; padding: 1.25rem; text-align: center; }}
.metric-card .number {{ font-family: 'Courier New', Consolas, monospace; font-size: 1.8rem; font-weight: 700; color: #1a4b8c; line-height: 1.2; }}
.metric-card .label {{ font-size: 0.8rem; color: #6b6b6b; margin-top: 0.4rem; text-transform: uppercase; letter-spacing: 0.06em; }}
.factor-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; margin: 1.5rem 0; }}
.factor-card {{ background: #fff; border: 1px solid #d4d0c8; border-radius: 8px; padding: 1.25rem; border-top: 3px solid #1a4b8c; }}
.factor-card h4 {{ margin: 0 0 0.5rem; font-size: 0.95rem; color: #1a4b8c; }}
.factor-card p {{ font-size: 0.85rem; color: #6b6b6b; margin: 0; line-height: 1.5; }}
.formula {{ background: #fff; border: 1px solid #d4d0c8; border-radius: 6px; padding: 1.25rem 1.5rem; margin: 1.5rem 0; font-family: 'Courier New', monospace; font-size: 0.9rem; line-height: 1.8; overflow-x: auto; white-space: pre-wrap; color: #1a4b8c; }}
.chart-figure {{ margin: 2rem 0; }}
.chart-figure figcaption {{ font-size: 0.9rem; font-weight: 600; margin-bottom: 0.75rem; }}
.fullscreen-btn {{ display: inline-block; padding: 0.5rem 1.2rem; background: #1a4b8c; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85rem; margin-bottom: 1rem; }}
.fullscreen-btn:hover {{ background: #153a6e; }}
.chart-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: #0a0e17; z-index: 9999; flex-direction: column; padding: 0; }}
.chart-overlay.active {{ display: flex; }}
.chart-overlay-header {{ display: flex; justify-content: space-between; align-items: center; padding: 0.6rem 1rem; background: #0d2137; color: #fff; flex-shrink: 0; border-bottom: 1px solid #1a2332; }}
.chart-overlay-header .fs-title {{ font-size: 0.9rem; font-weight: 600; }}
.chart-overlay-header .close-btn {{ background: none; border: 1px solid #4b5563; color: #e5e7eb; padding: 0.3rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }}
.chart-overlay-header .close-btn:hover {{ background: #1f2937; border-color: #9ca3af; }}
.chart-overlay-info {{ display: flex; gap: 1.5rem; padding: 0.5rem 1rem; background: #111827; color: #9ca3af; font-size: 0.8rem; flex-shrink: 0; flex-wrap: wrap; border-bottom: 1px solid #1a2332; }}
.chart-overlay-info .fs-label {{ color: #6b7280; }}
.chart-overlay-info .fs-value {{ color: #e5e7eb; font-weight: 600; }}
.fs-score-high {{ color: #4ade80 !important; }}
.fs-score-mid {{ color: #fbbf24 !important; }}
.fs-score-low {{ color: #f87171 !important; }}
.chart-overlay-body {{ flex: 1; min-height: 0; padding: 0; }}
.chart-overlay-body > div {{ width: 100%; height: 100%; }}
.fs-orient-hint {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(10,14,23,0.92); z-index: 10000; flex-direction: column; align-items: center; justify-content: center; color: #fff; text-align: center; }}
.fs-orient-hint.active {{ display: flex; }}
.fs-orient-hint-icon {{ font-size: 3.5rem; margin-bottom: 1rem; animation: phoneRotate 2s ease-in-out infinite; }}
@keyframes phoneRotate {{ 0%,100% {{ transform: rotate(0deg); }} 50% {{ transform: rotate(90deg); }} }}
footer {{ background: #0d2137; color: rgba(255,255,255,0.7); padding: 3rem 1.5rem; margin-top: 2rem; font-size: 0.85rem; }}
footer h2 {{ color: #fff; font-size: 1.1rem; margin-bottom: 1.5rem; }}
footer ol {{ padding-left: 1.2rem; }}
footer li {{ margin-bottom: 0.75rem; }}
footer a {{ color: #6aa3d8; text-decoration: none; }}
footer .disclaimer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid rgba(255,255,255,0.15); font-size: 0.75rem; color: rgba(255,255,255,0.4); }}
@media (max-width: 768px) {{ .report-header h1 {{ font-size: 1.8rem; }} .metric-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>
<header class="report-header">
  <div class="subtitle">A股估值系统设计</div>
  <h1>{STOCK_NAME}（{STOCK_CODE}）<br>{SUBTITLE}</h1>
  <div class="meta">2026年7月 &middot; 基于历年年报/季报数据自动校准 &middot; 最近10年K线回测</div>
</header>
<main class="container">
<section id="s1">
  <h2 class="section-num">Section 01</h2>
  <h2>公司业务全景与行业定位</h2>
  <p>{STOCK_NAME}（{EXCHANGE.upper()}{STOCK_CODE}）是<mark class="key">{INDUSTRY}行业</mark>的重要参与者。公司依托深厚的行业积累和竞争优势，在细分领域建立了稳固的市场地位。</p>
  <div class="metric-grid">
    <div class="metric-card"><div class="number">{REVENUE}</div><div class="label">2025年营收（亿元）</div></div>
    <div class="metric-card"><div class="number">{NET_PROFIT}</div><div class="label">2025年归母净利润（亿元）</div></div>
    <div class="metric-card"><div class="number">{GROSS_MARGIN}</div><div class="label">2025年毛利率</div></div>
    <div class="metric-card"><div class="number">~{MARKET_CAP}</div><div class="label">当前市值（亿元）</div></div>
  </div>
  <h3>行业特征与竞争格局</h3>
  <p>当前PE(TTM) {latest_pe:.1f}倍，PB {latest_pb:.2f}倍，总股本{TOTAL_SHARES}亿股。估值处于{'历史偏低' if latest['score'] >= 60 else '中性' if latest['score'] >= 40 else '偏高'}水平。</p>
{_financial_summary_html}
</section>
<section id="s2">
  <h2 class="section-num">Section 02</h2>
  <h2>估值评分模型设计</h2>
  <p>采用<mark class="key">{MODEL_NAME}估值模型</mark>，{num_active_factors}因子加权体系：</p>
  <div class="formula">{weights_display_lines}</div>
  <h3>评分模型参数</h3>
  <div class="table-wrap"><table>
    <thead><tr><th>因子</th><th>权重</th><th>参数/区间</th><th>说明</th></tr></thead>
    <tbody>
{factor_table_html}
    </tbody>
  </table></div>
  <h3>估值分数分界线标准</h3>
  <div class="table-wrap"><table>
    <thead><tr><th>分数区间</th><th>估值状态</th><th>投资含义</th></tr></thead>
    <tbody>
      <tr><td>80-100</td><td>极度低估</td><td>历史性低估区间，具备强烈安全边际</td></tr>
      <tr><td>70-79</td><td>低估</td><td>估值偏低，可以考虑分批建仓</td></tr>
      <tr><td>60-69</td><td>中性偏低</td><td>估值合理偏下，维持持仓观望</td></tr>
      <tr><td>40-59</td><td>中性偏高</td><td>估值合理偏上，关注止盈信号</td></tr>
      <tr><td>20-39</td><td>高估</td><td>估值偏高，考虑减仓或观望</td></tr>
      <tr><td>0-19</td><td>极度高估</td><td>严重高估，存在较大回调风险</td></tr>
    </tbody>
  </table></div>
</section>
<section id="s3">
  <h2 class="section-num">Section 03</h2>
  <h2>估值评分回测曲线（{period_label}）</h2>
  <div class="chart-figure">
    <figcaption>图1：{STOCK_NAME}估值评分回测曲线（{len(results)}个交易日）</figcaption>
    <button class="fullscreen-btn" onclick="openFullscreenChart()">&#x26F6; 横屏查看</button>
    <div id="chart-backtest" style="width:100%;height:550px;"></div>
    <p>图表说明：蓝色折线为综合估值分（0-100），浅灰色面积图为收盘价走势（元），紫色折线为PE(TTM)。绿色虚线为70分低估分界线，红色虚线为40分高估分界线。估值分越高代表越被低估。</p>
  </div>
</section>
<section id="s4">
  <h2 class="section-num">Section 04</h2>
  <h2>关键时点估值分析</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>日期</th><th>收盘价</th><th>PE(TTM)</th><th>PB</th><th>市值(亿)</th><th>估值分</th><th>状态</th></tr></thead>
    <tbody id="keyDateTable"></tbody>
  </table></div>
  <h3>最新估值状态</h3>
  <p>当前估值评分：<strong>{latest['score']}</strong> 分（{latest['date']}）</p>
  <p>收盘价 {latest['close']} 元 | PE(TTM) {latest['pe_ttm']} | PB {latest['pb']} | 总市值约 {latest['market_cap']:.0f} 亿元</p>
  <p>状态：<strong>{status_text}</strong></p>
</section>
<section id="s5">
  <h2 class="section-num">Section 05</h2>
  <h2>投资逻辑与风险提示</h2>
  <h3>核心看多逻辑</h3>
  <ul>
    <li>行业地位稳固，具备持续经营能力</li>
    <li>当前估值处于历史偏低水平，安全边际充足</li>
    <li>基本面稳健，分红政策稳定</li>
  </ul>
  <h3>主要风险因素</h3>
  <ul>
    <li>宏观经济下行影响行业需求</li>
    <li>行业竞争加剧导致利润率承压</li>
    <li>政策变化或行业监管趋严</li>
  </ul>
</section>
<footer>
  <h2>数据来源与参考</h2>
  <ol>
    <li>腾讯财经API - 最近10年日K线行情数据（前复权）</li>
    <li>东方财富财务数据 - {STOCK_NAME}历年年报、半年报、季度报告</li>
    <li>{STOCK_NAME}2025年年度报告</li>
  </ol>
  <div class="disclaimer">免责声明：本报告仅供学习和研究使用，不构成任何投资建议。估值模型基于历史数据和简化假设，不预测未来股价走势。投资有风险，入市需谨慎。</div>
</footer>
</main>

<!-- 全屏覆盖层 -->
<div id="chartOverlay" class="chart-overlay">
  <div class="chart-overlay-header">
    <span class="fs-title">估值评分回测曲线</span>
    <button class="close-btn" onclick="closeFullscreenChart()">✕ 关闭</button>
  </div>
  <div class="chart-overlay-info">
    <div><span class="fs-label">股票 </span><span class="fs-value" id="fsStockName">{STOCK_NAME}({STOCK_CODE})</span></div>
    <div><span class="fs-label">最新价 </span><span class="fs-value" id="fsPrice">{latest['close']} 元</span></div>
    <div><span class="fs-label">估值分 </span><span class="fs-value {status_class}" id="fsScore">{latest['score']}</span></div>
    <div><span class="fs-label">状态 </span><span class="fs-value {status_class}" id="fsStatus">{status_text}</span></div>
  </div>
  <div class="chart-overlay-body"><div id="chart-backtest-fullscreen"></div></div>
  <div id="fsOrientHint" class="fs-orient-hint">
    <div class="fs-orient-hint-icon">📱</div>
    <div style="font-size:1.2rem;font-weight:600;">请将手机旋转至横屏</div>
    <div style="font-size:0.9rem;opacity:0.7;margin-top:0.5rem;">以获得最佳图表查看体验</div>
  </div>
</div>

<script>
{echarts_js}
</script>
<script>
{val_data_js}

(function() {{
  var el = document.getElementById('chart-backtest');
  if (!el || typeof VALUATION_DATA === 'undefined') return;
  var data = VALUATION_DATA.data;
  var dates = data.map(function(d) {{ return d.date; }});
  var scores = data.map(function(d) {{ return d.score; }});
  var closes = data.map(function(d) {{ return d.close; }});
  var peTTMs = data.map(function(d) {{ return d.pe_ttm; }});
  var marketCaps = data.map(function(d) {{ return d.market_cap; }});
  var sortedScores = scores.slice().sort(function(a,b){{return a-b;}});
  var p20 = sortedScores[Math.floor(sortedScores.length * 0.2)];
  var p80 = sortedScores[Math.floor(sortedScores.length * 0.8)];
  data.forEach(function(d) {{
    var c = 0;
    for (var i = 0; i < sortedScores.length; i++) {{ if (sortedScores[i] <= d.score) c++; }}
    d._pct = Math.round(c / sortedScores.length * 100);
  }});

  var chart = echarts.init(el, null, {{ renderer: 'canvas' }});
  chart.setOption({{
    animation: false,
    tooltip: {{
      trigger: 'axis', confine: true,
      position: function(point, params, dom, rect, size) {{
        var tw = size.contentSize[0], th = size.contentSize[1];
        var cw = size.viewSize[0], ch = size.viewSize[1];
        var x, y;
        if (point[0] < cw / 2) {{
          x = point[0] + 15;
        }} else {{
          x = point[0] - tw - 15;
        }}
        if (point[1] < ch / 2) {{
          y = point[1] + 15;
        }} else {{
          y = point[1] - th - 15;
        }}
        if (x < 0) x = 0;
        if (x + tw > cw) x = cw - tw;
        if (y < 0) y = 0;
        if (y + th > ch) y = ch - th;
        return [x, y];
      }},
      axisPointer: {{ type: 'cross', crossStyle: {{ color: '#999', width: 0.5 }} }},
      formatter: function(p) {{
        var idx = p[0].dataIndex; var d = data[idx];
        return '<strong>' + d.date + '</strong> &nbsp; 历史百分位: <strong>' + d._pct + '%</strong><br/>估值分: <strong>' + d.score + '</strong><br/>收盘价: ' + d.close + ' 元<br/>PE(TTM): ' + d.pe_ttm + '<br/>PB: ' + d.pb + '<br/>总市值: ' + d.market_cap.toFixed(0) + ' 亿';
      }}
    }},
    legend: {{ data: ['估值分(0-100)', '收盘价(元)', 'PE(TTM)'], top: 8, textStyle: {{ color: '#1a1a1a', fontSize: 12 }}, itemGap: 20 }},
    grid: {{ left: 70, right: 85, top: 45, bottom: 65 }},
    xAxis: {{
      type: 'category', data: dates,
      axisLabel: {{ color: '#6b6b6b', fontSize: 11, rotate: 30, interval: Math.floor(dates.length / 12), formatter: function(v) {{ return v.substring(5); }} }},
      axisLine: {{ lineStyle: {{ color: '#d4d0c8' }} }}
    }},
    yAxis: [
      {{ type: 'value', name: '估值分', min: 0, max: 100, nameTextStyle: {{ color: '#1a4b8c', fontSize: 13 }}, axisLabel: {{ color: '#1a4b8c', fontSize: 12 }}, splitLine: {{ lineStyle: {{ color: '#d4d0c8' }} }} }},
      {{ type: 'value', name: '价格/PE/市值', nameTextStyle: {{ color: '#6b6b6b', fontSize: 13 }}, axisLabel: {{ color: '#6b6b6b', fontSize: 12 }}, splitLine: {{ show: false }} }}
    ],
    dataZoom: [
      {{ type: 'slider', xAxisIndex: 0, start: 0, end: 100, bottom: 8, height: 22, borderColor: '#d4d0c8', fillerColor: 'rgba(26,75,140,0.12)', handleStyle: {{ color: '#1a4b8c', borderColor: '#1a4b8c' }}, textStyle: {{ color: '#6b6b6b', fontSize: 11 }} }}
    ],
    series: [
      {{
        name: '估值分(0-100)', type: 'line', data: scores, yAxisIndex: 0,
        lineStyle: {{ color: '#1a4b8c', width: 1.5 }}, itemStyle: {{ color: '#1a4b8c' }}, symbol: 'none',
        markPoint: {{ data: INTRADAY_MARKPOINT, symbol: 'circle', symbolSize: 10, label: {{ show: true, position: 'top', color: '#f59e0b', fontSize: 11, formatter: function(p) {{ return p.value; }} }} }},
        areaStyle: {{ color: {{ type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{{ offset: 0, color: '#1a4b8c44' }}, {{ offset: 1, color: '#1a4b8c05' }}] }} }},
        markLine: {{
          silent: true,
          data: [
            {{ yAxis: 70, label: {{ formatter: '低估区间', position: 'insideEndTop', color: '#2d7d46', fontSize: 12, fontWeight: 'bold' }}, lineStyle: {{ color: '#2d7d46', type: 'dashed', width: 1.5 }} }},
            {{ yAxis: 40, label: {{ formatter: '高估区间', position: 'insideEndBottom', color: '#b22222', fontSize: 12, fontWeight: 'bold' }}, lineStyle: {{ color: '#b22222', type: 'dashed', width: 1.5 }} }},
            {{ yAxis: p80, label: {{ formatter: '80th百分位', position: 'insideEndTop', color: '#4ade80aa', fontSize: 10 }}, lineStyle: {{ color: '#4ade80aa', type: 'dashed', width: 1 }} }},
            {{ yAxis: p20, label: {{ formatter: '20th百分位', position: 'insideEndBottom', color: '#f87171aa', fontSize: 10 }}, lineStyle: {{ color: '#f87171aa', type: 'dashed', width: 1 }} }}
          ]
        }}, z: 5
      }},
      {{ name: '收盘价(元)', type: 'line', data: closes, yAxisIndex: 1, lineStyle: {{ color: '#d4d0c8', width: 1 }}, itemStyle: {{ color: '#d4d0c8' }}, symbol: 'none', areaStyle: {{ color: {{ type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{{ offset: 0, color: 'rgba(180,180,180,0.25)' }}, {{ offset: 1, color: 'rgba(180,180,180,0.02)' }}] }} }}, z: 1 }},
      {{ name: 'PE(TTM)', type: 'line', data: peTTMs, yAxisIndex: 1, lineStyle: {{ color: '#b8860b88', width: 1.5 }}, itemStyle: {{ color: '#b8860b' }}, symbol: 'none', z: 2 }}
    ]
  }});
  window.addEventListener('resize', function() {{ chart.resize(); }});

  // 填充关键日期表格
  var keyDates = [{key_dates_str}];
  var tbody = document.getElementById('keyDateTable');
  if (tbody) {{
    var added = 0;
    for (var i = data.length - 1; i >= 0 && added < 8; i--) {{
      if (keyDates.indexOf(data[i].date) !== -1 || added < 4) {{
        var r = data[i];
        var st = r.score >= 80 ? '极度低估' : r.score >= 70 ? '低估' : r.score >= 60 ? '中性偏低' : r.score >= 40 ? '中性偏高' : r.score >= 20 ? '高估' : '极度高估';
        tbody.innerHTML += '<tr><td>' + r.date + '</td><td>' + r.close + '</td><td>' + r.pe_ttm + '</td><td>' + r.pb + '</td><td>' + r.market_cap.toFixed(0) + '</td><td>' + r.score + '</td><td>' + st + '</td></tr>';
        added++;
      }}
    }}
  }}
}})();

// === Fullscreen chart ===
var _fsChart = null;
window.openFullscreenChart = openFullscreenChart;
window.closeFullscreenChart = closeFullscreenChart;
function openFullscreenChart() {{
  if (typeof VALUATION_DATA === 'undefined') return;
  var overlay = document.getElementById('chartOverlay');
  overlay.classList.add('active');
  document.body.style.overflow = 'hidden';
  var el = document.getElementById('chart-backtest-fullscreen');
  if (_fsChart) {{ _fsChart.dispose(); _fsChart = null; }}
  var data = VALUATION_DATA.data;
  var dates = data.map(function(d) {{ return d.date; }});
  var scores = data.map(function(d) {{ return d.score; }});
  var closes = data.map(function(d) {{ return d.close; }});
  var peTTMs = data.map(function(d) {{ return d.pe_ttm; }});
  var marketCaps = data.map(function(d) {{ return d.market_cap; }});
  var sortedScores = scores.slice().sort(function(a,b){{return a-b;}});
  var p20 = sortedScores[Math.floor(sortedScores.length * 0.2)];
  var p80 = sortedScores[Math.floor(sortedScores.length * 0.8)];
  data.forEach(function(d) {{
    var c = 0;
    for (var i = 0; i < sortedScores.length; i++) {{ if (sortedScores[i] <= d.score) c++; }}
    d._pct = Math.round(c / sortedScores.length * 100);
  }});
  var latest = data[data.length - 1];
  try {{ if (document.fullscreenEnabled || document.webkitFullscreenEnabled) {{ var d = document.documentElement; if (d.requestFullscreen) d.requestFullscreen(); else if (d.webkitRequestFullscreen) d.webkitRequestFullscreen(); }} }} catch(e) {{}}
  try {{ if (screen.orientation && screen.orientation.lock) {{ screen.orientation.lock('landscape').catch(function(){{}}); }} }} catch(e) {{}}
  var hint = document.getElementById('fsOrientHint');
  if (hint) {{
    if (window.innerWidth < window.innerHeight && window.screen.width < 768) {{ hint.classList.add('active'); }}
    else {{ hint.classList.remove('active'); }}
  }}
  _fsChart = echarts.init(el, null, {{ renderer: 'canvas' }});
  _fsChart.setOption({{
    backgroundColor: '#0a0e17',
    animation: false,
    tooltip: {{
      trigger: 'axis', confine: true,
      position: function(point, params, dom, rect, size) {{
        var tw = size.contentSize[0], th = size.contentSize[1];
        var cw = size.viewSize[0], ch = size.viewSize[1];
        var x, y;
        if (point[0] < cw / 2) {{
          x = point[0] + 15;
        }} else {{
          x = point[0] - tw - 15;
        }}
        if (point[1] < ch / 2) {{
          y = point[1] + 15;
        }} else {{
          y = point[1] - th - 15;
        }}
        if (x < 0) x = 0;
        if (x + tw > cw) x = cw - tw;
        if (y < 0) y = 0;
        if (y + th > ch) y = ch - th;
        return [x, y];
      }},
      backgroundColor: 'rgba(10,14,23,0.95)',
      borderColor: '#1a2332',
      textStyle: {{ color: '#e5e7eb', fontSize: 12 }},
      axisPointer: {{ type: 'cross', crossStyle: {{ color: '#6b7280', width: 0.5 }} }},
      formatter: function(p) {{
        var idx = p[0].dataIndex; var d = data[idx];
        return '<strong style="color:#60a5fa">' + d.date + '</strong> &nbsp; 历史百分位: <strong style="color:#fff">' + d._pct + '%</strong><br/>估值分: <strong style="color:#fff">' + d.score + '</strong><br/>收盘价: ' + d.close + ' 元<br/>PE(TTM): ' + d.pe_ttm + '<br/>PB: ' + d.pb + '<br/>总市值: ' + d.market_cap.toFixed(0) + ' 亿';
      }}
    }},
    legend: {{ data: ['估值分(0-100)', '收盘价(元)', 'PE(TTM)'], top: 8, textStyle: {{ color: '#9ca3af', fontSize: 12 }}, itemGap: 20 }},
    grid: {{ left: 65, right: 80, top: 45, bottom: 55 }},
    xAxis: {{
      type: 'category', data: dates,
      axisLabel: {{ color: '#6b7280', fontSize: 10, rotate: 30, interval: Math.floor(dates.length / 15), formatter: function(v) {{ return v.substring(5); }} }},
      axisLine: {{ lineStyle: {{ color: '#1a2332' }} }}
    }},
    yAxis: [
      {{ type: 'value', name: '估值分', min: 0, max: 100, nameTextStyle: {{ color: '#60a5fa', fontSize: 12 }}, axisLabel: {{ color: '#60a5fa', fontSize: 11 }}, splitLine: {{ lineStyle: {{ color: '#1a2332' }} }} }},
      {{ type: 'value', name: '价格/PE/市值', nameTextStyle: {{ color: '#9ca3af', fontSize: 12 }}, axisLabel: {{ color: '#9ca3af', fontSize: 11 }}, splitLine: {{ show: false }} }}
    ],
    dataZoom: [
      {{ type: 'slider', xAxisIndex: 0, start: 0, end: 100, bottom: 5, height: 20, borderColor: '#374151', fillerColor: 'rgba(96,165,250,0.12)', handleStyle: {{ color: '#60a5fa', borderColor: '#60a5fa' }}, textStyle: {{ color: '#9ca3af', fontSize: 10 }} }}
    ],
    series: [
      {{
        name: '估值分(0-100)', type: 'line', data: scores, yAxisIndex: 0,
        lineStyle: {{ color: '#60a5fa', width: 1.2 }}, itemStyle: {{ color: '#60a5fa' }}, symbol: 'none',
        markPoint: {{ data: INTRADAY_MARKPOINT, symbol: 'circle', symbolSize: 12, label: {{ show: true, position: 'top', color: '#f59e0b', fontSize: 12, formatter: function(p) {{ return p.value; }} }} }},
        areaStyle: {{ color: {{ type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{{ offset: 0, color: '#60a5fa33' }}, {{ offset: 1, color: '#60a5fa05' }}] }} }},
        markLine: {{
          silent: true,
          data: [
            {{ yAxis: 70, label: {{ formatter: '低估区间', position: 'insideEndTop', color: '#4ade80', fontSize: 11, fontWeight: 'bold' }}, lineStyle: {{ color: '#4ade80', type: 'dashed', width: 1.5 }} }},
            {{ yAxis: 40, label: {{ formatter: '高估区间', position: 'insideEndBottom', color: '#f87171', fontSize: 11, fontWeight: 'bold' }}, lineStyle: {{ color: '#f87171', type: 'dashed', width: 1.5 }} }},
            {{ yAxis: p80, label: {{ formatter: '80th百分位', position: 'insideEndTop', color: '#4ade80aa', fontSize: 10 }}, lineStyle: {{ color: '#4ade80aa', type: 'dashed', width: 1 }} }},
            {{ yAxis: p20, label: {{ formatter: '20th百分位', position: 'insideEndBottom', color: '#f87171aa', fontSize: 10 }}, lineStyle: {{ color: '#f87171aa', type: 'dashed', width: 1 }} }}
          ]
        }}, z: 5
      }},
      {{ name: '收盘价(元)', type: 'line', data: closes, yAxisIndex: 1, lineStyle: {{ color: '#9ca3af', width: 1 }}, itemStyle: {{ color: '#9ca3af' }}, symbol: 'none', areaStyle: {{ color: {{ type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{{ offset: 0, color: 'rgba(156,163,175,0.15)' }}, {{ offset: 1, color: 'rgba(156,163,175,0.02)' }}] }} }}, z: 1 }},
      {{ name: 'PE(TTM)', type: 'line', data: peTTMs, yAxisIndex: 1, lineStyle: {{ color: '#fbbf24', width: 1.2 }}, itemStyle: {{ color: '#fbbf24' }}, symbol: 'none', z: 2 }}
    ]
  }});
  var resizeTimer;
  window.addEventListener('resize', function() {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {{ if (_fsChart) _fsChart.resize(); }}, 100);
    var overlay = document.getElementById('chartOverlay');
    var hint = document.getElementById('fsOrientHint');
    if (overlay && overlay.classList.contains('active') && hint) {{
      if (window.innerWidth < window.innerHeight && window.screen.width < 768) {{ hint.classList.add('active'); }}
      else {{ hint.classList.remove('active'); }}
    }}
  }});
}}
function closeFullscreenChart() {{
  var overlay = document.getElementById('chartOverlay');
  overlay.classList.remove('active');
  document.body.style.overflow = '';
  if (_fsChart) {{ _fsChart.dispose(); _fsChart = null; }}
  var hint = document.getElementById('fsOrientHint');
  if (hint) hint.classList.remove('active');
  try {{ if (document.fullscreenElement || document.webkitFullscreenElement) {{ if (document.exitFullscreen) document.exitFullscreen(); else if (document.webkitExitFullscreen) document.webkitExitFullscreen(); }} }} catch(e) {{}}
  try {{ if (screen.orientation && typeof screen.orientation.unlock === 'function') {{ var p = screen.orientation.unlock(); if (p && typeof p.catch === 'function') p.catch(function(){{}}); }} }} catch(e) {{}}
  try {{ if (typeof screen.unlockOrientation === 'function') screen.unlockOrientation(); }} catch(e) {{}}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape' || e.keyCode === 27) closeFullscreenChart();
}});
document.addEventListener('fullscreenchange', function() {{
  if (!document.fullscreenElement && !document.webkitFullscreenElement) {{
    var overlay = document.getElementById('chartOverlay');
    if (overlay && overlay.classList.contains('active')) closeFullscreenChart();
  }}
}});
</script>
</body>
</html>'''

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"  -> {OUTPUT} ({os.path.getsize(OUTPUT)/1024:.0f}KB)")
