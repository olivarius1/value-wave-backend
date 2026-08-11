# -*- coding: utf-8 -*-
"""
基本面特征模型分类器（与数据获取、HTML渲染解耦）

设计目标：
- 不依赖人工 --model 指定，而是依据股票基本面特征自动推荐最合适的估值模型。
- 与 scoring_engine 配合：classify_model() 选出模型 → MODEL_PRESETS[model] 取权重 → 评分。

核心思路：
- 8种模型各自定义一组"特征评分规则"（_MODEL_RULES），命中加分、违背减分。
- 综合财务指标（ROE/毛利率/稳定性/增速/股息/研发）+ 行业关键词先验，得分最高者胜出。
- 输出透明证据（evidence），便于在报告中展示"为什么选这个模型"。

复用示例：
    from model_classifier import classify_model, fetch_rd_ratio
    rd = fetch_rd_ratio(code, exchange)
    result = classify_model(metrics, industry, rd_ratio=rd)
    model_type = result['model']
"""
import urllib.request
import urllib.parse
import json
import sys


# ===== 行业关键词先验（仅作为辅助证据，不单独决定结果）=====
_KEYWORD_HINTS = {
    'bank': ['银行', '保险', '证券', '信托', '金融'],
    'realestate': ['地产', '房地产', '物业'],
    'pharma': ['医药', '生物', '制药', '医疗', '疫苗', '创新药'],
    'cyclical': ['煤炭', '钢铁', '有色', '石油', '化工', '建材', '铝', '铜', '黄金', '锂', '稀土', '能源金属'],
    'tech': ['半导体', '芯片', '电子', '计算机', '软件', '通信', '光模块', '存储', '集成电路', '人工智能', '云计算', '光伏', '电池'],
    'staples': ['食品', '饮料', '白酒', '乳业', '调味', '农业', '养殖', '种植', '日用品'],
    'discretionary': ['家电', '汽车', '纺织', '服装', '旅游', '酒店', '零售', '轻工', '家具', '珠宝'],
}


def _kw_score(industry, model):
    """行业关键词命中得分（0/2/4）"""
    if not industry:
        return 0
    kws = _KEYWORD_HINTS.get(model, [])
    if any(k in industry for k in kws):
        return 4
    return 0


# ===== 各模型特征评分规则 =====
# 每条规则: (lambda features -> bool 命中, 分值, 证据文字)
# features 可用键: avg_roe, avg_gross_margin, gross_margin_stability,
#   revenue_growth_5y, latest_revenue_yoy, latest_profit_yoy, roe_trend,
#   rd_ratio, dividend_yield, industry
_MODEL_RULES = {
    'bank': [
        (lambda f: f.get('avg_roe', 0) >= 0.10, 3, 'ROE稳健(≥10%)'),
        (lambda f: f.get('avg_gross_margin', 0) < 0.30, 2, '低毛利(金融业态)'),
        (lambda f: f.get('dividend_yield', 0) >= 0.04, 2, '高股息'),
        (lambda f: f.get('revenue_growth_5y', 0) < 0.15, 1, '低增速'),
        (lambda f: _kw_score(f.get('industry', ''), 'bank') > 0, 6, '金融行业关键词'),
    ],
    'soe': [
        (lambda f: f.get('dividend_yield', 0) >= 0.04, 4, '高股息'),
        (lambda f: 0.06 <= f.get('avg_roe', 0) <= 0.15, 2, 'ROE稳健中等'),
        (lambda f: f.get('gross_margin_stability', 1) <= 0.03, 2, '毛利率稳定'),
        (lambda f: f.get('revenue_growth_5y', 0) < 0.20, 1, '增速平稳'),
        (lambda f: _kw_score(f.get('industry', ''), 'soe') > 0, 4, '基建/央企关键词'),
    ],
    'cyclical': [
        (lambda f: f.get('gross_margin_stability', 0) >= 0.05, 4, '毛利率波动大(周期特征)'),
        (lambda f: abs(f.get('roe_trend', 0)) >= 0.05, 3, 'ROE大幅波动'),
        (lambda f: abs(f.get('latest_profit_yoy', 0)) >= 0.50, 2, '利润大起大落'),
        (lambda f: _kw_score(f.get('industry', ''), 'cyclical') > 0, 6, '资源/周期关键词'),
    ],
    'tech': [
        (lambda f: (f.get('rd_ratio') or 0) >= 0.08, 5, '研发费用率高(≥8%)'),
        (lambda f: 0.03 <= (f.get('rd_ratio') or 0) < 0.08, 3, '研发投入中等(3-8%)'),
        (lambda f: f.get('revenue_growth_5y', 0) >= 0.25, 3, '高营收增速(5年CAGR≥25%)'),
        (lambda f: f.get('latest_revenue_yoy', 0) >= 0.30, 2, '近期高增长'),
        (lambda f: f.get('avg_gross_margin', 0) >= 0.30, 2, '高毛利'),
        (lambda f: _kw_score(f.get('industry', ''), 'tech') > 0, 4, '科技关键词'),
    ],
    'pharma': [
        (lambda f: (f.get('rd_ratio') or 0) >= 0.05, 4, '研发投入较高(≥5%)'),
        (lambda f: f.get('revenue_growth_5y', 0) >= 0.15, 2, '营收稳健增长'),
        (lambda f: f.get('avg_gross_margin', 0) >= 0.50, 3, '高毛利(医药特征)'),
        (lambda f: _kw_score(f.get('industry', ''), 'pharma') > 0, 6, '医药关键词'),
    ],
    'staples': [
        (lambda f: f.get('gross_margin_stability', 1) <= 0.02, 4, '毛利率极稳定(需求刚性)'),
        (lambda f: 0 <= f.get('revenue_growth_5y', 0) <= 0.20, 2, '增速平稳'),
        (lambda f: f.get('avg_roe', 0) >= 0.15, 2, 'ROE稳健偏高'),
        (lambda f: 0.20 <= f.get('avg_gross_margin', 0) < 0.50, 1, '毛利适中'),
        (lambda f: _kw_score(f.get('industry', ''), 'staples') > 0, 5, '必选消费关键词'),
    ],
    'discretionary': [
        (lambda f: f.get('avg_gross_margin', 0) >= 0.35, 3, '高毛利(品牌溢价)'),
        (lambda f: f.get('avg_roe', 0) >= 0.15, 2, 'ROE偏高'),
        (lambda f: f.get('revenue_growth_5y', 0) >= 0.10, 1, '成长性好'),
        (lambda f: _kw_score(f.get('industry', ''), 'discretionary') > 0, 5, '可选消费关键词'),
    ],
}


def _fetch_json(url, timeout=15):
    """通用东方财富数据中心API请求"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8-sig')
        return json.loads(raw)
    except Exception as e:
        print(f"  [model_classifier] API请求失败: {e}", file=sys.stderr)
        return None


def fetch_industry(stock_code, exchange):
    """
    获取股票行业分类（东方财富 EM2016 三级行业）

    Returns:
        str: 行业描述，如 '电子设备-半导体-集成电路'；失败返回 ''
    """
    secucode = f"{stock_code}.{'SH' if exchange == 'sh' else 'SZ'}"
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        "?reportName=RPT_F10_BASIC_ORGINFO&columns=ALL"
        f"&filter=(SECUCODE=%22{secucode}%22)"
        "&source=HSF10&client=PC"
    )
    data = _fetch_json(url)
    if not data or not data.get('success') or not data.get('result'):
        return ''
    items = data['result'].get('data', [])
    if not items:
        return ''
    # EM2016 为东财三级行业；INDUSTRYCSRC1 为证监会行业（兜底）
    return items[0].get('EM2016') or items[0].get('INDUSTRYCSRC1') or ''


def fetch_rd_ratio(stock_code, exchange, max_years=5):
    """
    获取近几期研发费用率（研发费用 RDEXPEND / 营业总收入）

    研发费用字段 RDEXPEND 与营收 TOTALOPERATEREVE 均在主表 RPT_F10_FINANCE_MAINFINADATA，
    仅取年报口径（REPORT_TYPE=年报）以保证可比性。

    Returns:
        list of dict: [{'year': 2024, 'rd': 5.2(亿), 'revenue': 100.0(亿), 'rd_ratio': 0.052}, ...]
        从新到旧；获取失败返回 []
    """
    secucode = f"{stock_code}.{'SH' if exchange == 'sh' else 'SZ'}"
    report_type_encoded = urllib.parse.quote('年报')
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        "?reportName=RPT_F10_FINANCE_MAINFINADATA"
        "&columns=REPORT_DATE,RDEXPEND,TOTALOPERATEREVE"
        f"&filter=(SECUCODE=%22{secucode}%22)(REPORT_TYPE=%22{report_type_encoded}%22)"
        f"&pageSize={max_years}"
        "&sortColumns=REPORT_DATE"
        "&sortTypes=-1"
        "&source=HSF10"
        "&client=PC"
    )
    data = _fetch_json(url)
    if not data or not data.get('success') or not data.get('result'):
        return []

    items = data['result'].get('data', [])
    result = []
    for it in items:
        try:
            report_date = (it.get('REPORT_DATE') or '')[:10]
            year = int(report_date[:4]) if report_date else 0
            revenue_raw = it.get('TOTALOPERATEREVE') or 0
            revenue = float(revenue_raw) / 1e8 if revenue_raw else 0
            rd_raw = it.get('RDEXPEND') or 0
            rd = float(rd_raw) / 1e8 if rd_raw else 0
            if year and revenue > 0:
                result.append({
                    'year': year,
                    'rd': round(rd, 2),
                    'revenue': round(revenue, 2),
                    'rd_ratio': round(rd / revenue, 4) if rd > 0 else 0,
                })
        except (ValueError, TypeError):
            continue
    return result


def classify_model(financial_metrics, industry='', rd_ratio=None, dividend_yield=None):
    """
    依据基本面特征推荐最合适的估值模型。

    Args:
        financial_metrics: compute_financial_metrics() 返回值
            (avg_roe/avg_gross_margin/gross_margin_stability/revenue_growth_5y/
             latest_revenue_yoy/latest_profit_yoy/roe_trend ...)
        industry: 行业描述（关键词先验）
        rd_ratio: 研发费用率(小数)，可选（建议用 fetch_rd_ratio 最新一期）
        dividend_yield: 股息率(小数)，可选

    Returns:
        dict: {
            'model': 推荐模型key,
            'model_name': 中文名(需调用方用 MODEL_PRESETS 补全),
            'score': 最高分,
            'confidence': 'high'/'medium'/'low',
            'scores': {model: score},
            'reasons': [命中证据列表],
            'features': 归一化后的特征字典,
        }
    """
    features = dict(financial_metrics or {})
    features['industry'] = industry or ''
    if rd_ratio is not None:
        features['rd_ratio'] = rd_ratio
    if dividend_yield is not None:
        features['dividend_yield'] = dividend_yield

    scores = {}
    evidence = {}
    for model, rules in _MODEL_RULES.items():
        total = 0
        ev = []
        for fn, pts, desc in rules:
            try:
                if fn(features):
                    total += pts
                    ev.append(f"{desc}(+{pts})")
            except Exception:
                continue
        scores[model] = total
        evidence[model] = ev

    best = max(scores, key=lambda k: scores[k])
    best_score = scores[best]
    sorted_scores = sorted(scores.values(), reverse=True)
    margin = best_score - sorted_scores[1] if len(sorted_scores) > 1 else best_score

    if best_score >= 8 and margin >= 3:
        confidence = 'high'
    elif best_score >= 5:
        confidence = 'medium'
    else:
        confidence = 'low'

    return {
        'model': best,
        'score': best_score,
        'confidence': confidence,
        'scores': scores,
        'reasons': evidence.get(best, []),
        'features': features,
    }


if __name__ == '__main__':
    # 自测：python model_classifier.py 001309 sz
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from financial_fetcher import fetch_financial_reports, compute_financial_metrics

    code = sys.argv[1] if len(sys.argv) > 1 else '600887'
    exch = sys.argv[2] if len(sys.argv) > 2 else ('sh' if code.startswith('6') else 'sz')
    reps = fetch_financial_reports(code, exch)
    metrics = compute_financial_metrics(reps)
    industry = fetch_industry(code, exch)
    rd_list = fetch_rd_ratio(code, exch)
    rd = rd_list[0]['rd_ratio'] if rd_list else None
    res = classify_model(metrics, industry, rd_ratio=rd)
    print(f"行业: {industry}")
    print(f"研发费用率: {rd} (近年: {[r['rd_ratio'] for r in rd_list]})")
    print(f"推荐模型: {res['model']} (得分{res['score']}, 置信度{res['confidence']})")
    print(f"证据: {res['reasons']}")
    print(f"全部得分: {res['scores']}")
