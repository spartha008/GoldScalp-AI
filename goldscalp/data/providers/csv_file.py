"""Historical candles from CSV files (Mode B).

Dialects
--------
generic_utc
    Comma-separated with a header row. Required columns: timestamp, open, high,
    low, close. Optional: tick_volume, spread (price units). ``timestamp`` must be
    ISO-8601 WITH an offset ("2026-01-05T14:30:00Z" or "+00:00"); a timestamp
    without an offset is rejected, never assumed to be UTC.

mt5_export
    The MetaTrader 5 terminal's bar export: tab-separated with the header
    <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>.
    DATE is YYYY.MM.DD, TIME is HH:MM[:SS], both in the broker's TRADE-SERVER
    time, so the caller must state the time basis (often rule:ny_close).
    <SPREAD> is in POINTS; converting it needs the symbol's point size, which
    must be given explicitly (it differs between brokers). <VOL> (real volume)
    is ignored. Reading this format needs no MetaTrader installation.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal

import numpy as np
import pandas as pd

from goldscalp.data.normalization import TimeBasis

DIALECTS = ("generic_utc", "mt5_export", "histdata_m1")
#: HistData.com documents its ASCII M1 files as EST WITHOUT daylight saving = fixed UTC-5.
HISTDATA_TIME_BASIS = "fixed:-05:00"
_OFFSET = re.compile(r"(?:Z|z|[+-]\d{2}:?\d{2})$")
MT5_COLUMNS = ["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"]


class CsvFormatError(ValueError):
    """The file does not match the declared dialect."""


def decode(content: bytes) -> str:
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvFormatError("file is neither UTF-8 nor UTF-16 text") from exc


def parse(content: bytes, dialect: str, *, time_basis: TimeBasis, point: Decimal | None = None) -> pd.DataFrame:
    """Raw rows with a UTC 'ts' column (NaT where unusable) and canonical price columns."""
    if dialect not in DIALECTS:
        raise CsvFormatError(f"unknown dialect {dialect!r}; expected one of {', '.join(DIALECTS)}")
    text = decode(content)
    if not text.strip():
        raise CsvFormatError("file is empty")
    if dialect == "generic_utc":
        return _generic(text, time_basis)
    if dialect == "histdata_m1":
        return _histdata(text, time_basis)
    return _mt5(text, time_basis, point)


def _histdata(text: str, basis: TimeBasis) -> pd.DataFrame:
    """HistData.com 'Generic ASCII' M1 bars: no header, ``YYYYMMDD HHMMSS;open;high;low;close;volume``.
    Bid prices, fixed UTC-5 (the caller must pass time basis fixed:-05:00)."""
    if basis.text != HISTDATA_TIME_BASIS:
        raise CsvFormatError(f"HistData M1 files are fixed UTC-5; use time basis {HISTDATA_TIME_BASIS!r}")
    frame = pd.read_csv(io.StringIO(text), sep=";", header=None, dtype=str, keep_default_na=False)
    if frame.shape[1] < 5:
        raise CsvFormatError("not a HistData M1 file: expected 'YYYYMMDD HHMMSS;open;high;low;close;volume'")
    naive = pd.to_datetime(frame[0].str.strip(), format="%Y%m%d %H%M%S", errors="coerce")
    out = pd.DataFrame({"ts": basis.to_utc(naive)})
    for index, column in ((1, "open"), (2, "high"), (3, "low"), (4, "close")):
        out[column] = pd.to_numeric(frame[index], errors="coerce")
    out["tick_volume"] = np.nan   # HistData's volume column is 0 for FX/metals; not a tick count
    out["spread"] = np.nan
    out["closed"] = True
    return out


def _generic(text: str, basis: TimeBasis) -> pd.DataFrame:
    if basis.text != "utc":
        raise CsvFormatError("generic_utc files carry their own offsets; use time basis 'utc'")
    frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    frame.columns = [c.strip().lower() for c in frame.columns]
    required = {"timestamp", "open", "high", "low", "close"}
    if not required <= set(frame.columns):
        raise CsvFormatError(f"generic_utc needs columns {sorted(required)}; found {list(frame.columns)}")
    stamps = frame["timestamp"].str.strip()
    has_offset = stamps.str.contains(_OFFSET, regex=True)
    parsed = pd.to_datetime(stamps.where(has_offset), utc=True, errors="coerce", format="ISO8601")
    out = pd.DataFrame({"ts": parsed})
    for column in ("open", "high", "low", "close"):
        out[column] = pd.to_numeric(frame[column], errors="coerce")
    out["tick_volume"] = pd.to_numeric(frame["tick_volume"], errors="coerce") if "tick_volume" in frame else np.nan
    out["spread"] = pd.to_numeric(frame["spread"], errors="coerce") if "spread" in frame else np.nan
    out["closed"] = True
    out.attrs["rows_without_offset"] = int((~has_offset).sum())
    return out


def _mt5(text: str, basis: TimeBasis, point: Decimal | None) -> pd.DataFrame:
    frame = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, keep_default_na=False)
    frame.columns = [c.strip().upper() for c in frame.columns]
    missing = [c for c in ("<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>") if c not in frame.columns]
    if missing:
        raise CsvFormatError(f"not an MT5 bar export: missing {missing}; expected tab-separated {' '.join(MT5_COLUMNS)}")
    dates = frame["<DATE>"].str.strip()
    times = frame["<TIME>"].str.strip() if "<TIME>" in frame.columns else pd.Series("00:00:00", index=frame.index)
    times = times.where(times.str.count(":") >= 2, times + ":00")
    naive = pd.to_datetime(dates + " " + times, format="%Y.%m.%d %H:%M:%S", errors="coerce")
    out = pd.DataFrame({"ts": basis.to_utc(naive)})
    for src, dst in (("<OPEN>", "open"), ("<HIGH>", "high"), ("<LOW>", "low"), ("<CLOSE>", "close")):
        out[dst] = pd.to_numeric(frame[src], errors="coerce")
    out["tick_volume"] = pd.to_numeric(frame["<TICKVOL>"], errors="coerce") if "<TICKVOL>" in frame else np.nan
    if "<SPREAD>" in frame.columns and (frame["<SPREAD>"].str.strip() != "").any():
        if point is None:
            raise CsvFormatError("this MT5 export has a <SPREAD> column in points; give the symbol's point size "
                                 "(e.g. 0.01) so spreads can be converted. GoldScalp does not assume it.")
        out["spread"] = pd.to_numeric(frame["<SPREAD>"], errors="coerce") * float(point)
    else:
        out["spread"] = np.nan
    out["closed"] = True
    return out
