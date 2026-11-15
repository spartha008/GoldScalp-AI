"""Market structure: swing highs/lows, BOS and CHoCH (definition structure@1).

Definitions (documented in PROPOSED_ARCHITECTURE.md section 5):
- Swing high at bar i: high[i] is strictly greater than the previous ``left``
  highs and >= the next ``right`` highs (mirror for lows). It is only KNOWN
  at the close of bar i+right ("confirmed_at"); nothing uses it earlier.
- The walk is causal: at each closed bar t it first confirms any swing whose
  right window ends at t, then checks the close of bar t.
- A CLOSE above the most recent confirmed, unbroken swing high is a bullish
  break. It is a BOS when the trend was already up (or undetermined), and a
  CHoCH when the trend was down. Mirror for closes below swing lows.
- Wick-only pierces are not breaks.
Only closed bars are read. Truncating the input never changes earlier results
(tests/test_analysis.py::test_structure_has_no_lookahead).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from goldscalp.core.clock import iso

VERSION = "structure@1"


def analyse(frame: pd.DataFrame, bar_seconds: int, left: int = 3, right: int = 3) -> dict[str, Any]:
    closed = frame[frame["closed"]] if "closed" in frame else frame
    highs = closed["high"].to_numpy(dtype="float64")
    lows = closed["low"].to_numpy(dtype="float64")
    closes = closed["close"].to_numpy(dtype="float64")
    times = closed.index
    n = len(closed)
    step = pd.Timedelta(seconds=bar_seconds)

    swings: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    last_high: dict[str, Any] | None = None
    last_low: dict[str, Any] | None = None
    trend = "undetermined"

    for t in range(n):
        i = t - right
        if i - left >= 0:
            window_high = highs[i - left:i]
            window_low = lows[i - left:i]
            after_high = highs[i + 1:t + 1]
            after_low = lows[i + 1:t + 1]
            if highs[i] > window_high.max() and highs[i] >= after_high.max():
                last_high = _swing("high", times[i], highs[i], times[t] + step)
                swings.append(last_high)
            if lows[i] < window_low.min() and lows[i] <= after_low.min():
                last_low = _swing("low", times[i], lows[i], times[t] + step)
                swings.append(last_low)
        close_time = times[t] + step
        if last_high is not None and not last_high["broken"] and closes[t] > last_high["price"]:
            kind = "CHoCH" if trend == "down" else "BOS"
            events.append(_event(kind, "bullish", last_high, times[t], close_time, closes[t]))
            last_high["broken"] = True
            trend = "up"
        if last_low is not None and not last_low["broken"] and closes[t] < last_low["price"]:
            kind = "CHoCH" if trend == "up" else "BOS"
            events.append(_event(kind, "bearish", last_low, times[t], close_time, closes[t]))
            last_low["broken"] = True
            trend = "down"

    return {
        "definition": VERSION, "params": {"left": left, "right": right},
        "bars_used": n, "trend": trend,
        "swings": [_public(s) for s in swings],
        "events": events,
        "last_swing_high": _public(last_high) if last_high else None,
        "last_swing_low": _public(last_low) if last_low else None,
    }


def _swing(kind: str, at: pd.Timestamp, price: float, confirmed_at: pd.Timestamp) -> dict[str, Any]:
    return {"kind": kind, "time": at, "price": float(price), "confirmed_at": confirmed_at, "broken": False}


def _public(s: dict[str, Any]) -> dict[str, Any]:
    return {"kind": s["kind"], "time": iso(s["time"].to_pydatetime()), "price": round(s["price"], 5),
            "confirmed_at": iso(s["confirmed_at"].to_pydatetime()), "broken": s["broken"]}


def _event(kind: str, direction: str, swing: dict[str, Any], bar_time: pd.Timestamp, close_time: pd.Timestamp,
           close: float) -> dict[str, Any]:
    return {"type": kind, "direction": direction, "level": round(float(swing["price"]), 5),
            "swing_time": iso(swing["time"].to_pydatetime()), "bar_time": iso(bar_time.to_pydatetime()),
            "confirmed_at": iso(close_time.to_pydatetime()), "close": round(float(close), 5)}

