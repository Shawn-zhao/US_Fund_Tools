"""v2.7 冷档超额部署 / 杠杆回测(作者:Anthropic Claude Opus 4.8)。

目标:在 v2.6 热度档位之上叠加"按档位设定目标股票敞口(可>1x=融资,可<1x=现金缓冲)",
检验能否年化(IRR)超过普通定投 ≥2 个百分点,并按 B 档(双指数+多起点)、C 档(样本外)验证。

诚实定位:这是把"在便宜时投入比定投更多的钱"做实——靠融资在冷档加杠杆。
它能制造真实超额,但引入融资利息、回撤放大与追缴(margin call)强平风险。
核心原则:热档绝不降仓(保持满仓 1.0x,避免牛市拖累),只在冷档加杠杆。
所有参数取圆整数、随热度单调、不对任何历史日期调参;杠杆设上限、按真实 3M 国债+点差计息、含追缴强平模型。

复用 backtest_v26_design.build_core / band_pct / hysteresis(均为本项目自写实现)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import backtest_v25_independent_Opus4_8 as v25
import backtest_v26_design_Opus4_8 as d


def irr_annual(VT: float, N: int) -> float:
    lo, hi = -0.5, 0.5
    for _ in range(100):
        mid = (lo + hi) / 2
        pv = sum((1 + mid) ** (N - t) for t in range(1, N + 1))
        if pv > VT:
            hi = mid
        else:
            lo = mid
    return (1 + (lo + hi) / 2) ** 12 - 1


def maxdd(s: pd.Series) -> float:
    return float((s / s.cummax() - 1).min() * 100)


def run_lev(price, band_series, L, rate_m, spread=1.5, adj=1.0,
            lev_cap=2.5, maint=0.25, trend_ma=0, start="2007-01-01", end=None):
    """每月:价格/利息更新 → 注入 1 单位供款 → 向'目标敞口=L(档)×净值'再平衡。
    L: {band: 目标杠杆};>1 融资买入,<1 留现金缓冲。
    cash<0=融资余额,按 (3M国债+spread) 计息;cash>=0 按 3M国债计息。
    追缴:价格/利息更新后若 净值 < maint×敞口,强制减仓至杠杆=1(模拟 margin call)。
    trend_ma>0:趋势闸门——只有当 决策日价格 ≥ 其 trend_ma 日均线(已企稳转头)才允许加杠杆(>1);
    跌势中(价<均线)即便处于冷档也只保持满仓 1.0x,不接下跌的刀。降仓(<1)不受闸门限制。"""
    h = band_series.loc[start:end].dropna()
    p = price.reindex(h.index).ffill().dropna()
    h = h.reindex(p.index).dropna()
    ma = p.rolling(trend_ma).mean() if trend_ma else None
    months = p.groupby(p.index.to_period("M")).head(1).index

    shares = 0.0
    cash = 0.0
    shares_d = 0.0
    blown = False
    rows = []
    for dt in months[1:]:
        prev = h.index[h.index < dt]
        if len(prev) == 0:
            continue
        band = str(h.loc[prev[-1]])
        px = float(p.loc[dt])

        ann = rate_m.get(dt.to_period("M") - 1, np.nan)
        ann = 0.0 if pd.isna(ann) else ann
        g_cash = (1 + ann / 100.0) ** (1 / 12) - 1
        g_borrow = (1 + (ann + spread) / 100.0) ** (1 / 12) - 1
        eq = shares * px
        cash = cash * (1 + (g_cash if cash >= 0 else g_borrow))
        NW = eq + cash

        if cash < 0 and NW <= maint * eq:
            if NW <= 0:
                blown = True
                rows.append((dt, 0.0, shares_d * px))
                break
            delta = NW - eq
            shares += delta / px
            cash -= delta
            eq = shares * px

        cash += 1.0
        NW = eq + cash
        if NW <= 0:
            blown = True
            rows.append((dt, 0.0, shares_d * px))
            break

        lev = min(L.get(band, 1.0), lev_cap)
        if lev > 1.0 and ma is not None:           # 趋势闸门:跌势中不加杠杆
            d0 = prev[-1]
            mv = ma.get(d0, np.nan)
            if pd.isna(mv) or float(p.loc[d0]) < mv:
                lev = 1.0
        target_eq = lev * NW
        delta = adj * (target_eq - eq)
        shares += delta / px
        cash -= delta

        eq = shares * px
        NW = eq + cash
        shares_d += 1.0 / px
        rows.append((dt, NW, shares_d * px))

    out = pd.DataFrame(rows, columns=["dt", "V", "Vd"]).set_index("dt")
    out["r"] = out["V"].pct_change()
    return out, blown


def evaluate(out, rate_m, start="2007-01-01"):
    V, Vd = out["V"], out["Vd"]
    r = out["r"].iloc[1:].replace([np.inf, -np.inf], np.nan).dropna()
    N = len(V)
    irr_h = irr_annual(float(V.iloc[-1]), N)
    irr_d = irr_annual(float(Vd.iloc[-1]), N)
    rf_m = (rate_m[rate_m.index >= pd.Period(start[:7])].mean() / 100.0) / 12
    down = r[r < rf_m]
    dd_dev = down.sub(rf_m).pow(2).mean() ** 0.5 * np.sqrt(12) if len(down) else np.nan
    sortino = (r.mean() - rf_m) * 12 / dd_dev if dd_dev and dd_dev > 0 else np.nan
    vol = r.std() * np.sqrt(12)
    mddh, mddd = maxdd(V), maxdd(Vd)
    return {
        "IRR热%": round(irr_h * 100, 2), "IRR定投%": round(irr_d * 100, 2),
        "超额pp": round((irr_h - irr_d) * 100, 2),
        "末值比": round(float(V.iloc[-1] / Vd.iloc[-1]), 3),
        "maxDD热": round(mddh, 1), "maxDD定投": round(mddd, 1),
        "波动%": round(vol * 100, 1), "Sortino": round(sortino, 2),
        "Calmar热": round(irr_h / (abs(mddh) / 100), 2) if mddh else np.nan,
        "Calmar定投": round(irr_d / (abs(mddd) / 100), 2),
    }


SCHEDULES = {
    # 核心原则:热档绝不降仓(保持 1.0x 满仓),只在冷档加杠杆。
    "冷杠杆1.5":     {"blue": 1.5, "green": 1.25, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0},
    "冷杠杆2.0":     {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0},
    "冷杠杆2.5":     {"blue": 2.5, "green": 1.75, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0},
    "冷杠杆+轻缓冲": {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 0.85, "red": 0.7},
}


def load_core():
    df = d.build_core()
    bser = d.hysteresis(df["heat_pct"].map(lambda x: d.band_pct(x, d.PCT_CUTS)))
    rate_m = v25.to_month_mean(v25.load_h15_series("RIFLGFCM03_N.B"))
    return df, bser, rate_m


def report(df, bser, rate_m, schedules, lev_cap=2.5, adj=1.0, trend_ma=0,
           starts=("2007-01-01",), ends=(None,)):
    assets = [("SPX", df["spx"]), ("NDX", df["ndx"])]
    for start in starts:
        for end in ends:
            tag = f"start={start}" + (f" end={end}" if end else "")
            print(f"\n{'='*98}\n>>> {tag}  (lev_cap={lev_cap}, adj={adj}, 趋势闸门MA={trend_ma or '无'})")
            for nm, L in schedules.items():
                line = f"  {nm:13}"
                allok = True
                for an, pr in assets:
                    out, blown = run_lev(pr.rename(an), bser, L, rate_m, lev_cap=lev_cap,
                                         adj=adj, trend_ma=trend_ma, start=start, end=end)
                    m = evaluate(out, rate_m, start=start)
                    ok = (m["超额pp"] >= 2.0) and not blown
                    allok = allok and ok
                    bl = " 爆仓!" if blown else ""
                    line += (f" | {an}{m['超额pp']:+5.2f}pp(热{m['IRR热%']}/投{m['IRR定投%']},DD{m['maxDD热']}vs{m['maxDD定投']}){bl}")
                print(line + ("   <== 双双≥2pp" if allok else ""))


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    df, bser, rate_m = load_core()

    print("#"*98)
    print("# A. 无趋势闸门 vs 有趋势闸门(2007 全样本):看闸门是否同时改善收益与回撤")
    print("#"*98)
    for tma in [0, 50, 100, 200]:
        report(df, bser, rate_m, SCHEDULES, lev_cap=2.5, adj=1.0, trend_ma=tma, starts=("2007-01-01",))

    print("\n" + "#"*98)
    print("# B. 多起点稳健性(选定 100 日趋势闸门,同一套参数换起点)")
    print("#"*98)
    report(df, bser, rate_m, SCHEDULES, lev_cap=2.5, adj=1.0, trend_ma=100,
           starts=("2007-01-01", "2010-01-01", "2015-01-01"))


if __name__ == "__main__":
    main()
