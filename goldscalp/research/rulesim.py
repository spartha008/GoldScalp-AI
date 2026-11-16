"""Bar-by-bar rule simulator for research screens (pre-Phase 7 stage 2). Historical simulation only.

It exists because the strategy builder cannot express clock-time entries and exits, stop-entry orders, indicator exits
or reversals. Fills and costs follow the bt@1 engine exactly, so results are comparable:

  - candles are BID prices; the ask is bid + spread (the candle's spread, else the configured one);
  - a buy fills at the ask + slippage and closes at the bid - slippage; a sell fills at the bid - slippage and closes at
    the ask + slippage; commission is charged per lot per side;
  - a rule decides at a bar's CLOSE and its orders act from the next bar's open (plus ``delay_bars``);
  - at an open: a due time exit, then a gap through the stop (fills at the open), then a gap through the target;
  - inside a bar: if both the stop and the target are touched, the stop counts first;
  - stop-entry orders fill at their price, or at the open if the bar opens beyond it; a buy triggers on the ask. On the
    bar where a stop-entry fills, a touched protective stop counts as hit and a target does not (worst case);
  - one position at a time; whatever is open at the window end is closed at the last close.

R is the net result divided by the initial risk. A position without a stop has no risk of its own, so its R uses a
NOMINAL unit, 1.5 x ATR(14) on M15 at entry, and the trade is marked ``nominal_r``.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.research import indicators as ind

NOMINAL_ATR_MULTIPLE = 1.5
NOMINAL_ATR_MINUTES = 15
NOMINAL_ATR_PERIOD = 14


@dataclass(frozen=True)
class Costs:
    configured_spread: float
    commission_per_lot_per_side: float
    slippage: float


class _Pos:
    __slots__ = ("side", "entry_time", "entry_i", "entry_price", "entry_spread", "stop", "stop_initial", "target", "risk",
                 "nominal", "exit_at", "be_trigger_r", "be_active", "mfe", "mae", "tag", "intrabar", "pending_exit")


class Rule:
    """A rule sees closed bars only. ``prepare`` runs once; ``on_close(i)`` after bar i has closed."""
    def __init__(self, params: dict[str, Any]) -> None:
        self.p = params
        self.sim: Sim

    def bind(self, sim: Sim) -> None:
        self.sim = sim
        self.prepare()

    def prepare(self) -> None:
        pass

    def on_close(self, i: int) -> None:
        raise NotImplementedError


class Sim:
    def __init__(self, bars: pd.DataFrame, *, bar_minutes: int, start: datetime, end: datetime, costs: Costs,
                 lots: float, ounces_per_lot: float, delay_bars: int = 0) -> None:
        if len(bars) and bars.index.tz is None:
            raise ValueError("bars must have a UTC index")
        self.bars = bars
        self.bar_minutes = bar_minutes
        self.open_time = bars.index
        self.close_time = bars.index + pd.Timedelta(minutes=bar_minutes)
        self.o, self.h, self.l, self.c = (bars[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
        raw = bars["spread"].to_numpy(dtype="float64") if "spread" in bars else np.full(len(bars), np.nan)
        self.s = np.where(np.isfinite(raw) & (raw > 0), raw, costs.configured_spread)
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)
        self.costs, self.lots, self.ounces = costs, lots, lots * ounces_per_lot
        self.delay = delay_bars
        self.i = -1
        self.pos: _Pos | None = None
        self.orders: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.diag: Counter = Counter()
        self._atr: np.ndarray | None = None

    # ------------------------------------------------------------------ what a rule may read
    @property
    def side(self) -> int:
        return self.pos.side if self.pos is not None else 0

    @property
    def intent(self) -> int:
        """The side once every pending market order and exit has acted (so a rule does not repeat an order)."""
        for o in reversed(self.orders):
            if o["kind"] == "market":
                return o["side"]
        if self.pos is None or self.pos.pending_exit is not None:
            return 0
        return self.pos.side

    @property
    def idle(self) -> bool:
        return self.pos is None and not any(o["kind"] == "market" for o in self.orders)

    @property
    def active(self) -> bool:
        """Orders placed now can act inside the window."""
        j = self.i + 1 + self.delay
        return j < len(self.open_time) and self.start <= self.open_time[j] < self.end

    # ------------------------------------------------------------------ what a rule may do (acts from the next open)
    def market(self, side: int, *, stop: float | None = None, stop_frac: float | None = None, target: float | None = None,
               target_r: float | None = None, exit_at: pd.Timestamp | None = None, hold_minutes: float | None = None,
               tag: str | None = None) -> None:
        self._order("market", side, stop=stop, stop_frac=stop_frac, target=target, target_r=target_r, exit_at=exit_at,
                    hold_minutes=hold_minutes, tag=tag)

    def stop_entry(self, side: int, price: float, *, stop: float | None, expire_at: pd.Timestamp,
                   max_stop_frac: float | None = None, target_r: float | None = None, be_trigger_r: float | None = None,
                   exit_at: pd.Timestamp | None = None, hold_minutes: float | None = None, group: str | None = None,
                   tag: str | None = None) -> None:
        self._order("stop", side, price=price, stop=stop, max_stop_frac=max_stop_frac, target_r=target_r,
                    be_trigger_r=be_trigger_r, expire_at=expire_at, exit_at=exit_at, hold_minutes=hold_minutes,
                    group=group, tag=tag)

    def exit(self, reason: str = "signal") -> None:
        if self.pos is not None and self.pos.pending_exit is None:
            self.pos.pending_exit = (self.i + 1 + self.delay, reason)

    def cancel(self, group: str | None = None) -> None:
        self.orders = [o for o in self.orders if not (o["kind"] == "stop" and (group is None or o.get("group") == group))]

    def _order(self, kind: str, side: int, **kw: Any) -> None:
        if not self.active:
            self.diag["orders_outside_window"] += 1
            return
        self.orders.append({"kind": kind, "side": 1 if side > 0 else -1, "active_from": self.i + 1 + self.delay, **kw})

    # ------------------------------------------------------------------ fills
    def _nominal_unit(self, i: int) -> float | None:
        if self._atr is None:
            m = self.bars if self.bar_minutes >= NOMINAL_ATR_MINUTES else ind.resample(self.bars, NOMINAL_ATR_MINUTES)
            a = ind.atr(m["high"], m["low"], m["close"], NOMINAL_ATR_PERIOD)
            self._atr = ind.known_at(a, max(self.bar_minutes, NOMINAL_ATR_MINUTES), self.open_time, self.bar_minutes)
        v = self._atr[i - 1] if i > 0 else np.nan
        return float(v) * NOMINAL_ATR_MULTIPLE if np.isfinite(v) and v > 0 else None

    def _open(self, o: dict[str, Any], i: int, bid: float, intrabar: bool) -> bool:
        """Open a position for order ``o`` at bar i. ``bid`` is the bid at the fill (for a buy the ask is bid + spread)."""
        side, s, slip = o["side"], self.s[i], self.costs.slippage
        entry = bid + s + slip if side > 0 else bid - slip
        stop = o.get("stop")
        if o.get("stop_frac"):
            stop = entry * (1 - o["stop_frac"]) if side > 0 else entry * (1 + o["stop_frac"])
        if stop is not None and o.get("max_stop_frac"):
            cap = entry * (1 - o["max_stop_frac"]) if side > 0 else entry * (1 + o["max_stop_frac"])
            stop = max(stop, cap) if side > 0 else min(stop, cap)
        if stop is not None:
            risk = entry - stop if side > 0 else stop - entry
            nominal = False
            if risk <= 0:
                self.diag["invalid_stop"] += 1
                return False
        else:
            unit = self._nominal_unit(i)
            if unit is None:
                self.diag["no_nominal_unit"] += 1
                return False
            risk, nominal = unit, True
        target = o.get("target")
        if target is None and o.get("target_r"):
            target = entry + side * o["target_r"] * risk
        exit_at = o.get("exit_at")
        if exit_at is None and o.get("hold_minutes"):
            exit_at = self.open_time[i] + pd.Timedelta(minutes=o["hold_minutes"])
        p = _Pos()
        p.side, p.entry_time, p.entry_i, p.entry_price, p.entry_spread = side, self.open_time[i], i, entry, s
        p.stop, p.stop_initial, p.target, p.risk, p.nominal = stop, stop, target, risk, nominal
        p.exit_at, p.be_trigger_r, p.be_active = exit_at, o.get("be_trigger_r"), False
        p.mfe, p.mae, p.tag, p.intrabar, p.pending_exit = 0.0, 0.0, o.get("tag"), intrabar, None
        self.pos = p
        self.diag["entries"] += 1
        return True

    def _close(self, i: int, bid: float, reason: str, at: pd.Timestamp) -> None:
        p, s, slip, oz = self.pos, self.s[i], self.costs.slippage, self.ounces
        assert p is not None
        exit_price = bid - slip if p.side > 0 else bid + s + slip
        gross = (exit_price - p.entry_price) * p.side * oz
        comm = self.costs.commission_per_lot_per_side * self.lots * 2
        net = gross - comm
        cost = comm + (p.entry_spread if p.side > 0 else s) * oz + slip * oz * 2
        risk_cash = p.risk * oz
        self.trades.append({
            "entry": iso(p.entry_time.to_pydatetime()), "exit": iso(at.to_pydatetime()), "side": "buy" if p.side > 0 else "sell",
            "lots": f"{self.lots:.2f}", "reason": reason, "net": round(net, 2), "r": round(net / risk_cash, 3),
            "mfe_r": round(p.mfe / p.risk, 3), "mae_r": round(p.mae / p.risk, 3),
            "hold": round((at - p.entry_time).total_seconds() / 60, 1), "session": None,
            "risk_usd": round(risk_cash, 2), "cost_usd": round(cost, 2), "nominal_r": p.nominal,
            "entry_price": round(p.entry_price, 3), "exit_price": round(exit_price, 3),
            "stop": None if p.stop_initial is None else round(p.stop_initial, 3), "tag": p.tag})
        self.pos = None

    # ------------------------------------------------------------------ the bar loop
    def run(self, rule: Rule) -> dict[str, Any]:
        rule.bind(self)
        n = len(self.open_time)
        last_in = None
        for i in range(n):
            t = self.open_time[i]
            if t >= self.end:
                break
            if t >= self.start:
                last_in = i
                self._bar(i)
            self.i = i
            rule.on_close(i)
        if self.pos is not None and last_in is not None:
            self._close(last_in, self.c[last_in], "window_end", self.close_time[last_in])
        return {"trades": self.trades, "exits": dict(Counter(t["reason"] for t in self.trades)), "diagnostics": dict(self.diag)}

    def _bar(self, i: int) -> None:
        bo, bh, bl, s, t = self.o[i], self.h[i], self.l[i], self.s[i], self.open_time[i]
        p = self.pos
        # a) at the open: due exits, then gaps through the stop or the target
        if p is not None:
            if p.pending_exit is not None and p.pending_exit[0] <= i:
                self._close(i, bo, p.pending_exit[1], t)
            elif p.exit_at is not None and t >= p.exit_at:
                self._close(i, bo, "time", t)
            elif p.stop is not None and (bo <= p.stop if p.side > 0 else bo + s >= p.stop):
                self._close(i, bo, "breakeven" if p.be_active else "stop", t)
            elif p.target is not None and (bo >= p.target if p.side > 0 else bo + s <= p.target):
                self._close(i, p.target if p.side > 0 else p.target - s, "target", t)
        # b) orders
        self.orders = [o for o in self.orders if not (o["kind"] == "stop" and t >= o["expire_at"])]
        due = [o for o in self.orders if o["active_from"] <= i]
        for o in [x for x in due if x["kind"] == "market"]:
            self.orders.remove(o)
            if self.pos is None:
                self._open(o, i, bo, intrabar=False)
            else:
                self.diag["market_order_while_in_position"] += 1
        if self.pos is None:
            hits = []
            for o in [x for x in due if x["kind"] == "stop"]:
                if o["side"] > 0:
                    if bo + s >= o["price"]:
                        hits.append((0.0, o, bo, False))
                    elif bh + s >= o["price"]:
                        hits.append((o["price"] - (bo + s), o, o["price"] - s, True))
                elif bo <= o["price"]:
                    hits.append((0.0, o, bo, False))
                elif bl <= o["price"]:
                    hits.append((bo - o["price"], o, o["price"], True))
            if hits:
                _, o, bid, intrabar = min(hits, key=lambda x: x[0])        # the level nearest the open is reached first
                self.orders.remove(o)
                if self._open(o, i, bid, intrabar) and o.get("group") is not None:
                    self.cancel(o["group"])
        # c) inside the bar
        p = self.pos
        if p is None:
            return
        if p.side > 0:
            fav, adv = bh - p.entry_price, p.entry_price - bl
            hit_stop = p.stop is not None and bl <= p.stop
            hit_target = p.target is not None and bh >= p.target
        else:
            fav, adv = p.entry_price - (bl + s), (bh + s) - p.entry_price
            hit_stop = p.stop is not None and bh + s >= p.stop
            hit_target = p.target is not None and bl + s <= p.target
        p.mfe, p.mae = max(p.mfe, fav), max(p.mae, adv)
        if p.entry_i == i and p.intrabar:
            hit_target = False
        at = self.close_time[i]
        if hit_stop:
            self._close(i, p.stop if p.side > 0 else p.stop - s, "breakeven" if p.be_active else "stop", at)
        elif hit_target:
            self._close(i, p.target if p.side > 0 else p.target - s, "target", at)
        elif p.be_trigger_r and not p.be_active and p.mfe >= p.be_trigger_r * p.risk:
            if p.stop is None or (p.entry_price > p.stop if p.side > 0 else p.entry_price < p.stop):
                p.stop = p.entry_price
            p.be_active = True


def simulate(bars: pd.DataFrame, rule: Rule, *, bar_minutes: int, start: datetime, end: datetime, costs: Costs,
             lots: float, ounces_per_lot: float, delay_bars: int = 0) -> dict[str, Any]:
    return Sim(bars, bar_minutes=bar_minutes, start=start, end=end, costs=costs, lots=lots,
               ounces_per_lot=ounces_per_lot, delay_bars=delay_bars).run(rule)
