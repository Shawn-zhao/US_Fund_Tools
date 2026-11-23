# 手工/登录态数据保留清单

生成时间: 2026-06-12

这些文件用于后续修改方案后的重复回测。不要删除 `data_manual/` 和 `backtest_outputs/cache/` 中列出的数据文件；脚本会优先读取本地缓存与手工数据，避免再次登录网页或重新抓长历史。

## 当前主回测覆盖结论

- 2007-01-02 至 2026-06-11/12: 当前 v2.5 回测所需核心数据已覆盖。
- 2000/2002 场景: 仍不是完整严格覆盖，因为 VIX3M 起于 2006-07-17，Barchart `$S5TH` 广度起于 2007-01-02。
- 非交易日没有日频记录属于正常情况，不视为缺失。
- 2026-06-13 复查 2000 互联网泡沫数据缺口: 详见 `data_manual/DOTCOM_DATA_GAPS.md`。已补充 World Bank 美国上市公司市值/GDP 年频辅助数据至 `backtest_outputs/cache/worldbank_usa_market_cap_gdp.csv`，但它不能直接替代严格日/月频回测口径。

## 关键手工数据

| 文件 | 内容 | 覆盖 | 行数 | SHA256 |
| --- | --- | --- | --- | --- |
| `data_manual/total_put_call.csv` | YCharts Total Put/Call，已转成 ISO 日期 CSV | 2006-11-01 至 2026-06-11 | 4930 | `D8FE2FB855B577BC7BF682DF52E05152673F874A7E8356C14D69E5BD9AD0D35F` |
| `data_manual/ycharts_total_put_call.json` | YCharts 99 页原始抓取结果 | 2006-11-01 至 2026-06-11 | 4930 | `93AD5C050BD469668BBDE80839BEBB4437C432FC970FD01FA38202FDEDAD5C97` |

## 关键缓存数据

| 文件 | 内容 | 覆盖 | 行数/说明 | SHA256 |
| --- | --- | --- | --- | --- |
| `backtest_outputs/cache/barchart_s5th_breadth.csv` | Barchart `$S5TH`，SPX 成分股高于 200DMA 比例 | 2007-01-02 至 2026-06-12 | 4915 | `A4ABD0DC3361E96842E77739356FB367B5D11E6C0F93B2B24267D9982E667EE0` |
| `backtest_outputs/cache/macrotrends_high_yield_spread_D.json` | HY OAS 全历史镜像 | 1996-12-31 至 2026-06-10 | 7689 | `931E05590CB89908647334814A33B460A8362364F2E9CF3EC8D5CC1B6637A2D4` |
| `backtest_outputs/cache/h15_all.zip` | Federal Reserve H.15 利率原始 ZIP | 至 2026-06-10 | 名义 10Y 与 TIPS 10Y | `79326129D8F757F99C94E4CA94B1C27B2634A0A444437F3D5726A3AD5D7B93F3` |

完整覆盖表见 `backtest_outputs/data_coverage.csv`。

## 每日状态页的后续数据

- `daily_status.py` 在更新行情后补抓两项日频数据。Put/Call 和换源前暂缺的广度保存在 `backtest_outputs/cache/live/`，GitHub Actions 在每次运行间恢复和保存该目录。公开仪表盘显示数据来源和日期。
- Put/Call 使用 [Cboe 每日统计](https://www.cboe.com/markets/us/options/market-statistics/daily) 的 `TOTAL PUT/CALL RATIO`，按交易日查询并校验返回日期。请求失败的日期留待下次补抓。
- 广度按 [Market Breadth Analysis](https://github.com/tradermonty/market-breadth-analysis) 发布的 `Breadth_Index_Raw` 补充。这是标普 500 成分股站上各自 200 日均线的比例，程序要求有至少 95% 成分股参与。与 Barchart 原始历史在 2,399 个共同交易日的相关系数为 0.986，平均绝对差为 5.78 个百分点。成分股范围和价格处理存在差异，2026 年 6 月 12 日之后的接续值是另一供应方的测量值。
- 以 **2026-11-20** 为交接日。Barchart 补充历史放在私有数据仓库的 `barchart_s5th_breadth.csv`（`date,breadth`），优先用于计算历史；补齐前仪表盘继续使用已有补缺缓存并显示 Barchart 的实际覆盖日期。
- 交接日之后，Actions 把市场广度的交易日期、数值、来源和 UTC 实际抓取时间写入同一私有仓库的 `breadth_daily.csv`。历史记录保留第一次收到的值，补抓日期记录实际补抓时间。离线复核可将环境变量 `MARKET_BREADTH_ARCHIVE_DIR` 指向私有仓库的本地副本。
