"""XAUUSD trading-week schedule.

Spot gold / CFD venues commonly follow a COMEX/Globex-style week: open Sunday
evening New York time, close Friday 17:00 New York time, with a daily break
between the daily close and reopen. Venues differ, so the schedule is part of
the instrument specification (config/instruments/*.json), never hard-coded.

Holidays are NOT modelled in Phase 1: holiday closures show up as unexpected
gaps in the gap report (documented limitation).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from collections.abc import Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldscalp.core.clock import UTC, ensure_aware

_SUNDAY, _FRIDAY, _SATURDAY = 6, 4, 5


@dataclass(frozen=True)
class TradingSchedule:
    timezone: str          # IANA zone the times are stated in, e.g. America/New_York
    daily_close: time      # trading stops at this local time (Mon-Fri)
    daily_reopen: time     # trading restarts at this local time (Sun-Thu)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TradingSchedule:
        expected = {"timezone", "daily_close", "daily_reopen"}
        if not isinstance(data, Mapping) or set(data) != expected:
            got = sorted(data) if isinstance(data, Mapping) else data
            raise ValueError(f"schedule must have exactly {sorted(expected)}, got {got}")
        ZoneInfo(str(data["timezone"]))  # raises for an unknown zone
        close, reopen = time.fromisoformat(str(data["daily_close"])), time.fromisoformat(str(data["daily_reopen"]))
        if not close < reopen:
            raise ValueError("schedule daily_close must be earlier than daily_reopen (same local day)")
        return cls(timezone=str(data["timezone"]), daily_close=close, daily_reopen=reopen)

    def to_mapping(self) -> dict[str, str]:
        return {"timezone": self.timezone, "daily_close": self.daily_close.strftime("%H:%M"),
                "daily_reopen": self.daily_reopen.strftime("%H:%M")}

    # ------------------------------------------------------------------ scalar

    def is_open(self, at: datetime) -> bool:
        return self.status(at)["open"]

    def status(self, at: datetime) -> dict[str, Any]:
        """Whether the market is open at ``at`` and, if not, why and until when."""
        local = ensure_aware(at).astimezone(ZoneInfo(self.timezone))
        wd, t = local.weekday(), local.time()
        if wd == _SATURDAY or (wd == _SUNDAY and t < self.daily_reopen) or (wd == _FRIDAY and t >= self.daily_close):
            reason = "weekly_close"
        elif self.daily_close <= t < self.daily_reopen and wd not in (_SATURDAY, _SUNDAY):
            reason = "daily_break"
        else:
            return {"open": True, "reason": "open", "next_change": self._next_close(local)}
        return {"open": False, "reason": reason, "next_change": self._next_open(local)}

    def _next_open(self, local: datetime) -> datetime:
        probe = local
        for _ in range(8):
            candidate = probe.replace(hour=self.daily_reopen.hour, minute=self.daily_reopen.minute,
                                      second=0, microsecond=0)
            if candidate > local and candidate.weekday() in (_SUNDAY, 0, 1, 2, 3):
                return candidate.astimezone(UTC)
            probe = (probe + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise RuntimeError("no reopen found within a week")  # pragma: no cover

    def _next_close(self, local: datetime) -> datetime:
        probe = local
        for _ in range(8):
            candidate = probe.replace(hour=self.daily_close.hour, minute=self.daily_close.minute,
                                      second=0, microsecond=0)
            if candidate > local and candidate.weekday() in (0, 1, 2, 3, _FRIDAY):
                return candidate.astimezone(UTC)
            probe = (probe + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise RuntimeError("no close found within a week")  # pragma: no cover

    # -------------------------------------------------------------- vectorised

    def open_mask(self, minutes_utc: pd.DatetimeIndex) -> np.ndarray:
        """Boolean array: is the market open at each UTC minute start."""
        local = minutes_utc.tz_convert(self.timezone)
        wd = np.asarray(local.day_of_week)
        mins = np.asarray(local.hour) * 60 + np.asarray(local.minute)
        close = self.daily_close.hour * 60 + self.daily_close.minute
        reopen = self.daily_reopen.hour * 60 + self.daily_reopen.minute
        closed = (wd == _SATURDAY) | ((wd == _SUNDAY) & (mins < reopen)) | ((wd == _FRIDAY) & (mins >= close))
        brk = (mins >= close) & (mins < reopen) & (wd != _SATURDAY) & (wd != _SUNDAY)
        return ~(closed | brk)

    def expected_bars(self, start: datetime, end: datetime, bar_seconds: int) -> pd.DatetimeIndex:
        """Bar open times in [start, end) during which the market is open for at
        least one minute. ``start`` is floored to the bar size."""
        start, end = ensure_aware(start), ensure_aware(end)
        if end <= start:
            return pd.DatetimeIndex([], tz=UTC)
        s = int(start.timestamp()) // bar_seconds * bar_seconds
        minutes = pd.date_range(pd.Timestamp(s, unit="s", tz=UTC), pd.Timestamp(end), freq="min",
                                inclusive="left")
        if len(minutes) == 0:
            return pd.DatetimeIndex([], tz=UTC)
        mask = self.open_mask(minutes)
        epochs = minutes.as_unit("s").asi8
        buckets = epochs[mask] // bar_seconds * bar_seconds
        return pd.DatetimeIndex(pd.to_datetime(np.unique(buckets), unit="s", utc=True))
