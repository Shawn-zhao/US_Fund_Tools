# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

美股「定投热度监测系统」——一个量化研究项目,不是传统应用。核心产物是:① 一套带版本演进的**设计方案文档**(Markdown,中文);② 验证这些方案的**回测脚本**(Python);③ 一个把信号每天算出来给人看的**网页仪表盘**(`daily_status.py` → `index.html`)。文档是方法论的"真理来源",Python 脚本是它的实现与验证。

## 常用命令

```bash
pip install -r requirements.txt          # 依赖只有 pandas / numpy / scipy

python3 backtest_v26_riskadj_Opus4_8.py  # 跑某个回测,结果直接 print 成表格
python3 backtest_v27_leverage_Opus4_8.py # 冷档杠杆方案回测
python3 sweep_v27.py / validate_v27.py   # 参数扫描 / 样本外稳健性验证
python3 daily_status.py                  # 抓数据→算信号→生成 status.json + index.html
python3 fetch_data.py                    # 仅更新月频宏观缓存(FRED/CAPE)
```

**没有 pytest 之类的测试框架**——回测脚本本身就是"测试":跑它、读它打印的对比表(策略 vs 普通定投的 IRR/回撤/Calmar),或读对应的 `回测复核报告_*.md`。`build_core()` 约 5 秒;脚本无命令行参数,改参数直接编辑脚本顶部的常量字典(如 `SCHEDULES` / `MULTS` / `PCT_CUTS`)。

## 架构大图(需要跨多个文件才能理解的部分)

**数据层 = `backtest_v25_independent_Opus4_8.py`。** 它持有所有数据加载函数(`load_yahoo / load_fred / load_cape / load_breadth / load_put_call / load_hy_oas / load_h15_series`)和分位原语(`expanding_pct / rolling_pct / hysteresis / to_month_last`)。**所有数据一律从 `backtest_outputs/cache/` 与 `data_manual/` 读缓存,从不重抓长历史**(见 `data_manual/README.md`)。其它脚本都 `import` 它复用这一层。

**信号引擎 = `backtest_v26_design_Opus4_8.py::build_core()`。** 这是整个项目的中枢:把估值(CAPE/ECY/巴菲特)、情绪(VIX/期限结构/PutCall)、趋势(200日均线偏离/回撤/周RSI)、广度信用(广度/HY利差)合成日频 `base`,再取扩展分位得 `heat_pct`。`band_pct()` + `hysteresis()` 把分位切成档位(blue/green/neutral/yellow/orange/red)。几乎所有回测和 `daily_status.py` 都基于它。

**C1 是 v2.6 的关键创新**:`CAPE_当日 = CAPE_上月 × (今日价 / 上月末价)`,让月度估值在崩盘当天就变便宜(修好 v2.5 把 2020.03 误判为中性的盲区)。改估值相关逻辑前务必理解这点。

**两条并行的模型谱系,互为交叉验证,不要混淆:**
- **Opus 4.8 谱系**:文件名带 `_Opus4_8` / `_Opus4.8`。彼此复用上面的数据层与 `build_core`。
- **ChatGPT / Codex 谱系**:`backtest_heat_v25.py`、`backtest_heat_v26.py`、`*_Codex-GPT5.md`、`backtest_outputs/backtest_report.md`。独立实现。
两者结论方向一致但数字口径不同,**不能逐位比较**。

**生产链路**:`daily_status.py`(抓最新行情→`build_core`→回放"合并版"状态机→输出 `status.json` + `index.html`)+ `fetch_data.py`(月频宏观抓取)+ `.github/workflows/daily.yml`(GitHub Actions 每工作日跑一次并提交回仓库;GitHub Pages 从 main 根目录部署)。

## 版本与文档脉络

- `美股定投热度监测方案_v2.5_冻结版.md` — 指标体系基线(四维度权重、ECY、修正项、迟滞、卖出双确认),被各版保留。
- `v2.6_Opus4.8` — 在 v2.5 上做 C1(价格调整估值)、C2(自校准分位档)、C3(现金缓冲)、D(H.15 数据修正)。
- `v2.6_实盘规则版_Codex-GPT5` — 另一条思路:废弃供款择时,改"固定定投 + 罕见时一次性 20% 避险状态机"。
- `v2.6_合并版_Opus4.8` — 执行用 Codex 状态机、信号用 Opus C1;`daily_status.py` 实现的就是这一版。
- `v2.7_杠杆` — 冷档杠杆实验(`backtest_v27_leverage` / `sweep` / `validate`)。
- 改方案时**先读对应的 `回测复核报告_*.md`**,再动脚本。

## 关键约束与坑(改动前必读)

- **核心结论(别重复推导):** 穷尽回测证明,在 2007–2026 长牛里,**任何不加杠杆的择时都无法在"每块钱年化"上跑赢普通定投**(结构性原因:长牛 + 早期深坑本金小 + 终点在高位)。唯一做出 +2pp 的是真实杠杆,代价是回撤放大与追缴风险。系统真实价值在**风险调整 + 纪律 + 冷信号**,不在超额收益。
- **反过拟合是硬约束:** 不要为了让某个历史点(2020/2021 等)落进预设档位去微调分位线或阈值;参数取圆整数、随热度单调、跨年代语义稳定。这是各版方案反复强调并明令的。
- **防前视:** 分位用截至当日的 mid-rank;月频指标滞后一月可用(`available_next_month`)。
- **H.15 利率坑:** 节假日用 `-9999` 等哨兵值标记,加载时已过滤 `|值|≥1000`,勿当真实利率读入。
- **`daily_status.py` 对 SPX/NDX 用同一个综合热度**(只有价格/200均线/回撤是各资产独立的);若要做成完全独立的双指数热度,需另行扩展 `build_core`。
- **自动抓取能力有限:** `fetch_data.py` 只能可靠自动更新 FRED(CPI/GDP/HY)与 CAPE(multpl,HTML 抓取较脆)。**广度(barchart)、Put/Call(CBOE)、H.15 利率(`h15_all.zip`)无稳定免费源,需手动更新**;它们权重低、变化慢,影响有限。
- **全中文输出,统一 UTF-8**;脚本里都有 `sys.stdout.reconfigure(encoding="utf-8")`。
- 免责:所有方案与报告均为量化方法的教育性讨论,不构成投资建议(文档内均有声明)。
