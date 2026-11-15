"""Asian, London and New York sessions (definition sessions@1).

Each session is defined in its OWN local timezone so daylight-saving changes
are handled correctly (the UK and US change clocks on different dates):
    Asian     09:00-18:00 Asia/Tokyo        (no DST)
    London    08:00-17:00 Europe/London
    New York  08:00-17:00 America/New_York
Windows are configurable conventions, not exchange facts; overlaps are derived.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from goldscalp.analysis.candles import candle_id, closed_view, fmt
from goldscalp.core.clock import UTC, ensure_aware, iso

VERSION = "sessions@1"


@dataclass(frozen=True)
class SessionDef:
    key: str
    name: str
    zone: str
    start: time
    end: time

    def window(self, local_day: date) -> tuple[datetime, datetime]:
        tz = ZoneInfo(self.zone)
        start = datetime.combine(local_day, self.start, tzinfo=tz).astimezone(UTC)
        end = datetime.combine(local_day, self.end, tzinfo=tz).astimezone(UTC)
        return start, end


SESSIONS: tuple[SessionDef, ...] = (
    SessionDef("asia", "Asian", "Asia/Tokyo", time(9, 0), time(18, 0)),
    SessionDef("london", "London", "Europe/London", time(8, 0), time(17, 0)),
    SessionDef("new_york", "New York", "America/New_York", time(8, 0), time(17, 0)),
)


def current_or_last_window(session: SessionDef, now: datetime) -> tuple[datetime, datetime]:
    """The window that is active at ``now``, else the most recent one that started before ``now``.
    Weekend days are skipped (spot gold does not trade then)."""
    now = ensure_aware(now)
    local_today = now.astimezone(ZoneInfo(session.zone)).date()
    for back in range(0, 8):
        day = local_today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start, end = session.window(day)
        if start <= now:
            return start, end
    raise RuntimeError("no session window found")  # pragma: no cover


def summary(frame: pd.DataFrame, now: datetime) -> dict[str, Any]:
    """Status and price range of each session's current or most recent window."""
    now = ensure_aware(now)
    out = []
    for s in SESSIONS:
        start, end = current_or_last_window(s, now)
        active = start <= now < end
        bars = frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(min(end, now)))]
        item: dict[str, Any] = {
            "key": s.key, "name": s.name, "zone": s.zone,
            "local_hours": f"{s.start.strftime('%H:%M')}-{s.end.strftime('%H:%M')}",
            "start": iso(start), "end": iso(end), "status": "active" if active else "completed",
            "bars": int(len(bars)),
        }
        if len(bars):
            hi_t, lo_t = bars["high"].idxmax(), bars["low"].idxmin()
            item.update(open=float(bars["open"].iloc[0]), high=float(bars["high"].max()),
                        low=float(bars["low"].min()), last=float(bars["close"].iloc[-1]),
                        range=round(float(bars["high"].max() - bars["low"].min()), 5),
                        high_time=iso(hi_t.to_pydatetime()), low_time=iso(lo_t.to_pydatetime()))
        out.append(item)
    active_names = [o["name"] for o in out if o["status"] == "active"]
    return {"definition": VERSION, "now": iso(now), "active": active_names,
            "overlap": len(active_names) > 1, "sessions": out}


# ---------------------------------------------------------------------------------------------
# sessions@2: configurable windows (config/analysis.json), overlaps, current session, ranges,
# per-session volatility comparison and breakout observations.
#
# Timezones: each window is defined in its own IANA zone and converted to UTC for every date, so
# daylight-saving changes are handled per session. Candle timestamps are UTC (MT5 server time is
# converted to UTC on ingestion; server time is never assumed to be UTC). The display timezone is a
# separate UI setting and does not affect any calculation here.
# ---------------------------------------------------------------------------------------------
VERSION2 = "sessions@2"
TIMEZONE_NOTE = ("Session windows are defined in each session's own IANA timezone and converted to UTC per "
                 "date (daylight-saving correct). Candle times are UTC; MT5 server time is converted on "
                 "ingestion and is never assumed to be UTC.")


def definitions(cfg: Any) -> tuple[SessionDef, ...]:
    out = []
    for d in cfg["sessions"]["definitions"]:
        sh, sm = (int(x) for x in d["start"].split(":"))
        eh, em = (int(x) for x in d["end"].split(":"))
        out.append(SessionDef(d["key"], d["name"], d["timezone"], time(sh, sm), time(eh, em)))
    return tuple(out)


def _window(defn: SessionDef, local_day: date) -> tuple[datetime, datetime]:
    start, end = defn.window(local_day)
    if end <= start:                     # a window that crosses local midnight
        end = defn.window(local_day + timedelta(days=1))[1]
    return start, end


def recent_windows(defn: SessionDef, as_of: datetime, count: int) -> list[tuple[datetime, datetime]]:
    """The ``count`` most recent weekday windows that STARTED at or before ``as_of`` (oldest first)."""
    as_of = ensure_aware(as_of)
    local_today = as_of.astimezone(ZoneInfo(defn.zone)).date()
    out: list[tuple[datetime, datetime]] = []
    back = 0
    while len(out) < count and back < count * 2 + 10:
        day = local_today - timedelta(days=back)
        back += 1
        if day.weekday() >= 5:
            continue
        start, end = _window(defn, day)
        if start <= as_of:
            out.append((start, end))
    return out[::-1]


def active_names(defs: tuple[SessionDef, ...], at: datetime) -> list[str]:
    """Sessions whose latest window (started at or before ``at``) contains ``at``."""
    at = ensure_aware(at)
    out = []
    for d in defs:
        wins = recent_windows(d, at, 1)
        if wins and wins[-1][0] <= at < wins[-1][1]:
            out.append(d.name)
    return out


def bars_in(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    return frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]


def range_item(bars: pd.DataFrame) -> dict[str, Any]:
    if bars is None or bars.empty:
        return {"bars": 0}
    hi_t, lo_t = bars["high"].idxmax(), bars["low"].idxmin()
    high, low = float(bars["high"].max()), float(bars["low"].min())
    return {"bars": int(len(bars)), "open": float(bars["open"].iloc[0]), "high": high, "low": low,
            "last": float(bars["close"].iloc[-1]), "range": round(high - low, 5),
            "high_time": iso(hi_t.to_pydatetime()), "low_time": iso(lo_t.to_pydatetime())}


def analyse(frame: pd.DataFrame, tf: Any, as_of: datetime, cfg: Any, *, history: pd.DataFrame | None = None,
            history_tf: Any = None, atr_value: float | None = None) -> dict[str, Any]:
    """Session observations as of ``as_of`` from CLOSED candles only.

    ``frame``: candles of ``tf`` (M5 or M1) for ranges and breakouts. ``history`` (optional, e.g. H1)
    supplies the volatility comparison over the previous ``compare_days`` windows.
    """
    as_of = ensure_aware(as_of)
    defs = definitions(cfg)
    bars = closed_view(frame, tf, as_of)
    hist = closed_view(history, history_tf, as_of) if history is not None and history_tf is not None else bars
    compare_days = int(cfg["sessions"]["compare_days"])
    items: list[dict[str, Any]] = []
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for d in defs:
        wins = recent_windows(d, as_of, compare_days + 1)
        windows[d.key] = wins
        start, end = wins[-1]
        active = start <= as_of < end
        item: dict[str, Any] = {"key": d.key, "name": d.name, "zone": d.zone,
                                "local_hours": f"{d.start.strftime('%H:%M')}-{d.end.strftime('%H:%M')}",
                                "start": iso(start), "end": iso(end),
                                "status": "active" if active else "completed",
                                "known_at": None if active else iso(end)}
        item.update(range_item(bars_in(bars, start, min(end, as_of))))
        if atr_value and item.get("range") is not None:
            item["range_vs_atr"] = round(item["range"] / atr_value, 2)
        past = [r["range"] for r in (range_item(bars_in(hist, ws, we)) for ws, we in wins[:-1]) if r["bars"]]
        compare: dict[str, Any] = {"windows": len(past),
                                   "average_range": round(sum(past) / len(past), 5) if past else None}
        if past and item.get("range") is not None and compare["average_range"]:
            ratio = item["range"] / compare["average_range"]
            compare["ratio"] = round(ratio, 2)
            compare["note"] = (f"range {'so far ' if active else ''}is {ratio:.0%} of the average {d.name} "
                               f"range over the previous {len(past)} sessions")
        item["compare"] = compare
        items.append(item)

    active_names = [i["name"] for i in items if i["status"] == "active"]
    overlaps = []
    for a_i, a in enumerate(defs):
        for b in defs[a_i + 1:]:
            (sa, ea), (sb, eb) = windows[a.key][-1], windows[b.key][-1]
            start, end = max(sa, sb), min(ea, eb)
            if start < end:
                overlaps.append({"sessions": [a.name, b.name], "start": iso(start), "end": iso(end),
                                 "status": "active" if start <= as_of < end else
                                 ("completed" if end <= as_of else "upcoming")})
    return {"definition": VERSION2, "as_of": iso(as_of), "timeframe": getattr(tf, "value", None),
            "active": active_names, "overlap": len(active_names) > 1, "overlaps": overlaps,
            "current": " / ".join(active_names) if active_names else "between sessions",
            "next": None if active_names else _next_start(defs, as_of),
            "sessions": items, "breakouts": _breakouts(bars, tf, defs, windows, as_of),
            "timezone_note": TIMEZONE_NOTE}


def _next_start(defs: tuple[SessionDef, ...], as_of: datetime) -> dict[str, Any] | None:
    found = []
    for d in defs:
        local_today = as_of.astimezone(ZoneInfo(d.zone)).date()
        for ahead in range(0, 8):
            day = local_today + timedelta(days=ahead)
            if day.weekday() >= 5:
                continue
            start, _ = _window(d, day)
            if start > as_of:
                found.append((start, d.name))
                break
    if not found:
        return None
    start, name = min(found)
    return {"name": name, "start": iso(start)}


def _breakouts(bars: pd.DataFrame, tf: Any, defs: tuple[SessionDef, ...],
               windows: dict[str, list[tuple[datetime, datetime]]], as_of: datetime) -> list[dict[str, Any]]:
    """First CLOSE beyond another session's completed range, per direction, during each session's latest
    window. Reference = the latest window of another session that started before the target window; it is
    used only once it is complete by ``as_of`` and only if it ended within 24 h of the target's start."""
    out: list[dict[str, Any]] = []
    names = {d.key: d.name for d in defs}
    for target in defs:
        t_start, t_end = windows[target.key][-1]
        for ref in defs:
            if ref.key == target.key:
                continue
            # the latest window of the other session that started before this one; it must be complete
            ref_win = next(((s, e) for s, e in reversed(windows[ref.key]) if s < t_start), None)
            if ref_win is None or ref_win[1] > as_of or ref_win[1] <= t_start - timedelta(hours=24):
                continue
            r_start, r_end = ref_win
            ref_bars = bars_in(bars, r_start, r_end)
            if ref_bars.empty:
                continue
            high, low = float(ref_bars["high"].max()), float(ref_bars["low"].min())
            scan = bars_in(bars, max(r_end, t_start), min(t_end, as_of))
            closes = scan["close"].to_numpy(dtype="float64")
            for direction, hit in (("bullish", closes > high), ("bearish", closes < low)):
                if not hit.any():
                    continue
                pos = int(hit.argmax())
                ts = scan.index[pos]
                level = high if direction == "bullish" else low
                side, word = ("above", "high") if direction == "bullish" else ("below", "low")
                out.append({
                    "id": f"{tf.value}:session_breakout:{target.key}:{ref.key}:{direction}:{iso(ts.to_pydatetime())}",
                    "kind": "session", "type": "session_breakout", "direction": direction, "timeframe": tf.value,
                    "time": iso(ts.to_pydatetime()), "price": level, "status": "confirmed",
                    "known_at": iso((ts + pd.Timedelta(seconds=tf.seconds)).to_pydatetime()),
                    "session": names[target.key], "reference_session": names[ref.key],
                    "explanation": (f"During the {names[target.key]} session a candle closed {fmt(float(closes[pos]))} "
                                    f"{side} the {names[ref.key]} session {word} {fmt(level)} "
                                    f"({iso(r_start)} to {iso(r_end)}). A close beyond a range is an observation, "
                                    "not a sweep and not a continuation call."),
                    "candles": [candle_id(tf, ts)], "definition": VERSION2})
    out.sort(key=lambda o: (o["known_at"], o["id"]))
    return out
