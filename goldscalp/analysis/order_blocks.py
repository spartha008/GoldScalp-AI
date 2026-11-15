"""Order-block CANDIDATES (definition ob@1). Algorithmic candidates, not institutional facts.

For every confirmed BOS/CHoCH from structure@2 (break candle t, broken swing at s):
- bullish break: the leg's extreme is the lowest low between s and t (index m). The candidate is the last
  BEARISH candle (close < open) at or before m, searching back at most ``search_bars`` candles from t.
- bearish break: mirror (highest high, last BULLISH candle).
- zone: the candle's full range (``zone = "range"``) or its body (``"body"``).
- displacement: the break candle's close must be at least ``displacement_atr`` x ATR beyond the zone
  (above its top for bullish, below its bottom for bearish); otherwise no candidate is reported.
- known at the BREAK candle's close (never earlier), linked to that structure event.
Mitigation with candles after the break, up to ``as_of``: untouched -> touched (price trades back into
the zone) -> invalidated (a close beyond the far side of the zone).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis import market_structure
from goldscalp.analysis.candles import Stamps, atr_series, closed_view, fmt, rounded
from goldscalp.analysis.config import AnalysisConfig, default_config
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe

VERSION = "ob@1"
LABEL = "algorithmic candidate"


def analyse(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None = None, cfg: AnalysisConfig | None = None,
            *, structure: dict[str, Any] | None = None, limit: int | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    ocfg = cfg["order_blocks"]
    bars = closed_view(frame, tf, as_of)
    out: dict[str, Any] = {"definition": VERSION, "timeframe": tf.value, "as_of": iso(as_of) if as_of else None,
                           "candidates": [], "open": [], "skipped": {"no_opposite_candle": 0, "weak_displacement": 0,
                                                                     "no_atr": 0, "duplicate": 0},
                           "label": LABEL,
                           "params": {k: ocfg[k] for k in ("displacement_atr", "zone", "search_bars")}}
    if bars is None or len(bars) < 3:
        return out
    if structure is None:
        structure = market_structure.analyse(frame, tf, as_of, cfg, limit=0)
    o, h, low, c = (bars[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    times = Stamps(bars.index, tf)
    pos = {s: i for i, s in enumerate(times.open)}
    atr = atr_series(bars, int(cfg["volatility"]["atr_period"]))
    search, need = int(ocfg["search_bars"]), float(ocfg["displacement_atr"])
    used: set[int] = set()
    found: list[dict[str, Any]] = []
    for event in structure["events"]:
        if event["type"] not in ("BOS", "CHoCH") or event["time"] not in pos or event["swing_time"] not in pos:
            continue
        t, s = pos[event["time"]], pos[event["swing_time"]]
        if not np.isfinite(atr[t]):
            out["skipped"]["no_atr"] += 1
            continue
        bullish = event["direction"] == "bullish"
        lo_i = max(0, t - search)
        leg = slice(s + 1, t + 1)
        m = s + 1 + int(np.argmin(low[leg]) if bullish else np.argmax(h[leg]))
        j = next((k for k in range(min(m, t - 1), lo_i - 1, -1) if (c[k] < o[k] if bullish else c[k] > o[k])), None)
        if j is None:
            out["skipped"]["no_opposite_candle"] += 1
            continue
        if j in used:
            out["skipped"]["duplicate"] += 1
            continue
        if ocfg["zone"] == "body":
            bottom, top = min(o[j], c[j]), max(o[j], c[j])
        else:
            bottom, top = low[j], h[j]
        displacement = (c[t] - top) if bullish else (bottom - c[t])
        if displacement < need * atr[t]:
            out["skipped"]["weak_displacement"] += 1
            continue
        used.add(j)
        found.append(_candidate(event, bullish, j, t, bottom, top, displacement, atr[t], times, tf, ocfg["zone"],
                                h, low, c))
    cap = limit if limit is not None else int(ocfg["max_returned"])
    open_ = [x for x in found if x["mitigation"] != "invalidated"]
    out.update(candidates=found[-cap:] if cap else found, open=open_[-cap:] if cap else open_)
    return out


def _candidate(event: dict[str, Any], bullish: bool, j: int, t: int, bottom: float, top: float, displacement: float,
               atr: float, times: Stamps, tf: Timeframe, zone_kind: str, h: np.ndarray, low: np.ndarray,
               c: np.ndarray) -> dict[str, Any]:
    direction = "bullish" if bullish else "bearish"
    colour = "bearish (down)" if bullish else "bullish (up)"
    item: dict[str, Any] = {
        "id": f"{tf.value}:ob:{direction}:{times.open[j]}", "kind": "order_block", "type": "ob",
        "label": f"{direction.capitalize()} order-block candidate", "candidate_label": LABEL,
        "direction": direction, "timeframe": tf.value, "time": times.open[j],
        "zone": {"top": rounded(top), "bottom": rounded(bottom)}, "zone_kind": zone_kind,
        "price": rounded((top + bottom) / 2), "status": "confirmed",
        "known_at": event["known_at"], "structure_event": event["id"], "structure_type": event["type"],
        "displacement": rounded(displacement), "displacement_atr": rounded(displacement / atr, 2),
        "candles": [times.cid(j), times.cid(t)],
        "explanation": (f"Algorithmic candidate: the last {colour} candle ({times.open[j]}, "
                        f"{zone_kind} {fmt(bottom)}-{fmt(top)}) before the move that produced the {event['type']} "
                        f"{direction} at {event['time']} (close {fmt(c[t])}, {fmt(displacement / atr, 2)} ATR beyond "
                        "the zone). Not a confirmed institutional order; an observation for study."),
        "mitigation": "untouched", "touched_at": None, "invalidated_at": None, "definition": VERSION}
    after = slice(t + 1, len(c))
    touch = (low[after] <= top) if bullish else (h[after] >= bottom)
    invalid = (c[after] < bottom) if bullish else (c[after] > top)
    if touch.any():
        item["touched_at"] = times.close[t + 1 + int(touch.argmax())]
        item["mitigation"] = "touched"
    if invalid.any():
        item["invalidated_at"] = times.close[t + 1 + int(invalid.argmax())]
        item["mitigation"] = "invalidated"
    return item
