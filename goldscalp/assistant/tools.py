"""Read-only data tools of the research assistant (PHASE4_ARCHITECTURE.md section 9.1).

Every tool reads STORED GoldScalp records (signals, SIMULATED trades, evaluations, research runs, proposals,
verification records) and returns {"data", "sources", "as_of"}; ``sources`` lists table:id references. No tool
writes anything, reaches a broker, or calls the network."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from goldscalp.core.clock import iso
from goldscalp.live import journal
from goldscalp.storage import improvement_repo, live_repo


def _out(gs: Any, data: Any, sources: list[str]) -> dict[str, Any]:
    return {"data": data, "sources": sorted(set(sources)), "as_of": iso(gs.clock.now())}


def _since(gs: Any, days: float | None) -> str | None:
    return None if not days else iso(gs.clock.now() - timedelta(days=float(days)))


def connection_status(gs: Any) -> dict[str, Any]:
    snap = gs.market.snapshot()
    with gs.db.read() as con:
        ver = live_repo.verifications(con, limit=1)
    v = ver[0] if ver else None
    provider = snap.get("provider") or {}
    details = snap.get("provider_details") or {}
    term = (details.get("terminal") or {}) if isinstance(details, dict) else {}
    data = {"provider": provider.get("id"), "state": snap.get("state"), "reason": snap.get("reason"),
            "quote": snap.get("quote"), "broker": term.get("broker"), "symbol": details.get("symbol"),
            "latest_verification": None if v is None else {k: v[k] for k in (
                "id", "started_at", "overall", "adapter", "broker", "is_vantage", "environment", "symbol",
                "actual_terminal")}}
    return _out(gs, data, [f"mt5_verifications:{v['id']}"] if v else [])


def latest_signal(gs: Any, strategy_id: int | None = None) -> dict[str, Any]:
    with gs.db.read() as con:
        sigs = live_repo.signals(con, strategy_id=strategy_id, limit=1)
        if not sigs:
            return _out(gs, None, [])
        sig = sigs[0]
        t = live_repo.trade_by_signal(con, sig["id"])
        ev = live_repo.trade_evaluation(con, t["id"]) if t else None
    sig = {k: v for k, v in sig.items() if k != "snapshot"}
    src = [f"strategy_signals:{sig['id']}"]
    trade = None
    if t is not None:
        src += [f"live_trades:{t['id']}", f"paper_positions:{t['position_id']}"]
        trade = {k: t[k] for k in ("id", "status", "side", "lots", "open_price", "open_time", "close_price", "close_time",
                                   "close_reason", "realized_net", "mfe", "mae", "stop_loss", "take_profit")}
        if t["status"] == "open":
            pos = next((p for p in gs.engine.positions_view("open", t["account_id"]) if p["id"] == t["position_id"]), None)
            trade["unrealized_pnl"] = (pos or {}).get("unrealized_pnl")
    if ev:
        src.append(f"trade_evaluations:{ev['id']}")
    return _out(gs, {"signal": sig, "trade": trade, "evaluation": ev}, src)


def trade_detail(gs: Any, trade_id: int | None = None, losing_only: bool = True) -> dict[str, Any]:
    with gs.db.read() as con:
        if trade_id is not None:
            t = live_repo.trade(con, trade_id)
        else:
            closed = live_repo.trades(con, status="closed", limit=200)
            pick = [x for x in closed if Decimal(x["realized_net"] or "0") < 0] if losing_only else closed
            pick.sort(key=lambda x: x["close_time"] or "", reverse=True)
            t = pick[0] if pick else None
        if t is None:
            return _out(gs, None, [])
        ev = live_repo.trade_evaluation(con, t["id"])
        sig = live_repo.signal(con, t["signal_id"])
    src = [f"live_trades:{t['id']}", f"paper_positions:{t['position_id']}", f"strategy_signals:{t['signal_id']}"]
    if ev:
        src.append(f"trade_evaluations:{ev['id']}")
    data = {"trade": {k: t[k] for k in ("id", "strategy_name", "version", "side", "lots", "open_price", "open_time",
                                        "close_price", "close_time", "close_reason", "realized_gross", "realized_net",
                                        "mfe", "mae", "stop_loss", "take_profit", "status")},
            "evaluation": ev, "signal": None if sig is None else {k: sig.get(k) for k in (
                "id", "cutoff", "direction", "timeframe", "supporting", "conflicting", "invalidation", "outcome")}}
    return _out(gs, data, src)


def strategy_performance(gs: Any, days: float | None = 7) -> dict[str, Any]:
    since = _since(gs, days)
    min_trades = 30
    start_bal = float(gs.settings.paper_starting_balance)
    rows, src = [], []
    with gs.db.read() as con:
        for m in live_repo.monitored_list(con):
            trades = [t for t in live_repo.trades(con, monitored_id=m["id"], limit=5000)
                      if since is None or (t["close_time"] or t["opened_at"]) >= since]
            sigs = [s for s in live_repo.signals(con, strategy_id=m["strategy_id"], version=m["version"], limit=5000)
                    if since is None or s["cutoff"] >= since]
            met = journal.live_metrics(trades, starting_balance=start_bal, min_trades=min_trades)
            src += [f"live_trades:{t['id']}" for t in trades] + [f"monitored_strategies:{m['id']}"]
            rows.append({"strategy_id": m["strategy_id"], "name": m["strategy_name"], "version": m["version"],
                         "mode": m["mode"], "status": m["status"], "signals": len(sigs),
                         "false_signals": sum(1 for s in sigs if (s.get("outcome") or {}).get("false_signal")),
                         **{k: met.get(k) for k in ("trades", "wins", "losses", "net", "win_rate", "profit_factor",
                                                    "expectancy_r", "max_drawdown", "sufficient", "reason",
                                                    "open_trades")}})
    traded = [r for r in rows if r["trades"]]
    best = max(traded, key=lambda r: (r["net"], -r["strategy_id"])) if traded else None
    return _out(gs, {"period_days": days, "since": since, "strategies": rows, "best_by_net": best,
                     "min_trades_for_ratios": min_trades}, src)


def false_signals_by_session(gs: Any, days: float | None = None) -> dict[str, Any]:
    since = _since(gs, days)
    groups: dict[str, dict[str, Any]] = {}
    src = []
    with gs.db.read() as con:
        sigs = live_repo.signals(con, start=since, limit=10000)
    for s in sigs:
        oc = s.get("outcome") or {}
        if oc.get("status") != "complete":
            continue
        sess = " / ".join((s.get("context") or {}).get("sessions") or []) or "none"
        g = groups.setdefault(sess, {"session": sess, "measured_signals": 0, "false_signals": 0, "examples": []})
        g["measured_signals"] += 1
        if oc.get("false_signal"):
            g["false_signals"] += 1
            if len(g["examples"]) < 5:
                g["examples"].append(s["id"])
            src.append(f"strategy_signals:{s['id']}")
    rows = sorted(groups.values(), key=lambda g: (-g["false_signals"], g["session"]))
    for g in rows:
        g["rate"] = round(g["false_signals"] / g["measured_signals"], 3) if g["measured_signals"] else None
    pending = sum(1 for s in sigs if (s.get("outcome") or {}).get("status") != "complete")
    return _out(gs, {"since": since, "sessions": rows, "pending_outcome": pending,
                     "definition": "false signal = the planned stop was touched first after the signal, or the move "
                                   "never reached half of the planned risk within the horizon (outcome@1)"}, src)


def backtest_vs_live(gs: Any, strategy_id: int | None = None, version: int | None = None) -> dict[str, Any]:
    rows, src = [], []
    start_bal = float(gs.settings.paper_starting_balance)
    with gs.db.read() as con:
        for m in live_repo.monitored_list(con):
            if (strategy_id is not None and m["strategy_id"] != strategy_id) or (version is not None and m["version"] != version):
                continue
            trades = live_repo.trades(con, monitored_id=m["id"], limit=5000)
            live = journal.live_metrics(trades, starting_balance=start_bal, min_trades=30)
            runs = (m["eligibility"] or {}).get("runs") or []
            base = next((r for r in runs if r.get("sufficient")), runs[0] if runs else None)
            diffs = []
            for t in trades:
                if t["status"] != "closed":
                    continue
                sig = live_repo.signal(con, t["signal_id"]) or {}
                rep = ((sig.get("outcome") or {}).get("replica") or {}).get("trade")
                if not rep:
                    continue
                same_exit = {"stop": "stop_loss", "target": "take_profit", "time": "time_exit"}.get(rep["exit_reason"]) \
                    == t["close_reason"]
                diffs.append({"trade_id": t["id"], "live_entry": float(t["open_price"]), "replica_entry": rep["entry_price"],
                              "entry_diff": round(float(t["open_price"]) - rep["entry_price"], 3),
                              "live_exit_reason": t["close_reason"], "replica_exit_reason": rep["exit_reason"],
                              "same_exit": same_exit, "live_r": journal.trade_record(t)["r_multiple"],
                              "replica_r": rep["r_multiple"]})
                src.append(f"live_trades:{t['id']}")
            if base:
                src.append(f"research_runs:{base['id']}")
            src.append(f"monitored_strategies:{m['id']}")
            rows.append({"strategy_id": m["strategy_id"], "name": m["strategy_name"], "version": m["version"],
                         "backtest": base, "live": {k: live.get(k) for k in ("trades", "net", "win_rate", "profit_factor",
                                                                             "expectancy_r", "sufficient", "reason")},
                         "replica_comparisons": diffs,
                         "same_exit_rate": round(sum(d["same_exit"] for d in diffs) / len(diffs), 3) if diffs else None,
                         "mean_entry_diff": round(sum(d["entry_diff"] for d in diffs) / len(diffs), 3) if diffs else None})
    known = ["Backtest entries fill at the next candle open; live-paper entries fill at the live quote when the cut-off "
             "is processed (latency is recorded).",
             "Backtest spreads come from stored candles (or the configured spread); live-paper fills pay the live quote "
             "spread.",
             "Backtest stops/targets are checked on candle highs/lows (with an intrabar rule); live-paper stops/targets "
             "trigger on polled quotes and close at the current price.",
             "Different periods: the backtest covered its research range, live-paper covers the monitoring period, and "
             "small live samples carry wide uncertainty."]
    return _out(gs, {"strategies": rows, "structural_differences": known}, src)


def proposals_summary(gs: Any, status: str | None = None) -> dict[str, Any]:
    with gs.db.read() as con:
        items = improvement_repo.proposals(con, status=status, limit=100)
    data = [{k: p[k] for k in ("id", "strategy_name", "base_version", "kind", "title", "diff", "explanation", "verdict",
                               "status", "created_at", "resulting_version")}
            | {"holdout": {"base": (p["validation"] or {}).get("base"), "candidate": (p["validation"] or {}).get("candidate"),
                           "why": (p["validation"] or {}).get("why")}, "limitations": p["limitations"]}
            for p in items]
    return _out(gs, data, [f"strategy_proposals:{p['id']}" for p in items])


def evaluated_versions(gs: Any) -> dict[str, Any]:
    rows, src = [], []
    with gs.db.read() as con:
        for m in live_repo.monitored_list(con):
            counts = live_repo.evaluation_counts(con, m["id"])
            acct = None
            if m["account_id"]:
                s = gs.engine.summary(m["account_id"])
                acct = {k: s.get(k) for k in ("account_id", "balance", "equity", "open_positions", "closed_positions")}
            rows.append({"monitored_id": m["id"], "strategy_id": m["strategy_id"], "name": m["strategy_name"],
                         "version": m["version"], "fingerprint": m["fingerprint"], "mode": m["mode"], "status": m["status"],
                         "started_at": m["started_at"], "step_timeframe": m["config"]["evaluation"]["step_timeframe"],
                         "evaluations": counts, "account": acct,
                         "validated_by": [r["id"] for r in (m["eligibility"] or {}).get("runs") or []][:5]})
            src.append(f"monitored_strategies:{m['id']}")
    return _out(gs, rows, src)


def journal_search(gs: Any, **filters: Any) -> dict[str, Any]:
    with gs.db.read() as con:
        items = journal.entries(con, **{k: v for k, v in filters.items() if v is not None}, limit=50)
    slim = [{k: e[k] for k in ("signal_id", "timestamp", "strategy", "session", "timeframe", "direction", "outcome",
                               "net_pnl", "close_reason", "invalidated", "went_well", "went_wrong")} for e in items]
    return _out(gs, slim, [f"strategy_signals:{e['signal_id']}" for e in items])


TOOLS = {"connection_status": connection_status, "latest_signal": latest_signal, "trade_detail": trade_detail,
         "strategy_performance": strategy_performance, "false_signals_by_session": false_signals_by_session,
         "backtest_vs_live": backtest_vs_live, "proposals_summary": proposals_summary,
         "evaluated_versions": evaluated_versions, "journal_search": journal_search}


def now_label(gs: Any) -> datetime:
    return gs.clock.now()
