"""Indicator library for research screens (pre-Phase 7 stage 2). Pure functions on closed candles.

No look-ahead by construction: every value at position i uses candles up to and including i only. Higher-timeframe and
daily values are made visible to a faster timeline only once their own bar has closed (``known_at``).
All candle indexes are UTC open times.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

NY = "America/New_York"
EPOCH = pd.Timestamp(1970, 1, 1)


# ------------------------------------------------------------------ moving averages and oscillators
def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def ema(x: pd.Series, n: int) -> pd.Series:
    """Standard EMA (alpha = 2 / (n + 1)), seeded with the SMA of the first n values."""
    out = pd.Series(np.nan, index=x.index, dtype="float64")
    v = x.to_numpy(dtype="float64")
    if len(v) < n:
        return out
    a = 2.0 / (n + 1)
    acc = float(np.mean(v[:n]))
    res = np.full(len(v), np.nan)
    res[n - 1] = acc
    for i in range(n, len(v)):
        acc = a * v[i] + (1 - a) * acc
        res[i] = acc
    return pd.Series(res, index=x.index)


def wilder(x: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing: SMA seed over the first n values, then prev + (x - prev) / n."""
    v = x.to_numpy(dtype="float64")
    res = np.full(len(v), np.nan)
    start = int(np.argmax(~np.isnan(v))) if np.isnan(v).any() else 0
    if len(v) - start < n:
        return pd.Series(res, index=x.index)
    acc = float(np.mean(v[start:start + n]))
    res[start + n - 1] = acc
    for i in range(start + n, len(v)):
        acc += (v[i] - acc) / n
        res[i] = acc
    return pd.Series(res, index=x.index)


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder's RSI. 100 when there were no losses in the smoothing window, 0 when there were no gains."""
    d = close.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn != 0, 100.0).where(~((up == 0) & (dn == 0)), 50.0)
    return out.where(up.notna() & dn.notna())


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1).where(prev.notna(), high - low)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's average true range."""
    return wilder(true_range(high, low, close), n)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(middle, upper, lower) with the population standard deviation, as Bollinger defines the bands."""
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def percent_b(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    _, up, lo = bollinger(close, n, k)
    return (close - lo) / (up - lo).replace(0, np.nan)


def keltner(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 20, mult: float = 1.5) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(middle, upper, lower): SMA(n) +/- mult x the simple average true range over n bars (the TTM squeeze form)."""
    mid = sma(close, n)
    rng = true_range(high, low, close).rolling(n, min_periods=n).mean()
    return mid, mid + mult * rng, mid - mult * rng


def linreg_value(x: pd.Series, n: int) -> pd.Series:
    """The end point of an n-bar least-squares line fitted to x (the 'linear regression value')."""
    t = np.arange(n, dtype="float64")
    tm, denom = t.mean(), float(((t - t.mean()) ** 2).sum())

    def last(w: np.ndarray) -> float:
        slope = float(((t - tm) * (w - w.mean())).sum()) / denom
        return float(w.mean() + slope * (n - 1 - tm))
    return x.rolling(n, min_periods=n).apply(last, raw=True)


def squeeze_momentum(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 20) -> pd.Series:
    """TTM squeeze histogram: linear-regression value of close - ((highest high + lowest low) / 2 + SMA) / 2."""
    mid = (high.rolling(n, min_periods=n).max() + low.rolling(n, min_periods=n).min()) / 2
    return linreg_value(close - (mid + sma(close, n)) / 2, n)


# ------------------------------------------------------------------ bars of other sizes
OHLC = {"open": "first", "high": "max", "low": "min", "close": "last"}


def resample(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Bars of ``minutes`` built from faster bars, indexed by their open time. Empty periods are dropped. A bar here may
    be incomplete at the end of the data; use ``known_at`` so nothing sees a bar before it has closed."""
    agg = {**OHLC, **({"tick_volume": "sum"} if "tick_volume" in bars else {})}
    out = bars.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


def known_at(values: pd.Series, minutes: int, base_index: pd.DatetimeIndex, base_minutes: int) -> np.ndarray:
    """``values`` (indexed by the open time of ``minutes`` bars) as seen at each base bar's CLOSE: the latest value
    whose own bar had closed by then."""
    closed = pd.Series(values.to_numpy(), index=values.index + pd.Timedelta(minutes=minutes))
    return closed.reindex(base_index + pd.Timedelta(minutes=base_minutes), method="ffill").to_numpy()


def closes_now(htf_index: pd.DatetimeIndex, minutes: int, base_index: pd.DatetimeIndex, base_minutes: int) -> np.ndarray:
    """True where a base bar's close coincides with the close of a ``minutes`` bar."""
    return np.asarray((base_index + pd.Timedelta(minutes=base_minutes)).isin(htf_index + pd.Timedelta(minutes=minutes)))


# ------------------------------------------------------------------ trading days and clock times
def trading_day(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The gold trading day of each bar: days roll over at 17:00 New York (daylight-saving aware). A bar that opens at or
    after 17:00 New York belongs to the next day."""
    return (index.tz_convert(NY) + pd.Timedelta(hours=7)).tz_localize(None).normalize()


def day_end(day: pd.Timestamp) -> pd.Timestamp:
    """The 17:00 New York rollover that ends trading day ``day`` (UTC)."""
    return (pd.Timestamp(day) + pd.Timedelta(hours=17)).tz_localize(NY).tz_convert("UTC")


def daily_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """One row per trading day (17:00 New York rollover): open, high, low, close."""
    return bars.groupby(trading_day(bars.index)).agg(OHLC)


def local_clock(index: pd.DatetimeIndex, tz: str, offset_hours: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(minute of the day, calendar day number) of each timestamp in ``tz`` (plus a fixed offset, for a server clock)."""
    naive = index.tz_convert(tz).tz_localize(None) + pd.Timedelta(hours=offset_hours)
    days = (naive.normalize() - EPOCH) // pd.Timedelta(days=1)
    return np.asarray(naive.hour * 60 + naive.minute, dtype="int64"), np.asarray(days, dtype="int64")


def at_local(day_number: int, minute: int, tz: str, offset_hours: int = 0) -> pd.Timestamp:
    """The UTC time of ``minute`` of the day on calendar day ``day_number`` in ``tz`` (plus the fixed offset)."""
    naive = EPOCH + pd.Timedelta(days=int(day_number), minutes=int(minute)) - pd.Timedelta(hours=offset_hours)
    return naive.tz_localize(tz, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")


def hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def session_vwap(bars: pd.DataFrame, in_session: np.ndarray, session_id: np.ndarray) -> np.ndarray:
    """Cumulative typical-price VWAP within each session, weighted by TICK volume (a proxy: spot gold has no traded
    volume). NaN outside the session."""
    tp = ((bars["high"] + bars["low"] + bars["close"]) / 3).to_numpy()
    vol = bars["tick_volume"].to_numpy(dtype="float64") if "tick_volume" in bars else np.ones(len(bars))
    out = np.full(len(bars), np.nan)
    pv = v = 0.0
    last: Any = None
    for i in range(len(bars)):
        if not in_session[i]:
            continue
        if session_id[i] != last:
            pv, v, last = 0.0, 0.0, session_id[i]
        pv += tp[i] * vol[i]
        v += vol[i]
        out[i] = pv / v if v > 0 else np.nan
    return out
