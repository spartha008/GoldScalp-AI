"""Missing-candle detection against the instrument's trading schedule.

Expected bars come from the schedule (weekly close and daily break are NOT
gaps). Missing bars are reported, never filled. Holidays are not modelled in
Phase 1, so a holiday shows up as a missing range.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import Timeframe

RANGE_LIMIT = 50


def gap_report(index: pd.DatetimeIndex, tf: Timeframe, schedule: TradingSchedule,
               start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    """Expected vs present bars over [start, end) (default: the frame's own span)."""
    if len(index) == 0:
        return {"expected": 0, "present": 0, "missing": 0, "outside_schedule": 0, "missing_ranges": [],
                "ranges_truncated": False}
    first = start or index.min().to_pydatetime()
    stop = end or (index.max() + pd.Timedelta(seconds=tf.seconds)).to_pydatetime()
    expected = schedule.expected_bars(first, stop, tf.seconds)
    present = index[(index >= pd.Timestamp(first)) & (index < pd.Timestamp(stop))]
    missing = expected.difference(present)
    outside = present.difference(expected)
    ranges = _ranges(missing, tf)
    return {"expected": int(len(expected)), "present": int(len(present)), "missing": int(len(missing)),
            "outside_schedule": int(len(outside)), "missing_ranges": ranges[:RANGE_LIMIT],
            "ranges_truncated": len(ranges) > RANGE_LIMIT}


def _ranges(missing: pd.DatetimeIndex, tf: Timeframe) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if len(missing) == 0:
        return out
    step = pd.Timedelta(seconds=tf.seconds)
    start = prev = missing[0]
    for stamp in missing[1:]:
        if stamp - prev != step:
            out.append({"from": iso(start.to_pydatetime()), "to": iso((prev + step).to_pydatetime()),
                        "bars": int((prev - start) / step) + 1})
            start = stamp
        prev = stamp
    out.append({"from": iso(start.to_pydatetime()), "to": iso((prev + step).to_pydatetime()),
                "bars": int((prev - start) / step) + 1})
    return out


def outside_schedule_fraction(index: pd.DatetimeIndex, tf: Timeframe, schedule: TradingSchedule) -> float:
    """Fraction of bars that open while the market is closed (whole bar closed)."""
    if len(index) == 0:
        return 0.0
    expected = schedule.expected_bars(index.min().to_pydatetime(),
                                      (index.max() + pd.Timedelta(seconds=tf.seconds)).to_pydatetime(), tf.seconds)
    return float(len(index.difference(expected))) / float(len(index))
