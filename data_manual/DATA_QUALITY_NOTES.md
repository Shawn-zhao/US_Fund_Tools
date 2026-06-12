# 数据质量检查记录

生成时间: 2026-06-12

## 交叉校验结果

| 指标 | 校验方式 | 结果 |
| --- | --- | --- |
| Total Put/Call | `data_manual/total_put_call.csv` vs CBOE 官方 `totalpc.csv`，重叠区间 2006-11-01 至 2019-10-04 | 3253 行完全一致，最大差异 0 |
| HY OAS | Macrotrends 历史 JSON vs FRED `BAMLH0A0HYM2`，重叠区间 2023-06-12 至 2026-06-09 | 786 行完全一致，最大差异 0 |
| `$S5TH` 广度 | Barchart 抓取序列自检 | 4915 行，2007-01-02 至 2026-06-12，无重复，值域 1.62 至 96.82，无 0-100 越界，无单日绝对跳变 >35pct |

## 可靠性判断

- 价格、VIX、VIX3M、W5000、CAPE、FRED/Fed 宏观与利率数据: 属于常用公开源，适合研究回测。
- Total Put/Call: 2006-2019 与 CBOE 官方归档完全一致；2019 后来自 YCharts 页面，页面注明来源为 CBOE Daily Market Statistics。适合研究回测。
- HY OAS: Macrotrends 与 FRED 当前可见窗口完全一致；FRED 近年仅保留滚动窗口，因此用 Macrotrends/GitHub/Eco3min 补长历史。适合研究回测，但不是交易所原始授权数据。
- `$S5TH` 广度: 来自 Barchart 历史接口，值域和连续性正常；这是当前体系中相对最需要保留来源说明的一项，因为没有另一个已接入源做完整交叉校验。

## 风险备注

- CBOE 官方对 Put/Call 历史数据有免责声明: 信息来自其认为可靠的来源，但不保证准确性。
- YCharts、Macrotrends、Barchart 都是二级/聚合数据源，适合策略研究，不等同于付费授权原始行情数据库。
- 当前结论不依赖单日小误差；热度策略使用分位数、均线、月度定投模拟，小范围数据噪声通常不会改变总体判断。
