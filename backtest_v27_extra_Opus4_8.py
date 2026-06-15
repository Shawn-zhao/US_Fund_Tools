"""v2.7-额外加仓(不借钱):冷档自掏腰包多买,其他正常买。作者:Opus 4.8。
极冷 mult 份、偏冷 mult 份、其余 1 份;多出来的是新增本金(非融资、非囤积现金),买入即满仓持有。
公平比较:资金加权 IRR(每月供款额可变,作为现金流求内部收益率),对比普通定投(每月 1 份)。
也报告期末财富、总投入,供透明对照。复用 v2.6 build_core 的热度/分档。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd
import backtest_v25_independent_Opus4_8 as v25
import backtest_v26_design_Opus4_8 as d


def irr_var(cfs, VT):
    """cfs: 按月顺序的供款额列表(t=1..N);求月利率使 sum cf_t*(1+m)^(N-t)=VT,返回年化。"""
    N = len(cfs)
    lo, hi = -0.5, 0.5
    for _ in range(100):
        m = (lo + hi) / 2
        pv = sum(c * (1 + m) ** (N - t) for t, c in enumerate(cfs, 1))
        if pv > VT:
            hi = m
        else:
            lo = m
    return (1 + (lo + hi) / 2) ** 12 - 1


def run_extra(price, band_series, mult, start="2007-01-01", end=None):
    h = band_series.loc[start:end].dropna()
    p = price.reindex(h.index).ffill().dropna()
    h = h.reindex(p.index).dropna()
    months = p.groupby(p.index.to_period("M")).head(1).index
    sh = shd = 0.0
    cfs, cfs_d = [], []
    for dt in months[1:]:
        prev = h.index[h.index < dt]
        if len(prev) == 0:
            continue
        band = str(h.loc[prev[-1]])
        px = float(p.loc[dt])
        c = mult.get(band, 1.0)
        sh += c / px
        shd += 1.0 / px
        cfs.append(c)
        cfs_d.append(1.0)
    px_last = float(p.loc[months[-1]])
    Vh, Vd = sh * px_last, shd * px_last
    irr_h, irr_d = irr_var(cfs, Vh), irr_var(cfs_d, Vd)
    mdd = lambda: None
    return {
        "IRR额外%": round(irr_h * 100, 2), "IRR定投%": round(irr_d * 100, 2),
        "超额pp": round((irr_h - irr_d) * 100, 2),
        "总投入比": round(sum(cfs) / sum(cfs_d), 2),
        "期末财富比": round(Vh / Vd, 2),
    }


def main():
    df = d.build_core()
    bser = d.hysteresis(df["heat_pct"].map(lambda x: d.band_pct(x, d.PCT_CUTS)))
    assets = [("SPX", df["spx"]), ("NDX", df["ndx"])]
    starts = ["2007-01-01", "2010-01-01", "2015-01-01"]

    SCHED = {
        "你的方案 极冷3/偏冷1.5": {"blue": 3.0, "green": 1.5},
        "强 极冷5/偏冷2":        {"blue": 5.0, "green": 2.0},
        "更强 极冷8/偏冷3":      {"blue": 8.0, "green": 3.0},
        "极端 极冷12/偏冷4/中性1.5": {"blue": 12.0, "green": 4.0, "neutral": 1.5},
    }
    for nm, base in SCHED.items():
        full = {"blue": 1.0, "green": 1.0, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0}
        full.update(base)
        print(f"\n=== {nm} ===")
        for s in starts:
            line = f"  {s[:4]}起 "
            for an, pr in assets:
                m = run_extra(pr.rename(an), bser, full, start=s)
                tag = "✓" if m["超额pp"] >= 2.0 else " "
                line += (f"| {an}{tag}{m['超额pp']:+5.2f}pp(额{m['IRR额外%']}/投{m['IRR定投%']},"
                         f"总投入{m['总投入比']}x,末财富{m['期末财富比']}x) ")
            print(line)

    # 样本外 2018 起,你的方案
    print("\n=== 样本外检验:你的方案(极冷3/偏冷1.5),2018 起独立 ===")
    full = {"blue": 3.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0}
    for an, pr in assets:
        m = run_extra(pr.rename(an), bser, full, start="2018-01-01")
        print(f"  {an}: 超额{m['超额pp']:+.2f}pp (额{m['IRR额外%']}/投{m['IRR定投%']}, 总投入{m['总投入比']}x)")


if __name__ == "__main__":
    main()
