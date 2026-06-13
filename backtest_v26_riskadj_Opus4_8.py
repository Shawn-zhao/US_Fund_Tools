"""v2.6 风险调整评估(干净版):热度驱动的"目标现金缓冲"。

================================================================
作者标注:本文件由 Anthropic Claude **Opus 4.8** 编写。
(项目中另有其它模型产出的版本并存,本文件用于与之对照参考。)
================================================================


模型(直观、可实盘、非对日期调参):
  - 档位 = 基础热度的扩展分位(C2),分位线 (10,30,75,90,97)。
  - 每月按档位设定"目标现金权重" cw(冷/中性=0%,只在偏热↑、过热↑↑、极端过热↑↑↑);
    现金(备用金)按真实 3M 国债利率计息。
  - 每月:先随价格更新持仓、注入 1 单位供款,再向目标现金权重再平衡:
      · 现金 > 目标 → 把多余现金买入股票(冷档自动满仓+动用囤积弹药);
      · 现金 < 目标 → 仅在偏热及以上按"每月向目标靠拢 adj 比例"卖出持仓(保守、抗抖动,符合§4.2)。
  缓冲在崩盘时垫住回撤(现金不跌),热档计息;代价是牛市中持有现金的拖累。

DCA 基准:全程 100% 股票、每月 1 单位。
风险指标:期末财富比、组合 maxDD、年化波动、Sortino、Calmar=IRR/|maxDD|。
判定"风险调整跑赢" = 期末≥0.95 且 maxDD 明显更低 且 Calmar、Sortino 双双更高(SPX、NDX 都要满足)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import backtest_v25_independent_Opus4_8 as v25
import backtest_v26_design_Opus4_8 as d


def run(df, price, band_series, cw, rate_m, adj=1.0, cold_extra=None, start="2007-01-01"):
    cold_extra = cold_extra or {}
    h = band_series.loc[start:].dropna()
    p = price.reindex(h.index).ffill().dropna()
    h = h.reindex(p.index).dropna()
    months = p.groupby(p.index.to_period("M")).head(1).index
    shares = reserve = 0.0
    shares_d = 0.0
    prev_px = None
    V_prev = None
    rows = []
    for dt in months[1:]:
        prev = h.index[h.index < dt]
        if len(prev) == 0:
            continue
        band = str(h.loc[prev[-1]])
        px = float(p.loc[dt])
        rc = rate_m.get(dt.to_period("M") - 1, np.nan)
        cash_ret = (1 + rc / 100.0) ** (1 / 12) - 1 if not pd.isna(rc) else 0.0
        # 1) 价格/利息更新
        eq = shares * px
        reserve *= (1 + cash_ret)
        W = eq + reserve                      # 供款前组合价值
        # 2) 注入供款
        reserve += 1.0
        total = eq + reserve
        # 3) 向目标现金权重再平衡
        target_cash = cw.get(band, 0.0) / 100.0 * total
        if reserve > target_cash:             # 现金多 → 买入(冷档自动满仓)
            buy = reserve - target_cash
            buy += cold_extra.get(band, 0.0) * 0.0   # 预留:冷档额外加力(此处用权重已足够)
            shares += buy / px
            reserve -= buy
        else:                                  # 现金少 → 仅偏热及以上,按 adj 比例向目标靠拢卖出
            if band in ("yellow", "orange", "red"):
                sell = (target_cash - reserve) * adj
                shares -= sell / px
                reserve += sell
        eq = shares * px
        Vt = eq + reserve
        shares_d += 1.0 / px
        r = (W / V_prev - 1.0) if V_prev else 0.0
        rows.append((dt, Vt, shares_d * px, r))
        V_prev = Vt
        prev_px = px
    out = pd.DataFrame(rows, columns=["dt", "V", "Vd", "r"]).set_index("dt")
    return out


def irr_annual(VT, N):
    lo, hi = -0.05, 0.05
    for _ in range(80):
        mid = (lo + hi) / 2
        pv = sum((1 + mid) ** (N - t) for t in range(1, N + 1))
        if pv > VT:
            hi = mid
        else:
            lo = mid
    return (1 + (lo + hi) / 2) ** 12 - 1


def evaluate(out, rate_m):
    V, Vd, r = out["V"], out["Vd"], out["r"].iloc[1:]
    maxdd = lambda s: float((s / s.cummax() - 1).min() * 100)
    N = len(V)
    irr_h, irr_d = irr_annual(float(V.iloc[-1]), N), irr_annual(float(Vd.iloc[-1]), N)
    rf_m = (rate_m[rate_m.index >= pd.Period("2007-01")].mean() / 100.0) / 12
    down = r[r < rf_m]
    dd_dev = down.sub(rf_m).pow(2).mean() ** 0.5 * np.sqrt(12) if len(down) else np.nan
    sortino = (r.mean() - rf_m) * 12 / dd_dev if dd_dev and dd_dev > 0 else np.nan
    vol = r.std() * np.sqrt(12)
    mddh, mddd = maxdd(V), maxdd(Vd)
    return {"期末比": round(float(V.iloc[-1] / Vd.iloc[-1]), 3),
            "IRR热%": round(irr_h * 100, 2), "IRR定投%": round(irr_d * 100, 2),
            "maxDD热": round(mddh, 1), "maxDD定投": round(mddd, 1),
            "波动%": round(vol * 100, 1), "Sortino": round(sortino, 2),
            "Calmar热": round(irr_h / (abs(mddh) / 100), 2), "Calmar定投": round(irr_d / (abs(mddd) / 100), 2)}


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    df = d.build_core()
    rate_m = v25.to_month_mean(v25.load_h15_series("RIFLGFCM03_N.B"))
    bser = d.hysteresis(df["heat_pct"].map(lambda x: d.band_pct(x, d.PCT_CUTS)))

    SCHED = {
        "仅极端(orange20/red40)":  {"yellow": 0, "orange": 20, "red": 40},
        "温和(y5/o20/r40)":        {"yellow": 5, "orange": 20, "red": 40},
        "中等(y10/o30/r50)":       {"yellow": 10, "orange": 30, "red": 50},
        "偏强(y15/o40/r60)":       {"yellow": 15, "orange": 40, "red": 60},
        "强(y20/o50/r70)":         {"yellow": 20, "orange": 50, "red": 70},
    }
    print("基准 DCA: ", {k: evaluate(run(df, df["spx"].rename("SPX"), bser, {}, rate_m), rate_m)[k]
                       for k in ["maxDD定投", "IRR定投%", "Calmar定投"]})
    print("=" * 100)
    for adj in [1.0, 1 / 3]:
        print(f">>> 向目标靠拢比例 adj={adj:.2f}（1.0=立即到位, 1/3=每月走1/3更保守）")
        for nm, cw in SCHED.items():
            line = f"  {nm:24}"
            ok = True
            for asset, price in [("SPX", df["spx"]), ("NDX", df["ndx"])]:
                m = evaluate(run(df, price.rename(asset), bser, cw, rate_m, adj=adj), rate_m)
                win = m["期末比"] >= 0.95 and m["maxDD热"] > m["maxDD定投"] + 1.5 and \
                    m["Calmar热"] > m["Calmar定投"] and m["Sortino"] > 0
                ok = ok and win
                line += (f" | {asset} 期末{m['期末比']} DD{m['maxDD热']}vs{m['maxDD定投']} "
                         f"Cal{m['Calmar热']}vs{m['Calmar定投']} Sor{m['Sortino']}")
            print(line + ("   <== 双双风险调整跑赢" if ok else ""))


if __name__ == "__main__":
    main()
