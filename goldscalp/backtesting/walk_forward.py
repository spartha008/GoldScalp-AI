"""Walk-forward validation (definition wf@1). PHASE3_ARCHITECTURE.md section 8.

Folds:  development [s, s+D)  ->  embargo [s+D, s+D+E)  ->  evaluation [s+D+E, s+D+E+T)
        rolling: s += step;  anchored: development always starts at the first start (expanding).
Every window is backtested on its own and FORCE-CLOSES open positions at its end, so no trade spans two
windows; the embargo adds separation. In "select" mode the candidate is chosen using ONLY its development
window's trades, then evaluated on the following unseen window. Every fold is reported; aggregated
out-of-sample results concatenate the evaluation trades of all folds.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from goldscalp.backtesting import engine, metrics
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.instrument import InstrumentSpec

VERSION = "wf@1"
OBJECTIVES = ("expectancy_r", "net", "profit_factor")


class WalkForwardError(ValueError):
    pass


def make_folds(start: datetime, end: datetime, *, dev: timedelta, evaluation: timedelta, step: timedelta,
               embargo: timedelta, anchored: bool = False) -> list[dict[str, datetime]]:
    if dev <= timedelta(0) or evaluation <= timedelta(0) or step <= timedelta(0) or embargo < timedelta(0):
        raise WalkForwardError("development, evaluation and step must be positive; the embargo must not be negative")
    folds = []
    s = start
    while True:
        dev_start = start if anchored else s
        dev_end = s + dev
        eval_start = dev_end + embargo
        eval_end = eval_start + evaluation
        if eval_end > end:
            break
        folds.append({"dev_start": dev_start, "dev_end": dev_end, "eval_start": eval_start, "eval_end": eval_end})
        s += step
    if not folds:
        raise WalkForwardError("the range is too short for one development + embargo + evaluation window")
    return folds


def _window(snapshots: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    return [s for s in snapshots if lo <= pd.Timestamp(s["cutoff"]) <= hi]


def _objective(m: dict[str, Any], objective: str, trades: list[dict[str, Any]]) -> float | None:
    if objective == "net":
        return m["net"]
    if objective == "expectancy_r":
        rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
        return sum(rs) / len(rs) if rs else None
    return m["gross_profit"] / m["gross_loss"] if m["gross_loss"] > 0 else None


def run(candidates: list[dict[str, Any]], timelines: dict[str, dict[str, Any]], candles: pd.DataFrame,
        base_tf: Timeframe, folds: list[dict[str, datetime]], *, mode: str, objective: str, min_dev_trades: int,
        assumptions: engine.Assumptions, spec: InstrumentSpec, names: list[str] | None = None) -> dict[str, Any]:
    if mode not in ("fixed", "select"):
        raise WalkForwardError("mode must be fixed or select")
    if objective not in OBJECTIVES:
        raise WalkForwardError(f"objective must be one of {list(OBJECTIVES)}")
    if mode == "fixed" and len(candidates) != 1:
        raise WalkForwardError("fixed mode evaluates exactly one strategy")
    if not candidates:
        raise WalkForwardError("no candidate strategies")
    names = names or [c["name"] for c in candidates]
    start_bal = float(assumptions.starting_balance)
    min_trades = assumptions.min_trades_for_ratios
    out_folds = []
    oos_trades: list[dict[str, Any]] = []
    for n, f in enumerate(folds, 1):
        fold: dict[str, Any] = {"fold": n, **{k: iso(v) for k, v in f.items()}, "candidates": []}
        chosen = 0
        if mode == "select":
            scores = []
            for i, c in enumerate(candidates):
                snaps = _window(timelines[c["evaluation"]["step_timeframe"]]["snapshots"], f["dev_start"], f["dev_end"])
                r = engine.run(c, snaps, candles, base_tf, start=f["dev_start"], end=f["dev_end"],
                               assumptions=assumptions, spec=spec, force_close_at_end=True)
                m = metrics.compute(r["trades"], starting_balance=start_bal, min_trades=min_trades,
                                    closed_equity=r["closed_equity"])
                score = _objective(m, objective, r["trades"]) if m["trades"] >= min_dev_trades else None
                scores.append(score)
                fold["candidates"].append({"name": names[i], "dev_trades": m["trades"], "dev_net": m["net"],
                                           "objective": None if score is None else round(score, 4),
                                           "qualified": score is not None})
            qualified = [(s, i) for i, s in enumerate(scores) if s is not None]
            if not qualified:
                fold.update(selected=None, note=f"no candidate had at least {min_dev_trades} development trades; "
                                                "the evaluation window was not traded")
                out_folds.append(fold)
                continue
            chosen = max(qualified, key=lambda x: (x[0], -x[1]))[1]       # ties: the earlier candidate
        c = candidates[chosen]
        snaps = _window(timelines[c["evaluation"]["step_timeframe"]]["snapshots"], f["eval_start"], f["eval_end"])
        r = engine.run(c, snaps, candles, base_tf, start=f["eval_start"], end=f["eval_end"], assumptions=assumptions,
                       spec=spec, force_close_at_end=True)
        m = metrics.compute(r["trades"], starting_balance=start_bal, min_trades=min_trades,
                            closed_equity=r["closed_equity"], marked_equity=r["marked_equity"])
        for t in r["trades"]:
            oos_trades.append({**t, "fold": n, "strategy": names[chosen]})
        fold.update(selected=names[chosen], evaluation={k: m[k] for k in (
            "trades", "wins", "losses", "net", "win_rate", "profit_factor", "expectancy_r", "max_drawdown",
            "sufficient", "reason")}, steps=r["diagnostics"]["steps"])
        if mode == "fixed":
            dev_snaps = _window(timelines[c["evaluation"]["step_timeframe"]]["snapshots"], f["dev_start"], f["dev_end"])
            dr = engine.run(c, dev_snaps, candles, base_tf, start=f["dev_start"], end=f["dev_end"],
                            assumptions=assumptions, spec=spec, force_close_at_end=True)
            dm = metrics.compute(dr["trades"], starting_balance=start_bal, min_trades=min_trades)
            fold["candidates"].append({"name": names[0], "dev_trades": dm["trades"], "dev_net": dm["net"]})
        out_folds.append(fold)

    oos_trades.sort(key=lambda t: (t["exit_time"], t["fold"], t["id"]))
    equity, curve = Decimal(str(start_bal)), []
    for t in oos_trades:
        equity += Decimal(str(t["net_pnl"]))
        curve.append({"time": t["exit_time"], "equity": round(float(equity), 2)})
    agg = metrics.compute(oos_trades, starting_balance=start_bal, min_trades=min_trades, closed_equity=curve)
    traded = [f for f in out_folds if f.get("evaluation")]
    nets = [f["evaluation"]["net"] for f in traded]
    variation = {"folds": len(out_folds), "traded_folds": len(traded),
                 "positive_folds": sum(1 for x in nets if x > 0),
                 "min_net": min(nets) if nets else None, "median_net": round(statistics.median(nets), 2) if nets else None,
                 "max_net": max(nets) if nets else None,
                 "stdev_net": round(statistics.stdev(nets), 2) if len(nets) > 1 else None}
    dev_nets = [c.get("dev_net") for f in out_folds for c in f["candidates"]
                if (mode == "fixed" or c["name"] == f.get("selected"))]
    return {"definition": VERSION, "mode": mode, "objective": objective, "min_dev_trades": min_dev_trades,
            "candidates": names, "folds": out_folds, "out_of_sample": agg, "out_of_sample_equity": curve,
            "out_of_sample_drawdown": metrics.drawdown_series(curve, start_bal),
            "out_of_sample_trades": [{k: v for k, v in t.items() if not k.startswith("_")} for t in oos_trades],
            "variation": variation,
            "in_sample_vs_out_of_sample": {"in_sample_net_total": round(sum(x for x in dev_nets if x is not None), 2),
                                           "out_of_sample_net_total": agg["net"]},
            "note": "Every fold is shown. Selection used development windows only; evaluation windows were unseen "
                    "by the selection and force-closed at their end."}
