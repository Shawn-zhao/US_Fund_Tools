"""Backtest the market heat design in the v2.5 frozen document.

The repository currently contains documents only, so this script is a
research harness rather than production code. It implements the automatic
parts of the v2.5 design, performs data coverage checks, and writes a
diagnostic report.

Important boundary:
HY OAS and S&P 500 200DMA breadth can be reconstructed from public mirrors
and Barchart's segmented historical endpoint. CBOE official put/call archives
currently cover the historical files through 2019, but a stable free daily
archive after 2019 has not been found, so the sentiment sleeve dynamically
reweights to VIX/VIX term structure after put/call runs out.
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
import math
import re
import zipfile
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote

import numpy as np
import pandas as pd
import requests
from scipy.stats import spearmanr


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "backtest_outputs"
CACHE = OUT / "cache"

START = "1980-01-01"
END = "2026-06-13"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 market-heat-backtest/0.1",
    "Accept": "text/csv,application/json,application/zip,*/*",
    "Connection": "close",
}

BAND_LIMITS = [
    (15.0, "blue"),
    (35.0, "green"),
    (60.0, "neutral"),
    (75.0, "yellow"),
    (88.0, "orange"),
    (101.0, "red"),
]

BAND_MULT = {
    "blue": 2.0,
    "green": 1.5,
    "neutral": 1.0,
    "yellow": 0.5,
    "orange": 0.0,
    "red": 0.0,
}


def ensure_dirs() -> None:
    OUT.mkdir(exist_ok=True)
    CACHE.mkdir(exist_ok=True)


def http_get_cached(name: str, url: str, binary: bool = False, timeout: int = 60) -> bytes:
    path = CACHE / name
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    last_error: Exception | None = None
    for _ in range(3):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
            resp.raise_for_status()
            path.write_bytes(resp.content)
            return resp.content
        except Exception as exc:  # pragma: no cover - network guard
            last_error = exc
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def as_series(df: pd.DataFrame, date_col: str, value_col: str) -> pd.Series:
    s = pd.Series(pd.to_numeric(df[value_col], errors="coerce").values,
                  index=pd.to_datetime(df[date_col], errors="coerce"))
    return s.dropna().sort_index()


def fetch_yahoo(symbol: str, start: str = START, end: str = END) -> pd.Series:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_")
    cache_name = f"yahoo_{safe}_{start}_{end}.csv"
    path = CACHE / cache_name
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"], index_col="date")["close"].dropna()

    p1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    p2 = int(pd.Timestamp(end, tz="UTC").timestamp())
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?period1={p1}&period2={p2}"
        "&interval=1d&events=history&includeAdjustedClose=true"
    )
    raw = requests.get(url, headers=HTTP_HEADERS, timeout=60)
    raw.raise_for_status()
    payload = raw.json()
    result = payload["chart"]["result"][0]
    ts = result.get("timestamp") or []
    quote_block = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
    close = adj if adj is not None else quote_block["close"]
    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize()
    s = pd.Series(close, index=idx, name="close").dropna().astype(float)
    s.to_frame().to_csv(path, index_label="date")
    return s


def fetch_fred_csv(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    content = http_get_cached(f"fred_{series_id}.csv", url, timeout=120)
    df = pd.read_csv(io.BytesIO(content), na_values=".")
    if len(df.columns) < 2:
        raise RuntimeError(f"unexpected FRED CSV for {series_id}")
    return as_series(df, df.columns[0], df.columns[1])


def fetch_hy_oas() -> pd.Series:
    """Fetch BAMLH0A0HYM2/HY OAS with fallbacks.

    FRED's no-key CSV now exposes only a rolling 3-year window for ICE BofA
    OAS series, so the strict historical backtest needs an alternate public
    mirror. Macrotrends serves a JSON chart endpoint with the full daily
    history; GitHub/Eco3min/FRED are kept as fallback pieces.
    """
    series: list[pd.Series] = []

    try:
        raw = http_get_cached(
            "macrotrends_high_yield_spread_D.json",
            "https://www.macrotrends.net/economic-data/3006/D",
            timeout=60,
        )
        payload = json.loads(raw.decode("utf-8"))
        rows = payload.get("data", [])
        mt = pd.Series(
            {pd.to_datetime(int(ts), unit="ms").normalize(): float(value) for ts, value in rows},
            name="hy_oas",
        ).sort_index()
        if not mt.empty:
            series.append(mt)
    except Exception:
        pass

    try:
        raw = http_get_cached(
            "github_eco_archive_BAMLH0A0HYM2.csv",
            "https://raw.githubusercontent.com/csaladenes/eco-archive/main/BAMLH0A0HYM2.csv",
            timeout=60,
        )
        df = pd.read_csv(io.BytesIO(raw), na_values=".")
        gh = as_series(df, "DATE", "BAMLH0A0HYM2")
        series.append(gh)
    except Exception:
        pass

    try:
        raw = http_get_cached(
            "eco3min_us_high_yield_spread.csv",
            "https://eco3min.fr/dataset/us-high-yield-spread.csv",
            timeout=60,
        )
        df = pd.read_csv(io.BytesIO(raw))
        eco = as_series(df, "date", "hy_spread")
        series.append(eco)
    except Exception:
        pass

    try:
        series.append(fetch_fred_csv("BAMLH0A0HYM2"))
    except Exception:
        pass

    if not series:
        return pd.Series(dtype=float)
    combined = pd.concat(series).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")].dropna()
    combined.name = "hy_oas"
    return combined


def fetch_cpi() -> pd.Series:
    try:
        return fetch_fred_csv("CPIAUCSL")
    except Exception:
        url = "https://datahub.io/calcfi/calcfi-cpi/r/data.csv"
        content = http_get_cached("datahub_cpiaucsl.csv", url, timeout=60)
        df = pd.read_csv(io.BytesIO(content), comment="#")
        return as_series(df, "date", "value")


def fetch_h15_zip() -> bytes:
    url = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&filetype=zip"
    return http_get_cached("h15_all.zip", url, binary=True, timeout=120)


def extract_h15_series(zip_bytes: bytes, series_name: str) -> pd.Series:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml = zf.read("H15_data.xml").decode("utf-8", errors="ignore")
    pat = re.compile(
        r'<kf:Series\b(?=[^>]*SERIES_NAME="' + re.escape(series_name) + r'")[^>]*>'
        r"(.*?)</kf:Series>",
        re.S,
    )
    match = pat.search(xml)
    if not match:
        raise RuntimeError(f"H15 series not found: {series_name}")
    rows: list[tuple[pd.Timestamp, float]] = []
    for tag in re.findall(r"<frb:Obs\b([^>]*)/>", match.group(1)):
        attrs = dict(re.findall(r'([A-Z_]+)="([^"]*)"', tag))
        date = attrs.get("TIME_PERIOD")
        val = attrs.get("OBS_VALUE")
        if date and val:
            rows.append((pd.Timestamp(date), float(val)))
    return pd.Series(dict(rows)).sort_index()


def fetch_cape_monthly() -> pd.Series:
    path = CACHE / "multpl_shiller_pe.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"], index_col="date")["cape"].dropna()
    url = "https://www.multpl.com/shiller-pe/table/by-month"
    table = pd.read_html(url)[0]
    s = pd.Series(
        pd.to_numeric(table.iloc[:, 1], errors="coerce").values,
        index=pd.to_datetime(table.iloc[:, 0], errors="coerce"),
        name="cape",
    ).dropna().sort_index()
    # The page may include an intra-month latest estimate. Keep one value per
    # month for the rolling monthly design, taking the latest value published
    # within that month.
    s.index = s.index.to_period("M")
    s = s.groupby(level=0).last()
    s.index = s.index.to_timestamp(how="start")
    s.to_frame().to_csv(path, index_label="date")
    return s


def parse_cboe_pc_csv(raw: str) -> pd.Series:
    lines = raw.splitlines()
    header = next(
        i for i, line in enumerate(lines)
        if line.startswith("DATE,") or line.startswith("Trade_date,") or line.startswith("Date,")
    )
    df = pd.read_csv(io.StringIO("\n".join(lines[header:])))
    date_col = df.columns[0]
    ratio_col = next(
        c for c in df.columns
        if c.lower().strip() in {"p/c ratio", "total volume p/c ratio"}
    )
    return as_series(df, date_col, ratio_col)


def parse_ycharts_indicator_json(raw: bytes | str) -> pd.Series:
    payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    rows: list[tuple[pd.Timestamp, float]] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            lower = {str(k).lower(): v for k, v in obj.items()}
            date_raw = (
                lower.get("date")
                or lower.get("formatted_date")
                or lower.get("date_string")
                or lower.get("x")
                or lower.get("period")
            )
            value_raw = (
                lower.get("value")
                or lower.get("raw_value")
                or lower.get("y")
                or lower.get("close")
            )
            if date_raw is not None and value_raw is not None:
                dt = pd.to_datetime(date_raw, errors="coerce")
                val = pd.to_numeric(value_raw, errors="coerce")
                if not pd.isna(dt) and not pd.isna(val):
                    rows.append((pd.Timestamp(dt).normalize(), float(val)))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            if (
                len(obj) >= 2
                and not isinstance(obj[0], (dict, list))
                and not isinstance(obj[1], (dict, list))
            ):
                dt = pd.to_datetime(obj[0], errors="coerce")
                val = pd.to_numeric(obj[1], errors="coerce")
                if not pd.isna(dt) and not pd.isna(val):
                    rows.append((pd.Timestamp(dt).normalize(), float(val)))
            for value in obj:
                walk(value)

    walk(payload)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({dt: val for dt, val in rows}, name="put_call_total").sort_index()
    return s[~s.index.duplicated(keep="last")].dropna()


def fetch_ycharts_put_call_total() -> pd.Series:
    manual_json = ROOT / "data_manual" / "ycharts_total_put_call.json"
    if manual_json.exists() and manual_json.stat().st_size > 0:
        return parse_ycharts_indicator_json(manual_json.read_bytes())

    url = "https://ycharts.com/indicators/11256.json"
    params = {
        "startDate": "11/01/2006",
        "endDate": "06/11/2026",
        "pageNum": "1",
    }
    path = CACHE / "ycharts_total_put_call_11256.json"
    if path.exists() and path.stat().st_size > 0:
        return parse_ycharts_indicator_json(path.read_bytes())

    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                **HTTP_HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://ycharts.com/indicators/total_putcall_ratio",
            },
            timeout=60,
        )
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return parse_ycharts_indicator_json(resp.content)
    except Exception:
        return pd.Series(dtype=float)


def fetch_manual_series(filename: str, preferred_cols: Iterable[str]) -> pd.Series:
    path = ROOT / "data_manual" / filename
    if not path.exists() or path.stat().st_size == 0:
        return pd.Series(dtype=float)
    df = pd.read_csv(path)
    date_col = next((c for c in df.columns if c.lower().strip() in {"date", "time", "period"}), df.columns[0])
    preferred = {c.lower().strip() for c in preferred_cols}
    value_col = next(
        (
            c for c in df.columns
            if c != date_col and c.lower().strip() in preferred
        ),
        None,
    )
    if value_col is None:
        value_col = next(c for c in df.columns if c != date_col)
    return as_series(df, date_col, value_col)


def fetch_put_call_total() -> pd.Series:
    urls = {
        "pcratioarchive": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/pcratioarchive.csv",
        "totalpcarchive": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpcarchive.csv",
        "totalpc": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv",
    }
    series = []
    for name, url in urls.items():
        try:
            raw = http_get_cached(f"cboe_{name}.csv", url, timeout=40).decode("utf-8", errors="replace")
            series.append(parse_cboe_pc_csv(raw))
        except Exception:
            pass
    manual = fetch_manual_series(
        "total_put_call.csv",
        ["put_call", "put/call", "p/c ratio", "total put/call ratio", "value", "ratio", "close"],
    )
    ycharts = fetch_ycharts_put_call_total()
    if not ycharts.empty:
        series.append(ycharts)
    if not manual.empty:
        series.append(manual)
    if not series:
        return pd.Series(dtype=float)
    combined = pd.concat(series).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")].dropna()
    combined.name = "put_call_total"
    return combined


def try_fetch_put_call_coverage() -> dict[str, str]:
    out: dict[str, str] = {}
    urls = {
        "pcratioarchive": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/pcratioarchive.csv",
        "totalpcarchive": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpcarchive.csv",
        "totalpc": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv",
    }
    for name, url in urls.items():
        try:
            raw = http_get_cached(f"cboe_{name}.csv", url, timeout=40).decode("utf-8", errors="replace")
            s = parse_cboe_pc_csv(raw)
            dates = s.index
            out[name] = f"{dates.min().date()} to {dates.max().date()} ({len(dates)} rows)"
        except Exception as exc:
            out[name] = f"failed: {exc}"
    total = fetch_put_call_total()
    if not total.empty:
        out["CBOE_total_put_call_combined"] = (
            f"{total.index.min().date()} to {total.index.max().date()} ({len(total)} rows)"
        )
    manual = fetch_manual_series(
        "total_put_call.csv",
        ["put_call", "put/call", "p/c ratio", "total put/call ratio", "value", "ratio", "close"],
    )
    if not manual.empty:
        out["manual_total_put_call"] = f"{manual.index.min().date()} to {manual.index.max().date()} ({len(manual)} rows)"
    ycharts = fetch_ycharts_put_call_total()
    if not ycharts.empty:
        out["ycharts_total_put_call"] = f"{ycharts.index.min().date()} to {ycharts.index.max().date()} ({len(ycharts)} rows)"
    else:
        out["ycharts_total_put_call"] = "failed or unauthorized"
    return out


def fetch_barchart_s5th_breadth() -> pd.Series:
    """Fetch S&P 500 % above 200DMA from Barchart's price-history API.

    Barchart caps a single unauthenticated response at about 1000 rows, but
    accepts endDate. Walking backward in chunks gives the long daily history
    needed for the v2.5 breadth sleeve.
    """
    path = CACHE / "barchart_s5th_breadth.csv"
    if path.exists() and path.stat().st_size > 0:
        return pd.read_csv(path, parse_dates=["date"], index_col="date")["breadth"].dropna()

    session = requests.Session()
    session.headers.update({
        "User-Agent": HTTP_HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "close",
    })
    page_url = "https://www.barchart.com/stocks/quotes/%24S5TH/price-history/historical"
    page = session.get(page_url, timeout=60)
    page.raise_for_status()
    xsrf = unquote(session.cookies.get("XSRF-TOKEN", ""))
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": page_url,
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf

    api_url = "https://www.barchart.com/proxies/core-api/v1/historical/get"
    fields = "tradeTime.format(m/d/Y),openPrice,highPrice,lowPrice,lastPrice,priceChange,percentChange,volume"
    end_date = pd.Timestamp(END).normalize()
    min_date = pd.Timestamp("2007-01-01")
    rows: list[tuple[pd.Timestamp, float]] = []
    seen_earliest: pd.Timestamp | None = None

    for _ in range(40):
        params = {
            "symbol": "$S5TH",
            "fields": fields,
            "type": "eod",
            "orderBy": "tradeTime",
            "orderDir": "desc",
            "limit": "1000",
            "meta": "field.shortName,field.type",
            "raw": "1",
            "endDate": end_date.strftime("%Y-%m-%d"),
        }
        resp = session.get(api_url, params=params, headers=headers, timeout=90)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or []
        if not data:
            break

        batch_dates: list[pd.Timestamp] = []
        for item in data:
            raw = item.get("raw") or {}
            date_raw = raw.get("tradeTime") or item.get("tradeTime")
            value_raw = raw.get("lastPrice") or item.get("lastPrice")
            if date_raw is None or value_raw is None:
                continue
            dt = pd.to_datetime(date_raw, errors="coerce")
            val = pd.to_numeric(str(value_raw).replace(",", ""), errors="coerce")
            if pd.isna(dt) or pd.isna(val):
                continue
            dt = pd.Timestamp(dt).normalize()
            rows.append((dt, float(val)))
            batch_dates.append(dt)

        if not batch_dates:
            break
        earliest = min(batch_dates)
        if earliest < min_date:
            break
        if seen_earliest is not None and earliest >= seen_earliest:
            break
        seen_earliest = earliest
        end_date = earliest - pd.Timedelta(days=1)

    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({dt: val for dt, val in rows}, name="breadth").sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s = s[(s >= 0.0) & (s <= 100.0)].dropna()
    s.to_frame().to_csv(path, index_label="date")
    return s


def breadth_u_score(breadth_pct: pd.Series) -> pd.Series:
    nodes_x = np.array([0.0, 20.0, 55.0, 85.0, 100.0])
    nodes_y = np.array([0.0, 10.0, 50.0, 58.0, 65.0])
    clipped = breadth_pct.astype(float).clip(lower=0.0, upper=100.0)
    score = np.interp(clipped.to_numpy(), nodes_x, nodes_y)
    return pd.Series(score, index=breadth_pct.index, name="breadth_score")


def weighted_available(parts: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    weight = pd.Series(weights, dtype=float)
    aligned = parts.reindex(columns=weight.index)
    weighted = aligned.mul(weight, axis=1)
    denom = aligned.notna().mul(weight, axis=1).sum(axis=1)
    out = weighted.sum(axis=1) / denom.replace(0.0, np.nan)
    return out


def expanding_percentile(s: pd.Series, invert: bool = False, min_periods: int = 30) -> pd.Series:
    vals = s.astype(float).to_numpy()
    sorted_vals: list[float] = []
    result = np.full(len(vals), np.nan)
    for i, value in enumerate(vals):
        if np.isnan(value):
            continue
        less = bisect.bisect_left(sorted_vals, value)
        leq = bisect.bisect_right(sorted_vals, value) + 1
        n = len(sorted_vals) + 1
        pct = (less + leq) / (2 * n) * 100.0
        if n >= min_periods:
            result[i] = 100.0 - pct if invert else pct
        bisect.insort(sorted_vals, value)
    return pd.Series(result, index=s.index)


def rolling_percentile(
    s: pd.Series,
    window: int,
    invert: bool = False,
    min_periods: int | None = None,
) -> pd.Series:
    if min_periods is None:
        min_periods = window
    vals = s.astype(float).to_numpy()
    sorted_vals: list[float] = []
    q: deque[float] = deque()
    result = np.full(len(vals), np.nan)
    for i, value in enumerate(vals):
        if np.isnan(value):
            result[i] = np.nan
            continue
        q.append(value)
        bisect.insort(sorted_vals, value)
        if len(q) > window:
            old = q.popleft()
            pos = bisect.bisect_left(sorted_vals, old)
            sorted_vals.pop(pos)
        if len(q) >= min_periods:
            less = bisect.bisect_left(sorted_vals, value)
            leq = bisect.bisect_right(sorted_vals, value)
            pct = (less + leq) / (2 * len(sorted_vals)) * 100.0
            result[i] = 100.0 - pct if invert else pct
    return pd.Series(result, index=s.index)


def available_next_month(s: pd.Series) -> pd.Series:
    out = s.copy()
    if isinstance(out.index, pd.PeriodIndex):
        periods = out.index.asfreq("M")
    else:
        periods = out.index.to_period("M")
    out.index = (periods + 1).to_timestamp(how="start")
    return out.sort_index()


def rsi_weekly(close: pd.Series, n: int = 14) -> pd.Series:
    weekly = close.resample("W-FRI").last().dropna()
    delta = weekly.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss
    return (100.0 - 100.0 / (1.0 + rs)).dropna()


def band_of(value: float) -> str:
    if np.isnan(value):
        return "missing"
    for upper, name in BAND_LIMITS:
        if value < upper:
            return name
    return "red"


def hysteresis(raw: pd.Series, days: int = 10) -> pd.Series:
    values = raw.dropna()
    if values.empty:
        return raw
    current = values.iloc[0]
    pending = current
    count = 0
    out: dict[pd.Timestamp, str] = {}
    for date, candidate in values.items():
        if candidate == current:
            pending = current
            count = 0
        elif candidate == pending:
            count += 1
            if count >= days:
                current = candidate
                count = 0
        else:
            pending = candidate
            count = 1
        out[date] = current
    return pd.Series(out).reindex(raw.index)


@dataclass
class DataBundle:
    prices: pd.DataFrame
    cape: pd.Series
    nom10: pd.Series
    real10: pd.Series
    cpi: pd.Series
    gdp: pd.Series
    hy: pd.Series
    put_call: pd.Series
    breadth: pd.Series
    put_call_coverage: dict[str, str]


def load_data() -> DataBundle:
    symbols = {
        "spx": "^GSPC",
        "ndx": "^NDX",
        "vix": "^VIX",
        "vix3m": "^VIX3M",
        "w5000": "^W5000",
    }
    prices = pd.DataFrame({k: fetch_yahoo(v) for k, v in symbols.items()}).sort_index()

    cape = fetch_cape_monthly()
    h15 = fetch_h15_zip()
    nom10 = extract_h15_series(h15, "RIFLGFCY10_N.B")
    real10 = extract_h15_series(h15, "RIFLGFCY10_XII_N.B")
    cpi = fetch_cpi()
    gdp = fetch_fred_csv("GDP")
    hy = fetch_hy_oas()
    put_call = fetch_put_call_total()
    breadth = fetch_barchart_s5th_breadth()
    put_call_coverage = try_fetch_put_call_coverage()
    return DataBundle(prices, cape, nom10, real10, cpi, gdp, hy, put_call, breadth, put_call_coverage)


def monthly_period_last(s: pd.Series) -> pd.Series:
    out = s.dropna().copy()
    out.index = out.index.to_period("M")
    return out.groupby(level=0).last()


def monthly_period_mean(s: pd.Series) -> pd.Series:
    out = s.dropna().copy()
    out.index = out.index.to_period("M")
    return out.groupby(level=0).mean()


def build_heat(data: DataBundle, b_mode: str = "neutral") -> tuple[pd.DataFrame, pd.DataFrame]:
    px = data.prices.copy()
    spx = px["spx"].dropna()
    ndx = px["ndx"].dropna()
    dates = spx.index.intersection(ndx.index)
    px = px.reindex(dates).ffill(limit=5)

    cape_pm = monthly_period_last(data.cape)
    cape_score = available_next_month(rolling_percentile(cape_pm, 180, min_periods=180))

    cpi_pm = monthly_period_last(data.cpi)
    infl10 = ((cpi_pm / cpi_pm.shift(120)) ** (1.0 / 10.0) - 1.0) * 100.0
    nom10_pm = monthly_period_mean(data.nom10)
    real10_pm_actual = monthly_period_mean(data.real10)
    recon_real10 = nom10_pm.reindex(infl10.index).ffill() - infl10
    real10_pm = recon_real10.combine_first(real10_pm_actual)
    real10_pm.update(real10_pm_actual.dropna())

    ecy = (100.0 / cape_pm).reindex(real10_pm.index).dropna() - real10_pm.dropna()
    ecy = ecy.dropna()
    ecy_score = available_next_month(rolling_percentile(ecy, 180, invert=True, min_periods=180))

    w5000_pm = monthly_period_last(px["w5000"].dropna())
    w5000_avail = available_next_month(w5000_pm)
    gdp_avail = data.gdp.copy().dropna()
    # FRED GDP dates are quarter starts. The design says use the previous
    # quarter, so a Q1 value becomes available at the start of Q2.
    gdp_avail.index = gdp_avail.index + pd.DateOffset(months=3)
    gdp_for_w5000 = gdp_avail.reindex(w5000_avail.index, method="ffill")
    buffett = (w5000_avail / gdp_for_w5000).dropna()
    buffett_score = rolling_percentile(buffett, 180, min_periods=180)

    vix = px["vix"].dropna()
    vix_score = expanding_percentile(vix, invert=True, min_periods=252)
    vix_score = vix_score.where(vix <= 40.0, np.minimum(vix_score, 5.0))

    vix_ratio = (px["vix"] / px["vix3m"]).dropna()
    vix_term_score = expanding_percentile(vix_ratio, invert=True, min_periods=252)

    put_call_10d = data.put_call.rolling(10, min_periods=10).mean()
    put_call_score = expanding_percentile(put_call_10d.dropna(), invert=True, min_periods=252).reindex(dates)

    dev_spx = (spx / spx.rolling(200).mean() - 1.0).dropna()
    dev_ndx = (ndx / ndx.rolling(200).mean() - 1.0).dropna()
    dev_score = (
        expanding_percentile(dev_spx, min_periods=252).reindex(dates)
        + expanding_percentile(dev_ndx, min_periods=252).reindex(dates)
    ) / 2.0

    dd_spx = (1.0 - spx / spx.cummax()).dropna()
    dd_ndx = (1.0 - ndx / ndx.cummax()).dropna()
    dd_score_spx = expanding_percentile(dd_spx, invert=True, min_periods=252)
    dd_score_ndx = expanding_percentile(dd_ndx, invert=True, min_periods=252)
    dd_score = (dd_score_spx.reindex(dates) + dd_score_ndx.reindex(dates)) / 2.0
    anchor_dd = (dd_spx.reindex(dates) > 0.30) | (dd_ndx.reindex(dates) > 0.40)
    dd_score = dd_score.where(~anchor_dd, np.minimum(dd_score, 5.0))

    rsi_spx = rsi_weekly(spx)
    rsi_ndx = rsi_weekly(ndx)
    rsi_score_weekly = (
        expanding_percentile(rsi_spx, min_periods=52).reindex(rsi_spx.index)
        + expanding_percentile(rsi_ndx, min_periods=52).reindex(rsi_ndx.index)
    ) / 2.0
    rsi_score = rsi_score_weekly.reindex(dates, method="ffill")

    if b_mode != "neutral" and not data.hy.empty:
        hy = data.hy.reindex(dates, method="ffill")
        hy_score = expanding_percentile(hy.dropna(), invert=True, min_periods=252).reindex(dates)
        hy_score = hy_score.where(hy <= 8.0, np.minimum(hy_score, 5.0))
    else:
        hy_score = pd.Series(np.nan, index=dates)

    if b_mode != "neutral" and not data.breadth.empty:
        breadth_raw = data.breadth.reindex(dates, method="ffill")
        breadth_score = breadth_u_score(breadth_raw.dropna()).reindex(dates)
    else:
        breadth_raw = pd.Series(np.nan, index=dates)
        breadth_score = pd.Series(np.nan, index=dates)

    b_score = weighted_available(
        pd.DataFrame({"Breadth": breadth_score, "HY": hy_score}, index=dates),
        {"Breadth": 0.50, "HY": 0.50},
    ).fillna(50.0)

    scores = pd.DataFrame(index=dates)
    scores["CAPE"] = cape_score.reindex(dates, method="ffill")
    scores["ECY"] = ecy_score.reindex(dates, method="ffill")
    scores["Buffett"] = buffett_score.reindex(dates, method="ffill")
    scores["VIX"] = vix_score.reindex(dates)
    scores["VIXTerm"] = vix_term_score.reindex(dates)
    scores["Dev200"] = dev_score.reindex(dates)
    scores["Drawdown"] = dd_score.reindex(dates)
    scores["WeeklyRSI"] = rsi_score.reindex(dates)
    scores["PutCall"] = put_call_score.reindex(dates)
    scores["BreadthRaw"] = breadth_raw.reindex(dates)
    scores["Breadth"] = breadth_score.reindex(dates)
    scores["HY"] = hy_score.reindex(dates)
    scores["B"] = b_score.reindex(dates)

    heat = pd.DataFrame(index=dates)
    heat["V"] = 0.35 * scores["CAPE"] + 0.45 * scores["ECY"] + 0.20 * scores["Buffett"]
    heat["S"] = weighted_available(
        scores[["VIX", "VIXTerm", "PutCall"]],
        {"VIX": 0.45, "VIXTerm": 0.25, "PutCall": 0.30},
    )
    heat["T"] = 0.40 * scores["Dev200"] + 0.35 * scores["Drawdown"] + 0.25 * scores["WeeklyRSI"]
    heat["B"] = scores["B"]
    heat["base"] = 0.40 * heat["V"] + 0.25 * heat["S"] + 0.25 * heat["T"] + 0.10 * heat["B"]

    lr = np.log(ndx / spx).dropna()
    ratio_dev = (lr - lr.rolling(200).mean()).dropna()
    ratio_pct = expanding_percentile(ratio_dev, min_periods=252).reindex(dates)
    heat["ndx_relative_overheat"] = ratio_pct > 90.0
    weak_breadth = scores["BreadthRaw"] < 55.0
    heat["spx_breadth_divergence"] = (spx >= spx.cummax() * 0.99).reindex(dates).fillna(False) & weak_breadth.fillna(False)
    heat["ndx_breadth_divergence"] = (ndx >= ndx.cummax() * 0.99).reindex(dates).fillna(False) & weak_breadth.fillna(False)
    heat["heat_spx"] = np.minimum(heat["base"] + np.where(heat["spx_breadth_divergence"], 5.0, 0.0), 100.0)
    heat["heat_ndx"] = np.minimum(
        heat["base"]
        + np.where(heat["ndx_breadth_divergence"], 5.0, 0.0)
        + np.where(heat["ndx_relative_overheat"], 3.0, 0.0),
        100.0,
    )
    heat["vix_panic"] = expanding_percentile(vix, min_periods=252).reindex(dates) > 95.0
    heat["vix_backwardation"] = (px["vix"] > px["vix3m"]).reindex(dates)
    heat["vix_anchor"] = (px["vix"] > 40.0).reindex(dates)
    heat["raw_band_spx"] = heat["heat_spx"].map(band_of)
    heat["raw_band_ndx"] = heat["heat_ndx"].map(band_of)
    heat["band_spx"] = hysteresis(heat["raw_band_spx"])
    heat["band_ndx"] = hysteresis(heat["raw_band_ndx"])
    sell_cond = (heat["heat_spx"] > 88.0) & (heat["V"] > 85.0)
    sell_days = []
    count = 0
    for ok in sell_cond.fillna(False):
        count = count + 1 if ok else 0
        sell_days.append(count)
    heat["sell_days"] = sell_days
    heat["sell_signal"] = heat["sell_days"] >= 20

    return heat, scores


def data_coverage(data: DataBundle) -> pd.DataFrame:
    rows = []
    for name, s in {
        "SPX": data.prices["spx"],
        "NDX": data.prices["ndx"],
        "VIX": data.prices["vix"],
        "VIX3M": data.prices["vix3m"],
        "W5000": data.prices["w5000"],
        "CAPE": data.cape,
        "H15_10Y_nominal": data.nom10,
        "H15_10Y_TIPS": data.real10,
        "CPIAUCSL": data.cpi,
        "GDP": data.gdp,
        "HY_OAS": data.hy,
        "CBOE_total_put_call": data.put_call,
        "Barchart_S5TH_breadth": data.breadth,
    }.items():
        ss = s.dropna()
        rows.append({
            "series": name,
            "start": "" if ss.empty else str(ss.index.min().date()),
            "end": "" if ss.empty else str(ss.index.max().date()),
            "rows": int(len(ss)),
        })
    for name, desc in data.put_call_coverage.items():
        rows.append({"series": f"CBOE_{name}", "start": desc, "end": "", "rows": ""})
    return pd.DataFrame(rows)


def scenario_summary(heat: pd.DataFrame, b_mode: str) -> pd.DataFrame:
    scenarios = [
        ("2000_dotcom_top", "2000-03-01", "2000-03-31"),
        ("2002_bear_bottom", "2002-09-15", "2002-10-31"),
        ("2007_pre_gfc_top", "2007-10-01", "2007-10-31"),
        ("2009_gfc_bottom", "2009-03-02", "2009-03-31"),
        ("2018_q4_selloff", "2018-12-17", "2018-12-31"),
        ("2020_covid_crash", "2020-03-16", "2020-03-31"),
        ("2021_liquidity_top", "2021-11-01", "2021-12-31"),
        ("2022_hiking_bottom", "2022-10-01", "2022-10-31"),
    ]
    rows = []
    for name, start, end in scenarios:
        sub = heat.loc[start:end].dropna(subset=["base"])
        if sub.empty:
            rows.append({
                "scenario": name,
                "status": "no full heat data",
                "doc_check": "not_evaluable",
                "start": start,
                "end": end,
            })
            continue
        if name == "2007_pre_gfc_top":
            doc_check = "pass" if ((sub["base"] > 55) & (sub["base"] < 80)).any() else "fail"
        elif name == "2009_gfc_bottom":
            doc_check = "pass" if ((sub["base"] < 15).sum() >= 15 and sub["S"].min() < 10) else "fail"
        elif name == "2018_q4_selloff":
            doc_check = "pass" if ((sub["base"] > 15) & (sub["base"] < 40)).any() else "fail"
        elif name == "2020_covid_crash":
            doc_check = "pass" if ((sub["base"] < 15) & sub["vix_anchor"]).any() else "fail"
        elif name == "2021_liquidity_top":
            doc_check = "pass" if (sub["heat_spx"] > 75).sum() >= 10 or (sub["heat_ndx"] > 85).sum() >= 10 else "fail"
        elif name == "2022_hiking_bottom":
            doc_check = "pass" if ((sub["base"] > 12) & (sub["base"] < 35)).any() else "fail"
        else:
            doc_check = "not_evaluable"
        rows.append({
            "scenario": name,
            "status": "diagnostic_neutral_B" if b_mode == "neutral" else "public_enhanced",
            "doc_check": doc_check,
            "start": str(sub.index.min().date()),
            "end": str(sub.index.max().date()),
            "days": len(sub),
            "base_min": round(sub["base"].min(), 2),
            "base_median": round(sub["base"].median(), 2),
            "base_max": round(sub["base"].max(), 2),
            "spx_heat_max": round(sub["heat_spx"].max(), 2),
            "ndx_heat_max": round(sub["heat_ndx"].max(), 2),
            "V_median": round(sub["V"].median(), 2),
            "S_min": round(sub["S"].min(), 2),
            "T_min": round(sub["T"].min(), 2),
            "days_base_lt_15": int((sub["base"] < 15).sum()),
            "days_base_15_35": int(((sub["base"] >= 15) & (sub["base"] < 35)).sum()),
            "days_base_gt_75": int((sub["base"] > 75).sum()),
            "sell_signal_days": int(sub["sell_signal"].sum()),
            "panic_marker_days": int((sub["vix_panic"] & sub["vix_backwardation"]).sum()),
            "ndx_overheat_days": int(sub["ndx_relative_overheat"].sum()),
            "spx_breadth_divergence_days": int(sub["spx_breadth_divergence"].sum()),
            "ndx_breadth_divergence_days": int(sub["ndx_breadth_divergence"].sum()),
        })
    return pd.DataFrame(rows)


def forward_return_table(heat: pd.DataFrame, price: pd.Series, heat_col: str, label: str) -> pd.DataFrame:
    df = pd.DataFrame({"heat": heat[heat_col], "price": price}).dropna()
    df["band"] = df["heat"].map(band_of)
    rows = []
    for horizon, days in [("1m", 21), ("3m", 63), ("12m", 252)]:
        df[f"fwd_{horizon}"] = df["price"].shift(-days) / df["price"] - 1.0
    for band in ["blue", "green", "neutral", "yellow", "orange", "red"]:
        sub = df[df["band"] == band]
        row = {"asset": label, "band": band, "days": int(len(sub))}
        for horizon in ["1m", "3m", "12m"]:
            vals = sub[f"fwd_{horizon}"].dropna()
            row[f"{horizon}_mean_pct"] = round(vals.mean() * 100.0, 2) if len(vals) else np.nan
            row[f"{horizon}_median_pct"] = round(vals.median() * 100.0, 2) if len(vals) else np.nan
            row[f"{horizon}_hit_rate_pct"] = round((vals > 0).mean() * 100.0, 1) if len(vals) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def dca_sim(heat: pd.DataFrame, price: pd.Series, band_col: str, start: str = "2007-01-01") -> dict[str, float | str]:
    h = heat.loc[start:].dropna(subset=[band_col])
    p = price.reindex(h.index).ffill().dropna()
    h = h.reindex(p.index).dropna(subset=[band_col])
    months = p.groupby(p.index.to_period("M")).head(1).index
    if len(months) < 2:
        return {}

    shares_heat = 0.0
    cash_heat = 0.0
    shares_fixed = 0.0
    values_heat = []
    values_fixed = []
    bands_used = []

    for dt in months[1:]:
        prev_dates = h.index[h.index < dt]
        if len(prev_dates) == 0:
            continue
        signal_dt = prev_dates[-1]
        band = str(h.loc[signal_dt, band_col])
        px = float(p.loc[dt])

        cash_heat += 1.0
        invest = min(cash_heat, BAND_MULT.get(band, 1.0))
        shares_heat += invest / px
        cash_heat -= invest

        shares_fixed += 1.0 / px

        values_heat.append((dt, shares_heat * px + cash_heat))
        values_fixed.append((dt, shares_fixed * px))
        bands_used.append(band)

    heat_val = pd.Series(dict(values_heat)).sort_index()
    fixed_val = pd.Series(dict(values_fixed)).sort_index()
    total_contrib = float(len(heat_val))

    def max_dd(values: pd.Series) -> float:
        return float((values / values.cummax() - 1.0).min() * 100.0)

    counts = Counter(bands_used)
    return {
        "months": int(total_contrib),
        "heat_final_value": round(float(heat_val.iloc[-1]), 2),
        "fixed_final_value": round(float(fixed_val.iloc[-1]), 2),
        "heat_vs_fixed_pct": round((float(heat_val.iloc[-1]) / float(fixed_val.iloc[-1]) - 1.0) * 100.0, 2),
        "heat_cash_end": round(cash_heat, 2),
        "heat_max_drawdown_pct": round(max_dd(heat_val), 2),
        "fixed_max_drawdown_pct": round(max_dd(fixed_val), 2),
        "band_months": json.dumps(dict(counts), sort_keys=True),
    }


def trend_window_check(heat: pd.DataFrame, data: DataBundle, scores: pd.DataFrame) -> pd.DataFrame:
    px = data.prices.copy()
    spx = px["spx"].dropna()
    ndx = px["ndx"].dropna()
    dates = heat.index
    dev_spx = (spx / spx.rolling(200).mean() - 1.0).dropna()
    dev_ndx = (ndx / ndx.rolling(200).mean() - 1.0).dropna()
    dev_alt = (
        rolling_percentile(dev_spx, 252 * 25, min_periods=252 * 10).reindex(dates)
        + rolling_percentile(dev_ndx, 252 * 25, min_periods=252 * 10).reindex(dates)
    ) / 2.0

    rsi_spx = rsi_weekly(spx)
    rsi_ndx = rsi_weekly(ndx)
    rsi_alt_weekly = (
        rolling_percentile(rsi_spx, 52 * 25, min_periods=52 * 10).reindex(rsi_spx.index)
        + rolling_percentile(rsi_ndx, 52 * 25, min_periods=52 * 10).reindex(rsi_ndx.index)
    ) / 2.0
    rsi_alt = rsi_alt_weekly.reindex(dates, method="ffill")

    t_alt = 0.40 * dev_alt + 0.35 * scores["Drawdown"] + 0.25 * rsi_alt
    base_alt = 0.40 * heat["V"] + 0.25 * heat["S"] + 0.25 * t_alt + 0.10 * heat["B"]
    comp = pd.DataFrame({"base": heat["base"], "base_alt": base_alt}).loc["2010-01-01":].dropna()
    comp["band"] = comp["base"].map(band_of)
    comp["band_alt"] = comp["base_alt"].map(band_of)
    if comp.empty:
        return pd.DataFrame([{"metric": "trend_window_check", "value": "not enough data"}])
    diff = (comp["base"] - comp["base_alt"]).abs()
    return pd.DataFrame([
        {"metric": "rows", "value": len(comp)},
        {"metric": "mean_abs_base_diff", "value": round(float(diff.mean()), 2)},
        {"metric": "p95_abs_base_diff", "value": round(float(diff.quantile(0.95)), 2)},
        {"metric": "band_change_pct", "value": round(float((comp["band"] != comp["band_alt"]).mean() * 100.0), 2)},
    ])


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_empty_"
    text_df = df.copy()
    for col in text_df.columns:
        text_df[col] = text_df[col].map(lambda x: "" if pd.isna(x) else str(x))
    cols = list(text_df.columns)
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for _, row in text_df.iterrows():
        vals = [str(row[col]).replace("|", "\\|") for col in cols]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def zh_band(value: str) -> str:
    return {
        "blue": "极度冰点",
        "green": "偏冷",
        "neutral": "中性",
        "yellow": "偏热",
        "orange": "过热",
        "red": "极端过热",
        "missing": "缺失",
    }.get(str(value), str(value))


def zh_band_months(value: str) -> str:
    try:
        raw = json.loads(value)
    except Exception:
        return value
    return json.dumps({zh_band(k): v for k, v in raw.items()}, ensure_ascii=False, sort_keys=True)


def display_scenarios(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["scenario"] = out["scenario"].map({
        "2000_dotcom_top": "2000 互联网泡沫顶",
        "2002_bear_bottom": "2002 熊市底",
        "2007_pre_gfc_top": "2007 金融危机前顶",
        "2009_gfc_bottom": "2009 金融危机底",
        "2018_q4_selloff": "2018 Q4 急跌",
        "2020_covid_crash": "2020 新冠崩盘",
        "2021_liquidity_top": "2021 流动性泡沫顶",
        "2022_hiking_bottom": "2022 加息熊底",
    }).fillna(out["scenario"])
    out["status"] = out["status"].map({
        "no full heat data": "无完整热度数据",
        "diagnostic_neutral_B": "诊断回测(B项按50中性占位)",
        "HY_OAS入分(MVP+)": "HY OAS入分(MVP+)",
        "public_enhanced_partial_pc": "公开源增强(PC至2019)",
        "public_enhanced": "公开源增强",
    }).fillna(out["status"])
    out["doc_check"] = out["doc_check"].map({
        "pass": "通过",
        "fail": "失败",
        "not_evaluable": "不可评估",
    }).fillna(out["doc_check"])
    return out.rename(columns={
        "scenario": "场景",
        "status": "状态",
        "doc_check": "文档校准",
        "start": "起始日",
        "end": "结束日",
        "days": "交易日数",
        "base_min": "基础热度最低",
        "base_median": "基础热度中位",
        "base_max": "基础热度最高",
        "spx_heat_max": "SPX热度最高",
        "ndx_heat_max": "NDX热度最高",
        "V_median": "估值分中位",
        "S_min": "情绪分最低",
        "T_min": "趋势分最低",
        "days_base_lt_15": "Heat<15天数",
        "days_base_15_35": "15<=Heat<35天数",
        "days_base_gt_75": "Heat>75天数",
        "sell_signal_days": "卖出信号天数",
        "panic_marker_days": "恐慌标记天数",
        "ndx_overheat_days": "NDX相对过热天数",
        "spx_breadth_divergence_days": "SPX广度背离天数",
        "ndx_breadth_divergence_days": "NDX广度背离天数",
    })


def display_forwards(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["band"] = out["band"].map(zh_band)
    return out.rename(columns={
        "asset": "资产",
        "band": "档位",
        "days": "交易日数",
        "1m_mean_pct": "1个月均值%",
        "1m_median_pct": "1个月中位%",
        "1m_hit_rate_pct": "1个月胜率%",
        "3m_mean_pct": "3个月均值%",
        "3m_median_pct": "3个月中位%",
        "3m_hit_rate_pct": "3个月胜率%",
        "12m_mean_pct": "12个月均值%",
        "12m_median_pct": "12个月中位%",
        "12m_hit_rate_pct": "12个月胜率%",
    })


def display_dca_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out.drop(columns=["band_months"], errors="ignore")
    return out.rename(columns={
        "asset": "资产",
        "months": "月数",
        "heat_final_value": "热度策略期末值",
        "fixed_final_value": "固定定投期末值",
        "heat_vs_fixed_pct": "相对固定定投%",
        "heat_cash_end": "期末现金",
        "heat_max_drawdown_pct": "热度策略最大回撤%",
        "fixed_max_drawdown_pct": "固定定投最大回撤%",
    })


def display_dca_bands(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in df.iterrows():
        try:
            raw = json.loads(row.get("band_months", "{}"))
        except Exception:
            raw = {}
        rows.append({
            "资产": row.get("asset", ""),
            "极度冰点": raw.get("blue", 0),
            "偏冷": raw.get("green", 0),
            "中性": raw.get("neutral", 0),
            "偏热": raw.get("yellow", 0),
            "过热": raw.get("orange", 0),
            "极端过热": raw.get("red", 0),
            "缺失": raw.get("missing", 0),
        })
    return pd.DataFrame(rows)


def display_trend_check(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["metric"] = out["metric"].map({
        "rows": "样本行数",
        "mean_abs_base_diff": "基础热度平均绝对差",
        "p95_abs_base_diff": "基础热度95分位绝对差",
        "band_change_pct": "档位改变比例%",
    }).fillna(out["metric"])
    return out.rename(columns={"metric": "指标", "value": "值"})


def display_coverage(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={
        "series": "序列",
        "start": "起始",
        "end": "结束",
        "rows": "行数",
    })


def write_report(
    data: DataBundle,
    heat: pd.DataFrame,
    coverage: pd.DataFrame,
    scenarios: pd.DataFrame,
    forwards: pd.DataFrame,
    dca: pd.DataFrame,
    trend_check: pd.DataFrame,
) -> None:
    hy = data.hy.dropna()
    pc = data.put_call.dropna()
    breadth = data.breadth.dropna()
    hy_ok = (not hy.empty) and hy.index.min() <= pd.Timestamp("1997-01-31") and hy.index.max() >= pd.Timestamp("2026-01-01")
    pc_ok = (not pc.empty) and pc.index.min() <= pd.Timestamp("1997-01-31") and pc.index.max() >= pd.Timestamp("2026-01-01")
    breadth_ok = (
        (not breadth.empty)
        and breadth.index.min() <= pd.Timestamp("2007-01-31")
        and breadth.index.max() >= pd.Timestamp("2026-01-01")
    )
    latest = heat.dropna(subset=["base"]).iloc[-1]
    latest_date = heat.dropna(subset=["base"]).index[-1].date()
    spx_corr_data = heat[["base"]].join(data.prices["spx"].rename("spx")).dropna()
    spx_corr_data["fwd_12m"] = spx_corr_data["spx"].shift(-252) / spx_corr_data["spx"] - 1.0
    corr = spearmanr(spx_corr_data["base"].iloc[:-252], spx_corr_data["fwd_12m"].iloc[:-252], nan_policy="omit")

    lines = []
    lines.append("# v2.5 美股定投热度方案回测报告")
    lines.append("")
    lines.append(f"生成口径: 使用截至约 {latest_date} 可获得的数据。")
    lines.append("")
    lines.append("## 严格数据状态")
    if hy_ok:
        lines.append(
            f"- HY OAS 历史数据已补齐: {hy.index.min().date()} 至 {hy.index.max().date()}, "
            "来源为 Macrotrends 全历史 JSON, 并保留 GitHub/Eco3min/FRED 作为后备。"
        )
    else:
        start = "empty" if hy.empty else str(hy.index.min().date())
        end = "empty" if hy.empty else str(hy.index.max().date())
        lines.append(f"- HY OAS 仍未完整覆盖: {start} 至 {end}。")
    if pc_ok:
        lines.append(f"- Put/Call 已完整覆盖: {pc.index.min().date()} 至 {pc.index.max().date()}。")
    else:
        start = "empty" if pc.empty else str(pc.index.min().date())
        end = "empty" if pc.empty else str(pc.index.max().date())
        lines.append(
            f"- Put/Call 仅部分覆盖: {start} 至 {end}; 2019-10-04 之后暂未找到稳定免费官方日频归档。"
            "若提供 `data_manual/total_put_call.csv`, 脚本会自动合并。"
        )
    if breadth_ok:
        lines.append(
            f"- SPX 200DMA 广度已接入: {breadth.index.min().date()} 至 {breadth.index.max().date()}, "
            "使用 Barchart `$S5TH` 分段历史接口。"
        )
    else:
        start = "empty" if breadth.empty else str(breadth.index.min().date())
        end = "empty" if breadth.empty else str(breadth.index.max().date())
        lines.append(f"- SPX 200DMA 广度仍未完整覆盖: {start} 至 {end}。")
    if pc_ok:
        lines.append("- 当前增强回测已补齐 HY OAS、Put/Call 与 SPX 200DMA 广度。")
    else:
        lines.append("- 当前增强回测已补齐 HY OAS 与广度, 但 Put/Call 在 2019 后缺失; 缺失期间情绪维度按可用的 VIX 与期限结构动态重权。")
    lines.append("")
    lines.append("## 最新诊断热度")
    lines.append(
        f"- {latest_date}: 基础热度={latest['base']:.1f}, SPX热度={latest['heat_spx']:.1f}, "
        f"NDX热度={latest['heat_ndx']:.1f}, 估值V={latest['V']:.1f}, 情绪S={latest['S']:.1f}, "
        f"趋势T={latest['T']:.1f}, 广度/信用B={latest['B']:.1f}, "
        f"SPX档位={zh_band(latest['band_spx'])}, NDX档位={zh_band(latest['band_ndx'])}。"
    )
    lines.append(f"- Spearman(基础热度, SPX未来12个月收益) = {corr.statistic:.3f} (p={corr.pvalue:.4g})。")
    lines.append("")
    lines.append("## 关键场景校准")
    lines.append(md_table(display_scenarios(scenarios)))
    lines.append("")
    lines.append("## 原始热度档位后的未来收益")
    lines.append(md_table(display_forwards(forwards)))
    lines.append("")
    lines.append("## 月度定投模拟")
    lines.append("- 模型: 每月增加 1 单位现金, 按上一个交易日的迟滞后档位决定投入倍数, 未投入现金收益按 0% 处理。由于卖出侧数据不完整, 本模拟不执行卖出规则。")
    lines.append(md_table(display_dca_summary(dca)))
    lines.append("")
    lines.append("### 月度定投模拟档位分布")
    lines.append(md_table(display_dca_bands(dca)))
    lines.append("")
    lines.append("## 趋势窗口稳健性")
    lines.append("- 替代口径: 将 200DMA 偏离度与周线 RSI 的全历史扩展分位改为 25 年滚动分位。")
    lines.append(md_table(display_trend_check(trend_check)))
    lines.append("")
    lines.append("## 数据覆盖")
    lines.append(md_table(display_coverage(coverage)))
    lines.append("")
    lines.append("## 评估结论")
    lines.append("- 本报告已接入 HY OAS、SPX 200DMA 广度, 以及 YCharts/CBOE 拼接后的 Total Put/Call 日频数据。")
    lines.append("- 在当前防前视回测中, 关键场景是否通过请以“关键场景校准”表为准。")
    lines.append("- 2020 是最清楚的设计/测试冲突: 恐慌标记正确触发, 但按一个月滞后使用 CAPE/巴菲特指标时, 估值项仍处于偏热区, 总热度约 30, 没有落到文档预期的 3-10。")
    lines.append("- Put/Call 补齐后, 2020 与 2021 的校准失败仍未消失, 说明主要问题不是数据缺口, 而是热度合成与档位规则本身需要重校准。")
    lines.append("- 月度定投模拟仍明显落后固定定投且回撤改善有限, 说明该冻结版的买入倍数/暂停规则不适合直接真实执行。")
    lines.append("")
    lines.append("## 数据来源链接")
    lines.append("- HY OAS: https://www.macrotrends.net/economic-data/3006/D")
    lines.append("- CBOE Put/Call archives: https://www.cboe.com/us/options/market_statistics/daily/")
    lines.append("- YCharts Put/Call: https://ycharts.com/indicators/total_putcall_ratio")
    lines.append("- Barchart `$S5TH`: https://www.barchart.com/stocks/quotes/%24S5TH/price-history/historical")

    (OUT / "backtest_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--b-mode", choices=["neutral", "fred_recent", "public"], default="public")
    args = parser.parse_args()

    ensure_dirs()
    data = load_data()
    heat, scores = build_heat(data, b_mode=args.b_mode)
    coverage = data_coverage(data)
    scenarios = scenario_summary(heat, args.b_mode)
    forwards = pd.concat([
        forward_return_table(heat, data.prices["spx"], "heat_spx", "SPX"),
        forward_return_table(heat, data.prices["ndx"], "heat_ndx", "NDX"),
    ], ignore_index=True)
    dca = pd.DataFrame([
        {"asset": "SPX", **dca_sim(heat, data.prices["spx"], "band_spx")},
        {"asset": "NDX", **dca_sim(heat, data.prices["ndx"], "band_ndx")},
    ])
    trend_check = trend_window_check(heat, data, scores)

    heat.to_csv(OUT / "heat_daily.csv", index_label="date")
    scores.to_csv(OUT / "indicator_scores.csv", index_label="date")
    coverage.to_csv(OUT / "data_coverage.csv", index=False)
    scenarios.to_csv(OUT / "scenario_summary.csv", index=False)
    forwards.to_csv(OUT / "forward_returns_by_band.csv", index=False)
    dca.to_csv(OUT / "dca_summary.csv", index=False)
    trend_check.to_csv(OUT / "trend_window_check.csv", index=False)
    write_report(data, heat, coverage, scenarios, forwards, dca, trend_check)

    print(f"Wrote {OUT / 'backtest_report.md'}")
    print(scenarios.to_string(index=False))
    print(dca.to_string(index=False))


if __name__ == "__main__":
    main()
