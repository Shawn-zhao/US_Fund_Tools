# 2000 互联网泡沫回测数据缺口记录

生成时间: 2026-06-13

## 结论

当前严格主回测没有纳入 2000 互联网泡沫。`backtest_heat_v26.py` 从 2007-01-01 开始运行, 不是因为指数价格缺失, 而是因为 v2.5/v2.6 的完整指标集在 2000 年不齐。

2000-03 现有数据诊断:

| 指标 | 2000-03 是否可用 | 说明 |
| --- | --- | --- |
| SPX/NDX 价格 | 可用 | Yahoo 缓存已覆盖 |
| CAPE | 可用 | Shiller 月度数据覆盖 |
| ECY | 可用 | CAPE + H.15 名义利率 + CPI 重构实际利率 |
| VIX | 可用 | 1990 起 |
| Put/Call | 可用 | CBOE 拼接数据 1995-09-27 起 |
| HY OAS | 可用 | 1996-12-31 起 |
| Buffett 分位 | 不可用 | 本地 `^W5000` 从 1989 起, 原规则要求 15 年滚动分位, 2000 年历史长度不足 |
| VIX3M/VXV | 不可用 | 本地 Yahoo 缓存从 2006-07-17 起; FRED VXVCLS 从 2007-12-04 起 |
| SPX 200DMA 广度 `$S5TH` | 不可用 | 未登录 Barchart API 从 2007-01-02 起 |

因此 2000-03 的 `base/heat_spx/heat_ndx` 严格口径为 NaN。2002 熊市底同样因为 Buffett 分位、VIX3M、广度缺失而无法进入严格主回测。

## 已补到的辅助数据

已缓存:

`backtest_outputs/cache/worldbank_usa_market_cap_gdp.csv`

来源: World Bank `CM.MKT.LCAP.GD.ZS`, 美国上市公司市值/GDP, 1975-2024, 年频、年末值。

用途: 可作为 Buffett 指标的历史校准辅助或代理研究数据。限制是它不是原规则的 Wilshire 5000/GDP 日/月频口径, 且年频数据用于实时信号时必须处理发布日期和可得性, 不能直接无脑前视使用。

## 公开来源搜索结果

| 数据 | 结果 |
| --- | --- |
| VIX3M/VXV | 没找到可覆盖 2000 的公开日频数据。FRED `VXVCLS` 页面标注 2007-12-04 起; CBOE/QuantConnect 公开 CSV 目前从 2009-09-18 起; 本地 Yahoo 缓存从 2006-07-17 起。 |
| `$S5TH` 广度 | Barchart 页面说明会员下载的日频数据可回到 2000-01-01, Barchart for Excel 日频可回到 1980-01-01; 但未登录 API 请求 2006 年及以前为空。MacroMicro 同类页面被 Cloudflare 拦截。 |
| Buffett 历史 | World Bank 年频市值/GDP可公开下载到 1975 起。FRED/Wilshire 更长历史公开 CSV 本次未成功拿到。 |

## 如果手工补数据, 建议文件格式

严格补 2000 需要优先补这几个文件:

| 文件 | 必需列 | 说明 |
| --- | --- | --- |
| `data_manual/s5th_breadth_2000_2006.csv` | `date,breadth` | `$S5TH`, S&P 500 成分股高于 200DMA 的百分比, 日频, 0-100 |
| `data_manual/vix3m_2000_2006.csv` | `date,close` | VIX3M/VXV 3个月隐含波动率, 日频。如果找不到, 只能在代理回测里移除期限结构项或用 VIX 近似, 不能称为严格口径。 |
| `data_manual/wilshire5000_pre1989.csv` | `date,close` | 用于补足 Buffett 15 年滚动分位历史。若找不到, 可用 World Bank 年频市值/GDP做代理, 但需单独标注。 |

## 可选的代理回测口径

若接受“代理回测”而非严格回测, 可以从 1997 或 2000 开始:

- 估值维度用 CAPE + ECY, Buffett 缺失时动态重权或用 World Bank 年频市值/GDP代理。
- 情绪维度用 VIX + Put/Call, 暂不使用 VIX3M 期限结构。
- 广度/信用维度用 HY OAS, 暂不使用 `$S5TH` 广度。

这种回测可以回答“系统大方向会不会把 2000 识别为极热、2002 识别为极冷”, 但不能和 2007 后严格主回测混在同一个通过/失败结论里。
