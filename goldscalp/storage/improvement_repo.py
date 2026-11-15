"""Improvement studies and proposals. A proposal changes no strategy: approval (a user action in the API) saves
the candidate as a NEW strategy version and records which version it became."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso

STATUS_BY_VERDICT = {"improved": "validated", "insufficient_sample": "inconclusive", "not_improved": "not_improved"}


def _j(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def create_study(con: sqlite3.Connection, sid: str, *, strategy_id: int, version: int, now: datetime,
                 params: dict[str, Any]) -> None:
    con.execute("INSERT INTO research_studies (id, strategy_id, version, created_at, status, params) "
                "VALUES (?, ?, ?, ?, 'queued', ?)", (sid, strategy_id, version, iso(now), _j(params)))


def set_study(con: sqlite3.Connection, sid: str, status: str, *, at: datetime, result: dict[str, Any] | None = None,
              error: str | None = None) -> None:
    if status == "running":
        con.execute("UPDATE research_studies SET status = ?, started_at = ? WHERE id = ?", (status, iso(at), sid))
    else:
        con.execute("UPDATE research_studies SET status = ?, finished_at = ?, result = ?, error = ? WHERE id = ?",
                    (status, iso(at), None if result is None else _j(result), error, sid))


def set_progress(con: sqlite3.Connection, sid: str, done: int, total: int) -> None:
    con.execute("UPDATE research_studies SET progress = ?, total = ? WHERE id = ?", (done, total, sid))


def mark_interrupted(con: sqlite3.Connection, now: datetime) -> int:
    return con.execute("UPDATE research_studies SET status = 'failed', finished_at = ?, error = ? "
                       "WHERE status IN ('queued', 'running')", (iso(now), "the server stopped during this study")).rowcount


def study(con: sqlite3.Connection, sid: str, *, with_result: bool = True) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM research_studies WHERE id = ?", (sid,)).fetchone()
    return None if row is None else _study(row, with_result)


def studies(con: sqlite3.Connection, *, strategy_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM research_studies", []
    if strategy_id is not None:
        sql += " WHERE strategy_id = ?"
        args.append(strategy_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_study(r, False) for r in con.execute(sql, args).fetchall()]


def _study(r: sqlite3.Row, with_result: bool) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "strategy_id", "version", "created_at", "status", "progress", "total", "started_at",
                             "finished_at", "error")}
    out["params"] = json.loads(r["params"])
    if with_result:
        out["result"] = json.loads(r["result"]) if r["result"] else None
    return out


def overlapping_holdouts(con: sqlite3.Connection, strategy_id: int, start: str, end: str, exclude: str) -> int:
    n = 0
    for r in con.execute("SELECT id, result FROM research_studies WHERE strategy_id = ? AND status = 'complete' "
                         "AND id != ?", (strategy_id, exclude)).fetchall():
        h = (json.loads(r["result"]).get("windows") or {}).get("holdout") if r["result"] else None
        if h and h["start"] < end and start < h["end"]:
            n += 1
    return n


def insert_proposal(con: sqlite3.Connection, pid: str, *, study_id: str, strategy_id: int, base_version: int,
                    base_fingerprint: str, p: dict[str, Any], candidate_fingerprint: str, limitations: list[str],
                    now: datetime) -> None:
    verdict = p["validation"]["verdict"]
    con.execute("INSERT INTO strategy_proposals (id, study_id, strategy_id, base_version, base_fingerprint, kind, title, "
                "candidate, candidate_fingerprint, diff, explanation, evidence, limitations, validation, verdict, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, study_id, strategy_id, base_version, base_fingerprint, p["kind"], p["title"], _j(p["candidate"]),
                 candidate_fingerprint, p["diff"], p["explanation"], _j(p["evidence"]), _j(limitations),
                 _j(p["validation"]), verdict, STATUS_BY_VERDICT[verdict], iso(now)))


def proposal(con: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT p.*, s.name AS strategy_name FROM strategy_proposals p JOIN strategies s "
                      "ON s.id = p.strategy_id WHERE p.id = ?", (pid,)).fetchone()
    return None if row is None else _proposal(row)


def proposals(con: sqlite3.Connection, *, strategy_id: int | None = None, status: str | None = None,
              study_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = ("SELECT p.*, s.name AS strategy_name FROM strategy_proposals p JOIN strategies s ON s.id = p.strategy_id "
                 "WHERE 1 = 1"), []
    for col, val in (("p.strategy_id", strategy_id), ("p.status", status), ("p.study_id", study_id)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    args.append(limit)
    return [_proposal(r) for r in con.execute(sql, args).fetchall()]


def decide(con: sqlite3.Connection, pid: str, status: str, *, at: datetime, note: str | None,
           resulting_version: int | None = None) -> bool:
    return con.execute("UPDATE strategy_proposals SET status = ?, decided_at = ?, decision_note = ?, "
                       "resulting_version = ? WHERE id = ? AND status IN ('validated', 'inconclusive', 'not_improved')",
                       (status, iso(at), note, resulting_version, pid)).rowcount > 0


def _proposal(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "study_id", "strategy_id", "base_version", "base_fingerprint", "kind", "title",
                             "candidate_fingerprint", "diff", "explanation", "verdict", "status", "created_at",
                             "decided_at", "decision_note", "resulting_version", "strategy_name")}
    for k in ("candidate", "evidence", "limitations", "validation"):
        out[k] = json.loads(r[k]) if r[k] else None
    return out
