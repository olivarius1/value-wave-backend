# -*- coding: utf-8 -*-
"""
回测结果 HTML 报告生成（暗色主题，复用项目 _shared/js/echarts.min.js）

图表：
1. 组合策略 vs 买入持有基准净值曲线
2. 固定阈值分层（<40 / 40-70 / >=70）未来 60/250 日平均收益
3. 五等分桶未来收益（稳健性检查）
4. 分数 vs 未来 250 日收益散点（采样展示）
"""
import json
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
ECHARTS_PATH = '../../../_shared/js/echarts.min.js'


def _js(obj):
    """JSON → JS 字面量（防 </script> 注入）"""
    return json.dumps(obj, ensure_ascii=False).replace('</', '<\\/')


def _fmt(x, digits=4):
    if x is None:
        return '-'
    return f"{x:.{digits}f}"


def _pct(x, digits=1):
    if x is None:
        return '-'
    return f"{x * 100:.{digits}f}%"


def build_html_report(html_path, meta, metrics, stock_daily, fut_map, stocks_meta, pct_thr=None):
    ic = metrics['ic']
    layers = metrics['layers']
    strategy = metrics['strategy']
    strategy_pct = metrics.get('strategy_pct')

    # 散点数据（采样 ≤ 3000 点）
    scatter = []
    step = max(1, sum(len(v) for v in stock_daily.values()) // 3000)
    for code, daily in stock_daily.items():
        for d in daily[::step]:
            fut = fut_map[code].get(d['date'], {}).get('fut_250')
            if fut is not None:
                scatter.append([d['score'], round(fut * 100, 2)])

    # 分层表数据（fixed 60）
    layer_rows = ''
    for i, b in enumerate(layers['fixed'][:3]):
        layer_rows += (
            f"<tr><td>{b['bucket']}</td><td>{b['n']}</td>"
            f"<td>{_pct(b['mean_ret'])}</td><td>{_pct(b['winrate'])}</td></tr>")
    layer_rows_250 = ''
    for b in layers['fixed'][3:]:
        layer_rows_250 += (
            f"<tr><td>{b['bucket']}</td><td>{b['n']}</td>"
            f"<td>{_pct(b['mean_ret'])}</td><td>{_pct(b['winrate'])}</td></tr>")

    # 个股表
    stock_rows = ''
    for s in sorted(stocks_meta, key=lambda x: x['code']):
        stock_rows += (
            f"<tr><td>{s['name']}</td><td>{s['code']}</td><td>{s['model']}</td>"
            f"<td>{s['samples']}</td><td>{s['first_score']}</td><td>{s['last_score']}</td></tr>")

    strat = strategy['strategy'] if strategy else {}
    bench = strategy['bench'] if strategy else {}
    strat_pct = (strategy_pct or {}).get('strategy') or {}
    strat_html = (f"<tr><td>策略A：绝对分数（分数&gt;=70持仓/&lt;40空仓）</td><td>{_pct(strat.get('total_return'))}</td>"
                  f"<td>{_pct(strat.get('annualized'))}</td><td>{_pct(strat.get('max_drawdown'), 2)}</td>"
                  f"<td>{_pct(strategy.get('winrate'))}</td><td>{_pct(strategy.get('turnover_ratio'))}</td></tr>"
                  f"<tr><td>策略B：个股历史百分位（&gt;=自身p80持仓/&lt;=自身p20空仓）</td><td>{_pct(strat_pct.get('total_return'))}</td>"
                  f"<td>{_pct(strat_pct.get('annualized'))}</td><td>{_pct(strat_pct.get('max_drawdown'), 2)}</td>"
                  f"<td>{_pct((strategy_pct or {}).get('winrate'))}</td><td>{_pct((strategy_pct or {}).get('turnover_ratio'))}</td></tr>"
                  f"<tr><td>基准（等权买入持有）</td><td>{_pct(bench.get('total_return'))}</td>"
                  f"<td>{_pct(bench.get('annualized'))}</td><td>{_pct(bench.get('max_drawdown'), 2)}</td>"
                  f"<td>-</td><td>-</td></tr>")

    params = meta['params']
    run_id = meta['run_id']
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>估值评分回测报告 {meta['run_id']}</title>
<script src="{ECHARTS_PATH}"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0f172a; color: #e2e8f0; font-family: 'Microsoft YaHei', sans-serif; padding: 24px; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin-bottom: 6px; }}
  .sub {{ color: #94a3b8; font-size: 13px; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #1e293b; border-radius: 10px; padding: 14px; }}
  .card .v {{ font-size: 20px; font-weight: 700; }}
  .card .l {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
  .card .good {{ color: #34d399; }} .card .bad {{ color: #f87171; }} .card .neu {{ color: #fbbf24; }}
  .chart {{ background: #1e293b; border-radius: 10px; padding: 16px; margin-bottom: 24px; }}
  .chart h3 {{ font-size: 15px; margin-bottom: 10px; color: #f1f5f9; }}
  .chart .note {{ font-size: 12px; color: #64748b; margin-top: 6px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; margin-bottom: 24px; font-size: 13px; }}
  th, td {{ padding: 8px 12px; text-align: right; border-bottom: 1px solid #334155; }}
  th {{ background: #1e3a5f; color: #93c5fd; font-weight: 600; }}
  td:first-child, th:first-child {{ text-align: left; }}
  tr:last-child td {{ border-bottom: none; }}
  .section {{ margin-bottom: 28px; }}
  .section h2 {{ font-size: 17px; color: #f1f5f9; margin-bottom: 12px; border-left: 4px solid #3b82f6; padding-left: 10px; }}
  .tag {{ display: inline-block; background: #334155; color: #cbd5e1; border-radius: 4px; padding: 1px 8px; font-size: 12px; margin: 0 4px 4px 0; }}
  .desc {{ color: #94a3b8; font-size: 13px; line-height: 1.7; margin-bottom: 16px; }}
  .scroll {{ max-height: 420px; overflow-y: auto; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>估值评分回测报告</h1>
  <div class="sub">run_id: {meta['run_id']} &middot; 生成于 {meta['generated_at'][:19]} &middot; {meta['stock_count']} 只股票 &middot; Point-in-Time 口径（无未来函数）
  <br><a href="reading_guide.md" style="color:#93c5fd">📖 回测报告阅读说明（术语表 + 图表解读）</a></div>

  <div class="cards">
    <div class="card"><div class="v { 'good' if (strategy.get('excess_total') or 0) > 0 else 'bad' }">{_pct(strategy.get('excess_total'))}</div><div class="l">策略A超额（绝对分数 vs 持有）</div></div>
    <div class="card"><div class="v { 'good' if ((strategy_pct or {}).get('excess_total') or 0) > 0 else 'bad' }">{_pct((strategy_pct or {}).get('excess_total'))}</div><div class="l">策略B超额（个股百分位 vs 持有）</div></div>
    <div class="card"><div class="v">{_pct(ic['fut_60']['pooled'])}</div><div class="l">池化 IC（未来60日）</div></div>
    <div class="card"><div class="v">{_pct(ic['fut_250']['pooled'])}</div><div class="l">池化 IC（未来250日）</div></div>
    <div class="card"><div class="v">{_pct(ic['fut_60']['positive_ratio'])}</div><div class="l">按股 IC&gt;0 占比（60日）</div></div>
    <div class="card"><div class="v">{strategy.get('strategy', {}).get('days', '-')}</div><div class="l">回测交易日</div></div>
  </div>

  <div class="section">
    <h2>1. 组合策略 vs 基准净值</h2>
    <div class="desc">
      策略A（绝对分数）：分数 &gt;= {params['score_buy']} 持仓 / &lt; {params['score_sell']} 空仓；
      策略B（个股历史百分位）：当日分数 &gt;= 自身历史 p80 持仓 / &lt;= p20 空仓（阈值按年度切片 PIT 重算，样本不足 50 不产生信号）；
      两策略均：中间区保持前态、T 日信号决定 T+1 日持仓（滞后1日，无交易成本）。基准 = 全部股票等权买入持有。
      <br>⚠️ 当前回测区间 {strategy['start']} ~ {strategy['end']} 由全部股票首个分数的最晚日期决定（2023 年前后上市的新股会拉高起点）；如需更换时间段，见下方第 6 节。</div>
    <div class="chart"><div id="chartNav" style="height:380px"></div></div>
    <table>
      <tr><th>组合</th><th>累计收益</th><th>年化</th><th>最大回撤</th><th>日胜率</th><th>换手率</th></tr>
      {strat_html}
    </table>
    <div class="note">解读：先看基准（虚线）——若区间整体上行（牛市），所有策略都涨；重点比较策略是否在回撤期降低了损失、在上涨期跟上了涨幅（超额收益 &gt; 0 才有意义）。</div>
  </div>

  <div class="section">
    <h2>2. 分层回测：分数 vs 未来收益（有效性核心证据）</h2>
    <div class="desc">把全部"股票×日"样本按分数分组，比较各组未来 60/250 日的平均收益。若分数有效，收益应随分数升高单调递增。周频采样（每 {params['weekly_sample']} 交易日取1样本）避免重叠样本扭曲。</div>
    <div class="chart">
      <h3>未来 60 日平均收益</h3>
      <div id="chartLayer60" style="height:260px"></div>
      <table>
        <tr><th>分数桶</th><th>样本数</th><th>平均收益(60日)</th><th>胜率</th></tr>
        {layer_rows}
      </table>
    </div>
    <div class="chart">
      <h3>未来 250 日平均收益</h3>
      <div id="chartLayer250" style="height:260px"></div>
      <table>
        <tr><th>分数桶</th><th>样本数</th><th>平均收益(250日)</th><th>胜率</th></tr>
        {layer_rows_250}
      </table>
    </div>
    <div class="chart">
      <h3>五等分桶（稳健性检查）</h3>
      <div id="chartQuintile" style="height:260px"></div>
      <div class="note">按分数区间分 5 档（0-20/20-40/40-60/60-80/80-100）检验单调性；若高分组收益明显更高，说明分数对收益有区分度。</div>
    </div>
  </div>

  <div class="section">
    <h2>3. IC 检验</h2>
    <div class="chart">
      <table>
        <tr><th>指标</th><th>未来 60 日</th><th>未来 250 日</th></tr>
        <tr><td>池化 IC（全部样本）</td><td>{_fmt(ic['fut_60']['pooled'])}</td><td>{_fmt(ic['fut_250']['pooled'])}</td></tr>
        <tr><td>按股 IC 均值</td><td>{_fmt(ic['fut_60']['by_stock_mean'])}</td><td>{_fmt(ic['fut_250']['by_stock_mean'])}</td></tr>
        <tr><td>按股 IC 中位数</td><td>{_fmt(ic['fut_60']['by_stock_median'])}</td><td>{_fmt(ic['fut_250']['by_stock_median'])}</td></tr>
        <tr><td>按股 IC&gt;0 占比</td><td>{_pct(ic['fut_60']['positive_ratio'])}</td><td>{_pct(ic['fut_250']['positive_ratio'])}</td></tr>
        <tr><td>有效股票数</td><td>{ic['fut_60']['stock_count']}</td><td>{ic['fut_250']['stock_count']}</td></tr>
        <tr><td>池化样本数</td><td>{ic['fut_60']['pooled_n']}</td><td>{ic['fut_250']['pooled_n']}</td></tr>
      </table>
    </div>
    <div class="chart">
      <h3>分数 vs 未来 250 日收益（周频采样散点）</h3>
      <div id="chartScatter" style="height:380px"></div>
    </div>
  </div>

  <div class="section">
    <h2>4. 个股明细</h2>
    <div class="chart">
      <div class="scroll">
      <table>
        <tr><th>名称</th><th>代码</th><th>模型</th><th>样本数</th><th>首个分数</th><th>最后分数</th></tr>
        {stock_rows}
      </table>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>5. 口径与已知限制</h2>
    <div class="desc">
      <span class="tag">区间: {params['range']}</span>
      <span class="tag">PE价格: {params['pe_price']}</span>
      <span class="tag">披露假设: {params['disclosure_assumption']}</span>
      <span class="tag">{params['dps_assumption']}</span>
      <span class="tag">信号: {params['signal_lag']}</span>
      <span class="tag">交易成本: {'无' if params['no_trade_cost'] else '有'}</span>
      <span class="tag">策略B: {params.get('pct_strategy', '')}</span>
      <br><br>
      收益基于前复权收盘价（含分红除权效应，不另计现金分红）；历史 PE/PB 用不复权真实交易价 / 已生效 EPS(BPS)；
      缺历史财务数据的交易日 PE/PB 因子计中性分（禁止当前值反推）；区间样本不足 50 个的年份不输出分数（warmup 期）。
      分数越高代表相对自身历史越低估；跨股票分数不可比（百分位口径）。
    </div>
  </div>

  <div class="section">
    <h2>6. 更换回测时间段 / 重新回测</h2>
    <div class="desc">
      页面上的净值区间由"全部股票首个分数的最晚日期"决定（新股会拉高起点，如 2023 年上市股票导致起点在 2023-05）。
      如需更换时间段（例如避开 2023-2026 单边行情），通过本地服务提交后自动重跑：
      <code>python scripts/backtest_web.py</code>（浏览器打开 <code>http://127.0.0.1:8643</code>），
      在"重新回测"表单填写起始/结束日期（如 2018-01-01 ~ 2022-12-31）提交，完成后自动跳转到新报告。
      也可直接命令行：<code>python scripts/run_backtest.py --start 2018-01-01 --end 2022-12-31</code>。
    </div>
    <div class="chart">
      <form method="POST" action="/run" style="display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap">
        <div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">回测起点（空=自动，从全部股票都有分数起）</div>
          <input type="date" name="start" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:6px 10px">
        </div>
        <div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">回测终点（空=最新数据日）</div>
          <input type="date" name="end" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:6px 10px">
        </div>
        <div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">股票子集（空=全量 watchlist，逗号分隔代码）</div>
          <input type="text" name="stocks" placeholder="如 600887,601899" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:6px 10px;width:220px">
        </div>
        <button type="submit" style="background:#3b82f6;color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer">重新回测（约1-2分钟）</button>
      </form>
      <div class="note">说明：本表单需通过 <code>python scripts/backtest_web.py</code> 启动的本地服务访问才可提交；直接双击打开 HTML 文件时请使用命令行方式。</div>
    </div>
  </div>
</div>

<script>
var curve = {_js((strategy or {}).get('curve') or [])};
var curvePct = {_js((strategy_pct or {}).get('curve') or [])};
var layers60 = {_js(layers['fixed'][:3])};
var layers250 = {_js(layers['fixed'][3:])};
var quint = {_js(layers['quintile'][:5] if layers.get('quintile') else [])};
var scatterData = {_js(scatter)};

echarts.init(document.getElementById('chartNav')).setOption({{ 
  backgroundColor: 'transparent',
  tooltip: {{trigger: 'axis'}},
  legend: {{data: ['策略A净值', '策略B净值', '基准净值'], textStyle: {{color: '#94a3b8'}}}},
  grid: {{left: 60, right: 20, top: 40, bottom: 30}},
  xAxis: {{type: 'category', data: curve.map(function(c){{return c.date;}}), axisLabel: {{color: '#94a3b8'}}}},
  yAxis: {{type: 'value', scale: true, axisLabel: {{color: '#94a3b8'}}}},
  series: [
    {{name: '策略A净值', type: 'line', showSymbol: false, data: curve.map(function(c){{return c.strategy;}}), lineStyle: {{width: 2}}, itemStyle: {{color: '#34d399'}}}},
    {{name: '策略B净值', type: 'line', showSymbol: false, data: curvePct.map(function(c){{return c.strategy;}}), lineStyle: {{width: 2, type: 'dotted'}}, itemStyle: {{color: '#fbbf24'}}}},
    {{name: '基准净值', type: 'line', showSymbol: false, data: curve.map(function(c){{return c.bench;}}), lineStyle: {{width: 2, type: 'dashed'}}, itemStyle: {{color: '#93c5fd'}}}}
  ]
}});

function layerOption(rows) {{
  var names = rows.map(function(r){{return r.bucket;}});
  var vals = rows.map(function(r){{return (r.mean_ret === null ? 0 : r.mean_ret * 100);}});
  return {{
    backgroundColor: 'transparent',
    tooltip: {{}},
    grid: {{left: 60, right: 20, top: 20, bottom: 30}},
    xAxis: {{type: 'category', data: names, axisLabel: {{color: '#94a3b8'}}}},
    yAxis: {{type: 'value', name: '平均收益%', axisLabel: {{color: '#94a3b8'}}}},
    series: [{{type: 'bar', data: vals, itemStyle: {{color: '#3b82f6'}}, label: {{show: true, position: 'top', formatter: function(p){{return p.value.toFixed(2) + '%';}}}}}}]
  }};
}}
echarts.init(document.getElementById('chartLayer60')).setOption(layerOption(layers60));
echarts.init(document.getElementById('chartLayer250')).setOption(layerOption(layers250));

var qNames = quint.map(function(r){{return r.bucket;}});
var qVals60 = quint.map(function(r){{return r.mean_ret === null ? 0 : r.mean_ret * 100;}});
echarts.init(document.getElementById('chartQuintile')).setOption({{
  backgroundColor: 'transparent',
  tooltip: {{}},
  grid: {{left: 60, right: 20, top: 20, bottom: 30}},
  xAxis: {{type: 'category', data: qNames, axisLabel: {{color: '#94a3b8'}}}},
  yAxis: {{type: 'value', name: '平均收益%', axisLabel: {{color: '#94a3b8'}}}},
  series: [{{type: 'bar', data: qVals60, itemStyle: {{color: '#8b5cf6'}}, label: {{show: true, position: 'top', formatter: function(p){{return p.value.toFixed(2) + '%';}}}}}}]
}});

echarts.init(document.getElementById('chartScatter')).setOption({{
  backgroundColor: 'transparent',
  tooltip: {{formatter: function(p){{return '分数 ' + p.value[0] + '<br/>250日收益 ' + p.value[1] + '%';}}}},
  grid: {{left: 60, right: 20, top: 30, bottom: 40}},
  xAxis: {{type: 'value', name: '分数', min: 0, max: 100, axisLabel: {{color: '#94a3b8'}}}},
  yAxis: {{type: 'value', name: '未来250日收益%', axisLabel: {{color: '#94a3b8'}}}},
  series: [{{type: 'scatter', symbolSize: 4, data: scatterData, itemStyle: {{color: 'rgba(59,130,246,0.55)'}}}}]
}});
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(html_path), exist_ok=True)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
