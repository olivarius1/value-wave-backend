#!/usr/bin/env python3
"""
估值报告生成入口
用法: python build_report.py 600887 --model staples [--pe MIN MAX] [--pb MIN MAX] [--digest-growth 0.6]
"""
import os
import sys
import datetime
import unicodedata

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)


# 人工校准区间（盈利 regime 切换后全10年自动区间失真，见 templates/growth_params.md 8.4c）：
# 命中时强制线性映射（use_rank=False），命令行 --pe/--pb 优先级更高
MANUAL_RANGES = {
    '601899': {'pe': (8.3, 18.0), 'pb': (2.35, 5.1)},   # 紫金矿业：净利5年增长20倍，全10年区间致PE/PB双0分硬截断
}


def _run_new_format():
    """新格式：argparse + 自动获取 + 缓存"""
    import argparse

    parser = argparse.ArgumentParser(
        description='A股估值报告生成工具',
        epilog='示例: python build_report.py 600887 --model staples'
    )
    parser.add_argument('code', help='股票代码 (如 600887)')
    parser.add_argument('--model', required=True,
                        choices=['staples', 'discretionary', 'tech', 'cyclical',
                                 'soe', 'bank', 'realestate', 'pharma', 'growth'],
                        help='行业估值模型')
    parser.add_argument('--pe', nargs=2, type=float, metavar=('MIN', 'MAX'),
                        help='手动PE区间 (覆盖自动计算)')
    parser.add_argument('--pb', nargs=2, type=float, metavar=('MIN', 'MAX'),
                        help='手动PB区间 (覆盖自动计算)')
    parser.add_argument('--growth', type=float, help='手动预期增速 (覆盖自动)')
    parser.add_argument('--name', help='手动股票名称 (覆盖自动)')
    parser.add_argument('--no-cache', action='store_true', help='强制全量刷新K线')
    parser.add_argument('--subtitle', help='报告副标题')
    parser.add_argument('--dps', type=float, help='每股年分红DPS(元)，股息率因子动态化：历史每日股息率=dps/当日价 (A2方案)')
    parser.add_argument('--digest-growth', type=float, default=0.60,
                        help='估值消化曲线触发阈值：高估档且最新报告期净利同比>=该值时，生成增速兑现假设下的消化版分数曲线 (默认 0.60)')

    # 解析已知参数，剩余的用 --key:value 格式解析为可选因子
    args, remaining = parser.parse_known_args()

    # 解析可选因子 (--roe:0.15 格式)
    optional_factors = {}
    for arg in remaining:
        if arg.startswith('--') and ':' in arg:
            fkey, fval = arg[2:].split(':', 1)
            try:
                optional_factors[fkey] = float(fval)
            except ValueError:
                pass

    stock_code = args.code
    exchange = 'sh' if stock_code.startswith('6') else 'sz'

    print(f"=== {stock_code} 估值报告生成 ===")

    # 1. 获取K线数据（带缓存+增量）
    from kline_cache import get_kline, get_kline_raw
    kline_result = get_kline(stock_code, exchange, no_cache=args.no_cache)
    kline_data = kline_result['kline']
    # 不复权真实价K线（历史 PE/PB 必须用当日真实交易价；前复权价会随最新除权整体缩放导致失真）
    raw_kline = get_kline_raw(stock_code, exchange, no_cache=args.no_cache)
    qt_pe = kline_result['pe']
    qt_pb = kline_result['pb']
    qt_price = kline_result['price']
    stock_name = args.name or kline_result['name'] or stock_code
    # NFKC 规范化：接口返回的全角字母/数字（如 粤电力Ａ）转半角，保证报告文件名与 watchlist 名称一致
    stock_name = unicodedata.normalize('NFKC', stock_name).strip()

    # ===== 盘中虚拟点检测（方案A：内存拼接，不写缓存）=====
    # 判断条件：qt 返回了当天日期的实时价格，且 K 线最后一天 < 今天（盘中）
    # 收盘后正式K线数据包含当日，条件不再满足，盘中点自动消失
    has_intraday = False
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    qt_date = str(kline_result.get('qt_date') or '')
    if qt_date == today_str and qt_price > 0 and kline_data and kline_data[-1][0] < today_str:
        intraday_vol = kline_result.get('volume', 0) or 0
        # 盘中无开高低，均用实时价；成交量为盘中累计量
        kline_data = kline_data + [[today_str, qt_price, qt_price, qt_price, qt_price, intraday_vol]]
        has_intraday = True
        print(f"  盘中模式: 检测到今日({today_str})实时价格 {qt_price:.2f}，追加盘中虚拟点")

    if not kline_data:
        print("错误: 无法获取K线数据，请检查网络连接")
        sys.exit(1)

    print(f"  股票: {stock_name}({stock_code}) | {len(kline_data)}天K线")

    # 2. 获取股票基本信息（总股本、行业、营收等）
    from financial_fetcher import (
        fetch_stock_info, fetch_pershare_data,
        compute_valuation_range, fetch_financial_reports,
        compute_financial_metrics, auto_fill_factors, fetch_industry
    )

    print(f"  获取股票基本信息...")
    stock_info = fetch_stock_info(stock_code, exchange)

    total_shares = stock_info.get('total_shares', 0)
    industry = stock_info.get('industry', '')
    if not industry:
        # info 缓存无行业字段时从 push2 接口补拉（东财三级行业，如 工业金属）
        industry = fetch_industry(stock_code, exchange)
        if industry:
            print(f"  [auto] 行业 = {industry} (东财行业分类)")
    revenue = stock_info.get('revenue', 0)
    net_profit = stock_info.get('net_profit', 0)
    gross_margin = stock_info.get('gross_margin', 0)
    eps_growth = args.growth if args.growth is not None else stock_info.get('eps_growth', 0.08)

    # 市值 = 总股本 × 当前股价
    market_cap = round(total_shares * qt_price, 0) if total_shares and qt_price else 0

    # 3. 获取历年EPS/BPS序列（用于PE/PB区间计算 & 真实历史PE评分口径）
    pershare_data = fetch_pershare_data(stock_code, exchange)

    # 3b. 分红送转事件 → EPS/BPS 逐日滚动重述序列（口径连续性修复），
    #     并检测序列覆盖：生效年缺失时评分回退当前值反推（历史曲线失真），必须显式警告
    from financial_fetcher import (
        fetch_bonus_events, build_adjusted_series, detect_series_coverage
    )
    bonus_events = fetch_bonus_events(stock_code, exchange)
    adj_series = build_adjusted_series(
        pershare_data, bonus_events, [r[0] for r in kline_data])
    for _w in detect_series_coverage(pershare_data, kline_data, bonus_events):
        print(f"  [警告] {_w}")
    if adj_series:
        _n_split = sum(1 for e in bonus_events if e['ratio'] > 0)
        _n_div = len(bonus_events) - _n_split
        print(f"  [auto] EPS/BPS 逐日重述: {len(adj_series['eps'])}天"
              f" | 送转{_n_split}次 派息{_n_div}次")

    # 计算PE/PB区间（命令行手动指定 > MANUAL_RANGES 校准区间 > 自动10th/90th百分位）
    manual_ranges = MANUAL_RANGES.get(stock_code, {})
    if args.pe:
        pe_min, pe_max = args.pe
        print(f"  PE区间: {pe_min} ~ {pe_max} (手动指定)")
    elif 'pe' in manual_ranges:
        pe_min, pe_max = manual_ranges['pe']
        print(f"  PE区间: {pe_min} ~ {pe_max} (校准区间)")
        if 'pb' not in manual_ranges and not args.pb:
            val_range = compute_valuation_range(raw_kline or kline_data, pershare_data, qt_pe, qt_pb)
            pb_min, pb_max = val_range['pb_min'], val_range['pb_max']
    else:
        print(f"  计算历史PE/PB百分位区间...")
        val_range = compute_valuation_range(raw_kline or kline_data, pershare_data, qt_pe, qt_pb)
        pe_min = val_range['pe_min']
        pe_max = val_range['pe_max']
        if not args.pb and 'pb' not in manual_ranges:
            pb_min = val_range['pb_min']
            pb_max = val_range['pb_max']

    if args.pb:
        pb_min, pb_max = args.pb
        print(f"  PB区间: {pb_min} ~ {pb_max} (手动指定)")
    elif 'pb' in manual_ranges:
        pb_min, pb_max = manual_ranges['pb']
        print(f"  PB区间: {pb_min} ~ {pb_max} (校准区间)")
    elif not args.pe:
        pass  # 已从 val_range 获取
    else:
        # 只指定了PE没指定PB，需要单独计算PB
        val_range = compute_valuation_range(raw_kline or kline_data, pershare_data, qt_pe, qt_pb)
        pb_min = val_range['pb_min']
        pb_max = val_range['pb_max']

    # 4. 自动填充可选因子
    model_type = args.model
    reports = fetch_financial_reports(stock_code, exchange)
    if reports:
        metrics = compute_financial_metrics(reports)
        optional_factors = auto_fill_factors(optional_factors, metrics, model_type)

    # 最新报告期净利润同比（盈利动能，用于报告提示注释；--growth 可覆盖；无数据时回退CAGR）
    latest_yoy = None
    latest_report_label = '最新报告期'
    if args.growth is not None:
        latest_yoy = args.growth
        latest_report_label = '手动预期'
    elif reports and reports[0].get('profit_yoy'):
        r0 = reports[0]
        latest_yoy = r0['profit_yoy']
        latest_report_label = f"{r0.get('report_date', '')[:4]} {r0.get('report_type_cn', '最新报告期')}"
        print(f"  [auto] 最新报告期增速 = {latest_yoy:.0%} ({latest_report_label}净利润同比)")

    # 动态DPS股息率：若提供每股分红且未手动指定股息率，自动换算当前股息率（保证因子权重生效）
    if args.dps and args.dps > 0 and 'dividend_yield' not in optional_factors:
        if qt_price and qt_price > 0:
            optional_factors['dividend_yield'] = round(args.dps / qt_price, 4)
            print(f"  [auto] 股息率 = {optional_factors['dividend_yield']:.2%} (DPS {args.dps:.2f}元 / 现价 {qt_price:.2f}元，历史逐日动态)")

    # 5. 构建配置并调用报告生成器
    subtitle = args.subtitle or f"{stock_name}估值框架与10年回测"
    gross_margin_str = f"{gross_margin:.1%}" if isinstance(gross_margin, float) and gross_margin > 0 else str(gross_margin)

    config = {
        'code': stock_code,
        'name': stock_name,
        'exchange': exchange,
        'total_shares': total_shares or 1,
        'pe_min': pe_min,
        'pe_max': pe_max,
        'pb_min': pb_min,
        'pb_max': pb_max,
        # PE/PB评分映射模式：手动指定或校准区间时保留线性映射（人工锚点），否则用历史百分位rank（避免触顶饱和）
        'use_rank_pe': args.pe is None and 'pe' not in manual_ranges,
        'use_rank_pb': args.pb is None and 'pb' not in manual_ranges,
        'eps_growth': eps_growth,
        'latest_yoy': latest_yoy,
        'latest_report_label': latest_report_label,
        'revenue': revenue or '0',
        'net_profit': net_profit or '0',
        'gross_margin': gross_margin_str,
        'market_cap': market_cap or '0',
        'industry': industry or '',
        'subtitle': subtitle,
        'model': model_type,
        'optional_factors': optional_factors,
        'dps': args.dps,
        'kline_files': [],
        'kline_data': kline_data,
        'raw_kline': raw_kline,
        'qt_pe': qt_pe,
        'qt_pb': qt_pb,
        'qt_price': qt_price,
        'has_intraday': has_intraday,
        'pershare_data': pershare_data,
        'adj_series': adj_series,
        'digest_growth': args.digest_growth,
    }

    # 通过全局变量传递config，exec report_generator.py
    # 注意：必须用同一个dict作为globals和locals，避免Python 3生成器表达式作用域问题
    _real_script = os.path.join(_SCRIPT_DIR, 'report_generator.py')
    _exec_globals = {'__builtins__': __builtins__, '_REPORT_CONFIG': config, '__file__': _real_script, '__name__': '__main__'}
    exec(compile(open(_real_script, encoding='utf-8').read(), _real_script, 'exec'), _exec_globals)


if __name__ == '__main__':
    _run_new_format()
