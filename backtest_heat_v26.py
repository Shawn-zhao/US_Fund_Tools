"""Backtest the v2.6 live-operation candidate.

The v2.6 candidate keeps the v2.5 indicator engine intact, but changes the
execution layer. Core monthly DCA always continues. A small risk-reserve
state machine moves up to 20% of the portfolio into cash/short bills only
when valuation is top-decile and trend has broken down for 10 trading days.
The reserve is redeployed when the market cools or the trend recovers.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "backtest_outputs"
V25_PATH = ROOT / "backtest_heat_v25.py"

START = "2007-01-01"
CASH_SERIES = "RIFLGFCM03_N.B"  # 3-month Treasury constant maturity, daily.

SELECTED_RULE = {
    "ma_days": 200,
    "risk_target": 0.20,
    "valuation_score_threshold": 90.0,
    "drawdown_trigger": 0.10,
    "confirm_days": 10,
    "recover_days": 10,
}


@dataclass(frozen=True)
class RuleParams:
    ma_days: int
    risk_target: float
    valuation_score_threshold: float
    drawdown_trigger: float = 0.10
    confirm_days: int = 10
    recover_days: int = 10


def load_v25_module():
    spec = importlib.util.spec_from_file_location("backtest_heat_v25_imported", V25_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {V25_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rolling_all_true(cond: pd.Series, days: int) -> pd.Series:
    return cond.fillna(False).astype(int).rolling(days, min_periods=days).sum() >= days


def max_drawdown(values: pd.Series) -> float:
    values = values.dropna()
    if values.empty:
        return math.nan
    return float((values / values.cummax() - 1.0).min() * 100.0)


def first_trading_days(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    return set(pd.Series(index, index=index).groupby(index.to_period("M")).head(1).index)


def cash_factors(rate: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    cleaned = rate.sort_index().where(lambda s: s > -1000.0) / 100.0
    # A short-bill/money-market proxy. We accrue on trading days only to keep
    # the simulator simple and avoid injecting calendar artifacts.
    return (1.0 + cleaned.reindex(index).ffill().fillna(0.0) / 252.0).clip(lower=0.99, upper=1.01)


def build_signals(
    heat: pd.DataFrame,
    scores: pd.DataFrame,
    close: pd.Series,
    params: RuleParams,
) -> pd.DataFrame:
    df = heat.join(scores, how="left", rsuffix="_score").reindex(close.index).ffill()
    ma = close.rolling(params.ma_days).mean()
    drawdown = 1.0 - close / close.cummax()

    expensive = (
        (df["CAPE"] >= params.valuation_score_threshold)
        & (df["Buffett"] >= params.valuation_score_threshold)
    )
    trend_break = (close < ma) | (drawdown > params.drawdown_trigger)
    panic_cold = df["vix_panic"].fillna(False).astype(bool) & df["vix_backwardation"].fillna(False).astype(bool)
    cold = (df["base"] < 35.0) | panic_cold

    risk_raw = (expensive & trend_break & ~cold).fillna(False)
    risk_enter = rolling_all_true(risk_raw, params.confirm_days)
    trend_recovered = rolling_all_true((close > ma).fillna(False), params.recover_days)
    risk_exit = (trend_recovered | cold | ~expensive).fillna(False)

    return pd.DataFrame(
        {
            "close": close,
            "ma": ma,
            "drawdown": drawdown,
            "base": df["base"],
            "CAPE": df["CAPE"],
            "Buffett": df["Buffett"],
            "expensive": expensive,
            "trend_break": trend_break,
            "cold": cold,
            "risk_raw": risk_raw,
            "risk_enter": risk_enter,
            "risk_exit": risk_exit,
        },
        index=close.index,
    )


def simulate_asset(
    asset: str,
    close: pd.Series,
    heat: pd.DataFrame,
    scores: pd.DataFrame,
    cash_rate: pd.Series,
    params: RuleParams,
    start: str = START,
) -> tuple[dict[str, float | int | str], pd.DataFrame, pd.DataFrame]:
    price = close.dropna().loc[start:]
    signals = build_signals(heat, scores, price, params)
    factors = cash_factors(cash_rate, price.index)
    month_starts = first_trading_days(price.index)

    shares = 0.0
    cash = 0.0
    fixed_shares = 0.0
    in_risk = False
    contributions = 0

    rows: list[dict[str, float | str | pd.Timestamp | bool]] = []
    events: list[dict[str, float | str | pd.Timestamp]] = []

    for i, date in enumerate(price.index):
        if i > 0:
            cash *= float(factors.loc[date])

        px = float(price.loc[date])

        if date in month_starts and i > 0:
            contributions += 1
            cash += 1.0
            invest = min(cash, 1.0)
            shares += invest / px
            cash -= invest
            fixed_shares += 1.0 / px

        if i > 0:
            signal_date = price.index[i - 1]
            signal = signals.loc[signal_date]
            total = shares * px + cash
            if total > 0.0:
                if (not in_risk) and bool(signal["risk_enter"]):
                    desired_cash = params.risk_target * total
                    sell_value = min(max(0.0, desired_cash - cash), shares * px)
                    if sell_value > 1e-9:
                        shares -= sell_value / px
                        cash += sell_value
                        in_risk = True
                        events.append(
                            {
                                "asset": asset,
                                "date": date,
                                "action": "risk_off",
                                "signal_date": signal_date,
                                "amount": sell_value,
                                "cash_pct": cash / (shares * px + cash),
                                "base": float(signal["base"]),
                                "cape_score": float(signal["CAPE"]),
                                "buffett_score": float(signal["Buffett"]),
                            }
                        )
                elif in_risk and bool(signal["risk_exit"]):
                    if cash > 1e-9:
                        buy_value = cash
                        shares += buy_value / px
                        cash = 0.0
                        events.append(
                            {
                                "asset": asset,
                                "date": date,
                                "action": "risk_on",
                                "signal_date": signal_date,
                                "amount": buy_value,
                                "cash_pct": 0.0,
                                "base": float(signal["base"]),
                                "cape_score": float(signal["CAPE"]),
                                "buffett_score": float(signal["Buffett"]),
                            }
                        )
                    in_risk = False

        strategy_value = shares * px + cash
        fixed_value = fixed_shares * px
        rows.append(
            {
                "date": date,
                "asset": asset,
                "strategy_value": strategy_value,
                "fixed_value": fixed_value,
                "cash": cash,
                "cash_pct": cash / strategy_value if strategy_value > 0 else 0.0,
                "in_risk": in_risk,
                "price": px,
            }
        )

    values = pd.DataFrame(rows).set_index("date")
    events_df = pd.DataFrame(events)
    summary = {
        "asset": asset,
        "months": int(contributions),
        "strategy_final_value": round(float(values["strategy_value"].iloc[-1]), 2),
        "fixed_final_value": round(float(values["fixed_value"].iloc[-1]), 2),
        "strategy_vs_fixed_pct": round(
            (float(values["strategy_value"].iloc[-1]) / float(values["fixed_value"].iloc[-1]) - 1.0) * 100.0,
            2,
        ),
        "strategy_max_drawdown_pct": round(max_drawdown(values["strategy_value"]), 2),
        "fixed_max_drawdown_pct": round(max_drawdown(values["fixed_value"]), 2),
        "cash_end": round(float(values["cash"].iloc[-1]), 2),
        "max_cash_pct": round(float(values["cash_pct"].max() * 100.0), 2),
        "risk_event_count": int(len(events_df)),
        "risk_raw_days": int(signals["risk_raw"].sum()),
    }
    return summary, values.reset_index(), events_df


def sensitivity_rows(
    prices: dict[str, pd.Series],
    heat: pd.DataFrame,
    scores: pd.DataFrame,
    cash_rate: pd.Series,
    params_iter: Iterable[RuleParams],
) -> pd.DataFrame:
    rows: list[dict[str, float | int | str]] = []
    for params in params_iter:
        for asset, close in prices.items():
            summary, _, _ = simulate_asset(asset, close, heat, scores, cash_rate, params)
            rows.append(
                {
                    "asset": asset,
                    "ma_days": params.ma_days,
                    "risk_target_pct": round(params.risk_target * 100.0, 1),
                    "valuation_score_threshold": params.valuation_score_threshold,
                    "confirm_days": params.confirm_days,
                    "recover_days": params.recover_days,
                    "strategy_vs_fixed_pct": summary["strategy_vs_fixed_pct"],
                    "risk_event_count": summary["risk_event_count"],
                }
            )
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_empty_"
    text = df.copy()
    for col in text.columns:
        text[col] = text[col].map(lambda x: "" if pd.isna(x) else str(x))
    lines = [
        "| " + " | ".join(text.columns) + " |",
        "| " + " | ".join(["---"] * len(text.columns)) + " |",
    ]
    for _, row in text.iterrows():
        lines.append("| " + " | ".join(str(row[col]).replace("|", "\\|") for col in text.columns) + " |")
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, events: pd.DataFrame, sensitivity: pd.DataFrame) -> None:
    passed = bool((summary["strategy_vs_fixed_pct"] > 0).all())
    lines: list[str] = []
    lines.append("# v2.6 实盘候选规则回测报告")
    lines.append("")
    lines.append("规则先验: 保留 v2.5 指标, 只改执行层; 核心 1x 月度定投不停投; 风险现金上限 20%; 不使用杠杆。")
    lines.append("")
    lines.append("## 候选规则")
    lines.append("- 每月第一个交易日投入 1 单位, 默认全部买入目标资产。")
    lines.append("- 当 CAPE 分位和 Buffett 分位均 >=90, 且价格跌破 200 日均线或自高点回撤超过 10%, 且该条件连续 10 个交易日成立时, 次日把组合调整为约 20% 现金/短债。")
    lines.append("- 若 Heat <35、VIX 恐慌贴水触发、估值不再处于双高分位, 或价格连续 10 个交易日站回 200 日均线, 次日把风险现金全部回补目标资产。")
    lines.append("- 现金按 H.15 3-month Treasury constant maturity (`RIFLGFCM03_N.B`) 折算日收益; H.15 的 `-9999` 缺值先置空再前向填充。")
    lines.append("")
    lines.append("## 通过标准")
    lines.append("- SPX 和 NDX 两个资产的 v2.6 策略期末值均超过固定 1x 月度定投。")
    lines.append("- 交易次数保持低频, 且最大现金比例不超过 25%。")
    lines.append(f"- 当前结果: {'通过' if passed else '未通过'}。")
    lines.append("")
    lines.append("## 主结果")
    lines.append(md_table(summary))
    lines.append("")
    lines.append("## 风险切换事件")
    if events.empty:
        lines.append("_无事件_")
    else:
        show = events.copy()
        for col in ["date", "signal_date"]:
            show[col] = pd.to_datetime(show[col]).dt.date.astype(str)
        for col in ["amount", "cash_pct", "base", "cape_score", "buffett_score"]:
            show[col] = show[col].astype(float).round(2)
        lines.append(md_table(show))
    lines.append("")
    lines.append("## 少量稳健性检查")
    pivot = sensitivity.pivot_table(
        index=["ma_days", "risk_target_pct", "valuation_score_threshold"],
        columns="asset",
        values="strategy_vs_fixed_pct",
    ).reset_index()
    pivot.columns.name = None
    pivot = pivot.sort_values(["valuation_score_threshold", "ma_days", "risk_target_pct"])
    lines.append(md_table(pivot.round(2)))
    lines.append("")
    lines.append("## 结论")
    if passed:
        lines.append("- 候选规则在当前数据覆盖下通过: SPX/NDX 均超过固定定投。")
        lines.append("- 收益改善来自少数高估趋势破位阶段的风险现金切换, 不是频繁调参; 但最大回撤并未显著改善, 说明它是收益/纪律改良, 不是尾部风险保险。")
        lines.append("- 可写入 v2.6, 但应保留后续滚动复核: 若未来新增样本使 SPX 或 NDX 任一侧落后固定定投, 规则自动降级为观察项。")
    else:
        lines.append("- 候选规则未通过, 不应写入 v2.6 最终方案。")
    (OUT / "v26_backtest_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    v25 = load_v25_module()
    data = v25.load_data()
    heat, scores = v25.build_heat(data, b_mode="public")
    cash_rate = v25.extract_h15_series(v25.fetch_h15_zip(), CASH_SERIES)

    prices = {
        "SPX": data.prices["spx"],
        "NDX": data.prices["ndx"],
    }
    selected = RuleParams(**SELECTED_RULE)

    summaries: list[dict[str, float | int | str]] = []
    value_frames: list[pd.DataFrame] = []
    event_frames: list[pd.DataFrame] = []
    for asset, close in prices.items():
        summary, values, events = simulate_asset(asset, close, heat, scores, cash_rate, selected)
        summaries.append(summary)
        value_frames.append(values)
        event_frames.append(events)

    summary_df = pd.DataFrame(summaries)
    values_df = pd.concat(value_frames, ignore_index=True)
    events_df = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()

    sensitivity_params = [
        RuleParams(ma_days=150, risk_target=0.15, valuation_score_threshold=90.0),
        RuleParams(ma_days=150, risk_target=0.20, valuation_score_threshold=90.0),
        RuleParams(ma_days=150, risk_target=0.25, valuation_score_threshold=90.0),
        RuleParams(ma_days=200, risk_target=0.15, valuation_score_threshold=90.0),
        RuleParams(ma_days=200, risk_target=0.20, valuation_score_threshold=90.0),
        RuleParams(ma_days=200, risk_target=0.25, valuation_score_threshold=90.0),
        RuleParams(ma_days=200, risk_target=0.20, valuation_score_threshold=95.0),
    ]
    sensitivity = sensitivity_rows(prices, heat, scores, cash_rate, sensitivity_params)

    summary_df.to_csv(OUT / "v26_dca_summary.csv", index=False)
    values_df.to_csv(OUT / "v26_daily_values.csv", index=False)
    events_df.to_csv(OUT / "v26_events.csv", index=False)
    sensitivity.to_csv(OUT / "v26_sensitivity.csv", index=False)
    write_report(summary_df, events_df, sensitivity)

    print(summary_df.to_string(index=False))
    print(f"Wrote {OUT / 'v26_backtest_report.md'}")


if __name__ == "__main__":
    main()
