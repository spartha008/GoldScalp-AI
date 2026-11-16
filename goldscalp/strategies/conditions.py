"""Evaluate a strategy@1 configuration on a feature snapshot (features@1) for one direction.

Every condition returns (passed, evidence). A DATA-QUALITY GATE applies first: every timeframe the
strategy uses must have enough closed candles, a fresh last candle and valid OHLC; otherwise the setup
cannot trigger (the reasons are returned).
"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from goldscalp.core.timeframes import Timeframe
from goldscalp.strategies.builder import timeframes_used

LEVEL_GROUPS = {"EQH": "EQ", "EQL": "EQ", "PDH": "PD", "PDL": "PD", "PWH": "PW", "PWL": "PW",
                "session_high": "session", "session_low": "session"}


def _within(known_at: str, cutoff: pd.Timestamp, tf: str, candles: int) -> bool:
    return pd.Timestamp(known_at) > cutoff - pd.Timedelta(seconds=Timeframe.parse(tf).seconds * candles)


def quality(config: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    problems = []
    for tf in sorted(timeframes_used(config)):
        item = snap["tf"].get(tf)
        if item is None:
            problems.append(f"{tf}: no data for this timeframe in the dataset")
            continue
        if not item["enough"]:
            problems.append(f"{tf}: only {item['bars']} closed candles")
        if not item["fresh"]:
            problems.append(f"{tf}: stale data (last closed candle too old)")
        problems.extend(item["problems"])
    return problems


def _check(c: dict[str, Any], snap: dict[str, Any], side: str) -> tuple[bool, str]:
    want, against = ("bullish", "bearish") if side == "long" else ("bearish", "bullish")
    cut = pd.Timestamp(snap["cutoff"])
    t = c["type"]
    tf = c.get("timeframe")
    item = snap["tf"].get(tf) if tf else None
    if tf and item is None:
        return False, f"{tf} data unavailable"
    if t == "structure_state":
        state = item["state"]
        ok = {"with": state == want, "against": state == against, "ranging": state == "ranging",
              "not_against": state in (want, "ranging")}[c["state"]]
        return ok, f"{tf} structure is {state}"
    if t == "structure_event":
        direction = want if c["direction"] == "with" else against
        hits = [e for e in item["events"] if (c["event"] == "any" or e["type"] == c["event"])
                and e["direction"] == direction and _within(e["known_at"], cut, tf, c["within_candles"])]
        if hits:
            e = hits[-1]
            return True, f"{tf} {e['type']} {e['direction']} through {e['price']} (known {e['known_at']})"
        return False, f"no {tf} {c['event']} {direction} within {c['within_candles']} candles"
    if t == "swing_sequence":
        hi, lo = item["high_label"], item["low_label"]
        if side == "long":
            ok = (hi == "HH" and lo == "HL") if c["pattern"] == "trend" else lo == "HL"
        else:
            ok = (hi == "LH" and lo == "LL") if c["pattern"] == "trend" else hi == "LH"
        return ok, f"{tf} last swing high {hi or 'n/a'}, last swing low {lo or 'n/a'}"
    if t == "liquidity_sweep":
        if not item.get("detail"):
            return False, f"{tf} liquidity not analysed"
        hits = [e for e in item["sweeps"] if e["type"] == "sweep" and e["direction"] == want
                and LEVEL_GROUPS.get(e["level_type"]) in c["level_types"]
                and _within(e["known_at"], cut, tf, c["within_candles"])]
        if hits:
            e = hits[-1]
            return True, f"{tf} potential {want} sweep of {e['level_type']} {e['price']} (known {e['known_at']})"
        return False, f"no {tf} {want} sweep within {c['within_candles']} candles"
    if t == "level_break":
        if not item.get("detail"):
            return False, f"{tf} liquidity not analysed"
        hits = [e for e in item["sweeps"] if e["type"] == "break" and e["direction"] == want
                and LEVEL_GROUPS.get(e["level_type"]) in c["level_types"]
                and _within(e["known_at"], cut, tf, c["within_candles"])]
        if hits:
            e = hits[-1]
            return True, f"{tf} confirmed {want} break of {e['level_type']} {e['price']} (known {e['known_at']})"
        return False, f"no confirmed {tf} {want} level break within {c['within_candles']} candles"
    if t == "level_retest":
        if not item.get("detail"):
            return False, f"{tf} liquidity not analysed"
        price, atr = snap.get("price"), (item.get("vol") or {}).get("atr")
        if price is None or not atr:
            return False, f"{tf} price or ATR unavailable"
        sign = 1 if side == "long" else -1
        for e in reversed(item["sweeps"]):
            if e["type"] != "break" or e["direction"] != want or LEVEL_GROUPS.get(e["level_type"]) not in c["level_types"]:
                continue
            if not _within(e["known_at"], cut, tf, c["within_candles"]) or _within(e["known_at"], cut, tf, c["min_age_candles"]):
                continue
            beyond = (price - e["price"]) * sign
            if 0 <= beyond <= c["max_distance_atr"] * atr:
                return True, (f"{tf} retest of the confirmed {want} break of {e['level_type']} {e['price']} "
                              f"(price {price}, {beyond / atr:.2f} ATR beyond; break known {e['known_at']})")
        return False, (f"no {tf} retest: price is not within {c['max_distance_atr']} ATR beyond a confirmed {want} "
                       f"break {c['min_age_candles']}-{c['within_candles']} candles old")
    if t in ("fvg", "order_block"):
        key = "fvg" if t == "fvg" else "ob"
        if not item.get("detail"):
            return False, f"{tf} zones not analysed"
        ok_states = ({"unmitigated", "partially_mitigated"} if c.get("allow_partial", True) else {"unmitigated"}) \
            if t == "fvg" else ({"untouched", "touched"} if c.get("allow_touched", True) else {"untouched"})
        price = snap["price"]
        zones = [z for z in item[key] if z["direction"] == want and z["mitigation"] in ok_states]
        if c["relation"] == "price_inside":
            hits = [z for z in zones if price is not None and z["bottom"] <= price <= z["top"]]
        else:
            hits = [z for z in zones if _within(z["known_at"], cut, tf, c["within_candles"])]
        name = "FVG" if t == "fvg" else "order-block candidate"
        if hits:
            z = hits[-1]
            return True, f"{tf} {want} {name} {z['bottom']}-{z['top']} ({z['mitigation']}, known {z['known_at']})"
        return False, f"no qualifying {tf} {want} {name}"
    if t == "htf_alignment":
        states = {k: (snap["tf"].get(k) or {}).get("state", "no data") for k in c["timeframes"]}
        agree = sum(1 for s in states.values() if s == want)
        bad = [k for k, s in states.items() if s == against]
        ok = agree >= c["min_agree"] and not (c["none_against"] and bad)
        return ok, ", ".join(f"{k} {s}" for k, s in states.items())
    if t == "session":
        active = snap["sessions"]
        listed = [s for s in active if s in c["sessions"]]
        overlap = len(active) > 1
        ok = bool(listed) and {"any": True, "required": overlap, "excluded": not overlap}[c["overlap"]]
        return ok, f"active: {' / '.join(active) or 'none'}"
    if t == "volatility_regime":
        cond = item["vol"]["condition"]
        return cond in c["allowed"], f"{tf} volatility {cond}"
    if t == "atr_range":
        value = item["vol"]["atr"] if c["unit"] == "price" else item["vol"]["atr_pct"]
        if value is None:
            return False, f"{tf} ATR unavailable"
        ok = (c["min"] is None or value >= c["min"]) and (c["max"] is None or value <= c["max"])
        return ok, f"{tf} ATR {value}{'%' if c['unit'] == 'percent' else ''}"
    if t == "spread":
        spread = snap["spread"]
        if spread is None:
            return c["if_unknown"] == "pass", "spread unknown"
        atr = (snap["tf"].get("M5") or {}).get("vol", {}).get("atr")
        ok = (c["max_spread"] is None or spread <= c["max_spread"]) and \
             (c["max_spread_atr"] is None or (atr is not None and spread <= c["max_spread_atr"] * atr))
        return ok, f"spread {spread}" + (f" ({spread / atr:.2f} x ATR M5)" if atr else "")
    if t == "ema_trend":
        ind = item.get("ind") or {}
        ema = ind.get("ema") or {}
        fast, slow, close = ema.get(c["fast"]), ema.get(c["slow"]), ind.get("close")
        if slow is None or (c["mode"] != "price_vs_slow" and fast is None) or close is None:
            return False, f"{tf} EMA{c['slow']} unavailable (needs {2 * int(c['slow'])} closed candles)"
        up = side == "long"
        price_ok = close > slow if up else close < slow
        stack_ok = fast is not None and (fast > slow if up else fast < slow)
        ok = {"price_vs_slow": price_ok, "fast_vs_slow": stack_ok, "both": price_ok and stack_ok}[c["mode"]]
        return ok, (f"{tf} close {close} vs EMA{c['slow']} {slow}" + (f", EMA{c['fast']} {fast}" if fast is not None else ""))
    if t == "momentum":
        rsi = (item.get("ind") or {}).get("rsi14")
        if rsi is None:
            return False, f"{tf} RSI(14) unavailable"
        level = c["level"]
        if c["mode"] == "confirm":
            ok = rsi >= level if side == "long" else rsi <= 100 - level
        else:
            ok = rsi <= level if side == "long" else rsi >= 100 - level
        return ok, f"{tf} RSI(14) {rsi}"
    if t == "clock_time":
        local = cut.to_pydatetime().astimezone(ZoneInfo(c["timezone"]))
        minute = local.hour * 60 + local.minute
        for hhmm in c["times"]:
            start = int(hhmm[:2]) * 60 + int(hhmm[3:])
            if (minute - start) % 1440 < c["window_minutes"]:
                return True, f"{local:%H:%M} {c['timezone']} is within {c['window_minutes']} min of {hhmm}"
        return False, f"{local:%H:%M} {c['timezone']} is not one of {', '.join(c['times'])}"
    if t == "signal_agrees":
        sig = snap.get("signal") or {}
        target = "BULLISH" if side == "long" else "BEARISH"
        ok = sig.get("direction") == target and abs(int(sig.get("score") or 0)) >= c["min_abs_score"]
        return ok, f"signal@1 {sig.get('direction')} score {sig.get('score')}"
    raise ValueError(f"unknown condition type {t}")       # pragma: no cover (validated earlier)


def evaluate(config: dict[str, Any], snap: dict[str, Any], side: str) -> dict[str, Any]:
    gate = quality(config, snap)
    results = []
    for c in config["conditions"]:
        if not c["enabled"]:
            continue
        ok, why = _check(c, snap, side)
        results.append({"id": c["id"], "type": c["type"], "passed": ok, "evidence": why})
    return {"side": side, "passed": not gate and all(r["passed"] for r in results), "gate": gate,
            "conditions": results}


def sides(config: dict[str, Any]) -> list[str]:
    return ["long", "short"] if config["direction"] == "both" else [config["direction"]]


def decide(config: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """Evaluate every allowed direction. Exactly one passing direction is a setup; two is ambiguous."""
    evals = {s: evaluate(config, snap, s) for s in sides(config)}
    passing = [s for s, e in evals.items() if e["passed"]]
    return {"setup": passing[0] if len(passing) == 1 else None, "ambiguous": len(passing) > 1, "evaluations": evals}
