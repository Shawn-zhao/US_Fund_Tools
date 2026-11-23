#!/usr/bin/env python3
"""用生产信号和可投资 ETF 的复权收盘价复核合并版双账户。"""
from __future__ import annotations

import io
import json
import math
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd
from scipy.optimize import brentq

import backtest_v25_independent_Opus4_8 as market
import daily_status as live
from fetch_data import UA_PLAIN, _get

END = None  # None 表示沿用生产指数缓存末日；可指定 YYYY-MM-DD
TRADE_COST_BPS = 5.0  # 每笔 ETF 成交按名义金额计费，5 个基点 = 0.05%
ETF_BY_ASSET = {"SPX": "SPY", "NDX": "QQQ"}
CACHE = Path.home() / ".cache" / "us-fund-tools" / "merged-backtest"
REPORT = Path(__file__).with_name("回测复核报告_v2.6_合并版_实盘口径.md")


def write_csv(data: pd.Series, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        data.to_csv(temp_path, index_label="date")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_csv(path: Path, name: str) -> pd.Series:
    return pd.read_csv(path, index_col="date", parse_dates=["date"])[name]


def adjusted_close(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """只下载本次回测需要的 ETF 复权行情。"""
    path = CACHE / f"yahoo_{symbol}_{start.date()}_{end.date()}.csv"
    if path.exists():
        prices = read_csv(path, "adjclose")
    else:
        params = urllib.parse.urlencode({
            "period1": int(start.tz_localize("UTC").timestamp()),
            "period2": int((end + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
            "interval": "1d",
        })
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = f"https://{host}/v8/finance/chart/{symbol}?{params}"
            try:
                result = json.loads(live.http_bytes(url))["chart"]["result"][0]
                if result["meta"]["symbol"] != symbol:
                    raise ValueError(f"{symbol} 的行情标识不匹配")
                dates = pd.to_datetime(result["timestamp"], unit="s", utc=True)
                dates = dates.tz_convert("America/New_York").tz_localize(None).normalize()
                prices = pd.Series(result["indicators"]["adjclose"][0]["adjclose"],
                                   index=dates, dtype=float, name="adjclose")
                validate_prices(prices, symbol, start, end)
                write_csv(prices, path)
                break
            except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
                last_error = exc
        else:
            raise RuntimeError(f"{symbol} 复权行情不可用，请检查 Yahoo Finance 或本地缓存") from last_error
    validate_prices(prices, symbol, start, end)
    return prices


def validate_prices(prices: pd.Series, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> None:
    if (prices.empty or not prices.index.is_unique or not prices.index.is_monotonic_increasing
            or not np.isfinite(prices).all() or (prices <= 0).any()
            or prices.index[0] < start or prices.index[-1] != end):
        raise ValueError(f"{symbol} 的复权行情有缺口或无效记录")


def cash_rates(end: pd.Timestamp) -> pd.Series:
    """用仓库 H.15 历史，超出缓存末日时补取同口径 FRED 尾段。"""
    rates = market.load_h15_series("RIFLGFCM03_N.B")
    last = rates.index.max()
    if end <= last:
        return rates

    start = last - pd.Timedelta(days=7)
    path = CACHE / f"fred_DGS3MO_{start.date()}_{end.date()}.csv"
    local_name = "fred_DGS3MO.csv"
    local = (market.load_fred(local_name).loc[start:end]
             if (market.CACHE / local_name).exists() else pd.Series(dtype=float))
    if valid_rate_tail(local, rates, end):
        tail = local
    elif path.exists():
        tail = read_csv(path, "DGS3MO")
    else:
        params = urllib.parse.urlencode({"id": "DGS3MO", "cosd": start.date(), "coed": end.date()})
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{params}"
        try:
            raw = _get(url, timeout=8, retries=2, ua=UA_PLAIN)
        except OSError:
            with urllib.request.urlopen(url, timeout=15) as response:
                raw = response.read()
        data = pd.read_csv(io.BytesIO(raw), parse_dates=["observation_date"])
        tail = pd.to_numeric(data.set_index("observation_date")["DGS3MO"], errors="coerce")
        tail = tail.dropna().sort_index()
    if not valid_rate_tail(tail, rates, end):
        raise ValueError("FRED 3 月期国债利率尾段不完整，或与 H.15 重叠值不一致")
    if not path.exists():
        write_csv(tail.rename("DGS3MO"), path)
    return pd.concat([rates, tail.loc[tail.index > last]]).loc[:end]


def valid_rate_tail(tail: pd.Series, history: pd.Series, end: pd.Timestamp) -> bool:
    overlap = tail.index.intersection(history.index)
    return (len(overlap) > 0 and tail.index.is_unique and np.isfinite(tail).all()
            and np.array_equal(tail.loc[overlap], history.loc[overlap])
            and tail.index.max() >= end - pd.Timedelta(days=7))


@dataclass(frozen=True)
class Order:
    signal_date: pd.Timestamp
    fill_date: pd.Timestamp
    action: str


@dataclass(frozen=True)
class Round:
    asset: str
    sell: Order
    buy: Order | None

    @property
    def orders(self) -> tuple[Order, ...]:
        return (self.sell, self.buy) if self.buy is not None else (self.sell,)


def scheduled_rounds(states: pd.DataFrame, calendar: pd.DatetimeIndex, asset: str) -> list[Round]:
    sessions = dict(zip(calendar[:-1], calendar[1:]))
    rounds = []
    sell = None
    for signal_date, action in states["trade_signal"].dropna().items():
        fill_date = sessions.get(signal_date)
        if fill_date is None:
            continue  # 样本末日的指令在区间外成交
        order = Order(signal_date, fill_date, action)
        if action == "derisk" and sell is None:
            sell = order
        elif action == "buyback" and sell is not None:
            rounds.append(Round(asset, sell, order))
            sell = None
        else:
            raise ValueError(f"{asset} 收到无法与账户配对的切换指令：{signal_date} {action}")
    if sell is not None:
        rounds.append(Round(asset, sell, None))
    return rounds


def run_account(prices: pd.Series, deposits: pd.Series, cash_growth: pd.Series,
                rounds: list[Round], cost: float) -> pd.DataFrame:
    actions = {order.fill_date: order.action for episode in rounds for order in episode.orders}
    equity = cash = 0.0
    rows = []
    for date, ratio, deposit, growth in zip(prices.index, prices.div(prices.shift()).fillna(1.0),
                                           deposits, cash_growth):
        equity *= ratio
        cash *= growth
        buy = deposit / (1 + cost)
        fees = buy * cost
        equity += buy
        action = actions.get(date)
        if action == "derisk":
            target = live.RISK_CASH_TARGET
            sale = (target * (equity + cash) - cash) / (1 - cost + target * cost)
            equity -= sale
            cash += sale * (1 - cost)
            fees += sale * cost
        elif action == "buyback":
            buy = cash / (1 + cost)
            equity += buy
            fees += buy * cost
            cash = 0.0
        rows.append((equity + cash, cash, fees, deposit))
    return pd.DataFrame(rows, index=prices.index, columns=["value", "cash", "fees", "deposit"])


@dataclass(frozen=True)
class Metrics:
    end_value: float
    xirr: float
    drawdown: float
    drawdown_date: pd.Timestamp
    max_cash: float
    fees: float


def summarize(book: pd.DataFrame) -> Metrics:
    values = book["value"]
    deposits = book["deposit"]
    # 当日流入发生在收盘，剔除流入后与昨日收盘净值相比。首日包含入场成本。
    gross = (values - deposits) / values.shift()
    gross.iloc[0] = values.iloc[0] / deposits.iloc[0]
    nav = gross.cumprod()
    drawdowns = nav.div(nav.cummax().clip(lower=1.0)) - 1
    years = (values.index[-1] - values.index).days.to_numpy() / 365.0
    invested = deposits.to_numpy()
    end_value = float(values.iloc[-1])

    def residual(log_return: float) -> float:
        return end_value - np.dot(invested, np.exp(years * log_return))

    # 累积到终点的方程严格单调，只有一期正现金流。
    xirr = math.expm1(brentq(residual, -5.0, 5.0))
    return Metrics(end_value, xirr, float(drawdowns.min()), drawdowns.idxmin(),
                   float(book["cash"].div(values).max()), float(book["fees"].sum()))


def combined_rounds(rounds: list[Round]) -> list[list[Round]]:
    """合并实际现金持有区间有交集的轮次。"""
    groups = []
    for episode in sorted(rounds, key=lambda item: item.sell.fill_date):
        if groups and any(item.buy is None or episode.sell.fill_date < item.buy.fill_date
                          for item in groups[-1]):
            groups[-1].append(episode)
        else:
            groups.append([episode])
    return groups


def as_money(amount: float) -> str:
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def gap(value: float, plain: float) -> str:
    difference = round((value - plain) * 100, 3)
    return f"{difference:+.3f} 个百分点" if difference else "0.000 个百分点"


def metric_row(name: str, metrics: Metrics, plain: Metrics) -> str:
    return (f"| {name} | {as_money(metrics.end_value)} | {metrics.xirr:.3%} | "
            f"{metrics.drawdown:.3%} | {metrics.max_cash:.3%} | {as_money(metrics.fees)} | "
            f"{metrics.end_value / plain.end_value - 1:+.3%} |")


def render_report(deposits, books, benchmarks, rounds, states, rates, missing_market_days,
                  simulate) -> str:
    days = deposits.index
    accounts = [*ETF_BY_ASSET, "组合"]
    base = {asset: summarize(benchmarks[asset]) for asset in accounts}
    full = {asset: summarize(books[asset]) for asset in accounts}
    title = {asset: f"{asset}（{symbol}）" for asset, symbol in ETF_BY_ASSET.items()}
    title["组合"] = "组合"
    missing_count = len(missing_market_days)
    missing_dates = ("（" + "、".join(str(day.date()) for day in missing_market_days) + "）"
                     if 0 < missing_count <= 10 else "")
    lines = [
        "# v2.6 合并版实盘口径回测",
        "",
        f"同额投入 {as_money(deposits['组合'].sum())}。组合期末合并版 "
        f"{as_money(full['组合'].end_value)}，固定定投 {as_money(base['组合'].end_value)}，"
        f"相差 {full['组合'].end_value / base['组合'].end_value - 1:+.3%}。"
        f"合并版 XIRR 相差 {gap(full['组合'].xirr, base['组合'].xirr)}，"
        f"最大回撤相差 {gap(full['组合'].drawdown, base['组合'].drawdown)}。"
        f"两组最深回撤分别落在 {full['组合'].drawdown_date.date()}（合并版）和 "
        f"{base['组合'].drawdown_date.date()}（固定定投）。",
        "",
        "## 样本与执行",
        "",
        f"- {days[0].date()} 至 {days[-1].date()}，共 {len(days):,} 个美股交易日、"
        f"{int(deposits['组合'].gt(0).sum())} 次月供，从零本金起步。"
        f"每月首交易日收盘买入 SPX {as_money(live.MONTHLY_PLAN['SPX']['amount'])}、"
        f"NDX {as_money(live.MONTHLY_PLAN['NDX']['amount'])}，供款约 70/30。"
        "两边各自持有并自然漂移，资金不互调，组合是两边日终市值之和。",
        "- 每天调用日报的 `compute_signals()` 和 `replay()`。SPX 与 NDX 共用综合热度和估值，"
        "通过各自的收盘价、均线与回撤独立切换状态。收盘后确认，次一实际交易日收盘执行。"
        "月供遇到切换时先做月供。减仓时按扣交易成本后的账户价值一次性配至 20% 现金，"
        "其后继续固定月供，买回日将全部现金及利息用于买回。",
        "- 两组都用 ETF 复权收盘价衡量市值，含拆分和分红再投资。SPX 全段用可交易的 SPY，"
        "VOO 2010-09-07 才成立。NDX 全段用 QQQ。按复权收盘价的涨跌计持仓总回报，"
        "实际净值路径会受分红再投资时点、税负和成交质量影响。"
        "[Yahoo Finance 复权说明](https://help.yahoo.com/kb/SLN28256.html)、"
        "[SPY](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy)、"
        "[VOO](https://investor.vanguard.com/tools-calculators/etf-fund-comparison-tool?Ticker=voo)、"
        "[QQQ](https://finance.yahoo.com/quote/QQQ/history/)。",
        f"- 每笔 ETF 买卖按名义金额扣 {TRADE_COST_BPS:g} 个基点，两组的月供也计费。假设可买零股，"
        "回测为美元税前结果，略去现金工具费用、清算成本、账户费、税收与额外冷区加仓。"
        "现金用美联储 H.15 3 个月期国债投资收益率（FRED DGS3MO 同口径）代理，"
        "按已知利率和 ACT/365 逐自然日复利，跨休市日照计。"
        "[FRED DGS3MO](https://fred.stlouisfed.org/series/DGS3MO)。",
        "- XIRR 以实际入金和期末市值按日期折现，年基数 365 天。最大回撤自日频单位净值计算，"
        "每个交易日收益因子为（当日收盘市值减当日收盘新增资金）除以前一交易日收盘市值，"
        "首日包含入场交易成本。",
        "",
        "## 数据覆盖",
        "",
    ]
    for asset in ETF_BY_ASSET:
        valid = states[asset].index.intersection(days)
        missing = len(days) - len(valid)
        covered = f"{valid[0].date()} 至 {valid[-1].date()}" if len(valid) else "全段缺失"
        pending = states[asset]["trade_signal"].get(days[-1])
        pending_text = {"derisk": "减仓", "buyback": "买回"}.get(pending, "无")
        lines.append(f"- {title[asset]} 有效状态日 {len(valid):,} 个（{covered}），缺失 {missing:,} 个，"
                     f"样本末日未在区间内成交的指令为{pending_text}。信号预热及缺行期间仍照常按月供款。")
    lines.extend([
        f"- 原始广度或 Put/Call 缺失 {missing_count:,} 个交易日{missing_dates}，"
        "已计入各账户的缺失日。"
        "这些日期暂停发交易指令并清零连续确认计数，保留上次状态。"
        "如上一实际交易日已经发出指令，仍按原定日期成交。",
        "- ETF 样本内同历，复权价格不填补。交易日历与生产行情按实际日期核验。"
        "GDP 自 2002 年按 BEA 的实际发布日期与当期值回放，早期暖机使用按月末可用的费城联储快照。"
        "HY 利差按历史归档与 FRED 日频记录拼接。CAPE、CPI、广度、Put/Call 等沿用仓库历史快照"
        "或补档，无法逐日校验所有的当时可见版本。"
        "[数据说明](data_manual/README.md)。",
        f"- 3 个月期国债最近观测 {rates.loc[:days[-1]].index.max().date()}。"
        "每段现金从上一收盘日前已公布的最近观测取息，假日期间沿用该利率。",
        "",
        "## 同额定投对照",
        "",
        "| 账户与规则 | 期末市值 | XIRR | 最大回撤 | 最高现金占比 | ETF 交易成本 | 期末相对定投 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for asset in accounts:
        lines.append(metric_row(f"{title[asset]} · 固定定投", base[asset], base[asset]))
        lines.append(metric_row(f"{title[asset]} · 合并版", full[asset], base[asset]))

    all_rounds = [episode for episodes in rounds.values() for episode in episodes]
    individual_impact = {}
    lines.extend([
        "",
        "## 逐轮切换",
        "",
        "持有天数从减仓成交至买回成交，按自然日计。轮次期末贡献用完整账户减去单独跳过"
        "该轮两笔切换后重跑的账户市值。",
        "",
        "| 账户 | 减仓确认 | 减仓成交 | 买回确认 | 买回成交 | 现金自然日 | 期末市值贡献 |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ])
    for episode in sorted(all_rounds, key=lambda item: item.sell.fill_date):
        sell, buy = episode.sell, episode.buy
        row = f"| {title[episode.asset]} | {sell.signal_date.date()} | {sell.fill_date.date()} |"
        if buy is None:
            lines.append(f"{row} — | — | 截至末日 {(days[-1] - sell.fill_date).days} | — |")
            continue
        asset = episode.asset
        omitted = simulate(asset, [other for other in rounds[asset] if other != episode])
        individual_impact[episode] = full[asset].end_value - omitted["value"].iloc[-1], omitted
        lines.append(f"{row} {buy.signal_date.date()} | {buy.fill_date.date()} | "
                     f"{(buy.fill_date - sell.fill_date).days} | "
                     f"{as_money(individual_impact[episode][0])} |")
    lines.extend([
        "",
        "## 去掉最好一轮",
        "",
        "单账户逐一跳过某个完整轮次的减仓和买回，从首日重跑其余动作及月供，选期末美元"
        "贡献最大的一轮。组合将两边现金持有区间实际重叠的轮次合为同一段，逐段同时跳过相应"
        "账户的动作，并重算组合。尚未买回的区间不参与筛选。",
        "",
        "| 重算账户 | 跳过的成交区间 | 该轮期末贡献 | 重算期末 | 重算 XIRR | 重算最大回撤 | 重算期末相对定投 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for asset in accounts:
        if asset == "组合":
            candidates = []
            for group in combined_rounds(all_rounds):
                if any(episode.buy is None for episode in group):
                    continue
                together = sum(simulate(account, [episode for episode in rounds[account]
                                                   if episode not in group])
                               for account in ETF_BY_ASSET)
                candidates.append((full[asset].end_value - together["value"].iloc[-1], group, together))
        else:
            candidates = [(impact, [episode], omitted) for episode, (impact, omitted)
                          in individual_impact.items() if episode.asset == asset]
        if not candidates:
            lines.append(f"| {title[asset]} | 无完整轮次 | — | — | — | — | — |")
            continue
        impact, group, omitted = max(candidates, key=lambda item: item[0])
        window = "、".join(f"{episode.asset} {episode.sell.fill_date.date()}–{episode.buy.fill_date.date()}"
                          for episode in group)
        result = summarize(omitted)
        relative = result.end_value / base[asset].end_value - 1
        lines.append(f"| {title[asset]} | {window} | {as_money(impact)} | {as_money(result.end_value)} | "
                     f"{result.xirr:.3%} | {result.drawdown:.3%} | {relative:+.3%} |")
        if asset == "组合":
            lines[2] += f"删掉贡献最大的完整轮次后，组合期末相对固定定投 {relative:+.3%}。"

    trailing = [asset for asset in ETF_BY_ASSET if full[asset].end_value < base[asset].end_value]
    close = "本区间两个账户均达到同额定投期末市值。"
    if trailing:
        close = f"本区间 {'、'.join(trailing)} 的期末市值低于同额定投，按方案门槛应降级为仅观察。"
    if any(full[asset].max_cash > 0.25 for asset in ETF_BY_ASSET):
        close += "至少一个账户最高现金占比超过方案复核线 25%。"
    lines.extend(["", close])
    lines.extend([
        "",
        "## 复跑",
        "",
        "安装 `requirements.txt` 中的依赖。复现本报告时，以环境变量 "
        "`MARKET_BREADTH_ARCHIVE_DIR` 指向授权的私有广度归档本地副本，"
        "用 `python daily_status.py` 的数据更新环节准备覆盖报告截至日的行情、宏观与 Put/Call 缓存。"
        "保留所用数据快照和报告截至日，随后在项目目录运行 `python backtest_v26_merged_live.py`。"
        f"脚本默认沿用生产行情缓存末日，锁定本表样本时设 `END = \"{days[-1].date()}\"`，"
        "交易成本可调 `TRADE_COST_BPS`。仓库随附缓存可直接回放其已覆盖的区间。"
        "[历史归档说明](data_manual/README.md)。",
        "",
        "初次运行时，从 Yahoo Finance 的 chart 接口取指定区间的 SPY、QQQ 复权收盘价。"
        "3 个月期国债样本超出 H.15 缓存时，优先读仓库内已有的 FRED 缓存；"
        "覆盖不足时请求尾段，核验与 H.15 重叠日的利率。"
        "下载缓存位于用户目录下的 `.cache/us-fund-tools/merged-backtest/`，按起止日期命名。"
        "同区间重跑直接读这些缓存。原始 ETF 数据保存在本机缓存中。",
        "",
        "避险切换次数有限，删轮是样本内敏感性检验。回测供量化方法研究参考，不构成投资建议。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    signals = live.compute_signals()
    start = pd.Timestamp(live.START)
    end_limit = pd.Timestamp(END) if END else signals.index.max()
    if end_limit > signals.index.max():
        raise ValueError(f"生产行情缓存仅到 {signals.index.max().date()}，未覆盖指定截止日")
    end = signals.loc[:end_limit].index.max()
    if pd.isna(end) or end <= start:
        raise ValueError("回测样本必须有多个交易日")
    if not 0 <= TRADE_COST_BPS < 10000:
        raise ValueError("交易成本须在 0 至 10000 个基点之间")
    prices = {asset: adjusted_close(symbol, start, end) for asset, symbol in ETF_BY_ASSET.items()}
    calendar = prices["SPX"].index
    if (not prices["NDX"].index.equals(calendar)
            or not signals.loc[start:end].index.difference(calendar).empty):
        raise ValueError("两只 ETF 或生产行情的交易日覆盖不一致，请复查原始缓存")

    # 用真实交易日补齐空行，使数据缺口重置生产状态机的连续确认计数。
    signals = signals.loc[:end].reindex(signals.loc[:end].index.union(calendar))
    states = {asset: live.replay(signals, asset.lower())[1] for asset in ETF_BY_ASSET}
    rounds = {asset: scheduled_rounds(states[asset], calendar, asset) for asset in ETF_BY_ASSET}
    month = calendar.to_period("M")
    deposits = pd.DataFrame({asset: np.where(~month.duplicated(), live.MONTHLY_PLAN[asset]["amount"], 0)
                             for asset in ETF_BY_ASSET}, index=calendar)
    deposits["组合"] = deposits.sum(axis=1)

    rates = cash_rates(end)
    prev = calendar.to_series().shift()
    # 只用上一收盘日前的观测，排除当日收盘后才公布的利率。
    usable = rates.reindex(pd.DatetimeIndex(prev.iloc[1:]) - pd.Timedelta(days=1), method="ffill")
    if usable.isna().any():
        raise ValueError("缺少用于现金计息的已知 3 个月国债利率")
    cash_growth = pd.Series(1.0, index=calendar)
    cash_growth.iloc[1:] = (1 + usable.to_numpy() / 36500) ** (calendar.to_series() - prev).dt.days.iloc[1:]
    cost = TRADE_COST_BPS / 10000

    def simulate(asset: str, selected: list[Round]) -> pd.DataFrame:
        return run_account(prices[asset], deposits[asset], cash_growth, selected, cost)

    books = {asset: simulate(asset, rounds[asset]) for asset in ETF_BY_ASSET}
    benchmarks = {asset: simulate(asset, []) for asset in ETF_BY_ASSET}
    for collection in (books, benchmarks):
        collection["组合"] = collection["SPX"] + collection["NDX"]
    market_complete = signals.loc[calendar, "market_indicators_complete"].eq(True).fillna(False)
    report = render_report(deposits, books, benchmarks, rounds, states, rates,
                           calendar[~market_complete], simulate)
    REPORT.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
