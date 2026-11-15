"""The ONLY write path for candles: normalise -> validate -> check -> store.

Historical imports (Mode B) and live provider candles (Mode A) both pass
through here and end up in the same schema, in separate series.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from goldscalp.core.clock import Clock, iso
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import gaps
from goldscalp.data.normalization import TimeBasis, normalize, to_canonical
from goldscalp.data.providers import csv_file
from goldscalp.data.validation import validate
from goldscalp.storage import candle_repo
from goldscalp.storage.candle_repo import SeriesInfo
from goldscalp.storage.db import Database

#: Refuse an import when more than this share of bars sits in closed market hours.
SCHEDULE_FIT_MAX_OUTSIDE = 0.02
SCHEDULE_FIT_MIN_ROWS = 10


class ImportRefused(ValueError):
    """The file was read but must not be stored (e.g. the time basis is wrong)."""


@dataclass
class ImportResult:
    series_key: str
    tf: str
    already_imported: bool
    rows_read: int
    rows_stored: int
    rows_rejected: int
    duplicates_dropped: int
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    first: str | None = None
    last: str | None = None
    outside_schedule_fraction: float = 0.0
    gaps: dict[str, Any] = field(default_factory=dict)
    import_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def import_csv(db: Database, clock: Clock, schedule: TradingSchedule, *, content: bytes, file_name: str,
               dialect: str, tf: Timeframe, time_basis: str, series_label: str, symbol: str,
               source_symbol: str, candle_side: str, price_decimals: int,
               point: Decimal | None = None) -> ImportResult:
    """Import one CSV file into the historical series ``csv:<series_label>``."""
    basis = TimeBasis.parse(time_basis)
    raw = csv_file.parse(content, dialect, time_basis=basis, point=point)
    sha = hashlib.sha256(content).hexdigest()
    info = SeriesInfo(key=f"csv:{_label(series_label)}", symbol=symbol.upper(), kind="historical",
                      source_id=f"csv:{dialect}", source_symbol=source_symbol, candle_side=candle_side,
                      price_decimals=price_decimals,
                      description=f"Historical candles imported from CSV ({dialect}); provenance per import")
    now = clock.now()
    with db.read() as con:
        existing_series = candle_repo.series_row(con, info.key)
        if existing_series is not None:
            prior = candle_repo.find_import(con, existing_series["id"], tf, sha)
            if prior is not None:
                return ImportResult(series_key=info.key, tf=tf.value, already_imported=True,
                                    rows_read=prior["rows_read"], rows_stored=0, rows_rejected=prior["rows_rejected"],
                                    duplicates_dropped=prior["duplicates_dropped"], import_id=prior["id"])
    frame, norm = normalize(raw)
    valid, report = validate(frame, tf, now=now)
    if raw.attrs.get("rows_without_offset"):
        report.samples.insert(0, f"{raw.attrs['rows_without_offset']} row(s) had a timestamp without a UTC offset")
    candles = to_canonical(valid)
    outside = gaps.outside_schedule_fraction(candles.index, tf, schedule)
    if len(candles) >= SCHEDULE_FIT_MIN_ROWS and outside > SCHEDULE_FIT_MAX_OUTSIDE:
        hint = _best_shift(candles.index, tf, schedule)
        raise ImportRefused(
            f"{outside:.0%} of bars fall while the market is closed under time basis {time_basis!r}; the time basis is "
            f"probably wrong.{hint} Nothing was stored.")
    gap_info = gaps.gap_report(candles.index, tf, schedule)
    epochs = candles.index.as_unit("s").asi8
    first_ts, last_ts = (int(epochs[0]), int(epochs[-1])) if len(epochs) else (None, None)
    result = ImportResult(series_key=info.key, tf=tf.value, already_imported=False, rows_read=len(raw),
                          rows_stored=len(candles), rows_rejected=report.rows_rejected,
                          duplicates_dropped=norm.duplicates_dropped, rejected_by_reason=report.rejected_by_reason,
                          samples=report.samples, first=iso(_ts(first_ts)), last=iso(_ts(last_ts)),
                          outside_schedule_fraction=round(outside, 4), gaps=gap_info)
    with db.transaction() as con:
        series_id = candle_repo.ensure_series(con, info, now)
        import_id = candle_repo.insert_import(
            con, series_id=series_id, tf=tf, origin="csv", dialect=dialect, file_name=file_name, sha=sha,
            time_basis=basis.text, rows_read=result.rows_read, rows_stored=result.rows_stored,
            rows_rejected=result.rows_rejected, duplicates=result.duplicates_dropped, first_ts=first_ts,
            last_ts=last_ts, report=result.to_dict(), now=now)
        candle_repo.upsert_candles(con, series_id, price_decimals, tf, candles, "csv", import_id, now)
    result.import_id = import_id
    return result


def store_live_candles(db: Database, clock: Clock, info: SeriesInfo, tf: Timeframe, raw: pd.DataFrame) -> dict[str, Any]:
    """Validate and upsert provider candles into the live series. Invalid rows are dropped and counted."""
    now = clock.now()
    frame, norm = normalize(raw)
    valid, report = validate(frame, tf, now=now)
    candles = to_canonical(valid)
    with db.transaction() as con:
        series_id = candle_repo.ensure_series(con, info, now)
        stored = candle_repo.upsert_candles(con, series_id, info.price_decimals, tf, candles, "provider", None, now)
    return {"rows_read": len(raw), "rows_stored": stored, "rows_rejected": report.rows_rejected,
            "duplicates_dropped": norm.duplicates_dropped, "rejected_by_reason": report.rejected_by_reason}


def _best_shift(index: pd.DatetimeIndex, tf: Timeframe, schedule: TradingSchedule) -> str:
    best_hours, best = 0, gaps.outside_schedule_fraction(index, tf, schedule)
    for hours in range(-12, 13):
        if hours == 0:
            continue
        shifted = index + pd.Timedelta(hours=hours)
        fraction = gaps.outside_schedule_fraction(shifted, tf, schedule)
        if fraction < best:
            best_hours, best = hours, fraction
    if best_hours == 0:
        return ""
    return (f" Shifting the timestamps by {best_hours:+d} h would leave {best:.1%} outside trading hours "
            "(for MT5 exports try time basis rule:ny_close or the broker's fixed server offset).")


def _label(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(text).strip())
    if not cleaned or len(cleaned) > 64:
        raise ValueError("series label must be 1-64 characters (letters, digits, - _ .)")
    return cleaned


def _ts(epoch: int | None) -> datetime | None:
    return None if epoch is None else pd.Timestamp(epoch, unit="s", tz="UTC").to_pydatetime()
