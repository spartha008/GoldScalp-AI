"""Fair value gaps (definition fvg@2). Observations, not trade zones.

Three CLOSED candles a (i-2), b (i-1), c (i):
- bullish FVG: low(c) > high(a); zone = [high(a), low(c)]
- bearish FVG: high(c) < low(a); zone = [high(c), low(a)]
Kept when size >= max(min_size_atr x ATR(c), min_size_abs). Known at the close of c.
De-duplication: a gap overlapping an earlier same-direction gap that is not yet fully mitigated by at
least ``overlap_dedupe`` of the smaller size is folded into the earlier one.
Mitigation uses only candles AFTER c and up to ``as_of``:
- bullish: depth = (top - lowest low since) / size; bearish: depth = (highest high since - bottom) / size
- unmitigated (depth 0), partially_mitigated (0 < depth < 1, %), fully_mitigated (traded through the zone).
A gap whose third candle is still forming is reported as DEVELOPING only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis.candles import Stamps, atr_series, candle_id, closed_view, fmt, forming_bar, rounded
from goldscalp.analysis.config import AnalysisConfig, default_config
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe

VERSION = "fvg@2"
EPS = 1e-9   # price comparisons tolerate float rounding (e.g. 101.6 - 101.5)


def analyse(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None = None, cfg: AnalysisConfig | None = None,
            *, limit: int | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    fcfg = cfg["fvg"]
    bars = closed_view(frame, tf, as_of)
    out: dict[str, Any] = {"definition": VERSION, "timeframe": tf.value, "as_of": iso(as_of) if as_of else None,
                           "gaps": [], "open": [], "developing": [], "filtered_small": 0, "merged": 0,
                           "params": {k: fcfg[k] for k in ("min_size_atr", "min_size_abs", "overlap_dedupe")}}
    if bars is None or len(bars) < 3:
        return out
    h, low = bars["high"].to_numpy(dtype="float64"), bars["low"].to_numpy(dtype="float64")
    times = Stamps(bars.index, tf)
    atr = atr_series(bars, int(cfg["volatility"]["atr_period"]))
    gaps: list[dict[str, Any]] = []
    small = merged = 0
    for i in range(2, len(bars)):
        for direction in ("bullish", "bearish"):
            if direction == "bullish" and low[i] > h[i - 2]:
                bottom, top = h[i - 2], low[i]
            elif direction == "bearish" and h[i] < low[i - 2]:
                bottom, top = h[i], low[i - 2]
            else:
                continue
            size = top - bottom
            minimum = max(float(fcfg["min_size_atr"]) * atr[i] if np.isfinite(atr[i]) else 0.0,
                          float(fcfg["min_size_abs"]))
            if size < minimum:
                small += 1
                continue
            parent = _overlapping(gaps, direction, bottom, top, i, float(fcfg["overlap_dedupe"]))
            if parent is not None:
                parent["merged_gaps"] += 1
                merged += 1
                continue
            gap = _gap(direction, bottom, top, i, times, tf, atr[i], minimum)
            _mitigate(gap, h, low, times, tf)
            gaps.append(gap)
    public = [{k: v for k, v in g.items() if not k.startswith("_")} for g in gaps]
    cap = limit if limit is not None else int(fcfg["max_returned"])
    out.update(gaps=public[-cap:] if cap else public,
               open=[g for g in public if g["mitigation"] != "fully_mitigated"][-cap:] if cap else
               [g for g in public if g["mitigation"] != "fully_mitigated"],
               developing=_developing(bars, frame, tf, as_of, atr, fcfg), filtered_small=small, merged=merged)
    return out


def _overlapping(gaps: list[dict[str, Any]], direction: str, bottom: float, top: float, i: int,
                 threshold: float) -> dict[str, Any] | None:
    for g in reversed(gaps):
        if g["direction"] != direction:
            continue
        if g["_full_index"] is not None and g["_full_index"] < i:     # already fully mitigated before this gap
            continue
        overlap = min(top, g["zone"]["top"]) - max(bottom, g["zone"]["bottom"])
        if overlap > 0 and overlap >= threshold * min(top - bottom, g["size"]) - EPS:
            return g
    return None


def _gap(direction: str, bottom: float, top: float, i: int, times: Stamps, tf: Timeframe, atr: float,
         minimum: float) -> dict[str, Any]:
    size = top - bottom
    word = "above" if direction == "bullish" else "below"
    first = "high" if direction == "bullish" else "low"
    third = "low" if direction == "bullish" else "high"
    return {"id": f"{tf.value}:fvg:{direction}:{times.open[i - 1]}", "kind": "fvg", "type": "fvg",
            "label": f"{direction.capitalize()} FVG", "direction": direction, "timeframe": tf.value,
            "time": times.open[i - 1], "zone": {"top": rounded(top), "bottom": rounded(bottom)},
            "price": rounded((top + bottom) / 2), "size": rounded(size),
            "size_atr": rounded(size / atr, 2) if np.isfinite(atr) and atr > 0 else None, "status": "confirmed",
            "known_at": times.close[i],
            "candles": [times.cid(i - 2), times.cid(i - 1), times.cid(i)],
            "explanation": (f"{direction.capitalize()} fair value gap {fmt(bottom)}-{fmt(top)} (size {fmt(size)}, "
                            f"minimum {fmt(minimum)}): the {third} of the candle at {times.open[i]} "
                            f"stayed {word} the {first} of the candle at {times.open[i - 2]}. "
                            "An observed price imbalance, not a prediction."),
            "mitigation": "unmitigated", "mitigated_pct": 0.0, "mitigation_note": "price has not returned into the gap",
            "first_touch_at": None, "fully_mitigated_at": None,
            "merged_gaps": 0, "definition": VERSION, "_index": i, "_full_index": None}


def _mitigate(gap: dict[str, Any], h: np.ndarray, low: np.ndarray, times: Stamps, tf: Timeframe) -> None:
    i, top, bottom = gap["_index"], gap["zone"]["top"], gap["zone"]["bottom"]
    size = top - bottom
    after_h, after_l = h[i + 1:], low[i + 1:]
    if not len(after_h):
        return
    if gap["direction"] == "bullish":
        touch, full = after_l <= top, after_l <= bottom
        depth = (top - after_l.min()) / size
    else:
        touch, full = after_h >= bottom, after_h >= top
        depth = (after_h.max() - bottom) / size
    if touch.any():
        gap["first_touch_at"] = times.close[i + 1 + int(touch.argmax())]
    if full.any():
        k = i + 1 + int(full.argmax())
        gap["_full_index"] = k
        gap["fully_mitigated_at"] = times.close[k]
        gap["mitigation"], gap["mitigated_pct"] = "fully_mitigated", 100.0
    elif depth > 0:
        gap["mitigation"], gap["mitigated_pct"] = "partially_mitigated", round(float(min(depth, 1.0)) * 100, 1)
    gap["mitigation_note"] = {"unmitigated": "price has not returned into the gap",
                          "partially_mitigated": f"price has filled {gap['mitigated_pct']}% of the gap",
                          "fully_mitigated": "price has traded through the whole gap"}[gap["mitigation"]]


def _developing(bars: pd.DataFrame, frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None, atr: np.ndarray,
                fcfg: dict[str, Any]) -> list[dict[str, Any]]:
    bar = forming_bar(frame, tf, as_of)
    if bar is None or len(bars) < 2:
        return []
    a = bars.iloc[-2]
    minimum = max(float(fcfg["min_size_atr"]) * atr[-1] if np.isfinite(atr[-1]) else 0.0, float(fcfg["min_size_abs"]))
    out = []
    for direction, bottom, top in (("bullish", float(a["high"]), float(bar["low"])),
                                   ("bearish", float(bar["high"]), float(a["low"]))):
        if top - bottom >= minimum:
            out.append({"id": f"{tf.value}:fvg:{direction}:developing", "kind": "fvg", "type": "fvg",
                        "label": f"Developing {direction} FVG", "direction": direction, "timeframe": tf.value,
                        "time": iso(bars.index[-1].to_pydatetime()),
                        "zone": {"top": rounded(top), "bottom": rounded(bottom)}, "status": "developing",
                        "known_at": None, "candles": [candle_id(tf, bars.index[-2]), candle_id(tf, bars.index[-1]),
                                                      candle_id(tf, bar.name)],
                        "explanation": "the third candle is still forming; the gap exists only if it closes this way",
                        "definition": VERSION})
    return out
