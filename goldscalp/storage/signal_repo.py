"""Signal history. A signal row is written only when the classification CHANGES for a series (and
mode / replay), so the table records transitions rather than one row per request."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def latest(con: sqlite3.Connection, mode: str, series_key: str, replay_id: str | None = None) -> sqlite3.Row | None:
    if replay_id is None:
        return con.execute("SELECT * FROM signals WHERE mode = ? AND series_key = ? AND replay_id IS NULL "
                           "ORDER BY cutoff DESC, generated_at DESC LIMIT 1", (mode, series_key)).fetchone()
    return con.execute("SELECT * FROM signals WHERE replay_id = ? ORDER BY cutoff DESC, generated_at DESC LIMIT 1",
                       (replay_id,)).fetchone()


def record_if_changed(con: sqlite3.Connection, payload: dict[str, Any]) -> bool:
    """Insert the signal if its direction differs from the latest stored one for the same stream.
    Returns True when a row was written."""
    prev = latest(con, payload["mode"], payload["series"], payload.get("replay_id"))
    if prev is not None and (prev["direction"] == payload["direction"] or prev["cutoff"] > payload["cutoff"]):
        return False
    con.execute(
        "INSERT OR IGNORE INTO signals (id, mode, replay_id, series_key, as_of, cutoff, generated_at, direction, "
        "score, definition, config_fingerprint, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (payload["id"], payload["mode"], payload.get("replay_id"), payload["series"], payload["as_of"],
         payload["cutoff"], payload["generated_at"], payload["direction"], int(payload["score"]),
         payload["definition"], payload["config_fingerprint"], json.dumps(payload, default=str)))
    return True


def stored_id_for(con: sqlite3.Connection, payload: dict[str, Any]) -> str | None:
    """The stored row that represents the current classification (the latest stored signal of the stream,
    if it has the same direction), so the paper ticket can reference a persisted signal."""
    prev = latest(con, payload["mode"], payload["series"], payload.get("replay_id"))
    return prev["id"] if prev is not None and prev["direction"] == payload["direction"] else None


def insert_evaluated(con: sqlite3.Connection, payload: dict[str, Any], evaluation: dict[str, Any]) -> None:
    """Store one signal found by a performance run, with its outcome (no change detection: the run already
    reduced its steps to changes of classification)."""
    con.execute(
        "INSERT OR IGNORE INTO signals (id, mode, replay_id, series_key, as_of, cutoff, generated_at, direction, "
        "score, definition, config_fingerprint, payload, evaluation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (payload["id"], payload["mode"], payload.get("replay_id"), payload["series"], payload["as_of"],
         payload["cutoff"], payload["generated_at"], payload["direction"], int(payload["score"]),
         payload["definition"], payload["config_fingerprint"], json.dumps(payload, default=str),
         json.dumps(evaluation, default=str)))


def get(con: sqlite3.Connection, signal_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return _row(row) if row else None


def history(con: sqlite3.Connection, *, mode: str | None = None, series_key: str | None = None,
            replay_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM signals WHERE 1 = 1", []
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    if series_key:
        sql += " AND series_key = ?"
        args.append(series_key)
    if replay_id:
        sql += " AND replay_id = ?"
        args.append(replay_id)
    sql += " ORDER BY cutoff DESC, generated_at DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in con.execute(sql, args).fetchall()]


def set_evaluation(con: sqlite3.Connection, signal_id: str, evaluation: dict[str, Any]) -> None:
    con.execute("UPDATE signals SET evaluation = ? WHERE id = ?", (json.dumps(evaluation, default=str), signal_id))


def delete_replay(con: sqlite3.Connection, replay_id: str) -> int:
    """Replay signals are working data of that replay; positions that reference one keep the id."""
    used = {r[0] for r in con.execute("SELECT DISTINCT signal_id FROM paper_positions WHERE signal_id IS NOT NULL")}
    rows = [r[0] for r in con.execute("SELECT id FROM signals WHERE replay_id = ?", (replay_id,))]
    doomed = [r for r in rows if r not in used]
    con.executemany("DELETE FROM signals WHERE id = ?", [(r,) for r in doomed])
    return len(doomed)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(r["payload"])
    payload["evaluation"] = json.loads(r["evaluation"]) if r["evaluation"] else None
    payload["stored"] = True
    return payload
