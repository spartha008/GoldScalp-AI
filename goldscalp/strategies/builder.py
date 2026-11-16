"""Strategy configurations (schema strategy@1). PHASE3_ARCHITECTURE.md section 3.

A strategy is a user-configured combination of Phase 2 observations (structure, liquidity, FVG,
order-block candidates, sessions, volatility, spread). Conditions are DIRECTION-RELATIVE: "with" means
bullish for a long candidate and bearish for a short one.

Validation is strict: unknown keys or condition types are errors; omitted optional parameters get
their documented defaults. The FINGERPRINT covers the logic only (not the name or description), so a
rename does not create a new version.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from goldscalp.core.fingerprint import digest

SCHEMA = "strategy@1"
TIMEFRAMES = ("M1", "M5", "M15", "H1")
STEP_TIMEFRAMES = ("M1", "M5", "M15", "H1")
SESSION_NAMES = ("Asian", "London", "New York")
LEVEL_TYPES = ("EQ", "PD", "PW", "session")
VOL_REGIMES = ("contracting", "normal", "expanding")
CLOCK_TIMEZONES = ("UTC", "America/New_York", "Europe/London")
DETAIL_TYPES = {"liquidity_sweep", "level_break", "level_retest", "fvg", "order_block"}


class StrategyError(ValueError):
    pass


# Parameter specs: (kind, default, rule). kind: tf | choice | int | num | bool | tfs | choices | numopt | times
CONDITIONS: dict[str, dict[str, Any]] = {
    "structure_state": {
        "label": "Structure state", "help": "The timeframe's structure@2 state (bullish / bearish / ranging).",
        "params": {"timeframe": ("tf", "H1", None),
                   "state": ("choice", "with", ("with", "against", "ranging", "not_against"))}},
    "structure_event": {
        "label": "BOS / CHoCH", "help": "A confirmed break of structure became known within the last N candles.",
        "params": {"timeframe": ("tf", "M5", None), "event": ("choice", "BOS", ("BOS", "CHoCH", "any")),
                   "direction": ("choice", "with", ("with", "against")),
                   "within_candles": ("int", 3, (1, 200))}},
    "swing_sequence": {
        "label": "HH / HL / LH / LL", "help": "trend: last high HH and last low HL (long; LH and LL for short). "
                                              "pullback: last low HL (long; last high LH for short).",
        "params": {"timeframe": ("tf", "M15", None), "pattern": ("choice", "trend", ("trend", "pullback"))}},
    "liquidity_sweep": {
        "label": "Liquidity sweep", "help": "A confirmed potential sweep in the strategy direction (a bullish sweep "
                                            "takes sell-side liquidity) became known within N candles.",
        "params": {"timeframe": ("tf", "M5", None), "within_candles": ("int", 12, (1, 500)),
                   "level_types": ("choices", ["EQ", "PD", "PW", "session"], LEVEL_TYPES)}},
    "level_break": {
        "label": "Level break (acceptance)", "help": "Phase 5. A CONFIRMED break of a liquidity level in the strategy "
                                                     "direction (price closed beyond it and stayed beyond it for the "
                                                     "acceptance candles) became known within N candles.",
        "params": {"timeframe": ("tf", "M5", None), "within_candles": ("int", 3, (1, 500)),
                   "level_types": ("choices", ["PD", "session"], LEVEL_TYPES)}},
    "level_retest": {
        "label": "Level retest (after acceptance)", "help": "Phase 5.2. A CONFIRMED break of a liquidity level in the "
                                                            "strategy direction is between min_age and within candles old, "
                                                            "and price is back within max_distance_atr x ATR of the broken "
                                                            "level while still on the breakout side.",
        "params": {"timeframe": ("tf", "M5", None), "within_candles": ("int", 24, (2, 500)),
                   "min_age_candles": ("int", 2, (1, 499)), "max_distance_atr": ("num", 0.5, (0.05, 5)),
                   "level_types": ("choices", ["PD", "session"], LEVEL_TYPES)}},
    "fvg": {
        "label": "Fair value gap", "help": "price_inside: price is inside an open same-direction FVG. recent: one "
                                           "formed within N candles.",
        "params": {"timeframe": ("tf", "M5", None), "relation": ("choice", "price_inside", ("price_inside", "recent")),
                   "within_candles": ("int", 12, (1, 500)), "allow_partial": ("bool", True, None)}},
    "order_block": {
        "label": "Order-block candidate", "help": "price_inside: price is inside an open same-direction candidate. "
                                                  "recent: one became known within N candles.",
        "params": {"timeframe": ("tf", "M15", None),
                   "relation": ("choice", "price_inside", ("price_inside", "recent")),
                   "within_candles": ("int", 12, (1, 500)), "allow_touched": ("bool", True, None)}},
    "htf_alignment": {
        "label": "Timeframe alignment", "help": "At least min_agree of the timeframes have structure in the "
                                                "strategy direction (optionally none against it).",
        "params": {"timeframes": ("tfs", ["H1", "M15"], TIMEFRAMES), "min_agree": ("int", 2, (1, 4)),
                   "none_against": ("bool", True, None)}},
    "session": {
        "label": "Session", "help": "At least one listed session is active. overlap: any / required / excluded.",
        "params": {"sessions": ("choices", ["London", "New York"], SESSION_NAMES),
                   "overlap": ("choice", "any", ("any", "required", "excluded"))}},
    "volatility_regime": {
        "label": "Volatility regime", "help": "The vol@2 condition (fast ATR / slow ATR) is one of the allowed ones.",
        "params": {"timeframe": ("tf", "M5", None), "allowed": ("choices", ["normal", "expanding"], VOL_REGIMES)}},
    "atr_range": {
        "label": "ATR range", "help": "The timeframe ATR is within the bounds (price units or % of price).",
        "params": {"timeframe": ("tf", "M5", None), "min": ("numopt", None, (0, 1e6)),
                   "max": ("numopt", None, (0, 1e6)), "unit": ("choice", "price", ("price", "percent"))}},
    "spread": {
        "label": "Spread filter", "help": "The spread is at most max_spread (USD) and at most max_spread_atr x ATR(M5).",
        "params": {"max_spread": ("numopt", 0.5, (0, 100)), "max_spread_atr": ("numopt", None, (0, 100)),
                   "if_unknown": ("choice", "fail", ("fail", "pass"))}},
    "ema_trend": {
        "label": "Moving-average trend", "help": "Phase 5. Direction-relative EMA structure on the timeframe's closed "
                                                 "candles: price beyond the slow EMA, fast EMA beyond the slow EMA, or both.",
        "params": {"timeframe": ("tf", "M15", None), "fast": ("choice", "20", ("9", "20", "50")),
                   "slow": ("choice", "50", ("50", "100", "200")),
                   "mode": ("choice", "both", ("price_vs_slow", "fast_vs_slow", "both"))}},
    "momentum": {
        "label": "Momentum (RSI 14)", "help": "Phase 5. confirm: RSI(14) >= level for a long (<= 100 - level for a short). "
                                              "not_extreme: RSI(14) <= level for a long (>= 100 - level for a short).",
        "params": {"timeframe": ("tf", "M5", None), "level": ("int", 55, (50, 90)),
                   "mode": ("choice", "confirm", ("confirm", "not_extreme"))}},
    "clock_time": {
        "label": "Clock time", "help": "Pre-Phase 7. The step candle closed at one of the listed clock times (HH:MM) in the "
                                       "timezone, or up to window_minutes later. Daylight-saving aware. It is the same for "
                                       "longs and shorts, so set the strategy direction to long or short.",
        "params": {"timezone": ("choice", "America/New_York", CLOCK_TIMEZONES), "times": ("times", ["09:30"], (1, 12)),
                   "window_minutes": ("int", 5, (1, 240))}},
    "signal_agrees": {
        "label": "Phase 2 signal agrees", "help": "The signal@1 classification is in the strategy direction with "
                                                  "|score| >= min_abs_score.",
        "params": {"min_abs_score": ("int", 4, (0, 20))}},
}

#: "none" (Pre-Phase 7): no stop, for rules whose source has none. The position leaves by its target, its time limit
#: or a structure flip only, R is undefined, and demo execution refuses such trades (it never sends an order without a
#: stop-loss).
STOP_TYPES = ("structure", "atr", "fixed", "none")
TARGET_TYPES = ("r_multiple", "atr", "fixed", "none")


def _num(value: Any, path: str, lo: float, hi: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyError(f"{path} must be a number")
    if integer and int(value) != value:
        raise StrategyError(f"{path} must be a whole number")
    if not lo <= value <= hi:
        raise StrategyError(f"{path} must be between {lo} and {hi}")
    return int(value) if integer else float(value)


def _dec(value: Any, path: str, lo: Decimal, hi: Decimal) -> str:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyError(f"{path} must be a decimal number") from exc
    if not d.is_finite() or not lo <= d <= hi:
        raise StrategyError(f"{path} must be between {lo} and {hi}")
    return format(d, "f")


def _keys(data: Any, allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StrategyError(f"{path} must be an object")
    extra = sorted(set(data) - allowed)
    if extra:
        raise StrategyError(f"{path} has unknown keys {extra}")
    return data


def _condition(raw: Any, i: int, seen: set[str]) -> dict[str, Any]:
    path = f"conditions[{i}]"
    if not isinstance(raw, dict) or "type" not in raw:
        raise StrategyError(f"{path} needs a type")
    ctype = raw["type"]
    if ctype not in CONDITIONS:
        raise StrategyError(f"{path}.type {ctype!r} is unknown; choose one of {sorted(CONDITIONS)}")
    spec = CONDITIONS[ctype]["params"]
    _keys(raw, {"id", "type", "enabled", *spec}, path)
    cid = raw.get("id") or f"c{i + 1}"
    if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", cid):
        raise StrategyError(f"{path}.id must be 1-32 letters, digits, '_' or '-'")
    if cid in seen:
        raise StrategyError(f"{path}.id {cid!r} is used twice")
    seen.add(cid)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise StrategyError(f"{path}.enabled must be true or false")
    out: dict[str, Any] = {"id": cid, "type": ctype, "enabled": enabled}
    for name, (kind, default, rule) in spec.items():
        value = raw.get(name, copy.deepcopy(default))
        p = f"{path}.{name}"
        if kind == "tf":
            if value not in TIMEFRAMES:
                raise StrategyError(f"{p} must be one of {list(TIMEFRAMES)}")
        elif kind == "choice":
            if value not in rule:
                raise StrategyError(f"{p} must be one of {list(rule)}")
        elif kind == "int":
            value = _num(value, p, rule[0], rule[1], integer=True)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise StrategyError(f"{p} must be true or false")
        elif kind == "numopt":
            if value is not None:
                value = _num(value, p, rule[0], rule[1])
        elif kind in ("tfs", "choices"):
            if not isinstance(value, list) or not value or len(set(value)) != len(value) \
                    or any(v not in rule for v in value):
                raise StrategyError(f"{p} must be a non-empty list of distinct values from {list(rule)}")
        elif kind == "times":
            if not isinstance(value, list) or not rule[0] <= len(value) <= rule[1] or len(set(value)) != len(value) \
                    or any(not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) for v in value):
                raise StrategyError(f"{p} must be a list of {rule[0]}-{rule[1]} distinct clock times like 09:30")
        out[name] = value
    if ctype == "htf_alignment" and out["min_agree"] > len(out["timeframes"]):
        raise StrategyError(f"{path}.min_agree cannot exceed the number of timeframes")
    if ctype == "atr_range":
        if out["min"] is None and out["max"] is None:
            raise StrategyError(f"{path} needs a min or a max")
        if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
            raise StrategyError(f"{path}.min must not exceed max")
    if ctype == "ema_trend" and int(out["fast"]) >= int(out["slow"]):
        raise StrategyError(f"{path}.fast must be shorter than slow")
    if ctype == "level_retest" and out["min_age_candles"] >= out["within_candles"]:
        raise StrategyError(f"{path}.min_age_candles must be below within_candles")
    if ctype == "spread" and out["max_spread"] is None and out["max_spread_atr"] is None:
        raise StrategyError(f"{path} needs max_spread or max_spread_atr")
    return out


def validate(data: Any) -> dict[str, Any]:
    """A normalized strategy@1 configuration (defaults filled in), or StrategyError."""
    top = _keys(data, {"schema", "name", "description", "direction", "evaluation", "conditions", "entry", "exit",
                       "risk"}, "strategy")
    if top.get("schema", SCHEMA) != SCHEMA:
        raise StrategyError(f"schema must be {SCHEMA!r}")
    name = top.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise StrategyError("name must be 1-80 characters")
    description = top.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        raise StrategyError("description must be text of at most 2000 characters")
    direction = top.get("direction", "long")
    if direction not in ("long", "short", "both"):
        raise StrategyError("direction must be long, short or both")

    ev = _keys(top.get("evaluation", {}), {"step_timeframe", "window"}, "evaluation")
    step = ev.get("step_timeframe", "M5")
    if step not in STEP_TIMEFRAMES:
        raise StrategyError(f"evaluation.step_timeframe must be one of {list(STEP_TIMEFRAMES)}")
    win = _keys(ev.get("window", {}), {"mode", "days", "start", "end"}, "evaluation.window")
    mode = win.get("mode", "all")
    if mode not in ("all", "last_days", "range"):
        raise StrategyError("evaluation.window.mode must be all, last_days or range")
    window = {"mode": mode, "days": _num(win.get("days", 10), "evaluation.window.days", 1, 3650, integer=True),
              "start": win.get("start"), "end": win.get("end")}
    if mode == "range":
        for key in ("start", "end"):
            if not isinstance(window[key], str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z", window[key]):
                raise StrategyError(f"evaluation.window.{key} must be a UTC time like 2026-11-01T00:00:00Z")
        if window["end"] <= window["start"]:
            raise StrategyError("evaluation.window.end must be after start")

    raw_conditions = top.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions or len(raw_conditions) > 20:
        raise StrategyError("conditions must be a list of 1-20 conditions")
    seen: set[str] = set()
    conditions = [_condition(c, i, seen) for i, c in enumerate(raw_conditions)]
    if not any(c["enabled"] for c in conditions):
        raise StrategyError("at least one condition must be enabled")

    en = _keys(top.get("entry", {}), {"trigger", "max_entry_delay_minutes"}, "entry")
    trigger = en.get("trigger", "on_change")
    if trigger not in ("on_change", "every_step"):
        raise StrategyError("entry.trigger must be on_change or every_step")
    entry = {"trigger": trigger, "max_entry_delay_minutes": _num(en.get("max_entry_delay_minutes", 10),
                                                                 "entry.max_entry_delay_minutes", 1, 1440, integer=True)}

    ex = _keys(top.get("exit", {}), {"stop", "target", "max_holding_minutes", "exit_on_structure_flip", "breakeven",
                                     "trail"}, "exit")
    st = _keys(ex.get("stop", {}), {"type", "timeframe", "buffer_atr", "atr_multiple", "distance"}, "exit.stop")
    stop = {"type": st.get("type", "structure"), "timeframe": st.get("timeframe", "M5"),
            "buffer_atr": _num(st.get("buffer_atr", 0.1), "exit.stop.buffer_atr", 0, 10),
            "atr_multiple": _num(st.get("atr_multiple", 1.5), "exit.stop.atr_multiple", 0.05, 50),
            "distance": None if st.get("distance") is None else _num(st["distance"], "exit.stop.distance", 0.01, 10000)}
    if stop["type"] not in STOP_TYPES:
        raise StrategyError(f"exit.stop.type must be one of {list(STOP_TYPES)}")
    if stop["timeframe"] not in TIMEFRAMES:
        raise StrategyError(f"exit.stop.timeframe must be one of {list(TIMEFRAMES)}")
    if stop["type"] == "fixed" and stop["distance"] is None:
        raise StrategyError("exit.stop.distance is required for a fixed stop")
    if stop["type"] == "none" and (ex.get("breakeven") or {}).get("enabled") or \
            stop["type"] == "none" and (ex.get("trail") or {}).get("enabled"):
        raise StrategyError("a break-even or trailing stop needs a stop: exit.stop.type must not be none")
    tg = _keys(ex.get("target", {}), {"type", "r", "atr_multiple", "distance", "timeframe"}, "exit.target")
    target = {"type": tg.get("type", "r_multiple"), "r": _num(tg.get("r", 2.0), "exit.target.r", 0.1, 50),
              "atr_multiple": None if tg.get("atr_multiple") is None else
              _num(tg["atr_multiple"], "exit.target.atr_multiple", 0.05, 100),
              "distance": None if tg.get("distance") is None else _num(tg["distance"], "exit.target.distance", 0.01, 10000),
              "timeframe": tg.get("timeframe", "M5")}
    if target["type"] not in TARGET_TYPES:
        raise StrategyError(f"exit.target.type must be one of {list(TARGET_TYPES)}")
    if target["timeframe"] not in TIMEFRAMES:
        raise StrategyError(f"exit.target.timeframe must be one of {list(TIMEFRAMES)}")
    if target["type"] == "atr" and target["atr_multiple"] is None:
        raise StrategyError("exit.target.atr_multiple is required for an ATR target")
    if target["type"] == "fixed" and target["distance"] is None:
        raise StrategyError("exit.target.distance is required for a fixed target")
    if stop["type"] == "none" and target["type"] == "r_multiple":
        raise StrategyError("an R-multiple target needs a stop: with exit.stop.type none, use target type none, atr or fixed")
    flip = _keys(ex.get("exit_on_structure_flip", {}), {"enabled", "timeframe"}, "exit.exit_on_structure_flip")
    flip_out = {"enabled": flip.get("enabled", False), "timeframe": flip.get("timeframe", "M5")}
    if not isinstance(flip_out["enabled"], bool) or flip_out["timeframe"] not in TIMEFRAMES:
        raise StrategyError("exit.exit_on_structure_flip needs enabled (true/false) and a timeframe")
    exit_cfg = {"stop": stop, "target": target,
                "max_holding_minutes": _num(ex.get("max_holding_minutes", 60), "exit.max_holding_minutes", 0, 10080,
                                            integer=True),
                "exit_on_structure_flip": flip_out}
    # Phase 5.2: optional break-even stop. It is OMITTED when off, so every existing configuration (and its
    # fingerprint) is unchanged.
    be = _keys(ex.get("breakeven") or {}, {"enabled", "trigger_r", "lock_r"}, "exit.breakeven")
    if be.get("enabled", False) is not False:
        if be.get("enabled") is not True:
            raise StrategyError("exit.breakeven.enabled must be true or false")
        be_out = {"enabled": True, "trigger_r": _num(be.get("trigger_r", 1.0), "exit.breakeven.trigger_r", 0.25, 10),
                  "lock_r": _num(be.get("lock_r", 0.0), "exit.breakeven.lock_r", 0, 5)}
        if be_out["lock_r"] >= be_out["trigger_r"]:
            raise StrategyError("exit.breakeven.lock_r must be below trigger_r")
        exit_cfg["breakeven"] = be_out
    # Phase 5.2: optional trailing stop (also omitted when off). After +activate_r, the stop follows the best price at
    # `distance` R (mode r) or `distance` x ATR of the stop timeframe at entry (mode atr); it only ever tightens.
    tr = _keys(ex.get("trail") or {}, {"enabled", "activate_r", "mode", "distance"}, "exit.trail")
    if tr.get("enabled", False) is not False:
        if tr.get("enabled") is not True:
            raise StrategyError("exit.trail.enabled must be true or false")
        tr_out = {"enabled": True, "activate_r": _num(tr.get("activate_r", 1.0), "exit.trail.activate_r", 0.25, 10),
                  "mode": tr.get("mode", "atr"), "distance": _num(tr.get("distance", 1.5), "exit.trail.distance", 0.1, 20)}
        if tr_out["mode"] not in ("r", "atr"):
            raise StrategyError("exit.trail.mode must be r or atr")
        exit_cfg["trail"] = tr_out

    rk = _keys(top.get("risk", {}), {"sizing", "lots", "risk_percent", "max_open_positions", "max_exposure_lots"}, "risk")
    sizing = rk.get("sizing", "fixed_lots")
    if sizing not in ("fixed_lots", "risk_percent"):
        raise StrategyError("risk.sizing must be fixed_lots or risk_percent")
    risk = {"sizing": sizing, "lots": _dec(rk.get("lots", "0.02"), "risk.lots", Decimal("0.01"), Decimal("100")),
            "risk_percent": _num(rk.get("risk_percent", 0.5), "risk.risk_percent", 0.01, 10),
            "max_open_positions": _num(rk.get("max_open_positions", 1), "risk.max_open_positions", 1, 20, integer=True),
            "max_exposure_lots": _dec(rk.get("max_exposure_lots", "1.00"), "risk.max_exposure_lots", Decimal("0.01"),
                                      Decimal("1000"))}
    return {"schema": SCHEMA, "name": name.strip(), "description": description, "direction": direction,
            "evaluation": {"step_timeframe": step, "window": window}, "conditions": conditions, "entry": entry,
            "exit": exit_cfg, "risk": risk}


def fingerprint(config: dict[str, Any]) -> str:
    """Digest of the LOGIC (everything except name and description)."""
    logic = {k: v for k, v in config.items() if k not in ("name", "description")}
    return digest(logic)


def timeframes_used(config: dict[str, Any]) -> set[str]:
    out = {config["evaluation"]["step_timeframe"]}
    if config["exit"]["stop"]["type"] in ("structure", "atr"):     # structure / ATR stops read that timeframe
        out.add(config["exit"]["stop"]["timeframe"])
    if config["exit"]["target"]["type"] == "atr":
        out.add(config["exit"]["target"]["timeframe"])
    if config["exit"]["exit_on_structure_flip"]["enabled"]:
        out.add(config["exit"]["exit_on_structure_flip"]["timeframe"])
    for c in config["conditions"]:
        if not c["enabled"]:
            continue
        if "timeframe" in c:
            out.add(c["timeframe"])
        if c["type"] == "htf_alignment":
            out.update(c["timeframes"])
        if c["type"] == "spread" and c["max_spread_atr"] is not None:
            out.add("M5")
    return out


def detail_timeframes(config: dict[str, Any]) -> set[str]:
    """Timeframes whose liquidity / FVG / order-block analysis the strategy reads."""
    return {c["timeframe"] for c in config["conditions"] if c["enabled"] and c["type"] in DETAIL_TYPES}


def describe(config: dict[str, Any]) -> list[str]:
    """Plain-language list of the enabled conditions (for reports)."""
    out = []
    for c in config["conditions"]:
        if not c["enabled"]:
            continue
        params = ", ".join(f"{k}={v}" for k, v in c.items() if k not in ("id", "type", "enabled"))
        out.append(f"{CONDITIONS[c['type']]['label']} ({params})")
    return out


def catalogue() -> dict[str, Any]:
    return {"schema": SCHEMA, "timeframes": list(TIMEFRAMES), "step_timeframes": list(STEP_TIMEFRAMES),
            "conditions": {k: {"label": v["label"], "help": v["help"],
                               "params": {p: {"kind": kind, "default": default,
                                              "choices": list(rule) if kind in ("choice", "choices", "tfs") else None,
                                              "range": list(rule) if kind in ("int", "numopt") else None}
                                          for p, (kind, default, rule) in v["params"].items()}}
                           for k, v in CONDITIONS.items()},
            "stop_types": list(STOP_TYPES), "target_types": list(TARGET_TYPES), "templates": templates(),
            "note": "Templates are examples of how conditions combine. They are not claims of profitable strategies."}


def templates() -> list[dict[str, Any]]:
    a = {"name": "Example A: aligned structure + M5 BOS",
         "description": "H1 and M15 structure in the trade direction, a fresh M5 BOS in the same direction, and an "
                        "acceptable spread. An example, not a claim of profitability.",
         "direction": "both", "evaluation": {"step_timeframe": "M5"},
         "conditions": [
             {"id": "h1", "type": "structure_state", "timeframe": "H1", "state": "with"},
             {"id": "m15", "type": "structure_state", "timeframe": "M15", "state": "with"},
             {"id": "bos", "type": "structure_event", "timeframe": "M5", "event": "BOS", "direction": "with",
              "within_candles": 3},
             {"id": "spread", "type": "spread", "max_spread": 0.5}]}
    b = {"name": "Example B: sweep + CHoCH + FVG in session",
         "description": "A recent liquidity sweep, an M5 CHoCH and a same-direction FVG, during London or New York. "
                        "An example, not a claim of profitability.",
         "direction": "both", "evaluation": {"step_timeframe": "M5"},
         "conditions": [
             {"id": "sweep", "type": "liquidity_sweep", "timeframe": "M5", "within_candles": 12},
             {"id": "choch", "type": "structure_event", "timeframe": "M5", "event": "CHoCH", "direction": "with",
              "within_candles": 6},
             {"id": "fvg", "type": "fvg", "timeframe": "M5", "relation": "recent", "within_candles": 6},
             {"id": "sess", "type": "session", "sessions": ["London", "New York"]}]}
    c = {"name": "Example C: alignment + confirmation + volatility",
         "description": "H1/M15 alignment, M5 swing confirmation and a non-contracting M5 volatility regime. An "
                        "example, not a claim of profitability.",
         "direction": "both", "evaluation": {"step_timeframe": "M5"},
         "conditions": [
             {"id": "align", "type": "htf_alignment", "timeframes": ["H1", "M15"], "min_agree": 2},
             {"id": "swing", "type": "swing_sequence", "timeframe": "M5", "pattern": "pullback"},
             {"id": "vol", "type": "volatility_regime", "timeframe": "M5", "allowed": ["normal", "expanding"]}]}
    return [validate(t) for t in (a, b, c)]
