# 估值系统思考流程（Thinking Flow）

当用户请求"分析 XXX 股票"时，按以下步骤执行：

## 第一阶段：确认与准备（并行）

### 1.1 确认股票代码
- 搜索确认股票全名、交易所、正确代码
- 常见陷阱：用户可能提供错误代码（如香农芯创 301291 vs 正确的 300475）
- 交易所规则：上海 sh（60xxxx, 68xxxx），深圳 sz（00xxxx, 30xxxx）

### 1.2 获取K线数据（2年/2批次 或 3年/3批次）
```bash
# 2年回测（2批次）
curl -sL -H "User-Agent: Mozilla/5.0" \
  "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600938,day,2024-07-09,2025-07-09,500,qfq" \
  -o kline_2024_2025.json
curl -sL -H "User-Agent: Mozilla/5.0" \
  "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600938,day,2025-07-09,2027-07-09,500,qfq" \
  -o kline_2025_2027.json
```

### 1.3 搜索基本面数据（并行）
搜索以下信息：
- 2025年营收、归母净利润、毛利率
- 总股本（亿股）
- 行业分类与竞争格局
- 关键业务结构

## 第二阶段：设计估值模型

### 2.1 确定模型类型

根据行业特征与公司基本面，从以下8种模型中选择最合适的一种：

| 模型名称 | 适用行业/场景 | 核心逻辑 |
|----------|--------------|----------|
| **growth**（成长股） | 消费电子、半导体、SaaS、新能源车 | 业绩稳定增长，现金流好，以PE+PEG为核心，高增速支撑高估值 |
| **cyclical**（周期股） | 煤炭、钢铁、化工、有色金属、航运 | 业绩随商品价格/周期大幅波动，以PB+ROE为核心，低PB买入、高PB卖出 |
| **value**（深度价值） | 银行、地产、传统制造、高速公路 | 股价长期低于净资产或内在价值，以PB+股息率+DCF为核心，关注安全边际 |
| **momentum**（动量/趋势） | 短期热门板块、题材股、技术突破股 | 以价格动量、均线偏离、量价关系为核心，适合趋势明确的行情阶段 |
| **dividend**（高股息/红利） | 公用事业、大型银行、煤炭龙头、水电 | 以股息率+PB+ROE为核心，追求稳定现金流回报，适合防御型配置 |
| ** turnaround**（困境反转） | 资产重组、业绩预增、行业出清后的剩者 | 以PB+业绩弹性+预期PE为核心，关注基本面边际改善信号 |
| **tech**（科技/研发驱动） | 创新药、AI、量子计算、先进制程 | 以PS+研发投入占比+营收增速为核心，亏损期看PS，盈利期切换PE |
| **financial**（金融 specialty） | 券商、保险、信托、租赁 | 以PB+ROE+EV/EBITDA为核心，关注资产质量、杠杆率、息差/利差变化 |

**模型选择决策流程：**

```
公司是否盈利且连续3年以上？
├─ 是 → 行业是否有明显周期性？
│       ├─ 是 → 周期幅度大？→ cyclical（周期股）
│       │         └─ 周期幅度小 → dividend（高股息）或 value（深度价值）
│       └─ 否 → 增速是否 > 15%？
│               ├─ 是 → 属于科技/研发密集型？→ tech（科技驱动）
│               │         └─ 否 → growth（成长股）
│               └─ 否 → 估值是否长期低于净资产？
│                       ├─ 是 → value（深度价值）
│                       └─ 否 → dividend（高股息）
└─ 否（亏损或刚扭亏）→ 是否处于困境反转阶段？
        ├─ 是 → turnaround（困境反转）
        └─ 否（科技亏损期）→ tech（科技/研发驱动）

金融行业（银行/券商/保险）→ 无论其他条件，优先使用 financial
短期趋势交易/技术分析为主 → momentum（动量/趋势）
```

### 2.2 设定参数
- **PE历史区间**：参考该股过去2-3年PE(TTM)的波动范围
- **PB历史区间**：参考该股过去2-3年PB的波动范围
- **预期增速**：根据行业增速、公司地位合理估算
- **PS历史区间**（tech模型适用）：参考过去2-3年市销率波动范围
- **股息率区间**（dividend模型适用）：参考过去分红记录与股息率水平
- **ROE区间**（financial/cyclical模型适用）：参考行业平均ROE水平

### 2.3 参数设计原则
- PE区间应覆盖95%以上的历史数据
- 增长型行业PE区间偏高（20-80），周期型偏低（3-25）
- PB区间一般 0.3-6.0，金融/资源类偏低
- 增速假设不宜过高，10-20%为合理区间
- tech模型的PS区间参考同行业可比公司，亏损期PS 2-15倍为常见范围
- dividend模型的股息率 > 4% 视为高股息，> 6% 视为极高（需警惕可持续性）

### 2.4 输入可选因子

根据所选模型，可额外传入可选因子来增强估值评分的针对性。可选因子通过命令行参数 `--factors` 传入（详见注意事项第7条）。

| 模型 | 默认因子 | 可选因子 |
|------|---------|---------|
| growth | PE, PB, PEG, MA偏离, 量能, 波动率 | +研发投入占比, +营收增速, +毛利率趋势 |
| cyclical | PE, PB, PEG, MA偏离, 量能, 波动率 | +商品价格指数, +产能利用率, +库存周期 |
| value | PE, PB, PEG, MA偏离, 量能, 波动率 | +股息率, +净资产折溢价, +EV/EBITDA |
| momentum | PE, PB, PEG, MA偏离, 量能, 波动率 | +RSI, +MACD信号, +资金流向 |
| dividend | PE, PB, PEG, MA偏离, 量能, 波动率 | +股息率, +分红连续年数, + payout比率 |
| turnaround | PE, PB, PEG, MA偏离, 量能, 波动率 | +业绩同比变化, +资产负债率, +管理层持股变化 |
| tech | PS, MA偏离, 量能, 波动率, +营收增速, +研发占比 | +用户/订单增速, +毛利率, +现金消耗率 |
| financial | PB, ROE, MA偏离, 量能, 波动率 | +不良贷款率, +净息差, +资本充足率 |

**可选因子说明：**
- 默认因子已内置在评分引擎中，无需手动传入
- 可选因子需要用户通过 `--factors` 参数显式指定
- 未提供的可选因子将使用模型内置的默认值（通常为中性值）
- 最多可同时启用5个额外可选因子

## 第三阶段：构建报告

### 3.1 合并K线数据
- 多批次数据按日期去重合并
- 确保数据连续性（约480-490个交易日）

### 3.2 计算估值评分
- 对每个交易日计算PE/PB/PEG/MA偏离/量能/波动率6个因子
- 加权汇总得到综合估值分（0-100）
- 计算历史百分位（20th/80th）

### 3.3 生成HTML
- 内联ECharts库
- 包含全屏覆盖层、十字轴光标、百分位线
- 输出自包含单文件

## 第四阶段：验证

### 4.1 语法验证
```bash
node --check  # 验证JS语法
```

### 4.2 数据验证
- 检查交易日数量（应约480-490天）
- 检查最新估值分是否合理
- 检查PE/PB是否与实际一致

## 注意事项

1. **腾讯API必须带User-Agent**，否则返回302
2. **K线数据最多500条/次**，2年需2次请求
3. **qt字段索引**：3=价格, 39=PE(TTM), 44=市值(万元), 46=PB
4. **总股本字段47可能返回0**，需要从网上搜索确认
5. **科创板代码**：sh688xxx，属于上海交易所
6. **创业板代码**：sz300xxx，属于深圳交易所
7. **可选因子命令行参数格式**：使用 `--factors` 参数传入可选因子，格式为 `key:value` 对，多个因子用逗号分隔。数值型因子直接传数值，枚举型因子传预定义值。
   ```bash
   # 格式
   --factors "因子名1:值1,因子名2:值2,因子名3:值3"

   # 示例：growth模型附加研发投入占比和营收增速
   --factors "rd_ratio:15.2,revenue_growth:28.5,gross_margin_trend:up"

   # 示例：cyclical模型附加商品价格与产能利用率
   --factors "commodity_index:112.5,capacity_util:0.85,inventory_cycle:replenishment"

   # 示例：dividend模型附加股息相关信息
   --factors "dividend_yield:5.8,consecutive_years:8,payout_ratio:0.6"

   # 示例：tech模型附加用户增速与毛利率
   --factors "user_growth:35.2,gross_margin:62.5,burn_rate:low"
   ```
   **枚举值参考**：
   - `gross_margin_trend`: `up` / `flat` / `down`
   - `inventory_cycle`: `accumulation` / `replenishment` / `depletion`
   - `burn_rate`: `low` / `medium` / `high`
   - `MACD_signal`: `golden_cross` / `dead_cross` / `neutral`
   - `fund_flow`: `net_inflow` / `net_outflow` / `balanced`

## 使用示例

### 示例1：成长股分析（growth模型）
```bash
# 假设分析宁德时代 sz300750
node run.js --code sz300750 --model growth --pe-range 20-80 --pb-range 2-8 \
  --growth-rate 25 --factors "rd_ratio:8.5,revenue_growth:22.3,gross_margin_trend:flat"
```

### 示例2：周期股分析（cyclical模型）
```bash
# 假设分析中国神华 sh601088
node run.js --code sh601088 --model cyclical --pe-range 5-18 --pb-range 0.8-2.5 \
  --growth-rate 5 --factors "commodity_index:98.2,capacity_util:0.88,inventory_cycle:depletion"
```

### 示例3：深度价值股分析（value模型）
```bash
# 假设分析某银行股 sh601398
node run.js --code sh601398 --model value --pe-range 4-8 --pb-range 0.4-0.9 \
  --growth-rate 3 --factors "dividend_yield:6.2,nav_discount:0.35,ev_ebitda:5.1"
```

### 示例4：高股息红利股分析（dividend模型）
```bash
# 假设分析长江电力 sh600900
node run.js --code sh600900 --model dividend --pe-range 15-25 --pb-range 2-5 \
  --growth-rate 8 --factors "dividend_yield:4.2,consecutive_years:15,payout_ratio:0.55"
```

### 示例5：科技研发驱动分析（tech模型）
```bash
# 假设分析某AI公司 sz300XXX（亏损期）
node run.js --code sz300XXX --model tech --ps-range 3-12 --growth-rate 45 \
  --factors "rd_ratio:32.1,revenue_growth:68.5,user_growth:52.3,gross_margin:71.2,burn_rate:medium"
```

### 示例6：困境反转分析（turnaround模型）
```bash
# 假设分析某重组股 sh600XXX
node run.js --code sh600XXX --model turnaround --pe-range 10-30 --pb-range 1-3 \
  --growth-rate 35 --factors "yoy_change:152.8,debt_ratio:0.55,mgmt_share_change:up"
```

### 示例7：金融 specialty 分析（financial模型）
```bash
# 假设分析某券商 sh601XXX
node run.js --code sh601XXX --model financial --pb-range 1-2.5 --roe-range 8-15 \
  --factors "npl_ratio:0.85,net_interest_margin:2.1,capital_adequacy:13.5"
```

### 示例8：动量趋势分析（momentum模型）
```bash
# 假设分析某热门题材股 sz000XXX
node run.js --code sz000XXX --model momentum --pe-range 15-40 --pb-range 1.5-4 \
  --factors "rsi:72,macd_signal:golden_cross,fund_flow:net_inflow"
```
