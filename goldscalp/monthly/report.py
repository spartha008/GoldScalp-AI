"""Monthly reports (PHASE5_ARCHITECTURE.md sections 7-8): one per run, one per strategy (M15 vs M5 vs M1) and one
combined report, with PDF, CSV and JSON exports. Built from stored run results: opening a report never re-runs a
backtest. Every figure is a hypothetical simulation on historical candles."""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from typing import Any

from goldscalp.backtesting import metrics
from goldscalp.backtesting.engine import RISK_EQUITY
from goldscalp.reports.pdf import PDF, Layout

SMALL_SAMPLE = 30
RISK_BANNER = ("High-risk hypothetical simulation scenarios — not real-account settings or trading recommendations. "
               "With compounded sizing a losing streak can cut simulated equity substantially. Never apply these "
               "settings to MT5, a broker or the live paper monitor.")
DISCLAIMER = ("Hypothetical historical simulation on stored XAUUSD candles under the stated assumptions. Not trading "
              "results; no orders were placed. One month is not enough to establish reliability, and past behaviour "
              "does not predict future results.")
EXIT_LABELS = {"stop": "stop-loss", "breakeven": "break-even stop", "trailing_stop": "trailing stop",
               "target": "take-profit", "time": "time exit", "structure_flip": "structure flip",
               "window_end": "month end (forced close)", "end_of_data": "end of data"}
COLOURS = [(0.72, 0.56, 0.12), (0.2, 0.45, 0.8), (0.5, 0.35, 0.75), (0.15, 0.6, 0.45)]


def _r(x: Any, n: int = 2) -> Any:
    return None if x is None else round(float(x), n)


# ------------------------------------------------------------------ one run
def build_run(engine_result: dict[str, Any], *, starting_balance: float, leverage: int, contract: dict[str, Any]) -> dict[str, Any]:
    trades = engine_result["trades"]
    balance = starting_balance
    enriched = []
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        ounces = float(t["ounces"])
        risk_oz = abs(t["entry_price"] - t["stop"]) if t.get("stop") is not None else None
        risk_usd = risk_oz * ounces if risk_oz else None
        planned_rr = abs(t["target"] - t["entry_price"]) / risk_oz if t.get("target") is not None and risk_oz else None
        margin = ounces * t["entry_price"] / leverage
        enriched.append({**t, "risk_usd": _r(risk_usd), "risk_pct_of_balance": _r(risk_usd / balance * 100, 3)
                         if risk_usd and balance > 0 else None, "planned_rr": _r(planned_rr, 3), "margin": _r(margin),
                         "balance_before": _r(balance), "balance_after": _r(balance + t["net_pnl"])})
        balance += t["net_pnl"]
    m = metrics.compute(enriched, starting_balance=starting_balance, min_trades=1,
                        closed_equity=engine_result["closed_equity"], marked_equity=engine_result["marked_equity"])
    nets = [t["net_pnl"] for t in enriched]
    diag = engine_result["diagnostics"]
    rejections = diag.get("rejections") or []
    marked = engine_result["marked_equity"]
    used = [p.get("used_margin") or 0 for p in marked]
    exits = Counter(t["exit_reason"] for t in enriched)
    risks = [t["risk_usd"] for t in enriched if t["risk_usd"] is not None]
    rrs = [t["planned_rr"] for t in enriched if t["planned_rr"] is not None]
    gross = sum(t["gross_pnl"] for t in enriched)
    costs = m["total_commission"] + m["total_spread_cost"] + m["total_slippage_cost"]
    end_balance = round(starting_balance + sum(nets), 2)
    performance = {
        "trades": m["trades"], "wins": m["wins"], "losses": m["losses"], "breakeven": m["breakeven"],
        "win_rate": m["win_rate"], "win_rate_ci95": m["win_rate_ci95"], "gross_profit": m["gross_profit"],
        "gross_loss": m["gross_loss"], "net": m["net"], "return_pct": round(m["net"] / starting_balance * 100, 3),
        "profit_factor": m["profit_factor"], "average_win": m["average_win"], "average_loss": m["average_loss"],
        "expectancy_usd": m["average_outcome"], "average_r": m["average_r"],
        "max_drawdown": m["max_drawdown_marked"] or m["max_drawdown"],
        "max_drawdown_pct": m["max_drawdown_marked_pct"] or m["max_drawdown_pct"],
        "max_drawdown_closed": m["max_drawdown"], "largest_win": _r(max(nets)) if nets else None,
        "largest_loss": _r(min(nets)) if nets else None, "average_holding_minutes": m["average_holding_minutes"],
        "starting_balance": starting_balance, "ending_balance": end_balance,
        "small_sample": m["trades"] < SMALL_SAMPLE,
        "sample_note": (f"only {m['trades']} trades: ratios are shown but not statistically meaningful"
                        if m["trades"] < SMALL_SAMPLE else None)}
    risk = {"average_risk_usd": _r(sum(risks) / len(risks)) if risks else None, "total_risk_usd": _r(sum(risks)),
            "average_risk_pct_of_balance": _r(sum(t["risk_pct_of_balance"] or 0 for t in enriched) / len(enriched), 3)
            if enriched else None,
            "planned_rr_average": _r(sum(rrs) / len(rrs), 3) if rrs else None,
            "planned_rr_range": [min(rrs), max(rrs)] if rrs else None,
            "realized_r_average": m["average_r"],
            "exits": {EXIT_LABELS.get(k, k): v for k, v in exits.items()},
            "stop_loss_exits": exits.get("stop", 0), "take_profit_exits": exits.get("target", 0),
            "other_exits": sum(v for k, v in exits.items() if k not in ("stop", "target")),
            "rejected_entries": len(rejections), "rejections_by_reason": dict(Counter(r["reason"] for r in rejections)),
            "margin_rejections": sum(1 for r in rejections if r["reason"] == "insufficient_margin"),
            "max_used_margin": _r(max(used)) if used else 0.0,
            "average_margin_per_trade": _r(sum(t["margin"] for t in enriched) / len(enriched)) if enriched else None,
            "max_margin_pct_of_equity": _r(max(((p.get("used_margin") or 0) / p["equity"] * 100) for p in marked
                                               if p["equity"]), 3) if marked else 0.0,
            "risk_note": "The configured risk is the planned loss at the stop; gaps, spread and slippage can make the "
                         "actual loss larger. It is not a guaranteed maximum."}
    cost = {"spread": m["total_spread_cost"], "commission": m["total_commission"], "slippage": m["total_slippage_cost"],
            "total": round(costs, 2), "share_of_abs_gross_pct": round(costs / abs(gross) * 100, 1) if gross else None}
    by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "net": 0.0})
    for t in enriched:
        d = t["entry_time"][:10]
        by_day[d]["trades"] += 1
        by_day[d]["net"] = round(by_day[d]["net"] + t["net_pnl"], 2)
    buckets = Counter()
    for t in enriched:
        r = t.get("r_multiple")
        if r is None:
            continue
        b = max(-3, min(3, int(r // 0.5) * 0.5)) if r >= 0 else max(-3.0, -((-r // 0.5) + 1) * 0.5)
        buckets[f"{b:+.1f}R"] += 1
    streak = longest = 0
    for t in enriched:
        streak = streak + 1 if t["net_pnl"] < 0 else 0
        longest = max(longest, streak)
    performance["longest_losing_streak"] = longest
    eq = [t for t in enriched if t.get("sizing") == RISK_EQUITY]
    if eq:          # Phase 5.1 hypothetical percentage-risk scenario (fixed-lot results are unchanged)
        planned = [t["planned_risk_usd"] for t in eq]
        budgets = [t["risk_budget_usd"] for t in eq]
        lots = [float(t["lots"]) for t in eq]
        beyond = [t["loss_beyond_plan_usd"] for t in eq if (t["loss_beyond_plan_usd"] or 0) > 0.005]
        skipped = diag.get("skipped") or {}
        risk["percentage_risk"] = {
            "mode": RISK_EQUITY, "risk_percent": eq[0]["risk_percent"],
            "formula": "risk budget = current simulated equity x risk %; planned loss per lot = (|entry - stop| + "
                       "slippage) x contract size + round-trip commission; lots = budget / planned loss per lot, rounded "
                       "DOWN to the lot step; capped at the broker maximum and the strategy's exposure limit",
            "average_risk_budget_usd": _r(sum(budgets) / len(budgets)), "max_risk_budget_usd": _r(max(budgets)),
            "min_risk_budget_usd": _r(min(budgets)),
            "average_planned_risk_usd": _r(sum(planned) / len(planned)), "max_planned_risk_usd": _r(max(planned)),
            "average_lots": _r(sum(lots) / len(lots), 3), "max_lots": max(lots), "min_lots": min(lots),
            "stop_loss_losses_usd": _r(sum(t["net_pnl"] for t in eq if t["exit_reason"] == "stop")),
            "losses_beyond_plan": len(beyond), "losses_beyond_plan_usd": _r(sum(beyond)),
            "budget_violations": sum(1 for t in eq if not t["within_budget"]),
            "capped_entries": sum(1 for t in eq if t["size_capped_by"]),
            "insufficient_volume_rejections": int(skipped.get("risk_budget_below_min_volume", 0)),
            "margin_rejections": int(skipped.get("insufficient_margin", 0)),
            "no_stop_rejections": int(skipped.get("no_stop", 0)),
            "compliance": ("every entry's planned risk was within its budget" if not any(not t["within_budget"] for t in eq)
                           else "SOME ENTRIES EXCEEDED THEIR BUDGET"),
            "note": "Planned risk is the loss if the stop fills as planned. Gaps through the stop, slippage and time / "
                    "structure exits make the actual result differ; losses beyond plan are listed separately."}
    charts = {"equity": [{"time": p["time"], "value": p["equity"]} for p in marked],
              "trade_outcomes": [{"time": t["exit_time"], "value": t["net_pnl"]} for t in enriched],
              "position_size": [{"time": t["entry_time"], "value": float(t["lots"])} for t in enriched],
              **({"risk_budget": [{"time": t["entry_time"], "value": t["risk_budget_usd"]} for t in eq],
                  "planned_risk": [{"time": t["entry_time"], "value": t["planned_risk_usd"]} for t in eq]} if eq else {}),
              "balance": [{"time": p["time"], "value": p.get("balance", p["equity"])} for p in marked],
              "drawdown": [{"time": p["time"], "value": p["drawdown"]}
                           for p in metrics.drawdown_series(marked, starting_balance)],
              "daily": [{"day": d, **v} for d, v in sorted(by_day.items())],
              "r_distribution": [{"bucket": k, "trades": v} for k, v in sorted(buckets.items(), key=lambda kv: float(kv[0][:-1]))],
              "win_loss": {"wins": m["wins"], "losses": m["losses"], "breakeven": m["breakeven"]}}
    result = {"performance": performance, "risk": risk, "costs": cost, "charts": charts, "trades": enriched,
              "rejections": rejections[:500], "contract": contract, "result_hash": engine_result["result_hash"],
              "diagnostics": {k: diag.get(k) for k in ("steps", "setups", "entries", "ambiguous_direction_steps",
                                                       "gated_steps", "skipped", "ambiguous_intrabar", "gate_reasons")}}
    result["interpretation"] = interpret(result)
    result["summary"] = summary_row(result)
    return result


def summary_row(res: dict[str, Any]) -> dict[str, Any]:
    p, r, c = res["performance"], res["risk"], res["costs"]
    return {"trades": p["trades"], "net": p["net"], "return_pct": p["return_pct"], "win_rate": p["win_rate"],
            "profit_factor": p["profit_factor"], "expectancy_usd": p["expectancy_usd"], "average_r": p["average_r"],
            "max_drawdown": p["max_drawdown"], "max_drawdown_pct": p["max_drawdown_pct"], "ending_balance": p["ending_balance"],
            "costs": c["total"], "average_risk_usd": r["average_risk_usd"], "planned_rr": r["planned_rr_average"],
            "rejected": r["rejected_entries"], "small_sample": p["small_sample"],
            "longest_losing_streak": p.get("longest_losing_streak"),
            **({"risk_percent": r["percentage_risk"]["risk_percent"], "average_lots": r["percentage_risk"]["average_lots"],
                "max_lots": r["percentage_risk"]["max_lots"],
                "losses_beyond_plan": r["percentage_risk"]["losses_beyond_plan"],
                "margin_rejections": r["percentage_risk"]["margin_rejections"],
                "insufficient_volume_rejections": r["percentage_risk"]["insufficient_volume_rejections"]}
               if r.get("percentage_risk") else {})}


def interpret(res: dict[str, Any]) -> list[str]:
    """Deterministic, factual reading of one run (section 8)."""
    p, r, c, trades = res["performance"], res["risk"], res["costs"], res["trades"]
    out = []
    if not trades:
        diag = res["diagnostics"]
        out.append(f"No simulated trade: {diag.get('setups', 0)} setups over {diag.get('steps', 0)} decision steps"
                   + (f"; {r['rejected_entries']} entries were rejected ({r['rejections_by_reason']})" if r["rejected_entries"] else "")
                   + ". Nothing can be concluded about performance.")
        return out
    for key, label in (("session", "session"), ("volatility", "volatility regime")):
        groups: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            groups[str(t.get(key) or "unknown")].append(t["net_pnl"])
        good = [(g, sum(v), len(v)) for g, v in groups.items() if len(v) >= 3 and sum(v) > 0]
        bad = [(g, sum(v), len(v)) for g, v in groups.items() if len(v) >= 3 and sum(v) < 0]
        if good:
            g = max(good, key=lambda x: x[1])
            out.append(f"Worked: {label} '{g[0]}' produced {g[1]:+.2f} USD over {g[2]} trades.")
        if bad:
            g = min(bad, key=lambda x: x[1])
            out.append(f"Did not work: {label} '{g[0]}' lost {g[1]:+.2f} USD over {g[2]} trades.")
    nets = sorted((t["net_pnl"] for t in trades), reverse=True)
    if p["net"] > 0 and nets:
        top3 = sum(nets[:3])
        if len(nets) <= 3 or top3 >= p["net"]:
            out.append(f"Concentration: the best {min(3, len(nets))} trades ({top3:+.2f} USD) account for all of the net "
                       f"result; without them the month would not be positive.")
        elif p["net"] - nets[0] < 0:
            out.append(f"Concentration: removing the single best trade ({nets[0]:+.2f} USD) turns the result negative.")
    if c["share_of_abs_gross_pct"] is not None and c["share_of_abs_gross_pct"] >= 25:
        out.append(f"Costs were significant: {c['total']:.2f} USD (spread {c['spread']:.2f}, commission "
                   f"{c['commission']:.2f}, slippage {c['slippage']:.2f}) = {c['share_of_abs_gross_pct']:.0f}% of |gross|.")
    else:
        out.append(f"Costs: {c['total']:.2f} USD in total"
                   + (f" ({c['share_of_abs_gross_pct']:.0f}% of |gross|)." if c["share_of_abs_gross_pct"] is not None else "."))
    if r["stop_loss_exits"] and r["stop_loss_exits"] >= 0.6 * p["trades"]:
        out.append(f"Most trades ended at the stop ({r['stop_loss_exits']} of {p['trades']}).")
    if r["other_exits"]:
        out.append(f"{r['other_exits']} trade(s) closed by time, structure or the month end, so their R differs from the "
                   "planned risk-to-reward.")
    if p["small_sample"]:
        out.append(p["sample_note"] + ".")
    out.append("This is one historical month; it does not establish reliability or predict future results.")
    return out


# ------------------------------------------------------------------ comparisons
def strategy_comparison(key: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """M15 vs M5 vs M1 for one strategy. Never crowns the highest return alone."""
    rows, notes = [], []
    for run in sorted(runs, key=lambda r: ("M15", "M5", "M1").index(r["timeframe"])):
        s = (run.get("result") or {}).get("summary") if run["status"] == "COMPLETED" else None
        rows.append({"timeframe": run["timeframe"], "status": run["status"], "reason": run.get("reason"), **(s or {})})
    done = [r for r in rows if r["status"] == "COMPLETED"]
    if done:
        best_net = max(done, key=lambda r: r["net"])
        lowest_dd = min(done, key=lambda r: r["max_drawdown"] or 0)
        notes.append(f"Highest net: {best_net['timeframe']} ({best_net['net']:+.2f} USD, {best_net['trades']} trades, max "
                     f"drawdown {best_net['max_drawdown']:.2f}). Lowest drawdown: {lowest_dd['timeframe']} "
                     f"({lowest_dd['max_drawdown']:.2f}).")
        for r in done:
            if r["small_sample"]:
                notes.append(f"{r['timeframe']}: {r['trades']} trades is a small sample.")
            if r["net"] and r["costs"] and abs(r["costs"]) >= 0.5 * abs(r["net"]):
                notes.append(f"{r['timeframe']}: costs ({r['costs']:.2f}) are large relative to the net result.")
        signs = {r["timeframe"]: (r["net"] > 0) for r in done if r["trades"]}
        if len(set(signs.values())) > 1:
            notes.append("The sign of the result changes with the timeframe: the outcome depends on the decision cadence "
                         "and execution candles, not only on the rules.")
        notes.append("No timeframe is declared best on return alone: drawdown, costs, sample size and consistency are "
                     "shown side by side.")
    missing = [r for r in rows if r["status"] != "COMPLETED"]
    for r in missing:
        notes.append(f"{r['timeframe']}: {r['status']} - {r.get('reason') or 'not run'}.")
    return {"strategy": key, "rows": rows, "assessment": notes}


def combined(evaluation: dict[str, Any], runs: list[dict[str, Any]], cands: list[dict[str, Any]]) -> dict[str, Any]:
    existing = [r for r in runs if r["kind"] == "existing"]
    table = []
    for r in existing:
        s = (r.get("result") or {}).get("summary") or {}
        table.append({"strategy": r["strategy_key"], "timeframe": r["timeframe"], "status": r["status"],
                      "reason": r.get("reason"), **s})
    done = [t for t in table if t["status"] == "COMPLETED"]
    by_strategy = {k: strategy_comparison(k, [r for r in existing if r["strategy_key"] == k])
                   for k in sorted({r["strategy_key"] for r in existing})}
    tf_totals = {tf: {"runs": sum(1 for t in done if t["timeframe"] == tf),
                      "net_sum": round(sum(t["net"] for t in done if t["timeframe"] == tf), 2),
                      "trades": sum(t["trades"] for t in done if t["timeframe"] == tf)} for tf in ("M15", "M5", "M1")}
    st_totals = {k: {"runs": sum(1 for t in done if t["strategy"] == k),
                     "net_sum": round(sum(t["net"] for t in done if t["strategy"] == k), 2),
                     "trades": sum(t["trades"] for t in done if t["strategy"] == k)} for k in by_strategy}
    findings = []
    if done:
        best = max(done, key=lambda t: (t["net"], -(t["max_drawdown"] or 0)))
        worst = min(done, key=lambda t: t["net"])
        findings.append(f"Strongest historical run: {best['strategy']} {best['timeframe']} ({best['net']:+.2f} USD, "
                        f"{best['return_pct']:+.2f}%, {best['trades']} trades, max drawdown {best['max_drawdown']:.2f}"
                        f"{', small sample' if best['small_sample'] else ''}).")
        findings.append(f"Weakest: {worst['strategy']} {worst['timeframe']} ({worst['net']:+.2f} USD).")
        positive = sum(1 for t in done if t["net"] > 0)
        findings.append(f"{positive} of {len(done)} completed runs ended positive.")
        small = sum(1 for t in done if t["small_sample"])
        if small:
            findings.append(f"{small} of {len(done)} runs had fewer than {SMALL_SAMPLE} trades.")
    incomplete = [t for t in table if t["status"] != "COMPLETED"]
    cand_rows = []
    for c in cands:
        mine = [r for r in runs if r["strategy_key"] == c["key"]]
        cand_rows.append({"key": c["key"], "name": c["name"], "concept": c["concept"], "chosen_rr": c["research"]["chosen_rr"],
                          "research_check_net": c["research"]["check"]["net"],
                          "research_check_trades": c["research"]["check"]["trades"], "flag": c["research"]["flag"],
                          "month": {r["timeframe"]: (r.get("result") or {}).get("summary") for r in mine
                                    if r["status"] == "COMPLETED"}})
    return {"evaluation": {k: evaluation[k] for k in ("id", "month", "range_start", "range_end", "status", "app_version")},
            "table": table, "by_strategy": by_strategy, "by_timeframe": tf_totals, "by_strategy_totals": st_totals,
            "separate_accounts_note": (f"Each run is a SEPARATE hypothetical ${evaluation['params']['starting_balance']} "
                                       "account. Sums across runs are labelled 'sum of separate accounts' and are not one "
                                       "account's balance."),
            "sum_of_separate_accounts_net": round(sum(t["net"] for t in done), 2), "completed_runs": len(done),
            "incomplete": incomplete, "findings": findings, "candidates": cand_rows,
            "conclusion": (findings[0] if findings else "No run completed.") +
            " One month of results cannot establish that any of these strategies is reliable; more months and "
            "out-of-sample periods are needed.", "disclaimer": DISCLAIMER}


# ------------------------------------------------------------------ exports
def ledger_csv(run: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "side", "setup_cutoff", "entry_time", "entry_price", "lots", "ounces", "stop", "stop_source", "target",
            "planned_rr", "risk_usd", "risk_pct_of_balance", "margin", "exit_time", "exit_price", "exit_reason",
            "ambiguous_bar", "gross_pnl", "commission", "spread_cost", "slippage_cost", "net_pnl", "r_multiple", "mfe",
            "mae", "holding_minutes", "session", "volatility", "balance_before", "balance_after"]
    trades = (run.get("result") or {}).get("trades") or []
    if any(t.get("sizing") == RISK_EQUITY for t in trades):     # Phase 5.1 hypothetical percentage-risk sizing
        cols += ["risk_percent", "equity_at_entry", "risk_budget_usd", "planned_loss_per_lot", "planned_risk_usd",
                 "size_capped_by", "within_budget", "loss_beyond_plan_usd"]
        w.writerow(["# HYPOTHETICAL percentage-risk scenario - not a real-account setting or trading recommendation"])
    w.writerow(["# SIMULATED historical backtest - no orders", run["strategy_key"], run["timeframe"], run["id"]])
    w.writerow(cols)
    for t in trades:
        w.writerow([t.get(c) for c in cols])
    return buf.getvalue()


def run_json(run: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    return {"label": "SIMULATED historical backtest", "run_id": run["id"], "evaluation_id": evaluation["id"],
            "month": evaluation["month"], "strategy_key": run["strategy_key"], "kind": run["kind"],
            "timeframe": run["timeframe"], "status": run["status"], "reason": run.get("reason"),
            "strategy_config": run["config"], "fingerprint": run["fingerprint"], "assumptions": run["assumptions"],
            "dataset": run["dataset"], "app_version": evaluation["app_version"], "result_hash": run.get("result_hash"),
            "result": run.get("result"), "disclaimer": DISCLAIMER}


def summary_csv(comb: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# SIMULATED historical backtests - separate hypothetical accounts", comb["evaluation"]["month"],
                comb["evaluation"]["id"]])
    cols = ["strategy", "timeframe", "status", "trades", "net", "return_pct", "win_rate", "profit_factor",
            "expectancy_usd", "average_r", "max_drawdown", "max_drawdown_pct", "ending_balance", "costs",
            "average_risk_usd", "planned_rr", "rejected", "small_sample", "reason"]
    w.writerow(cols)
    for t in comb["table"]:
        w.writerow([t.get(c) for c in cols])
    return buf.getvalue()


def _fmt(v: Any, n: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{n}f}"
    return str(v)


def _header(lay: Layout, title: str, evaluation: dict[str, Any], generated: str) -> None:
    lay.heading(title, 15)
    lay.paragraph(DISCLAIMER)
    lay.kv([("evaluation", f"{evaluation['id']} ({evaluation['status']})"),
            ("month", f"{evaluation['month']}: {evaluation['range_start']} to {evaluation['range_end']} (UTC)"),
            ("generated", generated), ("application", evaluation["app_version"])])


def run_pdf(run: dict[str, Any], evaluation: dict[str, Any], generated: str) -> bytes:
    pdf = PDF(title=f"GoldScalp monthly backtest {run['strategy_key']} {run['timeframe']}")
    lay = Layout(pdf)
    a = run["assumptions"].get("engine", run["assumptions"])
    scenario = (a.get("sizing_override") or {}).get("sizing") == RISK_EQUITY
    title = f"Monthly backtest: {run['config']['name']} - {run['timeframe']}"
    if scenario:
        title += f" - HYPOTHETICAL {a['sizing_override']['risk_percent']:g}% RISK SCENARIO"
    _header(lay, title, evaluation, generated)
    if scenario:
        lay.heading("WARNING", 12)
        lay.paragraph(RISK_BANNER)
    res = run.get("result")
    ds = run.get("dataset") or {}
    lay.heading("Run information", 12)
    lay.kv([("strategy", f"{run['config']['name']} ({run['kind']}), fingerprint {run['fingerprint'][:16]}"),
            ("instrument / timeframe", f"XAUUSD / {run['timeframe']} (decisions and execution)"),
            ("data source", f"{ds.get('series')} ({ds.get('store')} store); analysis base {ds.get('analysis_base')}"),
            ("execution candles", f"{ds.get('execution_candles')} {run['timeframe']} candles {ds.get('execution_first')} "
                                  f"to {ds.get('execution_last')}"),
            ("starting / ending balance", f"{a['starting_balance']} / {_fmt((res or {}).get('performance', {}).get('ending_balance'))} USD"),
            ("hypothetical leverage", f"1:{a['leverage']} (margin calculation only)"),
            ("costs", f"spread: candle spread else {a['configured_spread']}; commission {a['commission_per_lot_per_side']} "
                      f"per lot per side; slippage {a['slippage']} per oz per fill"),
            ("status", f"{run['status']}" + (f" - {run['reason']}" if run.get("reason") else ""))])
    if not res:
        lay.paragraph("No results: the run did not complete.")
        return pdf.output()
    p, r, c = res["performance"], res["risk"], res["costs"]
    lay.heading("Performance", 12)
    lay.kv([("trades (W / L / BE)", f"{p['trades']} ({p['wins']} / {p['losses']} / {p['breakeven']})"),
            ("win rate", _fmt(p["win_rate"], 3)), ("gross profit / loss", f"{_fmt(p['gross_profit'])} / {_fmt(p['gross_loss'])}"),
            ("net after costs", f"{_fmt(p['net'])} USD ({_fmt(p['return_pct'], 2)} %)"),
            ("profit factor", _fmt(p["profit_factor"], 3)),
            ("average win / loss", f"{_fmt(p['average_win'])} / {_fmt(p['average_loss'])}"),
            ("expectancy", f"{_fmt(p['expectancy_usd'])} USD per trade; average {_fmt(p['average_r'], 3)} R"),
            ("max drawdown", f"{_fmt(p['max_drawdown'])} USD ({_fmt(p['max_drawdown_pct'], 2)} %)"),
            ("largest win / loss", f"{_fmt(p['largest_win'])} / {_fmt(p['largest_loss'])}"),
            ("average holding", f"{_fmt(p['average_holding_minutes'], 1)} minutes"),
            ("sample", p["sample_note"] or "30 or more trades")])
    lay.heading("Risk and execution", 12)
    lay.kv([("average / total risk", f"{_fmt(r['average_risk_usd'])} / {_fmt(r['total_risk_usd'])} USD "
                                      f"(average {_fmt(r['average_risk_pct_of_balance'], 2)} % of balance)"),
            ("planned risk-to-reward", f"{_fmt(r['planned_rr_average'], 2)} R average; realized {_fmt(r['realized_r_average'], 3)} R"),
            ("exits", ", ".join(f"{k} {v}" for k, v in r["exits"].items()) or "-"),
            ("rejected entries", f"{r['rejected_entries']} {r['rejections_by_reason'] or ''}"),
            ("margin", f"max used {_fmt(r['max_used_margin'])} USD ({_fmt(r['max_margin_pct_of_equity'], 2)} % of equity); "
                       f"{r['margin_rejections']} margin rejections"),
            ("costs", f"spread {_fmt(c['spread'])}, commission {_fmt(c['commission'])}, slippage {_fmt(c['slippage'])}, "
                      f"total {_fmt(c['total'])} USD")])
    lay.paragraph(r["risk_note"], size=8)
    pr = r.get("percentage_risk")
    if pr:
        lay.heading(f"Percentage risk sizing ({pr['risk_percent']:g}% of current equity, hypothetical)", 12)
        lay.kv([("formula", pr["formula"]),
                ("risk budget per entry", f"average {_fmt(pr['average_risk_budget_usd'])}, min {_fmt(pr['min_risk_budget_usd'])}, "
                                          f"max {_fmt(pr['max_risk_budget_usd'])} USD"),
                ("planned risk (cost-inclusive)", f"average {_fmt(pr['average_planned_risk_usd'])}, max "
                                                  f"{_fmt(pr['max_planned_risk_usd'])} USD; {pr['compliance']}"),
                ("position size", f"average {_fmt(pr['average_lots'], 3)}, min {pr['min_lots']}, max {pr['max_lots']} lots; "
                                  f"{pr['capped_entries']} capped"),
                ("stop-loss losses", f"{_fmt(pr['stop_loss_losses_usd'])} USD"),
                ("losses beyond planned risk", f"{pr['losses_beyond_plan']} trades, {_fmt(pr['losses_beyond_plan_usd'])} USD"),
                ("rejections", f"minimum volume above budget {pr['insufficient_volume_rejections']}, margin "
                               f"{pr['margin_rejections']}, no stop {pr['no_stop_rejections']}"),
                ("longest losing streak", str(p.get("longest_losing_streak")))])
        lay.paragraph(pr["note"], size=8)
    ch = res["charts"]
    lay.chart("Equity and balance (USD)", [("equity", [x["value"] for x in ch["equity"]], COLOURS[0]),
                                           ("balance", [x["value"] for x in ch["balance"]], COLOURS[1])])
    lay.chart("Drawdown (USD)", [("drawdown", [x["value"] for x in ch["drawdown"]], (0.8, 0.2, 0.2))], height=80)
    if ch.get("position_size"):
        lay.chart("Position size per trade (lots)", [("lots", [x["value"] for x in ch["position_size"]], COLOURS[2])],
                  height=80)
    if ch.get("risk_budget"):
        lay.chart("Risk budget and planned risk per entry (USD)",
                  [("budget", [x["value"] for x in ch["risk_budget"]], COLOURS[0]),
                   ("planned", [x["value"] for x in ch["planned_risk"]], COLOURS[1])], height=80)
    if ch.get("trade_outcomes"):
        lay.chart("Trade outcomes (net USD, in exit order)",
                  [("net", [x["value"] for x in ch["trade_outcomes"]], COLOURS[3])], height=80)
    lay.heading("Interpretation", 12)
    for line in res["interpretation"]:
        lay.paragraph(line)
    lay.heading("Trades", 12)
    lay.table(["#", "side", "entry", "lots", "price", "stop", "target", "exit", "reason", "net", "R"],
              [[t["id"], t["side"], t["entry_time"][5:16], t["lots"], _fmt(t["entry_price"]), _fmt(t["stop"]),
                _fmt(t["target"]), t["exit_time"][5:16], EXIT_LABELS.get(t["exit_reason"], t["exit_reason"]),
                _fmt(t["net_pnl"]), _fmt(t.get("r_multiple"), 2)] for t in res["trades"][:120]],
              [22, 28, 62, 30, 48, 48, 48, 62, 64, 42, 34])
    if len(res["trades"]) > 120:
        lay.paragraph(f"{len(res['trades']) - 120} more trades are in the CSV ledger.")
    return pdf.output()


def strategy_pdf(comp: dict[str, Any], name: str, evaluation: dict[str, Any], generated: str) -> bytes:
    pdf = PDF(title=f"GoldScalp monthly comparison {comp['strategy']}")
    lay = Layout(pdf)
    _header(lay, f"Timeframe comparison: {name}", evaluation, generated)
    lay.table(["TF", "status", "trades", "net", "return %", "win rate", "PF", "exp. USD", "max DD", "costs", "avg risk"],
              [[x["timeframe"], x["status"], x.get("trades"), _fmt(x.get("net")), _fmt(x.get("return_pct")),
                _fmt(x.get("win_rate"), 3), _fmt(x.get("profit_factor"), 2), _fmt(x.get("expectancy_usd")),
                _fmt(x.get("max_drawdown")), _fmt(x.get("costs")), _fmt(x.get("average_risk_usd"))] for x in comp["rows"]],
              [28, 66, 34, 50, 44, 44, 34, 50, 46, 44, 46])
    lay.heading("Assessment", 12)
    for line in comp["assessment"]:
        lay.paragraph(line)
    return pdf.output()


def combined_pdf(comb: dict[str, Any], names: dict[str, str], generated: str, evaluation: dict[str, Any]) -> bytes:
    pdf = PDF(title=f"GoldScalp monthly combined report {comb['evaluation']['month']}")
    lay = Layout(pdf)
    _header(lay, f"Combined monthly report: {comb['evaluation']['month']}", evaluation, generated)
    lay.paragraph(comb["separate_accounts_note"])
    lay.heading("Nine-run comparison", 12)
    lay.table(["strategy", "TF", "status", "trades", "net", "return %", "PF", "exp. USD", "max DD", "costs"],
              [[names.get(t["strategy"], t["strategy"])[:24], t["timeframe"], t["status"], t.get("trades"), _fmt(t.get("net")),
                _fmt(t.get("return_pct")), _fmt(t.get("profit_factor"), 2), _fmt(t.get("expectancy_usd")),
                _fmt(t.get("max_drawdown")), _fmt(t.get("costs"))] for t in comb["table"]],
              [120, 28, 66, 34, 50, 44, 34, 50, 46, 44])
    lay.kv([("sum of separate accounts (net)", f"{_fmt(comb['sum_of_separate_accounts_net'])} USD over "
                                               f"{comb['completed_runs']} completed runs (NOT one account)")])
    lay.heading("By strategy and by timeframe", 12)
    lay.table(["group", "runs", "trades", "net (sum of separate accounts)"],
              [[names.get(k, k)[:30], v["runs"], v["trades"], _fmt(v["net_sum"])] for k, v in comb["by_strategy_totals"].items()]
              + [[k, v["runs"], v["trades"], _fmt(v["net_sum"])] for k, v in comb["by_timeframe"].items()],
              [200, 50, 60, 160])
    lay.heading("Key findings", 12)
    for line in comb["findings"]:
        lay.paragraph(line)
    if comb["incomplete"]:
        lay.heading("Incomplete runs and data limitations", 12)
        for t in comb["incomplete"]:
            lay.paragraph(f"{t['strategy']} {t['timeframe']}: {t['status']} - {t.get('reason')}")
    lay.heading("Research candidates (separate from the nine runs)", 12)
    for c in comb["candidates"]:
        month = "; ".join(f"{tf}: {s['trades']} trades, {s['net']:+.2f}" for tf, s in c["month"].items() if s) or "no month result"
        lay.paragraph(f"{c['name']} ({c['concept']}, {c['chosen_rr']:g}R): research check {c['research_check_trades']} trades, "
                      f"{_fmt(c['research_check_net'])} USD{' [' + c['flag'] + ']' if c['flag'] else ''}. Evaluation month: {month}.")
    lay.heading("Conclusion", 12)
    lay.paragraph(comb["conclusion"])
    return pdf.output()


# ------------------------------------------------------------------ Phase 5.1 risk-scenario study exports
def _cell(f: dict[str, Any]) -> str:
    if f.get("status") != "COMPLETED":
        return str(f.get("status") or "-")
    return (f"${f['ending_balance']:,.0f} ({f['net']:+.0f}), DD {f['max_drawdown_pct'] or 0:.0f}%, "
            f"{f['trades']} tr")


def _study_header(lay: Layout, title: str, study: dict[str, Any], generated: str) -> None:
    lay.heading(title, 15)
    lay.heading("WARNING", 12)
    lay.paragraph(RISK_BANNER)
    lay.paragraph(DISCLAIMER)
    lay.kv([("risk study", f"{study['id']} ({study['status']}), month {study['month']}"),
            ("baseline (fixed 0.02 lots, unchanged)", study["baseline_evaluation_id"]),
            ("scenario evaluations", ", ".join(study["evaluation_ids"])), ("generated", generated),
            ("application", study["app_version"])])


def scenario_pdf(comp: dict[str, Any], study: dict[str, Any], name: str, generated: str) -> bytes:
    pdf = PDF(title=f"GoldScalp risk scenarios {comp['strategy']}")
    lay = Layout(pdf)
    _study_header(lay, f"Risk scenario comparison: {name}", study, generated)
    cols = comp["columns"]
    lay.table(["TF"] + [c["label"][:22] for c in cols],
              [[r["timeframe"]] + [_cell(r["cells"][c["key"]]) for c in cols] for r in comp["rows"]],
              [30] + [120] * len(cols))
    lay.heading("Risk detail", 12)
    lay.table(["TF", "sizing", "avg lots", "max lots", "losing streak", "beyond plan", "margin rej.", "min-vol rej."],
              [[r["timeframe"], c["label"][:22], _fmt(f.get("average_lots"), 3), _fmt(f.get("max_lots"), 2),
                f"{f.get('longest_losing_streak')} ({_fmt(f.get('losing_streak_loss'))})",
                f"{f.get('losses_beyond_plan')} ({_fmt(f.get('losses_beyond_plan_usd'))})", f.get("margin_rejections"),
                f.get("insufficient_volume_rejections")]
               for r in comp["rows"] for c in cols for f in [r["cells"][c["key"]]] if f.get("status") == "COMPLETED"],
              [30, 110, 50, 50, 80, 80, 50, 50])
    lay.heading("Findings", 12)
    for line in comp["findings"]:
        lay.paragraph(line)
    return pdf.output()


def study_strategy_pdf(comp: dict[str, Any], study: dict[str, Any], name: str, generated: str) -> bytes:
    pdf = PDF(title=f"GoldScalp timeframe comparison {comp['strategy']}")
    lay = Layout(pdf)
    _study_header(lay, f"Timeframe comparison across risk scenarios: {name}", study, generated)
    for c in comp["columns"]:
        lay.heading(c["label"], 12)
        lay.table(["TF", "status", "trades", "end balance", "net", "return %", "PF", "max DD %", "streak", "costs"],
                  [[r["timeframe"], f.get("status"), f.get("trades"), _fmt(f.get("ending_balance")), _fmt(f.get("net")),
                    _fmt(f.get("return_pct")), _fmt(f.get("profit_factor"), 2), _fmt(f.get("max_drawdown_pct")),
                    f.get("longest_losing_streak"), _fmt(f.get("costs"))]
                   for r in comp["rows"] for f in [r["cells"][c["key"]]]],
                  [30, 66, 36, 60, 50, 44, 34, 44, 36, 44])
    lay.heading("Findings", 12)
    for line in comp["findings"]:
        lay.paragraph(line)
    return pdf.output()


def master_pdf(m: dict[str, Any], generated: str) -> bytes:
    pdf = PDF(title=f"GoldScalp risk scenario master report {m['study']['month']}")
    lay = Layout(pdf)
    _study_header(lay, f"Master report: strategies x timeframes x risk scenarios ({m['study']['month']})", m["study"],
                  generated)
    lay.paragraph(m["separate_accounts_note"])
    cols = m["columns"]
    lay.table(["strategy", "TF"] + [c["label"][:22] for c in cols],
              [[r["strategy"], r["timeframe"]] + [_cell(r["cells"][c["key"]]) for c in cols] for r in m["rows"]],
              [44, 28] + [110] * len(cols))
    lay.heading("Findings", 12)
    for line in m["findings"]:
        lay.paragraph(line)
    if m["candidates"]:
        lay.heading("Research candidates (separate from A/B/C)", 12)
        lay.table(["candidate", "TF"] + [c["label"][:22] for c in cols],
                  [[r["strategy"], r["timeframe"]] + [_cell(r["cells"][c["key"]]) for c in cols] for r in m["candidates"]],
                  [44, 28] + [110] * len(cols))
    return pdf.output()


def master_csv(m: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# HYPOTHETICAL percentage-risk scenarios - separate $1,000 accounts; not trading recommendations",
                m["study"]["id"], m["study"]["month"]])
    cols = ["status", "trades", "wins", "losses", "win_rate", "net", "return_pct", "ending_balance", "profit_factor",
            "expectancy_usd", "average_r", "max_drawdown", "max_drawdown_pct", "longest_losing_streak",
            "losing_streak_loss", "average_lots", "max_lots", "average_risk_budget_usd", "average_planned_risk_usd",
            "max_planned_risk_usd", "losses_beyond_plan", "losses_beyond_plan_usd", "budget_violations",
            "margin_rejections", "insufficient_volume_rejections", "costs", "run_id", "reason"]
    w.writerow(["kind", "strategy", "timeframe", "sizing", *cols])
    for kind, rows in (("existing", m["rows"]), ("candidate", m["candidates"])):
        for r in rows:
            for c in m["columns"]:
                f = r["cells"][c["key"]]
                w.writerow([kind, r["strategy"], r["timeframe"], c["label"], *[f.get(k) for k in cols]])
    return buf.getvalue()


# ------------------------------------------------------------------ Phase 5.2 strategy redesign and robustness report
def _e(x: Any) -> str:
    return "-" if x is None else f"{x:+.3f}"


def _diagnostic_pdf(lay: Layout, pdf: PDF, a: dict[str, Any]) -> bytes:
    lay.heading("Exit and stop-management experiments across all originals (development, M5)", 12)
    lay.table(["experiment", "change", "strategies", "improved", "median change (R/trade)", "median win-rate change"],
              [[x["experiment"], x["label"][:44], x["strategies"], x["improved"], _e(x["median_delta_r"]),
                _e(x.get("median_delta_win_rate"))] for x in a["exit_summary"]], [44, 200, 50, 50, 80, 80])
    for key, s in a["strategies"].items():
        lay.heading(f"{key} ({s['family']})", 12)
        for line in s["weakness"]["findings"]:
            lay.paragraph(line, size=8)
        ex = a["exit_experiments"][key]
        lay.table(["exit (entry unchanged)", "trades", "win rate", "R/trade", "change", "PF", "max DD R"],
                  [["original", ex["original"]["trades"], _fmt(ex["original"].get("win_rate"), 3),
                    _e(ex["original"]["expectancy_r"]), "-", _fmt(ex["original"]["profit_factor"], 2),
                    _fmt(ex["original"]["max_dd_r"])]] +
                  [[f"{r['experiment']} {r['label'][:30]}", r["trades"], _fmt(r.get("win_rate"), 3), _e(r["expectancy_r"]),
                    _e(r["delta_expectancy_r"]), _fmt(r["profit_factor"], 2), _fmt(r["max_dd_r"])] for r in ex["rows"]],
                  [180, 40, 46, 50, 50, 40, 50])
    return pdf.output()


def robustness_pdf(study: dict[str, Any], generated: str) -> bytes:
    a = study["analysis"]
    pdf = PDF(title=f"GoldScalp strategy redesign and robustness research {study['id']}")
    lay = Layout(pdf)
    diagnostic = a.get("stage") == "diagnostic"
    lay.heading("Strategy weakness diagnostics (Phase 5.2, stage 1)" if diagnostic else
                "Strategy redesign and multi-month robustness research (Phase 5.2)", 15)
    lay.paragraph(DISCLAIMER)
    lay.paragraph("Research versions only: nothing here changes a saved strategy, a monitor or an account. Expectancy is "
                  "in R after costs per trade (sizing-independent); each month is a separate hypothetical $1,000 account.")
    lay.kv([("study", f"{study['id']} ({study['status']}), {study['app_version']}"), ("generated", generated),
            ("development (design, tuning, choice)", ", ".join(a["development"])),
            ("hold-out (evaluated once)", ", ".join(a["hold_out"])),
            ("windows", "; ".join(f"{w['key']} {w['start'][:10]} to {w['end'][:10]}{' (partial)' if w['partial'] else ''}"
                                  for w in a["windows"])),
            ("walk-forward", "; ".join(f"test {f['test']} chosen on {'+'.join(f['select_on'])}" for f in a["folds"]))])
    for line in a["principles"]:
        lay.paragraph(line, size=8)
    if diagnostic:
        return _diagnostic_pdf(lay, pdf, a)
    lay.heading("Overview: originals vs redesigned research versions", 12)
    lay.table(["strategy", "original verdict", "orig. OOS R/trade (n)", "new verdict", "new OOS R/trade (n)",
               "new hold-out R/trade (n)"],
              [[o["strategy"], o["original_verdict"], f"{_e(o['original_oos_expectancy_r'])} ({o['original_oos_trades']})",
                o["new_verdict"], f"{_e(o['new_oos_expectancy_r'])} ({o['new_oos_trades']})",
                f"{_e(o['new_hold_out_expectancy_r'])} ({o['new_hold_out_trades']})"] for o in a["overview"]],
              [44, 92, 90, 92, 90, 100])
    lay.heading("Exit experiments across all originals (development, M5)", 12)
    lay.table(["experiment", "change", "strategies", "improved", "median change (R/trade)"],
              [[x["experiment"], x["label"][:46], x["strategies"], x["improved"], _e(x["median_delta_r"])]
               for x in a["exit_summary"]], [50, 220, 60, 60, 110])
    for key, s in a["strategies"].items():
        lay.heading(f"{key} -> {s['version_name']} ({s['family']})", 12)
        lay.paragraph("Weakness study (development, original rules):", size=9)
        for line in s["weakness"]["findings"]:
            lay.paragraph(line, size=8)
        ex = a["exit_experiments"][key]
        lay.table(["exit", "trades", "R/trade", "change", "PF", "max DD R", "months better"],
                  [["original", ex["original"]["trades"], _e(ex["original"]["expectancy_r"]), "-",
                    _fmt(ex["original"]["profit_factor"], 2), _fmt(ex["original"]["max_dd_r"]), "-"]] +
                  [[f"{r['experiment']} {r['label'][:28]}", r["trades"], _e(r["expectancy_r"]), _e(r["delta_expectancy_r"]),
                    _fmt(r["profit_factor"], 2), _fmt(r["max_dd_r"]), r["windows_improved"]] for r in ex["rows"]],
                  [170, 40, 50, 50, 40, 50, 60])
        sel = s["selection"]
        dev = s["development"].get("M5") or {}
        lay.kv([("variants tried", str(s["variants"])),
                ("chosen on development", f"{sel['chosen']} - {s['chosen_label'] or '-'}" + (f" [{sel['flag']}]" if sel["flag"] else "")),
                ("rule", sel["rule"]),
                ("development (M5)", f"{dev.get('trades')} trades, {_e(dev.get('expectancy_r'))} R/trade, PF "
                                     f"{_fmt(dev.get('profit_factor'), 2)}, max DD {_fmt(dev.get('max_dd_r'))} R"),
                ("walk-forward OOS (M5)", f"{s['walk_forward_oos']['trades']} trades, {_e(s['walk_forward_oos']['expectancy_r'])} "
                                          f"R/trade, PF {_fmt(s['walk_forward_oos']['profit_factor'], 2)}, "
                                          f"{s['walk_forward_oos']['positive_windows']} positive months"),
                ("hold-out", "; ".join(f"{tf}: {m['trades']} trades, {_e(m['expectancy_r'])} R/trade"
                                       for tf, m in s["hold_out"].items() if m.get("trades") is not None) or "-"),
                ("all variants on the hold-out", f"{s['hold_out_all_variants']['positive']} of "
                                                 f"{s['hold_out_all_variants']['variants_with_10_trades']} positive; median "
                                                 f"{_e(s['hold_out_all_variants']['median_expectancy_r'])} R/trade"),
                ("verdict (new)", f"{s['verdict']['verdict']}: {s['verdict']['why']}"),
                ("verdict (original)", f"{s['original']['verdict']['verdict']}: {s['original']['verdict']['why']}")])
        lay.table(["fold", "chosen on", "chosen", "test trades", "test R/trade"],
                  [[f["test"], "+".join(f["selected_on"]), f["chosen"] or "-",
                    (f["test_metrics"] or {}).get("trades", f["status"]), _e((f["test_metrics"] or {}).get("expectancy_r"))]
                   for f in s["walk_forward"]], [40, 100, 170, 60, 70])
    return pdf.output()


def to_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, default=str, indent=1).encode()
