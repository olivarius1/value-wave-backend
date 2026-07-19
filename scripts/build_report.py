#!/usr/bin/env python3
"""
估值报告生成入口
新格式: python build_report.py 600887 --model staples
旧格式: python build_report.py 600887 "伊利股份" sh 63.25 15 35 ... (16+位置参数)
"""
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)


def _is_old_format():
    """检测是否为旧格式（16+位置参数，第2个参数不含--）"""
    if len(sys.argv) < 17:
        return False
    # 旧格式第2个参数是股票名称（不含--）
    return not sys.argv[2].startswith('--')


def _run_old_format():
    """旧格式：直接exec report_generator.py（它自己解析sys.argv）"""
    _real_script = os.path.join(_SCRIPT_DIR, 'report_generator.py')
    sys.argv[0] = _real_script
    _exec_globals = {'__builtins__': __builtins__, '__file__': _real_script, '__name__': '__main__'}
    exec(compile(open(_real_script, encoding='utf-8').read(), _real_script, 'exec'), _exec_globals)


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
    from kline_cache import get_kline
    kline_result = get_kline(stock_code, exchange, no_cache=args.no_cache)
    kline_data = kline_result['kline']
    qt_pe = kline_result['pe']
    qt_pb = kline_result['pb']
    qt_price = kline_result['price']
    stock_name = args.name or kline_result['name'] or stock_code

    if not kline_data:
        print("错误: 无法获取K线数据，请检查网络连接")
        sys.exit(1)

    print(f"  股票: {stock_name}({stock_code}) | {len(kline_data)}天K线")

    # 2. 获取股票基本信息（总股本、行业、营收等）
    from financial_fetcher import (
        fetch_stock_info, fetch_pershare_data,
        compute_valuation_range, fetch_financial_reports,
        compute_financial_metrics, auto_fill_factors
    )

    print(f"  获取股票基本信息...")
    stock_info = fetch_stock_info(stock_code, exchange)

    total_shares = stock_info.get('total_shares', 0)
    industry = stock_info.get('industry', '')
    revenue = stock_info.get('revenue', 0)
    net_profit = stock_info.get('net_profit', 0)
    gross_margin = stock_info.get('gross_margin', 0)
    eps_growth = args.growth if args.growth is not None else stock_info.get('eps_growth', 0.08)

    # 市值 = 总股本 × 当前股价
    market_cap = round(total_shares * qt_price, 0) if total_shares and qt_price else 0

    # 3. 计算PE/PB区间（如果用户未手动指定）
    if args.pe:
        pe_min, pe_max = args.pe
        print(f"  PE区间: {pe_min} ~ {pe_max} (手动指定)")
    else:
        print(f"  计算历史PE/PB百分位区间...")
        pershare_data = fetch_pershare_data(stock_code, exchange)
        val_range = compute_valuation_range(kline_data, pershare_data, qt_pe, qt_pb)
        pe_min = val_range['pe_min']
        pe_max = val_range['pe_max']
        if not args.pb:
            pb_min = val_range['pb_min']
            pb_max = val_range['pb_max']

    if args.pb:
        pb_min, pb_max = args.pb
        print(f"  PB区间: {pb_min} ~ {pb_max} (手动指定)")
    elif not args.pe:
        pass  # 已从 val_range 获取
    else:
        # 只指定了PE没指定PB，需要单独计算PB
        pershare_data = fetch_pershare_data(stock_code, exchange) if 'pershare_data' not in dir() else pershare_data
        val_range = compute_valuation_range(kline_data, pershare_data, qt_pe, qt_pb)
        pb_min = val_range['pb_min']
        pb_max = val_range['pb_max']

    # 4. 自动填充可选因子
    model_type = args.model
    reports = fetch_financial_reports(stock_code, exchange)
    if reports:
        metrics = compute_financial_metrics(reports)
        optional_factors = auto_fill_factors(optional_factors, metrics, model_type)

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
        'eps_growth': eps_growth,
        'revenue': revenue or '0',
        'net_profit': net_profit or '0',
        'gross_margin': gross_margin_str,
        'market_cap': market_cap or '0',
        'industry': industry or '未知行业',
        'subtitle': subtitle,
        'model': model_type,
        'optional_factors': optional_factors,
        'kline_files': [],
        'kline_data': kline_data,
        'qt_pe': qt_pe,
        'qt_pb': qt_pb,
        'qt_price': qt_price,
    }

    # 通过全局变量传递config，exec report_generator.py
    # 注意：必须用同一个dict作为globals和locals，避免Python 3生成器表达式作用域问题
    _real_script = os.path.join(_SCRIPT_DIR, 'report_generator.py')
    _exec_globals = {'__builtins__': __builtins__, '_REPORT_CONFIG': config, '__file__': _real_script, '__name__': '__main__'}
    exec(compile(open(_real_script, encoding='utf-8').read(), _real_script, 'exec'), _exec_globals)


if __name__ == '__main__':
    if _is_old_format():
        _run_old_format()
    else:
        _run_new_format()
