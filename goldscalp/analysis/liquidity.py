"""Liquidity levels and sweep observations (definition liquidity@1).

Levels (each carries the time it became knowable, ``known_at``):
- EQH / EQL: two confirmed swing highs (lows) at least ``min_separation_bars`` apart whose prices differ
  by no more than tolerance = max(equal_tolerance_atr x ATR, equal_tolerance_min), with no candle in
  between trading beyond the higher (lower) of the two. Level = the higher high (lower low).
  Known when the second swing is confirmed.
- PDH / PDL: previous COMPLETE trading day (17:00 New York rollover). Known at that day's 17:00 NY close.
- PWH / PWL: previous COMPLETE trading week (Monday to Friday trading days). Known at Friday 17:00 NY.
- Session highs/lows: the most recent COMPLETED window of each configured session. Known at the window
  end. The active session's high/low so far is reported as DEVELOPING and never classified.

Interaction with a level (first interaction only, candles that OPEN at or after ``known_at``):
for a level above price (mirror for levels below)
- high beyond the level and the SAME candle closes back at/below it
    -> "potential bearish liquidity sweep: confirmed rejection"
- a candle CLOSES beyond the level -> "unconfirmed level break" (developing) until either
    - a close back at/below the level within ``sweep_return_bars`` candles
      -> "potential bearish liquidity sweep: delayed rejection", or
    - ``sweep_return_bars`` candles close without returning -> "confirmed break (acceptance)", not a sweep.
A breakout is never assumed to be a sweep.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldscalp.analysis import market_structure
from goldscalp.analysis import sessions as sessions_mod
from goldscalp.analysis.candles import Stamps, atr_series, candle_id, close_time, closed_view, fmt, rounded
from goldscalp.analysis.config import AnalysisConfig, default_config
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import Timeframe

VERSION = "liquidity@1"
NEW_YORK = ZoneInfo("America/New_York")
HIGH_SIDE = {"EQH", "PDH", "PWH", "session_high"}


def day_key(index: pd.DatetimeIndex) -> np.ndarray:
    """Trading-day key: the New York date after shifting by +7 h (the day rolls at 17:00 New York)."""
    return np.asarray((index.tz_convert(NEW_YORK) + pd.Timedelta(hours=7)).date)


def day_close(key: date) -> datetime:
    return datetime.combine(key, time(17, 0), tzinfo=NEW_YORK).astimezone(ZoneInfo("UTC"))


def analyse(frame: pd.DataFrame, tf: Timeframe, as_of: datetime | None = None, cfg: AnalysisConfig | None = None,
            *, structure: dict[str, Any] | None = None, reference: pd.DataFrame | None = None,
            reference_tf: Timeframe | None = None) -> dict[str, Any]:
    """``frame``: candles of ``tf``. ``reference`` (normally H1) supplies daily, weekly and session
    levels when ``frame`` does not cover them. ``structure`` must be a full (limit=0) structure@2 result."""
    cfg = cfg or default_config()
    lcfg = cfg["liquidity"]
    bars = closed_view(frame, tf, as_of)
    if as_of is None and bars is not None and len(bars):
        as_of = close_time(tf, bars.index[-1]).to_pydatetime()
    out: dict[str, Any] = {"definition": VERSION, "timeframe": tf.value, "as_of": iso(as_of) if as_of else None,
                           "levels": [], "events": [], "developing": [], "nearest_above": None,
                           "nearest_below": None,
                           "params": {k: lcfg[k] for k in ("equal_tolerance_atr", "equal_tolerance_min",
                                                           "min_separation_bars", "sweep_return_bars")}}
    if bars is None or bars.empty:
        return out
    as_of = ensure_aware(as_of)
    if structure is None:
        structure = market_structure.analyse(frame, tf, as_of, cfg, limit=0)
    ref_tf = reference_tf or tf
    ref = closed_view(reference, ref_tf, as_of) if reference is not None else bars
    atr = atr_series(bars, int(cfg["volatility"]["atr_period"]))

    levels = _equal_levels(bars, tf, structure["swings"], atr, lcfg)
    levels += _period_levels(bars, tf, ref, ref_tf, as_of)
    developing: list[dict[str, Any]] = []
    levels += _session_levels(bars, tf, ref, ref_tf, as_of, cfg, developing)

    bars_s = _Series(bars, tf)
    ref_s = _Series(ref, ref_tf) if ref is not None and len(ref) and ref is not bars else None
    for level in levels:
        level["interaction"] = _interaction(level, bars_s, ref_s, int(lcfg["sweep_return_bars"]))
        level["status"] = _status(level["interaction"])
    levels = _merge_equal(levels)
    levels.sort(key=lambda lv: (lv["known_at"], lv["id"]))
    events = sorted((lv["interaction"] for lv in levels if lv["interaction"]),
                    key=lambda e: (e["known_at"] or "9999", e["id"]))
    last = float(bars["close"].iloc[-1])
    active = [lv for lv in levels if lv["status"] in ("untouched", "testing")]
    above = [lv for lv in active if lv["price"] > last]
    below = [lv for lv in active if lv["price"] < last]
    cap = int(lcfg["max_levels_returned"])
    out.update(levels=levels[-cap:] if cap else levels, events=events, developing=developing,
               nearest_above=_public(min(above, key=lambda lv: lv["price"])) if above else None,
               nearest_below=_public(max(below, key=lambda lv: lv["price"])) if below else None,
               last_close=rounded(last))
    return out


# -------------------------------------------------------------------------------- levels
def _equal_levels(bars: pd.DataFrame, tf: Timeframe, swings: list[dict[str, Any]], atr: np.ndarray,
                  lcfg: dict[str, Any]) -> list[dict[str, Any]]:
    stamps = Stamps(bars.index, tf)
    pos = {s: i for i, s in enumerate(stamps.open)}
    close_pos = {s: i for i, s in enumerate(stamps.close)}
    highs, lows = bars["high"].to_numpy(dtype="float64"), bars["low"].to_numpy(dtype="float64")
    min_sep = int(lcfg["min_separation_bars"])
    out: list[dict[str, Any]] = []
    for kind, typ in (("swing_high", "EQH"), ("swing_low", "EQL")):
        group = [s for s in swings if s["type"] == kind and s["time"] in pos]
        for n, s in enumerate(group):
            i_s = pos[s["time"]]
            confirm = close_pos.get(s["known_at"], i_s)
            a = atr[confirm]
            tol = max(float(lcfg["equal_tolerance_atr"]) * a if np.isfinite(a) else 0.0,
                      float(lcfg["equal_tolerance_min"]))
            for p in reversed(group[max(0, n - 5):n]):
                i_p = pos[p["time"]]
                if i_s - i_p < min_sep or abs(s["price"] - p["price"]) > tol:
                    continue
                if typ == "EQH":
                    level = max(p["price"], s["price"])
                    clean = i_s - i_p < 2 or highs[i_p + 1:i_s].max() <= level
                else:
                    level = min(p["price"], s["price"])
                    clean = i_s - i_p < 2 or lows[i_p + 1:i_s].min() >= level
                if not clean:
                    continue
                side = "buy-side" if typ == "EQH" else "sell-side"
                word = "highs" if typ == "EQH" else "lows"
                out.append(_level(
                    f"{tf.value}:{typ}:{p['time']}:{s['time']}", typ, f"Equal {word}", level, tf, s["known_at"],
                    [p["candles"][0], s["candles"][0]],
                    (f"Equal {word}: swing {word[:-1]}s {fmt(p['price'])} ({p['time']}) and {fmt(s['price'])} "
                     f"({s['time']}) differ by {fmt(abs(s['price'] - p['price']), 3)} (tolerance {fmt(tol, 3)}). "
                     f"Resting {side} liquidity is commonly assumed beyond such levels; this is an observation."),
                    extra={"members": [p["time"], s["time"]], "tolerance": rounded(tol)}))
                break
    return out


def _period_levels(bars: pd.DataFrame, tf: Timeframe, ref: pd.DataFrame, ref_tf: Timeframe,
                   as_of: datetime) -> list[dict[str, Any]]:
    src, src_tf = (bars, tf) if len(bars) and len(ref) == 0 else (ref, ref_tf)
    if src is None or src.empty:
        return []
    keys = day_key(src.index)
    current = day_key(pd.DatetimeIndex([pd.Timestamp(as_of)]))[0]
    out: list[dict[str, Any]] = []
    past_days = sorted({k for k in keys if k < current})
    if past_days:
        d = past_days[-1]
        day_bars = src[keys == d]
        out += _hl_pair("PDH", "PDL", "previous trading day", day_bars, src_tf, day_close(d),
                        f"trading day {d.isoformat()} (17:00 New York rollover)")
    cur_week = current.isocalendar()[:2]
    weeks = sorted({k.isocalendar()[:2] for k in keys if k.isocalendar()[:2] < cur_week})
    if weeks:
        y, w = weeks[-1]
        mask = np.array([k.isocalendar()[:2] == (y, w) for k in keys])
        friday = date.fromisocalendar(y, w, 5)
        out += _hl_pair("PWH", "PWL", "previous trading week", src[mask], src_tf, day_close(friday),
                        f"trading week {y}-W{w:02d}")
    return out


def _hl_pair(high_type: str, low_type: str, what: str, period: pd.DataFrame, tf: Timeframe, known: datetime,
             span: str) -> list[dict[str, Any]]:
    if period.empty:
        return []
    hi_t, lo_t = period["high"].idxmax(), period["low"].idxmin()
    hi, lo = float(period["high"].max()), float(period["low"].min())
    note = f"{span}; {len(period)} {tf.value} candles from {iso(period.index[0].to_pydatetime())}"
    return [
        _level(f"{high_type}:{iso(known)}", high_type, f"{what.capitalize()} high", hi, tf, iso(known),
               [candle_id(tf, hi_t)], f"{what.capitalize()} high {fmt(hi)} at {iso(hi_t.to_pydatetime())} ({note}).",
               extra={"level_time": iso(hi_t.to_pydatetime())}),
        _level(f"{low_type}:{iso(known)}", low_type, f"{what.capitalize()} low", lo, tf, iso(known),
               [candle_id(tf, lo_t)], f"{what.capitalize()} low {fmt(lo)} at {iso(lo_t.to_pydatetime())} ({note}).",
               extra={"level_time": iso(lo_t.to_pydatetime())}),
    ]


def _session_levels(bars: pd.DataFrame, tf: Timeframe, ref: pd.DataFrame, ref_tf: Timeframe, as_of: datetime,
                    cfg: AnalysisConfig, developing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in sessions_mod.definitions(cfg):
        for start, end in reversed(sessions_mod.recent_windows(d, as_of, 2)):
            covered = len(bars) and bars.index[0] <= pd.Timestamp(start)
            src, src_tf = (bars, tf) if covered else (ref, ref_tf)
            window = sessions_mod.bars_in(src, start, min(end, as_of))
            if window.empty:
                continue
            hi_t, lo_t = window["high"].idxmax(), window["low"].idxmin()
            hi, lo = float(window["high"].max()), float(window["low"].min())
            span = f"{d.name} session {iso(start)} to {iso(end)}"
            if end > as_of:
                for typ, price, at in (("session_high", hi, hi_t), ("session_low", lo, lo_t)):
                    developing.append({
                        "id": f"{typ}:{d.key}:{iso(start)}:developing", "kind": "liquidity", "type": typ,
                        "label": f"{d.name} {'high' if typ == 'session_high' else 'low'} (so far)", "price": rounded(price),
                        "direction": "neutral", "timeframe": src_tf.value, "time": iso(at.to_pydatetime()),
                        "status": "developing", "known_at": None, "session": d.name,
                        "explanation": f"{span} is still active; its {'high' if typ == 'session_high' else 'low'} "
                                       f"so far is {fmt(price)} and may still change.",
                        "candles": [candle_id(src_tf, at)], "definition": VERSION})
                continue
            for typ, price, at in (("session_high", hi, hi_t), ("session_low", lo, lo_t)):
                word = "high" if typ == "session_high" else "low"
                out.append(_level(f"{typ}:{d.key}:{iso(start)}", typ, f"{d.name} session {word}", price, src_tf,
                                  iso(end), [candle_id(src_tf, at)],
                                  f"{d.name} session {word} {fmt(price)} at {iso(at.to_pydatetime())} ({span}).",
                                  extra={"session": d.name, "level_time": iso(at.to_pydatetime())}))
            break
    return out


def _level(level_id: str, typ: str, label: str, price: float, tf: Timeframe, known_at: str, candles: list[str],
           explanation: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    side = "buy-side" if typ in HIGH_SIDE else "sell-side"
    item = {"id": level_id, "kind": "liquidity", "type": typ, "label": label, "side": side,
            "direction": "neutral", "timeframe": tf.value, "price": rounded(price), "status": "untouched",
            "known_at": known_at, "explanation": explanation, "candles": candles, "definition": VERSION}
    item.update(extra or {})
    return item


# -------------------------------------------------------------------------------- interactions
class _Series:
    """Arrays of one candle frame, prepared once per analysis call."""

    def __init__(self, frame: pd.DataFrame, tf: Timeframe) -> None:
        self.index = frame.index
        self.h, self.low, self.c = (frame[k].to_numpy(dtype="float64") for k in ("high", "low", "close"))
        self.stamps = Stamps(frame.index, tf)
        self.tf = tf


def _interaction(level: dict[str, Any], bars: _Series, ref: _Series | None, return_bars: int) -> dict[str, Any] | None:
    known = pd.Timestamp(level["known_at"])
    if len(bars.index) and bars.index[0] > known and ref is not None and len(ref.index):
        # candles between the level and the start of this timeframe's history: check the reference
        lo = int(ref.index.searchsorted(known, side="left"))
        ends = ref.index + pd.Timedelta(seconds=ref.tf.seconds)
        hi = int(ends.searchsorted(bars.index[0], side="right"))
        if hi > lo:
            found = _classify(level, ref, lo, hi, return_bars)
            if found is not None and found["status"] == "confirmed":
                found["explanation"] += f" (checked on {ref.tf.value}: {bars.tf.value} candles start later)"
                return found
    lo = int(bars.index.searchsorted(known, side="left"))
    return _classify(level, bars, lo, len(bars.index), return_bars)


def classify(level: dict[str, Any], scan: pd.DataFrame, tf: Timeframe, return_bars: int) -> dict[str, Any] | None:
    """The first interaction of price with ``level`` in ``scan`` (closed candles after the level was known)."""
    if scan is None or scan.empty:
        return None
    return _classify(level, _Series(scan, tf), 0, len(scan), return_bars)


def _classify(level: dict[str, Any], s: _Series, lo: int, hi: int, return_bars: int) -> dict[str, Any] | None:
    if hi <= lo:
        return None
    price = float(level["price"])
    high_side = level["type"] in HIGH_SIDE
    h, low, c = s.h[lo:hi], s.low[lo:hi], s.c[lo:hi]
    pierced = h > price if high_side else low < price
    if not pierced.any():
        return None
    j = int(pierced.argmax())
    beyond = (c > price) if high_side else (c < price)
    n = hi - lo
    st, tf = s.stamps, s.tf
    t_j = st.open[lo + j]
    word, back_word = ("above", "below") if high_side else ("below", "above")
    sweep_dir = "bearish" if high_side else "bullish"
    break_dir = "bullish" if high_side else "bearish"
    base = {"kind": "liquidity", "level_id": level["id"], "level_type": level["type"], "level_label": level["label"],
            "price": level["price"], "timeframe": tf.value, "time": t_j, "definition": VERSION}
    extreme = h[j] if high_side else low[j]
    if not beyond[j]:
        return {**base, "id": f"{level['id']}:sweep:{t_j}", "type": "sweep",
                "sweep_type": "confirmed_rejection", "direction": sweep_dir, "status": "confirmed",
                "label": f"Potential {sweep_dir} liquidity sweep: confirmed rejection",
                "known_at": st.close[lo + j], "extreme": rounded(extreme), "candles": [st.cid(lo + j)],
                "explanation": (f"{level['label']} {fmt(price)}: the {tf.value} candle at {t_j} "
                                f"traded {word} it to {fmt(extreme)} and closed back {back_word} it at {fmt(c[j])} "
                                "(same-candle rejection). A potential sweep is an observation, not a reversal call.")}
    for m in range(j + 1, min(j + 1 + return_bars, n)):
        if not beyond[m]:
            return {**base, "id": f"{level['id']}:sweep:{t_j}", "type": "sweep",
                    "sweep_type": "delayed_rejection", "direction": sweep_dir, "status": "confirmed",
                    "label": f"Potential {sweep_dir} liquidity sweep: delayed rejection",
                    "known_at": st.close[lo + m],
                    "extreme": rounded(h[j:m + 1].max() if high_side else low[j:m + 1].min()),
                    "candles": [st.cid(lo + j), st.cid(lo + m)],
                    "explanation": (f"{level['label']} {fmt(price)}: closed {word} it at {fmt(c[j])} "
                                    f"({t_j}), then closed back {back_word} it at {fmt(c[m])} "
                                    f"{m - j} candle(s) later ({st.open[lo + m]}), within the "
                                    f"{return_bars}-candle return window.")}
    if j + return_bars < n:
        k = j + return_bars
        return {**base, "id": f"{level['id']}:break:{t_j}", "type": "break",
                "sweep_type": None, "direction": break_dir, "status": "confirmed",
                "label": "Confirmed break (acceptance), not a sweep",
                "known_at": st.close[lo + k], "extreme": rounded(extreme),
                "candles": [st.cid(lo + j), st.cid(lo + k)],
                "explanation": (f"{level['label']} {fmt(price)}: closed {word} it at {fmt(c[j])} "
                                f"({t_j}) and the next {return_bars} candle(s) did not close "
                                f"back {back_word} it. Price accepted beyond the level: a break, not a sweep.")}
    return {**base, "id": f"{level['id']}:break:{t_j}", "type": "break",
            "sweep_type": None, "direction": break_dir, "status": "developing",
            "label": "Unconfirmed level break", "known_at": None, "extreme": rounded(extreme),
            "candles": [st.cid(lo + j)],
            "explanation": (f"{level['label']} {fmt(price)}: closed {word} it at {fmt(c[j])} "
                            f"({t_j}). Unresolved: a close back {back_word} it within "
                            f"{return_bars} candle(s) would make it a potential sweep; otherwise it is a break.")}


def _status(interaction: dict[str, Any] | None) -> str:
    if interaction is None:
        return "untouched"
    if interaction["type"] == "sweep":
        return "swept"
    return "broken" if interaction["status"] == "confirmed" else "testing"


def _merge_equal(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Presentation de-duplication: an untouched equal-level pair whose price is within tolerance of a NEWER
    untouched pair of the same type is folded into it (counted as an extra touch)."""
    eq = [lv for lv in levels if lv["type"] in ("EQH", "EQL")]
    drop: set[str] = set()
    for i, older in enumerate(eq):
        for newer in eq[i + 1:]:
            if (newer["type"] == older["type"] and older["status"] == "untouched" and newer["status"] == "untouched"
                    and abs(newer["price"] - older["price"]) <= max(newer["tolerance"], older["tolerance"])):
                drop.add(older["id"])
                newer["touches"] = newer.get("touches", 2) + 1
                break
    return [lv for lv in levels if lv["id"] not in drop]


def _public(level: dict[str, Any]) -> dict[str, Any]:
    return {k: level[k] for k in ("id", "type", "label", "price", "side", "known_at", "status", "timeframe")}
