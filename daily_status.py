#!/usr/bin/env python3
"""每日热度状态引擎 + 网页生成(合并版 v2.6)。作者:Anthropic Claude Opus 4.8。

做三件事:
  1) 抓取 Yahoo 最新日线(^GSPC/^NDX/^VIX/^VIX3M/^W5000),追加进缓存 CSV(失败则用现有缓存)。
  2) 复用 build_core 用 C1 价格调整口径算出当日热度/估值/趋势/恐慌信号。
  3) 回放"合并版状态机"(固定定投 + 罕见时一次性 20% 避险),得出 SPX/NDX 今日状态,
     输出 status.json 与自包含 index.html(放服务器,每天刷新即看到今日状态)。

部署:用 cron 每天美股收盘后跑一次,例如(服务器东部时间 17:30):
    30 17 * * 1-5  cd /path/to/US_Fund_Tools && /usr/bin/python3 daily_status.py >> daily_status.log 2>&1
然后用任意静态服务器伺服本目录的 index.html 即可。
"""
from __future__ import annotations
import json, sys, time, random, datetime as dt, urllib.request, urllib.parse
from pathlib import Path
import numpy as np
import pandas as pd

CST = dt.timezone(dt.timedelta(hours=8))  # 东八区(北京时间)

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "backtest_outputs" / "cache"

# ---------- 配置:每月定投计划(可改) ----------
MONTHLY_PLAN = {
    "SPX": {"etf": "VOO", "amount": 1167},
    "NDX": {"etf": "QQQ", "amount": 500},
}
RISK_CASH_TARGET = 0.20   # 触发避险时的现金目标
CONFIRM_DAYS = 10         # 连续确认天数
START = "2007-01-01"

# 缓存文件名(与 build_core 一致)
YH = {
    "GSPC": "yahoo_GSPC_1980-01-01_2026-06-13.csv",
    "NDX":  "yahoo_NDX_1980-01-01_2026-06-13.csv",
    "VIX":  "yahoo_VIX_1980-01-01_2026-06-13.csv",
    "VIX3M":"yahoo_VIX3M_1980-01-01_2026-06-13.csv",
    "W5000":"yahoo_W5000_1980-01-01_2026-06-13.csv",
}
YH_SYMBOL = {"GSPC": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "VIX3M": "^VIX3M", "W5000": "^W5000"}


def log(*a):
    print(dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), *a)


# ---------- 1) 抓取并追加最新行情 ----------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http_bytes(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json,text/csv,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_yahoo(symbol: str, retries: int = 5):
    """返回 [(date_str, close), ...] 最近 3 个月日线。
    带指数退避重试,并在 query1/query2 间轮换,缓解 429/限流;全部失败才抛异常。"""
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for attempt in range(retries):
        host = hosts[attempt % len(hosts)]
        url = (f"https://{host}/v8/finance/chart/"
               + urllib.parse.quote(symbol) + "?range=3mo&interval=1d")
        try:
            j = json.loads(http_bytes(url).decode())
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            out = [(dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), float(c))
                   for t, c in zip(ts, closes) if c is not None]
            if out:
                return out
            raise ValueError("空数据")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                wait = min(2 ** attempt, 20) + random.uniform(0, 1.5)
                log(f"    {symbol} 第{attempt+1}次失败({repr(e)[:50]}),{wait:.1f}s 后重试")
                time.sleep(wait)
    raise last


def update_cache():
    """逐个标的抓取并把新日期追加到缓存 CSV。任一失败仅记录、用旧缓存。"""
    for key, fname in YH.items():
        path = CACHE / fname
        try:
            df = pd.read_csv(path, parse_dates=["date"])
            last = df["date"].max()
            rows = fetch_yahoo(YH_SYMBOL[key])
            new = [(d, c) for d, c in rows if pd.Timestamp(d) > last]
            if new:
                add = pd.DataFrame(new, columns=["date", "close"])
                add["date"] = pd.to_datetime(add["date"])
                df = pd.concat([df, add], ignore_index=True).drop_duplicates("date").sort_values("date")
                df.to_csv(path, index=False)
                log(f"  {key}: +{len(new)} 行,至 {new[-1][0]}")
            else:
                log(f"  {key}: 已最新({last.date()})")
        except Exception as e:
            log(f"  {key}: 抓取失败,用旧缓存 [{repr(e)[:80]}]")


# ---------- 2) 算信号 ----------
def compute_signals():
    import backtest_v25_independent_Opus4_8 as v25
    import backtest_v26_design_Opus4_8 as d
    df = d.build_core()
    df["heat"] = df["heat_pct"]
    # 原始 VIX 分位(高=恐慌)与期限结构
    vix = v25.load_yahoo(YH["VIX"]).reindex(df.index).ffill()
    vix3m = v25.load_yahoo(YH["VIX3M"]).reindex(df.index).ffill()
    df["vix"] = vix
    df["vix3m"] = vix3m
    df["vix_pct"] = v25.expanding_pct(vix.dropna()).reindex(df.index)
    return df


# ---------- 3) 回放合并版状态机 ----------
def replay(df, price_col):
    """对单个资产回放状态机,返回逐日状态 DataFrame(到今日)。"""
    price = df[price_col]
    ma200 = price.rolling(200).mean()
    dd = 1 - price / price.cummax()
    sub = df.loc[START:].copy()
    sub["price"] = price.loc[START:]
    sub["ma200"] = ma200.loc[START:]
    sub["dd"] = dd.loc[START:]
    need = ["CAPE", "Buffett", "heat", "vix", "vix3m", "vix_pct", "price", "ma200", "dd"]
    sub = sub.dropna(subset=need)

    state = "normal"
    trig = 0          # 连续满足减仓条件天数
    above = 0         # 连续 price>ma200 天数
    days_in = 0
    recs = []
    for ts, row in sub.iterrows():
        cape, buff, heat = row["CAPE"], row["Buffett"], row["heat"]
        trend_break = (row["price"] < row["ma200"]) or (row["dd"] > 0.10)
        derisk_cond = (cape >= 90) and (buff >= 90) and trend_break
        above = above + 1 if row["price"] > row["ma200"] else 0
        panic = (row["vix_pct"] > 95) and (row["vix"] > row["vix3m"])

        if state == "normal":
            trig = trig + 1 if derisk_cond else 0
            if trig >= CONFIRM_DAYS:
                state, trig, days_in = "risk_cash", 0, 0
            else:
                days_in += 1
        else:  # risk_cash
            buyback = (heat < 30) or panic or (cape < 90) or (buff < 90) or (above >= CONFIRM_DAYS)
            if buyback:
                state, days_in = "normal", 0
            else:
                days_in += 1
        recs.append((ts, state, trig, above, days_in, derisk_cond, panic))
    out = pd.DataFrame(recs, columns=["dt", "state", "trig", "above", "days_in",
                                      "derisk_cond", "panic"]).set_index("dt")
    return sub, out


def build_status():
    df = compute_signals()
    as_of = df.dropna(subset=["heat", "CAPE", "Buffett"]).index.max()
    assets = {}
    for asset, col in [("SPX", "spx"), ("NDX", "ndx")]:
        sub, st = replay(df, col)
        ts = st.index.max()
        r = sub.loc[ts]
        s = st.loc[ts]
        state = s["state"]
        heat = float(r["heat"])
        cold_extreme = heat <= 10
        plan = MONTHLY_PLAN[asset]
        if state == "normal":
            action = f"每月定投 ${plan['amount']:,} 买入 {plan['etf']};无需其他操作。"
            if cold_extreme:
                action += " ⚠️ 极冷:历史级便宜,有闲钱可额外加仓。"
            elif s["trig"] > 0:
                action += f"(已连续 {int(s['trig'])}/{CONFIRM_DAYS} 天满足减仓条件,未到 {CONFIRM_DAYS} 天不动)"
        else:
            action = (f"维持约 {int(RISK_CASH_TARGET*100)}% 现金(SGOV/BIL);"
                      f"每月定投 ${plan['amount']:,} 照常买入 {plan['etf']};等待买回条件。")
        assets[asset] = {
            "state": state,
            "state_zh": "风险现金" if state == "risk_cash" else ("极冷-满仓" if cold_extreme else "普通-满仓"),
            "color": "#c0392b" if state == "risk_cash" else ("#1f6feb" if cold_extreme else "#1a8f4c"),
            "action": action,
            "cold_extreme": bool(cold_extreme),
            "days_in_state": int(s["days_in"]),
            "metrics": {
                "热度分位": round(heat, 1),
                "CAPE分位": round(float(r["CAPE"]), 1),
                "巴菲特分位": round(float(r["Buffett"]), 1),
                "价格": round(float(r["price"]), 1),
                "200日均线": round(float(r["ma200"]), 1),
                "距200均线%": round((float(r["price"]) / float(r["ma200"]) - 1) * 100, 1),
                "距高点回撤%": round(-float(r["dd"]) * 100, 1),
                "VIX": round(float(r["vix"]), 1),
                "VIX3M": round(float(r["vix3m"]), 1),
                "VIX分位": round(float(r["vix_pct"]), 1),
                "VIX贴水": bool(r["vix"] > r["vix3m"]),
            },
        }
    return {
        "as_of": str(as_of.date()),
        "generated_at": dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M 北京时间"),
        "generated_epoch": int(dt.datetime.now(CST).timestamp() * 1000),
        "monthly_plan": MONTHLY_PLAN,
        "monthly_total": sum(p["amount"] for p in MONTHLY_PLAN.values()),
        "assets": assets,
    }


# ---------- 网页 ----------
def render_html(status):
    data_json = json.dumps(status, ensure_ascii=False)
    return HTML_TEMPLATE.replace("/*DATA*/", data_json)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3600">
<title>美股定投热度 · 今日状态</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--mut:#8b949e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:-apple-system,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.5}
.wrap{max-width:920px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.plan{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:20px;font-size:14px}
.plan b{color:#58a6ff}
.asset{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:0;margin-bottom:18px;overflow:hidden}
.banner{padding:18px 20px;color:#fff}
.banner .name{font-size:13px;opacity:.85;letter-spacing:1px}
.banner .state{font-size:26px;font-weight:700;margin-top:2px}
.banner .action{font-size:14px;margin-top:8px;opacity:.97}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--line)}
.m{background:var(--card);padding:12px 14px}
.m .k{color:var(--mut);font-size:12px}
.m .v{font-size:17px;font-weight:600;margin-top:2px}
.foot{color:var(--mut);font-size:12px;margin-top:8px}
.legend{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;font-size:13px;color:var(--mut)}
.legend b{color:var(--txt)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.warn{color:#f0b400}
a{color:#58a6ff}
</style>
</head>
<body>
<div class="wrap">
  <h1>美股定投热度 · 今日状态</h1>
  <div class="sub" id="sub"></div>
  <div class="plan" id="plan"></div>
  <div id="assets"></div>
  <div class="legend">
    <b>状态说明</b><br>
    <span class="dot" style="background:#1a8f4c"></span><b>普通-满仓</b>:每月按计划定投,不做其他操作。<br>
    <span class="dot" style="background:#1f6feb"></span><b>极冷-满仓</b>:热度分位≤10,历史级便宜,定投照常,有闲钱可额外加仓。<br>
    <span class="dot" style="background:#c0392b"></span><b>风险现金</b>:估值双高+趋势破位已确认,维持约20%现金;定投仍照常买入,等待买回。<br><br>
    <b>触发逻辑</b>:进入风险现金需「CAPE分位≥90 且 巴菲特分位≥90 且 (跌破200日均线 或 回撤>10%)」连续10个交易日;
    买回需「热度<30 或 恐慌贴水 或 估值退出双高 或 站回200均线连续10天」任一成立。
    <div class="foot" style="margin-top:10px">本页为量化方法的教育性展示,不构成投资建议。信号用 C1 价格调整日频口径计算。历史表现不代表未来收益。</div>
  </div>
</div>
<script>
const DATA = /*DATA*/;
document.getElementById('sub').textContent = '数据截至 ' + DATA.as_of + ' · 生成于 ' + DATA.generated_at;
(function(){
  const day = 86400000, nowMs = Date.now();
  // 以东八区(北京时间)为基准计算"距今几天";as_of 视为北京时间当日
  const asOfMs = Date.parse(DATA.as_of + 'T00:00:00+08:00');
  const ageData = Math.floor((nowMs - asOfMs) / day);
  const ageGen = DATA.generated_epoch ? Math.floor((nowMs - DATA.generated_epoch) / day) : -1;
  let msg = '';
  if (ageData >= 1) msg = '行情数据距今 ' + ageData + ' 天(非最新)';
  else if (ageGen >= 1) msg = '页面已 ' + ageGen + ' 天未重新生成,每日任务可能已停摆';
  if (msg){
    const sev = (ageData >= 5 || ageGen >= 5) ? '#c0392b' : '#b7791f';
    const b = document.createElement('div');
    b.style.cssText = 'background:'+sev+';color:#fff;padding:14px 18px;border-radius:12px;font-size:15px;font-weight:600;margin-bottom:16px';
    b.textContent = '⚠️ ' + msg + ' —— 若非周末/长假,请检查 GitHub Actions / 数据源是否正常。';
    const wrap = document.querySelector('.wrap');
    wrap.insertBefore(b, wrap.firstChild);
  }
})();
const plan = Object.entries(DATA.monthly_plan).map(([k,v])=>`${k} <b>$${v.amount.toLocaleString()}</b> → ${v.etf}`).join(' ＋ ');
document.getElementById('plan').innerHTML = '📅 每月第一个交易日定投(共 <b>$'+DATA.monthly_total.toLocaleString()+'</b>):' + plan + ' 。任何状态下这笔定投都照常执行。';
const box = document.getElementById('assets');
for(const [name,a] of Object.entries(DATA.assets)){
  const mh = Object.entries(a.metrics).map(([k,v])=>{
    let disp = (typeof v==='boolean')? (v?'是':'否') : v;
    return `<div class="m"><div class="k">${k}</div><div class="v">${disp}</div></div>`;
  }).join('');
  box.insertAdjacentHTML('beforeend',
    `<div class="asset">
       <div class="banner" style="background:${a.color}">
         <div class="name">${name}</div>
         <div class="state">${a.state_zh}</div>
         <div class="action">${a.action}</div>
         <div class="foot" style="color:#fff;opacity:.8">当前状态已持续 ${a.days_in_state} 个交易日</div>
       </div>
       <div class="metrics">${mh}</div>
     </div>`);
}
</script>
</body>
</html>
"""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log("== 更新月频宏观数据(FRED / CAPE) ==")
    try:
        import fetch_data
        fetch_data.update_macro(CACHE, log)
    except Exception as e:
        log("  月频更新整体失败,全部用旧缓存:", repr(e)[:100])
    log("== 更新行情 ==")
    update_cache()
    log("== 计算信号 ==")
    status = build_status()
    (ROOT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "index.html").write_text(render_html(status), encoding="utf-8")
    log("已写出 status.json 与 index.html。今日状态:")
    for k, a in status["assets"].items():
        log(f"  {k}: {a['state_zh']}  热度{a['metrics']['热度分位']}  CAPE{a['metrics']['CAPE分位']}  巴菲特{a['metrics']['巴菲特分位']}")


if __name__ == "__main__":
    main()
