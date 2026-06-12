# 手工/登录态数据保留清单

生成时间: 2026-06-12

这些文件用于后续修改方案后的重复回测。不要删除 `data_manual/` 和 `backtest_outputs/cache/` 中列出的数据文件；脚本会优先读取本地缓存与手工数据，避免再次登录网页或重新抓长历史。

## 当前主回测覆盖结论

- 2007-01-02 至 2026-06-11/12: 当前 v2.5 回测所需核心数据已覆盖。
- 2000/2002 场景: 仍不是完整严格覆盖，因为 VIX3M 起于 2006-07-17，Barchart `$S5TH` 广度起于 2007-01-02。
- 非交易日没有日频记录属于正常情况，不视为缺失。

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
