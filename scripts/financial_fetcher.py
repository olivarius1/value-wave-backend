#!/usr/bin/env python3
"""
财务报表数据获取器
从东方财富数据中心API获取A股历年年报、半年报、季报的核心财务指标
用于估值评分模型的参数自动校准
"""
import json
import urllib.request
import urllib.parse
import sys
import os


def _fetch_api(url, timeout=15):
    """获取东方财富数据中心API"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8-sig')
        return json.loads(raw)
    except Exception as e:
        print(f"  [financial_fetcher] API请求失败: {e}", file=sys.stderr)
        return None


def fetch_financial_reports(stock_code, exchange, max_reports=40):
    """
    获取股票历年财务报表核心指标 (东方财富数据中心API)
    
    Args:
        stock_code: 股票代码, e.g. '600887'
        exchange: 交易所, 'sh' or 'sz'
        max_reports: 最多获取的报告期数量
    
    Returns:
        list of dict, 按报告期从新到旧排列
    """
    secucode = f"{stock_code}.{'SH' if exchange == 'sh' else 'SZ'}"
    
    url = (
        f"https://datacenter.eastmoney.com/securities/api/data/v1/get"
        f"?reportName=RPT_F10_FINANCE_MAINFINADATA"
        f"&columns=ALL"
        f"&filter=(SECUCODE=%22{secucode}%22)"
        f"&pageSize={max_reports}"
        f"&sortColumns=REPORT_DATE"
        f"&sortTypes=-1"
        f"&source=HSF10"
        f"&client=PC"
    )
    
    data = _fetch_api(url)
    if not data or not data.get('success') or not data.get('result'):
        print(f"  [financial_fetcher] 无法获取 {stock_code} 财务数据", file=sys.stderr)
        return []
    
    items = data['result'].get('data', [])
    if not items:
        return []
    
    reports = []
    for item in items:
        try:
            report_date = item.get('REPORT_DATE', '')[:10]  # "2025-12-31"
            report_type_cn = item.get('REPORT_TYPE', '')  # "年报"/"一季报"/"半年报"/"三季报"
            
            if not report_date:
                continue
            
            # 判断报告类型
            if report_type_cn == '年报':
                report_type = 'annual'
            elif report_type_cn == '半年报':
                report_type = 'semi'
            elif report_type_cn == '一季报':
                report_type = 'q1'
            elif report_type_cn == '三季报':
                report_type = 'q3'
            else:
                report_type = 'other'
            
            # 营业总收入 (元 → 亿元)
            revenue_raw = item.get('TOTALOPERATEREVE') or 0
            revenue = float(revenue_raw) / 1e8 if revenue_raw else 0
            
            # 归母净利润 (元 → 亿元)
            profit_raw = item.get('PARENTNETPROFIT') or 0
            profit = float(profit_raw) / 1e8 if profit_raw else 0
            
            # ROE (加权净资产收益率, 百分比值 → 小数)
            roe_raw = item.get('ROEJQ') or 0
            roe = float(roe_raw) / 100 if roe_raw else 0
            
            # 毛利率 (销售毛利率, 百分比值 → 小数)
            gross_margin_raw = item.get('XSMLL') or 0
            gross_margin = float(gross_margin_raw) / 100 if gross_margin_raw else 0
            
            # 营收同比增速 (百分比值 → 小数)
            revenue_yoy_raw = item.get('TOTALOPERATEREVETZ') or 0
            revenue_yoy = float(revenue_yoy_raw) / 100 if revenue_yoy_raw else 0
            
            # 净利润同比增速 (百分比值 → 小数)
            profit_yoy_raw = item.get('PARENTNETPROFITTZ') or 0
            profit_yoy = float(profit_yoy_raw) / 100 if profit_yoy_raw else 0
            
            reports.append({
                'report_date': report_date,
                'report_type': report_type,
                'report_type_cn': report_type_cn,
                'revenue': round(revenue, 2),
                'net_profit': round(profit, 2),
                'roe': round(roe, 4),
                'gross_margin': round(gross_margin, 4),
                'revenue_yoy': round(revenue_yoy, 4),
                'profit_yoy': round(profit_yoy, 4),
            })
        except (ValueError, TypeError, KeyError) as e:
            continue
    
    return reports


def compute_financial_metrics(reports):
    """
    从财务报表数据计算估值模型所需的参考指标
    
    Args:
        reports: fetch_financial_reports() 的返回值
    
    Returns:
        dict: {
            'avg_roe': 近5年平均ROE,
            'avg_gross_margin': 近5年平均毛利率,
            'gross_margin_stability': 毛利率标准差(越小越稳定),
            'revenue_growth_5y': 5年营收复合增长率(CAGR),
            'latest_revenue_yoy': 最新年报营收同比,
            'latest_profit_yoy': 最新年报净利润同比,
            'roe_trend': ROE趋势(正=改善),
            'annual_reports_count': 可用年报数量,
        }
    """
    if not reports:
        return {}
    
    # 筛选年报
    annuals = [r for r in reports if r['report_type'] == 'annual']
    if not annuals:
        annuals = reports[:5]  # fallback
    
    metrics = {}
    metrics['annual_reports_count'] = len(annuals)
    
    # 近5年(或全部)年报
    recent = annuals[:min(5, len(annuals))]
    
    # 平均ROE
    roes = [r['roe'] for r in recent if r['roe'] > 0]
    if roes:
        metrics['avg_roe'] = round(sum(roes) / len(roes), 4)
    
    # 平均毛利率 & 稳定性
    margins = [r['gross_margin'] for r in recent if r['gross_margin'] > 0]
    if margins:
        metrics['avg_gross_margin'] = round(sum(margins) / len(margins), 4)
        if len(margins) >= 2:
            mean_m = sum(margins) / len(margins)
            variance = sum((m - mean_m) ** 2 for m in margins) / len(margins)
            metrics['gross_margin_stability'] = round(variance ** 0.5, 4)
    
    # 5年营收复合增长率 (CAGR)
    if len(annuals) >= 2:
        latest_rev = annuals[0]['revenue']
        oldest_idx = min(len(annuals) - 1, 4)
        oldest_rev = annuals[oldest_idx]['revenue']
        years = oldest_idx
        if oldest_rev > 0 and latest_rev > 0 and years > 0:
            cagr = (latest_rev / oldest_rev) ** (1.0 / years) - 1
            metrics['revenue_growth_5y'] = round(cagr, 4)
    
    # 最新年报同比
    if annuals[0]['revenue_yoy'] != 0:
        metrics['latest_revenue_yoy'] = annuals[0]['revenue_yoy']
    if annuals[0]['profit_yoy'] != 0:
        metrics['latest_profit_yoy'] = annuals[0]['profit_yoy']
    
    # ROE趋势 (近3年线性斜率, 正=改善)
    if len(recent) >= 3:
        roes_trend = [r['roe'] for r in recent[:3] if r['roe'] > 0]
        if len(roes_trend) >= 2:
            metrics['roe_trend'] = round(roes_trend[0] - roes_trend[-1], 4)
    
    return metrics


def auto_fill_factors(optional_factors, financial_metrics, model_type):
    """
    根据财务报表数据自动填充可选因子(用户未手动指定的)
    
    Args:
        optional_factors: dict, 用户已指定的因子 {key: value}
        financial_metrics: compute_financial_metrics() 的返回值
        model_type: 模型类型字符串
    
    Returns:
        dict: 更新后的因子字典
    """
    if not financial_metrics:
        return optional_factors
    
    updated = dict(optional_factors)
    
    # 自动填充规则 (仅在用户未指定时)
    if 'roe' not in updated or updated['roe'] is None:
        if 'avg_roe' in financial_metrics and financial_metrics['avg_roe'] > 0:
            updated['roe'] = financial_metrics['avg_roe']
            print(f"  [auto] ROE = {financial_metrics['avg_roe']:.2%} (近{financial_metrics.get('annual_reports_count', '?')}年报均值)")
    
    if 'margin_stability' not in updated or updated['margin_stability'] is None:
        if 'gross_margin_stability' in financial_metrics:
            updated['margin_stability'] = financial_metrics['gross_margin_stability']
            print(f"  [auto] 毛利率稳定性 = {financial_metrics['gross_margin_stability']:.4f} (标准差)")
    
    if 'revenue_growth' not in updated or updated['revenue_growth'] is None:
        if 'latest_revenue_yoy' in financial_metrics:
            updated['revenue_growth'] = financial_metrics['latest_revenue_yoy']
            print(f"  [auto] 营收增速 = {financial_metrics['latest_revenue_yoy']:.2%} (最新年报同比)")
    
    return updated


# ===== 10年K线数据获取 =====

def generate_kline_batches(stock_code, exchange, years=10):
    """生成获取N年K线数据所需的批次参数"""
    import datetime
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
    
    return batches


def fetch_kline_batches(stock_code, exchange, batches, output_dir):
    """批量获取K线数据并保存到文件"""
    full_code = f"{exchange}{stock_code}"
    files = []
    
    for i, (start, end) in enumerate(batches):
        filename = os.path.join(output_dir, f'_kline_batch_{i}_{stock_code}.json')
        url = (
            f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={full_code},day,{start},{end},500,qfq"
        )
        headers = {'User-Agent': 'Mozilla/5.0'}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            with open(filename, 'wb') as f:
                f.write(data)
            files.append(filename)
            # 验证数据
            jd = json.loads(data)
            stock_data = jd.get('data', {}).get(full_code, {})
            days = stock_data.get('qfqday') or stock_data.get('day', [])
            print(f"  K线批次{i+1}: {start}~{end} = {len(days)}天")
        except Exception as e:
            print(f"  K线批次{i+1}获取失败: {e}", file=sys.stderr)
    
    return files


# ===== 股票基本信息获取 =====

def fetch_stock_info(stock_code, exchange):
    """
    获取股票基本信息：总股本、行业、最新营收/净利润/毛利率
    
    Returns:
        dict: {
            'total_shares': 总股本(亿股),
            'industry': 行业描述,
            'revenue': 最新年报营收(亿),
            'net_profit': 最新年报净利润(亿),
            'gross_margin': 毛利率(小数),
            'eps_growth': 近5年净利润CAGR,
        }
    """
    secucode = f"{stock_code}.{'SH' if exchange == 'sh' else 'SZ'}"
    info = {}
    
    # 从东方财富获取总股本
    url = (
        f"https://datacenter.eastmoney.com/securities/api/data/v1/get"
        f"?reportName=RPT_F10_FINANCE_MAINFINADATA"
        f"&columns=SECUCODE,TOTAL_SHARE"
        f"&filter=(SECUCODE=%22{secucode}%22)"
        f"&pageSize=1"
        f"&sortColumns=REPORT_DATE"
        f"&sortTypes=-1"
        f"&source=HSF10"
        f"&client=PC"
    )
    data = _fetch_api(url)
    if data and data.get('success') and data.get('result'):
        items = data['result'].get('data', [])
        if items:
            item = items[0]
            # 总股本（股 → 亿股）
            total_shares_raw = item.get('TOTAL_SHARE')
            if total_shares_raw:
                info['total_shares'] = round(float(total_shares_raw) / 1e8, 2)
    
    # 从已有财报获取最新年报数据
    reports = fetch_financial_reports(stock_code, exchange, max_reports=10)
    if reports:
        annuals = [r for r in reports if r['report_type'] == 'annual']
        if annuals:
            latest = annuals[0]
            info['revenue'] = latest['revenue']
            info['net_profit'] = latest['net_profit']
            info['gross_margin'] = latest['gross_margin']
        # 计算近5年净利润CAGR作为默认增速
        if len(annuals) >= 2:
            latest_profit = annuals[0]['net_profit']
            oldest_idx = min(len(annuals) - 1, 4)
            oldest_profit = annuals[oldest_idx]['net_profit']
            years = oldest_idx
            if latest_profit > 0 and oldest_profit > 0 and years > 0:
                cagr = (latest_profit / oldest_profit) ** (1.0 / years) - 1
                info['eps_growth'] = round(cagr, 4)
    
    return info


# ===== 历史PE/PB百分位计算 =====

def fetch_pershare_data(stock_code, exchange, max_years=10):
    """
    获取历年每股收益(EPS)和每股净资产(BPS)
    
    Returns:
        list of dict: [{'year': 2024, 'eps': 1.82, 'bps': 5.31}, ...]  从新到旧
    """
    secucode = f"{stock_code}.{'SH' if exchange == 'sh' else 'SZ'}"
    report_type_encoded = urllib.parse.quote('年报')
    url = (
        f"https://datacenter.eastmoney.com/securities/api/data/v1/get"
        f"?reportName=RPT_F10_FINANCE_MAINFINADATA"
        f"&columns=REPORT_DATE,EPSJB,BPS"
        f"&filter=(SECUCODE=%22{secucode}%22)(REPORT_TYPE=%22{report_type_encoded}%22)"
        f"&pageSize={max_years}"
        f"&sortColumns=REPORT_DATE"
        f"&sortTypes=-1"
        f"&source=HSF10"
        f"&client=PC"
    )
    data = _fetch_api(url)
    if not data or not data.get('success') or not data.get('result'):
        return []
    
    items = data['result'].get('data', [])
    result = []
    for item in items:
        try:
            report_date = item.get('REPORT_DATE', '')[:10]
            year = int(report_date[:4])
            eps = float(item.get('EPSJB') or 0)
            bps = float(item.get('BPS') or 0)
            if eps > 0 and bps > 0:
                result.append({'year': year, 'eps': eps, 'bps': bps})
        except (ValueError, TypeError):
            continue
    return result


def compute_valuation_range(kline_data, pershare_data, current_pe=0, current_pb=0):
    """
    从历史K线+每年EPS/BPS计算PE/PB百分位区间
    
    Args:
        kline_data: [[date, open, close, high, low, volume], ...]
        pershare_data: [{'year': 2024, 'eps': 1.82, 'bps': 5.31}, ...]
        current_pe: 当前PE（用于兜底）
        current_pb: 当前PB（用于兜底）
    
    Returns:
        dict: {'pe_min', 'pe_max', 'pb_min', 'pb_max'}
    """
    if not pershare_data or not kline_data:
        # 兜底：用当前值的 0.5x ~ 2.0x
        pe = current_pe if current_pe > 0 else 15
        pb = current_pb if current_pb > 0 else 2
        return {
            'pe_min': round(pe * 0.5, 1),
            'pe_max': round(pe * 2.0, 1),
            'pb_min': round(pb * 0.5, 1),
            'pb_max': round(pb * 2.0, 1),
        }
    
    # 建立年份 -> EPS/BPS 映射
    eps_map = {d['year']: d['eps'] for d in pershare_data}
    bps_map = {d['year']: d['bps'] for d in pershare_data}
    
    pe_values = []
    pb_values = []
    
    for row in kline_data:
        try:
            date_str = row[0]  # "2024-03-15"
            close = float(row[2])  # 收盘价
            year = int(date_str[:4])
            
            if close <= 0:
                continue
            
            # 使用当年或前一年的EPS/BPS
            eps = eps_map.get(year) or eps_map.get(year - 1)
            bps = bps_map.get(year) or bps_map.get(year - 1)
            
            if eps and eps > 0:
                pe = close / eps
                if 0 < pe < 500:  # 过滤异常值
                    pe_values.append(pe)
            if bps and bps > 0:
                pb = close / bps
                if 0 < pb < 50:  # 过滤异常值
                    pb_values.append(pb)
        except (ValueError, IndexError):
            continue
    
    result = {}
    
    if len(pe_values) >= 50:
        pe_values.sort()
        result['pe_min'] = round(pe_values[int(len(pe_values) * 0.1)], 1)
        result['pe_max'] = round(pe_values[int(len(pe_values) * 0.9)], 1)
    else:
        pe = current_pe if current_pe > 0 else 15
        result['pe_min'] = round(pe * 0.5, 1)
        result['pe_max'] = round(pe * 2.0, 1)
    
    if len(pb_values) >= 50:
        pb_values.sort()
        result['pb_min'] = round(pb_values[int(len(pb_values) * 0.1)], 1)
        result['pb_max'] = round(pb_values[int(len(pb_values) * 0.9)], 1)
    else:
        pb = current_pb if current_pb > 0 else 2
        result['pb_min'] = round(pb * 0.5, 1)
        result['pb_max'] = round(pb * 2.0, 1)
    
    # 确保 min < max
    if result['pe_min'] >= result['pe_max']:
        result['pe_min'], result['pe_max'] = result['pe_max'], result['pe_min']
    if result['pb_min'] >= result['pb_max']:
        result['pb_min'], result['pb_max'] = result['pb_max'], result['pb_min']
    
    print(f"  [auto] PE区间: {result['pe_min']} ~ {result['pe_max']} (10th/90th百分位, {len(pe_values)}个样本)")
    print(f"  [auto] PB区间: {result['pb_min']} ~ {result['pb_max']} (10th/90th百分位, {len(pb_values)}个样本)")
    
    return result
