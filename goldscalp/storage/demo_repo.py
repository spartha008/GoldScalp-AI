"""Storage for the optional MT5 DEMO-account execution (migration 0007): links and mirrored demo trades."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso


def _j(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


# ------------------------------------------------------------------------------------------------ links
def links(con: sqlite3.Connection, *, enabled: bool | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM demo_links", []
    if enabled is not None:
        sql += " WHERE enabled = ?"
        args.append(1 if enabled else 0)
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in con.execute(sql + " ORDER BY monitored_id", args)]


def link(con: sqlite3.Connection, monitored_id: int) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM demo_links WHERE monitored_id = ?", (monitored_id,)).fetchone()
    return None if r is None else dict(r) | {"enabled": bool(r["enabled"])}


def enable_link(con: sqlite3.Connection, monitored_id: int, *, server: str, account_hash: str, now: datetime) -> None:
    con.execute("INSERT INTO demo_links (monitored_id, enabled, enabled_at, disabled_at, disabled_reason, server, "
                "account_hash, updated_at) VALUES (?, 1, ?, NULL, NULL, ?, ?, ?) "
                "ON CONFLICT(monitored_id) DO UPDATE SET enabled = 1, enabled_at = excluded.enabled_at, "
                "disabled_at = NULL, disabled_reason = NULL, server = excluded.server, "
                "account_hash = excluded.account_hash, updated_at = excluded.updated_at",
                (monitored_id, iso(now), server, account_hash, iso(now)))


def disable_links(con: sqlite3.Connection, now: datetime, reason: str, monitored_id: int | None = None) -> int:
    sql, args = "UPDATE demo_links SET enabled = 0, disabled_at = ?, disabled_reason = ?, updated_at = ? WHERE enabled = 1", \
        [iso(now), reason, iso(now)]
    if monitored_id is not None:
        sql += " AND monitored_id = ?"
        args.append(monitored_id)
    return con.execute(sql, args).rowcount


# ------------------------------------------------------------------------------------------------ demo trades
def _trade(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["events"] = json.loads(r["events"] or "[]")
    out["label"] = "MT5 DEMO account order (virtual funds)"
    return out


def insert_trade(con: sqlite3.Connection, *, live_trade_id: int, monitored_id: int, status: str, side: str,
                 symbol: str | None, volume: str, now: datetime, message: str | None = None, server: str | None = None,
                 requested_price: str | None = None, sl: str | None = None, tp: str | None = None,
                 events: list[dict[str, Any]] | None = None) -> int:
    cur = con.execute(
        "INSERT INTO demo_trades (live_trade_id, monitored_id, status, side, symbol, volume, requested_price, sl, tp, "
        "message, server, events, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (live_trade_id, monitored_id, status, side, symbol, volume, requested_price, sl, tp, message, server,
         _j(events or []), iso(now), iso(now)))
    return int(cur.lastrowid)


def update_trade(con: sqlite3.Connection, tid: int, now: datetime, *, event: dict[str, Any] | None = None,
                 **fields: Any) -> None:
    allowed = {"status", "symbol", "requested_price", "sl", "tp", "open_price", "opened_at", "position_ticket",
               "close_price", "closed_at", "close_reason", "profit", "commission", "swap", "message", "server"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown demo trade fields {sorted(bad)}")
    sets = [f"{k} = ?" for k in fields] + ["updated_at = ?"]
    args = list(fields.values()) + [iso(now)]
    if event is not None:
        sets.append("events = json_insert(events, '$[#]', json(?))")
        args.append(_j(event))
    con.execute(f"UPDATE demo_trades SET {', '.join(sets)} WHERE id = ?", (*args, tid))


def trade(con: sqlite3.Connection, tid: int) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM demo_trades WHERE id = ?", (tid,)).fetchone()
    return None if r is None else _trade(r)


def trade_for_live(con: sqlite3.Connection, live_trade_id: int) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM demo_trades WHERE live_trade_id = ?", (live_trade_id,)).fetchone()
    return None if r is None else _trade(r)


def trades(con: sqlite3.Connection, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM demo_trades", []
    if status is not None:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_trade(r) for r in con.execute(sql, args)]


def mirrored_live_ids(con: sqlite3.Connection, live_trade_ids: list[int]) -> set[int]:
    if not live_trade_ids:
        return set()
    marks = ",".join("?" * len(live_trade_ids))
    return {r[0] for r in con.execute(f"SELECT live_trade_id FROM demo_trades WHERE live_trade_id IN ({marks})",
                                      live_trade_ids)}
