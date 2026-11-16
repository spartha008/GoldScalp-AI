"""Storage for manual MT5 DEMO-account orders from the Dashboard order ticket (migration 0008)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso

FIELDS = {"status", "side", "symbol", "volume", "sl", "tp", "position_ticket", "price", "risk_usd", "reward_usd",
          "server", "message", "request", "reply"}


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"), default=str)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    for key in ("request", "reply"):
        out[key] = json.loads(r[key]) if r[key] else None
    out["label"] = "Manual order on your MT5 DEMO account (virtual funds)"
    return out


def insert(con: sqlite3.Connection, now: datetime, *, action: str, status: str, **fields: Any) -> int:
    bad = set(fields) - FIELDS
    if bad:
        raise ValueError(f"unknown manual order fields {sorted(bad)}")
    values = {k: (_j(v) if k in ("request", "reply") else v) for k, v in fields.items()}
    cols = ["action", "status", *values, "created_at", "updated_at"]
    cur = con.execute(f"INSERT INTO manual_orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      (action, status, *values.values(), iso(now), iso(now)))
    return int(cur.lastrowid)


def update(con: sqlite3.Connection, oid: int, now: datetime, **fields: Any) -> None:
    bad = set(fields) - FIELDS
    if bad:
        raise ValueError(f"unknown manual order fields {sorted(bad)}")
    values = {k: (_j(v) if k in ("request", "reply") else v) for k, v in fields.items()}
    sets = [f"{k} = ?" for k in values] + ["updated_at = ?"]
    con.execute(f"UPDATE manual_orders SET {', '.join(sets)} WHERE id = ?", (*values.values(), iso(now), oid))


def get(con: sqlite3.Connection, oid: int) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM manual_orders WHERE id = ?", (oid,)).fetchone()
    return None if r is None else _row(r)


def recent(con: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM manual_orders ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [_row(r) for r in rows]
