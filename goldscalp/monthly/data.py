"""Phase 5 data access and the month calendar (PHASE5_ARCHITECTURE.md section 4).

Candles come from the application database (READ-ONLY) or from history imported into the isolated research store.
A run never mixes sources. Every result records the store, series, timeframe, range, count and dataset hash."""
from __future__ import annotations

import calendar as _cal
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.gaps import gap_report
from goldscalp.monthly.store import ReadOnlySource
from goldscalp.storage import candle_repo, strategy_repo
from goldscalp.storage.db import Database

RUN_TIMEFRAMES = ("M15", "M5", "M1")
STORES = ("app", "research")
MIN_COVERAGE = 0.95
MIN_RESEARCH_DAYS = 20
MAX_CANDLES = 400_000


class DataSources:
    def __init__(self, app: ReadOnlySource, research: Database) -> None:
        self.app = app
        self.research = research

    def db(self, store: str) -> Any:
        if store not in STORES:
            raise ValueError(f"unknown store {store!r}")
        return self.app if store == "app" else self.research

    # -------------------------------------------------------------- candles
    def series(self) -> list[dict[str, Any]]:
        out = []
        for store in STORES:
            with self.db(store).read() as con:
                for s in con.execute("SELECT * FROM series ORDER BY key").fetchall():
                    tfs = {r["tf"]: {"first": iso(datetime.fromtimestamp(r["a"], UTC)),
                                     "last": iso(datetime.fromtimestamp(r["b"], UTC)), "bars": r["n"]}
                           for r in con.execute("SELECT tf, MIN(ts) a, MAX(ts) b, COUNT(*) n FROM candles WHERE "
                                                "series_id = ? AND closed = 1 GROUP BY tf", (s["id"],))}
                    if tfs:
                        out.append({"store": store, "key": s["key"], "source_id": s["source_id"], "kind": s["kind"],
                                    "candle_side": s["candle_side"], "timeframes": tfs,
                                    "label": f"{s['key']} ({'application database, read-only' if store == 'app' else 'research store import'})"})
        return out

    def series_row(self, store: str, key: str) -> Any:
        with self.db(store).read() as con:
            return candle_repo.series_row(con, key)

    def load(self, store: str, row: Any, tf: Timeframe, start: datetime | None, end: datetime | None,
             limit: int = MAX_CANDLES) -> pd.DataFrame:
        """CLOSED candles in [start, end), the latest ``limit`` (forming bars are never used)."""
        with self.db(store).read() as con:
            frame = candle_repo.load_candles(con, row["id"], row["price_decimals"], tf, start=start, end=end,
                                             limit=limit)
        return frame[frame["closed"]]

    # -------------------------------------------------------------- strategies (application database, read-only)
    def strategy(self, sid: int, version: int | None = None) -> dict[str, Any] | None:
        with self.app.read() as con:
            return strategy_repo.get(con, sid, version)

    def strategies(self) -> list[dict[str, Any]]:
        with self.app.read() as con:
            return strategy_repo.list_all(con)

    def default_versions(self) -> list[tuple[int, int]]:
        """The versions the live monitor is validating (A v2, B v2, C v4 on 14 Nov 2026), else current versions."""
        with self.app.read() as con:
            try:
                rows = con.execute("SELECT strategy_id, version FROM monitored_strategies WHERE status != 'stopped' "
                                   "ORDER BY strategy_id").fetchall()
            except Exception:  # noqa: BLE001 - an application database without Phase 4 tables
                rows = []
            if rows:
                return [(int(r[0]), int(r[1])) for r in rows]
            return [(s["id"], s["version"]) for s in strategy_repo.list_all(con)][:3]

    def verified_spec(self) -> dict[str, Any] | None:
        """The XAUUSD contract values from the latest ACTUAL-terminal verification record (adapter MetaTrader5)."""
        with self.app.read() as con:
            try:
                row = con.execute("SELECT id, started_at, checks FROM mt5_verifications WHERE adapter = 'MetaTrader5' "
                                  "ORDER BY started_at DESC LIMIT 1").fetchone()
            except Exception:  # noqa: BLE001
                row = None
        if row is None:
            return None
        check = next((c for c in json.loads(row["checks"]) if c["key"] == "symbol"), None)
        spec = ((check or {}).get("data") or {}).get("spec")
        return None if not spec else {"record": row["id"], "verified_at": row["started_at"], **spec}


# ------------------------------------------------------------------ calendar
def month_range(month: str) -> tuple[datetime, datetime]:
    try:
        y, m = (int(x) for x in month.split("-"))
        start = datetime(y, m, 1, tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"month must look like 2026-10, not {month!r}") from exc
    days = _cal.monthrange(y, m)[1]
    return start, start + timedelta(days=days)


def latest_completed_month(now: datetime) -> str:
    first = datetime(now.year, now.month, 1, tzinfo=UTC)
    prev = first - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def previous_month(month: str) -> str:
    start, _ = month_range(month)
    prev = start - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def coverage(frame: pd.DataFrame, tf: Timeframe, start: datetime, end: datetime, schedule: TradingSchedule) -> dict[str, Any]:
    window = frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    rep = gap_report(window.index, tf, schedule, start, end) if len(window) else \
        {"expected": int(len(schedule.expected_bars(start, end, tf.seconds))), "present": 0, "missing": None,
         "missing_ranges": []}
    expected = rep["expected"]
    present = rep["present"]
    return {"timeframe": tf.value, "expected": expected, "present": present,
            "share": round(present / expected, 4) if expected else 0.0,
            "first": iso(window.index[0].to_pydatetime()) if len(window) else None,
            "last": iso(window.index[-1].to_pydatetime()) if len(window) else None,
            "missing_ranges": (rep.get("missing_ranges") or [])[:5]}


def availability(src: DataSources, store: str, key: str, month: str, schedule: TradingSchedule, *,
                 research_days: int, lookback_days: int) -> dict[str, Any]:
    """Can the month be evaluated on this series? Per run timeframe, plus the research window before the month."""
    start, end = month_range(month)
    row = src.series_row(store, key)
    out: dict[str, Any] = {"month": month, "range": {"start": iso(start), "end": iso(end)}, "store": store,
                           "series": key, "timeframes": {}, "ready": [], "missing": []}
    if row is None:
        out["missing"].append(f"series {key!r} does not exist in the {store} store")
        out["research"] = {"sufficient": False, "reason": "no series"}
        return out
    frames = {tf: src.load(store, row, Timeframe.parse(tf), start - timedelta(days=max(research_days, lookback_days) + 7),
                           end) for tf in ("M1", "M5", "M15", "H1")}
    for tf in RUN_TIMEFRAMES:
        exec_cov = coverage(frames[tf], Timeframe.parse(tf), start, end, schedule)
        base = "M1" if tf == "M1" else "M5"
        base_frame = frames[base]
        base_cov = exec_cov if base == tf else coverage(base_frame, Timeframe.parse(base), start, end, schedule)
        before = base_frame[base_frame.index < pd.Timestamp(start)]
        history_from = before.index[0].to_pydatetime() if len(before) else None
        # The run loads its warm-up from (start - lookback); when that falls in a closed market (a weekend), the first
        # bar it can load is the first SCHEDULED bar after it. Requiring earlier data would ask for bars the run never
        # uses (e.g. Friday bars for a lookback that starts on Saturday).
        lookback_start = start - timedelta(days=lookback_days)
        scheduled = schedule.expected_bars(lookback_start, start, Timeframe.parse(base).seconds)
        needed_from = scheduled[0].to_pydatetime() if len(scheduled) else lookback_start
        enough_history = history_from is not None and history_from <= needed_from
        problems = []
        if exec_cov["share"] < MIN_COVERAGE:
            problems.append(f"{tf} execution candles cover {exec_cov['share']:.0%} of the month (need {MIN_COVERAGE:.0%}; "
                            f"stored {tf} {('starts ' + exec_cov['first']) if exec_cov['first'] else 'has none in the month'})")
        if base != tf and base_cov["share"] < MIN_COVERAGE:
            problems.append(f"{base} analysis candles cover {base_cov['share']:.0%} of the month")
        if not enough_history:
            problems.append(f"{base} analysis history before the month starts "
                            f"{iso(history_from) if history_from else 'nowhere'}; {lookback_days} days are needed "
                            f"(from the first trading bar at or after {iso(lookback_start)}: {iso(needed_from)})")
        out["timeframes"][tf] = {"execution": exec_cov, "analysis_base": base,
                                 "analysis": base_cov if base != tf else None,
                                 "history_from": iso(history_from) if history_from else None,
                                 "sufficient": not problems, "problems": problems}
        (out["ready"] if not problems else out["missing"]).extend([tf] if not problems else problems)
    r_start = start - timedelta(days=research_days)
    m5 = frames["M5"]
    research_part = m5[(m5.index >= pd.Timestamp(r_start)) & (m5.index < pd.Timestamp(start))]
    first = research_part.index[0].to_pydatetime() if len(research_part) else None
    days = (start - first).days if first else 0
    cov = coverage(m5, Timeframe.M5, max(r_start, first) if first else r_start, start, schedule)
    out["research"] = {"window": {"start": iso(max(r_start, first) if first else r_start), "end": iso(start)},
                       "days_available": days, "coverage": cov,
                       "sufficient": days >= MIN_RESEARCH_DAYS and cov["share"] >= 0.8,
                       "reason": None if days >= MIN_RESEARCH_DAYS and cov["share"] >= 0.8 else
                       f"only {days} days of M5 history before the month ({MIN_RESEARCH_DAYS} needed, 80% coverage)"}
    h1 = frames["H1"]
    out["h1_history_bars_before_month"] = int((h1.index < pd.Timestamp(start)).sum())
    return out


def default_month(src: DataSources, store: str, key: str, schedule: TradingSchedule, now: datetime, *,
                  research_days: int, lookback_days: int, search: int = 12) -> dict[str, Any]:
    """The latest COMPLETED month whose M15 and M5 runs have sufficient data. Never silently another month: the
    reason for every skipped month is returned."""
    month = latest_completed_month(now)
    skipped = []
    for _ in range(search):
        av = availability(src, store, key, month, schedule, research_days=research_days, lookback_days=lookback_days)
        if all(av["timeframes"].get(tf, {}).get("sufficient") for tf in ("M15", "M5")):
            return {"month": month, "availability": av, "skipped": skipped}
        skipped.append({"month": month, "missing": av["missing"]})
        month = previous_month(month)
    return {"month": None, "availability": None, "skipped": skipped}
