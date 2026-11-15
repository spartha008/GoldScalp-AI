"""The ONE place that enforces the as-of rule (PHASE2_ARCHITECTURE.md section 2).

closed_view(): candles whose ``closed`` flag is true AND whose bar has ended by ``as_of``.
forming_bar(): the bar that contains ``as_of`` (for DEVELOPING observations only).
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe


def closed_view(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    mask = np.array(frame["closed"].to_numpy(dtype=bool), copy=True) if "closed" in frame else np.ones(len(frame), dtype=bool)
    if as_of is not None:
        ends = frame.index + pd.Timedelta(seconds=tf.seconds)
        mask = mask & np.asarray(ends <= pd.Timestamp(as_of))
    return frame[mask]


def forming_bar(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None) -> pd.Series | None:
    """The still-forming bar at ``as_of`` (None if there is none)."""
    if frame is None or frame.empty or as_of is None:
        return None
    last_time = frame.index[-1]
    ends = last_time + pd.Timedelta(seconds=tf.seconds)
    if last_time <= pd.Timestamp(as_of) < ends or ("closed" in frame and not bool(frame["closed"].iloc[-1])
                                                  and last_time <= pd.Timestamp(as_of)):
        return frame.iloc[-1]
    return None


_STAMP_CACHE: dict[tuple[str, bytes], tuple[list[str], list[str]]] = {}   # shared read-only lists
_STAMP_LOCK = threading.Lock()


class Stamps:
    """ISO strings for a candle index, computed once per analysis call (vectorized). ``open[i]`` is the
    candle's open time, ``close[i]`` its close time, ``cid(i)`` its candle id. Same format as ``iso()``."""

    FORMAT = "%Y-%m-%dT%H:%M:%SZ"

    def __init__(self, index: pd.DatetimeIndex, tf: Timeframe) -> None:
        self.tf = tf
        if not len(index):
            self.open, self.close = [], []
            return
        key = (tf.value, hashlib.blake2b(index.as_unit("s").asi8.tobytes(), digest_size=16).digest())
        with _STAMP_LOCK:
            cached = _STAMP_CACHE.get(key)
        if cached is None:
            cached = (list(index.tz_convert("UTC").strftime(self.FORMAT)),
                      list((index + pd.Timedelta(seconds=tf.seconds)).tz_convert("UTC").strftime(self.FORMAT)))
            with _STAMP_LOCK:
                while len(_STAMP_CACHE) >= 64:
                    _STAMP_CACHE.pop(next(iter(_STAMP_CACHE)))
                _STAMP_CACHE[key] = cached
        self.open, self.close = cached

    def cid(self, i: int) -> str:
        return f"{self.tf.value}@{self.open[i]}"


def candle_id(tf: Timeframe, ts: pd.Timestamp) -> str:
    return f"{tf.value}@{iso(ts.to_pydatetime())}"


def close_time(tf: Timeframe, ts: pd.Timestamp) -> pd.Timestamp:
    return ts + pd.Timedelta(seconds=tf.seconds)


def atr_series(frame: pd.DataFrame, period: int) -> np.ndarray:
    """Wilder ATR per bar, NaN until ``period`` true ranges exist. Uses only bars up to each index."""
    n = len(frame)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    h, low, c = (frame[k].to_numpy(dtype="float64") for k in ("high", "low", "close"))
    tr = np.empty(n)
    tr[0] = h[0] - low[0]
    tr[1:] = np.maximum.reduce([h[1:] - low[1:], np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])])
    if n <= period:
        return out
    value = float(tr[1:period + 1].mean())
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


def fmt(price: float, digits: int = 2) -> str:
    return f"{price:,.{digits}f}"


def rounded(value: Any, digits: int = 5) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, digits) if np.isfinite(f) else None
