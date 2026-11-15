"""The ISOLATED monthly research store and the READ-ONLY view of the application database.

PHASE5_ARCHITECTURE.md sections 1 and 3. Phase 5 never writes to the application database
(``var/goldscalp.sqlite3``): it is wrapped in ``ReadOnlySource``, which exposes only ``read()`` (``mode=ro``
connections). Every Phase 5 write goes to the separate file ``var/monthly/monthly.sqlite3`` with its own
migrations, so nothing here can migrate, lock or alter the database the live monitors use."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.core.clock import iso
from goldscalp.storage.db import Database

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class ReadOnlySource:
    """The application database, read-only. There is deliberately no transaction() or migrate()."""

    def __init__(self, path: str | Path) -> None:
        self._db = Database(path)
        self.path = self._db.path

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._db.read() as con:
            yield con


def open_store(path: str | Path) -> Database:
    db = Database(path, migrations_dir=MIGRATIONS)
    db.migrate()
    return db


def _j(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _l(text: str | None, default: Any = None) -> Any:
    return default if text is None else json.loads(text)


# ------------------------------------------------------------------ evaluations
def create_evaluation(con: sqlite3.Connection, eid: str, *, month: str, start: datetime, end: datetime,
                      params: dict[str, Any], availability: dict[str, Any], app_version: str, now: datetime) -> None:
    con.execute("INSERT INTO evaluations (id, month, range_start, range_end, status, created_at, updated_at, params, "
                "availability, app_version) VALUES (?, ?, ?, ?, 'NOT STARTED', ?, ?, ?, ?, ?)",
                (eid, month, iso(start), iso(end), iso(now), iso(now), _j(params), _j(availability), app_version))


EVAL_FIELDS = {"status", "research_done", "frozen", "research", "phase", "progress", "total", "error", "finished_at",
               "availability"}


def update_evaluation(con: sqlite3.Connection, eid: str, now: datetime, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(now)]
    for key, value in fields.items():
        if key not in EVAL_FIELDS:
            raise ValueError(key)
        sets.append(f"{key} = ?")
        args.append(_j(value) if key in ("frozen", "research", "availability") else value)
    con.execute(f"UPDATE evaluations SET {', '.join(sets)} WHERE id = ?", (*args, eid))


def evaluation(con: sqlite3.Connection, eid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM evaluations WHERE id = ?", (eid,)).fetchone()
    return None if row is None else _evaluation(row)


def evaluations(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [_evaluation(r) for r in con.execute("SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?", (limit,))]


def mark_interrupted(con: sqlite3.Connection, now: datetime) -> int:
    n = con.execute("UPDATE evaluations SET status = 'FAILED', error = ?, finished_at = ? WHERE status IN "
                    "('RESEARCHING', 'RUNNING')", ("the research service stopped during this job", iso(now))).rowcount
    for table in ("risk_studies", "robustness_studies", "validation_studies", "discovery_trials", "sealed_runs"):
        con.execute(f"UPDATE {table} SET status = 'FAILED', error = ?, finished_at = ? WHERE status = 'RUNNING'",
                    ("the research service stopped during this job", iso(now)))
    return n


# ------------------------------------------------------------------ Phase 5.1 risk studies and import labels
STUDY_FIELDS = {"status", "phase", "progress", "total", "error", "finished_at", "params"}


def create_study(con: sqlite3.Connection, sid: str, *, baseline: str, month: str, evaluation_ids: list[str],
                 params: dict[str, Any], app_version: str, now: datetime) -> None:
    con.execute("INSERT INTO risk_studies (id, baseline_evaluation_id, month, evaluation_ids, params, status, app_version, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'READY', ?, ?, ?)",
                (sid, baseline, month, _j(evaluation_ids), _j(params), app_version, iso(now), iso(now)))


def update_study(con: sqlite3.Connection, sid: str, now: datetime, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(now)]
    for key, value in fields.items():
        if key not in STUDY_FIELDS:
            raise ValueError(key)
        sets.append(f"{key} = ?")
        args.append(_j(value) if key == "params" else value)
    con.execute(f"UPDATE risk_studies SET {', '.join(sets)} WHERE id = ?", (*args, sid))


def _study(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "baseline_evaluation_id", "month", "status", "phase", "progress", "total",
                             "app_version", "error", "created_at", "updated_at", "finished_at")}
    out["evaluation_ids"] = _l(r["evaluation_ids"], [])
    out["params"] = _l(r["params"], {})
    return out


def study(con: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM risk_studies WHERE id = ?", (sid,)).fetchone()
    return None if row is None else _study(row)


def studies(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [_study(r) for r in con.execute("SELECT * FROM risk_studies ORDER BY created_at DESC LIMIT ?", (limit,))]


def set_import_label(con: sqlite3.Connection, import_id: int, label: str, instrument: str, now: datetime) -> None:
    con.execute("INSERT INTO import_labels (import_id, label, instrument, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (import_id) DO NOTHING", (import_id, label, instrument, iso(now)))


def imports(con: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = con.execute("SELECT i.id, s.key AS series, i.tf, i.dialect, i.file_name, i.content_sha256, i.time_basis, "
                       "i.rows_read, i.rows_stored, i.rows_rejected, i.duplicates_dropped, i.first_ts, i.last_ts, "
                       "i.imported_at, l.label, l.instrument FROM data_imports i JOIN series s ON s.id = i.series_id "
                       "LEFT JOIN import_labels l ON l.import_id = i.id ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("first_ts", "last_ts"):
            d[k.replace("_ts", "")] = iso(datetime.fromtimestamp(d.pop(k), UTC)) if d.get(k) is not None else None
        d["content_sha256"] = d["content_sha256"][:16]
        out.append(d)
    return out


def _evaluation(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "month", "range_start", "range_end", "status", "created_at", "updated_at", "phase",
                             "progress", "total", "app_version", "error", "finished_at")}
    out["research_done"] = bool(r["research_done"])
    for k in ("params", "availability", "frozen", "research"):
        out[k] = _l(r[k])
    return out


# ------------------------------------------------------------------ research candidates (frozen)
def insert_candidate(con: sqlite3.Connection, cid: str, eid: str, c: dict[str, Any], now: datetime) -> None:
    con.execute("INSERT INTO candidates (id, evaluation_id, key, name, version, concept, config, fingerprint, "
                "documentation, research, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, eid, c["key"], c["name"], c["version"], c["concept"], _j(c["config"]), c["fingerprint"],
                 _j(c["documentation"]), _j(c["research"]), iso(now)))


def candidates(con: sqlite3.Connection, eid: str) -> list[dict[str, Any]]:
    out = []
    for r in con.execute("SELECT * FROM candidates WHERE evaluation_id = ? ORDER BY key", (eid,)):
        out.append({k: r[k] for k in ("id", "key", "name", "version", "concept", "fingerprint", "frozen_at")}
                   | {"config": _l(r["config"]), "documentation": _l(r["documentation"]), "research": _l(r["research"]),
                      "label": "RESEARCH CANDIDATE (not a saved strategy; never replaces A, B or C)"})
    return out


# ------------------------------------------------------------------ runs
def upsert_run(con: sqlite3.Connection, run: dict[str, Any]) -> None:
    con.execute("INSERT INTO runs (id, evaluation_id, strategy_key, kind, timeframe, status, config, fingerprint, "
                "assumptions, dataset, result, result_hash, reason, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(evaluation_id, strategy_key, timeframe) "
                "DO UPDATE SET status = excluded.status, dataset = excluded.dataset, result = excluded.result, "
                "result_hash = excluded.result_hash, reason = excluded.reason, started_at = excluded.started_at, "
                "finished_at = excluded.finished_at",
                (run["id"], run["evaluation_id"], run["strategy_key"], run["kind"], run["timeframe"], run["status"],
                 _j(run["config"]), run["fingerprint"], _j(run["assumptions"]),
                 None if run.get("dataset") is None else _j(run["dataset"]),
                 None if run.get("result") is None else _j(run["result"]), run.get("result_hash"), run.get("reason"),
                 run.get("started_at"), run.get("finished_at")))


def run(con: sqlite3.Connection, rid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    return None if row is None else _run(row, True)


def runs(con: sqlite3.Connection, eid: str, *, with_result: bool = False) -> list[dict[str, Any]]:
    return [_run(r, with_result) for r in con.execute("SELECT * FROM runs WHERE evaluation_id = ? ORDER BY kind DESC, "
                                                      "strategy_key, timeframe", (eid,))]


def _run(r: sqlite3.Row, with_result: bool) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "evaluation_id", "strategy_key", "kind", "timeframe", "status", "fingerprint",
                             "result_hash", "reason", "started_at", "finished_at")}
    out["config"] = _l(r["config"])
    out["assumptions"] = _l(r["assumptions"])
    out["dataset"] = _l(r["dataset"])
    if with_result:
        out["result"] = _l(r["result"])
    else:
        res = _l(r["result"]) or {}
        out["summary"] = res.get("summary")
    return out
