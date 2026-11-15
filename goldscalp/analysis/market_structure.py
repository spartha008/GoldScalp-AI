"""Market structure engine (definition structure@2).

Causal walk over CLOSED candles (candles.closed_view). At each closed candle t:
1. Confirm the swing whose right-hand window ends at t:
   swing high at i = t - k: high[i] > max(high[i-k .. i-1]) and high[i] >= max(high[i+1 .. t]);
   swing low  at i      : low[i]  < min(low[i-k .. i-1])  and low[i]  <= min(low[i+1 .. t]).
   It is KNOWN at the close of candle t (k = swing strength, configurable per timeframe).
   Labels: a swing high is HH if above the previous confirmed swing high, otherwise LH;
           a swing low  is HL if above the previous confirmed swing low,  otherwise LL.
2. Check the close of t against the most recent confirmed, unbroken swing:
   close > swing high + buffer -> bullish break; close < swing low - buffer -> bearish break
   (buffer = break_buffer_atr x ATR). A break in the direction of the prevailing break bias is a
   BOS; against it, a CHoCH. Wick-only pierces are not breaks.
   De-duplication: a same-direction BOS within dedupe_bars candles of the previous one whose level is
   within dedupe_atr x ATR of it is folded into that event (counted, not re-labelled).
State: bullish if the last break was bullish and the latest confirmed swing low is not an LL;
bearish if the last break was bearish and the latest swing high is not an HH; otherwise ranging
(also ranging when no break happened for ranging_after_bars candles).
Developing observations (swing candidates whose window is incomplete, a forming candle beyond a
level) are labelled "developing" and never become events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from numpy.lib.stride_tricks import sliding_window_view

from goldscalp.analysis.candles import Stamps, atr_series, candle_id, closed_view, fmt, forming_bar, rounded
from goldscalp.analysis.config import AnalysisConfig, default_config
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe

VERSION = "structure@2"


def analyse(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None = None, cfg: AnalysisConfig | None = None,
            *, limit: int | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    scfg = cfg["structure"]
    k = int(scfg["swing_strength"][tf.value])
    bars = closed_view(frame, tf, as_of)
    out = _walk(bars, tf, k, scfg, int(cfg["volatility"]["atr_period"]))
    out["developing"] = _developing(bars, frame, tf, as_of, k, out)
    cap = limit if limit is not None else int(scfg["max_events_returned"])
    out["events"] = out["events"][-cap:] if cap else out["events"]
    out["swings"] = out["swings"][-cap:] if cap else out["swings"]
    out.update(definition=VERSION, timeframe=tf.value, as_of=iso(as_of) if as_of else None,
               params={"swing_strength": k, "break_buffer_atr": scfg["break_buffer_atr"],
                       "dedupe_atr": scfg["dedupe_atr"], "dedupe_bars": scfg["dedupe_bars"],
                       "ranging_after_bars": scfg["ranging_after_bars"]})
    return out


def _walk(bars: pd.DataFrame, tf: Timeframe, k: int, scfg: dict[str, Any], atr_period: int) -> dict[str, Any]:
    n = len(bars)
    swings: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"bars_used": n, "first_bar": None, "last_closed_bar": None, "swings": swings,
                               "events": events, "state": "ranging", "state_explanation": [], "bias_from_breaks": None,
                               "last_high_label": None, "last_low_label": None, "last_swing_high": None,
                               "last_swing_low": None, "reference_high": None, "reference_low": None,
                               "dedupe_folded": 0, "bars_since_break": None}
    if n == 0:
        summary["state_explanation"] = ["no closed candles available"]
        return summary
    highs, lows = bars["high"].to_numpy(dtype="float64"), bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")
    times = Stamps(bars.index, tf)
    atr = atr_series(bars, atr_period)
    # swing tests, vectorized: high[i] > max(left k) and high[i] >= max(right k) (lows mirrored)
    is_sh = np.zeros(n, dtype=bool)
    is_sl = np.zeros(n, dtype=bool)
    if k >= 1 and n >= 2 * k + 1:
        wmax = sliding_window_view(highs, k).max(axis=1)      # wmax[j] = max(highs[j:j+k])
        wmin = sliding_window_view(lows, k).min(axis=1)
        idx = np.arange(k, n - k)
        is_sh[idx] = (highs[idx] > wmax[idx - k]) & (highs[idx] >= wmax[idx + 1])
        is_sl[idx] = (lows[idx] < wmin[idx - k]) & (lows[idx] <= wmin[idx + 1])
    prev_high = prev_low = None
    ref_high: dict[str, Any] | None = None
    ref_low: dict[str, Any] | None = None
    bias: str | None = None
    last_break_index: int | None = None
    folded = 0

    for t in range(n):
        i = t - k
        if i - k >= 0:
            if is_sh[i]:
                label = "H" if prev_high is None else ("HH" if highs[i] > prev_high else "LH")
                ref_high = _swing("swing_high", label, highs[i], times, i, t, tf, prev_high)
                swings.append(ref_high)
                prev_high = highs[i]
            if is_sl[i]:
                label = "L" if prev_low is None else ("HL" if lows[i] > prev_low else "LL")
                ref_low = _swing("swing_low", label, lows[i], times, i, t, tf, prev_low)
                swings.append(ref_low)
                prev_low = lows[i]
        buffer = float(scfg["break_buffer_atr"]) * atr[t] if np.isfinite(atr[t]) else 0.0
        for swing, direction in ((ref_high, "bullish"), (ref_low, "bearish")):
            if swing is None or swing["broken"]:
                continue
            crossed = closes[t] > swing["price"] + buffer if direction == "bullish" else closes[t] < swing["price"] - buffer
            if not crossed:
                continue
            swing["broken"] = True
            kind = "CHoCH" if bias is not None and bias != direction else "BOS"
            previous = events[-1] if events and events[-1]["direction"] == direction else None
            if (kind == "BOS" and previous is not None
                    and t - previous["_index"] <= int(scfg["dedupe_bars"]) and np.isfinite(atr[t])
                    and abs(swing["price"] - previous["price"]) <= float(scfg["dedupe_atr"]) * atr[t]):
                previous["folded_breaks"] += 1
                folded += 1
            else:
                events.append(_break(kind, direction, swing, t, times, closes[t], buffer, tf, bias))
            bias = direction
            last_break_index = t

    summary.update(first_bar=times.open[0], last_closed_bar=times.open[-1],
                   bias_from_breaks=bias, dedupe_folded=folded)
    last_high = next((s for s in reversed(swings) if s["type"] == "swing_high"), None)
    last_low = next((s for s in reversed(swings) if s["type"] == "swing_low"), None)
    summary["last_high_label"] = last_high["label"] if last_high else None
    summary["last_low_label"] = last_low["label"] if last_low else None
    summary["last_swing_high"] = _public(last_high)
    summary["last_swing_low"] = _public(last_low)
    summary["reference_high"] = _public(ref_high) if ref_high and not ref_high["broken"] else None
    summary["reference_low"] = _public(ref_low) if ref_low and not ref_low["broken"] else None
    summary["bars_since_break"] = (n - 1 - last_break_index) if last_break_index is not None else None
    after = [s["label"] for s in swings if events and s["known_at"] > events[-1]["known_at"]]
    state, why = _classify(bias, summary["last_high_label"], summary["last_low_label"], summary["bars_since_break"],
                           int(scfg["ranging_after_bars"]), events, after)
    summary["state"], summary["state_explanation"] = state, why
    summary["swings"] = [_public(s) for s in swings]
    summary["events"] = [{k2: v for k2, v in e.items() if not k2.startswith("_")} for e in events]
    return summary


def _classify(bias: str | None, high_label: str | None, low_label: str | None, since: int | None,
              ranging_after: int, events: list[dict[str, Any]], after: list[str]) -> tuple[str, list[str]]:
    """``after``: labels of swings confirmed after the last break (an opposing HH/LL there = mixed)."""
    why: list[str] = []
    if bias is None:
        return "ranging", ["no confirmed break of structure yet"]
    last = events[-1] if events else None
    if last is not None:
        why.append(f"last break: {last['type']} {last['direction']} through {fmt(last['price'])} "
                   f"(known {last['known_at']})")
    if since is not None and since > ranging_after:
        why.append(f"no structure break for {since} candles (> {ranging_after})")
        return "ranging", why
    if bias == "bullish":
        if "LL" in after:
            why.append("but a lower low (LL) was confirmed after that break: structure is mixed")
            return "ranging", why
        why.append(f"swing sequence: latest high {high_label or 'n/a'}, latest low {low_label or 'n/a'}")
        return "bullish", why
    if "HH" in after:
        why.append("but a higher high (HH) was confirmed after that break: structure is mixed")
        return "ranging", why
    why.append(f"swing sequence: latest high {high_label or 'n/a'}, latest low {low_label or 'n/a'}")
    return "bearish", why


def _swing(kind: str, label: str, price: float, times: Stamps, i: int, t: int, tf: Timeframe,
           previous: float | None) -> dict[str, Any]:
    side = "high" if kind == "swing_high" else "low"
    if previous is None:
        relation = f"first confirmed swing {side}"
    else:
        relation = f"{'above' if price > previous else 'at or below'} the previous swing {side} {fmt(previous)}"
    return {"id": f"{tf.value}:{kind}:{times.open[i]}", "kind": "structure", "type": kind, "label": label,
            "direction": "bearish" if kind == "swing_high" else "bullish", "timeframe": tf.value,
            "time": times.open[i], "price": rounded(price), "status": "confirmed",
            "known_at": times.close[t],
            "explanation": f"{label}: swing {side} {fmt(price)}, {relation}; confirmed after {t - i} later candles",
            "candles": [times.cid(i), times.cid(t)], "broken": False, "definition": VERSION}


def _break(kind: str, direction: str, swing: dict[str, Any], t: int, times: Stamps, close: float,
           buffer: float, tf: Timeframe, bias: str | None) -> dict[str, Any]:
    side = "above" if direction == "bullish" else "below"
    if bias is None:
        context = "the first structure break in the available candles"
    elif kind == "BOS":
        context = f"continuing the prevailing {direction} structure"
    else:
        context = f"against the prevailing {bias} structure (change of character)"
    return {"id": f"{tf.value}:{kind}:{direction}:{times.open[t]}", "kind": "structure", "type": kind,
            "label": kind, "direction": direction, "timeframe": tf.value, "time": times.open[t],
            "price": swing["price"], "close": rounded(close), "status": "confirmed",
            "known_at": times.close[t], "swing_time": swing["time"], "swing_label": swing["label"],
            "explanation": (f"{kind}: candle closed {fmt(close)} {side} the confirmed swing "
                            f"{'high' if direction == 'bullish' else 'low'} {fmt(swing['price'])} "
                            f"({swing['label']} from {swing['time']}, buffer {fmt(buffer, 3)}), {context}"),
            "candles": [swing["candles"][0], times.cid(t)], "folded_breaks": 0, "definition": VERSION,
            "_index": t}


def _public(swing: dict[str, Any] | None) -> dict[str, Any] | None:
    if swing is None:
        return None
    return {k: v for k, v in swing.items() if not k.startswith("_")}


def _developing(bars: pd.DataFrame, frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None, k: int,
                summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Observations that are NOT confirmed yet. Never used for events, signals or statistics."""
    out: list[dict[str, Any]] = []
    n = len(bars)
    if n > k:
        highs, lows = bars["high"].to_numpy(dtype="float64"), bars["low"].to_numpy(dtype="float64")
        for i in range(max(k, n - k), n):
            later = n - 1 - i
            if highs[i] > highs[i - k:i].max() and (later == 0 or highs[i] >= highs[i + 1:].max()):
                out.append(_candidate("swing_high", highs[i], bars.index[i], tf, k - later))
            if lows[i] < lows[i - k:i].min() and (later == 0 or lows[i] <= lows[i + 1:].min()):
                out.append(_candidate("swing_low", lows[i], bars.index[i], tf, k - later))
    bar = forming_bar(frame, tf, as_of)
    if bar is not None:
        for ref, direction in ((summary["reference_high"], "bullish"), (summary["reference_low"], "bearish")):
            if ref is None:
                continue
            price = float(bar["close"])
            beyond = price > ref["price"] if direction == "bullish" else price < ref["price"]
            if beyond:
                out.append({"id": f"{tf.value}:developing_break:{direction}", "kind": "structure",
                            "type": "developing_break", "direction": direction, "timeframe": tf.value,
                            "time": iso(bar.name.to_pydatetime()), "price": ref["price"], "status": "developing",
                            "known_at": None,
                            "explanation": (f"the forming candle trades at {fmt(price)} beyond the swing "
                                            f"{'high' if direction == 'bullish' else 'low'} {fmt(ref['price'])}; "
                                            "it only becomes a break if the candle CLOSES beyond it"),
                            "candles": [candle_id(tf, bar.name)], "definition": VERSION})
    return out


def _candidate(kind: str, price: float, at: pd.Timestamp, tf: Timeframe, needed: int) -> dict[str, Any]:
    side = "high" if kind == "swing_high" else "low"
    return {"id": f"{tf.value}:candidate_{kind}:{iso(at.to_pydatetime())}", "kind": "structure",
            "type": f"candidate_{kind}", "direction": "bearish" if kind == "swing_high" else "bullish",
            "timeframe": tf.value, "time": iso(at.to_pydatetime()), "price": rounded(price), "status": "developing",
            "known_at": None,
            "explanation": (f"possible swing {side} {fmt(price)}: needs {needed} more closed candle(s) without a "
                            f"{'higher high' if side == 'high' else 'lower low'} before it is confirmed"),
            "candles": [candle_id(tf, at)], "definition": VERSION}
