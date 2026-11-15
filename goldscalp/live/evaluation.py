"""Per-trade evaluation of a closed SIMULATED validation trade (definition eval@1).
PHASE4_ARCHITECTURE.md section 7.

Deterministic: every answer is computed from the stored signal, trade, paper position, monitor re-checks,
entry/exit market context and the validating Phase 3 run. Each of the seven answers has a verdict
(yes / no / partly / unknown), one sentence and the evidence (facts with values). The evaluation is inserted
once and never updated. Improvement hints are HYPOTHESES for the improvement engine: one trade is not evidence.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

VERSION = "eval@1"
PLANNED_EXITS = {"stop_loss", "take_profit", "time_exit", "strategy_exit"}
LATE_FILL_SECONDS = 30
HTF = ("H1", "M15", "M5")
HINT_CAVEAT = "A hypothesis for the improvement engine to test on historical data: one trade is not evidence."


def _d(x: Any) -> Decimal | None:
    return None if x is None or x == "" else Decimal(str(x))


def _f(x: Decimal | float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


def _rel(state: str | None, side: str) -> str:
    want, against = ("bullish", "bearish") if side == "long" else ("bearish", "bullish")
    return "with" if state == want else "against" if state == against else "ranging" if state == "ranging" else "unknown"


def _baseline(baseline: dict[str, Any]) -> dict[str, Any] | None:
    runs = baseline.get("runs") or []
    good = [r for r in runs if r.get("sufficient")] or runs
    return good[0] if good else None


def evaluate_trade(t: dict[str, Any], sig: dict[str, Any] | None, *, baseline: dict[str, Any],
                   feed: dict[str, Any]) -> dict[str, Any]:
    sig = sig or {}
    side = "long" if t["side"] == "buy" else "short"
    plan, entry, exit_ = t["plan"], t["entry"], t["exit"] or {}
    ctx_in, ctx_out = t["context_entry"] or {}, t["context_exit"] or {}
    step = sig.get("timeframe") or "M5"
    net, gross = _d(t["realized_net"]) or Decimal(0), _d(t["realized_gross"]) or Decimal(0)
    ounces = _d(t["ounces"]) or Decimal(0)
    open_price = _d(t["open_price"]) or Decimal(0)
    stop = _d(t["stop_loss"])
    risk_oz = abs(open_price - stop) if stop is not None else None
    r = (net / (risk_oz * ounces)) if risk_oz and ounces else None
    commission = (_d(t["open_commission"]) or 0) + (_d(t["close_commission"]) or 0)
    open_spread = _d(t["open_spread"]) or Decimal(0)
    close_spread = _d(exit_.get("spread_at_close"))
    spread_paid = open_spread if side == "long" else (close_spread if close_spread is not None else open_spread)
    spread_cost = spread_paid * ounces
    slip_cost = (_d(t["open_slippage"]) or 0) * ounces * 2
    costs = commission + spread_cost + slip_cost
    mfe, mae = _d(t["mfe"]) or Decimal(0), _d(t["mae"]) or Decimal(0)
    outcome = "win" if net > 0 else "loss" if net < 0 else "breakeven"
    reason = t["close_reason"]
    holding = exit_.get("holding_minutes")
    tags: list[str] = []
    well: list[str] = []
    wrong: list[str] = []
    answers: list[dict[str, Any]] = []

    def answer(n: int, q: str, verdict: str, text: str, evidence: list[str]) -> None:
        answers.append({"n": n, "question": q, "verdict": verdict, "answer": text, "evidence": evidence})

    # 1 ------------------------------------------------------------------ why
    supporting = sig.get("supporting") or []
    answer(1, "Why was the signal generated?", "explained",
           f"All {len(supporting)} enabled condition(s) of {t['strategy_name']} v{t['version']} held for a {side} at the "
           f"{step} close {sig.get('cutoff', '?')} (live@1 monitor, the backtest's as-of snapshot).",
           [c["evidence"] for c in supporting] + [f"rules: {line}" for line in (sig.get("description") or [])[:6]])

    # 2 ------------------------------------------------------------------ conditions satisfied
    checks = [c for c in t["checks"] if "conditions_still_pass" in c]
    failed = [c for c in checks if not c["conditions_still_pass"]]
    latency = entry.get("latency_from_cutoff_seconds")
    ev2 = [f"entry {latency} s after the cut-off, quote {entry.get('quote_age_seconds')} s old "
           f"({entry.get('price_state')})",
           f"fill {entry.get('fill_price')} vs planned estimate {entry.get('planned_entry_estimate')}"]
    if latency is not None and latency > LATE_FILL_SECONDS:
        tags.append("late_fill")
    if not checks:
        v2, text2 = "yes", "The conditions held at the cut-off; the trade closed before the next cut-off was evaluated."
    elif not failed:
        v2, text2 = "yes", f"The conditions held at the cut-off and at all {len(checks)} later cut-off(s) while open."
    else:
        first = failed[0]
        v2 = "partly"
        text2 = (f"The conditions held at the cut-off but stopped holding at {first['cutoff']} "
                 f"(failed: {', '.join(first['failed']) or 'n/a'}); {len(failed)} of {len(checks)} later checks failed.")
        tags.append("conditions_faded")
        ev2.append(f"re-check at {first['cutoff']}: setup now {first.get('setup_now')}")
    answer(2, "Were the original conditions satisfied?", v2, text2, ev2)

    # 3 ------------------------------------------------------------------ higher timeframes
    tfs = (ctx_in.get("timeframes") or {})
    rel = {tf: _rel((tfs.get(tf) or {}).get("state"), side) for tf in HTF if tf != step and tf in tfs}
    against = [tf for tf, x in rel.items() if x == "against"]
    agree = [tf for tf, x in rel.items() if x == "with"]
    sig2 = (ctx_in.get("signal") or {})
    ev3 = [f"{tf} structure {(tfs.get(tf) or {}).get('state')} ({x} the {side})" for tf, x in rel.items()]
    ev3.append(f"Phase 2 signal@1 {sig2.get('direction')} score {sig2.get('score')}")
    if not rel:
        v3, text3 = "unknown", "No higher-timeframe context was recorded."
    elif against:
        v3, text3 = "no", f"{' and '.join(against)} structure was against the {side} at entry."
        tags.append("htf_against")
        wrong.append(f"higher timeframe against the trade ({', '.join(against)})")
    elif len(agree) == len(rel):
        v3, text3 = "yes", f"{' and '.join(agree)} structure agreed with the {side}."
        well.append("higher timeframes agreed")
    else:
        v3, text3 = "partly", f"Agreeing: {', '.join(agree) or 'none'}; the others were ranging."
    answer(3, "Did higher-timeframe analysis agree?", v3, text3, ev3)

    # 4 ------------------------------------------------------------------ conditions changed
    ev4, changes = [], []
    if ctx_out.get("timeframes"):
        if ctx_in.get("sessions") != ctx_out.get("sessions"):
            changes.append("session")
            ev4.append(f"sessions {' / '.join(ctx_in.get('sessions') or []) or 'none'} -> "
                       f"{' / '.join(ctx_out.get('sessions') or []) or 'none'}")
        for tf in (step, "M15", "H1"):
            a, b = (tfs.get(tf) or {}), (ctx_out["timeframes"].get(tf) or {})
            if a.get("state") != b.get("state"):
                changes.append(f"{tf} structure")
                ev4.append(f"{tf} structure {a.get('state')} -> {b.get('state')}")
            if a.get("volatility") != b.get("volatility"):
                changes.append(f"{tf} volatility")
                ev4.append(f"{tf} volatility {a.get('volatility')} -> {b.get('volatility')}")
        atr_a, atr_b = (tfs.get(step) or {}).get("atr"), (ctx_out["timeframes"].get(step) or {}).get("atr")
        if atr_a and atr_b:
            pct = (atr_b - atr_a) / atr_a * 100
            ev4.append(f"{step} ATR {atr_a:.3f} -> {atr_b:.3f} ({pct:+.0f}%)")
            if abs(pct) >= 30:
                changes.append(f"{step} ATR {pct:+.0f}%")
        turned = [c for c in changes if c.endswith("structure") and
                  _rel((ctx_out["timeframes"].get(c.split()[0]) or {}).get("state"), side) == "against"]
        if turned:
            tags.append("structure_turned")
            wrong.append(f"structure turned against the trade ({', '.join(turned)})")
        if any("volatility" in c or "ATR" in c for c in changes):
            tags.append("regime_change")
        v4 = "yes" if changes else "no"
        text4 = ("Changed during the trade: " + ", ".join(changes) + ".") if changes else \
            "Session, structure and volatility were unchanged between entry and exit."
    else:
        v4, text4 = "unknown", "No exit context was recorded (no snapshot at the exit)."
    answer(4, "Did market conditions change?", v4, text4, ev4)

    # 5 ------------------------------------------------------------------ spread / volatility
    sc = ctx_in.get("spread_context") or {}
    atr = plan.get("atr")
    ev5 = [f"costs {_f(costs)} USD = commission {_f(commission)} + spread {_f(spread_cost)} + slippage {_f(slip_cost)}",
           f"gross {_f(gross)} USD, net {_f(net)} USD"]
    reasons5 = []
    if gross > 0 and net <= 0:
        tags.append("costs_flipped_sign")
        reasons5.append("costs turned a gross gain into a net loss")
        wrong.append("costs turned a gross gain into a net loss")
    if sc.get("spread") is not None and sc.get("median"):
        ev5.append(f"spread at signal {sc['spread']} vs last-hour median {sc['median']} / p95 {sc.get('p95')}")
        if sc.get("above_p95") or (sc.get("vs_median") or 0) >= 1.5:
            tags.append("spread_wide")
            reasons5.append("the spread was unusually wide")
            wrong.append(f"spread {sc['spread']} above normal (median {sc['median']})")
    if atr and risk_oz:
        ratio = float(risk_oz) / float(atr)
        ev5.append(f"stop distance {_f(risk_oz, 3)} = {ratio:.2f} x ATR of the stop timeframe ({atr:.3f})")
        if ratio < 1:
            tags.append("stop_inside_noise")
            if outcome == "loss":
                reasons5.append("the stop was inside one ATR of normal movement")
    vol = (tfs.get(step) or {}).get("volatility")
    ev5.append(f"{step} volatility regime at entry: {vol}")
    share = float(costs / abs(gross)) if gross else None
    if share is not None:
        ev5.append(f"costs = {share * 100:.0f}% of |gross|")
    if reasons5:
        v5, text5 = "yes", "Yes: " + "; ".join(reasons5) + "."
    elif share is not None and share >= 0.3:
        v5, text5 = "partly", f"Costs were {share * 100:.0f}% of the gross result."
    else:
        v5, text5 = "no", "Spread, costs and volatility were within normal bounds for this trade."
        if share is not None and share < 0.3:
            well.append("costs small relative to the move")
    answer(5, "Did spread or volatility affect the outcome?", v5, text5, ev5)

    # 6 ------------------------------------------------------------------ behaved as expected
    ev6 = [f"exit: {reason} after {holding} minutes (planned max {plan.get('max_holding_minutes')})",
           f"result {_f(r, 3)} R" if r is not None else "R unavailable (no stop)"]
    base = _baseline(baseline)
    if base:
        ev6.append(f"validating {base['kind']} {base['id']}: {base.get('trades')} trades, expectancy "
                   f"{base.get('expectancy_r')} R, win rate {base.get('win_rate')} ({base.get('scope')})")
    replica = ((sig.get("outcome") or {}).get("replica") or {}).get("trade")
    rep_note = None
    if replica:
        ev6.append(f"backtest replica: {replica['exit_reason']} at {replica['exit_price']}, {replica['r_multiple']} R")
        same = {"stop": "stop_loss", "target": "take_profit", "time": "time_exit"}.get(replica["exit_reason"]) == reason
        rep_note = "matched" if same else "differed"
    v6 = "yes"
    problems6 = []
    if reason not in PLANNED_EXITS:
        v6 = "no"
        problems6.append(f"closed by '{reason}', not by a planned exit")
    if any("exit_delayed" in c for c in t["checks"]):
        v6 = "partly" if v6 == "yes" else v6
        problems6.append("a planned exit was delayed (no LIVE quote)")
    if rep_note == "differed":
        v6 = "partly" if v6 == "yes" else v6
        problems6.append("the backtest replica of the same decision exited differently")
    text6 = ("The trade followed its plan: entry at the cut-off and a planned exit (" + reason + ")"
             + (", like the backtest replica" if rep_note == "matched" else "") + ".") if v6 == "yes" else \
        "; ".join(problems6).capitalize() + "."
    answer(6, "Did the strategy behave as expected?", v6, text6, ev6)

    # 7 ------------------------------------------------------------------ improvements (hypotheses)
    hints = {"htf_against": "require higher-timeframe agreement (structure_state H1 not_against, or htf_alignment)",
             "spread_wide": "add or tighten a spread filter (max_spread)",
             "stop_inside_noise": "widen the stop (higher ATR multiple or structure buffer)",
             "costs_flipped_sign": "the move was small relative to costs: a larger target or fewer low-range entries",
             "regime_change": "test a volatility_regime condition for the regime this trade entered in",
             "structure_turned": "test an earlier exit when structure turns (exit_on_structure_flip)",
             "conditions_faded": "the setup faded after entry: test a shorter maximum holding time",
             "late_fill": "the entry was processed late: check data latency (fills here follow the cut-off)"}
    ideas = [hints[tag] for tag in tags if tag in hints]
    text7 = ("; ".join(ideas).capitalize() + ". " + HINT_CAVEAT) if ideas else \
        "Nothing specific stands out for this trade. " + HINT_CAVEAT
    answer(7, "What could have been improved?", "hypothesis" if ideas else "none", text7, [f"tags: {', '.join(tags) or 'none'}"])

    if reason == "take_profit":
        well.append("target reached")
    if reason == "stop_loss":
        wrong.append(f"stopped out after {holding} minutes")
    if risk_oz and mfe >= risk_oz and outcome != "win":
        wrong.append(f"gave back a favourable excursion of {_f(mfe / risk_oz)} R")
    if risk_oz and outcome == "win" and mae < risk_oz / 2:
        well.append("small adverse excursion (< 0.5 R)")
    if v2 == "yes":
        well.append("conditions held while open")
    summary = (f"{outcome.upper()} {_f(net):+.2f} USD" + (f" ({float(r):+.2f} R)" if r is not None else "")
               + f" via {reason} after {holding} min - SIMULATED")
    return {"definition": VERSION, "summary": summary, "outcome": outcome, "answers": answers,
            "tags": sorted(set(tags)), "went_well": well, "went_wrong": wrong,
            "metrics": {"net": _f(net), "gross": _f(gross), "r": _f(r, 3), "costs": _f(costs),
                        "mfe": _f(mfe, 3), "mae": _f(mae, 3), "holding_minutes": holding}}
