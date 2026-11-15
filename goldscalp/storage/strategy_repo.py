"""Versioned strategy configurations. A version is immutable; saving a changed configuration adds one.
Deleting archives the strategy (its versions stay, because runs and journal entries refer to them)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso
from goldscalp.strategies.builder import fingerprint


def create(con: sqlite3.Connection, config: dict[str, Any], now: datetime, note: str | None = None) -> int:
    cur = con.execute("INSERT INTO strategies (name, created_at, updated_at, current_version) VALUES (?, ?, ?, 1)",
                      (config["name"], iso(now), iso(now)))
    sid = int(cur.lastrowid)
    con.execute("INSERT INTO strategy_versions (strategy_id, version, config, fingerprint, created_at, note) "
                "VALUES (?, 1, ?, ?, ?, ?)", (sid, json.dumps(config, sort_keys=True), fingerprint(config), iso(now), note))
    return sid


def save(con: sqlite3.Connection, sid: int, config: dict[str, Any], now: datetime, note: str | None = None) -> tuple[int, bool]:
    """Returns (current version, created?). An identical configuration creates no version."""
    row = con.execute("SELECT current_version FROM strategies WHERE id = ? AND archived = 0", (sid,)).fetchone()
    if row is None:
        raise KeyError(sid)
    current = int(row["current_version"])
    old = con.execute("SELECT config FROM strategy_versions WHERE strategy_id = ? AND version = ?", (sid, current)).fetchone()
    if old is not None and json.loads(old["config"]) == config:
        return current, False
    version = current + 1
    con.execute("INSERT INTO strategy_versions (strategy_id, version, config, fingerprint, created_at, note) "
                "VALUES (?, ?, ?, ?, ?, ?)", (sid, version, json.dumps(config, sort_keys=True), fingerprint(config),
                                              iso(now), note))
    con.execute("UPDATE strategies SET name = ?, updated_at = ?, current_version = ? WHERE id = ?",
                (config["name"], iso(now), version, sid))
    return version, True


def get(con: sqlite3.Connection, sid: int, version: int | None = None) -> dict[str, Any] | None:
    s = con.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
    if s is None:
        return None
    v = con.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? AND version = ?",
                    (sid, version or s["current_version"])).fetchone()
    if v is None:
        return None
    return {"id": s["id"], "name": s["name"], "created_at": s["created_at"], "updated_at": s["updated_at"],
            "current_version": s["current_version"], "archived": bool(s["archived"]), "version": v["version"],
            "fingerprint": v["fingerprint"], "version_created_at": v["created_at"], "note": v["note"],
            "config": json.loads(v["config"])}


def versions(con: sqlite3.Connection, sid: int) -> list[dict[str, Any]]:
    rows = con.execute("SELECT version, fingerprint, created_at, note FROM strategy_versions WHERE strategy_id = ? "
                       "ORDER BY version DESC", (sid,)).fetchall()
    return [dict(r) for r in rows]


def list_all(con: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM strategies" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY updated_at DESC"
    out = []
    for s in con.execute(sql).fetchall():
        item = get(con, s["id"])
        if item is not None:
            out.append(item)
    return out


def archive(con: sqlite3.Connection, sid: int, now: datetime) -> bool:
    cur = con.execute("UPDATE strategies SET archived = 1, updated_at = ? WHERE id = ? AND archived = 0", (iso(now), sid))
    return cur.rowcount > 0
