"""Resample canonical candles to a coarser timeframe.

open=first, high=max, low=min, close=last, tick_volume=sum (NA if all NA),
spread=max of the sub-bars, closed = every sub-bar closed. ``sub_bars`` records
how many source bars each output bar was built from, so an incomplete bar is
visible rather than hidden.
"""
from __future__ import annotations

import pandas as pd

from goldscalp.core.timeframes import Timeframe
from goldscalp.data.normalization import CANDLE_COLUMNS, empty_candles


def resample(frame: pd.DataFrame, source: Timeframe, target: Timeframe, as_of=None) -> pd.DataFrame:
    """``as_of`` (UTC datetime): a target bar is closed only if every sub-bar is closed AND the target
    bar has ended by ``as_of``. Without it, a partially elapsed hour built from closed M1 bars would
    wrongly look like a closed H1 candle (Phase 2 inspection finding)."""
    if target.seconds < source.seconds or target.seconds % source.seconds:
        raise ValueError(f"cannot resample {source} to {target}")
    if target is source:
        out = frame.copy()
        out["sub_bars"] = 1
        return out
    if frame.empty:
        out = empty_candles()
        out["sub_bars"] = pd.Series(dtype="int64")
        return out
    epochs = frame.index.as_unit("s").asi8
    keys = pd.to_datetime(epochs // target.seconds * target.seconds, unit="s", utc=True)
    grouped = frame.groupby(keys, sort=True)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "tick_volume": grouped["tick_volume"].sum(min_count=1).astype("Int64"),
        "spread": grouped["spread"].max(),
        "closed": grouped["closed"].all(),
        "sub_bars": grouped["open"].size().astype("int64"),
    })
    out.index.name = "ts"
    if as_of is not None:
        ended = (out.index + pd.Timedelta(seconds=target.seconds)) <= pd.Timestamp(as_of)
        out["closed"] = out["closed"] & ended
    return out[[*CANDLE_COLUMNS, "sub_bars"]]
