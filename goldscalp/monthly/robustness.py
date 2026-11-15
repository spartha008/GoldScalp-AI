"""Phase 5.2: strategy redesign and multi-month robustness research (definition robustness@1). Simulation only.

Protocol (fixed BEFORE any redesigned rule is run on any data):
  windows       calendar months clipped to the study range, each a SEPARATE hypothetical $1,000 account
  development   the windows before ``development_end`` - the only data used to study weaknesses and to choose
                variants
  hold-out      the windows from ``development_end`` - evaluated once, with the variant chosen on development
  walk-forward  anchored monthly folds: choose on every window before the test window, test on it
Configurations (all pre-declared here):
  benchmarks        the frozen originals (Example A v2, B v2, C v4 from the application database, read-only, and
                    research candidates R1-R9 from a fixed-lot baseline evaluation) - never modified
  exit experiments  each original's ENTRY unchanged, ONE exit dimension changed (time exit, structure exit,
                    target, stop width): independent experiments, compared on development data
  redesign variants per original, a small grid of entry variants x family exits; the variant with the best
                    development expectancy (minimum sample) becomes that strategy's research version
                    (Example A v3, B v3, C v5, R1 v2 ... R9 v2). Research versions live in the research store only;
                    adopting one into the application is a separate, explicit user decision.
Every timeline is computed once per (timeframe, window) and every configuration runs on it (identical inputs)."""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import statistics
import threading
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from goldscalp.backtesting import features
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import runner
from goldscalp.monthly import store as st
from goldscalp.strategies import builder

DEFINITION = "robustness@1"
RUN_TFS = ("M5", "M15", "M1")         # M5 first: the choices made on M5 decide what else runs on M15 / M1
SELECTION_TF = "M5"            # the strategies decide on M5-based conditions; M15 and M1 are reported alongside
MIN_DEV_TRADES = 30            # eligibility for selection on the development windows (pooled)
MIN_TRADES_PER_WINDOW = 7.5    # walk-forward folds: eligibility scales with the number of selection windows
MIN_COVERAGE = 0.95
LOOKBACK_DAYS = 5
COSTS = {"configured_spread": "0.30", "commission": "3.50", "slippage": "0.05"}


class RobustnessError(ValueError):
    pass


# ------------------------------------------------------------------ protocol: windows
def windows(start: datetime, end: datetime, development_end: datetime) -> list[dict[str, Any]]:
    """Calendar months clipped to [start, end). Windows before development_end are development, the rest hold-out."""
    if not start < development_end < end:
        raise RobustnessError("need start < development_end < end")
    out, cur, n = [], start, 1
    while cur < end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        w_end = min(nxt, end)
        if cur < development_end < w_end:
            raise RobustnessError("development_end must fall on a window boundary (the first day of a month)")
        out.append({"key": f"W{n}", "start": iso(cur), "end": iso(w_end), "month": cur.strftime("%Y-%m"),
                    "partial": cur.day != 1 or w_end != nxt,
                    "role": "development" if w_end <= development_end else "hold-out"})
        cur, n = w_end, n + 1
    return out


def folds(wins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anchored walk-forward: test each window from the third on, choosing on every earlier DEVELOPMENT window only.
    Hold-out windows never influence any choice (a hold-out test month after another hold-out month is chosen on the
    development windows, exactly like the final hold-out)."""
    return [{"test": wins[k]["key"], "select_on": [w["key"] for w in wins[:k] if w["role"] == "development"]}
            for k in range(2, len(wins))]


def strict_params(params: dict[str, Any]) -> dict[str, Any]:
    """The same study with every fold restricted to development windows (for studies stored before this rule)."""
    return {**params, "folds": folds(params["windows"]) if params.get("stage", "full") == "full" else []}


# ------------------------------------------------------------------ configurations
STRUCT_M5 = {"type": "structure", "timeframe": "M5", "buffer_atr": 0.1, "atr_multiple": 1.5}
STRUCT_M15 = {"type": "structure", "timeframe": "M15", "buffer_atr": 0.1, "atr_multiple": 1.5}


def _atr(m: float, tf: str = "M5") -> dict[str, Any]:
    return {"type": "atr", "timeframe": tf, "atr_multiple": m}


def _exit(stop: dict[str, Any], r: float, hold: int, flip: str | None = None) -> dict[str, Any]:
    return {"stop": dict(stop), "target": {"type": "r_multiple", "r": r}, "max_holding_minutes": hold,
            "exit_on_structure_flip": {"enabled": bool(flip), "timeframe": flip or "M5"}}


#: exits for the redesigned versions, per family (labels explain each)
REDESIGN_EXITS: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
    # Iteration 3, from the development diagnostics (rb_825da1543f0f): a longer or no time limit worsened 10-11 of 12
    # originals, the wider M15 structure stop improved 9 of 12, and stops under 1 ATR were the worst bucket.
    "trend": {"Xa": ("M15 structure stop, 2R, 60-minute limit", _exit(STRUCT_M15, 2.0, 60)),
              "Xb": ("M15 structure stop, 1.5R, 60-minute limit", _exit(STRUCT_M15, 1.5, 60)),
              "Xc": ("M5 structure stop, 2R, 60-minute limit", _exit(STRUCT_M5, 2.0, 60)),
              "Xd": ("M15 structure stop, 2R, 120-minute limit", _exit(STRUCT_M15, 2.0, 120))},
    "breakout": {"Xa": ("M15 structure stop, 2R, 60-minute limit", _exit(STRUCT_M15, 2.0, 60)),
                 "Xb": ("M15 structure stop, 1.5R, 60-minute limit", _exit(STRUCT_M15, 1.5, 60)),
                 "Xc": ("M5 structure stop, 2R, 60-minute limit", _exit(STRUCT_M5, 2.0, 60)),
                 "Xe": ("2.0 x ATR(M5) stop, 2R, 60-minute limit", _exit(_atr(2.0), 2.0, 60))},
    "reversal": {"Xb": ("M15 structure stop, 1.5R, 60-minute limit", _exit(STRUCT_M15, 1.5, 60)),
                 "Xf": ("M15 structure stop, 1R, 60-minute limit", _exit(STRUCT_M15, 1.0, 60)),
                 "Xg": ("M5 structure stop, 1.5R, 60-minute limit", _exit(STRUCT_M5, 1.5, 60)),
                 "Xc": ("M15 structure stop, 2R, 60-minute limit", _exit(STRUCT_M15, 2.0, 60))},
}


BREAKEVEN = {"enabled": True, "trigger_r": 1.0, "lock_r": 0.0}
TRAIL_R = {"enabled": True, "activate_r": 1.0, "mode": "r", "distance": 1.0}
TRAIL_ATR = {"enabled": True, "activate_r": 1.0, "mode": "atr", "distance": 1.5}


def _with_target(e: dict[str, Any], r: float) -> dict[str, Any]:
    return {**e, "target": {**e["target"], "type": "r_multiple", "r": r}}


#: independent exit experiments: each changes ONE dimension of the original's own exit
EXIT_EXPERIMENTS: dict[str, tuple[str, Any]] = {
    "E1": ("time limit 120 minutes", lambda e: {**e, "max_holding_minutes": 120}),
    "E2": ("time limit 240 minutes", lambda e: {**e, "max_holding_minutes": 240}),
    "E3": ("no time exit (stop / target / month end)", lambda e: {**e, "max_holding_minutes": 0}),
    "E4": ("structure exit: no time limit, exit on M5 structure flip",
           lambda e: {**e, "max_holding_minutes": 0, "exit_on_structure_flip": {"enabled": True, "timeframe": "M5"}}),
    "E5": ("target 1.5R", lambda e: _with_target(e, 1.5)),
    "E6": ("target 1R", lambda e: _with_target(e, 1.0)),
    "E7": ("tight stop: 1.0 x ATR(M5)", lambda e: {**e, "stop": _atr(1.0)}),
    "E8": ("wide stop: M15 structure", lambda e: {**e, "stop": dict(STRUCT_M15)}),
    "E9": ("break-even stop at +1R", lambda e: {**e, "breakeven": dict(BREAKEVEN)}),
    "E10": ("trailing stop 1R behind the best price after +1R (target kept)", lambda e: {**e, "trail": dict(TRAIL_R)}),
    "E11": ("let winners run: 1.5 x ATR trailing stop after +1R, no target, no time limit",
            lambda e: {**e, "trail": dict(TRAIL_ATR), "target": {"type": "none"}, "max_holding_minutes": 0}),
}

#: stop-management overlays applied to EVERY redesign variant (paired comparison: same entry and exit otherwise)
OVERLAYS: dict[str, tuple[str, Any]] = {
    "be": ("+ break-even stop at +1R", lambda e: {**e, "breakeven": dict(BREAKEVEN)}),
    "tr": ("+ trailing stop 1R behind the best price after +1R (target kept)", lambda e: {**e, "trail": dict(TRAIL_R)}),
}

FAMILY = {"A": "trend", "B": "breakout", "C": "reversal", "R1": "trend", "R2": "breakout", "R3": "reversal",
          "R4": "breakout", "R5": "trend", "R6": "trend", "R7": "breakout", "R8": "reversal", "R9": "breakout"}
VERSION_NAMES = {"A": "Example A v3 — trend continuation", "B": "Example B v3 — breakout acceptance",
                 "C": "Example C v5 — liquidity sweep reversal"}
SPREAD = {"id": "spread", "type": "spread", "max_spread": 0.5, "if_unknown": "fail"}
LDN_NY = {"id": "sess", "type": "session", "sessions": ["London", "New York"], "overlap": "any"}
LDN_ONLY = {"id": "sess", "type": "session", "sessions": ["London"], "overlap": "excluded"}
NY_ONLY = {"id": "sess", "type": "session", "sessions": ["New York"], "overlap": "any"}
VOL_ACTIVE = {"id": "vol", "type": "volatility_regime", "timeframe": "M5", "allowed": ["normal", "expanding"]}
VOL_CALM_M15 = {"id": "volq", "type": "volatility_regime", "timeframe": "M15", "allowed": ["contracting", "normal"]}
HTF_CLEAR = {"id": "htf", "type": "htf_alignment", "timeframes": ["H1", "M15"], "min_agree": 2, "none_against": True}
VOL_NORMAL_M15 = {"id": "voln", "type": "volatility_regime", "timeframe": "M15", "allowed": ["normal"]}
H1_WITH = {"id": "h1w", "type": "structure_state", "timeframe": "H1", "state": "with"}
ASIA_LDN = {"id": "sess", "type": "session", "sessions": ["Asian", "London"], "overlap": "any"}
H1_NOT_AGAINST = {"id": "h1na", "type": "structure_state", "timeframe": "H1", "state": "not_against"}


def _c(**kw: Any) -> dict[str, Any]:
    return dict(kw)


def _replace(conds: list[dict[str, Any]], cid: str, new: dict[str, Any]) -> list[dict[str, Any]]:
    return [new if c["id"] == cid else c for c in conds]


def _plain(conds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in c.items() if k != "enabled"} for c in conds]


def entry_variants(key: str, original: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """(label, description, conditions) for each pre-declared entry variant of the redesigned version of ``key``."""
    base = _plain(original["conditions"])
    if key == "A":       # trend continuation (user hypothesis)
        core = [dict(HTF_CLEAR), _c(id="ema15", type="ema_trend", timeframe="M15", fast="20", slow="50", mode="both"),
                _c(id="pull", type="swing_sequence", timeframe="M5", pattern="pullback"),
                _c(id="mom", type="momentum", timeframe="M5", level=55, mode="confirm"), dict(SPREAD)]
        return [("a", "H1+M15 aligned, M15 EMA trend, M5 pullback, M5 momentum >= 55", core),
                ("b", "a + M15 volatility normal (expanding and contracting were negative for A, C, R5, R6)",
                 core + [dict(VOL_NORMAL_M15)]),
                ("c", "a + Asian / London sessions (New York was negative for A, R1, R5 in development)",
                 core + [dict(ASIA_LDN)]),
                ("d", "a + M15 volatility normal + London / New York (your session hypothesis)",
                 core + [dict(VOL_NORMAL_M15), dict(LDN_NY)])]
    if key == "B":       # breakout acceptance (user hypothesis); London and New York compared separately
        # Design iteration 2 (frequency check on development data, 8-11 Sep, before any result was seen): accepted
        # PD/session LEVEL breaks were known within 3 M5 candles on only 4 of 1,656 side-snapshots, so the first grid
        # could not trade. The structure break is now the M15 BOS (accepted: M15 structure in the direction), the retest
        # is a pullback into the breakout's M5 fair value gap, and one level-acceptance retest variant is kept, looser.
        accept = [_c(id="bos", type="structure_event", timeframe="M15", event="BOS", direction="with", within_candles=4),
                  _c(id="acc", type="structure_state", timeframe="M15", state="with"), dict(H1_WITH),
                  _c(id="mom", type="momentum", timeframe="M5", level=55, mode="confirm"), dict(VOL_ACTIVE), dict(SPREAD)]
        retest = [_c(id="bos", type="structure_event", timeframe="M15", event="BOS", direction="with", within_candles=8),
                  _c(id="acc", type="structure_state", timeframe="M15", state="with"), dict(H1_WITH),
                  _c(id="fvg", type="fvg", timeframe="M5", relation="price_inside", within_candles=12, allow_partial=True),
                  _c(id="mom", type="momentum", timeframe="M15", level=55, mode="confirm"), dict(VOL_ACTIVE), dict(SPREAD)]
        level = [_c(id="rt", type="level_retest", timeframe="M5", within_candles=48, min_age_candles=2,
                    max_distance_atr=1.0, level_types=["PD", "session"]), dict(H1_WITH),
                 _c(id="mom", type="momentum", timeframe="M15", level=55, mode="confirm"), dict(VOL_ACTIVE), dict(SPREAD)]
        return [("a", "accepted M15 BOS + M5 momentum, London only", accept + [dict(LDN_ONLY)]),
                ("b", "accepted M15 BOS + M5 momentum, New York", accept + [dict(NY_ONLY)]),
                ("c", "retest: accepted M15 BOS, price back in the M5 breakout FVG, M15 momentum, London only",
                 retest + [dict(LDN_ONLY)]),
                ("d", "retest: accepted M15 BOS, price back in the M5 breakout FVG, M15 momentum, New York",
                 retest + [dict(NY_ONLY)]),
                ("e", "retest of an accepted PD/session level break (within 1 ATR, 4 hours), London / New York",
                 level + [dict(LDN_NY)])]
    if key == "C":       # liquidity sweep reversal in confirmed ranges (user hypothesis)
        # Design iteration 2 (same frequency check): an M5 sweep within 6 candles AND an M5 CHoCH within 4 candles
        # coincided 0 times in 3 days, so the windows are widened to 12 and 6 candles (1 hour / 30 minutes).
        core = [_c(id="range", type="structure_state", timeframe="H1", state="ranging"),
                _c(id="sweep", type="liquidity_sweep", timeframe="M5", within_candles=12, level_types=["EQ", "PD", "PW", "session"]),
                _c(id="choch", type="structure_event", timeframe="M5", event="CHoCH", direction="with", within_candles=6),
                dict(VOL_CALM_M15), dict(SPREAD)]
        return [("a", "H1 range, M5 sweep, M5 CHoCH, M15 volatility not expanding", core),
                ("b", "a + M5 momentum not stretched (RSI <= 60)", core + [_c(id="mom", type="momentum", timeframe="M5",
                                                                               level=60, mode="not_extreme")]),
                ("c", "trend-aligned instead of range: H1 structure WITH the trade (sweep = pullback liquidity grab)",
                 _replace(core, "range", dict(H1_WITH))),
                ("d", "a with the sweep on M15 levels (within 4 M15 candles)", _replace(core, "sweep", _c(
                    id="sweep", type="liquidity_sweep", timeframe="M15", within_candles=4, level_types=["EQ", "PD", "PW", "session"])))]
    fam = FAMILY[key]
    ids = {c["id"] for c in base}
    out = [("a", "original entry", base)]
    if key == "R7":      # too selective in research (6 trades in 6 weeks): loosen, keep the concept
        loose = _replace(_replace(base, "brk", _c(id="brk", type="level_break", timeframe="M5", within_candles=6,
                                                  level_types=["PD", "session"])),
                         "mom", _c(id="mom", type="momentum", timeframe="M5", level=52, mode="confirm"))
        return out + [("b", "loosened: break within 6 candles, momentum >= 52", loose),
                      ("c", "loosened, London and New York (overlap not required)", _replace(loose, "ovl", dict(LDN_NY)))]
    if key == "R8":      # too selective (2 trades in 6 weeks): loosen windows, add New York
        loose = _replace(_replace(base, "sweep", _c(id="sweep", type="liquidity_sweep", timeframe="M5", within_candles=8,
                                                    level_types=["PD", "session", "EQ"])),
                         "choch", _c(id="choch", type="structure_event", timeframe="M5", event="CHoCH", direction="with",
                                     within_candles=8))
        return out + [("b", "loosened: sweep and CHoCH within 8 candles", loose),
                      ("c", "loosened, London and New York", _replace(loose, "ldn", dict(LDN_NY)))]
    if fam == "trend":
        clear = dict(HTF_CLEAR) if "htf" not in ids else dict(H1_WITH)
        return out + [("b", "original + clear H1 trend (H1 ranging was negative for R1, R6)", base + [clear]),
                      ("c", "original + M15 volatility normal", base + [dict(VOL_NORMAL_M15)]),
                      ("d", "original + Asian / London sessions (New York was negative for A, R1, R5)",
                       base + ([dict(ASIA_LDN)] if "sess" not in ids else []))]
    if fam == "breakout":
        b_extra = [dict(H1_WITH)] if "h1" not in ids and "htf" not in ids else \
            [_c(id="mom15", type="momentum", timeframe="M15", level=55, mode="confirm")]
        if "brk" in ids or "fvg" in ids:
            c_label, c_conds = "retest of an accepted break instead of the original trigger", \
                [c for c in base if c["id"] not in ("brk", "fvg")] + [_c(
                    id="rt", type="level_retest", timeframe="M5", within_candles=24, min_age_candles=2, max_distance_atr=0.5,
                    level_types=["PD", "session"])]
        else:
            c_label, c_conds = "original + accepted PD/session break (acceptance)", base + [_c(
                id="brk", type="level_break", timeframe="M5", within_candles=6, level_types=["PD", "session"])]
        return out + [("b", "original + higher-timeframe confirmation", base + b_extra), ("c", c_label, c_conds),
                      ("d", "original + M15 volatility normal (expanding and contracting were negative for several "
                            "strategies)", base + [dict(VOL_NORMAL_M15)])]
    # reversal (R3)
    return out + [("b", "original + M15 volatility not expanding", base + [dict(VOL_CALM_M15)]),
                  ("c", "original + Asian / London only (New York was the worst session for R3)", base + [dict(ASIA_LDN)]),
                  ("d", "trend-aligned: H1 structure WITH the trade instead of ranging", _replace(base, "range", dict(H1_WITH)))]


def _config(original: dict[str, Any], *, name: str, description: str, conditions: list[dict[str, Any]] | None = None,
            exit_: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(original)
    cfg["name"], cfg["description"] = name[:80], description[:400]
    if conditions is not None:
        cfg["conditions"] = copy.deepcopy(conditions)
    if exit_ is not None:
        cfg["exit"] = copy.deepcopy(exit_)
    cfg["conditions"] = _plain(cfg["conditions"])
    return builder.validate(cfg)


def build_configs(originals: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every configuration of the study, pre-declared. ``originals``: key -> {name, config, version, source}."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(item: dict[str, Any]) -> None:
        """Duplicates are dropped only within the same strategy and role, so every grid stays complete."""
        fp = builder.fingerprint(item["config"])
        if (item["strategy"], item["role"], fp) in seen:
            return
        seen.add((item["strategy"], item["role"], fp))
        out.append({**item, "fingerprint": fp})

    for key, o in originals.items():
        add({"key": f"{key}|original", "strategy": key, "role": "benchmark", "family": FAMILY[key],
             "label": f"{o['name']} (benchmark, unchanged)", "config": o["config"]})
    for key, o in originals.items():
        orig_fp = builder.fingerprint(o["config"])
        for ek, (label, fn) in EXIT_EXPERIMENTS.items():
            cfg = _config(o["config"], name=f"{key} exit experiment {ek}", description=f"{o['name']} entry; exit: {label}",
                          exit_=fn(copy.deepcopy(o["config"]["exit"])))
            if builder.fingerprint(cfg) != orig_fp:
                add({"key": f"{key}|{ek}", "strategy": key, "role": "exit_experiment", "family": FAMILY[key],
                     "experiment": ek, "label": label, "config": cfg})
    for key, o in originals.items():
        fam = FAMILY[key]
        for ev, edesc, conds in entry_variants(key, o["config"]):
            for xk, (xdesc, ex) in REDESIGN_EXITS[fam].items():
                for ok, (odesc, ofn) in [("", ("", None)), *OVERLAYS.items()]:
                    if ok == "be" and ex["target"].get("type") == "r_multiple" and ex["target"]["r"] <= 1.0:
                        continue                       # a 1R target closes before a break-even move could matter
                    exit_ = ofn(copy.deepcopy(ex)) if ofn else ex
                    suffix = f"-{ok}" if ok else ""
                    name = f"{VERSION_NAMES.get(key, key + ' v2')} [{ev}/{xk}{suffix}]"
                    cfg = _config(o["config"], name=name, description=f"Entry: {edesc}. Exit: {xdesc}{' ' + odesc if odesc else ''}.",
                                  conditions=conds, exit_=exit_)
                    add({"key": f"{key}|v-{ev}-{xk}{suffix}", "strategy": key, "role": "redesign", "family": fam,
                         "entry": ev, "exit": xk, "overlay": ok or "none",
                         "label": f"entry {ev}: {edesc}; exit {xk}: {xdesc}{' ' + odesc if odesc else ''}", "config": cfg})
    return out


# ------------------------------------------------------------------ the job
def _coverage(src: Any, store: str, row: Any, tf: Timeframe, start: datetime, end: datetime, schedule: Any) -> dict[str, Any]:
    frame = src.load(store, row, tf, start - timedelta(days=LOOKBACK_DAYS + 7), end, limit=2_000_000)
    expected = schedule.expected_bars(start, end, tf.seconds)
    present = frame.index[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    share = len(present.intersection(expected)) / len(expected) if len(expected) else 0.0
    scheduled = schedule.expected_bars(start - timedelta(days=LOOKBACK_DAYS), start, tf.seconds)
    needed = scheduled[0] if len(scheduled) else pd.Timestamp(start - timedelta(days=LOOKBACK_DAYS))
    before = frame.index[frame.index < pd.Timestamp(start)]
    warm = len(before) > 0 and before[0] <= needed
    problems = []
    if share < MIN_COVERAGE:
        problems.append(f"{tf.value} coverage {share:.1%} (need {MIN_COVERAGE:.0%})")
    if not warm:
        problems.append(f"{tf.value} history before the window starts {iso(before[0].to_pydatetime()) if len(before) else 'nowhere'}")
    return {"share": round(share, 4), "expected": int(len(expected)), "present": int(len(present)), "warmup": warm,
            "sufficient": not problems, "problems": problems}


def _rel(state: str | None, side: str) -> str:
    if state in (None, "no data"):
        return "unknown"
    if state == "ranging":
        return "ranging"
    return "with" if (state == "bullish") == (side == "buy") else "against"


def compact(trades: list[dict[str, Any]], snaps: dict[str, dict[str, Any]], stop_tf: str) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        s = snaps.get(t["setup_cutoff"]) or {"tf": {}}
        h1, m15 = s["tf"].get("H1") or {}, s["tf"].get("M15") or {}
        atr = ((s["tf"].get(stop_tf) or {}).get("vol") or {}).get("atr")
        risk_oz = abs(t["entry_price"] - t["stop"]) if t.get("stop") is not None else None
        ounces = float(t["ounces"])
        costs = (t.get("commission") or 0) + (t.get("spread_cost") or 0) + (t.get("slippage_cost") or 0)
        out.append({"entry": t["entry_time"], "exit": t["exit_time"], "side": t["side"], "lots": t["lots"],
                    "reason": t["exit_reason"], "net": t["net_pnl"], "r": t.get("r_multiple"),
                    "mfe_r": t.get("mfe_r"), "mae_r": t.get("mae_r"), "hold": t["holding_minutes"],
                    "session": t.get("session"), "vol_m5": t.get("volatility"),
                    "vol_m15": (m15.get("vol") or {}).get("condition") or "unknown",
                    "h1": _rel(h1.get("state"), t["side"]), "m15": _rel(m15.get("state"), t["side"]),
                    "stop_atr": round(risk_oz / atr, 3) if risk_oz and atr else None,
                    "risk_usd": round(risk_oz * ounces, 2) if risk_oz else None, "cost_usd": round(costs, 2)})
    return out


def summarize(trades: list[dict[str, Any]], starting_balance: float = 1000.0) -> dict[str, Any]:
    """R-based (sizing-independent) and USD figures; trades in exit order."""
    ts = sorted(trades, key=lambda t: t["exit"])
    rs = [t["r"] for t in ts if t["r"] is not None]
    nets = [t["net"] for t in ts]
    gp = sum(n for n in nets if n > 0)
    gl = -sum(n for n in nets if n < 0)
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    eq = peak_e = starting_balance
    dd_usd = 0.0
    for n in nets:
        eq += n
        peak_e = max(peak_e, eq)
        dd_usd = max(dd_usd, peak_e - eq)
    risk = [t["risk_usd"] for t in ts if t.get("risk_usd")]
    costs = [t["cost_usd"] for t in ts]
    return {"trades": len(ts), "wins": sum(1 for n in nets if n > 0), "losses": sum(1 for n in nets if n < 0),
            "win_rate": round(sum(1 for n in nets if n > 0) / len(ts), 4) if ts else None,
            "net_usd": round(sum(nets), 2), "sum_r": round(sum(rs), 3),
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
            "profit_factor": round(gp / gl, 3) if gl > 0 else (None if not gp else 99.0),
            "max_dd_r": round(dd, 3), "max_dd_usd": round(dd_usd, 2),
            "avg_hold_minutes": round(sum(t["hold"] for t in ts) / len(ts), 1) if ts else None,
            "exits": dict(Counter(t["reason"] for t in ts)),
            "costs_usd": round(sum(costs), 2),
            "cost_r": round(sum(costs) / sum(risk), 4) if risk and sum(risk) else None}


STAGES = ("diagnostic", "full")


def tf_roles(stage: str, tf: str) -> set[str]:
    """Which configuration roles run on which timeframe. Exit experiments are judged on development M5 only; the
    diagnostic stage needs only the originals' M5 + M15 trades (weakness study) and the M5 experiments."""
    if stage == "diagnostic":
        return {"M5": {"benchmark", "exit_experiment"}, "M15": {"benchmark"}}.get(tf, set())
    return {"M5": {"benchmark", "redesign"}}.get(tf, {"benchmark", "chosen"})


def create(ms: Any, *, start: datetime, end: datetime, development_end: datetime, baseline_evaluation: str,
           sources: dict[str, tuple[str, str]], stage: str = "full", diagnostic_study: str | None = None) -> dict[str, Any]:
    if stage not in STAGES:
        raise RobustnessError(f"stage must be one of {STAGES}")
    wins = windows(start, end, development_end)
    if stage == "diagnostic":                     # development months only: the weakness study comes first
        wins = [w for w in wins if w["role"] == "development"]
    with ms.store.read() as con:
        base = st.evaluation(con, baseline_evaluation)
        cands = st.candidates(con, baseline_evaluation) if base else []
    if base is None or not base.get("frozen"):
        raise RobustnessError(f"baseline evaluation {baseline_evaluation} is missing or not frozen")
    originals: dict[str, dict[str, Any]] = {}
    for f in base["frozen"]["strategies"]:
        rec = ms.sources.strategy(f["strategy_id"], f["version"])
        if rec is None or builder.fingerprint(rec["config"]) != f["fingerprint"] or rec["fingerprint"] != f["fingerprint"]:
            raise RobustnessError(f"{f['name']} v{f['version']}: the stored version no longer matches its fingerprint")
        originals[f["key"]] = {"name": f"{rec['name']} v{rec['version']}", "config": rec["config"],
                               "version": rec["version"], "source": "application database (read-only)",
                               "fingerprint": f["fingerprint"]}
    for c in cands:
        originals[c["key"]] = {"name": c["name"], "config": c["config"], "version": c["version"],
                               "source": f"research candidate frozen in {baseline_evaluation}", "fingerprint": c["fingerprint"]}
    configs = build_configs(originals)
    if stage == "diagnostic":
        configs = [c for c in configs if c["role"] != "redesign"]
    else:                                   # exit experiments were judged in the diagnostic stage
        configs = [c for c in configs if c["role"] != "exit_experiment"]
    sid = "rb_" + secrets.token_hex(6)
    total = sum(1 for tf in RUN_TFS for c in configs if c["role"] in tf_roles(stage, tf)) * len(wins)
    if stage == "full":   # M15 / M1 also run each strategy's development and walk-forward choices (known after M5)
        total += len(originals) * 2 * len(wins)
    params = {"definition": DEFINITION, "stage": stage, "windows": wins,
              "folds": folds(wins) if stage == "full" else [], "selection_tf": SELECTION_TF,
              "min_dev_trades": MIN_DEV_TRADES, "baseline_evaluation": baseline_evaluation,
              "sources": {k: list(v) for k, v in sources.items()}, "costs": COSTS, "lots": "as configured (0.02)",
              "starting_balance": runner.STARTING_BALANCE, "leverage": runner.LEVERAGE,
              "originals": {k: {kk: v[kk] for kk in ("name", "version", "source", "fingerprint")} for k, v in originals.items()},
              "configs": [{k: c[k] for k in c if k != "config"} for c in configs],
              "note": "Development windows choose; hold-out windows are evaluated once with the development choice.",
              "diagnostic_study": diagnostic_study,
              "design_iterations": ["1: first grids (study rb_da267aa33110, cancelled after one window).",
                                    "2: B v3 and C v5 entry grids revised after a frequency check on development data "
                                    "(8-11 Sep 2026): the first B v3 / C v5 combinations fired less than once a week. Only "
                                    "how often conditions pass was looked at, no result.",
                                    "3: exits and filters revised from the diagnostic study rb_825da1543f0f (development "
                                    "months only): 60-minute limits and M15 structure stops (longer holds hurt 10-11 of 12 "
                                    "originals; the M15 structure stop helped 9 of 12); M15 volatility normal, H1 trend and "
                                    "session variants supported by several strategies; trailing overlay keeps the target."]}
    now = ms.clock.now()
    with ms.store.transaction() as con:
        con.execute("INSERT INTO robustness_studies (id, status, params, app_version, created_at, updated_at, progress, total) "
                    "VALUES (?, 'READY', ?, ?, ?, ?, 0, ?)", (sid, json.dumps(params, default=str), runner.app_version(),
                                                              iso(now), iso(now), total))
        con.executemany("INSERT INTO robustness_configs (study_id, key, config, fingerprint) VALUES (?, ?, ?, ?)",
                        [(sid, c["key"], json.dumps(c["config"], sort_keys=True), c["fingerprint"]) for c in configs])
    return {"id": sid, "configs": len(configs), "windows": wins}


def _update(ms: Any, sid: str, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(ms.clock.now())]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(json.dumps(v, default=str) if k == "analysis" else v)
    with ms.store.transaction() as con:
        con.execute(f"UPDATE robustness_studies SET {', '.join(sets)} WHERE id = ?", (*args, sid))


def run_study(ms: Any, sid: str, cancel: threading.Event) -> None:
    with ms.store.read() as con:
        row = con.execute("SELECT * FROM robustness_studies WHERE id = ?", (sid,)).fetchone()
        configs = {r["key"]: json.loads(r["config"]) for r in con.execute(
            "SELECT key, config FROM robustness_configs WHERE study_id = ?", (sid,))}
    params = json.loads(row["params"])
    stage = params.get("stage", "full")
    roles = {c["key"]: c["role"] for c in params["configs"]}
    spec, contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    assumptions = runner.make_assumptions(COSTS)
    all_configs = configs
    _update(ms, sid, status="RUNNING", error=None)
    done = 0
    try:
        for tf_name in RUN_TFS:
            wanted = tf_roles(stage, tf_name)
            if "chosen" in wanted:          # the development and walk-forward choices, made on the stored M5 results
                chosen = chosen_keys(load_results(ms, sid), params)
                configs = {k: c for k, c in all_configs.items() if roles[k] in wanted or k in chosen}
            else:
                configs = {k: c for k, c in all_configs.items() if roles[k] in wanted}
            if not configs:
                continue
            detail = set().union(*(builder.detail_timeframes(c) for c in configs.values()))
            tf = Timeframe.parse(tf_name)
            store, series = params["sources"][tf_name]
            srow = ms.sources.series_row(store, series)
            base_tf = Timeframe.M1 if tf is Timeframe.M1 else Timeframe.M5
            for w in params["windows"]:
                start, end = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
                cov = _coverage(ms.sources, store, srow, base_tf, start, end, ms.app_spec.schedule) if srow else \
                    {"sufficient": False, "problems": [f"series {series} not found in the {store} store"]}
                if srow and cov["sufficient"] and base_tf is not tf:
                    ex = _coverage(ms.sources, store, srow, tf, start, end, ms.app_spec.schedule)
                    if not ex["sufficient"]:
                        cov = ex
                if not cov["sufficient"]:
                    with ms.store.transaction() as con:
                        con.executemany("INSERT OR REPLACE INTO robustness_results (study_id, config_key, timeframe, window, "
                                        "status, reason) VALUES (?, ?, ?, ?, 'INCOMPLETE', ?)",
                                        [(sid, k, tf_name, w["key"], "; ".join(cov["problems"])) for k in configs])
                    done += len(configs)
                    _update(ms, sid, progress=done, phase=f"{tf_name} {w['key']} incomplete")
                    continue

                def tick(n: int, tf_name: str = tf_name, w: dict[str, Any] = w) -> None:
                    if n % 200 == 0:
                        _update(ms, sid, phase=f"{tf_name} {w['key']} ({w['month']}) timeline: {n} steps")
                prepared = runner.prepare(ms.sources, store, srow, tf, start, end, lookback_days=LOOKBACK_DAYS,
                                          detail=detail, cfg=ms.analysis_cfg, progress=tick, cancelled=cancel.is_set)
                snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
                rows = []
                for key, cfg in configs.items():
                    if cancel.is_set():
                        raise features.Cancelled("cancelled")
                    res = runner.run_one(cfg, prepared, tf, start, end, assumptions, spec)
                    trades = compact(res["trades"], snaps, cfg["exit"]["stop"]["timeframe"])
                    rows.append((sid, key, tf_name, w["key"], "COMPLETED", None, json.dumps(summarize(trades)),
                                 json.dumps(trades), json.dumps(prepared["dataset"], default=str), res["result_hash"],
                                 json.dumps({"setups": res["diagnostics"]["setups"], "skipped": res["diagnostics"]["skipped"]})))
                done += len(configs)
                with ms.store.transaction() as con:
                    con.executemany("INSERT OR REPLACE INTO robustness_results (study_id, config_key, timeframe, window, "
                                    "status, reason, metrics, trades, dataset, result_hash, diagnostics) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                _update(ms, sid, progress=done, phase=f"{tf_name} {w['key']} done")
                del prepared, snaps
        _update(ms, sid, phase="analysis")
        analysis = analyse(load_results(ms, sid), params)
        _update(ms, sid, status="COMPLETED", phase=None, analysis=analysis, finished_at=iso(ms.clock.now()))
        if stage == "full":
            persist_versions(ms, sid, analysis, params)
    except Exception as exc:  # noqa: BLE001 - recorded; the service keeps running
        msg = "cancelled" if isinstance(exc, features.Cancelled) else f"{type(exc).__name__}: {exc}"
        _update(ms, sid, status="FAILED", error=msg, phase=None, finished_at=iso(ms.clock.now()))


def load_results(ms: Any, sid: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    with ms.store.read() as con:
        for r in con.execute("SELECT config_key, timeframe, window, status, reason, metrics, trades FROM robustness_results "
                             "WHERE study_id = ?", (sid,)):
            out[(r["config_key"], r["timeframe"], r["window"])] = {
                "status": r["status"], "reason": r["reason"], "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
                "trades": json.loads(r["trades"]) if r["trades"] else []}
    return out


# ------------------------------------------------------------------ analyses (pure functions over stored results)
def pooled(results: dict, key: str, tf: str, wins: list[str]) -> dict[str, Any]:
    trades, missing = [], []
    for w in wins:
        r = results.get((key, tf, w))
        if r is None or r["status"] != "COMPLETED":
            missing.append(w)
            continue
        trades.extend(r["trades"])
    out = summarize(trades)
    out["windows"], out["missing_windows"] = list(wins), missing
    out["positive_windows"] = sum(1 for w in wins if (results.get((key, tf, w)) or {}).get("status") == "COMPLETED"
                                  and summarize(results[(key, tf, w)]["trades"])["sum_r"] > 0)
    return out


def select(results: dict, candidates: list[str], wins: list[str], *, allowed: list[str],
           min_trades: float) -> dict[str, Any]:
    """Choose among ``candidates`` on ``wins`` only. ``allowed`` is the protocol's permitted set (the guard)."""
    if not set(wins) <= set(allowed):
        raise RobustnessError(f"selection may only use {allowed}, not {sorted(set(wins) - set(allowed))}")
    rows = []
    for k in candidates:
        m = pooled(results, k, SELECTION_TF, wins)
        rows.append({"key": k, **{f: m[f] for f in ("trades", "expectancy_r", "sum_r", "profit_factor", "max_dd_r",
                                                     "positive_windows")}})
    eligible = [r for r in rows if r["trades"] >= min_trades and r["expectancy_r"] is not None]
    if eligible:
        best = max(eligible, key=lambda r: (r["expectancy_r"], r["positive_windows"], -r["max_dd_r"]))
        return {"chosen": best["key"], "flag": None, "rows": rows,
                "rule": f"highest development expectancy (R after costs) with at least {min_trades:g} trades"}
    fallback = [r for r in rows if r["trades"] >= max(10, min_trades / 2) and r["expectancy_r"] is not None]
    if fallback:
        best = max(fallback, key=lambda r: (r["expectancy_r"], r["trades"]))
        return {"chosen": best["key"], "flag": "insufficient sample", "rows": rows,
                "rule": f"no variant reached {min_trades:g} trades; best of those with at least {max(10, min_trades / 2):g}"}
    return {"chosen": None, "flag": "no tradable variant", "rows": rows, "rule": "no variant traded enough to judge"}


def verdict(oos: dict[str, Any], hold: dict[str, Any], n_oos_windows: int) -> dict[str, str]:
    """Pre-declared, sizing-independent rules (R after costs)."""
    t, e = oos["trades"], oos["expectancy_r"]
    if t < 30 or e is None:
        return {"verdict": "insufficient sample", "why": f"{t} out-of-sample trades (30 needed)"}
    if e <= 0:
        return {"verdict": "fails", "why": f"out-of-sample expectancy {e:+.3f} R per trade after costs"}
    pf = oos["profit_factor"] or 0
    months_ok = oos["positive_windows"] >= max(1, round(0.75 * n_oos_windows))
    hold_ok = hold.get("expectancy_r") is not None and hold["expectancy_r"] > 0
    if pf >= 1.1 and months_ok and hold_ok:
        return {"verdict": "consistent", "why": f"OOS {e:+.3f} R/trade, PF {pf:.2f}, {oos['positive_windows']} of "
                                                f"{n_oos_windows} months positive, hold-out positive"}
    return {"verdict": "positive but inconsistent",
            "why": f"OOS {e:+.3f} R/trade, PF {pf:.2f}, {oos['positive_windows']} of {n_oos_windows} months positive, "
                   f"hold-out {'positive' if hold_ok else 'not positive'}"}


def _bucket(groups: dict[str, list[float]]) -> list[dict[str, Any]]:
    return [{"group": g, "trades": len(v), "avg_r": round(sum(v) / len(v), 3), "sum_r": round(sum(v), 2)}
            for g, v in sorted(groups.items(), key=lambda kv: -len(kv[1])) if v]


def weakness(results: dict, key: str, dev: list[str]) -> dict[str, Any]:
    """Why the ORIGINAL loses, on development windows only (M5 and M15 trades pooled)."""
    trades = [t for tf in ("M5", "M15") for w in dev
              for t in (results.get((key, tf, w)) or {}).get("trades", []) if t["r"] is not None]
    if not trades:
        return {"trades": 0, "findings": ["No development trades: nothing to analyse."]}
    n = len(trades)
    by = defaultdict(list)
    for t in trades:
        by[t["reason"]].append(t)
    exits = [{"exit": k, "trades": len(v), "share": round(len(v) / n, 3), "avg_r": round(statistics.fmean(x["r"] for x in v), 3)}
             for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))]
    timed = by.get("time", [])
    stops = by.get("stop", [])
    groups = {f: defaultdict(list) for f in ("session", "h1", "m15", "vol_m15", "stop_width", "side")}
    for t in trades:
        groups["side"][t["side"]].append(t["r"])
        groups["session"][t["session"] or "none"].append(t["r"])
        groups["h1"][t["h1"]].append(t["r"])
        groups["m15"][t["m15"]].append(t["r"])
        groups["vol_m15"][t["vol_m15"]].append(t["r"])
        sw = t["stop_atr"]
        groups["stop_width"]["unknown" if sw is None else "< 1 ATR" if sw < 1 else "1-2 ATR" if sw <= 2 else "> 2 ATR"].append(t["r"])
    cost_r = [t["cost_usd"] / t["risk_usd"] for t in trades if t.get("risk_usd")]
    out = {"trades": n, "expectancy_r": round(statistics.fmean(t["r"] for t in trades), 4), "exits": exits,
           "time_exits": {"share": round(len(timed) / n, 3),
                          "avg_r": round(statistics.fmean(t["r"] for t in timed), 3) if timed else None,
                          "positive_share": round(sum(1 for t in timed if t["r"] > 0) / len(timed), 3) if timed else None,
                          "reached_1r_share": round(sum(1 for t in timed if (t["mfe_r"] or 0) >= 1) / len(timed), 3) if timed else None},
           "stop_exits": {"share": round(len(stops) / n, 3),
                          "had_1r_first_share": round(sum(1 for t in stops if (t["mfe_r"] or 0) >= 1) / len(stops), 3) if stops else None,
                          "avg_mfe_r": round(statistics.fmean((t["mfe_r"] or 0) for t in stops), 3) if stops else None},
           "median_stop_atr": statistics.median([t["stop_atr"] for t in trades if t["stop_atr"]]) if any(t["stop_atr"] for t in trades) else None,
           "median_cost_r": round(statistics.median(cost_r), 4) if cost_r else None,
           "median_mfe_r": round(statistics.median([(t["mfe_r"] or 0) for t in trades]), 3),
           "mfe_reached": {f"{x:g}R": round(sum(1 for t in trades if (t["mfe_r"] or 0) >= x) / n, 3)
                           for x in (0.5, 1.0, 1.5, 2.0, 3.0)},
           "mae_before_win": round(statistics.median([(t["mae_r"] or 0) for t in trades if t["r"] > 0]), 3)
           if any(t["r"] > 0 for t in trades) else None,
           "by": {k: _bucket(v) for k, v in groups.items()}}
    f = []
    e = out["exits"]
    f.append(f"{n} development trades (M5 + M15), {out['expectancy_r']:+.3f} R per trade after costs. Exits: "
             + ", ".join(f"{x['exit']} {x['share']:.0%} (avg {x['avg_r']:+.2f} R)" for x in e) + ".")
    te = out["time_exits"]
    if timed and te["share"] >= 0.3:
        f.append(f"The time exit closes {te['share']:.0%} of trades at {te['avg_r']:+.2f} R on average; "
                 f"{te['reached_1r_share']:.0%} of them had been at least +1R in profit before being closed.")
    se = out["stop_exits"]
    if stops:
        f.append(f"Stop-outs: {se['share']:.0%} of trades; {se['had_1r_first_share']:.0%} had first reached +1R "
                 f"(average best excursion {se['avg_mfe_r']:.2f} R).")
    if out["median_cost_r"] is not None:
        f.append(f"Median cost per trade is {out['median_cost_r']:.2f} R (spread, commission, slippage relative to the "
                 f"planned risk); median stop width {out['median_stop_atr']:.2f} x ATR.")
    mr = out["mfe_reached"]
    f.append(f"How far trades went in favour (best excursion): {mr['0.5R']:.0%} reached +0.5R, {mr['1R']:.0%} +1R, "
             f"{mr['1.5R']:.0%} +1.5R, {mr['2R']:.0%} +2R, {mr['3R']:.0%} +3R.")
    for dim, label in (("h1", "H1 structure relative to the trade"), ("session", "session"), ("vol_m15", "M15 volatility"),
                       ("stop_width", "stop width"), ("side", "direction")):
        rows = [b for b in out["by"][dim] if b["trades"] >= 10]
        if len(rows) >= 2:
            best, worst = max(rows, key=lambda b: b["avg_r"]), min(rows, key=lambda b: b["avg_r"])
            if best["avg_r"] - worst["avg_r"] >= 0.15:
                f.append(f"{label}: best '{best['group']}' {best['avg_r']:+.2f} R over {best['trades']} trades, worst "
                         f"'{worst['group']}' {worst['avg_r']:+.2f} R over {worst['trades']}.")
    out["findings"] = f
    return out


def analyse(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("stage") == "diagnostic":
        return analyse_diagnostic(results, params)
    out = analyse_full(results, params)
    out["overlay_effect"] = overlay_effect(results, params)
    return out


def _exit_rows(results: dict, configs: list[dict[str, Any]], key: str, dev: list[str]) -> dict[str, Any]:
    orig = f"{key}|original"
    base_dev = pooled(results, orig, SELECTION_TF, dev)
    rows = []
    for c in [c for c in configs if c["strategy"] == key and c["role"] == "exit_experiment"]:
        m = pooled(results, c["key"], SELECTION_TF, dev)
        better = sum(1 for w in dev if (results.get((c["key"], SELECTION_TF, w)) or {}).get("status") == "COMPLETED"
                     and (results.get((orig, SELECTION_TF, w)) or {}).get("status") == "COMPLETED"
                     and summarize(results[(c["key"], SELECTION_TF, w)]["trades"])["sum_r"]
                     > summarize(results[(orig, SELECTION_TF, w)]["trades"])["sum_r"])
        rows.append({"experiment": c["experiment"], "label": c["label"], "trades": m["trades"],
                     "win_rate": m["win_rate"], "expectancy_r": m["expectancy_r"], "profit_factor": m["profit_factor"],
                     "max_dd_r": m["max_dd_r"],
                     "delta_expectancy_r": None if m["expectancy_r"] is None or base_dev["expectancy_r"] is None
                     else round(m["expectancy_r"] - base_dev["expectancy_r"], 4),
                     "windows_improved": better, "exits": m["exits"]})
    return {"original": {f: base_dev[f] for f in ("trades", "win_rate", "expectancy_r", "profit_factor", "max_dd_r",
                                                  "exits")}, "rows": rows}


def _exit_summary(exit_experiments: dict[str, Any]) -> list[dict[str, Any]]:
    summary = []
    for ek, (label, _fn) in EXIT_EXPERIMENTS.items():
        rows = [r for v in exit_experiments.values() for r in v["rows"] if r["experiment"] == ek]
        deltas = [r["delta_expectancy_r"] for r in rows if r["delta_expectancy_r"] is not None]
        wr = [(r["win_rate"], v["original"]["win_rate"]) for v in exit_experiments.values() for r in v["rows"]
              if r["experiment"] == ek and r["win_rate"] is not None and v["original"]["win_rate"] is not None]
        summary.append({"experiment": ek, "label": label, "strategies": len(deltas),
                        "improved": sum(1 for d in deltas if d > 0),
                        "median_delta_r": round(statistics.median(deltas), 4) if deltas else None,
                        "median_delta_win_rate": round(statistics.median([a - b for a, b in wr]), 4) if wr else None})
    return summary


def analyse_diagnostic(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    """Stage 1: why the originals lose and what each exit / stop-management change does - development months only."""
    dev = [w["key"] for w in params["windows"]]
    configs = params["configs"]
    keys = list(dict.fromkeys(c["strategy"] for c in configs))
    out: dict[str, Any] = {"definition": DEFINITION, "stage": "diagnostic", "development": dev, "hold_out": [],
                           "windows": params["windows"], "folds": [], "strategies": {}, "exit_experiments": {}}
    for key in keys:
        orig = f"{key}|original"
        out["strategies"][key] = {"family": FAMILY[key], "weakness": weakness(results, orig, dev),
                                  "development": {tf: pooled(results, orig, tf, dev) for tf in ("M15", "M5")}}
        out["exit_experiments"][key] = _exit_rows(results, configs, key, dev)
    out["exit_summary"] = _exit_summary(out["exit_experiments"])
    out["principles"] = ["Development months only: nothing from the hold-out months is used.",
                         "Expectancy is in R after costs (sizing-independent); win rate is shown but is not the goal."]
    return out


def overlay_effect(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    """Paired effect of break-even / trailing on every redesign variant (same entry and exit otherwise)."""
    dev = [w["key"] for w in params["windows"] if w["role"] == "development"]
    hold = [w["key"] for w in params["windows"] if w["role"] == "hold-out"]
    base = {(c["strategy"], c["entry"], c["exit"]): c["key"] for c in params["configs"]
            if c["role"] == "redesign" and c.get("overlay") == "none"}
    out = {}
    for ov in OVERLAYS:
        pairs = [(c["key"], base[(c["strategy"], c["entry"], c["exit"])]) for c in params["configs"]
                 if c["role"] == "redesign" and c.get("overlay") == ov and (c["strategy"], c["entry"], c["exit"]) in base]
        row = {}
        for label, wins in (("development", dev), ("hold_out", hold)):
            d, wr = [], []
            for k_on, k_off in pairs:
                on, off = pooled(results, k_on, SELECTION_TF, wins), pooled(results, k_off, SELECTION_TF, wins)
                if on["expectancy_r"] is not None and off["expectancy_r"] is not None and off["trades"] >= 10:
                    d.append(on["expectancy_r"] - off["expectancy_r"])
                    wr.append((on["win_rate"] or 0) - (off["win_rate"] or 0))
            row[label] = {"pairs": len(d), "improved": sum(1 for x in d if x > 0),
                          "median_delta_r": round(statistics.median(d), 4) if d else None,
                          "median_delta_win_rate": round(statistics.median(wr), 4) if wr else None}
        out[ov] = {"label": OVERLAYS[ov][0], **row}
    return out


def analyse_full(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    dev = [w["key"] for w in params["windows"] if w["role"] == "development"]
    hold = [w["key"] for w in params["windows"] if w["role"] == "hold-out"]
    configs = params["configs"]
    keys = list(dict.fromkeys(c["strategy"] for c in configs))
    min_dev = MIN_DEV_TRADES
    out: dict[str, Any] = {"definition": DEFINITION, "development": dev, "hold_out": hold, "windows": params["windows"],
                           "folds": params["folds"], "strategies": {}, "exit_experiments": {}, "overview": []}
    for key in keys:
        mine = [c for c in configs if c["strategy"] == key]
        orig = f"{key}|original"
        # 1) weakness study (development only)
        weak = weakness(results, orig, dev)
        # 2) exit experiments (development only), each against the original
        out["exit_experiments"][key] = _exit_rows(results, configs, key, dev)
        # 3) redesign: choose on development; walk-forward; hold-out
        variants = [c["key"] for c in mine if c["role"] == "redesign"]
        chosen = select(results, variants, dev, allowed=dev, min_trades=min_dev)
        wf, oos_trades, oos_wins = [], [], []
        base_oos_trades = []
        for fd in params["folds"]:
            s = select(results, variants, fd["select_on"], allowed=fd["select_on"],
                       min_trades=MIN_TRADES_PER_WINDOW * len(fd["select_on"]))
            test = results.get((s["chosen"], SELECTION_TF, fd["test"])) if s["chosen"] else None
            tm = summarize(test["trades"]) if test and test["status"] == "COMPLETED" else None
            if tm:
                oos_trades.extend(test["trades"])
                oos_wins.append(tm["sum_r"] > 0)
            bt = results.get((orig, SELECTION_TF, fd["test"]))
            if bt and bt["status"] == "COMPLETED":
                base_oos_trades.extend(bt["trades"])
            wf.append({"test": fd["test"], "selected_on": fd["select_on"], "chosen": s["chosen"], "flag": s["flag"],
                       "test_metrics": tm, "status": "COMPLETED" if tm else (test or {}).get("status", "no choice")})
        oos = summarize(oos_trades)
        oos["positive_windows"] = sum(oos_wins)
        base_oos = summarize(base_oos_trades)
        base_oos["positive_windows"] = sum(1 for fd in params["folds"]
                                           if (results.get((orig, SELECTION_TF, fd["test"])) or {}).get("status") == "COMPLETED"
                                           and summarize(results[(orig, SELECTION_TF, fd["test"])]["trades"])["sum_r"] > 0)
        holdout = {tf: pooled(results, chosen["chosen"], tf, hold) for tf in RUN_TFS} if chosen["chosen"] else {}
        base_hold = {tf: pooled(results, orig, tf, hold) for tf in RUN_TFS}
        all_var_oos = [summarize([t for w in hold for t in (results.get((k, SELECTION_TF, w)) or {}).get("trades", [])])
                       for k in variants]
        disp = [m["expectancy_r"] for m in all_var_oos if m["expectancy_r"] is not None and m["trades"] >= 10]
        n_f = len(params["folds"])
        v_new = verdict(oos, holdout.get(SELECTION_TF, {}), n_f) if chosen["chosen"] else \
            {"verdict": "no version", "why": chosen["flag"]}
        v_old = verdict(base_oos, base_hold[SELECTION_TF], n_f)
        out["strategies"][key] = {
            "family": FAMILY[key], "version_name": VERSION_NAMES.get(key, f"{key} v2"),
            "original": {"key": orig, "development": {tf: pooled(results, orig, tf, dev) for tf in ("M15", "M5")},
                         "walk_forward_oos": base_oos, "hold_out": base_hold, "verdict": v_old},
            "weakness": weak, "variants": len(variants), "selection": chosen,
            "chosen_label": next((c["label"] for c in mine if c["key"] == chosen["chosen"]), None),
            "development": {tf: pooled(results, chosen["chosen"], tf, dev) for tf in ("M15", "M5")} if chosen["chosen"] else {},
            "walk_forward": wf, "walk_forward_oos": oos, "hold_out": holdout,
            "hold_out_all_variants": {"variants_with_10_trades": len(disp),
                                      "median_expectancy_r": round(statistics.median(disp), 4) if disp else None,
                                      "positive": sum(1 for x in disp if x > 0)},
            "verdict": v_new}
        out["overview"].append({"strategy": key, "family": FAMILY[key],
                                "original_verdict": v_old["verdict"], "new_verdict": v_new["verdict"],
                                "original_oos_expectancy_r": base_oos["expectancy_r"], "original_oos_trades": base_oos["trades"],
                                "new_oos_expectancy_r": oos["expectancy_r"], "new_oos_trades": oos["trades"],
                                "new_hold_out_expectancy_r": (holdout.get(SELECTION_TF) or {}).get("expectancy_r"),
                                "new_hold_out_trades": (holdout.get(SELECTION_TF) or {}).get("trades"),
                                "chosen": chosen["chosen"], "flag": chosen["flag"]})
    out["exit_summary"] = _exit_summary(out["exit_experiments"])
    out["principles"] = ["Expectancy is in R after costs (sizing-independent); USD figures use the configured 0.02 lots.",
                         "Variants are chosen on development windows only; walk-forward folds choose on earlier windows "
                         "only; the hold-out is evaluated once with the development choice.",
                         "A better result in one month is not a better strategy: verdicts require positive "
                         "out-of-sample expectancy, at least 30 trades, consistency across months and a positive hold-out."]
    return out


def persist_versions(ms: Any, sid: str, analysis: dict[str, Any], params: dict[str, Any]) -> None:
    """Record each strategy's development-chosen redesign as a RESEARCH version (research store only)."""
    now = iso(ms.clock.now())
    with ms.store.transaction() as con:
        for key, s in analysis["strategies"].items():
            if not s["selection"]["chosen"]:
                continue
            cfg = json.loads(con.execute("SELECT config FROM robustness_configs WHERE study_id = ? AND key = ?",
                                         (sid, s["selection"]["chosen"])).fetchone()[0])
            cfg = {**cfg, "name": s["version_name"][:80]}
            cfg = builder.validate(cfg)
            con.execute("INSERT OR REPLACE INTO research_versions (id, study_id, strategy, name, config, fingerprint, "
                        "chosen_variant, verdict, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"{sid}_{key}", sid, key, s["version_name"], json.dumps(cfg, sort_keys=True),
                         builder.fingerprint(cfg), s["selection"]["chosen"], s["verdict"]["verdict"],
                         json.dumps({"verdict": s["verdict"], "development": s["development"].get(SELECTION_TF),
                                     "walk_forward_oos": s["walk_forward_oos"],
                                     "hold_out": s["hold_out"].get(SELECTION_TF)}, default=str), now))


def chosen_keys(results: dict, params: dict[str, Any]) -> set[str]:
    """Each strategy's development choice and walk-forward fold choices (all made on M5 results only)."""
    dev = [w["key"] for w in params["windows"] if w["role"] == "development"]
    out: set[str] = set()
    for key in dict.fromkeys(c["strategy"] for c in params["configs"]):
        variants = [c["key"] for c in params["configs"] if c["strategy"] == key and c["role"] == "redesign"]
        if not variants:
            continue
        c = select(results, variants, dev, allowed=dev, min_trades=MIN_DEV_TRADES)["chosen"]
        if c:
            out.add(c)
        for fd in params["folds"]:
            c = select(results, variants, fd["select_on"], allowed=fd["select_on"],
                       min_trades=MIN_TRADES_PER_WINDOW * len(fd["select_on"]))["chosen"]
            if c:
                out.add(c)
    return out


def results_hash(results: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(results):
        h.update(json.dumps([k, results[k]["status"], results[k]["metrics"]], sort_keys=True, default=str).encode())
    return h.hexdigest()


def default_range(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Jun 22 - Nov 14 2026 for the stored MT5 history (Jun 15 - Nov 13): development to 1 Oct."""
    return (datetime(2026, 6, 22, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
