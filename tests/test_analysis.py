"""Analysis: no look-ahead, golden structure case, session DST handling, ATR."""
from __future__ import annotations

from datetime import datetime, UTC

import numpy as np
import pandas as pd

from goldscalp.analysis import sessions, structure, volatility
from tests.conftest import synthetic_m1

UTC = UTC


def frame_from(closes, highs=None, lows=None, start="2026-11-11T14:00Z"):
    idx = pd.date_range(start, periods=len(closes), freq="min", tz="UTC")
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs if highs is not None else closes + 0.5, dtype=float)
    lows = np.asarray(lows if lows is not None else closes - 0.5, dtype=float)
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "tick_volume": 1, "spread": np.nan, "closed": True}, index=idx)


def test_structure_has_no_lookahead(spec):
    """Results on candles[:k] equal full-series results that were known by bar k's close."""
    frame = synthetic_m1("2026-11-11T00:00Z", "2026-11-11T08:00Z", spec.schedule, seed=11)
    full = structure.analyse(frame, 60)
    for k in range(8, len(frame), 3):   # many cut points: a one-bar peek must show up somewhere
        part = structure.analyse(frame.iloc[:k], 60)
        cutoff = (frame.index[k - 1] + pd.Timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        known = [e for e in full["events"] if e["confirmed_at"] <= cutoff]
        assert part["events"] == known
        assert [s["time"] for s in part["swings"]] == [s["time"] for s in full["swings"] if s["confirmed_at"] <= cutoff]


def test_structure_golden_bos_then_choch():
    # Worked by hand (highs = close + 0.5, lows = close - 0.5, 3 bars each side):
    #  - swing high at bar 3 (104.5), confirmed at bar 6; bar 8 closes 105 above it -> bullish BOS (trend up)
    #  - swing low at bar 6 (100.5), confirmed at bar 9; bar 10 closes 99 below it -> bearish CHoCH (trend flips)
    #  - swing low at bar 10 (98.5), confirmed at bar 13; bar 15 closes 97 below it -> bearish BOS (trend continues)
    closes = [100, 101, 102, 104, 103, 102, 101, 103, 105, 103, 99, 100, 101, 102, 100, 97]
    frame = frame_from(closes)
    result = structure.analyse(frame, 60, left=3, right=3)
    kinds = [(e["type"], e["direction"], e["level"]) for e in result["events"]]
    assert kinds == [("BOS", "bullish", 104.5), ("CHoCH", "bearish", 100.5), ("BOS", "bearish", 98.5)]
    assert result["events"][1]["confirmed_at"] == "2026-11-11T14:11:00Z"   # close of bar 10
    assert result["trend"] == "down"


def test_swing_is_confirmed_from_its_own_window_only():
    # Swing high at bar 3 (105.5): bars 4-6 are lower, so it is confirmed at bar 6's close. Bar 7 is much
    # higher; an implementation that peeks at bar 7 would wrongly drop the swing.
    frame = frame_from([100, 101, 102, 105, 104, 103, 102, 108])
    result = structure.analyse(frame, 60, left=3, right=3)
    highs = [s for s in result["swings"] if s["kind"] == "high"]
    assert [(s["time"], s["price"], s["confirmed_at"]) for s in highs] == [
        ("2026-11-11T14:03:00Z", 105.5, "2026-11-11T14:07:00Z")]


def test_forming_bar_is_ignored():
    frame = frame_from([100, 101, 102, 104, 103, 102, 101, 103, 200])
    frame.loc[frame.index[-1], "closed"] = False
    result = structure.analyse(frame, 60)
    assert result["bars_used"] == 8
    assert all(e["close"] != 200 for e in result["events"])


def test_london_session_follows_uk_dst_while_new_york_follows_us_dst():
    # 2026-03-16: US already on DST (since Mar 8), UK not yet (from Mar 29).
    london = sessions.SESSIONS[1]
    ny = sessions.SESSIONS[2]
    assert london.window(datetime(2026, 3, 16).date())[0] == datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
    assert ny.window(datetime(2026, 3, 16).date())[0] == datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    assert london.window(datetime(2026, 7, 15).date())[0] == datetime(2026, 7, 15, 7, 0, tzinfo=UTC)


def test_session_summary_ranges(spec):
    frame = synthetic_m1("2026-11-11T00:00Z", "2026-11-11T15:00Z", spec.schedule)
    now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    out = sessions.summary(frame, now)
    by = {s["key"]: s for s in out["sessions"]}
    assert by["asia"]["status"] == "completed" and by["new_york"]["status"] == "active"
    assert out["active"] == ["London", "New York"] and out["overlap"] is True
    window = frame[(frame.index >= pd.Timestamp("2026-11-11T08:00Z")) & (frame.index < now)]
    assert by["london"]["high"] == window["high"].max()


def test_atr_wilder_on_constant_range():
    frame = frame_from([100.0] * 30, highs=[101.0] * 30, lows=[99.0] * 30)
    assert volatility.atr(frame) == 2.0


def test_trading_day_rolls_at_17_new_york():
    idx = pd.DatetimeIndex(["2026-11-11T21:59Z", "2026-11-11T23:00Z"], tz="UTC")   # 16:59 and 18:00 NY
    frame = pd.DataFrame({"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2]}, index=idx)
    days = volatility.trading_days(frame)
    assert list(map(str, days.index)) == ["2026-11-11", "2026-11-12"]
