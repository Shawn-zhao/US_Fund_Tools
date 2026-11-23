"""日报与回测共享的状态机回归检查。运行 python -m unittest test_daily_replay。"""

import unittest
from unittest.mock import patch

import pandas as pd

import backtest_v25_independent_Opus4_8 as v25
import backtest_v26_design_Opus4_8 as design
import daily_status


class DailyReplayTest(unittest.TestCase):
    def test_market_indicators_require_observations_on_the_same_day(self):
        days = pd.bdate_range("2024-01-02", periods=3)
        core = pd.DataFrame({"heat_pct": 60.0}, index=days)
        price = pd.Series(100.0, index=days)
        breadth = price.drop(days[1])
        put_call = price.drop(days[2])
        with (
            patch.object(design, "build_core", return_value=core),
            patch.object(v25, "load_yahoo", return_value=price),
            patch.object(v25, "load_breadth", return_value=breadth),
            patch.object(v25, "load_put_call", return_value=put_call),
        ):
            signals = daily_status.compute_signals()
        self.assertEqual(signals["market_indicators_complete"].tolist(), [True, False, False])
        self.assertTrue(signals["heat"].notna().all())

    def test_missing_observation_interrupts_both_confirmations_and_preserves_state(self):
        days = pd.bdate_range("2024-01-02", periods=250)
        signals = pd.DataFrame({
            "spx": 100.0, "CAPE": 90.0, "Buffett": 90.0, "heat": 60.0,
            "vix": 20.0, "vix3m": 25.0, "vix_pct": 70.0,
            "market_indicators_complete": True,
        }, index=days)
        signals.loc[days[200]:days[229], "spx"] = 80.0
        gaps = days[[209, 220, 239]]
        signals.loc[gaps, "market_indicators_complete"] = False
        signals.loc[days[220], "heat"] = 10.0

        _, states = daily_status.replay(signals, "spx")
        self.assertTrue(gaps.intersection(states.index).empty)
        self.assertEqual(states.loc[days[208], "trig"], 9)
        self.assertEqual(states.loc[days[210], "trig"], 1)
        self.assertEqual(states.loc[days[221], "state"], "risk_cash")
        self.assertEqual(states.loc[days[238], "above"], 9)
        self.assertEqual(states.loc[days[240], "above"], 1)
        self.assertEqual(states["trade_signal"].dropna().to_dict(), {
            days[219]: "derisk", days[249]: "buyback",
        })
