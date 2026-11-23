#!/usr/bin/env python3
"""每日热度状态引擎 + 网页生成(合并版 v2.6)。作者:Anthropic Claude Opus 4.8。

做三件事:
  1) 更新 Yahoo 日线、宏观、市场广度和 Put/Call 缓存(失败则用现有缓存)。
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
import pandas as pd
from archive_daily_status import latest_published_signals

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


def fetch_yahoo(symbol: str, retries: int = 5, *, since: dt.date | None = None):
    """返回 [(date_str, close), ...] 已收盘日线，从 since 当日继续抓取。
    带指数退避重试,并在 query1/query2 间轮换,缓解 429/限流;全部失败才抛异常。"""
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for attempt in range(retries):
        now = dt.datetime.now(dt.timezone.utc)
        params = {"interval": "1d", "range": "3mo"}
        if since is not None:
            boundary = dt.datetime.combine(since, dt.time.min, tzinfo=dt.timezone.utc)
            params = {"interval": "1d", "period1": int(boundary.timestamp()), "period2": int(now.timestamp())}
        host = hosts[attempt % len(hosts)]
        url = (f"https://{host}/v8/finance/chart/"
               + urllib.parse.quote(symbol) + "?" + urllib.parse.urlencode(params))
        try:
            j = json.loads(http_bytes(url).decode())
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            regular = res.get("meta", {}).get("currentTradingPeriod", {}).get("regular", {})
            pending_session = None
            if regular and now.timestamp() < regular["end"]:
                pending_session = dt.datetime.fromtimestamp(regular["start"], dt.timezone.utc).date().isoformat()
            out = []
            for timestamp, close in zip(ts, closes):
                day = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()
                if close is not None and day != pending_session:
                    out.append((day, float(close)))
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
            rows = fetch_yahoo(YH_SYMBOL[key], since=last.date())
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
    # build_core 只保留两指数共有的日期。把仅一个指数有报价的交易日补回，
    # 让其缺失的信号中断确认计数，也让新鲜度检查能发现最新交易日的缺口。
    spx, ndx = (v25.load_yahoo(YH[key]) for key in ("GSPC", "NDX"))
    df = df.reindex(spx.index.union(ndx.index))
    df["spx"], df["ndx"] = spx, ndx
    df["heat"] = df["heat_pct"]
    # 热度可在分量缺失时继续估算，交易确认仍须两项当天均有原始观测。
    breadth_days = v25.load_breadth().dropna().index
    put_call_days = v25.load_put_call().dropna().index
    df["market_indicators_complete"] = df.index.isin(breadth_days) & df.index.isin(put_call_days)
    # 原始 VIX 分位(高=恐慌)与期限结构
    vix = v25.load_yahoo(YH["VIX"]).reindex(df.index)
    vix3m = v25.load_yahoo(YH["VIX3M"]).reindex(df.index)
    df["vix"] = vix
    df["vix3m"] = vix3m
    df["vix_pct"] = v25.expanding_pct(vix.dropna()).reindex(df.index)
    return df


# ---------- 2b) 数据新鲜度自检 ----------
# 上游输入停更时,信号链会"整列变 NaN"从而把 as_of 冻在过去某一天,而过程完全静默:
#   - reweight() 对缺项会静默按剩余权重归一(例如 Put/Call 停更后 S 只剩 VIX);
#   - daily_in_monthly_window_pct() 的 lag=1 要求"上月"存在于月度序列,月频源停更
#     会让 ECY/CAPE/Buffett 整体断供,进而把 base/heat_pct 一起拖死。
# 这里显式打印每个输入与分量的最后可用日期,并点名停更项,写进 status.json 供页面告警。
def freshcheck(df):
    import backtest_v25_independent_Opus4_8 as v25
    last = df.index.max()
    # (名称, 最后可用日期, 容忍滞后天数)
    inputs = [
        ("行情 SPX(Yahoo)",     df["spx"].dropna().index.max(), 5),
        ("行情 NDX(Yahoo)",     df["ndx"].dropna().index.max(), 5),
        ("VIX(Yahoo)",          v25.load_yahoo(YH["VIX"]).index.max(), 5),
        ("VIX3M(Yahoo)",        v25.load_yahoo(YH["VIX3M"]).index.max(), 5),
        ("HY利差(FRED)",        v25.load_hy_oas().index.max(), 5),
        ("10Y利率(FRED)",       v25.load_rate_series("RIFLGFCY10_N.B").index.max(), 5),
        ("10Y TIPS(FRED)",      v25.load_rate_series("RIFLGFCY10_XII_N.B").index.max(), 5),
        ("CAPE(multpl·月)",     v25.load_cape().index.max(), 60),
        ("CPI(FRED·月)",        v25.load_fred("fred_CPIAUCSL.csv").index.max(), 60),
        ("GDP（BEA 发布）",     v25.load_gdp_released().index.max(), 200),
    ]
    report, stale = {}, []
    log("  -- 输入源最后可用日期(基准 = 最后交易日 %s)--" % last.date())
    for name, d, tol in inputs:
        if d is None or pd.isna(d):
            log(f"     {name:<22} 全缺"); stale.append(name); continue
        age = int((last - d).days)
        report[name] = {"last": str(d.date()), "age_days": age}
        if age > tol:
            stale.append(name)
        log(f"     {name:<22} {d.date()}  落后 {age:>4} 天" + ("   <== 停更" if age > tol else ""))

    for name, series in [("市场广度", v25.load_breadth()), ("Put/Call", v25.load_put_call())]:
        # 以本次用于计算信号的交易日为准,同时暴露最近10个交易日的缺口。
        latest = series.index[series.index <= last].max()
        missing = len(df.index[-10:].difference(series.index))
        report[name] = {"last": str(latest.date()) if pd.notna(latest) else None,
                        "source": series.attrs["source"], "missing_last_10": missing}
        if name == "市场广度" and pd.notna(series.attrs["barchart_last"]):
            report[name]["barchart_last"] = series.attrs["barchart_last"].date().isoformat()
        if missing:
            stale.append(name)
        log(f"     {name:<22} {report[name]['last']}  近10个交易日缺 {missing} 天")

    comps = ["CAPE", "ECY", "Buffett", "V", "VIX", "VIXTerm", "PutCall", "S",
             "Dev200", "Drawdown", "WeeklyRSI", "T", "BreadthU", "HY", "B", "base", "heat_pct"]
    log("  -- 信号分量最后可用日期 --")
    for c in comps:
        s = df[c].dropna()
        if not len(s):
            log(f"     {c:<10} 全缺"); stale.append(c); continue
        d = s.index.max()
        dead = bool(d < last)
        report["分量:" + c] = {"last": str(d.date()), "dead": dead}
        if dead:
            stale.append(c)
        note = ""
        if dead:
            note = "   <== 其后全 NaN(断供)"
            if c in ("CAPE", "ECY", "Buffett", "V", "base", "heat_pct"):
                note += " [会拖住 as_of]"
        log(f"     {c:<10} {d.date()}" + note)
    return report, stale


# ---------- 3) 回放合并版状态机 ----------
def replay(df, price_col):
    """对单个资产回放状态机,返回逐日状态 DataFrame(到今日)。"""
    price = df[price_col]
    ma200 = price.dropna().rolling(200).mean()
    dd = 1 - price / price.cummax()
    sub = df.loc[START:].copy()
    sub["price"] = price.loc[START:]
    sub["ma200"] = ma200.loc[START:]
    sub["dd"] = dd.loc[START:]
    need = ["CAPE", "Buffett", "heat", "vix", "vix3m", "vix_pct", "price", "ma200", "dd"]
    valid = sub[need].notna().all(axis=1)
    if "market_indicators_complete" in sub:
        valid &= sub["market_indicators_complete"].eq(True).fillna(False)

    state = "normal"
    trig = 0          # 连续满足减仓条件天数
    above = 0         # 连续 price>ma200 天数
    days_in = 0
    recs = []
    for (ts, row), has_data in zip(sub.iterrows(), valid):
        if not has_data:
            trig = above = 0
            continue

        cape, buff, heat = row["CAPE"], row["Buffett"], row["heat"]
        trend_break = (row["price"] < row["ma200"]) or (row["dd"] > 0.10)
        above = above + 1 if row["price"] > row["ma200"] else 0
        panic = (row["vix_pct"] > 95) and (row["vix"] > row["vix3m"])
        cold = heat < 30
        derisk_cond = (cape >= 90) and (buff >= 90) and trend_break and not (cold or panic)
        trade_signal = None

        if state == "normal":
            trig = trig + 1 if derisk_cond else 0
            if trig >= CONFIRM_DAYS:
                state, trig, days_in = "risk_cash", 0, 0
                trade_signal = "derisk"
            else:
                days_in += 1
        else:  # risk_cash
            buyback = cold or panic or (cape < 90) or (buff < 90) or (above >= CONFIRM_DAYS)
            if buyback:
                state, days_in = "normal", 0
                trade_signal = "buyback"
            else:
                days_in += 1
        recs.append((ts, state, trig, above, days_in, derisk_cond, panic, trade_signal))
    out = pd.DataFrame(recs, columns=["dt", "state", "trig", "above", "days_in",
                                      "derisk_cond", "panic", "trade_signal"]).set_index("dt")
    return sub.loc[valid], out


def latest_trade_signal(st):
    trades = st["trade_signal"].dropna()
    if trades.empty:
        return None
    signal = {
        "action": trades.iloc[-1],
        "signal_date": trades.index[-1].date().isoformat(),
        "execution_timing": "next_trading_day",
    }
    if signal["action"] == "buyback":
        signal["derisk_signal_date"] = trades.index[-2].date().isoformat()
    return signal


def build_status():
    df = compute_signals()
    market_last = df.index.max()
    market_date = market_last.date().isoformat()
    log("== 数据新鲜度自检 ==")
    dq, stale = freshcheck(df)
    missing_current = [name for name in ("市场广度", "Put/Call") if dq.get(name, {}).get("last") != market_date]
    published = latest_published_signals(market_date, MONTHLY_PLAN)
    assets = {}
    for asset, col in [("SPX", "spx"), ("NDX", "ndx")]:
        sub, st = replay(df, col)
        if st.empty:
            raise ValueError(f"{asset} 没有数据齐全的交易日，无法生成状态")
        ts = st.index.max()
        decision_date = ts.date().isoformat()
        r = sub.loc[ts]
        s = st.loc[ts]
        state = s["state"]
        heat = float(r["heat"])
        cold_extreme = heat <= 10
        plan = MONTHLY_PLAN[asset]
        last_signal = latest_trade_signal(st)
        trade_signal = None
        state_zh = "持有避险现金" if state == "risk_cash" else "照常定投"
        if missing_current:
            state_zh = "仅观察"
            action = (f"{'、'.join(missing_current)} 缺 {market_date} 的观测。"
                      f"最近完整信号截至 {decision_date}，月供照常。")
        elif ts < market_last:
            state_zh = "最新信号缺失"
            action = f"最新完整数据截至 {decision_date}。等数据更新，月供照常。"
        elif s["trade_signal"] == "derisk":
            trade_signal = last_signal
            state_zh = "一次性减仓"
            action = (f"{decision_date} 收盘发出信号。下一交易日把 {plan['etf']} 这一组合"
                      f"一次性调到约 {RISK_CASH_TARGET:.0%} 现金或短债，转出后保留这笔资金。"
                      "若已按同一信号减仓，无需再卖。月供照常。")
        elif s["trade_signal"] == "buyback":
            trade_signal = last_signal
            derisk_date = last_signal["derisk_signal_date"]
            state_zh = "一次性买回"
            action = (f"{decision_date} 收盘发出信号。若执行了 {derisk_date} 的减仓"
                      f"且仍持有本轮避险资金，下一交易日用这笔资金一次性买回 {plan['etf']}。"
                      "未减仓的只需照常定投。")
        elif state == "risk_cash":
            action = "已减仓的保留当次转出的现金或短债，等待买回信号。月供照常买入。"
        elif s["trig"] > 0:
            action = f"减仓条件已连续满足 {int(s['trig'])}/{CONFIRM_DAYS} 个交易日。月供照常。"
        elif cold_extreme:
            action = "热度处于历史极冷区。月供照常。"
        else:
            action = "本次收盘未触发减仓或买回。"
        assets[asset] = {
            "state": state,
            "state_zh": state_zh,
            "color": ("#6e491f" if ts < market_last or missing_current else "#a9362b" if state == "risk_cash"
                      else "#1f6feb" if cold_extreme else "#1a6e3f"),
            "action": action,
            "decision_date": decision_date,
            "trade_signal": trade_signal,
            "last_trade_signal": last_signal,
            "last_published_trade_signal": published.get(asset),
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
    as_of = min(a["decision_date"] for a in assets.values())
    if as_of < market_date:
        log(f"  !! 警告:行情已到 {market_date}，至少一个资产的完整信号只算到 {as_of}。")
    return {
        "as_of": as_of,
        "market_last": market_date,
        "generated_at": dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M 北京时间"),
        "generated_epoch": int(dt.datetime.now(CST).timestamp() * 1000),
        "data_quality": {"stale": stale, "missing_current": missing_current, "items": dq},
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
.signals{padding:10px 20px;font-size:13px;color:var(--mut);border-bottom:1px solid var(--line)}
.signals div + div{margin-top:4px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--line)}
.m{background:var(--card);padding:12px 14px}
.m .k{color:var(--mut);font-size:12px}
.m .v{font-size:17px;font-weight:600;margin-top:2px}
.foot{color:var(--mut);font-size:12px;margin-top:8px}
.legend{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;font-size:13px;color:var(--mut)}
.legend b{color:var(--txt)}
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
    页面显示规则演算结果，实际仓位和成交请查自己的交易记录。漏看时可核对最近信号的日期和成交记录。
    <details>
      <summary>触发条件</summary>
      CAPE 和巴菲特分位均至少为 90，价格跌破 200 日均线或回撤超过 10%，且未进入冷区或恐慌。连续 10 个交易日均满足时，触发一次减仓。
      持有避险现金期间，热度分位低于 30、VIX 分位超过 95 且 VIX 高于 VIX3M、估值退出双高、或价格连续 10 日高于 200 日均线，满足任一项即触发一次买回。广度、Put/Call 等所需数据缺失时，暂停发出交易信号并重新计算连续天数。
    </details>
    <div class="foot" style="margin-top:10px">本页为量化方法的教育性展示，不构成投资建议。估值采用 C1 价格调整口径。历史表现不代表未来收益。</div>
  </div>
</div>
<script>
const DATA = /*DATA*/;
document.getElementById('sub').textContent = '数据截至 ' + DATA.as_of + ' · 生成于 ' + DATA.generated_at;
(function(){
  const day = 86400000, TZ = 8*3600*1000, nowMs = Date.now();
  // 以东八区(北京时间)为基准。as_of 是「上一交易日」,所以不能简单地按"距今≥1天"报警,
  // 否则每周末和每天收盘前都会误报。改为统计 as_of 之后"缺失的周一~周五天数"。
  function missingWeekdays(asOfISO){
    const start = Date.parse(asOfISO + 'T00:00:00+08:00');
    const todayB = Math.floor((nowMs + TZ) / day) * day - TZ;   // 北京时间今天 00:00
    let n = 0;
    for (let t = start + day; t < todayB; t += day){
      const w = new Date(t + TZ).getUTCDay();                   // 该北京日期的星期
      if (w >= 1 && w <= 5) n++;
    }
    return n;
  }
  const marketLast = DATA.market_last || DATA.as_of;
  const ageMarket = Math.floor((nowMs - Date.parse(marketLast + 'T00:00:00+08:00')) / day);
  const ageGen = DATA.generated_epoch ? Math.floor((nowMs - DATA.generated_epoch) / day) : -1;
  const stale = (DATA.data_quality && DATA.data_quality.stale) || [];
  const missing = missingWeekdays(marketLast);
  let msg = '', hint = '';
  if (DATA.as_of < marketLast){
    // 行情抓到了新的,但信号算不出来 —— 只可能是某个上游输入停更
    msg = '行情已更新到 ' + marketLast + '，完整信号只算到 ' + DATA.as_of;
    hint = stale.length ? '停更/断供的输入:' + stale.join('、')
                        : '请查看 Actions 日志里的「数据新鲜度自检」。';
  } else if (missing >= 2){
    msg = '行情数据距今 ' + ageMarket + ' 天,缺 ' + missing + ' 个交易日(非最新)';
    hint = '请检查 GitHub Actions / 行情数据源是否正常。';
  } else if (ageGen >= 2){
    msg = '页面已 ' + ageGen + ' 天未重新生成';
    hint = '每日任务可能已停摆,请检查 GitHub Actions。';
  }
  if (msg){
    const sev = (DATA.as_of < marketLast || missing >= 4 || ageGen >= 4) ? '#c0392b' : '#b7791f';
    const b = document.createElement('div');
    b.style.cssText = 'background:'+sev+';color:#fff;padding:14px 18px;border-radius:12px;font-size:15px;font-weight:600;margin-bottom:16px';
    b.textContent = '⚠️ ' + msg + ' —— ' + hint;
    const wrap = document.querySelector('.wrap');
    wrap.insertBefore(b, wrap.firstChild);
  }
})();
const sources = ['市场广度', 'Put/Call'].map(name => {
  const item = DATA.data_quality.items[name];
  const note = document.createElement('div');
  note.textContent = name + ' ' + (item.last || '暂无数据') + '（' + item.source + '）'
    + (item.barchart_last && item.source !== 'Barchart' ? ' · Barchart 历史至 ' + item.barchart_last : '')
    + (item.missing_last_10 ? ' · 近 10 个交易日缺 ' + item.missing_last_10 + ' 天' : '');
  if (item.missing_last_10) note.className = 'warn';
  return note;
});
document.getElementById('sub').append(...sources);
const plan = Object.entries(DATA.monthly_plan).map(([k,v])=>`${k} <b>$${v.amount.toLocaleString()}</b> → ${v.etf}`).join(' ＋ ');
document.getElementById('plan').innerHTML = '每月第一个交易日定投，共 <b>$'+DATA.monthly_total.toLocaleString()+'</b> · ' + plan;
const box = document.getElementById('assets');
function describeSignal(signal){
  if (!signal) return '暂无记录';
  const action = signal.action === 'derisk' ? '一次性减仓' : '一次性买回';
  const entry = signal.derisk_signal_date ? '（对应 ' + signal.derisk_signal_date + ' 减仓信号）' : '';
  return signal.signal_date + ' 收盘 · ' + action + entry;
}
for(const [name,a] of Object.entries(DATA.assets)){
  const published = a.last_published_trade_signal;
  const revised = published && published.signal_date === DATA.market_last
    && a.decision_date === DATA.market_last && !DATA.data_quality.missing_current?.length
    && published.action !== a.trade_signal?.action;
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
       </div>
       <div class="signals">
         ${a.trade_signal ? '' : `<div>本次演算最近信号 · ${describeSignal(a.last_trade_signal)}</div>`}
         <div>此前已发布信号 · ${describeSignal(published)}${revised ? '（本次演算有修订）' : ''}</div>
       </div>
       <div class="metrics">${mh}</div>
     </div>`);
}
</script>
</body>
</html>
"""


def main():
    import backtest_v25_independent_Opus4_8 as v25
    import fetch_data

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log("== 更新月频宏观数据(FRED / CAPE) ==")
    try:
        fetch_data.update_macro(CACHE, log)
    except Exception as e:
        log("  月频更新整体失败,全部用旧缓存:", repr(e)[:100])
    log("== 更新行情 ==")
    update_cache()
    log("== 更新市场广度和 Put/Call ==")
    trading_days = v25.load_yahoo(YH["GSPC"]).index.date
    fetch_data.update_market_indicators(CACHE, ROOT / "data_manual", trading_days, log)
    log("== 计算信号 ==")
    status = build_status()
    (ROOT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "index.html").write_text(render_html(status), encoding="utf-8")
    log("已写出 status.json 与 index.html。今日状态:")
    for k, a in status["assets"].items():
        log(f"  {k}: {a['state_zh']}  热度{a['metrics']['热度分位']}  CAPE{a['metrics']['CAPE分位']}  巴菲特{a['metrics']['巴菲特分位']}")


if __name__ == "__main__":
    main()
