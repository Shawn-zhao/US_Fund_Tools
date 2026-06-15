"""扫描:要多大冷档杠杆才能在双指数、多起点上达到 +2pp?(趋势闸门 MA100,含追缴强平)
并统计每个组合的最大回撤、是否爆仓。作者:Opus 4.8。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import backtest_v27_leverage_Opus4_8 as L

df, bser, rate_m = L.load_core()
assets = [("SPX", df["spx"]), ("NDX", df["ndx"])]
starts = ["2007-01-01", "2010-01-01", "2015-01-01"]

# 纯①冷档杠杆,逐步加大(blue/green),neutral与热档恒为1.0
SCHED = {
    "冷2.5x": {"blue": 2.5, "green": 1.75},
    "冷3x":   {"blue": 3.0, "green": 2.0},
    "冷4x":   {"blue": 4.0, "green": 2.5},
    "冷6x":   {"blue": 6.0, "green": 3.5},
    # ①+② 混合:连中性也按趋势加杠杆(便宜或中性且上升趋势都加)
    "①+②中性也2x": {"blue": 3.0, "green": 2.5, "neutral": 2.0, "yellow": 1.5, "orange": 1.0, "red": 1.0},
    "①+②中性也3x": {"blue": 3.0, "green": 3.0, "neutral": 3.0, "yellow": 2.0, "orange": 1.0, "red": 1.0},
}

for nm, base in SCHED.items():
    full = {"blue": 1.0, "green": 1.0, "neutral": 1.0, "yellow": 1.0, "orange": 1.0, "red": 1.0}
    full.update(base)
    cap = max(full.values())
    print(f"\n=== {nm}  (lev_cap={cap}, 趋势闸门MA=100) ===")
    for start in starts:
        line = f"  {start[:4]}起 "
        for an, pr in assets:
            out, blown = L.run_lev(pr.rename(an), bser, full, rate_m, lev_cap=cap,
                                   adj=1.0, trend_ma=100, start=start)
            m = L.evaluate(out, rate_m, start=start)
            tag = "✓" if (m["超额pp"] >= 2.0 and not blown) else " "
            bl = "爆仓" if blown else ""
            line += f"| {an}{tag}{m['超额pp']:+5.2f}pp DD{m['maxDD热']}(投{m['maxDD定投']}){bl} "
        print(line)
