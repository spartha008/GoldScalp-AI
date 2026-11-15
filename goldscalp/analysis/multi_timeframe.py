"""Multi-timeframe analysis (definition mtf@1) for H1, M15, M5 and M1.

``build()`` runs every engine per timeframe on CLOSED candles up to ``as_of`` (a higher-timeframe
candle that is still forming is excluded and reported as such). ``summarise()`` produces the per-
timeframe summary and plain-language explanations of where timeframes disagree.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.analysis import fvg, liquidity, market_structure, order_blocks, volatility
from goldscalp.analysis.candles import close_time, closed_view, forming_bar
from goldscalp.analysis.config import AnalysisConfig, default_config
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import Timeframe

VERSION = "mtf@1"
ORDER = ("H1", "M15", "M5", "M1")
FRESH_GRACE_SECONDS = 90


EMPTY_DETAIL = {"liquidity": {"levels": [], "events": [], "developing": [], "nearest_above": None,
                               "nearest_below": None},
                "fvg": {"gaps": [], "open": [], "developing": []},
                "order_blocks": {"candidates": [], "open": []}}


def build(frames: dict[str, pd.DataFrame], as_of: datetime, cfg: AnalysisConfig | None = None, *,
          detail: tuple[str, ...] | None = None) -> dict[str, Any]:
    """``detail``: timeframes that get liquidity, FVG and order-block analysis (default: all). Bulk
    evaluation passes ``signals.DETAIL_TIMEFRAMES`` (the only ones the signal reads); the signal is
    identical either way (tested)."""
    cfg = cfg or default_config()
    as_of = ensure_aware(as_of)
    h1 = frames.get("H1")
    results: dict[str, Any] = {}
    for key in ORDER:
        frame = frames.get(key)
        if frame is None:
            continue
        tf = Timeframe.parse(key)
        bars = closed_view(frame, tf, as_of)
        st = market_structure.analyse(frame, tf, as_of, cfg, limit=0)
        results[key] = {
            "timeframe": key, "bars": bars, "frame": frame, "structure": st, "detail": True,
            "volatility": volatility.timeframe_profile(bars if bars is not None else frame.iloc[0:0], cfg),
            "freshness": freshness(frame, bars, tf, as_of),
        }
        if detail is not None and key not in detail:
            results[key].update(copy.deepcopy(EMPTY_DETAIL), detail=False)
            continue
        results[key].update(
            liquidity=liquidity.analyse(frame, tf, as_of, cfg, structure=st, reference=h1,
                                        reference_tf=Timeframe.H1 if h1 is not None else None),
            fvg=fvg.analyse(frame, tf, as_of, cfg, limit=0),
            order_blocks=order_blocks.analyse(frame, tf, as_of, cfg, structure=st, limit=0))
    return results


def freshness(frame: pd.DataFrame, bars: pd.DataFrame | None, tf: Timeframe, as_of: datetime) -> dict[str, Any]:
    forming = forming_bar(frame, tf, as_of)
    if bars is None or bars.empty:
        return {"last_closed": None, "age_seconds": None, "fresh": False, "forming_excluded": forming is not None,
                "note": "no closed candles"}
    ended = close_time(tf, bars.index[-1])
    age = (pd.Timestamp(as_of) - ended).total_seconds()
    fresh = age <= tf.seconds + FRESH_GRACE_SECONDS
    note = (f"last closed {tf.value} candle ended {int(age)} s before the cut-off"
            + ("" if fresh else " (stale)")
            + ("; the forming candle is excluded from confirmed analysis" if forming is not None else ""))
    return {"last_closed": iso(bars.index[-1].to_pydatetime()), "closed_at": iso(ended.to_pydatetime()),
            "age_seconds": int(age), "fresh": fresh, "forming_excluded": forming is not None, "note": note}


def _nearest_zone(items: list[dict[str, Any]], price: float | None) -> dict[str, Any] | None:
    if price is None or not items:
        return None
    def distance(z: dict[str, Any]) -> float:
        top, bottom = z["zone"]["top"], z["zone"]["bottom"]
        return 0.0 if bottom <= price <= top else min(abs(price - top), abs(price - bottom))
    best = min(items, key=distance)
    return {"id": best["id"], "direction": best["direction"], "zone": best["zone"], "known_at": best["known_at"],
            "distance": round(distance(best), 5), "contains_price": distance(best) == 0.0}


def summarise(results: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    rows = []
    for key in ORDER:
        r = results.get(key)
        if r is None:
            continue
        st = r["structure"]
        last_event = st["events"][-1] if st["events"] else None
        price = r["volatility"].get("last_close")
        fv = [g for g in r["fvg"]["gaps"] if g["mitigation"] != "fully_mitigated"]
        ob = [o for o in r["order_blocks"]["candidates"] if o["mitigation"] != "invalidated"]
        rows.append({
            "timeframe": key, "state": st["state"] if st["bars_used"] else "no data",
            "state_explanation": st["state_explanation"],
            "last_event": None if last_event is None else {k: last_event[k] for k in
                                                           ("type", "direction", "price", "known_at", "explanation")},
            "last_high_label": st["last_high_label"], "last_low_label": st["last_low_label"],
            "reference_high": (st["reference_high"] or {}).get("price"),
            "reference_low": (st["reference_low"] or {}).get("price"),
            "volatility": {k: r["volatility"].get(k) for k in ("condition", "atr", "atr_pct", "fast_slow_ratio")},
            "nearest_liquidity_above": r["liquidity"]["nearest_above"],
            "nearest_liquidity_below": r["liquidity"]["nearest_below"],
            "open_fvg": len(fv), "nearest_fvg": _nearest_zone(fv, price),
            "open_order_blocks": len(ob), "nearest_order_block": _nearest_zone(ob, price),
            "freshness": r["freshness"], "bars": st["bars_used"], "last_close": price,
        })
    states = {row["timeframe"]: row["state"] for row in rows}
    return {"definition": VERSION, "as_of": iso(ensure_aware(as_of)), "timeframes": rows,
            "alignment": _alignment(states), "disagreements": explain_disagreements(states)}


def _alignment(states: dict[str, str]) -> dict[str, Any]:
    core = [states.get(k) for k in ("H1", "M15", "M5") if k in states]
    bull, bear = core.count("bullish"), core.count("bearish")
    if core and bull == len(core):
        label = "aligned bullish"
    elif core and bear == len(core):
        label = "aligned bearish"
    elif bull and bear:
        label = "mixed"
    elif bull:
        label = "leaning bullish (others ranging)"
    elif bear:
        label = "leaning bearish (others ranging)"
    else:
        label = "ranging / no clear structure"
    return {"label": label, "bullish": bull, "bearish": bear, "of": len(core),
            "note": "Structure alignment is an observation about past candles, not a forecast."}


def explain_disagreements(states: dict[str, str]) -> list[str]:
    present = [k for k in ORDER if states.get(k) in ("bullish", "bearish", "ranging")]
    out: list[str] = []
    directional = [states[k] for k in present if states[k] != "ranging"]
    if len(present) >= 2 and len(directional) == len(present) and len(set(directional)) == 1:
        return [f"All available timeframes are {directional[0]}: structure is aligned "
                "(an observation about past candles, not a forecast)."]
    for higher, lower in zip(present, present[1:], strict=False):
        hs, ls = states[higher], states[lower]
        if hs == ls:
            continue
        if hs != "ranging" and ls != "ranging":
            opposite = "bearish" if hs == "bullish" else "bullish"
            trend = "up-trend" if hs == "bullish" else "down-trend"
            out.append(f"{lower} {ls} against {hs} {higher}: a pullback within the {higher} {trend}, or an early "
                       f"sign of a reversal. Only an {higher} {opposite} CHoCH would confirm a reversal on {higher}.")
        elif hs == "ranging":
            out.append(f"{higher} ranging while {lower} is {ls}: the {lower} move has no {higher} directional "
                       "backing and may stay inside the larger range.")
        else:
            out.append(f"{lower} ranging inside the {hs} {higher} structure: a consolidation; {lower} has not "
                       f"broken structure in either direction recently.")
    if not out and present and all(states[k] == "ranging" for k in present):
        out.append("All timeframes are ranging: no directional structure on any timeframe.")
    return out
