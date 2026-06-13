"""v2.6 设计实验脚本(先回测、后定稿)。

================================================================
作者标注:本文件由 Anthropic Claude **Opus 4.8** 编写。
(项目中另有其它模型产出的版本并存,本文件用于与之对照参考。)
================================================================


目标:在不过拟合的前提下,设计能直接实盘、且回测跑赢普通定投的规则。
三项有原理支撑的改动(来自独立复核报告 §8):
  C1 价格调整日频估值:CAPE_当日 = CAPE_上月 × (今日价 / 上月末价);ECY/巴菲特同理用当日价。
     —— CAPE 分母(10年均盈利)是慢变量、分子(价)每日已知,崩盘当天估值应立刻变便宜。
  C2 自校准分位档位:档位按"基础热度在自身历史中的扩展分位"划分,用常规分位线,不对日期调参。
  C3 倍数+备用金:冷档更激进买、热档不砍到 0,未投现金(备用金)按短债收益计息并在冷档动用。

复用 backtest_v25_independent 的数据加载与底层函数(均为我自己的实现)。
本脚本只做实验与选型;选定后再写入 v2.6 文档与最终回测。
"""

from __future__ import annotations

import bisect
from collections import Counter, deque

import numpy as np
import pandas as pd

import backtest_v25_independent_Opus4_8 as v25

BAND_ZH = v25.BAND_ZH


# ---------- C1:价格调整日频估值用的"日频值 vs 月度历史窗口"分位 ----------
def daily_in_monthly_window_pct(daily_val: pd.Series, monthly_hist: pd.Series,
                                lag: int, window: int, invert: bool) -> pd.Series:
    """对每个交易日,把当日(价格调整后)估值,排到截至 (当月-lag) 的最近 window 个
    月度历史值里(mid-rank)。月度窗口按月缓存,日级复用。"""
    mh = monthly_hist.dropna()
    months = list(mh.index)
    pos = {m: i for i, m in enumerate(months)}
    vals = mh.values
    cache: dict[int, list[float]] = {}
    out = pd.Series(np.nan, index=daily_val.index)
    for t, v in daily_val.items():
        if pd.isna(v):
            continue
        m = t.to_period("M") - lag
        i = pos.get(m)
        if i is None or (i + 1) < window:
            continue
        if i not in cache:
            cache[i] = sorted(vals[i - window + 1: i + 1])
        win = cache[i]
        less = bisect.bisect_left(win, v)
        leq = bisect.bisect_right(win, v)
        p = (less + leq) / (2 * len(win)) * 100.0
        out[t] = 100.0 - p if invert else p
    return out


def build_core() -> pd.DataFrame:
    """构建 v2.6 各维度日频得分与基础热度(含 C1 价格调整估值)。"""
    spx = v25.load_yahoo("yahoo_GSPC_1980-01-01_2026-06-13.csv")
    ndx = v25.load_yahoo("yahoo_NDX_1980-01-01_2026-06-13.csv")
    vix = v25.load_yahoo("yahoo_VIX_1980-01-01_2026-06-13.csv")
    vix3m = v25.load_yahoo("yahoo_VIX3M_1980-01-01_2026-06-13.csv")
    w5000 = v25.load_yahoo("yahoo_W5000_1980-01-01_2026-06-13.csv")
    cape = v25.load_cape()
    cpi = v25.load_fred("fred_CPIAUCSL.csv")
    gdp = v25.load_fred("fred_GDP.csv")
    hy = v25.load_hy_oas()
    pc = v25.load_put_call()
    breadth = v25.load_breadth()
    nom10 = v25.load_h15_series("RIFLGFCY10_N.B")
    tips10 = v25.load_h15_series("RIFLGFCY10_XII_N.B")

    idx = spx.index.intersection(ndx.index)
    spx, ndx = spx.reindex(idx), ndx.reindex(idx)
    df = pd.DataFrame(index=idx)
    df["spx"], df["ndx"] = spx, ndx
    per = idx.to_period("M")

    # ---- C1 价格调整估值 ----
    cape_m = v25.to_month_last(cape)
    spx_eom = spx.groupby(spx.index.to_period("M")).last()
    avail = per - 1
    cape_av = pd.Series(cape_m.reindex(avail).values, index=idx)
    eom_av = pd.Series(spx_eom.reindex(avail).values, index=idx)
    cape_daily = cape_av * spx / eom_av
    df["CAPE"] = daily_in_monthly_window_pct(cape_daily, cape_m, lag=1, window=180, invert=False)

    cpi_m = v25.to_month_last(cpi)
    infl10 = ((cpi_m / cpi_m.shift(120)) ** (1 / 10) - 1) * 100.0
    nom10_m = v25.to_month_mean(nom10)
    tips_m = v25.to_month_mean(tips10)
    recon_real = nom10_m.reindex(infl10.index).ffill() - infl10
    real10_m = tips_m.combine_first(recon_real)
    # 日频实际利率:TIPS 日频 ffill,2003 前用重构月度 ffill
    real10_daily = tips10.reindex(idx, method="ffill")
    recon_daily = v25.daily_ffill(recon_real.to_timestamp(how="start"), idx)
    real10_daily = real10_daily.combine_first(recon_daily)
    ecy_daily = 100.0 / cape_daily - real10_daily
    ecy_m = (100.0 / cape_m).reindex(real10_m.index) - real10_m
    ecy_m = ecy_m.dropna()
    df["ECY"] = daily_in_monthly_window_pct(ecy_daily, ecy_m, lag=1, window=180, invert=True)

    gdp_av = gdp.copy()
    gdp_av.index = gdp_av.index + pd.DateOffset(months=3)
    gdp_daily = gdp_av.reindex(idx, method="ffill")
    buffett_daily = w5000.reindex(idx, method="ffill") / gdp_daily
    w5000_m = v25.to_month_last(w5000)
    gdp_m = v25.to_month_last(gdp_av).reindex(w5000_m.index, method="ffill")
    buffett_m = (w5000_m / gdp_m).dropna()
    df["Buffett"] = daily_in_monthly_window_pct(buffett_daily, buffett_m, lag=1, window=180, invert=False)

    df["V"] = 0.35 * df["CAPE"] + 0.45 * df["ECY"] + 0.20 * df["Buffett"]

    # ---- 情绪 / 趋势 / 广度(与 v2.5 同口径,全历史扩展) ----
    df["VIX"] = v25.expanding_pct(vix.reindex(idx).dropna(), invert=True).reindex(idx)
    df.loc[vix.reindex(idx) > 40, "VIX"] = np.minimum(df["VIX"], 5.0)
    df["VIXTerm"] = v25.expanding_pct((vix / vix3m).dropna(), invert=True).reindex(idx)
    pc10 = pc.rolling(10, min_periods=10).mean().dropna()
    df["PutCall"] = v25.expanding_pct(pc10, invert=True).reindex(idx)
    df["S"] = v25.reweight(df[["VIX", "VIXTerm", "PutCall"]], {"VIX": 0.45, "VIXTerm": 0.25, "PutCall": 0.30})

    dev_s = (spx / spx.rolling(200).mean() - 1).dropna()
    dev_n = (ndx / ndx.rolling(200).mean() - 1).dropna()
    df["Dev200"] = (v25.expanding_pct(dev_s).reindex(idx) + v25.expanding_pct(dev_n).reindex(idx)) / 2
    dd_s = (1 - spx / spx.cummax()).dropna()
    dd_n = (1 - ndx / ndx.cummax()).dropna()
    dd_score = (v25.expanding_pct(dd_s, invert=True).reindex(idx)
                + v25.expanding_pct(dd_n, invert=True).reindex(idx)) / 2
    anchor = (dd_s.reindex(idx) > 0.30) | (dd_n.reindex(idx) > 0.40)
    df["Drawdown"] = dd_score.where(~anchor.fillna(False), np.minimum(dd_score, 5.0))
    rsi_s = v25.expanding_pct(v25.weekly_rsi(spx), min_periods=52)
    rsi_n = v25.expanding_pct(v25.weekly_rsi(ndx), min_periods=52)
    df["WeeklyRSI"] = ((rsi_s + rsi_n) / 2).reindex(idx, method="ffill")
    df["T"] = 0.40 * df["Dev200"] + 0.35 * df["Drawdown"] + 0.25 * df["WeeklyRSI"]

    hy_d = hy.reindex(idx, method="ffill")
    df["HY"] = v25.expanding_pct(hy.dropna(), invert=True).reindex(hy.index).reindex(idx, method="ffill")
    df.loc[hy_d > 8.0, "HY"] = np.minimum(df["HY"], 5.0)
    br_d = breadth.reindex(idx, method="ffill")
    df["BreadthU"] = v25.breadth_u(br_d.dropna()).reindex(idx)
    df["B"] = v25.reweight(df[["BreadthU", "HY"]], {"BreadthU": 0.50, "HY": 0.50})

    df["base"] = 0.40 * df["V"] + 0.25 * df["S"] + 0.25 * df["T"] + 0.10 * df["B"]
    df["heat_pct"] = v25.expanding_pct(df["base"].dropna(), min_periods=252).reindex(idx)
    return df


# ---------- C2 档位 ----------
FIXED_EDGES = [(15, "blue"), (35, "green"), (60, "neutral"), (75, "yellow"), (88, "orange"), (101, "red")]


def band_fixed(v):
    if pd.isna(v):
        return "missing"
    for ub, n in FIXED_EDGES:
        if v < ub:
            return n
    return "red"


def band_pct(p, cuts):
    """cuts = (blue, green, neutral, yellow, orange) 的分位上界。"""
    if pd.isna(p):
        return "missing"
    b, g, n, y, o = cuts
    if p < b:
        return "blue"
    if p < g:
        return "green"
    if p < n:
        return "neutral"
    if p < y:
        return "yellow"
    if p < o:
        return "orange"
    return "red"


def hysteresis(raw: pd.Series, days=10) -> pd.Series:
    vals = raw.dropna()
    if vals.empty:
        return raw
    cur = pend = vals.iloc[0]
    cnt = 0
    out = {}
    for dt, c in vals.items():
        if c == cur:
            pend, cnt = cur, 0
        elif c == pend:
            cnt += 1
            if cnt >= days:
                cur, cnt = c, 0
        else:
            pend, cnt = c, 1
        out[dt] = cur
    return pd.Series(out).reindex(raw.index)


# ---------- C3 定投模拟(备用金计息) ----------
def dca(df, price, band_series, mult, reserve_annual=0.0, deploy=None, rate_m=None, cap=None, start="2007-01-01"):
    """deploy = {band: 额外动用"当前备用金"的比例}(在 mult 之外,把囤积的弹药在冷档打出去)。
    rate_m = 按月(Period M)的真实短端年化利率(%);给定则备用金按真实利率计息,否则用常数 reserve_annual。"""
    deploy = deploy or {}
    h = band_series.loc[start:].dropna()
    p = price.reindex(h.index).ffill().dropna()
    h = h.reindex(p.index).dropna()
    months = p.groupby(p.index.to_period("M")).head(1).index
    g_const = (1 + reserve_annual) ** (1 / 12)
    sh = res = shf = 0.0
    vh, vf, used = [], [], []
    for dt in months[1:]:
        prev = h.index[h.index < dt]
        if len(prev) == 0:
            continue
        band = str(h.loc[prev[-1]])
        px = float(p.loc[dt])
        if rate_m is not None:
            per = (dt.to_period("M") - 1)
            r = rate_m.get(per, np.nan)
            g = (1 + (r / 100.0)) ** (1 / 12) if not pd.isna(r) else 1.0
        else:
            g = g_const
        res_g = res * g
        avail = res_g + 1.0
        target = mult.get(band, 1.0) + deploy.get(band, 0.0) * res_g
        if cap is not None:
            target = max(target, avail - cap)   # 备用金上限:超出部分立即投入,避免结构性欠配
        inv = min(target, avail)
        sh += inv / px
        res = avail - inv
        shf += 1.0 / px
        vh.append(sh * px + res)
        vf.append(shf * px)
        used.append(band)
    vh, vf = pd.Series(vh), pd.Series(vf)
    mdd = lambda v: float((v / v.cummax() - 1).min() * 100)
    return {"rel%": round((vh.iloc[-1] / vf.iloc[-1] - 1) * 100, 2),
            "heat_mdd%": round(mdd(vh), 1), "fix_mdd%": round(mdd(vf), 1),
            "end_res": round(res, 1), "bands": dict(Counter(used))}


# ---------- 实验 ----------
MULTS = {
    "pause(v2.5)":  {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 0.5, "orange": 0.0, "red": 0.0},
    "soft":         {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 0.75, "orange": 0.5, "red": 0.25},
    "aggr_cold":    {"blue": 3.0, "green": 2.0, "neutral": 1.0, "yellow": 0.75, "orange": 0.5, "red": 0.5},
    "aggr_cold2":   {"blue": 3.0, "green": 1.75, "neutral": 1.0, "yellow": 0.8, "orange": 0.6, "red": 0.4},
}
PCT_CUTS = (10, 30, 75, 90, 97)


def dca_continuous(df, price, p_series, k=2.0, lo=0.0, hi=3.0, reserve_annual=0.0, start="2007-01-01"):
    """连续倾斜定投:每月目标 = clamp(k*(1 - 分位/100), lo, hi)。分位均匀 → 平均≈1x(对定投公平)。"""
    p = p_series.loc[start:].dropna()
    px = price.reindex(p.index).ffill().dropna()
    p = p.reindex(px.index).dropna()
    months = px.groupby(px.index.to_period("M")).head(1).index
    g = (1 + reserve_annual) ** (1 / 12)
    sh = res = shf = 0.0
    tgt_sum = 0.0
    vh, vf = [], []
    for dt in months[1:]:
        prev = p.index[p.index < dt]
        if len(prev) == 0:
            continue
        pp = float(p.loc[prev[-1]])
        price_t = float(px.loc[dt])
        target = min(max(k * (1 - pp / 100.0), lo), hi)
        tgt_sum += target
        avail = res * g + 1.0
        inv = min(target, avail)
        sh += inv / price_t
        res = avail - inv
        shf += 1.0 / price_t
        vh.append(sh * price_t + res)
        vf.append(shf * price_t)
    vh, vf = pd.Series(vh), pd.Series(vf)
    mdd = lambda v: float((v / v.cummax() - 1).min() * 100)
    return {"rel%": round((vh.iloc[-1] / vf.iloc[-1] - 1) * 100, 2),
            "heat_mdd%": round(mdd(vh), 1), "fix_mdd%": round(mdd(vf), 1),
            "end_res": round(res, 1), "avg_target": round(tgt_sum / len(vh), 3)}


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    df = build_core()

    # 校准:2020 是否被治好(价格调整后)
    for s, e, name in [("2020-03-16", "2020-03-31", "2020.03"),
                       ("2009-03-02", "2009-03-31", "2009.03"),
                       ("2022-10-01", "2022-10-31", "2022.10"),
                       ("2021-11-01", "2021-12-31", "2021.11-12")]:
        sub = df.loc[s:e].dropna(subset=["base"])
        if not sub.empty:
            print(f"  {name}: base中位={sub['base'].median():.1f} 分位中位={sub['heat_pct'].median():.1f} V中位={sub['V'].median():.1f}")
    print("=" * 70)

    print(">>> 离散档位方案:")
    band_modes = {"fixed": df["base"].map(band_fixed),
                  "pct": df["heat_pct"].map(lambda x: band_pct(x, PCT_CUTS))}
    for bm_name, raw in band_modes.items():
        bser = hysteresis(raw)
        for mname, mult in MULTS.items():
            for ry in [0.0, 0.02]:
                rs = dca(df, df["spx"].rename("SPX"), bser, mult, ry)
                rn = dca(df, df["ndx"].rename("NDX"), bser, mult, ry)
                flag = "  <== 双双跑赢" if rs["rel%"] > 0 and rn["rel%"] > 0 else ""
                print(f"{bm_name:5} | {mname:11} | 备用金{int(ry*100)}% | "
                      f"SPX {rs['rel%']:+6.2f}% (回撤{rs['heat_mdd%']}) | "
                      f"NDX {rn['rel%']:+6.2f}% (回撤{rn['fix_mdd%']}) {flag}")

    # 真实短端利率(3个月国债)作为备用金收益率
    tb3 = v25.load_h15_series("RIFLGFCM03_N.B")
    rate_m = v25.to_month_mean(tb3)

    print(">>> 无拖累+冷档打弹药方案(pct档位, neutral/yellow=1x 不拖累, 只在orange/red囤, blue/green打):")
    bser = hysteresis(df["heat_pct"].map(lambda x: band_pct(x, PCT_CUTS)))
    # mult: 中性/偏热都 1x(不拖累),只在 orange/red 减;冷档 deploy 动用备用金
    NODRAG = [
        ("守正出奇A", {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 0.5, "red": 0.0},
         {"blue": 1 / 3, "green": 1 / 6}),
        ("守正出奇B", {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 0.5, "red": 0.0},
         {"blue": 1 / 2, "green": 1 / 4}),
        ("守正出奇C", {"blue": 2.5, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 0.5, "red": 0.0},
         {"blue": 1 / 2, "green": 1 / 6}),
        ("仅极端减D", {"blue": 2.0, "green": 1.5, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 0.0},
         {"blue": 1 / 2, "green": 1 / 4}),
    ]
    for nm, mult, dep in NODRAG:
        for cap in [None, 12, 6, 3]:
            rs = dca(df, df["spx"].rename("SPX"), bser, mult, deploy=dep, rate_m=rate_m, cap=cap)
            rn = dca(df, df["ndx"].rename("NDX"), bser, mult, deploy=dep, rate_m=rate_m, cap=cap)
            flag = "  <== 双双跑赢" if rs["rel%"] > 0 and rn["rel%"] > 0 else ""
            capt = "无" if cap is None else f"{cap}月"
            print(f"{nm} | 备用金上限{capt:>4} | SPX {rs['rel%']:+6.2f}% (回撤{rs['heat_mdd%']} vs {rs['fix_mdd%']}, 末备{rs['end_res']}) | "
                  f"NDX {rn['rel%']:+6.2f}% {flag}")


if __name__ == "__main__":
    main()
