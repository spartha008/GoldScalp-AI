"""Signal centre (definition signal@1): a deterministic, rule-based classification of the CURRENT
evidence as BULLISH, BEARISH, NEUTRAL or NO SIGNAL. No machine learning, no hidden weights.

Gates (any failure gives NO SIGNAL, with every reason listed):
- live mode and the market state is not LIVE (no signals on delayed, stale or disconnected data);
- a timeframe's last closed candle is stale relative to the cut-off;
- the spread is above ``max_spread`` or above ``max_spread_atr_m5`` x ATR(M5);
- fewer than ``min_bars`` closed candles on any timeframe;
- invalid candles (non-finite or inconsistent OHLC, unordered or duplicate times) near the cut-off.

Evidence (weights from config/analysis.json, each item listed with its rule):
- H1, M15 and M5 structure state (bullish +w / bearish -w, ranging 0);
- an M1 BOS/CHoCH known within the last ``m1_event_lookback`` M1 candles;
- a confirmed M5 liquidity sweep known within the last ``sweep_lookback_m5`` M5 candles;
- the last price inside an open same-direction M5/M15 FVG or order-block candidate.
Classification: BULLISH if score >= classify_threshold and neither H1 nor M15 is bearish; BEARISH mirror;
otherwise NEUTRAL. The score is an "evidence agreement score (not a probability)".
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis import fvg, liquidity, market_structure, multi_timeframe, order_blocks
from goldscalp.analysis import sessions as sessions_mod
from goldscalp.analysis import volatility
from goldscalp.analysis.candles import fmt
from goldscalp.analysis.config import AnalysisConfig
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import Timeframe

VERSION = "signal@1"
SCORE_LABEL = "evidence agreement score (not a probability)"
DIRECTIONS = ("BULLISH", "BEARISH", "NEUTRAL", "NO SIGNAL")
DISCLAIMER = ("Educational rule-based observation of past candles. Not financial advice, not a prediction and "
              "not a recommendation to trade.")
DEFINITIONS = {"structure": market_structure.VERSION, "liquidity": liquidity.VERSION, "fvg": fvg.VERSION,
               "order_blocks": order_blocks.VERSION, "volatility": volatility.VERSION2,
               "sessions": sessions_mod.VERSION2, "multi_timeframe": multi_timeframe.VERSION, "signal": VERSION}
CHECK_BARS = 50
DETAIL_TIMEFRAMES = ("M5", "M15")   # the only timeframes whose liquidity/zones the rules read


def signal_id(mode: str, series: str, cutoff: str, fingerprint: str, direction: str,
              replay_id: str | None = None) -> str:
    """Deterministic: the same inputs and outcome always give the same id."""
    raw = f"{VERSION}|{mode}|{replay_id or ''}|{series}|{cutoff}|{fingerprint}|{direction}"
    return "sig_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def classify(score: int, h1_state: str | None, m15_state: str | None, threshold: int, gates: list[str]) -> str:
    """The signal@1 classification rule (pure, deterministic)."""
    if gates:
        return "NO SIGNAL"
    if score >= threshold and "bearish" not in (h1_state, m15_state):
        return "BULLISH"
    if score <= -threshold and "bullish" not in (h1_state, m15_state):
        return "BEARISH"
    return "NEUTRAL"


def candle_problems(bars: pd.DataFrame | None, tf: str) -> list[str]:
    if bars is None or bars.empty:
        return []
    tail = bars.tail(CHECK_BARS)
    o, h, low, c = (tail[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    problems = []
    if not np.isfinite(np.stack([o, h, low, c])).all():
        problems.append(f"{tf}: non-finite prices in the last {len(tail)} candles")
    elif ((h < np.maximum(o, c)) | (low > np.minimum(o, c)) | (low <= 0)).any():
        problems.append(f"{tf}: inconsistent OHLC (high below open/close or low above open/close)")
    if not tail.index.is_monotonic_increasing or tail.index.has_duplicates:
        problems.append(f"{tf}: candle times are not strictly increasing")
    return problems


def evaluate(results: dict[str, Any], as_of: datetime, cfg: AnalysisConfig, *, mode: str, series: str,
             market_state: str | None, spread: float | None, spread_source: str, source: str | None,
             generated_at: datetime, sessions_result: dict[str, Any] | None = None,
             replay_id: str | None = None) -> dict[str, Any]:
    """``results``: multi_timeframe.build() output. ``spread``: current spread in price units (None if
    unknown). ``generated_at`` is the wall-clock time the signal was computed (recorded, never used in rules)."""
    scfg = cfg["signals"]
    as_of = ensure_aware(as_of)
    summary = multi_timeframe.summarise(results, as_of)
    rows = {r["timeframe"]: r for r in summary["timeframes"]}
    cutoff = _cutoff(results)
    gates: list[str] = []

    if mode == "live" and market_state != "LIVE":
        gates.append(f"market data is {market_state or 'unavailable'}, not LIVE: no signal on delayed, stale or "
                     "disconnected data")
    for key in multi_timeframe.ORDER:
        r = results.get(key)
        need = int(scfg["min_bars"][key])
        have = 0 if r is None or r["bars"] is None else len(r["bars"])
        if have < need:
            gates.append(f"{key}: {have} closed candles, at least {need} required")
        if r is not None:
            if not r["freshness"]["fresh"]:
                gates.append(f"{key}: stale data ({r['freshness']['note']})")
            gates.extend(candle_problems(r["bars"], key))
    atr_m5 = rows.get("M5", {}).get("volatility", {}).get("atr")
    if spread is not None:
        if spread > float(scfg["max_spread"]):
            gates.append(f"spread {fmt(spread)} is above the maximum {fmt(float(scfg['max_spread']))}")
        if atr_m5 and spread > float(scfg["max_spread_atr_m5"]) * atr_m5:
            gates.append(f"spread {fmt(spread)} is above {scfg['max_spread_atr_m5']} x ATR(M5) "
                         f"({fmt(float(scfg['max_spread_atr_m5']) * atr_m5)})")

    evidence = _evidence(results, rows, scfg, as_of)
    score = int(sum(e["score"] for e in evidence))
    h1, m15 = rows.get("H1", {}).get("state"), rows.get("M15", {}).get("state")
    threshold = int(scfg["classify_threshold"])
    direction = classify(score, h1, m15, threshold, gates)
    lean = 1 if (direction == "BULLISH" or (direction in ("NEUTRAL", "NO SIGNAL") and score > 0)) else (
        -1 if (direction == "BEARISH" or score < 0) else 0)
    supporting = [e for e in evidence if lean and np.sign(e["score"]) == lean]
    conflicting = [e for e in evidence if lean and np.sign(e["score"]) == -lean]
    if lean == 0:
        conflicting = [e for e in evidence if e["score"] != 0]
    neutral = [e for e in evidence if e["score"] == 0]

    active = sessions_mod.active_names(sessions_mod.definitions(cfg), as_of)   # same rule as sessions@2
    session_ctx = {"current": " / ".join(active) if active else "between sessions", "active": active,
                   "overlap": len(active) > 1}
    context = {
        "spread": spread, "spread_source": spread_source,
        "spread_vs_atr_m5": round(spread / atr_m5, 3) if spread is not None and atr_m5 else None,
        "volatility": {k: rows[k]["volatility"] for k in rows},
        "session": session_ctx,
    }
    payload = {
        "id": signal_id(mode, series, cutoff, cfg.fingerprint, direction, replay_id), "definition": VERSION,
        "mode": mode, "replay_id": replay_id, "series": series, "direction": direction,
        "score": score, "score_label": SCORE_LABEL, "threshold": threshold,
        "timeframe": "M5", "as_of": iso(as_of), "cutoff": cutoff, "generated_at": iso(ensure_aware(generated_at)),
        "reference_price": next((rows[k]["last_close"] for k in ("M1", "M5", "M15", "H1")
                                 if k in rows and rows[k]["last_close"] is not None), None),
        "atr_m5": atr_m5,
        "structure": {k: {"state": rows[k]["state"], "explanation": rows[k]["state_explanation"]} for k in rows},
        "lean": "bullish" if lean > 0 else "bearish" if lean < 0 else "none",
        "supporting": supporting, "conflicting": conflicting, "neutral": neutral, "gates_failed": gates,
        "invalidation": _invalidation(direction, rows),
        "context": context,
        "source": {"series": series, "provider": source, "market_state": market_state, "mode": mode},
        "freshness": {k: rows[k]["freshness"] for k in rows},
        "disagreements": summary["disagreements"], "alignment": summary["alignment"],
        "definitions": DEFINITIONS, "config_fingerprint": cfg.fingerprint,
        "input_range": {k: _range(results[k]["bars"]) for k in results},
        "disclaimer": DISCLAIMER,
    }
    payload["explanation"] = _explain(payload)
    return payload


def _cutoff(results: dict[str, Any]) -> str:
    """The data cut-off: close time of the latest closed candle on the finest timeframe available."""
    for key in ("M1", "M5", "M15", "H1"):
        fr = results.get(key, {}).get("freshness") if key in results else None
        if fr and fr.get("closed_at"):
            return fr["closed_at"]
    return "none"


def _range(bars: pd.DataFrame | None) -> list[str] | None:
    if bars is None or bars.empty:
        return None
    return [iso(bars.index[0].to_pydatetime()), iso(bars.index[-1].to_pydatetime())]


def _evidence(results: dict[str, Any], rows: dict[str, Any], scfg: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    w = scfg["weights"]
    items: list[dict[str, Any]] = []
    for key in ("H1", "M15", "M5"):
        row = rows.get(key)
        if row is None:
            continue
        state = row["state"]
        sign = 1 if state == "bullish" else -1 if state == "bearish" else 0
        items.append({"rule": f"{key} structure", "timeframe": key, "direction": state if sign else "neutral",
                      "weight": int(w[key]), "score": sign * int(w[key]),
                      "observation": f"{key} structure is {state}: " + "; ".join(row["state_explanation"])})
    m1 = results.get("M1")
    if m1 is not None:
        lookback = pd.Timedelta(seconds=Timeframe.M1.seconds * int(scfg["m1_event_lookback"]))
        recent = [e for e in m1["structure"]["events"] if pd.Timestamp(e["known_at"]) > pd.Timestamp(as_of) - lookback]
        if recent:
            e = recent[-1]
            sign = 1 if e["direction"] == "bullish" else -1
            items.append({"rule": "recent M1 structure break", "timeframe": "M1", "direction": e["direction"],
                          "weight": int(w["M1"]), "score": sign * int(w["M1"]), "observation": e["explanation"],
                          "known_at": e["known_at"]})
    m5 = results.get("M5")
    if m5 is not None:
        lookback = pd.Timedelta(seconds=Timeframe.M5.seconds * int(scfg["sweep_lookback_m5"]))
        sweeps = [e for e in m5["liquidity"]["events"] if e["type"] == "sweep" and e["status"] == "confirmed"
                  and pd.Timestamp(e["known_at"]) > pd.Timestamp(as_of) - lookback]
        if sweeps:
            e = sweeps[-1]
            sign = 1 if e["direction"] == "bullish" else -1
            items.append({"rule": "recent M5 liquidity sweep", "timeframe": "M5", "direction": e["direction"],
                          "weight": int(w["sweep"]), "score": sign * int(w["sweep"]), "observation": e["explanation"],
                          "known_at": e["known_at"]})
    for direction in ("bullish", "bearish"):
        zone = _containing_zone(results, direction)
        if zone is not None:
            sign = 1 if direction == "bullish" else -1
            items.append({"rule": f"price inside an open {direction} zone", "timeframe": zone["timeframe"],
                          "direction": direction, "weight": int(w["zone"]), "score": sign * int(w["zone"]),
                          "observation": (f"last close is inside the {zone['label']} {fmt(zone['zone']['bottom'])}-"
                                          f"{fmt(zone['zone']['top'])} ({zone['timeframe']}, known {zone['known_at']})"),
                          "known_at": zone["known_at"]})
    return items


def _containing_zone(results: dict[str, Any], direction: str) -> dict[str, Any] | None:
    for key in ("M5", "M15"):
        r = results.get(key)
        if r is None:
            continue
        price = r["volatility"].get("last_close")
        if price is None:
            continue
        zones = [g for g in r["fvg"]["gaps"] if g["mitigation"] != "fully_mitigated"]
        zones += [o for o in r["order_blocks"]["candidates"] if o["mitigation"] != "invalidated"]
        for z in reversed(zones):
            if z["direction"] == direction and z["zone"]["bottom"] <= price <= z["zone"]["top"]:
                return z
    return None


def _invalidation(direction: str, rows: dict[str, Any]) -> list[dict[str, Any]]:
    if direction not in ("BULLISH", "BEARISH"):
        return []
    out = []
    for key in ("M5", "M15"):
        row = rows.get(key)
        if row is None:
            continue
        level = row["reference_low"] if direction == "BULLISH" else row["reference_high"]
        if level is None:
            continue
        side = "below" if direction == "BULLISH" else "above"
        swing = "swing low" if direction == "BULLISH" else "swing high"
        out.append({"timeframe": key, "price": level, "condition": f"an {key} candle CLOSES {side} {fmt(level)}",
                    "meaning": f"a close {side} the last confirmed {key} {swing} would break {key} structure "
                               f"against this observation"})
    return out


def _explain(p: dict[str, Any]) -> str:
    if p["direction"] == "NO SIGNAL":
        return "No signal: " + "; ".join(p["gates_failed"]) + "."
    parts = [f"{p['direction']} with an {SCORE_LABEL} of {p['score']:+d} (threshold +/-{p['threshold']})."]
    neutral = p["direction"] == "NEUTRAL"
    if p["supporting"]:
        word = f"Evidence leaning {p['lean']}" if neutral else "Supporting"
        parts.append(f"{word}: " + "; ".join(f"{e['rule']} ({e['score']:+d})" for e in p["supporting"]) + ".")
    if p["conflicting"]:
        word = "Opposing evidence" if neutral else "Conflicting"
        parts.append(f"{word}: " + "; ".join(f"{e['rule']} ({e['score']:+d})" for e in p["conflicting"]) + ".")
    if p["direction"] == "NEUTRAL":
        parts.append("The evidence does not meet the rule (score below the threshold, or H1/M15 structure "
                     "opposes it).")
    if p["disagreements"]:
        parts.append(p["disagreements"][0])
    parts.append(DISCLAIMER)
    return " ".join(parts)


def forward_outcome(payload: dict[str, Any], after: pd.DataFrame, base_tf: Timeframe,
                    horizons_m5: list[int]) -> dict[str, Any]:
    """HISTORICAL OBSERVATION of what price did after a BULLISH/BEARISH signal, from closed candles that
    came after its cut-off (replay only, computed once the replay cursor has passed each horizon).
    move = signed close-to-close change; MFE/MAE = best/worst excursion, also in ATR(M5) units."""
    direction = payload["direction"]
    if direction not in ("BULLISH", "BEARISH") or payload.get("reference_price") is None:
        return {"label": "historical observation", "applicable": False, "horizons": []}
    sign = 1.0 if direction == "BULLISH" else -1.0
    entry = float(payload["reference_price"])
    atr = payload.get("atr_m5")
    out = []
    for n in horizons_m5:
        need = max(1, int(np.ceil(n * Timeframe.M5.seconds / base_tf.seconds)))
        if len(after) < need:
            out.append({"m5_candles": n, "status": "pending", "candles_needed": need, "candles_seen": int(len(after))})
            continue
        window = after.iloc[:need]
        close = float(window["close"].iloc[-1])
        move = (close - entry) * sign
        best = (float(window["high"].max()) - entry) if sign > 0 else (entry - float(window["low"].min()))
        worst = (entry - float(window["low"].min())) if sign > 0 else (float(window["high"].max()) - entry)
        item = {"m5_candles": n, "status": "complete", "until": iso(window.index[-1].to_pydatetime()),
                "close": round(close, 5), "move": round(move, 5), "mfe": round(best, 5), "mae": round(worst, 5)}
        if atr:
            item.update(move_atr=round(move / atr, 2), mfe_atr=round(best / atr, 2), mae_atr=round(worst / atr, 2))
        out.append(item)
    return {"label": "historical observation", "applicable": True, "reference_price": entry, "horizons": out,
            "note": "What price did after this past signal in the replayed data. Past behaviour does not "
                    "predict future results."}
