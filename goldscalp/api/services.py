"""Application wiring shared by the routers."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException, Request

from goldscalp.core.clock import Clock, iso
from goldscalp.core.config import Settings
from goldscalp.analysis.intelligence import FRAME_LIMITS
from goldscalp.analysis.replay import ReplayDataset
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data.resample import resample
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.storage import candle_repo
from goldscalp.storage.db import Database

MAX_DERIVED_SOURCE_BARS = 60000
MAX_BASE_CANDLES = 200_000


@dataclass
class AppServices:
    settings: Settings
    db: Database
    clock: Clock
    spec: InstrumentSpec
    market: Any            # MarketHub (the active MarketDataService behind it)
    engine: PaperTradingEngine
    analysis_cfg: Any = None   # goldscalp.analysis.config.AnalysisConfig (Phase 2)
    replays: Any = None        # goldscalp.analysis.replay.ReplayStore (Phase 2)
    paper_costs: Any = None    # the paper simulator's CostModel (defaults for performance evaluation)
    perf_jobs: Any = None      # goldscalp.api.routers.performance.PerformanceJobs (Phase 2)
    feed: Any = None           # goldscalp.live.feed.FeedMonitor (Phase 4)
    monitor: Any = None        # goldscalp.live.monitor.LiveMonitor (Phase 4)
    demo: Any = None           # goldscalp.execution.mirror.DemoMirror (optional MT5 DEMO-account execution)
    manual: Any = None         # goldscalp.execution.manual.ManualTicket (Dashboard order ticket, MT5 DEMO account)


def services(request: Request) -> AppServices:
    return request.app.state.gs


def clean(value: Any) -> Any:
    """JSON-safe copy: NaN/inf -> None, numpy scalars -> Python, Decimal -> str, datetimes -> ISO.
    (Adapted from SpicyStock src/report.py _sanitise() @ 96ef4732.)"""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def pick_series(db: Database, key: str | None, preferred: str | None = None) -> Any:
    """The requested series, or the default: the ACTIVE provider's live series if it has candles,
    else another live series, else the newest CSV series."""
    with db.read() as con:
        if key:
            row = candle_repo.series_row(con, key)
            if row is None:
                raise HTTPException(404, f"unknown series {key!r}")
            return row
        rows = con.execute("SELECT s.*, (SELECT COUNT(*) FROM candles c WHERE c.series_id = s.id) AS n "
                           "FROM series s ORDER BY s.kind = 'live' DESC, s.id DESC").fetchall()
    rows = sorted(rows, key=lambda r: r["key"] != preferred)   # stable: the preferred series first
    for row in rows:
        if row["n"] > 0:
            return row
    return None


def active_series_key(gs: AppServices) -> str | None:
    series = gs.market.series
    return series.key if series is not None else None


def load_tf(db: Database, series_row: Any, tf: Timeframe, limit: int, as_of: Any = None, end: Any = None) -> pd.DataFrame:
    """Candles for a timeframe: stored ("native") bars, extended with bars resampled from
    newer M1 bars ("derived"). A series that only has M1 is fully derived. ``as_of`` (normally now)
    keeps a partially elapsed derived bar marked as forming, never closed. ``end`` (exclusive) pages back
    through history: the latest ``limit`` bars that open before it."""
    decimals = series_row["price_decimals"]
    with db.read() as con:
        native = candle_repo.load_candles(con, series_row["id"], decimals, tf, end=end, limit=limit)
        if tf is Timeframe.M1:
            native["origin"] = "native"
            native["sub_bars"] = pd.NA
            return native
        start = None
        if not native.empty:
            start = (native.index.max() + pd.Timedelta(seconds=tf.seconds)).to_pydatetime()
        m1 = candle_repo.load_candles(con, series_row["id"], decimals, Timeframe.M1, start=start, end=end,
                                      limit=min(limit * tf.minutes, MAX_DERIVED_SOURCE_BARS))
    native["origin"] = "native"
    native["sub_bars"] = pd.NA
    if m1.empty:
        return native
    derived = resample(m1, Timeframe.M1, tf, as_of=as_of)
    derived["origin"] = "derived_from_M1"
    combined = pd.concat([native, derived])
    combined = combined[~combined.index.duplicated(keep="first")].sort_index()
    return combined.tail(limit)


def choose_base(db: Database, row: Any, step_tf: Timeframe, start: datetime, end: datetime) -> tuple[Timeframe, str]:
    """The finest stored timeframe (dividing the step) whose coverage starts at or before ``start``.
    Phase 3 fix: the Phase 2 rule took M1 whenever ANY M1 candle fell in the window, so a range starting
    before M1 coverage silently started late."""
    candidates = [tf for tf in ALL_TIMEFRAMES if tf.seconds <= step_tf.seconds and step_tf.seconds % tf.seconds == 0]
    with db.read() as con:
        spans = {tf: con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE series_id = ? AND tf = ? "
                                 "AND closed = 1", (row["id"], tf.value)).fetchone() for tf in candidates}
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    for tf in candidates:
        first, last, n = spans[tf]
        if n and first <= start_s + tf.seconds and last >= start_s:
            return tf, f"{tf.value}: the finest stored timeframe whose candles cover the window start"
    for tf in candidates:
        first, last, n = spans[tf]
        if n and first < end_s and last >= start_s:
            return tf, (f"{tf.value}: no timeframe covers the window start; {tf.value} candles begin "
                        f"{iso(datetime.fromtimestamp(first, tz=UTC))}, so the evaluation starts there")
    return step_tf, f"{step_tf.value}: no stored candles in the window"


def load_dataset(db: Database, row: Any, step_tf: Timeframe, start: datetime, end: datetime,
                 lookback_days: int) -> ReplayDataset:
    """Base candles (``choose_base``) from ``start - lookback`` (aligned to the hour) to ``end``, plus native
    higher-timeframe history that ended before the first base candle. Only CLOSED candles are used."""
    first_wanted = pd.Timestamp(start - timedelta(days=lookback_days)).floor("h").to_pydatetime()
    decimals = row["price_decimals"]
    base_tf, note = choose_base(db, row, step_tf, start, end)
    with db.read() as con:
        base = candle_repo.load_candles(con, row["id"], decimals, base_tf, start=first_wanted, end=end,
                                        limit=MAX_BASE_CANDLES)
        base = base[base["closed"]]
        history: dict[str, pd.DataFrame] = {}
        if len(base):
            first = base.index[0].to_pydatetime()
            for tf in ALL_TIMEFRAMES:
                if tf.seconds <= base_tf.seconds:
                    continue
                native = candle_repo.load_candles(con, row["id"], decimals, tf, end=first,
                                                  limit=FRAME_LIMITS[tf.value])
                native = native[native["closed"]]
                native = native[(native.index + pd.Timedelta(seconds=tf.seconds)) <= pd.Timestamp(first)]
                if len(native):
                    history[tf.value] = native
    return ReplayDataset(series_key=row["key"], source_id=row["source_id"], base_tf=base_tf, base=base,
                         history=history, note=note)


def load_outcome_candles(db: Database, row: Any, base_tf: Timeframe, start: datetime, until: datetime) -> pd.DataFrame:
    """Closed base candles in [start, until) used ONLY to measure outcomes after signals (never for signals)."""
    with db.read() as con:
        frame = candle_repo.load_candles(con, row["id"], row["price_decimals"], base_tf, start=start, end=until,
                                         limit=MAX_BASE_CANDLES)
    return frame[frame["closed"]]


def candles_payload(frame: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for ts, row in frame.iterrows():
        out.append({
            "time": int(ts.timestamp()), "open": clean(row["open"]), "high": clean(row["high"]),
            "low": clean(row["low"]), "close": clean(row["close"]),
            "tick_volume": None if pd.isna(row["tick_volume"]) else int(row["tick_volume"]),
            "spread": clean(row["spread"]), "closed": bool(row["closed"]),
            "origin": row.get("origin", "native"),
            "sub_bars": None if pd.isna(row.get("sub_bars", pd.NA)) else int(row["sub_bars"]),
        })
    return out
