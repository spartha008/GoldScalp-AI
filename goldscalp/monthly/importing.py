"""Phase 5.1: historical CSV import preview/validation and per-timeframe coverage for the research store.

The preview runs the SAME parser, normaliser and validator as the importer (goldscalp.data.ingestion) without
storing anything; the import itself is ``ingestion.import_csv`` (idempotent per file hash). Missing market prices are
reported, never invented or interpolated. Only the isolated research store is ever written (by the import call)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import gaps
from goldscalp.data.ingestion import SCHEDULE_FIT_MAX_OUTSIDE, _best_shift
from goldscalp.data.normalization import TimeBasis, normalize, to_canonical
from goldscalp.data.providers import csv_file
from goldscalp.data.validation import REASONS, validate
from goldscalp.monthly.data import MIN_COVERAGE, month_range

MAX_LISTED = 200


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def month_coverage(index: pd.DatetimeIndex, tf: Timeframe, schedule: TradingSchedule, month: str,
                   lookback_days: int) -> dict[str, Any]:
    """Coverage of one month for one timeframe from a candle index: expected vs present bars, every missing interval
    (up to 200), the dates with missing bars, history before the month and the warm-up rule (history from the first
    scheduled bar at or after month start - lookback, as the monthly runs load it)."""
    start, end = month_range(month)
    window = index[(index >= pd.Timestamp(start)) & (index < pd.Timestamp(end))]
    expected = schedule.expected_bars(start, end, tf.seconds)
    missing = expected.difference(window)
    ranges: list[dict[str, Any]] = []
    if len(missing):
        step = pd.Timedelta(seconds=tf.seconds)
        run_start = prev = missing[0]
        for ts in missing[1:]:
            if ts - prev != step:
                ranges.append({"from": iso(run_start.to_pydatetime()), "to": iso((prev + step).to_pydatetime()),
                               "bars": int((prev - run_start) / step) + 1})
                run_start = ts
            prev = ts
        ranges.append({"from": iso(run_start.to_pydatetime()), "to": iso((prev + step).to_pydatetime()),
                       "bars": int((prev - run_start) / step) + 1})
    by_date = pd.Series(1, index=missing).groupby(missing.strftime("%Y-%m-%d")).sum() if len(missing) else pd.Series(dtype=int)
    before = index[index < pd.Timestamp(start)]
    lookback_start = start - timedelta(days=lookback_days)
    scheduled = schedule.expected_bars(lookback_start, start, tf.seconds)
    needed_from = scheduled[0].to_pydatetime() if len(scheduled) else lookback_start
    history_from = before[0].to_pydatetime() if len(before) else None
    share = round(len(window) / len(expected), 4) if len(expected) else 0.0
    warm = history_from is not None and history_from <= needed_from
    return {"month": month, "timeframe": tf.value, "range": {"start": iso(start), "end": iso(end)},
            "expected": int(len(expected)), "present": int(len(window)), "missing": int(len(missing)), "share": share,
            "meets_coverage": share >= MIN_COVERAGE, "min_coverage": MIN_COVERAGE,
            "missing_intervals": ranges[:MAX_LISTED], "missing_intervals_total": len(ranges),
            "missing_dates": [{"date": d, "bars": int(n)} for d, n in by_date.items()][:MAX_LISTED],
            "first_in_month": iso(window[0].to_pydatetime()) if len(window) else None,
            "last_in_month": iso(window[-1].to_pydatetime()) if len(window) else None,
            "history_before_month_from": iso(history_from) if history_from else None,
            "history_before_month_bars": int(len(before)),
            "warmup": {"lookback_days": lookback_days, "needed_from": iso(needed_from), "satisfied": warm,
                       "rule": f"history must start by the first scheduled {tf.value} bar at or after "
                               f"{iso(lookback_start)} (the month start - {lookback_days} days)"},
            "ready": share >= MIN_COVERAGE and warm}


def dst_transitions(basis: TimeBasis, first: datetime, last: datetime) -> list[dict[str, Any]]:
    """Days inside [first, last] on which the time basis changes its UTC offset (US DST for rule:ny_close)."""
    days = pd.date_range(pd.Timestamp(first).normalize().tz_localize(None), pd.Timestamp(last).normalize().tz_localize(None),
                         freq="D")
    if len(days) < 2:
        return []
    noon = pd.Series(days + pd.Timedelta(hours=12))
    utc = basis.to_utc(noon)
    offsets = ((noon.dt.tz_localize("UTC") - utc).dt.total_seconds() / 3600).round(2)
    out = []
    for i in range(1, len(offsets)):
        if offsets.iloc[i] != offsets.iloc[i - 1] and pd.notna(offsets.iloc[i]) and pd.notna(offsets.iloc[i - 1]):
            out.append({"date": days[i].strftime("%Y-%m-%d"), "offset_before_hours": float(offsets.iloc[i - 1]),
                        "offset_after_hours": float(offsets.iloc[i])})
    return out


def preview(content: bytes, *, file_name: str, instrument: str, dialect: str, tf: Timeframe, time_basis: str,
            point: Decimal | None, schedule: TradingSchedule, month: str, lookback_days: int, now: datetime,
            already_imported: bool = False) -> dict[str, Any]:
    """Parse and validate without storing. Returns checks (PASS / WARN / FAIL), samples and month coverage."""
    checks: list[dict[str, str]] = []
    basis = TimeBasis.parse(time_basis)
    try:
        raw = csv_file.parse(content, dialect, time_basis=basis, point=point)
    except csv_file.CsvFormatError as exc:
        return {"ok": False, "checks": [_check("CSV structure", "FAIL", str(exc))], "file_name": file_name}
    sha = hashlib.sha256(content).hexdigest()
    text_rows = csv_file.decode(content).splitlines()
    checks.append(_check("CSV structure", "PASS", f"{len(raw)} data rows in the {dialect} layout"))
    bad_ts = int(raw["ts"].isna().sum())
    checks.append(_check("Date and time parsing", "PASS" if bad_ts == 0 else "FAIL",
                         f"{bad_ts} row(s) with a missing, unparseable, nonexistent or ambiguous time" if bad_ts else
                         "every row's date and time parsed"))
    order_ok = bool(raw["ts"].dropna().is_monotonic_increasing)
    checks.append(_check("Candle ordering", "PASS" if order_ok else "WARN",
                         "rows are in time order" if order_ok else "rows are out of order; they are sorted on import"))
    frame, norm = normalize(raw)
    checks.append(_check("Duplicate timestamps", "PASS" if norm.duplicates_dropped == 0 else "WARN",
                         f"{norm.duplicates_dropped} duplicate timestamp(s); the last copy is kept"))
    valid, report = validate(frame, tf, now=now)
    price_reasons = {k: v for k, v in report.rejected_by_reason.items() if k in ("non_finite_price", "non_positive_price",
                                                                                 "ohlc_inconsistent")}
    checks.append(_check("OHLC values and prices", "PASS" if not price_reasons else "FAIL",
                         "all prices finite, positive and consistent (low <= open/close <= high)" if not price_reasons else
                         "; ".join(f"{v} x {REASONS[k]}" for k, v in price_reasons.items())))
    other = {k: v for k, v in report.rejected_by_reason.items() if k not in price_reasons and k != "bad_timestamp"}
    if other:
        checks.append(_check("Other rejected rows", "WARN", "; ".join(f"{v} x {REASONS.get(k, k)}" for k, v in other.items())))
    candles = to_canonical(valid)
    idx = candles.index
    if len(idx) == 0:
        checks.append(_check("Usable candles", "FAIL", "no valid candle remains after validation"))
        return {"ok": False, "checks": checks, "file_name": file_name, "sha256": sha[:16]}
    aligned = report.rejected_by_reason.get("misaligned", 0)
    spacing = pd.Series(idx[1:] - idx[:-1]).dt.total_seconds()
    modal = float(spacing.mode().iloc[0]) if len(spacing) else float(tf.seconds)
    tf_ok = aligned == 0 and modal == tf.seconds
    checks.append(_check("Timeframe consistency", "PASS" if tf_ok else "FAIL",
                         f"bars are {tf.value} (most common spacing {modal / 60:g} min, {aligned} misaligned)" if tf_ok else
                         f"declared {tf.value} but the most common spacing is {modal / 60:g} min and {aligned} bar(s) are "
                         "misaligned: check the timeframe"))
    stem = file_name.upper()
    inst_ok = stem.startswith(instrument.upper())
    checks.append(_check("Instrument", "PASS" if inst_ok else "WARN",
                         f"file name starts with {instrument}" if inst_ok else
                         f"the file name does not start with {instrument}; MT5 bar exports carry no symbol column, so "
                         "confirm the file is the right instrument"))
    parts = stem.split("_")
    named_tf = parts[1] if len(parts) > 1 and parts[1] in {t.value for t in Timeframe} else None
    if named_tf and named_tf != tf.value:
        checks.append(_check("Timeframe in file name", "FAIL", f"the file name says {named_tf}, not {tf.value}"))
    outside = gaps.outside_schedule_fraction(idx, tf, schedule)
    hint = _best_shift(idx, tf, schedule) if outside > SCHEDULE_FIT_MAX_OUTSIDE else ""
    checks.append(_check("Time zone (time basis)", "PASS" if outside <= SCHEDULE_FIT_MAX_OUTSIDE else "FAIL",
                         f"{outside:.1%} of bars fall in closed market hours under {time_basis}" + hint))
    transitions = dst_transitions(basis, idx[0].to_pydatetime(), idx[-1].to_pydatetime())
    checks.append(_check("Daylight-saving transitions", "PASS",
                         "; ".join(f"{t['date']}: server offset {t['offset_before_hours']:+g} h -> {t['offset_after_hours']:+g} h"
                                   for t in transitions) if transitions else "no offset change inside the file's range"))
    spread_known = float((candles["spread"].fillna(0) > 0).mean())
    checks.append(_check("Spread", "PASS" if spread_known > 0.5 else "WARN",
                         f"spread known on {spread_known:.1%} of bars" + (f" (points x {point})" if point else "") +
                         ("" if spread_known > 0.5 else "; the configured spread is used where it is missing")))
    gap = gaps.gap_report(idx, tf, schedule)
    checks.append(_check("Missing candles (whole file)", "PASS" if gap["missing"] == 0 else "WARN",
                         f"{gap['missing']} of {gap['expected']} expected bars missing; they are reported, never filled"))
    cov = month_coverage(idx, tf, schedule, month, lookback_days)
    checks.append(_check(f"{month} coverage", "PASS" if cov["meets_coverage"] else "FAIL",
                         f"{cov['present']} of {cov['expected']} bars ({cov['share']:.1%}); {MIN_COVERAGE:.0%} needed"))
    checks.append(_check("Pre-month warm-up", "PASS" if cov["warmup"]["satisfied"] else "FAIL",
                         f"history from {cov['history_before_month_from'] or 'none'}; {cov['warmup']['rule']}"))
    samples = []
    for i in range(min(5, len(raw))):
        r = raw.iloc[i]
        samples.append({"source_row": text_rows[i + 1] if i + 1 < len(text_rows) else "",
                        "utc": iso(r["ts"].to_pydatetime()) if pd.notna(r["ts"]) else None,
                        **{k: None if pd.isna(r[k]) else float(r[k]) for k in ("open", "high", "low", "close")},
                        "spread_usd": None if pd.isna(r["spread"]) else round(float(r["spread"]), 4)})
    fails = [c for c in checks if c["status"] == "FAIL" and c["check"] not in (f"{month} coverage", "Pre-month warm-up")]
    return {"ok": not fails, "importable": not fails, "file_name": file_name, "sha256": sha[:16],
            "already_imported": already_imported, "instrument": instrument, "timeframe": tf.value, "dialect": dialect,
            "time_basis": time_basis, "rows_read": int(len(raw)), "rows_valid": int(len(candles)),
            "rows_rejected": int(report.rows_rejected), "duplicates_dropped": int(norm.duplicates_dropped),
            "first": iso(idx[0].to_pydatetime()), "last": iso(idx[-1].to_pydatetime()),
            "outside_schedule_fraction": round(outside, 4), "dst_transitions": transitions, "checks": checks,
            "samples": samples, "coverage": cov,
            "note": "Preview only: nothing was stored. Missing bars are reported and never filled or interpolated."}
