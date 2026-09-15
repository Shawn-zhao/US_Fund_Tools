#!/usr/bin/env python3
"""月频/宏观数据自动抓取器。作者:Anthropic Claude Opus 4.8。

被 daily_status.py 调用,把可靠来源的慢变量自动更新到缓存(格式与 v25 加载函数完全兼容)。
每个抓取器都是"尽力而为":失败只记录、保留旧缓存,绝不写坏文件。

能自动抓(可靠):
  - FRED CPIAUCSL  -> fred_CPIAUCSL.csv         (CPI,月度)
  - FRED GDP       -> fred_GDP.csv              (GDP,季度)
  - FRED BAMLH0A0HYM2 -> macrotrends_high_yield_spread_D.json  (HY 利差,日度;转成 load_hy_oas 的 json 格式)
  - FRED DGS10     -> fred_DGS10.csv            (10年期名义利率,日度;= H.15 RIFLGFCY10_N.B)
  - FRED DFII10    -> fred_DFII10.csv           (10年期 TIPS 实际利率,日度;= H.15 RIFLGFCY10_XII_N.B)
  - multpl Shiller PE -> multpl_shiller_pe.csv  (CAPE,月度;HTML 抓取,较脆,失败回退缓存)

仍需手动/单独维护(无稳定免费源,见文末说明):
  - barchart 广度 barchart_s5th_breadth.csv
  - CBOE Put/Call(data_manual/total_put_call.csv)
  - 美联储 H.15 利率 h15_all.zip(已降级为 10年期利率的兜底源,见 load_rate_series)
这些权重低、变化慢,偶尔手动刷新即可;但停更过久会让对应用分量整列断供
(见 daily_status.py 的 freshcheck),需靠日志/页面告警发现。
"""
from __future__ import annotations
import json, re, io, time, random, datetime as dt, urllib.request, urllib.parse
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 重要:fred.stlouisfed.org 会掐断带浏览器 UA 的请求(连接建立后不返回数据,最终超时);
# 用非浏览器 UA 才能拿到 fredgraph.csv。曾经因为统一用 UA(浏览器) 导致 FRED 全线抓取失败。
UA_PLAIN = "US_Fund_Tools/1.0 (daily-status; +https://github.com/shawn-zhao/US_Fund_Tools)"


def _get(url: str, timeout: int = 15, retries: int = 3, ua: str | None = None) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua or UA, "Accept": "text/csv,application/json,text/html,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 15) + random.uniform(0, 1))
    raise last


def _atomic_write(path: Path, data: bytes):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


# ---------- FRED:fredgraph.csv,无需 API key ----------
def fetch_fred_csv(series_id: str) -> bytes:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series_id
    raw = _get(url, ua=UA_PLAIN)   # 必须用非浏览器 UA,否则 FRED 不返回数据
    # 校验:第一行应是表头且含 series_id,至少有几行数据
    head = raw[:200].decode("utf-8", "ignore")
    if series_id not in head or b"\n" not in raw[:10_000]:
        raise ValueError(f"FRED 返回异常:{head[:80]}")
    return raw


def update_fred(cache: Path, series_id: str, fname: str, log):
    try:
        raw = fetch_fred_csv(series_id)
        _atomic_write(cache / fname, raw)
        last = raw.decode("utf-8", "ignore").strip().splitlines()[-1].split(",")[0]
        log(f"  FRED {series_id} -> {fname}(至 {last})")
    except Exception as e:
        log(f"  FRED {series_id} 失败,保留旧缓存 [{repr(e)[:70]}]")


def update_hy_oas(cache: Path, log):
    """HY 利差:从 FRED BAMLH0A0HYM2 抓日度,转成 load_hy_oas 读的 json 格式。"""
    try:
        raw = fetch_fred_csv("BAMLH0A0HYM2").decode("utf-8", "ignore")
        data = []
        for line in raw.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2 or parts[1] in (".", ""):
                continue
            try:
                ts = int(dt.datetime.strptime(parts[0], "%Y-%m-%d")
                         .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                data.append([ts, float(parts[1])])
            except ValueError:
                continue
        if len(data) < 100:
            raise ValueError("有效行过少")
        out = {"data": data, "metadata": {"source": "FRED BAMLH0A0HYM2",
                                          "updated": dt.date.today().isoformat()}}
        _atomic_write(cache / "macrotrends_high_yield_spread_D.json",
                      json.dumps(out).encode("utf-8"))
        log(f"  HY 利差 BAMLH0A0HYM2 -> json({len(data)} 点,至 {parts[0]})")
    except Exception as e:
        log(f"  HY 利差失败,保留旧缓存 [{repr(e)[:70]}]")


# ---------- CAPE:multpl Shiller PE 月表(HTML 抓取,较脆) ----------
def update_cape_multpl(cache: Path, log):
    try:
        html = _get("https://www.multpl.com/shiller-pe/table/by-month").decode("utf-8", "ignore")
        m = re.search(r'<table[^>]*id="datatable"[^>]*>(.*?)</table>', html, re.S) \
            or re.search(r"<table[^>]*>(.*?)</table>", html, re.S)
        if not m:
            raise ValueError("未找到表格")
        pairs = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", m.group(1), re.S)
        best = {}  # 月初日期 -> (是否day==1, 值);按月去重,优先月初值,匹配缓存约定
        for d_raw, v_raw in pairs:
            d_str = re.sub(r"<[^>]+>", "", d_raw).strip()
            # 先去 HTML 实体(如 &#x2002; 含数字2002,会污染取值),再取小数
            v_txt = re.sub(r"&#?\w+;", " ", re.sub(r"<[^>]+>", " ", v_raw))
            mnum = re.search(r"-?\d+(?:\.\d+)?", v_txt)
            if not mnum:
                continue
            try:
                d = dt.datetime.strptime(d_str, "%b %d, %Y")
            except ValueError:
                continue
            key = d.replace(day=1).strftime("%Y-%m-%d")
            is1 = (d.day == 1)
            if key not in best or (is1 and not best[key][0]):
                best[key] = (is1, float(mnum.group()))
        recs = sorted((k, v) for k, (_, v) in best.items())
        if len(recs) < 100:
            raise ValueError(f"解析到 {len(recs)} 行,过少")
        body = "date,cape\n" + "\n".join(f"{d},{v}" for d, v in recs)
        _atomic_write(cache / "multpl_shiller_pe.csv", body.encode("utf-8"))
        log(f"  CAPE(multpl)-> multpl_shiller_pe.csv({len(recs)} 行,至 {recs[-1][0]})")
    except Exception as e:
        log(f"  CAPE(multpl)失败,保留旧缓存 [{repr(e)[:70]}]")



def update_macro(cache: Path, log=print):
    update_fred(cache, "CPIAUCSL", "fred_CPIAUCSL.csv", log)
    update_fred(cache, "GDP", "fred_GDP.csv", log)
    update_fred(cache, "BAMLH0A0HYM2", "fred_BAMLH0A0HYM2.csv", log)
    # 10年期利率:ECY(实际利率)依赖它;停更会让整条信号链冻结在"上一个月"。
    # FRED DGS10/DFII10 即 H.15 的 RIFLGFCY10_N.B / RIFLGFCY10_XII_N.B,逐点同值。
    update_fred(cache, "DGS10", "fred_DGS10.csv", log)
    update_fred(cache, "DFII10", "fred_DFII10.csv", log)
    update_hy_oas(cache, log)
    update_cape_multpl(cache, log)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = Path(__file__).resolve().parent
    update_macro(root / "backtest_outputs" / "cache", print)
