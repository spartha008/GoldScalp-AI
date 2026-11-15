"""Row-level candle validation. Invalid rows are rejected and counted; nothing is repaired.

OHLC rule adapted from SpicyStock src/market_data.py readable_pair() @ 96ef4732,
vectorised over the whole frame.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from goldscalp.core.timeframes import Timeframe

REASONS = {
    "bad_timestamp": "timestamp missing, unparseable, nonexistent or ambiguous in its timezone",
    "misaligned": "timestamp is not on a bar boundary for the timeframe",
    "future": "bar opens in the future",
    "non_finite_price": "a price is missing or not a finite number",
    "non_positive_price": "a price is zero or negative",
    "ohlc_inconsistent": "low <= min(open, close) <= max(open, close) <= high does not hold",
    "negative_volume": "tick volume is negative",
    "negative_spread": "spread is negative",
}
SAMPLE_LIMIT = 20


@dataclass
class ValidationReport:
    rows_checked: int
    rows_valid: int
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)

    @property
    def rows_rejected(self) -> int:
        return self.rows_checked - self.rows_valid


def validate(frame: pd.DataFrame, tf: Timeframe, *, now: datetime | None = None) -> tuple[pd.DataFrame, ValidationReport]:
    """Split a normalised frame (with a 'ts' column) into valid rows and a report."""
    n = len(frame)
    reasons = np.full(n, "", dtype=object)

    def mark(mask: np.ndarray, reason: str) -> None:
        free = (reasons == "") & mask
        reasons[free] = reason

    ts = frame["ts"]
    valid_ts = ts.notna().to_numpy()
    mark(~valid_ts, "bad_timestamp")
    if valid_ts.any():
        epochs = epoch_seconds(ts)
        mark(valid_ts & (epochs % tf.seconds != 0), "misaligned")
        if now is not None:
            mark(valid_ts & (epochs > int(now.timestamp())), "future")
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype="float64")
    mark(~np.isfinite(prices).all(axis=1), "non_finite_price")
    with np.errstate(invalid="ignore"):
        mark((prices <= 0).any(axis=1), "non_positive_price")
        o, h, low, c = prices.T
        ok = (low <= np.minimum(o, c)) & (np.maximum(o, c) <= h)
    mark(~ok, "ohlc_inconsistent")
    vol = frame["tick_volume"].astype("Float64")
    mark((vol < 0).fillna(False).to_numpy(dtype=bool), "negative_volume")
    spread = frame["spread"].to_numpy(dtype="float64")
    with np.errstate(invalid="ignore"):
        mark(np.isfinite(spread) & (spread < 0), "negative_spread")

    bad = reasons != ""
    counts = Counter(reasons[bad])
    samples = []
    for idx in np.flatnonzero(bad)[:SAMPLE_LIMIT]:
        stamp = frame["ts"].iloc[idx]
        samples.append(f"row {int(idx)}: {REASONS[reasons[idx]]} (ts={stamp if pd.notna(stamp) else 'n/a'})")
    report = ValidationReport(rows_checked=n, rows_valid=int((~bad).sum()), rejected_by_reason=dict(counts),
                              samples=samples)
    return frame[~bad].reset_index(drop=True), report


def epoch_seconds(stamps: pd.Series) -> np.ndarray:
    """UTC-aware datetimes as int64 epoch seconds (NaT becomes a sentinel; mask it)."""
    naive = stamps.dt.tz_convert("UTC").dt.tz_localize(None)
    return naive.to_numpy().astype("datetime64[s]").astype("int64")
