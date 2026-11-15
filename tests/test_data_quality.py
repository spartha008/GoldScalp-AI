"""Candle normalisation, validation, timezones, gaps, resampling and CSV import."""
from __future__ import annotations

from datetime import datetime, UTC
from decimal import Decimal

import pandas as pd
import pytest

from goldscalp.core.clock import LocalTimeError, local_to_utc, parse_iso_aware
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import gaps
from goldscalp.data.ingestion import ImportRefused, import_csv
from goldscalp.data.normalization import TimeBasis, normalize
from goldscalp.data.providers.csv_file import CsvFormatError
from goldscalp.data.resample import resample
from goldscalp.data.validation import validate
from goldscalp.storage import candle_repo
from tests.conftest import csv_bytes, synthetic_m1

UTC = UTC


def raw(rows):
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
    return frame


def test_invalid_candles_are_rejected_with_reasons():
    frame, _ = normalize(raw([
        ("2026-11-11T15:00Z", 4000, 4001, 3999, 4000.5),   # ok
        ("2026-11-11T15:01Z", 4000, 3990, 3999, 4000.5),   # high below open
        ("2026-11-11T15:02Z", 4000, 4001, 3999, float("nan")),
        ("2026-11-11T15:03Z", -1, 4001, -2, 4000),
        ("2026-11-11T15:04:30Z", 4000, 4001, 3999, 4000.5),  # not on a minute boundary
    ]))
    valid, report = validate(frame, Timeframe.M1)
    assert len(valid) == 1
    assert report.rejected_by_reason == {"ohlc_inconsistent": 1, "non_finite_price": 1, "non_positive_price": 1,
                                         "misaligned": 1}


def test_duplicate_timestamps_keep_the_last_copy():
    frame, report = normalize(raw([
        ("2026-11-11T15:00Z", 4000, 4001, 3999, 4000.5),
        ("2026-11-11T15:01Z", 4001, 4002, 4000, 4001.5),
        ("2026-11-11T15:00Z", 4000, 4003, 3998, 4002.0),   # correction of the 15:00 bar, arrives last
    ]))
    assert report.duplicates_dropped == 1
    first = frame[frame["ts"] == pd.Timestamp("2026-11-11T15:00Z")].iloc[0]
    assert first["close"] == 4002.0
    assert list(frame["ts"]) == sorted(frame["ts"])


def test_future_bars_are_rejected():
    frame, _ = normalize(raw([("2026-11-11T15:05Z", 4000, 4001, 3999, 4000.5)]))
    _, report = validate(frame, Timeframe.M1, now=datetime(2026, 11, 11, 15, 0, tzinfo=UTC))
    assert report.rejected_by_reason == {"future": 1}


def test_offsetless_timestamps_are_refused():
    with pytest.raises(ValueError, match="no UTC offset"):
        parse_iso_aware("2026-11-11T15:00:00")
    assert parse_iso_aware("2026-11-11T15:00:00Z") == datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


def test_nonexistent_and_ambiguous_local_times_are_refused():
    with pytest.raises(LocalTimeError, match="does not exist"):
        local_to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")       # spring-forward gap
    with pytest.raises(LocalTimeError, match="ambiguous"):
        local_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York")      # fall-back overlap
    assert local_to_utc(datetime(2026, 11, 11, 10, 0), "America/New_York") == datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


def test_ny_close_server_time_follows_us_dst():
    basis = TimeBasis.parse("rule:ny_close")
    stamps = pd.Series(pd.to_datetime(["2026-07-15 12:00:00", "2026-11-11 12:00:00"]))
    utc = basis.to_utc(stamps)
    assert list(utc) == [pd.Timestamp("2026-07-15T09:00Z"), pd.Timestamp("2026-11-11T10:00Z")]  # UTC+3 / UTC+2


def test_unknown_time_basis_is_refused():
    with pytest.raises(ValueError, match="unknown time basis"):
        TimeBasis.parse("server")


def test_missing_candles_are_detected_but_weekend_and_break_are_not_gaps(spec):
    frame = synthetic_m1("2026-11-12T20:30Z", "2026-11-12T23:30Z", spec.schedule)   # spans the 22:00-23:00 UTC break
    expected_open = len(frame)
    holed = frame.drop(frame.index[[10, 11, 12]])
    report = gaps.gap_report(holed.index, Timeframe.M1, spec.schedule)
    assert report["missing"] == 3
    assert report["missing_ranges"][0]["bars"] == 3
    assert report["expected"] == expected_open                     # the daily break is not expected
    weekend = synthetic_m1("2026-11-13T21:00Z", "2026-11-15T23:30Z", spec.schedule)  # Fri close -> Sun open
    assert gaps.gap_report(weekend.index, Timeframe.M1, spec.schedule)["missing"] == 0


def test_resample_composition_property(spec):
    m1 = synthetic_m1("2026-11-11T00:00Z", "2026-11-11T12:00Z", spec.schedule)
    direct = resample(m1, Timeframe.M1, Timeframe.H1)
    via = resample(resample(m1, Timeframe.M1, Timeframe.M15).drop(columns=["sub_bars"]), Timeframe.M15, Timeframe.H1)
    pd.testing.assert_frame_equal(direct[["open", "high", "low", "close", "tick_volume"]],
                                  via[["open", "high", "low", "close", "tick_volume"]])
    assert (direct["sub_bars"] == 60).all()


def _import(db, clock, spec, content, **kw):
    args = dict(file_name="t.csv", dialect="generic_utc", tf=Timeframe.M1, time_basis="utc", series_label="t",
                symbol="XAUUSD", source_symbol="XAUUSD", candle_side="bid", price_decimals=2)
    args.update(kw)
    return import_csv(db, clock, spec.schedule, content=content, **args)


def test_csv_import_round_trip_and_idempotency(db, clock, spec):
    frame = synthetic_m1("2026-11-10T13:00Z", "2026-11-10T15:00Z", spec.schedule)
    result = _import(db, clock, spec, csv_bytes(frame))
    assert (result.rows_read, result.rows_stored, result.rows_rejected) == (120, 120, 0)
    assert _import(db, clock, spec, csv_bytes(frame)).already_imported is True
    with db.read() as con:
        row = candle_repo.series_row(con, "csv:t")
        stored = candle_repo.load_candles(con, row["id"], 2, Timeframe.M1, limit=1000)
    assert len(stored) == 120
    assert stored["close"].round(2).tolist() == frame["close"].tolist()


def test_import_with_wrong_timezone_is_refused_and_stores_nothing(db, clock, spec):
    frame = synthetic_m1("2026-11-10T13:00Z", "2026-11-10T15:00Z", spec.schedule)
    shifted = frame.copy()
    shifted.index = shifted.index + pd.Timedelta(hours=8)   # 21:00-23:00 UTC: mostly inside the daily break
    lines = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    for ts, r in shifted.iterrows():
        lines.append(f"{ts:%Y.%m.%d}\t{ts:%H:%M:%S}\t{r['open']}\t{r['high']}\t{r['low']}\t{r['close']}\t10\t0\t25")
    content = "\n".join(lines).encode()
    with pytest.raises(ImportRefused, match="time basis is probably wrong"):
        _import(db, clock, spec, content, dialect="mt5_export", time_basis="utc", point=Decimal("0.01"))
    with db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0


def test_mt5_spread_needs_an_explicit_point_size(db, clock, spec):
    content = b"<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n2026.11.10\t17:00:00\t4000\t4001\t3999\t4000\t5\t0\t25"
    with pytest.raises(CsvFormatError, match="point size"):
        _import(db, clock, spec, content, dialect="mt5_export", time_basis="rule:ny_close")


def test_mt5_export_converts_server_time_and_spread_points(db, clock, spec):
    rows = [f"2026.11.10\t21:{m:02d}:00\t4000.10\t4000.90\t3999.80\t4000.50\t12\t0\t25" for m in range(15)]
    content = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n" + "\n".join(rows)).encode()
    result = _import(db, clock, spec, content, dialect="mt5_export", time_basis="rule:ny_close",
                     point=Decimal("0.01"), series_label="mt5")
    assert result.first == "2026-11-10T19:00:00Z"            # server 21:00 = NY 14:00 (EST) = 19:00 UTC
    with db.read() as con:
        row = candle_repo.series_row(con, "csv:mt5")
        stored = candle_repo.load_candles(con, row["id"], 2, Timeframe.M1)
    assert stored["spread"].iloc[0] == pytest.approx(0.25)   # 25 points x 0.01


def test_generic_csv_rejects_rows_without_offsets(db, clock, spec):
    content = b"timestamp,open,high,low,close\n2026-11-10T15:00:00Z,4000,4001,3999,4000\n2026-11-10T15:01:00,4000,4001,3999,4000"
    result = _import(db, clock, spec, content, series_label="off")
    assert result.rows_stored == 1
    assert result.rejected_by_reason == {"bad_timestamp": 1}
    assert "without a UTC offset" in result.samples[0]


def test_histdata_m1_dialect_uses_fixed_utc_minus_5(db, clock, spec):
    rows = [f"20261110 {9 + m // 60:02d}{m % 60:02d}00;4000.10;4000.90;3999.80;4000.50;0" for m in range(20)]
    result = _import(db, clock, spec, "\n".join(rows).encode(), dialect="histdata_m1", time_basis="fixed:-05:00",
                     series_label="hd")
    assert result.rows_stored == 20
    assert result.first == "2026-11-10T14:00:00Z"            # 09:00 EST (no DST) = 14:00 UTC
    with pytest.raises(CsvFormatError, match="fixed UTC-5"):
        _import(db, clock, spec, b"20261110 090000;1;2;0.5;1;0", dialect="histdata_m1", time_basis="utc",
                series_label="hd2")
