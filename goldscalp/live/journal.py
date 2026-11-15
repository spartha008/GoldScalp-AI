"""The live journal (PHASE4_ARCHITECTURE.md section 6) and live-paper metrics.

One journal entry per strategy signal, joined with its SIMULATED trade, paper position and evaluation.
Live-paper metrics reuse the Phase 3 metrics@1 formulas (ratios withheld below the minimum sample)."""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from goldscalp.backtesting import metrics
from goldscalp.storage import live_repo

OUTCOMES = ("win", "loss", "breakeven", "open", "not_traded", "invalidated")
INVALIDATING = {"stop_loss": "stop loss hit", "strategy_exit": "structure flip (strategy exit)"}


def trade_record(t: dict[str, Any]) -> dict[str, Any]:
    """A closed live trade in the shape metrics@1 expects (same fields as a backtest trade)."""
    ounces = Decimal(t["ounces"])
    entry = Decimal(t["open_price"])
    stop = Decimal(t["stop_loss"]) if t["stop_loss"] else None
    risk = abs(entry - stop) if stop is not None else None
    net = Decimal(t["realized_net"] or "0")
    close_spread = (t["exit"] or {}).get("spread_at_close")
    spread_paid = Decimal(t["open_spread"]) if t["side"] == "buy" else Decimal(close_spread or t["open_spread"])
    mfe, mae = Decimal(t["mfe"]), Decimal(t["mae"])
    atr = (t["plan"] or {}).get("atr")
    return {"id": t["id"], "side": t["side"], "net_pnl": float(net),
            "commission": float(Decimal(t["open_commission"] or 0) + Decimal(t["close_commission"] or 0)),
            "spread_cost": float(spread_paid * ounces), "slippage_cost": float(Decimal(t["open_slippage"]) * ounces * 2),
            "exit_reason": t["close_reason"], "r_multiple": float(net / (risk * ounces)) if risk else None,
            "mfe": float(mfe), "mae": float(mae), "mfe_r": float(mfe / risk) if risk else None,
            "mae_r": float(mae / risk) if risk else None, "mfe_atr": float(mfe) / atr if atr else None,
            "mae_atr": float(mae) / atr if atr else None,
            "holding_minutes": (t["exit"] or {}).get("holding_minutes") or 0,
            "session": " / ".join((t["context_entry"] or {}).get("sessions") or []) or "none",
            "volatility": (((t["context_entry"] or {}).get("timeframes") or {}).get((t["plan"] or {}).get("step") or "M5")
                           or {}).get("volatility") or "unknown",
            "timeframe": None, "exit_time": t["close_time"], "entry_time": t["open_time"]}


def live_metrics(trades: list[dict[str, Any]], *, starting_balance: float, min_trades: int) -> dict[str, Any]:
    closed = sorted([t for t in trades if t["status"] == "closed"], key=lambda t: t["close_time"] or "")
    rows = [trade_record(t) for t in closed]
    equity, curve = Decimal(str(starting_balance)), []
    for r in rows:
        equity += Decimal(str(r["net_pnl"]))
        curve.append({"time": r["exit_time"], "equity": float(equity)})
    m = metrics.compute(rows, starting_balance=starting_balance, min_trades=min_trades, closed_equity=curve)
    m["by_session"] = metrics.breakdown(rows, "session", starting_balance=starting_balance, min_trades=min_trades)
    m["open_trades"] = sum(1 for t in trades if t["status"] == "open")
    m["label"] = "SIMULATED live-paper results"
    return m


def _outcome(sig: dict[str, Any], t: dict[str, Any] | None) -> tuple[str, bool, str | None]:
    if t is None:
        so = sig.get("outcome") or {}
        return "not_traded", bool(so.get("first_touch") == "stop"), (
            "the stop level was touched first after the signal (observed, not traded)"
            if so.get("first_touch") == "stop" else None)
    if t["status"] == "open":
        return "open", False, None
    net = Decimal(t["realized_net"] or "0")
    outcome = "win" if net > 0 else "loss" if net < 0 else "breakeven"
    how = INVALIDATING.get(t["close_reason"])
    return outcome, how is not None, how


def entries(con: Any, *, start: str | None = None, end: str | None = None, strategy_id: int | None = None,
            version: int | None = None, session: str | None = None, timeframe: str | None = None,
            direction: str | None = None, outcome: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    out = []
    for sig in live_repo.signals(con, strategy_id=strategy_id, version=version, start=start, end=end, limit=2000):
        if timeframe and sig["timeframe"] != timeframe:
            continue
        if direction and sig["direction"] != direction:
            continue
        sessions = (sig.get("context") or {}).get("sessions") or []
        if session and session not in sessions and not (session == "none" and not sessions):
            continue
        t = live_repo.trade_by_signal(con, sig["id"])
        result, invalidated, how = _outcome(sig, t)
        if outcome and not (outcome == result or (outcome == "invalidated" and invalidated)):
            continue
        ev = live_repo.trade_evaluation(con, t["id"]) if t else None
        costs = None
        if t is not None:
            rec = trade_record(t) if t["status"] == "closed" else None
            costs = {"spread_at_signal": sig["price"]["spread"], "spread_at_fill": t["open_spread"],
                     "spread_at_close": (t["exit"] or {}).get("spread_at_close"),
                     "commission": rec["commission"] if rec else t["open_commission"],
                     "spread_cost": rec["spread_cost"] if rec else None,
                     "slippage_cost": rec["slippage_cost"] if rec else None}
        ctx = sig.get("context") or {}
        out.append({
            "signal_id": sig["id"], "timestamp": sig["cutoff"], "generated_at": sig["generated_at"],
            "strategy": sig["strategy"], "session": sessions, "timeframe": sig["timeframe"],
            "direction": sig["direction"], "signal_status": sig["status"], "status_reason": sig.get("status_reason"),
            "entry_assumptions": {"plan": {k: sig["plan"].get(k) for k in ("entry_estimate", "entry_assumption", "lots",
                                                                            "sizing")},
                                  "actual": None if t is None else t["entry"]},
            "exit_assumptions": {"stop": sig["plan"].get("stop"), "stop_source": sig["plan"].get("stop_source"),
                                 "target": sig["plan"].get("target"),
                                 "max_holding_minutes": sig["plan"].get("max_holding_minutes"),
                                 "exit_on_structure_flip": sig["plan"].get("exit_on_structure_flip"),
                                 "invalidation": sig.get("invalidation")},
            "outcome": result, "net_pnl": None if t is None else t["realized_net"],
            "close_reason": None if t is None else t["close_reason"],
            "signal_outcome": sig.get("outcome"),
            "mfe": None if t is None else t["mfe"], "mae": None if t is None else t["mae"],
            "costs": costs, "invalidated": invalidated, "invalidated_how": how,
            "went_well": (ev or {}).get("went_well") or [], "went_wrong": (ev or {}).get("went_wrong") or [],
            "evaluation_summary": (ev or {}).get("summary"),
            "market_conditions": {"sessions": sessions, "price": sig["price"], "spread_context": sig.get("spread_context"),
                                  "timeframes": ctx.get("timeframes"), "phase2_signal": ctx.get("signal")},
            "supporting": sig.get("supporting"), "conflicting": sig.get("conflicting"),
            "trade_id": None if t is None else t["id"], "position_id": None if t is None else t["position_id"],
            "notes": sig.get("notes"), "label": "SIMULATED"})
        if len(out) >= limit:
            break
    return out


def to_csv(items: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["label", "timestamp", "strategy", "version", "session", "timeframe", "direction", "signal_status",
                "outcome", "net_pnl", "close_reason", "invalidated", "invalidated_how", "mfe", "mae", "spread_at_signal",
                "spread_at_fill", "commission", "planned_stop", "planned_target", "went_well", "went_wrong", "notes",
                "signal_id", "trade_id"])
    for e in items:
        c = e["costs"] or {}
        w.writerow([e["label"], e["timestamp"], e["strategy"]["name"], e["strategy"]["version"], " / ".join(e["session"]),
                    e["timeframe"], e["direction"], e["signal_status"], e["outcome"], e["net_pnl"], e["close_reason"],
                    e["invalidated"], e["invalidated_how"], e["mfe"], e["mae"], c.get("spread_at_signal"),
                    c.get("spread_at_fill"), c.get("commission"), e["exit_assumptions"]["stop"],
                    e["exit_assumptions"]["target"], "; ".join(e["went_well"]), "; ".join(e["went_wrong"]),
                    e["notes"], e["signal_id"], e["trade_id"]])
    return buf.getvalue()
