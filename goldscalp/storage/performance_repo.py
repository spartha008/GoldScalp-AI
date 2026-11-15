"""Performance-evaluation runs and stored analysis configurations."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso


def save_config(con: sqlite3.Connection, fingerprint: str, data: dict[str, Any], now: datetime) -> None:
    con.execute("INSERT OR IGNORE INTO analysis_configs (fingerprint, data, first_seen) VALUES (?, ?, ?)",
                (fingerprint, json.dumps(data, sort_keys=True), iso(now)))


def get_config(con: sqlite3.Connection, fingerprint: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM analysis_configs WHERE fingerprint = ?", (fingerprint,)).fetchone()
    return None if row is None else {"fingerprint": row["fingerprint"], "first_seen": row["first_seen"],
                                     "config": json.loads(row["data"])}


def create_run(con: sqlite3.Connection, run_id: str, *, created_at: datetime, series_key: str,
               params: dict[str, Any], total: int, fingerprint: str) -> None:
    con.execute("INSERT INTO performance_runs (id, created_at, series_key, params, status, progress, total, "
                "config_fingerprint) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)",
                (run_id, iso(created_at), series_key, json.dumps(params, default=str), total, fingerprint))


def set_status(con: sqlite3.Connection, run_id: str, status: str, *, at: datetime | None = None,
               error: str | None = None, result: dict[str, Any] | None = None) -> None:
    if status == "running":
        con.execute("UPDATE performance_runs SET status = ?, started_at = ? WHERE id = ?", (status, iso(at), run_id))
    else:
        con.execute("UPDATE performance_runs SET status = ?, finished_at = ?, error = ?, result = ? WHERE id = ?",
                    (status, iso(at), error, json.dumps(result, default=str) if result is not None else None, run_id))


def set_progress(con: sqlite3.Connection, run_id: str, done: int) -> None:
    con.execute("UPDATE performance_runs SET progress = ? WHERE id = ?", (done, run_id))


def mark_interrupted(con: sqlite3.Connection, now: datetime) -> int:
    """Runs left queued/running by a previous server process can never finish."""
    cur = con.execute("UPDATE performance_runs SET status = 'failed', finished_at = ?, error = ? "
                      "WHERE status IN ('queued', 'running')", (iso(now), "the server stopped during this run"))
    return cur.rowcount


def get_run(con: sqlite3.Connection, run_id: str, *, with_result: bool = True) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM performance_runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _row(row, with_result)


def list_runs(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM performance_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r, False) for r in rows]


def delete_run(con: sqlite3.Connection, run_id: str) -> tuple[bool, int]:
    """Delete a run and the signals it stored (signals linked to a paper position are kept)."""
    used = {r[0] for r in con.execute("SELECT DISTINCT signal_id FROM paper_positions WHERE signal_id IS NOT NULL")}
    ids = [r[0] for r in con.execute("SELECT id FROM signals WHERE replay_id LIKE ?", (f"{run_id}:%",))]
    doomed = [i for i in ids if i not in used]
    con.executemany("DELETE FROM signals WHERE id = ?", [(i,) for i in doomed])
    cur = con.execute("DELETE FROM performance_runs WHERE id = ?", (run_id,))
    return cur.rowcount > 0, len(doomed)


def _row(r: sqlite3.Row, with_result: bool) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "created_at", "series_key", "status", "progress", "total", "config_fingerprint",
                             "started_at", "finished_at", "error")}
    out["params"] = json.loads(r["params"])
    if with_result:
        out["result"] = json.loads(r["result"]) if r["result"] else None
    return out
