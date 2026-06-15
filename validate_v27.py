"""C 档样本外 + 稳健性检验:对能过 B 档的"①+②混合"方案。作者:Opus 4.8。
检验:① 子区间分段(2007-2013/2013-2019/2019-2026);② 样本外(2018起独立);
③ 趋势闸门MA敏感性(50/100/200);④ 融资点差敏感性(1.5%/2.5%);⑤ 渐进再平衡 adj=1/3。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import backtest_v27_leverage_Opus4_8 as L

df, bser, rate_m = L.load_core()
assets = [("SPX", df["spx"]), ("NDX", df["ndx"])]

CAND = {"blue": 3.0, "green": 2.5, "neutral": 2.0, "yellow": 1.5, "orange": 1.0, "red": 1.0}
GENTLE = {"blue": 2.5, "green": 2.0, "neutral": 1.6, "yellow": 1.2, "orange": 1.0, "red": 0.9}
CAP = 3.0

def line(L_, start, end, ma=100, spread=1.5, adj=1.0):
    s = f"  {str(start)[:7]}~{str(end)[:7] if end else '今'} "
    allok = True
    for an, pr in assets:
        out, blown = L.run_lev(pr.rename(an), bser, L_, rate_m, lev_cap=CAP, spread=spread,
                               adj=adj, trend_ma=ma, start=start, end=end)
        m = L.evaluate(out, rate_m, start=str(start))
        ok = m["超额pp"] >= 2.0 and not blown
        allok = allok and ok
        s += f"| {an}{'✓' if ok else ' '}{m['超额pp']:+5.2f}pp(热{m['IRR热%']}/投{m['IRR定投%']},DD{m['maxDD热']}){'爆仓' if blown else ''} "
    return s + ("  <==双双≥2pp" if allok else "")

print("### 候选方案 CAND: blue3/green2.5/neutral2/yellow1.5 (热档1x), 趋势MA100, 点差1.5%, 立即再平衡")
print("\n[1] 子区间分段(每段独立计算 IRR,看是否每段都≥2pp):")
for s, e in [("2007-01-01","2013-01-01"),("2013-01-01","2019-01-01"),("2019-01-01",None),
             ("2007-01-01","2017-01-01"),("2017-01-01",None)]:
    print(line(CAND, s, e))

print("\n[2] 样本外:用 2007-2017 当'调参段'(其实未对日期调参),2018 起样本外独立:")
print(line(CAND, "2018-01-01", None))

print("\n[3] 趋势闸门 MA 敏感性(2007全程,换MA长度,看是否依赖某个特定值):")
for ma in [50, 100, 150, 200]:
    print(f"  MA={ma:3} " + line(CAND, "2007-01-01", None, ma=ma)[6:])

print("\n[4] 融资点差敏感性(点差1.5% vs 2.5%,2007全程):")
for sp in [1.5, 2.5, 3.5]:
    print(f"  点差{sp}% " + line(CAND, "2007-01-01", None, spread=sp)[6:])

print("\n[5] 渐进再平衡 adj=1/3(每月只走1/3,更抗抖动、更可实盘):")
for start in ["2007-01-01","2010-01-01","2015-01-01"]:
    print(line(CAND, start, None, adj=1/3))

print("\n### 更温和方案 GENTLE: blue2.5/green2/neutral1.6/yellow1.2 (回撤更低,看是否仍≥2pp)")
for start in ["2007-01-01","2010-01-01","2015-01-01"]:
    print(line(GENTLE, start, None))
