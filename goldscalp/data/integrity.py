"""Candle-integrity checks (PHASE4_ARCHITECTURE.md section 3.4), shared by the MT5 terminal verification and
``/api/live/integrity`` (stored candles). Nothing is repaired: defects are counted and reported.

Errors (FAIL): invalid OHLC, duplicate or non-increasing timestamps, timestamps off the timeframe grid, a
CLOSED bar that ends in the future, more than one forming bar.
Warnings (WARN): bars missing against the trading schedule, a stale last closed bar while the market is
open, zero or negative spreads.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.gaps import gap_report
from goldscalp.data.resample import resample

FUTURE_TOLERANCE = pd.Timedelta(seconds=5)
EXAMPLES = 5


def _examples(index: pd.DatetimeIndex, mask: np.ndarray) -> list[str]:
    return [iso(t.to_pydatetime()) for t in index[mask][:EXAMPLES]]


def check_candles(frame: pd.DataFrame, tf: Timeframe, *, now: datetime, schedule: TradingSchedule) -> dict[str, Any]:
    """``frame``: candles indexed by UTC open time with open/high/low/close[/spread]/closed."""
    out: dict[str, Any] = {"timeframe": tf.value, "bars": int(len(frame)), "errors": [], "warnings": []}
    if frame.empty:
        out.update(status="FAIL", first=None, last=None, last_closed=None)
        out["errors"].append({"check": "no_bars", "count": 0, "detail": "no candles"})
        return out
    idx = frame.index
    closed = frame["closed"].to_numpy(dtype=bool) if "closed" in frame else np.ones(len(frame), dtype=bool)
    o, h, lo, c = (frame[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    bad = ~np.isfinite(np.column_stack([o, h, lo, c])).all(axis=1) | (lo <= 0) | (lo > np.minimum(o, c)) \
        | (h < np.maximum(o, c))
    epochs = idx.as_unit("s").asi8
    dup = idx.duplicated()
    order = np.zeros(len(idx), dtype=bool)
    order[1:] = np.diff(epochs) <= 0
    misaligned = epochs % tf.seconds != 0
    ends = idx + pd.Timedelta(seconds=tf.seconds)
    future = closed & (ends > pd.Timestamp(now) + FUTURE_TOLERANCE)
    forming = int((~closed).sum())
    for name, mask, detail in (
            ("invalid_ohlc", bad, "low <= min(open, close) <= max(open, close) <= high, all positive"),
            ("duplicate_timestamp", dup, "the same open time twice"),
            ("non_increasing_timestamp", order & ~dup, "open times must strictly increase"),
            ("misaligned_timestamp", misaligned, f"open time not on the {tf.value} grid"),
            ("closed_bar_in_future", future, "a bar marked closed ends after now")):
        if mask.any():
            out["errors"].append({"check": name, "count": int(mask.sum()), "detail": detail,
                                  "examples": _examples(idx, mask)})
    if forming > 1:
        out["errors"].append({"check": "several_forming_bars", "count": forming,
                              "detail": "at most the newest bar may still be forming"})
    if "spread" in frame:
        sp = frame["spread"].to_numpy(dtype="float64")
        zero = np.isfinite(sp) & (sp <= 0)
        if zero.any():
            out["warnings"].append({"check": "zero_or_negative_spread", "count": int(zero.sum()),
                                    "detail": "spread <= 0 (unknown spread)", "examples": _examples(idx, zero)})
    closed_idx = idx[closed]
    gaps = gap_report(closed_idx, tf, schedule) if len(closed_idx) else None
    if gaps and gaps["missing"]:
        out["warnings"].append({"check": "missing_bars", "count": gaps["missing"],
                                "detail": f"{gaps['missing']} of {gaps['expected']} bars expected by the trading "
                                          "schedule are absent (holidays also show up here)",
                                "examples": [r["from"] for r in gaps["missing_ranges"][:EXAMPLES]]})
    last_closed = closed_idx.max() if len(closed_idx) else None
    if last_closed is not None and schedule.is_open(now):
        age = pd.Timestamp(now) - (last_closed + pd.Timedelta(seconds=tf.seconds))
        if age > pd.Timedelta(seconds=2 * tf.seconds + 60):
            out["warnings"].append({"check": "stale_last_bar", "count": 1,
                                    "detail": f"the market is open but the last closed bar ended "
                                              f"{int(age.total_seconds())} s ago"})
    out.update(first=iso(idx.min().to_pydatetime()), last=iso(idx.max().to_pydatetime()),
               last_closed=iso(last_closed.to_pydatetime()) if last_closed is not None else None,
               forming_bars=forming, gaps=gaps,
               status="FAIL" if out["errors"] else "WARN" if out["warnings"] else "PASS")
    return out


def cross_timeframe(m1: pd.DataFrame, native: pd.DataFrame, tf: Timeframe, *, tolerance: float) -> dict[str, Any]:
    """Native ``tf`` bars versus the same bars rebuilt from M1, on windows where every M1 bar is present and
    closed. A mismatch beyond ``tolerance`` (one price point) means the feeds disagree."""
    out: dict[str, Any] = {"timeframe": tf.value, "compared": 0, "mismatches": 0, "examples": []}
    if m1.empty or native.empty:
        out["status"] = "SKIPPED"
        out["detail"] = "not enough data to compare"
        return out
    rebuilt = resample(m1[m1["closed"]], Timeframe.M1, tf)
    full = rebuilt[rebuilt["sub_bars"] == tf.minutes]
    nat = native[native["closed"]] if "closed" in native else native
    common = full.index.intersection(nat.index)
    if not len(common):
        out["status"] = "SKIPPED"
        out["detail"] = "no fully covered window in common"
        return out
    a = full.loc[common, ["open", "high", "low", "close"]].to_numpy(dtype="float64")
    b = nat.loc[common, ["open", "high", "low", "close"]].to_numpy(dtype="float64")
    bad = (np.abs(a - b) > tolerance + 1e-9).any(axis=1)
    out.update(compared=int(len(common)), mismatches=int(bad.sum()),
               examples=[{"time": iso(t.to_pydatetime()), "native": [float(x) for x in b[i]],
                          "from_m1": [float(x) for x in a[i]]}
                         for i, t in enumerate(common) if bad[i]][:EXAMPLES])
    share = out["mismatches"] / out["compared"]
    out["status"] = "PASS" if out["mismatches"] == 0 else "WARN" if share <= 0.02 else "FAIL"
    out["detail"] = (f"{out['compared']} {tf.value} bars rebuilt from M1 compared with the terminal's own "
                     f"{tf.value} bars; {out['mismatches']} differ by more than {tolerance}")
    return out
