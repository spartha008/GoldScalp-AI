"""Research runs (backtests, comparisons, walk-forward): parameters, status, progress and results."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso


def create(con: sqlite3.Connection, run_id: str, *, kind: str, name: str | None, created_at: datetime,
           series_key: str, params: dict[str, Any], total: int, fingerprint: str) -> None:
    con.execute("INSERT INTO research_runs (id, kind, name, created_at, series_key, params, status, progress, total, "
                "config_fingerprint) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                (run_id, kind, name, iso(created_at), series_key, json.dumps(params, default=str), total, fingerprint))


def set_status(con: sqlite3.Connection, run_id: str, status: str, *, at: datetime, error: str | None = None,
               result: dict[str, Any] | None = None) -> None:
    if status == "running":
        con.execute("UPDATE research_runs SET status = ?, started_at = ? WHERE id = ?", (status, iso(at), run_id))
    else:
        con.execute("UPDATE research_runs SET status = ?, finished_at = ?, error = ?, result = ? WHERE id = ?",
                    (status, iso(at), error, json.dumps(result, default=str) if result is not None else None, run_id))


def set_progress(con: sqlite3.Connection, run_id: str, done: int, total: int | None = None) -> None:
    if total is None:
        con.execute("UPDATE research_runs SET progress = ? WHERE id = ?", (done, run_id))
    else:
        con.execute("UPDATE research_runs SET progress = ?, total = ? WHERE id = ?", (done, total, run_id))


def update_meta(con: sqlite3.Connection, run_id: str, *, name: str | None, saved: bool | None) -> bool:
    row = con.execute("SELECT name, saved FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return False
    con.execute("UPDATE research_runs SET name = ?, saved = ? WHERE id = ?",
                (row["name"] if name is None else name, row["saved"] if saved is None else int(saved), run_id))
    return True


def mark_interrupted(con: sqlite3.Connection, now: datetime) -> int:
    cur = con.execute("UPDATE research_runs SET status = 'failed', finished_at = ?, error = ? "
                      "WHERE status IN ('queued', 'running')", (iso(now), "the server stopped during this run"))
    return cur.rowcount


def get(con: sqlite3.Connection, run_id: str, *, with_result: bool = True) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _row(row, with_result)


def list_runs(con: sqlite3.Connection, *, kind: str | None = None, saved: bool | None = None,
              limit: int = 100) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM research_runs WHERE 1 = 1", []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if saved is not None:
        sql += " AND saved = ?"
        args.append(int(saved))
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_row(r, False) for r in con.execute(sql, args).fetchall()]


def delete(con: sqlite3.Connection, run_id: str) -> bool:
    return con.execute("DELETE FROM research_runs WHERE id = ?", (run_id,)).rowcount > 0


def _row(r: sqlite3.Row, with_result: bool) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "kind", "name", "created_at", "series_key", "status", "progress", "total",
                             "config_fingerprint", "started_at", "finished_at", "error")}
    out["saved"] = bool(r["saved"])
    out["params"] = json.loads(r["params"])
    if with_result:
        out["result"] = json.loads(r["result"]) if r["result"] else None
    return out
