"""Strategy journal: the link between a strategy setup and the SIMULATED paper position it prefilled.
The outcome (exit, net P/L, close reason) is read from paper_positions when the journal is read."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso


def insert(con: sqlite3.Connection, *, strategy_id: int, strategy_version: int, setup_id: str, signal_id: str | None,
           signal_time: str, position_id: int, opened_at: datetime, planned: dict[str, Any],
           entry_costs: dict[str, Any], conditions: list[dict[str, Any]]) -> int:
    cur = con.execute(
        "INSERT INTO strategy_journal (strategy_id, strategy_version, setup_id, signal_id, signal_time, position_id, "
        "opened_at, planned, entry_costs, conditions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, strategy_version, setup_id, signal_id, signal_time, position_id, iso(opened_at),
         json.dumps(planned, default=str), json.dumps(entry_costs, default=str), json.dumps(conditions, default=str)))
    return int(cur.lastrowid)


def entries(con: sqlite3.Connection, *, strategy_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = ("SELECT j.*, s.name AS strategy_name, p.side, p.lots, p.open_price, p.open_time, p.status, p.close_price, "
           "p.close_time, p.close_reason, p.realized_gross, p.realized_net, p.open_commission, p.close_commission, "
           "p.open_spread, p.open_slippage, p.stop_loss, p.take_profit "
           "FROM strategy_journal j JOIN strategies s ON s.id = j.strategy_id "
           "JOIN paper_positions p ON p.id = j.position_id")
    args: list[Any] = []
    if strategy_id is not None:
        sql += " WHERE j.strategy_id = ?"
        args.append(strategy_id)
    sql += " ORDER BY j.opened_at DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in con.execute(sql, args).fetchall()]


def _row(r: sqlite3.Row) -> dict[str, Any]:
    planned = json.loads(r["planned"])
    if r["status"] == "closed":
        reason = {"stop_loss": f"invalidated: stop loss at {r['stop_loss']} ({planned.get('stop_source', 'planned stop')})",
                  "take_profit": f"target reached at {r['take_profit']}",
                  "manual": "closed manually before the planned exit"}.get(r["close_reason"], r["close_reason"])
    else:
        reason = None
    return {"id": r["id"], "strategy_id": r["strategy_id"], "strategy_name": r["strategy_name"],
            "strategy_version": r["strategy_version"], "setup_id": r["setup_id"], "signal_id": r["signal_id"],
            "signal_time": r["signal_time"], "position_id": r["position_id"], "opened_at": r["opened_at"],
            "planned": planned, "entry_costs": json.loads(r["entry_costs"]), "conditions": json.loads(r["conditions"]),
            "side": r["side"], "lots": r["lots"], "entry_price": r["open_price"], "entry_time": r["open_time"],
            "status": r["status"], "exit_price": r["close_price"], "exit_time": r["close_time"],
            "close_reason": r["close_reason"], "invalidation_reason": reason,
            "gross_pnl": r["realized_gross"], "net_pnl": r["realized_net"],
            "commission": {"open": r["open_commission"], "close": r["close_commission"]},
            "label": "SIMULATED paper position (no broker order)"}
