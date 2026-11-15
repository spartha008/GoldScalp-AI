"""Volatility measures (definition vol@1).

- ATR(14), Wilder smoothing, on CLOSED bars of each timeframe.
    TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)
    ATR  = mean(TR_1..TR_14), then ATR_t = (ATR_{t-1} x 13 + TR_t) / 14
- Trading day rolls at 17:00 America/New_York (day key = local time + 7 h).
- ADR(20) = mean(high - low) of the last 20 COMPLETE trading days.
- Realized volatility = standard deviation of H1 log returns over the last 24
  closed bars, in percent (not annualised).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis.candles import atr_series, closed_view
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import Timeframe

VERSION = "vol@1"
ATR_PERIOD = 14
ADR_DAYS = 20
RV_BARS = 24


def atr(frame: pd.DataFrame, period: int = ATR_PERIOD) -> float | None:
    closed = frame[frame["closed"]] if "closed" in frame else frame
    if len(closed) < period + 1:
        return None
    h, low, c = (closed[k].to_numpy(dtype="float64") for k in ("high", "low", "close"))
    prev = c[:-1]
    tr = np.maximum.reduce([h[1:] - low[1:], np.abs(h[1:] - prev), np.abs(low[1:] - prev)])
    value = float(tr[:period].mean())
    for x in tr[period:]:
        value = (value * (period - 1) + float(x)) / period
    return value


def trading_days(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLC on the 17:00 New York rollover."""
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "bars"])
    local = frame.index.tz_convert("America/New_York")
    keys = (local + pd.Timedelta(hours=7)).date
    grouped = frame.groupby(keys, sort=True)
    return pd.DataFrame({"open": grouped["open"].first(), "high": grouped["high"].max(),
                         "low": grouped["low"].min(), "close": grouped["close"].last(),
                         "bars": grouped["open"].size()})


def summary(frames: dict[str, pd.DataFrame], hourly: pd.DataFrame, now: datetime) -> dict[str, Any]:
    now = ensure_aware(now)
    atrs = {tf: _round(atr(df)) for tf, df in frames.items()}
    days = trading_days(hourly)
    today_key = (pd.Timestamp(now).tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    complete = days[days.index < today_key]
    adr = float((complete["high"] - complete["low"]).tail(ADR_DAYS).mean()) if len(complete) else None
    today = days.loc[today_key] if today_key in days.index else None
    today_range = float(today["high"] - today["low"]) if today is not None else None
    closed = hourly[hourly["closed"]] if "closed" in hourly else hourly
    rv = None
    if len(closed) > RV_BARS:
        rets = np.diff(np.log(closed["close"].to_numpy(dtype="float64")[-(RV_BARS + 1):]))
        rv = float(np.std(rets, ddof=1) * 100)
    return {
        "definition": VERSION, "as_of": iso(now), "atr14": atrs,
        "adr20": _round(adr), "adr_days_used": int(min(len(complete), ADR_DAYS)),
        "today_range": _round(today_range),
        "today_range_pct_of_adr": _round(today_range / adr * 100 if adr and today_range is not None else None, 1),
        "realized_vol_h1_pct": _round(rv, 4), "realized_vol_bars": RV_BARS,
    }


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


# ---------------------------------------------------------------------------------------------
# vol@2: configurable (config/analysis.json), per timeframe, price units AND % of price.
#   ATR (Wilder, ``atr_period``); expansion/contraction = fast ATR / slow ATR
#   (>= expansion_ratio: expanding, <= contraction_ratio: contracting, else normal);
#   rolling volatility = std of log returns over ``rolling_window`` closed bars;
#   last candle range and its percentile among the previous ``range_lookback`` ranges;
#   average body, average range, body/range ratio over ``rolling_window`` bars;
#   ADR = mean daily range of the last ``adr_days`` COMPLETE trading days (17:00 New York rollover).
# Only closed candles up to ``as_of`` are used.
# ---------------------------------------------------------------------------------------------
VERSION2 = "vol@2"


def wilder(frame: pd.DataFrame, period: int) -> float | None:
    values = atr_series(frame, period)
    return float(values[-1]) if len(values) and np.isfinite(values[-1]) else None


def timeframe_profile(bars: pd.DataFrame, cfg: Any) -> dict[str, Any]:
    vcfg = cfg["volatility"]
    out: dict[str, Any] = {"bars": int(len(bars))}
    if len(bars) < 2:
        out["condition"], out["explanation"] = "unknown", "not enough closed candles"
        return out
    last = float(bars["close"].iloc[-1])
    atr_v = wilder(bars, int(vcfg["atr_period"]))
    fast, slow = wilder(bars, int(vcfg["fast_atr"])), wilder(bars, int(vcfg["slow_atr"]))
    window = bars.tail(int(vcfg["rolling_window"]))
    rets = np.diff(np.log(bars["close"].to_numpy(dtype="float64")[-(int(vcfg["rolling_window"]) + 1):]))
    ranges = (bars["high"] - bars["low"]).to_numpy(dtype="float64")
    lookback = ranges[-(int(vcfg["range_lookback"]) + 1):-1]
    body = (window["close"] - window["open"]).abs()
    rng = window["high"] - window["low"]
    out.update({
        "last_close": _round(last, 5), "atr": _round(atr_v), "atr_pct": _round(atr_v / last * 100 if atr_v else None, 4),
        "fast_atr": _round(fast), "slow_atr": _round(slow),
        "rolling_vol_pct": _round(float(np.std(rets, ddof=1) * 100) if len(rets) > 2 else None, 4),
        "rolling_vol_price": _round(float(np.std(np.diff(window["close"].to_numpy(dtype="float64")), ddof=1))
                                    if len(window) > 3 else None),
        "last_range": _round(float(ranges[-1])), "last_range_pct": _round(float(ranges[-1]) / last * 100, 4),
        "last_range_percentile": _round(float((lookback <= ranges[-1]).mean() * 100) if len(lookback) else None, 1),
        "avg_body": _round(float(body.mean())), "avg_range": _round(float(rng.mean())),
        "body_to_range": _round(float(body.sum() / rng.sum()) if float(rng.sum()) > 0 else None, 3),
    })
    if fast and slow:
        ratio = fast / slow
        out["fast_slow_ratio"] = _round(ratio, 3)
        if ratio >= float(vcfg["expansion_ratio"]):
            out["condition"] = "expanding"
        elif ratio <= float(vcfg["contraction_ratio"]):
            out["condition"] = "contracting"
        else:
            out["condition"] = "normal"
        out["explanation"] = (f"ATR({vcfg['fast_atr']}) {fast:.2f} vs ATR({vcfg['slow_atr']}) {slow:.2f}: ratio "
                              f"{ratio:.2f} ({out['condition']}; thresholds {vcfg['contraction_ratio']} / "
                              f"{vcfg['expansion_ratio']})")
    else:
        out["condition"] = "unknown"
        out["explanation"] = f"needs more than {vcfg['slow_atr']} closed candles for the expansion check"
    return out


def analyse(frames: dict[str, pd.DataFrame], as_of: datetime, cfg: Any, *, daily_source: pd.DataFrame | None = None,
            daily_tf: Any = None, sessions_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """``frames``: {timeframe value: candles}. ``daily_source`` (normally H1) gives ADR and today's range."""
    as_of = ensure_aware(as_of)
    per_tf = {}
    for key, frame in frames.items():
        tf = Timeframe.parse(key)
        bars = closed_view(frame, tf, as_of)
        per_tf[key] = timeframe_profile(bars if bars is not None else frame.iloc[0:0], cfg)
    adr_days = int(cfg["volatility"]["adr_days"])
    result: dict[str, Any] = {"definition": VERSION2, "as_of": iso(as_of), "timeframes": per_tf,
                              "params": dict(cfg["volatility"])}
    if daily_source is not None and daily_tf is not None:
        hourly = closed_view(daily_source, daily_tf, as_of)
        days = trading_days(hourly) if hourly is not None and len(hourly) else trading_days(daily_source.iloc[0:0])
        today_key = (pd.Timestamp(as_of).tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
        complete = days[days.index < today_key].tail(adr_days)
        adr = float((complete["high"] - complete["low"]).mean()) if len(complete) else None
        today = days.loc[today_key] if today_key in days.index else None
        today_range = float(today["high"] - today["low"]) if today is not None else None
        last = float(hourly["close"].iloc[-1]) if hourly is not None and len(hourly) else None
        result["adr"] = {"days_used": int(len(complete)), "adr": _round(adr),
                         "adr_pct": _round(adr / last * 100 if adr and last else None, 4),
                         "today_range": _round(today_range),
                         "today_pct_of_adr": _round(today_range / adr * 100 if adr and today_range is not None else None, 1),
                         "source_timeframe": daily_tf.value}
    if sessions_result:
        result["sessions"] = [{"name": s["name"], "status": s["status"], "range": s.get("range"),
                               "ratio": s["compare"].get("ratio"), "note": s["compare"].get("note")}
                              for s in sessions_result.get("sessions", [])]
    return result
