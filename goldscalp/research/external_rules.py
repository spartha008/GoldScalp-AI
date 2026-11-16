"""Externally sourced strategy rules for the pre-Phase 7 screen, stage 2 (research only).

Each class implements one published rule set for the rule simulator. Sources, the original wording, every parameter
and every deviation are declared in config/research/pre_phase7/candidates_stage2.json; nothing here is tuned.
A rule reads closed bars only: at ``on_close(i)`` it may use bars up to i.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from goldscalp.research import indicators as ind
from goldscalp.research.rulesim import Rule

LONG, SHORT = 1, -1


def _side(name: str) -> int:
    return LONG if name == "long" else SHORT


class ClockTrades(Rule):
    """Fixed clock-time entries with fixed clock-time exits (time-of-day rules; no stops)."""
    def prepare(self) -> None:
        self.tz = self.p["tz"]
        self.cm, self.cd = ind.local_clock(self.sim.close_time, self.tz)
        self.legs = [(ind.hhmm(t["enter"]), _side(t["side"]), ind.hhmm(t["exit"]), t.get("tag")) for t in self.p["trades"]]

    def on_close(self, i: int) -> None:
        for enter, side, leave, tag in self.legs:
            if self.cm[i] == enter:
                day = self.cd[i] + (0 if leave > enter else 1)
                self.sim.market(side, exit_at=ind.at_local(day, leave, self.tz), tag=tag)


class ArbClock(Rule):
    """Breakout of a clock-time range on the first decision-bar close beyond it (educational London-breakout rules)."""
    def prepare(self) -> None:
        p, s = self.p, self.sim
        self.om, self.od = ind.local_clock(s.open_time, p["tz"])
        self.cm, _ = ind.local_clock(s.close_time, p["tz"])
        d = ind.daily_bars(s.bars)
        prev_atr = ind.atr(d["high"], d["low"], d["close"], p["atr_days"]).shift(1)      # through the previous day
        self.datr = prev_atr.reindex(ind.trading_day(s.open_time)).to_numpy()
        self.rs, self.re, self.ee, self.ex = (ind.hhmm(p[k]) for k in ("range_start", "range_end", "entry_end", "exit"))
        self.day, self.hi, self.lo, self.done = None, -np.inf, np.inf, False

    def on_close(self, i: int) -> None:
        p, s = self.p, self.sim
        if self.od[i] != self.day:
            self.day, self.hi, self.lo, self.done = self.od[i], -np.inf, np.inf, False
        if self.rs <= self.om[i] < self.re:
            self.hi, self.lo = max(self.hi, s.h[i]), min(self.lo, s.l[i])
            return
        cm = self.cm[i]
        if self.done or not s.idle or not (self.re < cm <= self.ee) or cm % p["decide_minutes"] or not np.isfinite(self.hi):
            return
        rng, a = self.hi - self.lo, self.datr[i]
        if not np.isfinite(a) or rng < p["min_range_atr"] * a or rng > p["max_range_atr"] * a:
            self.done = True                                    # range filter: no trade today
            return
        c = s.c[i]
        if c > self.hi or c < self.lo:
            side = LONG if c > self.hi else SHORT
            s.market(side, stop=self.lo if side > 0 else self.hi, target_r=p["target_r"],
                     exit_at=ind.at_local(self.day, self.ex, p["tz"]))
            self.done = True


class ArbServer(Rule):
    """Server-time range with OCO stop orders at the range end (vendor London-breakout rules for gold)."""
    def prepare(self) -> None:
        p, s = self.p, self.sim
        self.off = p["server_offset_from_ny_hours"]
        self.om, self.od = ind.local_clock(s.open_time, ind.NY, self.off)
        self.cm, self.cd = ind.local_clock(s.close_time, ind.NY, self.off)
        self.rs, self.re, self.ex = (ind.hhmm(p[k]) for k in ("range_start", "range_end", "exit"))
        self.day, self.hi, self.lo, self.done = None, -np.inf, np.inf, False

    def on_close(self, i: int) -> None:
        p, s = self.p, self.sim
        if self.od[i] != self.day:
            self.day, self.hi, self.lo, self.done = self.od[i], -np.inf, np.inf, False
        if self.rs <= self.om[i] < self.re:
            self.hi, self.lo = max(self.hi, s.h[i]), min(self.lo, s.l[i])
        if self.done or self.cm[i] != self.re or self.cd[i] != self.day or not np.isfinite(self.hi) or not s.idle:
            return
        self.done = True
        price, rng = s.c[i], self.hi - self.lo
        if rng < p["min_range_frac"] * price or rng > p["max_range_frac"] * price:
            return
        end = ind.at_local(self.day, self.ex, ind.NY, self.off)
        kw = {"max_stop_frac": p["max_stop_frac"], "target_r": p["target_r"], "be_trigger_r": p["be_trigger_r"],
              "expire_at": end, "exit_at": end, "group": f"arbg-{self.day}"}
        s.stop_entry(LONG, self.hi + p["buffer"], stop=self.lo, **kw)
        s.stop_entry(SHORT, self.lo - p["buffer"], stop=self.hi, **kw)


class Crabel(Rule):
    """Opening-range 'stretch' breakout the day after a narrow-range day; the opposite order is the stop."""
    def prepare(self) -> None:
        p, s = self.p, self.sim
        d = ind.daily_bars(s.bars)
        raw = pd.concat([d["high"] - d["open"], d["open"] - d["low"]], axis=1).min(axis=1)
        self.stretch = raw.rolling(p["stretch_days"], min_periods=p["stretch_days"]).mean().shift(1)
        rng = d["high"] - d["low"]
        self.setup = (rng <= rng.rolling(p["nr"], min_periods=p["nr"]).min()).shift(1, fill_value=False)
        self.nr7 = (rng <= rng.rolling(7, min_periods=7).min()).shift(1, fill_value=False)
        self.td = ind.trading_day(s.open_time)

    def on_close(self, i: int) -> None:
        s, day = self.sim, self.td[i]
        if i == 0 or self.td[i - 1] == day or not bool(self.setup.get(day, False)):
            return
        st = self.stretch.get(day, np.nan)
        if not np.isfinite(st) or st <= 0:
            return
        o, end = s.o[i], ind.day_end(day) - pd.Timedelta(minutes=self.p["exit_before_rollover_minutes"])
        kw = {"expire_at": end, "exit_at": end, "group": f"crb-{day.date()}", "tag": "NR7" if bool(self.nr7.get(day, False)) else "NR4"}
        s.stop_entry(LONG, o + st, stop=o - st, **kw)
        s.stop_entry(SHORT, o - st, stop=o + st, **kw)


class OrbFirstBar(Rule):
    """Trade in the direction of the session's first bar; stop at that bar's other end (5-minute ORB)."""
    def prepare(self) -> None:
        p = self.p
        self.om, self.od = ind.local_clock(self.sim.open_time, p["tz"])
        self.open_min, self.exit_min = ind.hhmm(p["open"]), ind.hhmm(p["exit"])

    def on_close(self, i: int) -> None:
        s = self.sim
        if self.om[i] != self.open_min or s.c[i] == s.o[i]:
            return
        side = LONG if s.c[i] > s.o[i] else SHORT
        s.market(side, stop=s.l[i] if side > 0 else s.h[i], target_r=self.p["target_r"],
                 exit_at=ind.at_local(self.od[i], self.exit_min, self.p["tz"]))


class _SessionPrices(Rule):
    """Prices at fixed clock times per session day (the close of the bar that ends at that time)."""
    def clock(self) -> None:
        tz = self.p["tz"]
        self.om, self.od = ind.local_clock(self.sim.open_time, tz)
        self.cm, self.cd = ind.local_clock(self.sim.close_time, tz)

    def price_at(self, minute: int) -> dict[int, float]:
        m = self.cm == minute
        return dict(zip(self.cd[m].tolist(), self.sim.c[m].tolist(), strict=True))


class NoiseArea(_SessionPrices):
    """Intraday momentum outside a time-of-day 'noise area' (base version, no VWAP stop)."""
    def prepare(self) -> None:
        p = self.p
        self.clock()
        self.open_min, self.close_min = ind.hhmm(p["open"]), ind.hhmm(p["close"])
        first = self.om == self.open_min
        self.day_open = dict(zip(self.od[first].tolist(), self.sim.o[first].tolist(), strict=True))
        self.checks = list(range(self.open_min + p["check_minutes"], self.close_min, p["check_minutes"]))
        days = sorted(self.day_open)
        closes = self.price_at(self.close_min)
        moves = pd.DataFrame({m: pd.Series(self.price_at(m)) for m in self.checks}).reindex(days)
        moves = (moves.div(pd.Series(self.day_open), axis=0) - 1).abs()
        n = p["lookback_days"]
        self.sigma = moves.rolling(n, min_periods=n).mean().shift(1)                    # previous n session days only
        self.prev_close = pd.Series(closes).reindex(days).shift(1)

    def on_close(self, i: int) -> None:
        s, m, day = self.sim, self.cm[i], self.cd[i]
        if m not in self.checks or day not in self.day_open:
            return
        sg, pc, o = self.sigma.at[day, m], self.prev_close.get(day, np.nan), self.day_open[day]
        if not (np.isfinite(sg) and np.isfinite(pc)):
            return
        price = s.c[i]
        want = LONG if price > max(o, pc) * (1 + sg) else SHORT if price < min(o, pc) * (1 - sg) else 0
        if want and want != s.intent:
            if s.pos is not None:
                s.exit("reverse")
            s.market(want, exit_at=ind.at_local(day, self.close_min, self.p["tz"]))


class HalfHour(_SessionPrices):
    """The overnight-to-morning return decides the direction of the session's last half hour."""
    def prepare(self) -> None:
        p = self.p
        self.clock()
        self.entry, self.exit_min = ind.hhmm(p["entry"]), ind.hhmm(p["exit"])
        self.signal = self.price_at(ind.hhmm(p["signal"]))
        self.closes = pd.Series(self.price_at(ind.hhmm(p["prev_close"]))).sort_index()

    def on_close(self, i: int) -> None:
        day = self.cd[i]
        if self.cm[i] != self.entry or day not in self.signal:
            return
        k = int(self.closes.index.searchsorted(day))            # session closes strictly before today
        if k == 0:
            return
        r1 = self.signal[day] / float(self.closes.iloc[k - 1]) - 1
        if r1:
            self.sim.market(LONG if r1 > 0 else SHORT, exit_at=ind.at_local(day, self.exit_min, self.p["tz"]))


class VwapTrend(Rule):
    """Long above the session VWAP, short below, always in the market during the session (tick-volume proxy)."""
    def prepare(self) -> None:
        p, s = self.p, self.sim
        self.om, self.od = ind.local_clock(s.open_time, p["tz"])
        self.open_min, self.close_min = ind.hhmm(p["open"]), ind.hhmm(p["close"])
        self.inside = (self.om >= self.open_min) & (self.om < self.close_min)
        self.vwap = ind.session_vwap(s.bars, self.inside, self.od)

    def on_close(self, i: int) -> None:
        s = self.sim
        if not self.inside[i] or not np.isfinite(self.vwap[i]):
            return
        flat_at = ind.at_local(self.od[i], self.close_min, self.p["tz"]) - pd.Timedelta(minutes=self.p["flat_bars_before_close"] * s.bar_minutes)
        if s.close_time[i] >= flat_at:
            return
        want = LONG if s.c[i] > self.vwap[i] else SHORT if s.c[i] < self.vwap[i] else 0
        if want and want != s.intent:
            if s.pos is not None:
                s.exit("flip")
            s.market(want, exit_at=flat_at)


class _Htf(Rule):
    """Helpers for rules that decide on slower bars built from the simulator's bars."""
    def htf(self, minutes: int) -> pd.DataFrame:
        self.minutes = minutes
        self.m = ind.resample(self.sim.bars, minutes)
        self.now = ind.closes_now(self.m.index, minutes, self.sim.open_time, self.sim.bar_minutes)
        return self.m

    def seen(self, values: pd.Series) -> np.ndarray:
        return ind.known_at(values.astype("float64"), self.minutes, self.sim.open_time, self.sim.bar_minutes)


class TurtleSoup(_Htf):
    """Turtle Soup Plus One: a failed break of the prior N-bar extreme, entered on a stop order the next bar."""
    def prepare(self) -> None:
        p = self.p
        m, n = self.htf(p["bars_minutes"]), p["lookback"]
        prior_low, prior_high = m["low"].shift(1).rolling(n, min_periods=n).min(), m["high"].shift(1).rolling(n, min_periods=n).max()
        age_low = m["low"].shift(1).rolling(n, min_periods=n).apply(lambda w: len(w) - int(np.argmin(w)), raw=True)
        age_high = m["high"].shift(1).rolling(n, min_periods=n).apply(lambda w: len(w) - int(np.argmax(w)), raw=True)
        k = p["min_bars_since_prior"]
        self.buy = self.seen((m["low"] < prior_low) & (age_low >= k) & (m["close"] <= prior_low))
        self.sell = self.seen((m["high"] > prior_high) & (age_high >= k) & (m["close"] >= prior_high))
        self.pl, self.ph, self.sl, self.sh = self.seen(prior_low), self.seen(prior_high), self.seen(m["low"]), self.seen(m["high"])

    def on_close(self, i: int) -> None:
        p, s = self.p, self.sim
        if not self.now[i] or not s.idle:
            return
        kw = {"target_r": p["target_r"], "hold_minutes": p["hold_minutes"],
              "expire_at": s.close_time[i] + pd.Timedelta(minutes=p["bars_minutes"]), "group": f"ts-{i}"}
        if self.buy[i] == 1:
            s.stop_entry(LONG, self.pl[i] + p["buffer"], stop=self.sl[i] - p["buffer"], **kw)
        elif self.sell[i] == 1:
            s.stop_entry(SHORT, self.ph[i] - p["buffer"], stop=self.sh[i] + p["buffer"], **kw)


class Rsi2(_Htf):
    """RSI(2) extremes with the trend (200 SMA); exit on a close beyond the 5 SMA; percentage stop."""
    def prepare(self) -> None:
        p = self.p
        m = self.htf(p["bars_minutes"])
        self.r, self.c = self.seen(ind.rsi(m["close"], p["rsi_period"])), self.seen(m["close"])
        self.trend, self.fast = self.seen(ind.sma(m["close"], p["trend_sma"])), self.seen(ind.sma(m["close"], p["exit_sma"]))

    def on_close(self, i: int) -> None:
        p, s = self.p, self.sim
        if not self.now[i] or not np.isfinite(self.trend[i]) or not np.isfinite(self.r[i]):
            return
        c = self.c[i]
        if s.pos is not None:
            if (c > self.fast[i]) if s.side > 0 else (c < self.fast[i]):
                s.exit("signal")
        elif s.idle:
            if c > self.trend[i] and self.r[i] <= p["long_below"]:
                s.market(LONG, stop_frac=p["stop_frac"])
            elif c < self.trend[i] and self.r[i] >= p["short_above"]:
                s.market(SHORT, stop_frac=p["stop_frac"])


class PercentB(_Htf):
    """Bollinger %b held at an extreme for several bars, with the trend; exit at the other extreme. No stop."""
    def prepare(self) -> None:
        p = self.p
        m = self.htf(p["bars_minutes"])
        pb, k = ind.percent_b(m["close"], p["bb_period"], p["bb_k"]), p["consecutive"]
        self.pb, self.c, self.trend = self.seen(pb), self.seen(m["close"]), self.seen(ind.sma(m["close"], p["trend_sma"]))
        self.low_run = self.seen((pb < p["low"]).rolling(k, min_periods=k).sum() == k)
        self.high_run = self.seen((pb > p["high"]).rolling(k, min_periods=k).sum() == k)

    def on_close(self, i: int) -> None:
        p, s = self.p, self.sim
        if not self.now[i] or not np.isfinite(self.trend[i]) or not np.isfinite(self.pb[i]):
            return
        if s.pos is not None:
            if (self.pb[i] > p["high"]) if s.side > 0 else (self.pb[i] < p["low"]):
                s.exit("signal")
        elif s.idle:
            if self.c[i] > self.trend[i] and self.low_run[i] == 1:
                s.market(LONG)
            elif self.c[i] < self.trend[i] and self.high_run[i] == 1:
                s.market(SHORT)


class TtmSqueeze(_Htf):
    """Squeeze (Bollinger inside Keltner) releases; direction from the momentum histogram; exit when it turns. No stop."""
    def prepare(self) -> None:
        p = self.p
        m, n = self.htf(p["bars_minutes"]), p["period"]
        _, bu, bl = ind.bollinger(m["close"], n, p["bb_k"])
        _, ku, kl = ind.keltner(m["high"], m["low"], m["close"], n, p["kc_mult"])
        on = ((bu < ku) & (bl > kl)).fillna(False)
        mom = ind.squeeze_momentum(m["high"], m["low"], m["close"], n)
        d = mom.diff()
        self.fire = self.seen((~on) & on.shift(1, fill_value=False))
        self.mom, self.d, self.d_prev = self.seen(mom), self.seen(d), self.seen(d.shift(1))

    def on_close(self, i: int) -> None:
        s = self.sim
        if not self.now[i] or not np.isfinite(self.d_prev[i]):
            return
        if s.pos is not None:
            if (self.d[i] < 0 and self.d_prev[i] < 0) if s.side > 0 else (self.d[i] > 0 and self.d_prev[i] > 0):
                s.exit("signal")
        elif s.idle and self.fire[i] == 1:
            if self.mom[i] > 0 and self.d[i] > 0:
                s.market(LONG)
            elif self.mom[i] < 0 and self.d[i] < 0:
                s.market(SHORT)


class MaCross(Rule):
    """Always in the market: long while the short moving average is above the long one, short while below."""
    def prepare(self) -> None:
        c = self.sim.bars["close"]
        self.a = ind.sma(c, self.p["short"]).to_numpy()
        self.b = ind.sma(c, self.p["long"]).to_numpy()

    def on_close(self, i: int) -> None:
        s = self.sim
        if not (np.isfinite(self.a[i]) and np.isfinite(self.b[i])) or self.a[i] == self.b[i]:
            return
        want = LONG if self.a[i] > self.b[i] else SHORT
        if want != s.intent:
            if s.pos is not None:
                s.exit("flip")
            s.market(want)


class EmaPullback(_Htf):
    """Slower-bar EMA trend; enter when price closes back beyond the fast EMA after a pullback; stop beyond the pullback."""
    def prepare(self) -> None:
        p = self.p
        m = self.htf(p["trend_minutes"])
        fast, slow = ind.ema(m["close"], p["fast"]), ind.ema(m["close"], p["slow"])
        trend = pd.Series(0.0, index=m.index)
        trend[(fast > slow) & (m["close"] > fast)] = 1.0
        trend[(fast < slow) & (m["close"] < fast)] = -1.0
        self.trend = self.seen(trend.where(slow.notna()))
        self.e = ind.ema(self.sim.bars["close"], p["entry_ema"]).to_numpy()
        self.armed, self.extreme = 0, np.nan

    def on_close(self, i: int) -> None:
        p, s, t, e = self.p, self.sim, self.trend[i], self.e[i]
        if not np.isfinite(t) or not np.isfinite(e) or t == 0:
            self.armed = 0
            return
        if self.armed and self.armed != t:
            self.armed = 0
        edge = s.l[i] if t > 0 else s.h[i]
        if (s.c[i] <= e) if t > 0 else (s.c[i] >= e):                     # pulled back to or through the fast EMA
            self.extreme = edge if not self.armed else (min(self.extreme, edge) if t > 0 else max(self.extreme, edge))
            self.armed = int(t)
        elif self.armed:                                                  # the first close back beyond it
            extreme = min(self.extreme, edge) if t > 0 else max(self.extreme, edge)
            self.armed = 0
            if s.idle:
                s.market(int(t), stop=extreme - p["buffer"] if t > 0 else extreme + p["buffer"], target_r=p["target_r"],
                         hold_minutes=p["hold_minutes"])


class Replay(Rule):
    """Parity check only: re-enter one recorded trade at its entry bar with its stop, target and time limit."""
    def prepare(self) -> None:
        self.at = pd.Timestamp(self.p["entry_time"])

    def on_close(self, i: int) -> None:
        s = self.sim
        if i + 1 < len(s.open_time) and s.open_time[i + 1] == self.at:
            s.market(LONG if self.p["side"] == "buy" else SHORT, stop=self.p["stop"], target=self.p["target"],
                     hold_minutes=self.p["hold_minutes"])


RULES: dict[str, type[Rule]] = {
    "clock_trades": ClockTrades, "arb_clock": ArbClock, "arb_server": ArbServer, "crabel": Crabel,
    "orb_first_bar": OrbFirstBar, "noise_area": NoiseArea, "half_hour": HalfHour, "vwap_trend": VwapTrend,
    "turtle_soup": TurtleSoup, "rsi2": Rsi2, "percent_b": PercentB, "ttm_squeeze": TtmSqueeze, "ma_cross": MaCross,
    "ema_pullback": EmaPullback, "replay": Replay,
}


def make(name: str, params: dict[str, Any]) -> Rule:
    if name not in RULES:
        raise KeyError(f"unknown rule {name!r}")
    return RULES[name](dict(params))
