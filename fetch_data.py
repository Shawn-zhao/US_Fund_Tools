#!/usr/bin/env python3
"""月频/宏观数据自动抓取器。作者:Anthropic Claude Opus 4.8。

被 daily_status.py 调用,把可靠来源的慢变量自动更新到缓存(格式与 v25 加载函数完全兼容)。
每个抓取器都是"尽力而为":失败只记录、保留旧缓存,绝不写坏文件。

能自动抓(可靠):
  - FRED CPIAUCSL  -> fred_CPIAUCSL.csv         (CPI,月度)
  - BEA 季度 GDP 发布历史 -> gdp_bea_release_vintages.csv (带实际发布日期和值)
  - FRED BAMLH0A0HYM2 -> fred_BAMLH0A0HYM2.csv (HY 利差,日度,按日合并)
  - FRED DGS10     -> fred_DGS10.csv            (10年期名义利率,日度;= H.15 RIFLGFCY10_N.B)
  - FRED DFII10    -> fred_DFII10.csv           (10年期 TIPS 实际利率,日度;= H.15 RIFLGFCY10_XII_N.B)
  - multpl Shiller PE -> multpl_shiller_pe.csv  (CAPE,月度;HTML 抓取,较脆,失败回退缓存)

每日状态任务另调用 update_market_indicators,补齐日频市场广度和 Put/Call。
美联储 H.15 利率 h15_all.zip 仍是手工维护的兜底源。
"""
from __future__ import annotations
import csv, json, math, re, io, os, time, random, datetime as dt, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gdp_vintages import BEA_GDP_URL, GDP_RELEASES, parse_bea, read_vintages, vintage_csv

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 重要:fred.stlouisfed.org 会掐断带浏览器 UA 的请求(连接建立后不返回数据,最终超时);
# 用非浏览器 UA 才能拿到 fredgraph.csv。曾经因为统一用 UA(浏览器) 导致 FRED 全线抓取失败。
UA_PLAIN = "US_Fund_Tools/1.0 (daily-status; +https://github.com/shawn-zhao/US_Fund_Tools)"
BREADTH_URL = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv"
CBOE_DAILY_URL = "https://www.cboe.com/markets/us/options/market-statistics/daily"
BREADTH_LIVE = Path("live/market_breadth_200dma.csv")
PUT_CALL_LIVE = Path("live/cboe_total_put_call.csv")
HY_SERIES = "BAMLH0A0HYM2"
HY_FRED = "fred_BAMLH0A0HYM2.csv"
HY_MACROTRENDS = "macrotrends_high_yield_spread_D.json"
HY_MACROTRENDS_BRIDGE = "hy_oas_macrotrends_bridge.csv"
HY_FRED_ARCHIVE = "github_eco_archive_BAMLH0A0HYM2.csv"
BARCHART_BREADTH = "barchart_s5th_breadth.csv"
BREADTH_ARCHIVE = "breadth_daily.csv"
BREADTH_SOURCE = "Market Breadth Analysis"
# 日本时间 2026-11-23 上午时，最近一个已收盘的美股交易日。
BREADTH_ARCHIVE_AFTER = dt.date(2026, 11, 20)


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


# ---------- 日频广度和期权情绪 ----------
def _read_observations(path: Path, date_col: str, value_col: str) -> dict[dt.date, float]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {dt.date.fromisoformat(row[date_col]): float(row[value_col]) for row in csv.DictReader(f)}


def _save_observations(path: Path, saved: dict[dt.date, float], new: dict[dt.date, float]) -> None:
    additions = new.keys() - saved.keys()
    if additions:
        saved.update((day, new[day]) for day in additions)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "date,value\n" + "".join(f"{day},{saved[day]:.8g}\n" for day in sorted(saved))
        _atomic_write(path, body.encode("utf-8"))


def breadth_archive_dir() -> Path | None:
    path = os.environ.get("MARKET_BREADTH_ARCHIVE_DIR")
    return Path(path) if path else None


def _append_breadth_archive(path: Path, observations: dict[dt.date, float], fetched_at: str) -> int:
    fields = ("date", "value", "source", "fetched_at_utc")
    saved = {}
    exists = path.exists()
    if exists:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != list(fields):
                raise ValueError("市场广度存档的 CSV 表头不匹配")
            saved = {dt.date.fromisoformat(row["date"]): row for row in reader}
    additions = observations.keys() - saved.keys()
    if additions or not exists:
        for day in additions:
            saved[day] = {"date": day.isoformat(), "value": f"{observations[day]:.8g}",
                          "source": BREADTH_SOURCE, "fetched_at_utc": fetched_at}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(saved[day] for day in sorted(saved))
        _atomic_write(path, buffer.getvalue().encode("utf-8"))
    return len(additions)


def _fetch_breadth() -> tuple[dict[dt.date, float], set[dt.date]]:
    raw = _get(BREADTH_URL, timeout=12, ua=UA_PLAIN).decode("utf-8-sig")
    observations = {}
    sessions = set()
    for row in csv.DictReader(io.StringIO(raw)):
        day = dt.date.fromisoformat(row["Date"])
        if day.weekday() >= 5:
            continue
        sessions.add(day)
        # Raw 是站上各自 200DMA 的比例。200MA 列是该比例再平滑后的值。
        if not row["Breadth_Index_Raw"] or not row["Coverage_200"]:
            continue
        share, coverage = float(row["Breadth_Index_Raw"]), float(row["Coverage_200"])
        if 0 <= share <= 1 and 0.95 <= coverage <= 1:
            observations[day] = share * 100
    if not observations:
        raise ValueError("广度来源没有通过范围和覆盖率校验的记录")
    return observations, sessions


def _fetch_cboe_put_call(day: dt.date) -> float:
    url = CBOE_DAILY_URL + "?dt=" + day.isoformat()
    html = _get(url, timeout=8, retries=2, ua=UA_PLAIN).decode("utf-8")
    selected = re.search(r'\\"selectedDate\\":\\"(\d{4}-\d{2}-\d{2})\\"', html)
    if selected is None or selected[1] != day.isoformat():
        raise ValueError("Cboe 返回日期与请求日期不一致")
    ratio = re.search(r'<td\b[^>]*>\s*TOTAL PUT/CALL RATIO\s*</td>\s*'
                      r'<td\b[^>]*>\s*(\d+(?:\.\d+)?)\s*</td>', html)
    if ratio is None or float(ratio[1]) <= 0:
        raise ValueError("Cboe 未发布有效的总 Put/Call 比率")
    return float(ratio[1])


def update_market_indicators(cache, manual, trading_days, log=print):
    """按交易日追加缺口,把原始历史与每日下载分开保留。"""
    trading_days = set(trading_days)
    if not trading_days:
        log("  市场广度/Put/Call：无行情日期，保留旧缓存")
        return
    last = max(trading_days)
    try:
        breadth, available_sessions = _fetch_breadth()
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        # 日报行情缓存缺少某天时,也可用广度源列出的 SPY 交易日补取 Cboe。
        trading_days.update(day for day in available_sessions if day <= last)
        original = _read_observations(cache / BARCHART_BREADTH, "date", "breadth")
        archive = breadth_archive_dir()
        if archive is not None:
            extension = archive / BARCHART_BREADTH
            if extension.exists():
                original.update(_read_observations(extension, "date", "breadth"))
            new_daily = {day: value for day, value in breadth.items() if BREADTH_ARCHIVE_AFTER < day <= last}
            archived = _append_breadth_archive(archive / BREADTH_ARCHIVE, new_daily, fetched_at)
            log(f"  市场广度私有存档：新增 {archived} 天")
        path = cache / BREADTH_LIVE
        saved = _read_observations(path, "date", "value") if path.exists() else {}
        first = max(original)
        preview_last = last if archive is None else min(last, BREADTH_ARCHIVE_AFTER)
        new = {day: value for day, value in breadth.items() if first < day <= preview_last}
        count = len(new.keys() - saved.keys())
        _save_observations(path, saved, new)
        log(f"  市场广度补缺缓存：新增 {count} 天，至 {max(saved.keys() | original.keys())}")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, csv.Error) as exc:
        log(f"  市场广度更新失败，保留旧缓存：{exc}")

    try:
        original = _read_observations(manual / "total_put_call.csv", "Date", "Value")
        path = cache / PUT_CALL_LIVE
        saved = _read_observations(path, "date", "value") if path.exists() else {}
        # 每批先抓最近的交易日,并留出名额补旧缺口。失败的日期下一次继续尝试。
        first = max(original)
        missing = sorted(day for day in trading_days - saved.keys() if day > first)
        batch = list(dict.fromkeys(missing[-10:][::-1] + missing[:120]))

        def fetch(day):
            try:
                return day, _fetch_cboe_put_call(day)
            except (OSError, RuntimeError, ValueError) as exc:
                log(f"  Cboe {day} 更新失败：{exc}")
                return day, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            new = {day: value for day, value in pool.map(fetch, batch) if value is not None}
        _save_observations(path, saved, new)
        log(f"  Put/Call(Cboe)：新增 {len(new)} 天，至 {max(saved.keys() | original.keys())}，待补 {len(missing) - len(new)} 天")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, csv.Error) as exc:
        log(f"  Put/Call 更新失败，保留旧缓存：{exc}")


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


def update_gdp_releases(cache: Path, log):
    try:
        path = cache / GDP_RELEASES
        saved = read_vintages(path) if path.exists() else {}
        incoming = parse_bea(_get(BEA_GDP_URL, timeout=15, retries=2))
        additions = incoming.keys() - saved.keys()
        # 已存发布记录保留原始下载值，点时序列不会因 BEA 重写历史工作簿而变化。
        if additions:
            saved.update((key, incoming[key]) for key in additions)
            _atomic_write(path, vintage_csv(saved))
        log(f"  BEA 名义 GDP 发布记录至 {max(saved)[0]}（新增 {len(additions)} 条）")
    except Exception as e:
        log(f"  BEA 名义 GDP 更新失败，保留已存发布记录 [{e}]")


def _parse_hy_csv(raw: bytes) -> dict[dt.date, float]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames is None or HY_SERIES not in reader.fieldnames:
        raise ValueError("HY 利差 CSV 表头不匹配")
    date_field = reader.fieldnames[0]
    if date_field.lower() not in {"date", "observation_date"}:
        raise ValueError("HY 利差 CSV 缺少日期列")
    values = {}
    for row in reader:
        text = row[HY_SERIES]
        if text is None or text.strip() in {"", "."}:
            continue
        day, value = dt.date.fromisoformat(row[date_field]), float(text)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"HY 利差值异常: {day} / {text}")
        values[day] = value
    return values


def load_hy_observations(cache: Path) -> dict[dt.date, float]:
    """读固定历史底稿与后续 FRED 缓存。较新的同系列来源优先。"""
    values = {}
    for filename in (HY_FRED_ARCHIVE, HY_MACROTRENDS_BRIDGE):
        path = cache / filename
        if path.exists():
            values.update(_parse_hy_csv(path.read_bytes()))
    history = cache / HY_MACROTRENDS
    if history.exists():
        raw = json.loads(history.read_text(encoding="utf-8"))
        for stamp, value in raw["data"]:
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                date = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).date()
                values[date] = float(value)
    fred = cache / HY_FRED
    if fred.exists():
        values.update(_parse_hy_csv(fred.read_bytes()))
    return values


def update_hy_oas(cache: Path, log):
    """FRED 的滚动窗口只更新重叠日期及新日期，过去的记录保留。"""
    try:
        path = cache / HY_FRED
        saved = _parse_hy_csv(path.read_bytes()) if path.exists() else {}
        incoming = _parse_hy_csv(fetch_fred_csv(HY_SERIES))
        if not incoming:
            raise ValueError("FRED 返回空的 HY 利差数据")
        merged = saved | incoming
        if merged != saved:
            body = f"observation_date,{HY_SERIES}\n" + "".join(
                f"{date},{merged[date]}\n" for date in sorted(merged)
            )
            _atomic_write(path, body.encode("utf-8"))
        log(f"  HY 利差 FRED 缓存 {len(merged)} 点（{min(merged)} 至 {max(merged)}）")
    except Exception as e:
        log(f"  HY 利差更新失败，保留旧缓存 [{e}]")


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
    update_fred(cache, "GDP", "fred_GDP.csv", log)  # 兼容独立回测的最新修订值缓存
    update_gdp_releases(cache, log)
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
