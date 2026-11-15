"""SQLite access: WAL, foreign keys, numbered migrations, explicit transactions.

Transaction handling adapted from SpicyStock src/morning_service.py
Store._transaction() @ 96ef4732: isolation_level=None, BEGIN IMMEDIATE, roll
back on ANY BaseException, always close. Read-only snapshots open a
``file:...?mode=ro`` URI. PRAGMAs and migrations are new.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Iterator
from urllib.parse import quote

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
#: First line of a migration that rebuilds a referenced table (run with foreign keys off, checked before commit).
FK_OFF_DIRECTIVE = "-- goldscalp: foreign_keys=off"


class Database:
    def __init__(self, path: str | Path, migrations_dir: Path | None = None) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{quote(self.path.as_posix())}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None, check_same_thread=False)
        else:
            con = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self._connect(readonly=True)
        try:
            con.execute("BEGIN")
            yield con
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()

    def migrate(self) -> list[int]:
        """Apply pending migrations in order. Returns the versions applied."""
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                        "name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            done = {row[0] for row in con.execute("SELECT version FROM schema_migrations")}
        finally:
            con.close()
        applied = []
        for path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(path.name[:4])
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            if sql.lstrip().startswith(FK_OFF_DIRECTIVE):
                self._migrate_without_foreign_keys(version, path.stem, sql)
            else:
                with self.transaction() as tx:
                    for statement in _statements(sql):
                        tx.execute(statement)
                    tx.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                               (version, path.stem, datetime.now(UTC).isoformat()))
            applied.append(version)
        return applied

    def _migrate_without_foreign_keys(self, version: int, name: str, sql: str) -> None:
        """SQLite's documented table-rebuild procedure: foreign keys OFF (outside the transaction), apply,
        then PRAGMA foreign_key_check must report nothing before COMMIT; otherwise everything rolls back."""
        con = self._connect()
        try:
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("BEGIN IMMEDIATE")
            try:
                for statement in _statements(sql):
                    con.execute(statement)
                problems = con.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise sqlite3.IntegrityError(f"migration {name} breaks foreign keys: {[tuple(p) for p in problems[:5]]}")
                con.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                            (version, name, datetime.now(UTC).isoformat()))
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def schema_version(self) -> int:
        with self.read() as con:
            row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)


def _statements(sql: str) -> list[str]:
    """Split a migration file on ';' line endings (migrations contain no triggers)."""
    parts, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";"):
            parts.append("\n".join(buf))
            buf = []
    if buf:
        parts.append("\n".join(buf))
    return parts
