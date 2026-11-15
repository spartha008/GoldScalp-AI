"""Phase 4 storage: MT5 verification records, feed health, monitored strategy versions, monitor evaluations,
strategy signals, SIMULATED live trades, per-trade evaluations (insert-only) and the assistant log."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso

SIMULATED = "SIMULATED"


def _j(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _l(text: str | None, default: Any = None) -> Any:
    return default if text is None else json.loads(text)


# ------------------------------------------------------------------ MT5 verification records
def insert_verification(con: sqlite3.Connection, rec: dict[str, Any]) -> None:
    con.execute("INSERT INTO mt5_verifications (id, started_at, finished_at, overall, adapter, broker, is_vantage, "
                "environment, symbol, checks, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec["id"], rec["started_at"], rec["finished_at"], rec["overall"], rec.get("adapter") or "none",
                 rec.get("broker"), None if rec.get("is_vantage") is None else int(rec["is_vantage"]),
                 rec.get("environment"), rec.get("symbol"), _j(rec["checks"]),
                 _j({k: rec.get(k) for k in ("definition", "counts", "server")})))


def verifications(con: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM mt5_verifications ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [_verification(r) for r in rows]


def verification(con: sqlite3.Connection, vid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM mt5_verifications WHERE id = ?", (vid,)).fetchone()
    return _verification(row) if row else None


def _verification(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "started_at", "finished_at", "overall", "adapter", "broker", "environment", "symbol")}
    out["is_vantage"] = None if r["is_vantage"] is None else bool(r["is_vantage"])
    out["checks"] = _l(r["checks"], [])
    out.update(_l(r["summary"], {}))
    out["actual_terminal"] = r["adapter"] == "MetaTrader5"
    return out


# ------------------------------------------------------------------ feed health
def insert_feed_sample(con: sqlite3.Connection, s: dict[str, Any]) -> None:
    con.execute("INSERT OR REPLACE INTO feed_samples (ts, provider, state, bid, ask, spread, quote_time, tick_age_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (s["ts"], s["provider"], s["state"], s.get("bid"), s.get("ask"),
                                                    s.get("spread"), s.get("quote_time"), s.get("tick_age_seconds")))


def insert_feed_event(con: sqlite3.Connection, ts: datetime, provider: str, old: str | None, new: str,
                      reason: str | None) -> None:
    con.execute("INSERT INTO feed_events (ts, provider, from_state, to_state, reason) VALUES (?, ?, ?, ?, ?)",
                (iso(ts), provider, old, new, reason))


def feed_samples(con: sqlite3.Connection, *, start: str | None = None, end: str | None = None,
                 limit: int = 5000) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM feed_samples WHERE 1 = 1", []
    if start:
        sql += " AND ts >= ?"
        args.append(start)
    if end:
        sql += " AND ts <= ?"
        args.append(end)
    sql = f"SELECT * FROM ({sql} ORDER BY ts DESC LIMIT ?) ORDER BY ts"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def feed_events(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM feed_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ------------------------------------------------------------------ monitored strategy versions
def insert_monitored(con: sqlite3.Connection, *, strategy_id: int, version: int, fingerprint: str, mode: str,
                     account_id: int | None, eligibility: dict[str, Any], now: datetime, note: str | None) -> int:
    cur = con.execute("INSERT INTO monitored_strategies (strategy_id, version, fingerprint, mode, status, account_id, "
                      "eligibility, state, started_at, updated_at, note) VALUES (?, ?, ?, ?, 'active', ?, ?, '{}', ?, ?, ?)",
                      (strategy_id, version, fingerprint, mode, account_id, _j(eligibility), iso(now), iso(now), note))
    return int(cur.lastrowid)


def monitored(con: sqlite3.Connection, mid: int) -> dict[str, Any] | None:
    row = con.execute(_MON_SQL + " WHERE m.id = ?", (mid,)).fetchone()
    return _monitored(row) if row else None


def monitored_list(con: sqlite3.Connection, *, status: str | None = None,
                   include_stopped: bool = True) -> list[dict[str, Any]]:
    sql, args = _MON_SQL + " WHERE 1 = 1", []
    if status:
        sql += " AND m.status = ?"
        args.append(status)
    elif not include_stopped:
        sql += " AND m.status != 'stopped'"
    return [_monitored(r) for r in con.execute(sql + " ORDER BY m.id", args).fetchall()]


def find_monitored(con: sqlite3.Connection, strategy_id: int, version: int) -> dict[str, Any] | None:
    row = con.execute(_MON_SQL + " WHERE m.strategy_id = ? AND m.version = ? AND m.status != 'stopped'",
                      (strategy_id, version)).fetchone()
    return _monitored(row) if row else None


def update_monitored(con: sqlite3.Connection, mid: int, now: datetime, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(now)]
    for key, value in fields.items():
        if key not in ("status", "mode", "account_id", "state", "stopped_at", "note"):
            raise ValueError(key)
        sets.append(f"{key} = ?")
        args.append(_j(value) if key == "state" else value)
    con.execute(f"UPDATE monitored_strategies SET {', '.join(sets)} WHERE id = ?", (*args, mid))


_MON_SQL = ("SELECT m.*, s.name AS strategy_name, v.config AS config FROM monitored_strategies m "
            "JOIN strategies s ON s.id = m.strategy_id "
            "JOIN strategy_versions v ON v.strategy_id = m.strategy_id AND v.version = m.version")


def _monitored(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "strategy_id", "version", "fingerprint", "mode", "status", "account_id",
                             "started_at", "updated_at", "stopped_at", "note", "strategy_name")}
    out["eligibility"] = _l(r["eligibility"], {})
    out["state"] = _l(r["state"], {})
    out["config"] = _l(r["config"], {})
    return out


# ------------------------------------------------------------------ monitor evaluations (one per cut-off)
def insert_evaluation(con: sqlite3.Connection, mid: int, cutoff: str, now: datetime, outcome: str,
                      reason: str | None = None, signal_id: str | None = None) -> None:
    con.execute("INSERT OR IGNORE INTO monitor_evaluations (monitored_id, cutoff, evaluated_at, outcome, reason, "
                "signal_id) VALUES (?, ?, ?, ?, ?, ?)", (mid, cutoff, iso(now), outcome, reason, signal_id))


def evaluations(con: sqlite3.Connection, mid: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM monitor_evaluations WHERE monitored_id = ? ORDER BY cutoff DESC LIMIT ?",
                       (mid, limit)).fetchall()
    return [dict(r) for r in rows]


def evaluation_counts(con: sqlite3.Connection, mid: int) -> dict[str, int]:
    rows = con.execute("SELECT outcome, COUNT(*) AS n FROM monitor_evaluations WHERE monitored_id = ? GROUP BY outcome",
                       (mid,)).fetchall()
    return {r["outcome"]: int(r["n"]) for r in rows}


# ------------------------------------------------------------------ strategy signals
def insert_signal(con: sqlite3.Connection, sig: dict[str, Any]) -> bool:
    cur = con.execute("INSERT OR IGNORE INTO strategy_signals (id, monitored_id, strategy_id, version, cutoff, "
                      "generated_at, timeframe, direction, status, status_reason, session, payload) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (sig["id"], sig["monitored_id"], sig["strategy"]["id"], sig["strategy"]["version"], sig["cutoff"],
                       sig["generated_at"], sig["timeframe"], sig["direction"], sig["status"], sig.get("status_reason"),
                       " / ".join(sig["context"]["sessions"]) or "none", _j(sig)))
    return cur.rowcount > 0


def set_signal_status(con: sqlite3.Connection, sid: str, status: str, reason: str | None) -> None:
    con.execute("UPDATE strategy_signals SET status = ?, status_reason = ? WHERE id = ?", (status, reason, sid))


def set_signal_outcome(con: sqlite3.Connection, sid: str, outcome: dict[str, Any], now: datetime) -> None:
    con.execute("UPDATE strategy_signals SET outcome = ?, outcome_at = ? WHERE id = ?", (_j(outcome), iso(now), sid))


def set_signal_notes(con: sqlite3.Connection, sid: str, notes: str | None) -> bool:
    return con.execute("UPDATE strategy_signals SET notes = ? WHERE id = ?", (notes, sid)).rowcount > 0


def signal(con: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM strategy_signals WHERE id = ?", (sid,)).fetchone()
    return _signal(row) if row else None


def signals(con: sqlite3.Connection, *, strategy_id: int | None = None, version: int | None = None,
            start: str | None = None, end: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM strategy_signals WHERE 1 = 1", []
    for col, val in (("strategy_id", strategy_id), ("version", version)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    if start:
        sql += " AND cutoff >= ?"
        args.append(start)
    if end:
        sql += " AND cutoff <= ?"
        args.append(end)
    sql += " ORDER BY cutoff DESC, id LIMIT ?"
    args.append(limit)
    return [_signal(r) for r in con.execute(sql, args).fetchall()]


def signals_pending_outcome(con: sqlite3.Connection, before: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM strategy_signals WHERE outcome IS NULL AND cutoff <= ? ORDER BY cutoff LIMIT ?",
                       (before, limit)).fetchall()
    return [_signal(r) for r in rows]


def _signal(r: sqlite3.Row) -> dict[str, Any]:
    out = _l(r["payload"], {})
    out.update(status=r["status"], status_reason=r["status_reason"], outcome=_l(r["outcome"]), outcome_at=r["outcome_at"],
               notes=r["notes"], label=SIMULATED)
    return out


# ------------------------------------------------------------------ SIMULATED live trades
def insert_trade(con: sqlite3.Connection, *, signal_id: str, monitored_id: int, strategy_id: int, version: int,
                 account_id: int, position_id: int, opened_at: datetime, plan: dict[str, Any], entry: dict[str, Any],
                 context: dict[str, Any]) -> int:
    cur = con.execute("INSERT INTO live_trades (signal_id, monitored_id, strategy_id, version, account_id, position_id, "
                      "opened_at, status, plan, entry, context_entry) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                      (signal_id, monitored_id, strategy_id, version, account_id, position_id, iso(opened_at), _j(plan),
                       _j(entry), _j(context)))
    return int(cur.lastrowid)


def update_excursions(con: sqlite3.Connection, tid: int, mfe: str, mae: str, mfe_at: str | None,
                      mae_at: str | None) -> None:
    con.execute("UPDATE live_trades SET mfe = ?, mae = ?, mfe_at = ?, mae_at = ? WHERE id = ?",
                (mfe, mae, mfe_at, mae_at, tid))


def append_check(con: sqlite3.Connection, tid: int, check: dict[str, Any], keep: int = 500) -> None:
    row = con.execute("SELECT checks FROM live_trades WHERE id = ?", (tid,)).fetchone()
    checks = _l(row["checks"], []) if row else []
    checks.append(check)
    con.execute("UPDATE live_trades SET checks = ? WHERE id = ?", (_j(checks[-keep:]), tid))


def close_trade(con: sqlite3.Connection, tid: int, exit_info: dict[str, Any], context: dict[str, Any],
                closed_at: str) -> None:
    con.execute("UPDATE live_trades SET status = 'closed', exit = ?, context_exit = ?, closed_at = ? "
                "WHERE id = ? AND status = 'open'", (_j(exit_info), _j(context), closed_at, tid))


_TRADE_SQL = ("SELECT t.*, p.side, p.lots, p.ounces, p.open_price, p.open_time, p.open_spread, p.open_slippage, "
              "p.open_commission, p.stop_loss, p.take_profit, p.status AS position_status, p.close_price, p.close_time, "
              "p.close_reason, p.close_commission, p.realized_gross, p.realized_net, p.margin, p.open_source, "
              "p.close_source, p.close_price_state, s.name AS strategy_name "
              "FROM live_trades t JOIN paper_positions p ON p.id = t.position_id JOIN strategies s ON s.id = t.strategy_id")


def trade(con: sqlite3.Connection, tid: int) -> dict[str, Any] | None:
    row = con.execute(_TRADE_SQL + " WHERE t.id = ?", (tid,)).fetchone()
    return _trade(row) if row else None


def trade_by_signal(con: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = con.execute(_TRADE_SQL + " WHERE t.signal_id = ?", (sid,)).fetchone()
    return _trade(row) if row else None


def trades(con: sqlite3.Connection, *, status: str | None = None, strategy_id: int | None = None,
           version: int | None = None, monitored_id: int | None = None, start: str | None = None,
           end: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    sql, args = _TRADE_SQL + " WHERE 1 = 1", []
    for col, val in (("t.status", status), ("t.strategy_id", strategy_id), ("t.version", version),
                     ("t.monitored_id", monitored_id)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    if start:
        sql += " AND t.opened_at >= ?"
        args.append(start)
    if end:
        sql += " AND t.opened_at <= ?"
        args.append(end)
    sql += " ORDER BY t.opened_at DESC LIMIT ?"
    args.append(limit)
    return [_trade(r) for r in con.execute(sql, args).fetchall()]


def open_trades_with_closed_positions(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(_TRADE_SQL + " WHERE t.status = 'open' AND p.status = 'closed'").fetchall()
    return [_trade(r) for r in rows]


def _trade(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "signal_id", "monitored_id", "strategy_id", "version", "account_id", "position_id",
                             "opened_at", "status", "mfe", "mae", "mfe_at", "mae_at", "closed_at", "side", "lots",
                             "ounces", "open_price", "open_time", "open_spread", "open_slippage", "open_commission",
                             "stop_loss", "take_profit", "position_status", "close_price", "close_time", "close_reason",
                             "close_commission", "realized_gross", "realized_net", "margin", "open_source",
                             "close_source", "close_price_state", "strategy_name")}
    out["plan"] = _l(r["plan"], {})
    out["entry"] = _l(r["entry"], {})
    out["context_entry"] = _l(r["context_entry"], {})
    out["checks"] = _l(r["checks"], [])
    out["exit"] = _l(r["exit"])
    out["context_exit"] = _l(r["context_exit"])
    out["label"] = "SIMULATED paper position (no broker order)"
    return out


# ------------------------------------------------------------------ per-trade evaluations (insert-only)
def insert_trade_evaluation(con: sqlite3.Connection, tid: int, ev: dict[str, Any], now: datetime) -> bool:
    cur = con.execute("INSERT OR IGNORE INTO trade_evaluations (trade_id, definition, created_at, summary, answers, "
                      "tags, went_well, went_wrong) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (tid, ev["definition"], iso(now), ev["summary"], _j(ev["answers"]), _j(ev["tags"]),
                       _j(ev["went_well"]), _j(ev["went_wrong"])))
    return cur.rowcount > 0


def trade_evaluation(con: sqlite3.Connection, tid: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM trade_evaluations WHERE trade_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "trade_id": row["trade_id"], "definition": row["definition"], "created_at": row["created_at"],
            "summary": row["summary"], "answers": _l(row["answers"], []), "tags": _l(row["tags"], []),
            "went_well": _l(row["went_well"], []), "went_wrong": _l(row["went_wrong"], []), "label": SIMULATED}


# ------------------------------------------------------------------ research-assistant log
def insert_assistant_log(con: sqlite3.Connection, *, now: datetime, question: str, mode: str, intent: str | None,
                         answer: str, tools: list[Any], sources: list[str]) -> int:
    cur = con.execute("INSERT INTO assistant_log (asked_at, question, mode, intent, answer, tools, sources) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (iso(now), question, mode, intent, answer, _j(tools), _j(sources)))
    return int(cur.lastrowid)


def assistant_log(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM assistant_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "tools": _l(r["tools"], []), "sources": _l(r["sources"], [])} for r in rows]
