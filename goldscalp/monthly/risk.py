"""Phase 5.1: hypothetical percentage-risk scenario studies (5 %, 7.5 %, 10 % of current simulated equity).

A study is created from a completed FIXED-LOT baseline evaluation (never modified). It creates one new evaluation per
scenario, each with its own ID, frozen fingerprints (re-checked against the application database), the baseline's
research candidates (copied, so the chosen risk-to-reward variants cannot change), its own assumptions and its own
runs = separate hypothetical $1,000 accounts. One job computes each timeframe's decision timeline ONCE and runs every
scenario x strategy on it (identical inputs; only the sizing differs). Simulation only: nothing reaches MT5, a
broker, the paper simulator or the application database."""
from __future__ import annotations

import secrets
import threading
from collections import Counter
from typing import Any

from goldscalp.backtesting import engine, features
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import report, runner
from goldscalp.monthly import store as st
from goldscalp.monthly.data import RUN_TIMEFRAMES, availability, month_range
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

DEFINITION = "monthly-risk@1"


class StudyRefused(ValueError):
    """The study cannot be created (the reason is shown to the user)."""


def scenario_label(s: dict[str, Any]) -> str:
    return f"{s['key']} {s['risk_percent']:g}%"


# ------------------------------------------------------------------ creation
def create(ms: Any, baseline_id: str, *, scenarios: list[float], candidate_keys: list[str]) -> dict[str, Any]:
    with ms.store.read() as con:
        base = st.evaluation(con, baseline_id)
        base_cands = {c["key"]: c for c in st.candidates(con, baseline_id)} if base else {}
    if base is None:
        raise StudyRefused(f"unknown baseline evaluation {baseline_id}")
    if base["status"] not in ("COMPLETED", "INCOMPLETE") or not base["frozen"]:
        raise StudyRefused(f"the baseline must be a finished, frozen evaluation (status {base['status']})")
    bp = base["params"]
    if bp.get("risk_scenario") or bp.get("risk_percent_override"):
        raise StudyRefused("the baseline must use each strategy's configured fixed-lot sizing, not a risk override")
    unknown = [k for k in candidate_keys if k not in base_cands]
    if unknown:
        raise StudyRefused(f"candidates not in the baseline: {', '.join(unknown)}")
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(not 0 < s <= 10 for s in scenarios):
        raise StudyRefused("scenarios must be distinct risk percentages above 0 and at most 10")
    spec, contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    if not contract.get("verified"):
        raise StudyRefused("percentage risk sizing needs the verified XAUUSD contract (an actual MT5 terminal "
                           "verification record); nothing was created")
    frozen_strategies, problems = [], []
    for f in base["frozen"]["strategies"]:
        rec = ms.sources.strategy(f["strategy_id"], f["version"])
        if rec is None:
            problems.append(f"{f['name']} v{f['version']} no longer exists")
            continue
        fp = builder.fingerprint(rec["config"])
        if fp != rec["fingerprint"] or fp != f["fingerprint"]:
            problems.append(f"{f['name']} v{f['version']}: fingerprint differs from the baseline's frozen version")
            continue
        frozen_strategies.append({**{k: f[k] for k in ("key", "strategy_id", "version", "name", "fingerprint",
                                                         "version_created_at")}, "config": rec["config"]})
    if problems:
        raise StudyRefused("; ".join(problems))
    month = base["month"]
    start, end = month_range(month)
    now = ms.clock.now()
    sid = "rs_" + secrets.token_hex(6)
    ordered = sorted(scenarios)
    labelled = [{"key": f"S{i + 1}", "risk_percent": float(p)} for i, p in enumerate(ordered)]
    av = availability(ms.sources, bp["store"], bp["series"], month, ms.app_spec.schedule,
                      research_days=bp["research_days"], lookback_days=bp["lookback_days"])
    version = runner.app_version()
    eids = []
    with ms.store.transaction() as con:
        for sc in labelled:
            eid = "me_" + secrets.token_hex(6)
            eids.append(eid)
            params = {**{k: v for k, v in bp.items() if k != "risk_percent_override"}, "risk_scenario": sc,
                      "baseline_evaluation": baseline_id, "risk_study": sid, "candidates_selected": list(candidate_keys),
                      "label": f"HYPOTHETICAL percentage-risk scenario {scenario_label(sc)}"}
            st.create_evaluation(con, eid, month=month, start=start, end=end, params=params, availability=av,
                                 app_version=version, now=now)
            assumptions = runner.make_assumptions(params)
            stored = {"engine": assumptions.to_mapping(), "spec": spec.to_mapping(), "contract": contract,
                      "risk_scenario": sc, "banner": runner.RISK_BANNER}
            strategies = [{**f, "readiness": runner.readiness(f["config"], params, contract)} for f in frozen_strategies]
            for key in candidate_keys:
                c = base_cands[key]
                st.insert_candidate(con, f"{eid}_{key}", eid, c, now)
            items = [(f["key"], "existing", f["config"], f["fingerprint"]) for f in strategies] + \
                    [(k, "candidate", base_cands[k]["config"], base_cands[k]["fingerprint"]) for k in candidate_keys]
            for key, kind, config, fp in items:
                for tf in RUN_TIMEFRAMES:
                    st.upsert_run(con, {"id": f"{eid}_{key}_{tf}", "evaluation_id": eid, "strategy_key": key,
                                        "kind": kind, "timeframe": tf, "status": "NOT STARTED", "config": config,
                                        "fingerprint": fp, "assumptions": stored})
            st.update_evaluation(con, eid, now, status="READY", research_done=1,
                                 research={**(base["research"] or {}), "copied_from": baseline_id,
                                           "note": "Research candidates and their chosen risk-to-reward variants are "
                                                   "copied from the baseline evaluation unchanged."},
                                 frozen={"at": iso(now), "strategies": strategies, "candidates": list(candidate_keys),
                                         "assumptions": stored, "risk_scenario": sc, "baseline_evaluation": baseline_id,
                                         "risk_study": sid})
        st.create_study(con, sid, baseline=baseline_id, month=month, evaluation_ids=eids,
                        params={"scenarios": labelled, "candidates": list(candidate_keys), "definition": DEFINITION,
                                "store": bp["store"], "series": bp["series"], "banner": runner.RISK_BANNER},
                        app_version=version, now=now)
    return {"id": sid, "evaluation_ids": eids}


# ------------------------------------------------------------------ the job
def run_study(ms: Any, sid: str, cancel: threading.Event, timeframes: tuple[str, ...] | None = None) -> None:
    """Run every scenario x strategy x timeframe, or only ``timeframes`` (a recorded re-run of a finished study;
    the other timeframes' stored runs are left exactly as they are)."""
    with ms.store.read() as con:
        s = st.study(con, sid)
        evs = [st.evaluation(con, e) for e in s["evaluation_ids"]]
        planned = {e["id"]: [r for r in st.runs(con, e["id"], with_result=False)
                             if timeframes is None or r["timeframe"] in timeframes] for e in evs}
    p = evs[0]["params"]
    start, end = month_range(s["month"])
    total = sum(len(v) for v in planned.values())
    now = ms.clock.now()
    with ms.store.transaction() as con:
        st.update_study(con, sid, now, status="RUNNING", progress=0, total=total, error=None)
        for e in evs:
            st.update_evaluation(con, e["id"], now, status="RUNNING", error=None, phase=f"risk study {sid}")
    try:
        av = availability(ms.sources, p["store"], p["series"], s["month"], ms.app_spec.schedule,
                          research_days=p["research_days"], lookback_days=p["lookback_days"])
        row = ms.sources.series_row(p["store"], p["series"])
        done = 0
        for tf in RUN_TIMEFRAMES:
            mine = [r for e in evs for r in planned[e["id"]] if r["timeframe"] == tf]
            if not mine:
                continue
            tf_av = av["timeframes"][tf]
            if not tf_av["sufficient"]:
                with ms.store.transaction() as con:
                    for r in mine:
                        st.upsert_run(con, {**r, "status": "INCOMPLETE", "reason": "; ".join(tf_av["problems"]),
                                            "dataset": None, "result": None})
                done += len(mine)
                continue
            run_tf = Timeframe.parse(tf)
            detail = set().union(*(builder.detail_timeframes(r["config"]) for r in mine))

            def tick(n: int, tf: str = tf) -> None:
                with ms.store.transaction() as con:
                    st.update_study(con, sid, ms.clock.now(), phase=f"{tf} timeline: {n} decision steps")
            prepared = runner.prepare(ms.sources, p["store"], row, run_tf, start, end, lookback_days=p["lookback_days"],
                                      detail=detail, cfg=ms.analysis_cfg, progress=tick, cancelled=cancel.is_set)
            for r in mine:
                if cancel.is_set():
                    raise features.Cancelled()
                started = iso(ms.clock.now())
                a = r["assumptions"]
                res = runner.run_one(r["config"], prepared, run_tf, start, end,
                                     engine.Assumptions.from_mapping(a["engine"]), InstrumentSpec.from_mapping(a["spec"]))
                built = report.build_run(res, starting_balance=float(a["engine"]["starting_balance"]),
                                         leverage=int(a["engine"]["leverage"]), contract=a["contract"])
                done += 1
                with ms.store.transaction() as con:
                    st.upsert_run(con, {**r, "status": "COMPLETED", "dataset": prepared["dataset"], "result": built,
                                        "result_hash": res["result_hash"], "reason": None, "started_at": started,
                                        "finished_at": iso(ms.clock.now())})
                    st.update_study(con, sid, ms.clock.now(), progress=done, phase=f"{tf} runs")
        statuses = []
        with ms.store.transaction() as con:
            for e in evs:
                final = st.runs(con, e["id"])
                status = "COMPLETED" if all(r["status"] == "COMPLETED" for r in final) else "INCOMPLETE"
                statuses.append(status)
                st.update_evaluation(con, e["id"], ms.clock.now(), status=status, phase=None, availability=av,
                                     finished_at=iso(ms.clock.now()))
            st.update_study(con, sid, ms.clock.now(), status="COMPLETED" if set(statuses) == {"COMPLETED"} else "INCOMPLETE",
                            phase=None, finished_at=iso(ms.clock.now()))
    except Exception as exc:  # noqa: BLE001 - recorded; the service keeps running
        message = "cancelled" if isinstance(exc, features.Cancelled) else f"{type(exc).__name__}: {exc}"
        with ms.store.transaction() as con:
            for e in evs:
                st.update_evaluation(con, e["id"], ms.clock.now(), status="FAILED", error=message, phase=None,
                                     finished_at=iso(ms.clock.now()))
            st.update_study(con, sid, ms.clock.now(), status="FAILED", error=message, phase=None,
                            finished_at=iso(ms.clock.now()))


# ------------------------------------------------------------------ facts per run (baseline or scenario)
def run_facts(run: dict[str, Any]) -> dict[str, Any]:
    """Comparable figures from a STORED run (no recalculation of the backtest)."""
    base = {"status": run["status"], "reason": run.get("reason"), "run_id": run["id"]}
    res = run.get("result")
    if run["status"] != "COMPLETED" or not res:
        return base
    p, r, c = res["performance"], res["risk"], res["costs"]
    trades = sorted(res["trades"], key=lambda t: t["exit_time"])
    streak = worst = 0
    streak_loss = worst_loss = 0.0
    for t in trades:
        if t["net_pnl"] < 0:
            streak += 1
            streak_loss += t["net_pnl"]
            if streak > worst or (streak == worst and streak_loss < worst_loss):
                worst, worst_loss = streak, streak_loss
        else:
            streak, streak_loss = 0, 0.0
    lots = [float(t["lots"]) for t in trades]
    planned = [t.get("planned_risk_usd", t.get("risk_usd")) for t in trades]
    planned = [x for x in planned if x is not None]
    budgets = [t["risk_budget_usd"] for t in trades if t.get("risk_budget_usd") is not None]
    beyond = [t["loss_beyond_plan_usd"] for t in trades if (t.get("loss_beyond_plan_usd") or 0) > 0.005]
    rej = Counter(x["reason"] for x in res.get("rejections") or [])
    rej_total = (res.get("diagnostics") or {}).get("skipped") or {}
    return {**base, "trades": p["trades"], "wins": p["wins"], "losses": p["losses"], "win_rate": p["win_rate"],
            "net": p["net"], "return_pct": p["return_pct"], "ending_balance": p["ending_balance"],
            "profit_factor": p["profit_factor"], "expectancy_usd": p["expectancy_usd"], "average_r": p["average_r"],
            "max_drawdown": p["max_drawdown"], "max_drawdown_pct": p["max_drawdown_pct"],
            "largest_win": p["largest_win"], "largest_loss": p["largest_loss"],
            "longest_losing_streak": worst, "losing_streak_loss": round(worst_loss, 2),
            "average_lots": round(sum(lots) / len(lots), 3) if lots else None, "max_lots": max(lots) if lots else None,
            "min_lots": min(lots) if lots else None,
            "average_planned_risk_usd": round(sum(planned) / len(planned), 2) if planned else None,
            "max_planned_risk_usd": round(max(planned), 2) if planned else None,
            "average_risk_budget_usd": round(sum(budgets) / len(budgets), 2) if budgets else None,
            "max_risk_budget_usd": round(max(budgets), 2) if budgets else None,
            "stop_loss_losses_usd": round(sum(t["net_pnl"] for t in trades if t["exit_reason"] == "stop"), 2),
            "losses_beyond_plan": len(beyond), "losses_beyond_plan_usd": round(sum(beyond), 2),
            "budget_violations": sum(1 for t in trades if t.get("within_budget") is False),
            "capped_entries": sum(1 for t in trades if t.get("size_capped_by")),
            "rejected": sum(rej_total.values()) if rej_total else sum(rej.values()),
            "insufficient_volume_rejections": int(rej_total.get("risk_budget_below_min_volume", 0)),
            "margin_rejections": int(rej_total.get("insufficient_margin", 0)),
            "exposure_rejections": int(rej_total.get("exposure", 0)),
            "costs": c["total"], "planned_rr": r["planned_rr_average"], "small_sample": p["small_sample"],
            "max_used_margin": r["max_used_margin"]}


# ------------------------------------------------------------------ study reports
def load(ms: Any, sid: str) -> dict[str, Any]:
    with ms.store.read() as con:
        s = st.study(con, sid)
        if s is None:
            raise StudyRefused(f"unknown risk study {sid}")
        base = st.evaluation(con, s["baseline_evaluation_id"])
        base_runs = st.runs(con, base["id"], with_result=True)
        evs = []
        for eid in s["evaluation_ids"]:
            ev = st.evaluation(con, eid)
            ev["runs_full"] = st.runs(con, eid, with_result=True)
            evs.append(ev)
    return {"study": s, "baseline": base, "baseline_runs": base_runs, "evaluations": evs}


def _columns(data: dict[str, Any]) -> list[dict[str, Any]]:
    cols = [{"key": "baseline", "label": "Baseline fixed 0.02 lots", "evaluation_id": data["baseline"]["id"],
             "runs": data["baseline_runs"]}]
    for ev in data["evaluations"]:
        sc = ev["params"]["risk_scenario"]
        cols.append({"key": sc["key"], "label": f"{sc['risk_percent']:g}% of equity", "risk_percent": sc["risk_percent"],
                     "evaluation_id": ev["id"], "status": ev["status"], "runs": ev["runs_full"]})
    return cols


def _find(runs: list[dict[str, Any]], key: str, tf: str) -> dict[str, Any] | None:
    return next((r for r in runs if r["strategy_key"] == key and r["timeframe"] == tf), None)


def grid(data: dict[str, Any], kind: str = "existing") -> list[dict[str, Any]]:
    """Every strategy x timeframe with one cell per column (baseline + scenarios); each cell is a separate account."""
    cols = _columns(data)
    keys = sorted({r["strategy_key"] for ev in data["evaluations"] for r in ev["runs_full"] if r["kind"] == kind})
    rows = []
    for key in keys:
        for tf in RUN_TIMEFRAMES:
            cells = {}
            for c in cols:
                run = _find(c["runs"], key, tf)
                cells[c["key"]] = run_facts(run) if run else {"status": "NOT IN BASELINE" if c["key"] == "baseline" else "-"}
            name = next((r["config"]["name"] for ev in data["evaluations"] for r in ev["runs_full"]
                         if r["strategy_key"] == key), key)
            rows.append({"strategy": key, "name": name, "timeframe": tf, "cells": cells})
    return rows


def scenario_comparison(data: dict[str, Any], key: str) -> dict[str, Any]:
    """5 % vs 7.5 % vs 10 % (and the fixed-lot baseline) for ONE strategy, per timeframe."""
    cols = _columns(data)
    rows = [r for r in grid(data, "existing") + grid(data, "candidate") if r["strategy"] == key]
    findings = []
    for r in rows:
        sc = [(c["risk_percent"], r["cells"][c["key"]]) for c in cols if c["key"] != "baseline"]
        done = [(pct, f) for pct, f in sc if f.get("status") == "COMPLETED"]
        if len(done) < 2:
            if any(f.get("status") != "COMPLETED" for _p, f in sc):
                findings.append(f"{r['timeframe']}: not all scenarios completed - "
                                + "; ".join(f"{p:g}% {f.get('status')}: {f.get('reason') or ''}" for p, f in sc
                                            if f.get("status") != "COMPLETED"))
            continue
        nets = [f["net"] for _p, f in done]
        dds = [f["max_drawdown_pct"] or 0 for _p, f in done]
        trades = [f["trades"] for _p, f in done]
        line = (f"{r['timeframe']}: ending balance " + ", ".join(f"{p:g}% ${f['ending_balance']:,.2f}" for p, f in done)
                + "; max drawdown " + ", ".join(f"{p:g}% {f['max_drawdown_pct'] or 0:.1f}%" for p, f in done) + ".")
        findings.append(line)
        if all(n < 0 for n in nets) and all(nets[i + 1] <= nets[i] for i in range(len(nets) - 1)):
            findings.append(f"{r['timeframe']}: higher risk magnified the losses (net {nets[0]:+.2f} -> {nets[-1]:+.2f} USD).")
        elif all(n > 0 for n in nets) and all(dds[i + 1] >= dds[i] for i in range(len(dds) - 1)):
            findings.append(f"{r['timeframe']}: higher risk increased both the result and the drawdown "
                            f"({dds[0]:.1f}% -> {dds[-1]:.1f}%); one month is not evidence that this would persist.")
        if len(set(trades)) > 1:
            findings.append(f"{r['timeframe']}: trade counts differ between scenarios ({', '.join(str(t) for t in trades)}): "
                            "sizing changed which later signals could be taken (margin, minimum volume or "
                            "compounding), not the strategy's signals.")
        beyond = [(p, f) for p, f in done if f["losses_beyond_plan"]]
        if beyond:
            findings.append(f"{r['timeframe']}: losses beyond the planned stop risk (gaps / slippage): "
                            + ", ".join(f"{p:g}% {f['losses_beyond_plan']} trade(s), {f['losses_beyond_plan_usd']:.2f} USD"
                                        for p, f in beyond) + ".")
        margin = [(p, f) for p, f in done if f["margin_rejections"]]
        if margin:
            findings.append(f"{r['timeframe']}: entries rejected for hypothetical margin: "
                            + ", ".join(f"{p:g}% {f['margin_rejections']}" for p, f in margin) + ".")
        streaks = [(p, f) for p, f in done if f["longest_losing_streak"] >= 3]
        if streaks:
            findings.append(f"{r['timeframe']}: longest losing streak " + ", ".join(
                f"{p:g}% {f['longest_losing_streak']} trades ({f['losing_streak_loss']:+.2f} USD)" for p, f in streaks) + ".")
    findings.append("No scenario is a recommended setting: these are hypothetical stress tests on one historical month.")
    return {"strategy": key, "columns": [{k: c[k] for k in c if k != "runs"} for c in cols], "rows": rows,
            "findings": findings, "banner": runner.RISK_BANNER}


def strategy_comparison(data: dict[str, Any], key: str) -> dict[str, Any]:
    """M15 vs M5 vs M1 for ONE strategy, within each risk scenario (and the baseline)."""
    cols = _columns(data)
    rows = [r for r in grid(data, "existing") + grid(data, "candidate") if r["strategy"] == key]
    findings = []
    for c in cols:
        done = [(r["timeframe"], r["cells"][c["key"]]) for r in rows if r["cells"][c["key"]].get("status") == "COMPLETED"]
        missing = [(r["timeframe"], r["cells"][c["key"]]) for r in rows if r["cells"][c["key"]].get("status") != "COMPLETED"]
        if done:
            best = max(done, key=lambda x: x[1]["net"])
            low_dd = min(done, key=lambda x: x[1]["max_drawdown_pct"] or 0)
            findings.append(f"{c['label']}: highest net {best[0]} ({best[1]['net']:+.2f} USD, {best[1]['trades']} trades); "
                            f"lowest drawdown {low_dd[0]} ({low_dd[1]['max_drawdown_pct'] or 0:.1f}%).")
        for tf, f in missing:
            findings.append(f"{c['label']} {tf}: {f.get('status')} - {f.get('reason') or 'not run'}.")
    findings.append("No timeframe is declared best on return alone; each cell is a separate hypothetical account.")
    return {"strategy": key, "columns": [{k: c[k] for k in c if k != "runs"} for c in cols], "rows": rows,
            "findings": findings, "banner": runner.RISK_BANNER}


def master(data: dict[str, Any]) -> dict[str, Any]:
    cols = _columns(data)
    existing = grid(data, "existing")
    cands = grid(data, "candidate")
    scenario_runs = [r["cells"][c["key"]] for r in existing for c in cols if c["key"] != "baseline"]
    done = [f for f in scenario_runs if f.get("status") == "COMPLETED"]
    findings = [f"{len(done)} of {len(scenario_runs)} percentage-risk runs of A/B/C completed "
                f"({sum(1 for f in done if f['net'] > 0)} ended positive)."]
    for c in cols:
        if c["key"] == "baseline":
            continue
        mine = [r["cells"][c["key"]] for r in existing if r["cells"][c["key"]].get("status") == "COMPLETED"]
        if mine:
            worst = min(mine, key=lambda f: f["ending_balance"])
            findings.append(f"{c['label']}: lowest ending balance ${worst['ending_balance']:,.2f}; deepest drawdown "
                            f"{max(f['max_drawdown_pct'] or 0 for f in mine):.1f}%; margin rejections "
                            f"{sum(f['margin_rejections'] for f in mine)}; minimum-volume rejections "
                            f"{sum(f['insufficient_volume_rejections'] for f in mine)}; losses beyond plan "
                            f"{sum(f['losses_beyond_plan'] for f in mine)}.")
    s = data["study"]
    return {"study": {k: s[k] for k in ("id", "month", "status", "baseline_evaluation_id", "evaluation_ids", "created_at",
                                        "finished_at", "app_version", "error")},
            "scenarios": s["params"]["scenarios"], "columns": [{k: c[k] for k in c if k != "runs"} for c in cols],
            "rows": existing, "candidates": cands, "findings": findings,
            "separate_accounts_note": ("Every cell is a SEPARATE hypothetical $1,000 account (strategy x timeframe x "
                                       "sizing). Balances are never added together: no cell, row or column is a "
                                       "portfolio."),
            "banner": runner.RISK_BANNER, "disclaimer": report.DISCLAIMER}
