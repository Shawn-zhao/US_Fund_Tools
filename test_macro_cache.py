"""宏观缓存离线回归检查，用 python -m unittest test_macro_cache 运行。"""

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import backtest_v25_independent_Opus4_8 as v25
import fetch_data


class MacroCacheTest(unittest.TestCase):
    def test_gdp_switches_on_documented_release_dates(self):
        expected = pd.Series(
            [21729.1, 21537.9, 21539.7, 19408.8, 30485.7, 31095.1],
            index=pd.to_datetime([
                "2020-04-28", "2020-04-29", "2020-07-29", "2020-07-30",
                "2025-12-22", "2025-12-23",
            ]),
            name="GDP",
        )
        daily_gdp = v25.daily_ffill(v25.load_gdp_released(), expected.index)
        pd.testing.assert_series_equal(daily_gdp, expected)

    def test_short_hy_window_preserves_archive_and_cached_dates(self):
        original = fetch_data.load_hy_observations(v25.CACHE)
        self.assertEqual(min(original), dt.date(1996, 12, 31))
        last_day = max(original)
        next_day = last_day + dt.timedelta(days=1)
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            for fname in (
                fetch_data.HY_FRED_ARCHIVE,
                fetch_data.HY_MACROTRENDS_BRIDGE,
                fetch_data.HY_FRED,
            ):
                shutil.copyfile(v25.CACHE / fname, cache / fname)
            # 模拟旧更新器用短窗口覆盖历史 JSON。
            stamp = int(dt.datetime.combine(last_day, dt.time(), dt.timezone.utc).timestamp() * 1000)
            (cache / fetch_data.HY_MACROTRENDS).write_text(
                f'{{"data":[[{stamp},{original[last_day]}]]}}', encoding="utf-8"
            )
            self.assertEqual(fetch_data.load_hy_observations(cache), original)
            short_download = (
                f"observation_date,BAMLH0A0HYM2\n{last_day},.\n{next_day},3.5\n"
            ).encode("utf-8")
            with patch.object(fetch_data, "fetch_fred_csv", return_value=short_download):
                fetch_data.update_hy_oas(cache, lambda _: None)
                first_result = (cache / fetch_data.HY_FRED).read_bytes()
                fetch_data.update_hy_oas(cache, lambda _: None)
            self.assertEqual((cache / fetch_data.HY_FRED).read_bytes(), first_result)
            self.assertEqual(
                fetch_data.load_hy_observations(cache),
                original | {next_day: 3.5},
            )
