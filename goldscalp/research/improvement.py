"""Strategy improvement engine (definition improve@1, validation validate@1). PHASE4_ARCHITECTURE.md section 8.

A STUDY of one strategy version, started by the user:

  windows   [discovery | embargo | holdout] over stored history, fixed at the start and stored. Live-paper
            evidence is used only if it lies entirely after the holdout (the holdout ends before the first live
            item), so the holdout is unseen by every piece of evidence behind a proposal.
  findings  from the discovery backtest (trades annotated with session, volatility, higher-timeframe relation,
            entry spread), a spread stress test (x1.5, x2) and live-paper trades / signal outcomes.
  proposals each finding maps to ONE change from a fixed catalogue of existing strategy@1 conditions.
  validation base and candidate are backtested on the HOLDOUT on one shared as-of timeline with identical
            assumptions, plus three holdout sub-windows. Verdict: improved / not_improved / insufficient_sample.

Nothing here changes a strategy: adoption is the user's approve action (a new version), never automatic.
"""
from __future__ import annotations

import copy
import statistics
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.replay import ReplayDataset
from goldscalp.backtesting import engine, features, metrics
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

VERSION = "improve@1"
VALIDATE_VERSION = "validate@1"
MIN_GROUP = 8
DD_TOLERANCE = 1.25
SUB_WINDOWS = 3
DISCLAIMER = ("Hypothetical research on stored history and SIMULATED live-paper records. A proposal is a hypothesis "
              "tested once on unseen data; it is never adopted without your approval, and past behaviour does not "
              "predict future performance.")


class StudyError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ windows
def plan_windows(coverage_start: datetime, coverage_end: datetime, *, discovery_pct: float, embargo_pct: float,
                 live_first: datetime | None, use_live: bool) -> dict[str, Any]:
    """[discovery | embargo | holdout] over [coverage_start, end). With live evidence, end = the first live item."""
    end = coverage_end
    live_used, live_note = False, None
    if use_live and live_first is not None:
        candidate_end = min(coverage_end, live_first)
        if (candidate_end - coverage_start) >= (coverage_end - coverage_start) * 0.5:
            end, live_used = candidate_end, True
        else:
            live_note = ("live-paper evidence was excluded: ending the history before the first live item would leave "
                         "less than half of the stored range")
    elif use_live:
        live_note = "no live-paper evidence is stored for this version"
    span = end - coverage_start
    if span <= timedelta(hours=6):
        raise StudyError("the stored history is too short for a discovery and a holdout window")
    d_end = coverage_start + span * discovery_pct
    h_start = d_end + span * embargo_pct
    out = {"discovery": {"start": coverage_start, "end": d_end}, "embargo": {"start": d_end, "end": h_start},
           "holdout": {"start": h_start, "end": end}, "live_evidence_used": live_used, "live_note": live_note,
           "live_after": live_first if live_used else None}
    check_disjoint(out)
    return out


def check_disjoint(w: dict[str, Any]) -> None:
    d, h = w["discovery"], w["holdout"]
    if not (d["start"] < d["end"] <= h["start"] < h["end"]):
        raise StudyError("discovery and holdout windows overlap")
    if w.get("live_evidence_used") and w.get("live_after") is not None and w["live_after"] < h["end"]:
        raise StudyError("live-paper evidence overlaps the holdout")


def _iso_windows(w: dict[str, Any]) -> dict[str, Any]:
    return {k: ({kk: iso(vv) for kk, vv in v.items()} if isinstance(v, dict) else
                iso(v) if isinstance(v, datetime) else v) for k, v in w.items()}


# ------------------------------------------------------------------------------------------------ findings
def _rel(state: str | None, side: str) -> str:
    want, against = ("bullish", "bearish") if side == "buy" else ("bearish", "bullish")
    return "with" if state == want else "against" if state == against else "ranging" if state == "ranging" else "unknown"


def annotate(trades: list[dict[str, Any]], snaps: dict[str, dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    stop_tf = config["exit"]["stop"]["timeframe"]
    out = []
    for t in trades:
        snap = snaps.get(t["setup_cutoff"]) or {}
        tfs = snap.get("tf") or {}
        atr = ((tfs.get(stop_tf) or {}).get("vol") or {}).get("atr")
        risk = abs(t["entry_price"] - t["stop"]) if t.get("stop") is not None else None
        out.append({**t, "h1_rel": _rel((tfs.get("H1") or {}).get("state"), t["side"]),
                    "m15_rel": _rel((tfs.get("M15") or {}).get("state"), t["side"]),
                    "entry_spread": snap.get("spread"), "stop_atr": (risk / atr) if risk and atr else None})
    return out


def _mean_r(trades: list[dict[str, Any]]) -> float | None:
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return round(sum(rs) / len(rs), 4) if rs else None


def _group_finding(trades: list[dict[str, Any]], key: str, kind: str, label: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or "unknown"), []).append(t)
    out = []
    for name, members in groups.items():
        rest = [t for t in trades if str(t.get(key) or "unknown") != name]
        if len(members) < MIN_GROUP or name == "unknown" or not rest:
            continue
        g, r = _mean_r(members), _mean_r(rest)
        if g is None or r is None or g >= 0 or g >= r:
            continue
        net = round(sum(t["net_pnl"] for t in members), 2)
        out.append({"kind": kind, "group": name, "n": len(members), "expectancy_r": g, "rest_expectancy_r": r,
                    "net": net, "title": f"Weak {label}: {name}",
                    "detail": f"{len(members)} discovery trades in {label} '{name}' averaged {g:+.3f} R (net {net:+.2f} USD) "
                              f"vs {r:+.3f} R for the other {len(rest)} trades",
                    "evidence": [f"discovery trade #{t['id']} ({t['entry_time']}, {t['r_multiple']} R)" for t in members[:12]]})
    return out


def findings(trades: list[dict[str, Any]], config: dict[str, Any], *, stress: dict[str, float],
             live: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = len(trades)
    stops = [t for t in trades if t["exit_reason"] == "stop"]
    false = [t for t in stops if (t.get("mfe_r") or 0) < 0.5]
    if n:
        out.append({"kind": "false_signals", "n": n, "count": len(false), "rate": round(len(false) / n, 3),
                    "title": "Repeated false signals" if len(false) >= MIN_GROUP else "False-signal rate",
                    "detail": f"{len(false)} of {n} discovery trades were stopped out before reaching +0.5 R",
                    "by_session": _count(false, "session"), "actionable": False,
                    "evidence": [f"discovery trade #{t['id']} ({t['entry_time']})" for t in false[:12]]})
    out += _group_finding(trades, "session", "weak_session", "session")
    out += _group_finding(trades, "volatility", "poor_volatility", "volatility regime")
    out += _group_finding(trades, "h1_rel", "htf_combination", "H1 structure relation")
    out += _group_finding(trades, "m15_rel", "htf_combination_m15", "M15 structure relation")
    base = stress.get("1")
    if base is not None and n:
        s15, s2 = stress.get("1.5"), stress.get("2")
        sensitive = s15 is not None and ((base > 0 and s15 <= 0) or (base > 0 and s15 < base * 0.5))
        spreads = [t["entry_spread"] for t in trades if t.get("entry_spread")]
        out.append({"kind": "spread_sensitivity", "n": n, "net_x1": base, "net_x1_5": s15, "net_x2": s2,
                    "median_entry_spread": round(statistics.median(spreads), 4) if spreads else None,
                    "flagged": sensitive, "title": "Excessive spread sensitivity" if sensitive else "Spread sensitivity",
                    "detail": f"discovery net {base:+.2f} USD at the recorded spreads, {s15:+.2f} at x1.5, "
                              f"{s2:+.2f} at x2" if s15 is not None and s2 is not None else "stress test incomplete",
                    "actionable": sensitive, "evidence": ["discovery backtest repeated with spread_multiplier 1.5 and 2"]})
    hold = config["exit"]["max_holding_minutes"] or 0
    quick = [t for t in stops if hold and t["holding_minutes"] <= hold / 4]
    tight = [t for t in stops if t.get("stop_atr") is not None and t["stop_atr"] < 1]
    if stops:
        out.append({"kind": "invalidation", "n": n, "stops": len(stops), "quick_stops": len(quick),
                    "tight_stops": len(tight), "exit_reasons": _count(trades, "exit_reason"),
                    "flagged": len(stops) >= MIN_GROUP and max(len(quick), len(tight)) >= 0.4 * len(stops),
                    "title": "Recurring invalidation pattern",
                    "detail": f"{len(stops)} stop-outs: {len(quick)} within the first quarter of the holding time, "
                              f"{len(tight)} with the stop closer than 1 ATR",
                    "evidence": [f"discovery trade #{t['id']} stop {t['stop']} ({t['holding_minutes']} min)"
                                 for t in (quick or tight)[:12]]})
    if live:
        out.append({"kind": "backtest_vs_live", **live, "title": "Backtest vs live-paper",
                    "actionable": False})
    return out


def _count(trades: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        k = str(t.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ------------------------------------------------------------------------------------------------ proposals
def _set_condition(config: dict[str, Any], ctype: str, match: Callable[[dict[str, Any]], bool],
                   new: dict[str, Any]) -> tuple[dict[str, Any], str]:
    cand = copy.deepcopy(config)
    for c in cand["conditions"]:
        if c["type"] == ctype and c["enabled"] and match(c):
            before = {k: c[k] for k in new}
            c.update(new)
            return cand, f"condition {c['id']} ({ctype}): {before} -> {new}"
    cid = f"p{len(cand['conditions']) + 1}"
    ids = {c["id"] for c in cand["conditions"]}
    while cid in ids:
        cid += "x"
    cand["conditions"].append({"id": cid, "type": ctype, "enabled": True, **new})
    return cand, f"new condition {cid} ({ctype}): {new}"


def propose(config: dict[str, Any], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per actionable finding, from a fixed catalogue of existing strategy@1 conditions."""
    out = []
    step = config["evaluation"]["step_timeframe"]
    for f in found:
        cand, diff, explanation = None, None, None
        if f["kind"] == "weak_session" and " / " not in f["group"] and f["group"] in builder.SESSION_NAMES:
            current = next((c for c in config["conditions"] if c["type"] == "session" and c["enabled"]), None)
            keep = [s for s in (current["sessions"] if current else builder.SESSION_NAMES) if s != f["group"]]
            if keep:
                cand, diff = _set_condition(config, "session", lambda c: True, {"sessions": keep})
                explanation = (f"Trades during the {f['group']} session averaged {f['expectancy_r']:+.3f} R in discovery "
                               f"(n={f['n']}). Only trade when {', '.join(keep)} is active (times outside every session "
                               "are excluded too).")
        elif f["kind"] == "poor_volatility" and f["group"] in builder.VOL_REGIMES:
            current = next((c for c in config["conditions"] if c["type"] == "volatility_regime" and c["enabled"]
                            and c["timeframe"] == step), None)
            allowed = [v for v in (current["allowed"] if current else builder.VOL_REGIMES) if v != f["group"]]
            if allowed:
                cand, diff = _set_condition(config, "volatility_regime", lambda c: c["timeframe"] == step,
                                            {"timeframe": step, "allowed": allowed})
                explanation = (f"Trades entered in a '{f['group']}' {step} volatility regime averaged "
                               f"{f['expectancy_r']:+.3f} R (n={f['n']}). Exclude that regime.")
        elif f["kind"] in ("htf_combination", "htf_combination_m15") and f["group"] == "against":
            tf = "H1" if f["kind"] == "htf_combination" else "M15"
            if not any(c["type"] == "structure_state" and c["enabled"] and c["timeframe"] == tf for c in config["conditions"]):
                cand, diff = _set_condition(config, "structure_state", lambda c, tf=tf: c["timeframe"] == tf,
                                            {"timeframe": tf, "state": "not_against"})
                explanation = (f"Trades taken while {tf} structure was against them averaged {f['expectancy_r']:+.3f} R "
                               f"(n={f['n']}). Require {tf} structure not against the trade.")
        elif f["kind"] == "spread_sensitivity" and f.get("flagged") and f.get("median_entry_spread"):
            limit = round(float(f["median_entry_spread"]), 3)
            current = next((c for c in config["conditions"] if c["type"] == "spread" and c["enabled"]), None)
            if current is None or current["max_spread"] is None or limit < current["max_spread"]:
                cand, diff = _set_condition(config, "spread", lambda c: True,
                                            {"max_spread": limit, "if_unknown": "fail"})
                explanation = (f"Discovery net fell from {f['net_x1']:+.2f} to {f['net_x1_5']:+.2f} USD with spreads x1.5. "
                               f"Skip entries when the spread exceeds the median entry spread {limit}.")
        elif f["kind"] == "invalidation" and f.get("flagged"):
            cand = copy.deepcopy(config)
            stop = cand["exit"]["stop"]
            before = dict(stop)
            if stop["type"] == "atr":
                stop["atr_multiple"] = round(stop["atr_multiple"] + 0.5, 3)
            elif stop["type"] == "structure":
                stop["buffer_atr"] = round(min(stop["buffer_atr"] + 0.1, 10), 3)
            else:
                stop["distance"] = round(stop["distance"] * 1.25, 3)
            diff = f"exit.stop: {before} -> {stop}"
            explanation = (f"{f['quick_stops']} quick and {f['tight_stops']} tight stop-outs among {f['stops']}. "
                           "Give the stop more room.")
        if cand is None:
            continue
        try:
            cand = builder.validate(cand)
        except builder.StrategyError:
            continue
        if builder.fingerprint(cand) == builder.fingerprint(config):
            continue
        cand["name"] = config["name"]
        out.append({"kind": f["kind"], "title": f["title"], "candidate": cand, "diff": diff, "explanation": explanation,
                    "evidence": {k: f.get(k) for k in ("detail", "n", "group", "expectancy_r", "rest_expectancy_r",
                                                       "net", "evidence") if f.get(k) is not None}})
    return out


# ------------------------------------------------------------------------------------------------ validation
def _window_snaps(snaps: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    return [s for s in snaps if lo <= pd.Timestamp(s["cutoff"]) <= hi]


def _run(config: dict[str, Any], snaps: list[dict[str, Any]], candles: pd.DataFrame, base_tf: Timeframe,
         start: datetime, end: datetime, assumptions: engine.Assumptions, spec: InstrumentSpec) -> dict[str, Any]:
    r = engine.run(config, _window_snaps(snaps, start, end), candles, base_tf, start=start, end=end,
                   assumptions=assumptions, spec=spec, force_close_at_end=True)
    m = metrics.compute(r["trades"], starting_balance=float(assumptions.starting_balance),
                        min_trades=assumptions.min_trades_for_ratios, closed_equity=r["closed_equity"],
                        marked_equity=r["marked_equity"])
    return {"result": r, "metrics": m}


def decide_verdict(bm: dict[str, Any], cm: dict[str, Any], better_subs: int, min_trades: int) -> tuple[str, str]:
    """validate@1: insufficient_sample below min_trades for either version; improved only if net AND expectancy R are
    higher, the candidate wins >= 2 of the holdout sub-windows and its drawdown is at most DD_TOLERANCE x the base."""
    if bm["trades"] < min_trades or cm["trades"] < min_trades:
        return "insufficient_sample", (f"holdout trades: base {bm['trades']}, candidate {cm['trades']}; at least "
                                       f"{min_trades} each are needed before ratios can support a decision")
    dd_ok = cm["max_drawdown"] <= bm["max_drawdown"] * DD_TOLERANCE + 1e-9
    exp_ok = (cm["expectancy_r"] if cm["expectancy_r"] is not None else float("-inf")) > \
             (bm["expectancy_r"] if bm["expectancy_r"] is not None else float("-inf"))
    ok = cm["net"] > bm["net"] and exp_ok and better_subs >= 2 and dd_ok
    why = (f"net {bm['net']:+.2f} -> {cm['net']:+.2f} USD, expectancy {bm['expectancy_r']} -> {cm['expectancy_r']} R, "
           f"better in {better_subs}/{SUB_WINDOWS} sub-windows, max drawdown {bm['max_drawdown']} -> {cm['max_drawdown']}")
    return ("improved" if ok else "not_improved"), why


def validate(base: dict[str, Any], cand: dict[str, Any], snaps: list[dict[str, Any]], candles: pd.DataFrame,
             base_tf: Timeframe, holdout: dict[str, datetime], assumptions: engine.Assumptions,
             spec: InstrumentSpec) -> dict[str, Any]:
    keys = ("trades", "wins", "losses", "net", "win_rate", "win_rate_ci95", "profit_factor", "expectancy_r",
            "max_drawdown", "max_drawdown_pct", "sufficient", "reason")
    b = _run(base, snaps, candles, base_tf, holdout["start"], holdout["end"], assumptions, spec)
    c = _run(cand, snaps, candles, base_tf, holdout["start"], holdout["end"], assumptions, spec)
    span = (holdout["end"] - holdout["start"]) / SUB_WINDOWS
    subs = []
    for i in range(SUB_WINDOWS):
        s, e = holdout["start"] + span * i, holdout["start"] + span * (i + 1)
        bs = _run(base, snaps, candles, base_tf, s, e, assumptions, spec)["metrics"]
        cs = _run(cand, snaps, candles, base_tf, s, e, assumptions, spec)["metrics"]
        subs.append({"start": iso(s), "end": iso(e), "base_trades": bs["trades"], "base_net": bs["net"],
                     "candidate_trades": cs["trades"], "candidate_net": cs["net"],
                     "candidate_better": cs["net"] > bs["net"]})
    bm, cm = b["metrics"], c["metrics"]
    min_trades = assumptions.min_trades_for_ratios
    better_subs = sum(1 for s in subs if s["candidate_better"])
    verdict, why = decide_verdict(bm, cm, better_subs, min_trades)
    return {"definition": VALIDATE_VERSION, "verdict": verdict, "why": why,
            "holdout": {"start": iso(holdout["start"]), "end": iso(holdout["end"])},
            "base": {k: bm.get(k) for k in keys}, "candidate": {k: cm.get(k) for k in keys},
            "sub_windows": subs, "better_sub_windows": better_subs,
            "base_result_hash": b["result"]["result_hash"], "candidate_result_hash": c["result"]["result_hash"],
            "rule": (f"improved = enough trades (>= {min_trades} each) AND higher net AND higher expectancy R AND better "
                     f"in >= 2 of {SUB_WINDOWS} holdout sub-windows AND max drawdown at most {DD_TOLERANCE}x the base")}


# ------------------------------------------------------------------------------------------------ the study
def run_study(*, record: dict[str, Any], ds: ReplayDataset, candles: pd.DataFrame, windows: dict[str, Any],
              cfg: AnalysisConfig, assumptions: engine.Assumptions, spec: InstrumentSpec,
              live: dict[str, Any] | None, progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    config = record["config"]
    step = Timeframe.parse(config["evaluation"]["step_timeframe"])
    d, h = windows["discovery"], windows["holdout"]
    cutoffs = features.step_cutoffs(ds, step, d["start"], h["end"])
    if not cutoffs:
        raise StudyError("no step candles in the study range")
    total = len(cutoffs)

    def tick(n: int) -> None:
        if progress is not None and (n % 10 == 0 or n == total):
            progress(n, total + 5)
    snaps = features.compute_timeline(ds, step, cutoffs, cfg, detail=builder.detail_timeframes(config), progress=tick,
                                      cancelled=cancelled)
    by_cut = {s["cutoff"]: s for s in snaps}
    disc = _run(config, snaps, candles, ds.base_tf, d["start"], d["end"], assumptions, spec)
    trades = annotate(disc["result"]["trades"], by_cut, config)
    stress = {"1": disc["metrics"]["net"]}
    for mult in ("1.5", "2"):
        a = engine.Assumptions.from_mapping({**assumptions.to_mapping(), "spread_multiplier": mult})
        stress[mult] = _run(config, snaps, candles, ds.base_tf, d["start"], d["end"], a, spec)["metrics"]["net"]
    if progress is not None:
        progress(total + 2, total + 5)
    found = findings(trades, config, stress=stress, live=live)
    proposals = propose(config, found)
    for p in proposals:
        p["validation"] = validate(config, p["candidate"], snaps, candles, ds.base_tf, h, assumptions, spec)
    if progress is not None:
        progress(total + 5, total + 5)
    keys = ("trades", "wins", "losses", "net", "win_rate", "profit_factor", "expectancy_r", "max_drawdown", "sufficient",
            "reason")
    return {"definition": VERSION, "windows": _iso_windows(windows), "series": ds.series_key,
            "base_tf": ds.base_tf.value, "dataset_hash": features.dataset_hash(ds), "steps": total,
            "assumptions": assumptions.to_mapping(),
            "discovery": {k: disc["metrics"].get(k) for k in keys}, "spread_stress": stress, "findings": found,
            "proposals": proposals, "disclaimer": DISCLAIMER}


def limitations(study: dict[str, Any], proposal: dict[str, Any], *, reused_holdout: int) -> list[str]:
    v = proposal["validation"]
    out = [f"Holdout sample: base {v['base']['trades']} trades, candidate {v['candidate']['trades']} trades "
           f"({study['windows']['holdout']['start']} to {study['windows']['holdout']['end']}).",
           f"Discovery evidence: {proposal['evidence'].get('n', 'n/a')} trades in one window of one series "
           f"({study['series']}); a different market regime can reverse the pattern.",
           "The change was chosen from discovery evidence and tested once on the holdout. Studying the same strategy "
           "again on the same history erodes the holdout's independence.",
           "Fills, costs and intrabar ordering follow the Phase 3 bt@1 assumptions; live fills differ (see the "
           "backtest-vs-live finding)."]
    if reused_holdout:
        out.append(f"{reused_holdout} earlier study/studies of this strategy used an overlapping holdout.")
    if v["verdict"] == "insufficient_sample":
        out.append("Too few holdout trades: the comparison cannot support adoption; approving it requires an explicit "
                   "acknowledgement.")
    return out
