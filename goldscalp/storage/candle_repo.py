"""SQL for series, imports and candles. Prices are integers scaled by 10^price_decimals."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.normalization import CANDLE_COLUMNS, empty_candles


@dataclass(frozen=True)
class SeriesInfo:
    key: str
    symbol: str
    kind: str              # 'live' | 'historical'
    source_id: str
    source_symbol: str
    candle_side: str       # 'bid' | 'ask' | 'mid' | 'last' | 'unknown'
    price_decimals: int
    description: str


class SeriesConflict(ValueError):
    """An existing series key with different immutable properties."""


def ensure_series(con: sqlite3.Connection, info: SeriesInfo, now: datetime) -> int:
    row = con.execute("SELECT * FROM series WHERE key = ?", (info.key,)).fetchone()
    if row:
        for field in ("symbol", "kind", "source_id", "candle_side", "price_decimals"):
            if row[field] != getattr(info, field):
                raise SeriesConflict(f"series {info.key!r} already exists with {field}={row[field]!r}; "
                                     f"refusing to mix data with {field}={getattr(info, field)!r}")
        return int(row["id"])
    cur = con.execute(
        "INSERT INTO series (key, symbol, kind, source_id, source_symbol, candle_side, price_decimals, description, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (info.key, info.symbol, info.kind, info.source_id, info.source_symbol, info.candle_side,
         info.price_decimals, info.description, iso(now)))
    return int(cur.lastrowid)


def series_row(con: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM series WHERE key = ?", (key,)).fetchone()


def list_series(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for row in con.execute("SELECT * FROM series ORDER BY kind, key").fetchall():
        stats = con.execute("SELECT tf, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM candles "
                            "WHERE series_id = ? GROUP BY tf", (row["id"],)).fetchall()
        item = dict(row)
        item["timeframes"] = {s["tf"]: {"bars": s["n"], "first": iso(_dt(s["first_ts"])), "last": iso(_dt(s["last_ts"]))}
                              for s in stats}
        out.append(item)
    return out


def upsert_candles(con: sqlite3.Connection, series_id: int, price_decimals: int, tf: Timeframe,
                   frame: pd.DataFrame, origin: str, import_id: int | None, now: datetime) -> int:
    """Insert or replace candles (a forming bar is updated in place until it closes)."""
    if frame.empty:
        return 0
    scale = 10 ** price_decimals
    epochs = frame.index.as_unit("s").asi8
    prices = np.rint(frame[["open", "high", "low", "close"]].to_numpy(dtype="float64") * scale).astype("int64")
    spread = frame["spread"].to_numpy(dtype="float64")
    vol = frame["tick_volume"]
    rows = []
    stamp = iso(now)
    for i in range(len(frame)):
        sp = None if not np.isfinite(spread[i]) else int(round(spread[i] * scale))
        v = None if pd.isna(vol.iloc[i]) else int(vol.iloc[i])
        o, h, low, c = (int(x) for x in prices[i])
        rows.append((series_id, tf.value, int(epochs[i]), o, h, low, c, v, sp, int(bool(frame["closed"].iloc[i])),
                     origin, import_id, stamp))
    con.executemany(
        "INSERT INTO candles (series_id, tf, ts, o, h, l, c, tick_volume, spread, closed, origin, import_id, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (series_id, tf, ts) DO UPDATE SET "
        "o = excluded.o, h = excluded.h, l = excluded.l, c = excluded.c, tick_volume = excluded.tick_volume, "
        "spread = excluded.spread, closed = excluded.closed, origin = excluded.origin, "
        "import_id = excluded.import_id, updated_at = excluded.updated_at", rows)
    return len(rows)


def load_candles(con: sqlite3.Connection, series_id: int, price_decimals: int, tf: Timeframe, *,
                 start: datetime | None = None, end: datetime | None = None, limit: int = 2000) -> pd.DataFrame:
    """Latest ``limit`` candles in [start, end), oldest first."""
    clauses, params = ["series_id = ?", "tf = ?"], [series_id, tf.value]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(int(start.timestamp()))
    if end is not None:
        clauses.append("ts < ?")
        params.append(int(end.timestamp()))
    params.append(int(limit))
    rows = con.execute(f"SELECT * FROM (SELECT ts, o, h, l, c, tick_volume, spread, closed FROM candles WHERE "
                       f"{' AND '.join(clauses)} ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC", params).fetchall()
    if not rows:
        return empty_candles()
    scale = float(10 ** price_decimals)
    data = np.array([[r["o"], r["h"], r["l"], r["c"]] for r in rows], dtype="float64") / scale
    frame = pd.DataFrame(data, columns=["open", "high", "low", "close"])
    frame["tick_volume"] = pd.array([r["tick_volume"] for r in rows], dtype="Int64")
    frame["spread"] = np.array([np.nan if r["spread"] is None else r["spread"] / scale for r in rows], dtype="float64")
    frame["closed"] = np.array([bool(r["closed"]) for r in rows])
    frame.index = pd.to_datetime(np.array([r["ts"] for r in rows], dtype="int64"), unit="s", utc=True)
    frame.index.name = "ts"
    return frame[list(CANDLE_COLUMNS)]


def find_import(con: sqlite3.Connection, series_id: int, tf: Timeframe, sha: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM data_imports WHERE series_id = ? AND tf = ? AND content_sha256 = ?",
                       (series_id, tf.value, sha)).fetchone()


def insert_import(con: sqlite3.Connection, *, series_id: int, tf: Timeframe, origin: str, dialect: str | None,
                  file_name: str | None, sha: str, time_basis: str, rows_read: int, rows_stored: int,
                  rows_rejected: int, duplicates: int, first_ts: int | None, last_ts: int | None,
                  report: dict[str, Any], now: datetime) -> int:
    cur = con.execute(
        "INSERT INTO data_imports (series_id, tf, origin, dialect, file_name, content_sha256, time_basis, rows_read, "
        "rows_stored, rows_rejected, duplicates_dropped, first_ts, last_ts, report_json, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (series_id, tf.value, origin, dialect, file_name, sha, time_basis, rows_read, rows_stored, rows_rejected,
         duplicates, first_ts, last_ts, json.dumps(report, sort_keys=True, default=str), iso(now)))
    return int(cur.lastrowid)


def list_imports(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = con.execute("SELECT i.*, s.key AS series_key FROM data_imports i JOIN series s ON s.id = i.series_id "
                       "ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["report"] = json.loads(item.pop("report_json"))
        item["first_ts"], item["last_ts"] = iso(_dt(r["first_ts"])), iso(_dt(r["last_ts"]))
        out.append(item)
    return out


def _dt(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None
    return pd.Timestamp(int(epoch), unit="s", tz="UTC").to_pydatetime()
