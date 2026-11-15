"""Normalise raw candles into the one internal schema used by every mode.

Canonical candle frame (live and historical alike):
    index   ts           DatetimeIndex, UTC, bar OPEN time, sorted, unique
    open, high, low, close   float64
    tick_volume              Int64 (nullable) - tick count, NOT traded volume
    spread                   float64 (NaN when the source does not record it), price units
    closed                   bool - False only for a bar that is still forming
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

CANDLE_COLUMNS = ("open", "high", "low", "close", "tick_volume", "spread", "closed")
_FIXED = re.compile(r"^fixed:([+-])(\d{2}):(\d{2})$")
NY_CLOSE_SERVER_OFFSET = timedelta(hours=7)  # "New York close" servers: NY local + 7h (UTC+2 / UTC+3 in US DST)


@dataclass(frozen=True)
class TimeBasis:
    """How the timestamps of a source are expressed.

    utc            already UTC (or carry their own offset)
    fixed:+HH:MM   a constant offset from UTC, no DST
    tz:<IANA>      wall-clock time in an IANA zone (DST applies)
    rule:ny_close  trade-server time that follows New York DST at +7h
                   (UTC+2 in winter, UTC+3 in US summer), as many MT5 servers do
    """

    text: str

    @classmethod
    def parse(cls, text: str) -> TimeBasis:
        raw = str(text).strip()
        if raw in ("utc", "rule:ny_close") or _FIXED.match(raw):
            return cls(raw)
        if raw.startswith("tz:"):
            ZoneInfo(raw[3:])  # raises for unknown zones
            return cls(raw)
        raise ValueError(f"unknown time basis {text!r}; use utc, fixed:+HH:MM, tz:<IANA zone> or rule:ny_close")

    def to_utc(self, stamps: pd.Series) -> pd.Series:
        """Convert a Series of datetimes to UTC. Naive values are interpreted by
        this basis; nonexistent/ambiguous local times become NaT (rejected later)."""
        values = pd.to_datetime(stamps, errors="coerce")
        if self.text == "utc":
            if getattr(values.dt, "tz", None) is None:
                return values.dt.tz_localize("UTC")
            return values.dt.tz_convert("UTC")
        if getattr(values.dt, "tz", None) is not None:
            raise ValueError(f"timestamps already carry an offset; use time basis 'utc', not {self.text!r}")
        match = _FIXED.match(self.text)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3))) * sign
            return (values - offset).dt.tz_localize("UTC")
        if self.text == "rule:ny_close":
            local = values - NY_CLOSE_SERVER_OFFSET
            zone = "America/New_York"
        else:
            local = values
            zone = self.text[3:]
        localized = local.dt.tz_localize(zone, ambiguous="NaT", nonexistent="NaT")
        return localized.dt.tz_convert("UTC")


@dataclass
class NormalizeReport:
    rows_in: int
    duplicates_dropped: int


def normalize(raw: pd.DataFrame) -> tuple[pd.DataFrame, NormalizeReport]:
    """Coerce types, sort STABLY by ts, drop duplicate timestamps keeping the LAST
    copy. Adapted from SpicyStock src/market_data.py _download_batch() @ 96ef4732.

    ``raw`` must have a UTC-aware 'ts' column (NaT allowed; validation rejects it).
    """
    frame = raw.copy()
    for column in ("open", "high", "low", "close", "spread"):
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    if "tick_volume" not in frame:
        frame["tick_volume"] = pd.NA
    frame["tick_volume"] = pd.to_numeric(frame["tick_volume"], errors="coerce").astype("Float64")
    if "closed" not in frame:
        frame["closed"] = True
    frame["closed"] = frame["closed"].astype(bool)
    frame = frame.sort_values("ts", kind="stable", na_position="first")
    has_ts = frame["ts"].notna()
    duplicated = frame["ts"].duplicated(keep="last") & has_ts
    dropped = int(duplicated.sum())
    frame = frame[~duplicated]
    return frame[["ts", *CANDLE_COLUMNS]].reset_index(drop=True), NormalizeReport(len(raw), dropped)


def to_canonical(frame: pd.DataFrame) -> pd.DataFrame:
    """Index a validated frame by ts and fix the column dtypes."""
    out = frame.set_index("ts")
    out.index.name = "ts"
    out["tick_volume"] = out["tick_volume"].round().astype("Int64")
    return out[list(CANDLE_COLUMNS)]


def empty_candles() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype="float64") for c in ("open", "high", "low", "close", "spread")})
    frame["tick_volume"] = pd.Series(dtype="Int64")
    frame["closed"] = pd.Series(dtype=bool)
    frame.index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return frame[list(CANDLE_COLUMNS)]
