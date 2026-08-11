#!/usr/bin/env python3
"""
growth_params.md 全股票批量估值报告生成
- 基本面分析 + 板块划分 + 模型映射
- 周期股自动获取商品价格偏离度(akshare)
- 支持增量（kline_cache已内置增量逻辑）
- 每日可重复运行，同日不重复拉取
"""
import os
import sys
import subprocess
import time
import traceback

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)

# ============================================================
# 股票池：基于 growth_params.md 典型公司，按8大板块划分
# 格式: (代码, 名称, 模型, 额外因子字典或None)
# ============================================================

STOCK_POOL = {
    # ─── staples 必选消费 ───
    # 特征：业绩稳定、现金流好、需求刚性、防御性强
    'staples': [
        ('603899', '晨光股份', {'margin_stability': 0.015}),   # 文具龙头，毛利率极稳定
        ('600887', '伊利股份', None),                           # 乳制品龙头
        ('603288', '海天味业', {'margin_stability': 0.01}),     # 调味品龙头，高毛利稳定
        ('603345', '安井食品', None),                           # 速冻食品龙头
        ('000895', '双汇发展', None),                           # 肉制品龙头
    ],

    # ─── discretionary 可选消费 ───
    # 特征：品牌溢价明显、PEG敏感、受消费周期影响
    'discretionary': [
        ('601888', '中国中免', None),                           # 免税龙头
        ('600519', '贵州茅台', {'brand_premium': 2.5}),         # 高端白酒，极强品牌力
        ('600809', '山西汾酒', {'brand_premium': 1.8}),         # 次高端白酒
        ('000333', '美的集团', None),                           # 家电龙头
    ],

    # ─── tech 科技制造 ───
    # 特征：高研发投入、增速快、PEG权重最高
    'tech': [
        ('601689', '拓普集团', {'rd_ratio': 0.06}),             # 汽车零部件
        ('688041', '海光信息', {'rd_ratio': 0.28}),             # 国产CPU设计
        ('600584', '长电科技', {'rd_ratio': 0.05}),             # 半导体封测
        ('603078', '江化微', {'rd_ratio': 0.07}),               # 湿电子化学品
        ('688083', '中望软件', {'rd_ratio': 0.30}),             # 工业软件
    ],

    # ─── cyclical 周期资源 ───
    # 特征：业绩随商品价格大幅波动，需追踪商品价格位置
    # commodity_dev 由 akshare 实时计算
    'cyclical': [
        ('601899', '紫金矿业', {'capacity_util': 0.88}),        # 黄金/铜采选
        ('601600', '中国铝业', {'capacity_util': 0.82}),        # 铝
        ('600096', '云天化', {'capacity_util': 0.85}),          # 磷化工
        ('601919', '中远海控', None),                           # 集装箱航运
        ('600938', '中国海油', {'capacity_util': 0.92}),        # 海上油气
    ],

    # ─── soe 央企基建 ───
    # 特征：高股息、订单驱动、PB估值为主
    'soe': [
        ('601668', '中国建筑', {'dividend_yield': 0.05, 'order_growth': 0.10}),
        ('600011', '华能国际', {'dividend_yield': 0.04}),
        ('600377', '宁沪高速', {'dividend_yield': 0.05}),
        ('600941', '中国移动', {'dividend_yield': 0.05}),
        ('601816', '京沪高铁', None),
        ('601088', '中国神华', {'dividend_yield': 0.07}),       # 煤炭央企，高股息核心逻辑
        ('002398', '垒知集团', None),                           # 建筑外加剂+工程检测，订单驱动
    ],

    # ─── bank 银行保险 ───
    # 特征：PB为核心、ROE决定估值、PE权重为0
    'bank': [
        ('601398', '工商银行', {'dividend_yield': 0.055, 'npl_ratio': 0.013}),
        ('600036', '招商银行', {'dividend_yield': 0.04, 'npl_ratio': 0.009}),
        ('002142', '宁波银行', {'npl_ratio': 0.008}),
        ('601318', '中国平安', {'dividend_yield': 0.04}),       # 保险
        ('600030', '中信证券', None),                           # 证券
    ],

    # ─── realestate 地产 ───
    # 特征：NAV折价为核心、去化率和杠杆率为关键
    'realestate': [
        ('600048', '保利发展', {'leverage': 0.45}),             # 央企地产龙头
    ],

    # ─── pharma 医药消费 ───
    # 特征：营收增速权重高、受政策周期影响大
    'pharma': [
        ('600276', '恒瑞医药', None),                           # 化学制药龙头
        ('600436', '片仔癀', None),                             # 中药龙头
        ('300760', '迈瑞医疗', None),                           # 医疗器械龙头
        ('603259', '药明康德', None),                           # CXO龙头
        ('300015', '爱尔眼科', None),                           # 眼科连锁
    ],
}

# 周期股对应的商品期货品种 (akshare获取)
COMMODITY_MAP = {
    '601899': ('黄金', 'AU0', '沪金'),        # 紫金矿业 → 金价
    '601600': ('铝', 'AL0', '沪铝'),          # 中国铝业 → 铝价
    '600096': ('磷矿石', None, '磷化工'),     # 云天化 → 磷矿石(无期货,用化工指数)
    '601919': ('航运', None, '集运指数'),     # 中远海控 → 集运指数
    '600938': ('原油', 'SC0', '原油'),        # 中国海油 → 原油
}


def get_commodity_deviation(stock_code):
    """
    使用akshare获取商品价格，计算相对历史均值的偏离度
    返回: float (如 -0.10 表示低于均值10%) 或 None
    """
    try:
        import akshare as ak
        import pandas as pd

        commodity_info = COMMODITY_MAP.get(stock_code)
        if not commodity_info:
            return None

        name, symbol, label = commodity_info

        if symbol is None:
            # 无直接期货品种，尝试用现货价格
            return _get_spot_deviation(stock_code, name, ak)

        # 获取期货主力连续合约日线
        try:
            df = ak.futures_main_sina(symbol=symbol, start_date="20200101", end_date="20261231")
            if df is not None and len(df) > 100:
                close_col = '收盘价' if '收盘价' in df.columns else 'close'
                prices = df[close_col].astype(float)
                current = prices.iloc[-1]
                # 用近3年均值作为基准
                hist_mean = prices.tail(750).mean() if len(prices) >= 750 else prices.mean()
                deviation = (current - hist_mean) / hist_mean
                print(f"    [{label}] 当前={current:.1f}, 均值={hist_mean:.1f}, 偏离={deviation:+.2%}")
                return round(deviation, 4)
        except Exception as e:
            print(f"    [{label}] 期货数据获取失败: {e}")

        return None

    except ImportError:
        print("    [warn] akshare未安装，跳过商品价格获取")
        return None
    except Exception as e:
        print(f"    [warn] 商品价格获取异常: {e}")
        return None


def _get_spot_deviation(stock_code, commodity_name, ak):
    """获取现货价格偏离度（无期货品种时的备选方案）"""
    try:
        if stock_code == '600096':
            # 磷矿石：用化工行业指数代替
            df = ak.futures_main_sina(symbol="MA0", start_date="20200101", end_date="20261231")
            if df is not None and len(df) > 100:
                close_col = '收盘价' if '收盘价' in df.columns else 'close'
                prices = df[close_col].astype(float)
                current = prices.iloc[-1]
                hist_mean = prices.tail(750).mean() if len(prices) >= 750 else prices.mean()
                deviation = (current - hist_mean) / hist_mean
                print(f"    [甲醇/化工 proxy] 偏离={deviation:+.2%}")
                return round(deviation, 4)
        elif stock_code == '601919':
            # 集运指数
            df = ak.futures_main_sina(symbol="EC0", start_date="20230101", end_date="20261231")
            if df is not None and len(df) > 50:
                close_col = '收盘价' if '收盘价' in df.columns else 'close'
                prices = df[close_col].astype(float)
                current = prices.iloc[-1]
                hist_mean = prices.mean()
                deviation = (current - hist_mean) / hist_mean
                print(f"    [集运指数] 偏离={deviation:+.2%}")
                return round(deviation, 4)
    except Exception as e:
        print(f"    [spot] {commodity_name} 现货获取失败: {e}")
    return None


def build_single_stock(code, name, model, extra_factors=None):
    """调用 build_report.py 生成单只股票报告"""
    cmd = [
        sys.executable, os.path.join(_SCRIPT_DIR, 'build_report.py'),
        code, '--model', model, '--name', name
    ]

    # 添加额外因子
    if extra_factors:
        for key, val in extra_factors.items():
            cmd.append(f'--{key}:{val}')

    print(f"  命令: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=_SKILL_DIR,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=300
    )

    if result.returncode != 0:
        print(f"  [ERROR] {name}({code}) 生成失败:")
        print(f"  {result.stderr[-500:]}" if result.stderr else "  无错误输出")
        return False
    else:
        # 打印关键输出
        for line in result.stdout.splitlines():
            if any(k in line for k in ['分数', '状态', '报告已保存', '缓存', '增量', 'auto']):
                print(f"  {line.strip()}")
        return True


def main():
    print("=" * 60)
    print("  growth_params.md 全股票批量估值报告")
    print(f"  共 {sum(len(v) for v in STOCK_POOL.values())} 只股票, 8大板块")
    print("=" * 60)

    total = 0
    success = 0
    failed = []

    for model, stocks in STOCK_POOL.items():
        print(f"\n{'─' * 50}")
        print(f"  板块: {model} ({len(stocks)}只)")
        print(f"{'─' * 50}")

        for code, name, factors in stocks:
            total += 1
            print(f"\n[{total}] {name}({code}) - {model}")

            # 周期股：获取商品价格偏离度
            if model == 'cyclical' and code in COMMODITY_MAP:
                dev = get_commodity_deviation(code)
                if dev is not None:
                    if factors is None:
                        factors = {}
                    factors['commodity_dev'] = dev

            try:
                ok = build_single_stock(code, name, model, factors)
                if ok:
                    success += 1
                else:
                    failed.append(f"{name}({code})")
            except subprocess.TimeoutExpired:
                print(f"  [TIMEOUT] {name}({code}) 超时")
                failed.append(f"{name}({code})")
            except Exception as e:
                print(f"  [EXCEPTION] {name}({code}): {e}")
                failed.append(f"{name}({code})")

            # 避免API限流，每只股票间隔1秒
            time.sleep(1)

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  完成: {success}/{total} 成功")
    if failed:
        print(f"  失败: {', '.join(failed)}")
    print(f"  报告目录: {os.path.join(_SKILL_DIR, 'local_reports')}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
