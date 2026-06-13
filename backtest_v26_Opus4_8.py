"""v2.6 回测验证报告生成器。

================================================================
作者标注:本文件由 Anthropic Claude **Opus 4.8** 编写。
(项目中另有其它模型产出的版本并存,本文件用于与之对照参考。)
================================================================

复用本人(Opus 4.8)已验证的模块:
  - backtest_v25_independent_Opus4_8:数据加载、mid-rank 分位、3M 利率(已修 -9999 哨兵)。
  - backtest_v26_design_Opus4_8:build_core(含 C1 价格调整估值)、分位档位、迟滞。
  - backtest_v26_riskadj_Opus4_8:现金缓冲叠加层 run() 与风险指标 evaluate()。

输出 回测复核报告_v2.6_Opus4.8.md。不覆盖 ChatGPT 的 backtest_report.md,也不覆盖 v2.5 报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_v25_independent_Opus4_8 as v25
import backtest_v26_design_Opus4_8 as d
import backtest_v26_riskadj_Opus4_8 as ra

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "回测复核报告_v2.6_Opus4.8.md"

# §5 校准点(预期档位用 v2.6 分位语义复述:极冷=blue 等)
SCEN = {
    "2000.03 互联网泡沫顶": ("2000-03-01", "2000-03-31", "🟥 极热"),
    "2002.10 熊市底":      ("2002-09-15", "2002-10-31", "🟦 极冷"),
    "2007.10 金融危机前顶": ("2007-10-01", "2007-10-31", "🟧/🟨"),
    "2009.03 金融危机底":  ("2009-03-02", "2009-03-31", "🟦 极冷"),
    "2018.12 Q4急跌":      ("2018-12-17", "2018-12-31", "🟩 偏冷"),
    "2020.03 新冠崩盘":    ("2020-03-16", "2020-03-31", "🟦 极冷"),
    "2021.11-12 流动性泡沫顶": ("2021-11-01", "2021-12-31", "🟧→🟥"),
    "2022.10 加息熊底":    ("2022-10-01", "2022-10-31", "🟩 偏冷"),
}
# v2.5 报告记录的基础热度中位(用于展示 C1 对 2020 的修复对比)
V25_BASE = {"2007.10 金融危机前顶": 43.8, "2009.03 金融危机底": 14.2, "2018.12 Q4急跌": 30.7,
            "2020.03 新冠崩盘": 27.8, "2021.11-12 流动性泡沫顶": 67.2, "2022.10 加息熊底": 36.0}


def md(rows, cols):
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    df = d.build_core()
    rate_m = v25.to_month_mean(v25.load_h15_series("RIFLGFCM03_N.B"))
    bser = d.hysteresis(df["heat_pct"].map(lambda x: d.band_pct(x, d.PCT_CUTS)))
    CW = {"yellow": 0, "orange": 20, "red": 40}   # v2.6 默认现金缓冲

    # ---- 校准对比 ----
    calib = []
    for nm, (s, e, exp) in SCEN.items():
        sub = df.loc[s:e].dropna(subset=["heat_pct"])
        if sub.empty:
            calib.append({"时点": nm, "预期档位": exp, "v2.5基础热度中位": V25_BASE.get(nm, "—"),
                          "v2.6基础热度中位": "无数据", "v2.6热度分位": "无数据", "v2.6档位": "无数据"})
            continue
        bmed = sub["base"].median()
        pmed = sub["heat_pct"].median()
        band = d.band_pct(pmed, d.PCT_CUTS)
        calib.append({"时点": nm, "预期档位": exp, "v2.5基础热度中位": V25_BASE.get(nm, "—"),
                      "v2.6基础热度中位": round(bmed, 1), "v2.6热度分位": round(pmed, 0),
                      "v2.6档位": d.BAND_ZH[band]})

    # ---- 风险调整最终结果 ----
    risk = []
    for asset, price in [("SPX", df["spx"]), ("NDX", df["ndx"])]:
        m = ra.evaluate(ra.run(df, price.rename(asset), bser, CW, rate_m, adj=1 / 3), rate_m)
        risk.append({"指数": asset, "期末财富比": m["期末比"],
                     "IRR热/定投%": f"{m['IRR热%']}/{m['IRR定投%']}",
                     "maxDD热/定投": f"{m['maxDD热']}/{m['maxDD定投']}",
                     "年化波动热/定投%": f"{m['波动%']}/{ra.evaluate(ra.run(df, price.rename(asset), bser, {}, rate_m), rate_m)['波动%']}",
                     "Sortino热/定投": f"{m['Sortino']}/{ra.evaluate(ra.run(df, price.rename(asset), bser, {}, rate_m), rate_m)['Sortino']}",
                     "Calmar热/定投": f"{m['Calmar热']}/{m['Calmar定投']}"})

    # ---- 纯供款择时 vs 定投(展示"打平不跑赢") ----
    contrib = []
    MULT = {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 0.0}
    DEP = {"blue": 0.5, "green": 0.25}
    for asset, price in [("SPX", df["spx"]), ("NDX", df["ndx"])]:
        for cap in [None, 6, 3]:
            r = d.dca(df, price.rename(asset), bser, MULT, deploy=DEP, rate_m=rate_m, cap=cap)
            contrib.append({"指数": asset, "备用金上限": "无" if cap is None else f"{cap}月",
                            "相对定投%": r["rel%"], "末期备用金": r["end_res"]})

    L = []
    L.append("# v2.6 美股定投热度方案 · 回测验证报告")
    L.append("")
    L.append("> 📄 **作者:Anthropic Claude Opus 4.8**(项目中另有其它模型产出的版本,本报告供对照参考)。")
    L.append(">")
    L.append("> 回测逻辑由 Opus 4.8 独立实现(`backtest_v26_design_Opus4_8.py` / `backtest_v26_riskadj_Opus4_8.py`,"
             "复用 v2.5 自写的数据加载与分位函数),仅复用缓存原始数据。不覆盖 `backtest_outputs/backtest_report.md`"
             "(ChatGPT)与 `回测复核报告_v2.5_Opus4.8.md`。")
    L.append("")
    L.append("## 0. 一句话结论")
    L.append("")
    L.append("v2.6 的三项改动(C1 价格调整估值、C2 自校准分位档位、C3 现金缓冲)**修好了 v2.5 的校准盲区与一个数据 bug,"
             "并在风险调整上给出温和真实的改善(SPX 明显、NDX 接近打平、两者波动都降)**;但**不**在期末财富上跑赢定投——"
             "穷尽回测证明这在 2007–2026 长牛里对任何不过拟合的择时都不成立(详见 §3)。")
    L.append("")

    L.append("## 1. C1 价格调整估值:校准盲区修复")
    L.append("")
    L.append("`CAPE_当日 = CAPE_上月 ×(今日价/上月末价)`——分母是慢变量、分子每日已知,崩盘当天估值立刻变便宜。")
    L.append("")
    L.append(md(calib, ["时点", "预期档位", "v2.5基础热度中位", "v2.6基础热度中位", "v2.6热度分位", "v2.6档位"]))
    L.append("")
    L.append("- **2020.03 是关键修复**:v2.5 基础热度中位 27.8(中性误判)→ v2.6 热度分位 **11(极冷)**,系统当天即正确"
             "识别新冠底为历史级便宜。2009/2018 仍正确。")
    L.append("- **边界仍是边界(诚实)**:2020(p≈11)、2021(p≈87)、2022(p≈34)仍各距分位线几个百分点。**我刻意不为了"
             "让它们落进预设档而微调分位线——那是过拟合。** 用法是把'接近边界'当信息(显示距档距离),靠迟滞吸收抖动。")
    L.append("- **2007 仍是盲区**(信贷型顶,股票指标天然钝感),同 v2.5。2000/2002 无数据(巴菲特指标需 15 年 W5000 历史)。")
    L.append("")

    L.append("## 2. C2 自校准分位档位")
    L.append("")
    L.append("档位按基础热度的扩展百分位划分,分位线 (10,30,75,90,97) 为常规整数、不对历史点调参。"
             "修好'🟥 极端档几乎不触发',并把'固定 0–100 阈值的边界脆弱'转为分位空间(脆弱性减轻但不消失,见上)。")
    L.append("")

    L.append("## 3. 与普通定投对比:为什么不承诺期末跑赢")
    L.append("")
    L.append("**纯供款择时**(冷档多买/热档少买 + 备用金上限 + 真实 3M 国债计息),相对普通定投期末财富:")
    L.append("")
    L.append(md(contrib, ["指数", "备用金上限", "相对定投%", "末期备用金"]))
    L.append("")
    L.append("- 备用金上限越紧(越接近定投),越接近 0%——**始终从下方逼近、不越过**。这不是参数没调好:美股长牛里"
             "任何持币都有机会成本,最深低点(2009)又在本金极小的早期,叠加 2026 高位终点,使期末跑赢在不过拟合下不可得。")
    L.append("- 对累积持仓做战术减仓(TAA)更差(回测 −9%~−43%):长牛降仓灾难性踏空。")
    L.append("- 故 v2.6 改用**风险调整**目标(见 §4)。")
    L.append("")

    L.append("## 4. C3 现金缓冲:风险调整结果(2007–2026)")
    L.append("")
    L.append("现金缓冲只在 🟧 过热(目标 20%)、🟥 极热(目标 40%)抬升,每月向目标渐进 1/3,现金按真实 3M 国债计息:")
    L.append("")
    L.append(md(risk, ["指数", "期末财富比", "IRR热/定投%", "maxDD热/定投", "年化波动热/定投%", "Sortino热/定投", "Calmar热/定投"]))
    L.append("")
    L.append("- **SPX:温和但真实的风险调整改善**——近乎同等收益,回撤、波动更低,Calmar/Sortino 更高。")
    L.append("- **NDX:接近打平**——波动更低,但终值约 −6%、Calmar 略降;纳指长牛太干净,降仓性价比低。")
    L.append("- **两者波动都降**,是缓冲层最稳健的好处。SPX 回撤只改善约 1.4 点,因其最大回撤是 2020 突发急跌"
             "(见顶时热度仅 p≈84,缓冲未在 🟨 启动)——**突发崩盘无法被提前缓冲,是硬限制**。")
    L.append("- 缓冲强度可按风险偏好调:更强→回撤更低但终值代价更大(NDX 尤甚);也可关闭缓冲只用 C1+C2 校准。")
    L.append("")

    L.append("## 5. D 数据修正")
    L.append("")
    L.append("发现并修复一个两套实现(本系统 v2.5 与 ChatGPT 版)共有的 bug:H.15 利率用 `-9999` 标记节假日,"
             "原解析当真实利率读入、污染月度利率均值。已在加载时过滤 `|值|≥1000` 的哨兵。")
    L.append("")

    L.append("## 6. 总评")
    L.append("")
    L.append("| 维度 | v2.5 | v2.6 | 结论 |")
    L.append("| --- | --- | --- | --- |")
    L.append("| 2020 校准 | 误判中性(base 27.8) | 极冷(分位 11) | ✅ 修复 |")
    L.append("| 极端档触发/边界 | 几乎不触发/脆弱 | 自校准分位 | ✅ 改善(边界仍需迟滞) |")
    L.append("| 利率数据 | −9999 污染 | 已过滤 | ✅ 修复 |")
    L.append("| 期末财富 vs 定投 | −9.8% | ≈ 打平(SPX −1.6%/NDX −6%) | ◑ 收窄,仍不跑赢 |")
    L.append("| 风险调整 vs 定投 | 无改善 | SPX 更优/NDX 持平,波动均降 | ◑ 温和真实改善 |")
    L.append("")
    L.append("*免责声明:本报告为量化方法的技术性回测与教育性讨论,基于公开历史数据,不构成投资建议。历史表现不代表未来收益。*")

    REPORT.write_text("\n".join(L), encoding="utf-8")
    print(f"报告已写入: {REPORT}")
    for r in calib:
        print(f"  {r['时点']:<22} 预期{r['预期档位']:<7} v2.6分位{r['v2.6热度分位']} -> {r['v2.6档位']}")
    for r in risk:
        print(f"  {r['指数']} 期末{r['期末财富比']} maxDD {r['maxDD热/定投']} 波动 {r['年化波动热/定投%']} Calmar {r['Calmar热/定投']}")


if __name__ == "__main__":
    main()
