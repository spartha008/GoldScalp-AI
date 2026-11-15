"""Time handling: every instant is offset-aware and normalised to UTC.

Naive datetimes and offset-less ISO strings are refused rather than guessed
(adapted from SpicyStock src/sessions.py market_time() and src/timing.py
_instant()). Local wall-clock times that do not exist or are ambiguous across a
DST change are refused (adapted from SpicyStock src/morning_halts.py _eastern()).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

__all__ = ["UTC", "Clock", "SystemClock", "FixedClock", "LocalTimeError", "ensure_aware", "parse_iso_aware",
           "local_to_utc", "iso"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A controllable clock for tests and replays."""

    def __init__(self, at: datetime) -> None:
        self._at = ensure_aware(at, "FixedClock start")

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = ensure_aware(at, "FixedClock.set")

    def advance(self, **delta: float) -> datetime:
        self._at = self._at + timedelta(**delta)
        return self._at


class LocalTimeError(ValueError):
    """A local wall-clock time that does not map to exactly one instant."""


def ensure_aware(value: datetime, name: str = "datetime") -> datetime:
    """``value`` in UTC; a naive datetime is refused, never assumed to be UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an offset-aware datetime, got {value!r}")
    return value.astimezone(UTC)


def parse_iso_aware(text: str, name: str = "timestamp") -> datetime:
    """An ISO-8601 string WITH an offset (or 'Z'), as UTC. Offset-less is refused."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{name} must be a non-empty ISO-8601 string")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} {text!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} {text!r} has no UTC offset; refusing to guess its timezone")
    return parsed.astimezone(UTC)


def local_to_utc(naive: datetime, zone: str) -> datetime:
    """Interpret a naive wall-clock time in ``zone`` and return UTC.

    Refuses times that fall in a DST gap (nonexistent) or a DST overlap
    (ambiguous) instead of silently choosing one.
    """
    if naive.tzinfo is not None:
        raise ValueError("local_to_utc expects a naive wall-clock datetime")
    tz = ZoneInfo(zone)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    # Non-existence first: a time in the spring-forward gap also has differing fold offsets.
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        raise LocalTimeError(f"{naive.isoformat()} does not exist in {zone} (DST gap)")
    if first.utcoffset() != second.utcoffset():
        raise LocalTimeError(f"{naive.isoformat()} is ambiguous in {zone} (DST overlap)")
    return first.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    """Canonical UTC ISO string ('...Z') or None."""
    if value is None:
        return None
    return ensure_aware(value).isoformat().replace("+00:00", "Z")
