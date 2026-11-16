"""Deterministic historical backtesting engine (definition bt@1). PHASE3_ARCHITECTURE.md section 5.

Decisions are made ONLY at step cut-offs, from the feature snapshot of that cut-off (candles closed by
then). Execution happens on later base candles:

  for each base candle b (open o, close c):
    a) at o: pending structure-flip exits and time exits fill at o; a gap beyond a stop fills at o
             (a gap beyond a target fills at the target: conservative)
    b) at o: pending entries fill at o (long: ask + slippage; short: bid - slippage), unless the data gap
             since the signal exceeds max_entry_delay (cancelled), exposure is full, or the size is below
             the minimum lot
    c) intrabar: stop / target against bid (long) or ask (short) high/low. Both inside one candle is
             AMBIGUOUS: the run's intrabar rule decides (default stop_first) and the trade is flagged
    d) at c, if c is a step cut-off: evaluate the strategy -> pending entries / exits for the next open,
             marked-to-market equity point

Spread: the candle's stored spread, else the configured spread (labelled). Commission per lot per side
and adverse slippage per fill use the paper simulator's own formulas. Nothing here places orders or
touches paper accounts.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.core.clock import iso
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.costs import CostModel, commission
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.positions import Side, close_fill_price, gross_pnl, open_fill_price, required_margin
from goldscalp.simulation.pricing import ExecutionQuote
from goldscalp.strategies import builder, conditions

VERSION = "bt@1"
INTRABAR_RULES = ("stop_first", "target_first")


class BacktestError(ValueError):
    pass


@dataclass(frozen=True)
class Assumptions:
    configured_spread: Decimal
    commission_per_lot_per_side: Decimal
    slippage: Decimal
    intrabar: str = "stop_first"
    starting_balance: Decimal = Decimal("10000")
    leverage: int = 1
    min_trades_for_ratios: int = 30
    sizing_override: dict[str, Any] | None = None
    # Phase 4 stress test (spread sensitivity): every spread (candle or configured) is multiplied by this.
    # 1 = unchanged; it is left out of the mapping when 1, so Phase 3 runs keep identical parameters.
    spread_multiplier: Decimal = Decimal(1)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Assumptions:
        try:
            out = cls(configured_spread=Decimal(str(data["configured_spread"])),
                      commission_per_lot_per_side=Decimal(str(data["commission_per_lot_per_side"])),
                      slippage=Decimal(str(data["slippage"])), intrabar=data.get("intrabar", "stop_first"),
                      starting_balance=Decimal(str(data.get("starting_balance", "10000"))),
                      leverage=int(data.get("leverage", 1)),
                      min_trades_for_ratios=int(data.get("min_trades_for_ratios", 30)),
                      sizing_override=data.get("sizing_override"),
                      spread_multiplier=Decimal(str(data.get("spread_multiplier", "1"))))
        except (KeyError, ArithmeticError, ValueError, TypeError) as exc:
            raise BacktestError(f"invalid assumptions: {exc}") from exc
        if out.intrabar not in INTRABAR_RULES:
            raise BacktestError(f"intrabar must be one of {list(INTRABAR_RULES)}")
        if min(out.configured_spread, out.commission_per_lot_per_side, out.slippage) < 0 or out.starting_balance <= 0:
            raise BacktestError("costs must not be negative and the starting balance must be positive")
        if not 1 <= out.leverage <= 1000:
            raise BacktestError("leverage must be between 1 and 1000")
        if not 1 <= out.min_trades_for_ratios <= 10000:
            raise BacktestError("min_trades_for_ratios must be between 1 and 10000")
        if not Decimal("0.1") <= out.spread_multiplier <= Decimal(10):
            raise BacktestError("spread_multiplier must be between 0.1 and 10")
        so = out.sizing_override or {}
        if so.get("sizing") == RISK_EQUITY:
            try:
                pct = Decimal(str(so["risk_percent"]))
            except (KeyError, ArithmeticError, ValueError) as exc:
                raise BacktestError(f"{RISK_EQUITY} needs a numeric risk_percent") from exc
            if not Decimal(0) < pct <= RISK_EQUITY_MAX_PERCENT:
                raise BacktestError(f"{RISK_EQUITY}: risk_percent must be above 0 and at most {RISK_EQUITY_MAX_PERCENT}")
        return out

    def cost_model(self) -> CostModel:
        return CostModel.from_mapping({"spread_mode": "market_else_configured",
                                       "configured_spread": str(self.configured_spread) if self.configured_spread > 0 else "0.01",
                                       "commission_per_lot_per_side": str(self.commission_per_lot_per_side),
                                       "slippage": str(self.slippage)})

    def to_mapping(self) -> dict[str, Any]:
        return {"configured_spread": str(self.configured_spread),
                "commission_per_lot_per_side": str(self.commission_per_lot_per_side), "slippage": str(self.slippage),
                "intrabar": self.intrabar, "starting_balance": str(self.starting_balance), "leverage": self.leverage,
                "min_trades_for_ratios": self.min_trades_for_ratios, "sizing_override": self.sizing_override,
                **({"spread_multiplier": str(self.spread_multiplier)} if self.spread_multiplier != 1 else {})}


def _d(x: float) -> Decimal:
    return Decimal(str(round(float(x), 6)))


def _f(x: Decimal | None, digits: int = 2) -> float | None:
    return None if x is None else round(float(x), digits)


class _Position:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def run(config: dict[str, Any], snapshots: list[dict[str, Any]], candles: pd.DataFrame, base_tf: Timeframe, *,
        start: datetime, end: datetime, assumptions: Assumptions, spec: InstrumentSpec,
        force_close_at_end: bool = False, step_tf: Timeframe | None = None) -> dict[str, Any]:
    """Backtest one strategy on a prepared feature timeline. ``candles`` are CLOSED base candles (those
    after ``end`` are used only to manage positions still open, unless ``force_close_at_end``)."""
    step_tf = step_tf or Timeframe.parse(config["evaluation"]["step_timeframe"])
    costs = assumptions.cost_model()
    risk = dict(config["risk"])
    if assumptions.sizing_override:
        risk.update(assumptions.sizing_override)
    equity_mode = risk["sizing"] == RISK_EQUITY
    be_cfg = config["exit"].get("breakeven") or {}
    breakeven = be_cfg if be_cfg.get("enabled") else None   # Phase 5.2 optional break-even stop

    tr_cfg = config["exit"].get("trail") or {}
    trail = tr_cfg if tr_cfg.get("enabled") else None       # Phase 5.2 optional trailing stop

    def stop_reason(p: _Position) -> str:
        return "trailing_stop" if p.trail_moved else "breakeven" if p.be_active else "stop"

    def manage(p: _Position, at: pd.Timestamp) -> None:
        """Break-even / trailing stop at this bar's CLOSE (it can fill from the next bar on: the order of high and low
        inside a bar is unknown, so the same bar is never used). Stops only ever tighten."""
        if not p.risk_per_oz:
            return
        if breakeven is not None and not p.be_active and p.mfe >= Decimal(str(breakeven["trigger_r"])) * p.risk_per_oz:
            lock = Decimal(str(breakeven["lock_r"])) * p.risk_per_oz
            level = p.entry_price + lock if p.side is Side.BUY else p.entry_price - lock
            if p.stop is None or (level > p.stop if p.side is Side.BUY else level < p.stop):
                p.stop = level
            p.be_active, p.be_time = True, at
        if trail is not None and p.mfe >= Decimal(str(trail["activate_r"])) * p.risk_per_oz:
            unit = p.risk_per_oz if trail["mode"] == "r" else p.atr
            if not unit:
                return
            dist = Decimal(str(trail["distance"])) * unit
            level = (p.entry_price + p.mfe - dist) if p.side is Side.BUY else (p.entry_price - p.mfe + dist)
            if p.stop is None or (level > p.stop if p.side is Side.BUY else level < p.stop):
                p.stop = level
                p.trail_moved = True
                p.trail_time = at
    equity_pct = Decimal(str(risk["risk_percent"])) if equity_mode else Decimal(0)
    stop_cfg, target_cfg = config["exit"]["stop"], config["exit"]["target"]
    max_hold = pd.Timedelta(minutes=int(config["exit"]["max_holding_minutes"])) if config["exit"]["max_holding_minutes"] else None
    flip = config["exit"]["exit_on_structure_flip"]
    max_delay = pd.Timedelta(minutes=int(config["entry"]["max_entry_delay_minutes"]))
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    snap_at = {pd.Timestamp(s["cutoff"]): s for s in snapshots}
    first_cut = min(snap_at) if snap_at else end_ts
    frame = candles[candles.index >= first_cut - pd.Timedelta(seconds=base_tf.seconds)]
    if "closed" in frame and not bool(frame["closed"].all()):
        raise BacktestError("incomplete (forming) candles must not reach the backtest engine")
    o_arr, h_arr, l_arr, c_arr = (frame[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    s_arr = frame["spread"].to_numpy(dtype="float64") if "spread" in frame else np.full(len(frame), np.nan)
    times = frame.index
    bar = pd.Timedelta(seconds=base_tf.seconds)

    balance = assumptions.starting_balance
    open_pos: list[_Position] = []
    pending: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    closed_curve: list[dict[str, Any]] = []
    marked_curve: list[dict[str, Any]] = []
    diag = {"steps": 0, "setups": 0, "entries": 0, "ambiguous_direction_steps": 0, "gated_steps": 0,
            "skipped": {"exposure": 0, "below_min_lot": 0, "entry_gap": 0, "window_end": 0, "insufficient_margin": 0},
            "ambiguous_intrabar": 0, "condition_pass_counts": {}, "gate_reasons": {}, "rejections": []}

    def reject(reason: str, e: dict[str, Any], at: pd.Timestamp, **detail: Any) -> None:
        diag["skipped"][reason] = diag["skipped"].get(reason, 0) + 1
        if len(diag["rejections"]) < 2000:
            diag["rejections"].append({"time": iso(at.to_pydatetime()), "setup_cutoff": e["snap"]["cutoff"],
                                       "side": e["side"], "reason": reason, **detail})
    last_pass = {"long": False, "short": False}
    seq = 0

    mult = assumptions.spread_multiplier

    def spread_at(i: int) -> tuple[Decimal, str]:
        v = s_arr[i]
        spread, src = (_d(v), "candle") if np.isfinite(v) and v > 0 else (assumptions.configured_spread, "configured")
        return (spread, src) if mult == 1 else (spread * mult, f"{src} x {mult}")

    def quote(bid: Decimal, spread: Decimal, src: str, i: int) -> ExecutionQuote:
        return ExecutionQuote(bid=bid, ask=bid + spread, spread=spread, spread_source=src,
                              quote_time=times[i].to_pydatetime(), state=MarketState.HISTORICAL, source_id="backtest")

    def close(p: _Position, i: int, bid: Decimal, reason: str, at: pd.Timestamp, *, ambiguous: bool = False) -> None:
        nonlocal balance
        spread, src = spread_at(i)
        exit_price = close_fill_price(p.side, quote(bid, spread, src, i), costs.slippage)
        gross = gross_pnl(p.side, p.entry_price, exit_price, p.ounces)
        comm = commission(p.lots, costs) * 2
        net = gross - comm
        balance += net
        risk_cash = p.risk_per_oz * p.ounces if p.risk_per_oz else None
        trades.append({
            "id": p.id, "side": p.side.value, "setup_cutoff": p.cutoff, "entry_time": iso(p.entry_time.to_pydatetime()),
            "entry_price": _f(p.entry_price, 3), "lots": format(p.lots, "f"), "ounces": format(p.ounces, "f"),
            "stop": _f(p.stop, 3), "stop_source": p.stop_source, "target": _f(p.target, 3),
            "exit_time": iso(at.to_pydatetime()), "exit_price": _f(exit_price, 3), "exit_reason": reason,
            "ambiguous_bar": ambiguous or p.ambiguous, "gross_pnl": _f(gross), "commission": _f(comm),
            "spread_cost": _f((p.entry_spread if p.side is Side.BUY else spread) * p.ounces),
            "slippage_cost": _f(costs.slippage * p.ounces * 2), "net_pnl": _f(net),
            "r_multiple": _f(net / risk_cash, 3) if risk_cash else None,
            "mfe": _f(p.mfe, 3), "mae": _f(p.mae, 3),
            "mfe_r": _f(p.mfe / p.risk_per_oz, 3) if p.risk_per_oz else None,
            "mae_r": _f(p.mae / p.risk_per_oz, 3) if p.risk_per_oz else None,
            "mfe_atr": _f(p.mfe / p.atr, 3) if p.atr else None, "mae_atr": _f(p.mae / p.atr, 3) if p.atr else None,
            "holding_minutes": round((at - p.entry_time).total_seconds() / 60, 1),
            "session": p.session, "volatility": p.volatility, "timeframe": step_tf.value,
            "conditions": p.conditions, "signal_id": p.signal_id, "_net": net,
        })
        if breakeven is not None or trail is not None:   # Phase 5.2 stop management only (others: unchanged trades)
            trades[-1].update({"stop": _f(p.stop_initial, 3), "stop_final": _f(p.stop, 3),
                               "breakeven_moved": p.be_active, "trail_moved": p.trail_moved,
                               "breakeven_at": iso(p.be_time.to_pydatetime()) if p.be_time is not None else None})
        if p.sized is not None:                 # Phase 5.1 equity-risk sizing only (other modes: unchanged trades)
            s = p.sized
            trades[-1].update({
                "sizing": RISK_EQUITY, "risk_percent": float(s["risk_percent"]), "equity_at_entry": _f(s["equity"]),
                "risk_budget_usd": _f(s["budget"]), "planned_loss_per_lot": _f(s["per_lot_loss"]),
                "planned_risk_usd": _f(s["planned_risk"]), "size_capped_by": s["capped_by"],
                "within_budget": bool(s["planned_risk"] <= s["budget"]),
                "loss_beyond_plan_usd": _f(max(Decimal(0), -net - s["planned_risk"]))})
        closed_curve.append({"time": iso(at.to_pydatetime()), "equity": _f(balance)})
        open_pos.remove(p)

    def plan_stop(side: Side, snap: dict[str, Any], entry_bid: Decimal, entry_ask: Decimal):
        return plan_stop_level(stop_cfg, side, snap, entry_bid, entry_ask)

    def plan_target(side: Side, entry: Decimal, stop: Decimal | None, snap: dict[str, Any]) -> Decimal | None:
        return plan_target_level(target_cfg, side, entry, stop, snap)

    def size(risk_per_oz: Decimal | None) -> Decimal | None:
        return size_lots(risk, balance, risk_per_oz, spec)

    def decide_at(cut: pd.Timestamp, mark_bid: Decimal | None, mark_spread: Decimal) -> None:
        """Decisions of the snapshot taken at ``cut`` (executed from the next base open on)."""
        snap = snap_at[cut]
        if cut > end_ts or cut < start_ts:
            return
        diag["steps"] += 1
        if flip["enabled"]:
            state = (snap["tf"].get(flip["timeframe"]) or {}).get("state")
            for p in open_pos:
                if state == ("bearish" if p.side is Side.BUY else "bullish"):
                    p.exit_next_open = True
        decision = conditions.decide(config, snap)
        for side_name, ev in decision["evaluations"].items():
            for r in ev["conditions"]:
                key = f"{side_name}:{r['id']}"
                diag["condition_pass_counts"][key] = diag["condition_pass_counts"].get(key, 0) + (1 if r["passed"] else 0)
            if ev["gate"]:
                diag["gated_steps"] += 1
                for g in ev["gate"]:
                    reason = g.split(":")[0] + ": " + ("stale" if "stale" in g else "too few candles" if "closed candles" in g
                                                      else "no data" if "no data" in g else "invalid candles")
                    diag["gate_reasons"][reason] = diag["gate_reasons"].get(reason, 0) + 1
        if decision["ambiguous"]:
            diag["ambiguous_direction_steps"] += 1
        trigger = config["entry"]["trigger"]
        for side_name in ("long", "short"):
            passing = decision["setup"] == side_name
            fire = passing and (trigger == "every_step" or not last_pass[side_name])
            last_pass[side_name] = passing
            if fire and cut < end_ts:
                diag["setups"] += 1
                pending.append({"side": side_name, "not_before": cut, "snap": snap,
                                "evaluation": decision["evaluations"][side_name]})
        if mark_bid is not None:
            unreal = Decimal(0)
            for p in open_pos:
                mark = mark_bid if p.side is Side.BUY else mark_bid + mark_spread
                unreal += gross_pnl(p.side, p.entry_price, mark, p.ounces) - commission(p.lots, costs)
            marked_curve.append({"time": iso(cut.to_pydatetime()), "equity": _f(balance + unreal), "balance": _f(balance),
                                 "used_margin": _f(sum((p.margin for p in open_pos), Decimal(0)))})

    cuts = sorted(snap_at)
    k = 0
    last_i = None
    last_bid: Decimal | None = None
    last_spread = assumptions.configured_spread
    for i in range(len(frame)):
        o_t = times[i]
        c_t = o_t + bar
        # 0) every cut-off reached by this candle's open is decided first (handles missing candles)
        while k < len(cuts) and cuts[k] <= o_t:
            decide_at(cuts[k], last_bid, last_spread)
            k += 1
        if force_close_at_end and o_t >= end_ts:
            break
        if o_t >= end_ts and not open_pos and not pending:
            break
        bo, bh, bl = _d(o_arr[i]), _d(h_arr[i]), _d(l_arr[i])
        spread, spread_src = spread_at(i)
        # a) at the open: structure-flip / time exits, gaps through stops or targets
        for p in list(open_pos):
            if p.exit_next_open:
                close(p, i, bo, "structure_flip", o_t)
            elif max_hold is not None and o_t >= p.entry_time + max_hold:
                close(p, i, bo, "time", o_t)
            elif p.side is Side.BUY and p.stop is not None and bo <= p.stop:
                close(p, i, bo, stop_reason(p), o_t)
            elif p.side is Side.SELL and p.stop is not None and bo + spread >= p.stop:
                close(p, i, bo, stop_reason(p), o_t)
            elif p.side is Side.BUY and p.target is not None and bo >= p.target:
                close(p, i, p.target, "target", o_t)
            elif p.side is Side.SELL and p.target is not None and bo + spread <= p.target:
                close(p, i, p.target - spread, "target", o_t)
        # b) pending entries at the open (new entries only inside the window)
        for e in list(pending):
            if o_t < e["not_before"]:
                continue
            pending.remove(e)
            if o_t >= end_ts:
                reject("window_end", e, o_t, detail_text="setup at the window end: no candle left to enter")
                continue
            if o_t - e["not_before"] > max_delay:
                reject("entry_gap", e, o_t, detail_text=f"next candle opened {o_t - e['not_before']} after the setup")
                continue
            side = Side.BUY if e["side"] == "long" else Side.SELL
            q = quote(bo, spread, spread_src, i)
            entry_price = open_fill_price(side, q, costs.slippage)
            stop, stop_source, atr = plan_stop(side, e["snap"], q.bid, q.ask)
            risk_per_oz = abs(entry_price - stop) if stop is not None else None
            sized: dict[str, Any] | None = None
            if equity_mode:
                exposure = sum((p.lots for p in open_pos), Decimal(0))
                if len(open_pos) >= int(risk["max_open_positions"]):
                    reject("exposure", e, o_t, open_positions=len(open_pos), exposure_lots=format(exposure, "f"))
                    continue
                mark_equity = balance + sum((gross_pnl(p.side, p.entry_price, bo if p.side is Side.BUY else bo + spread,
                                                       p.ounces) - commission(p.lots, costs) for p in open_pos), Decimal(0))
                sized = size_risk_of_equity(
                    equity=mark_equity, risk_percent=equity_pct, entry_price=entry_price, stop=stop,
                    slippage=costs.slippage, commission_per_lot_per_side=assumptions.commission_per_lot_per_side,
                    spec=spec, max_lots=Decimal(str(risk["max_exposure_lots"])) - exposure)
                if sized["lots"] is None:
                    reject(sized["reason"], e, o_t, equity=_f(mark_equity), risk_budget=_f(sized["budget"]),
                           planned_loss_per_lot=_f(sized["per_lot_loss"]),
                           min_lot_planned_risk=_f(sized.get("min_lot_planned_risk")), stop_source=stop_source)
                    continue
                lots = sized["lots"]
            else:
                lots = size(risk_per_oz)
            if lots is None:
                reject("below_min_lot", e, o_t, risk_per_oz=_f(risk_per_oz, 3))
                continue
            exposure = sum((p.lots for p in open_pos), Decimal(0))
            if len(open_pos) >= int(risk["max_open_positions"]) or exposure + lots > Decimal(str(risk["max_exposure_lots"])):
                reject("exposure", e, o_t, open_positions=len(open_pos), exposure_lots=format(exposure, "f"),
                       lots=format(lots, "f"))
                continue
            # margin, as the paper simulator requires: ounces x price / leverage, from free margin at this open
            margin = required_margin(spec.ounces(lots), entry_price, assumptions.leverage)
            used = sum((p.margin for p in open_pos), Decimal(0))
            unreal = sum((gross_pnl(p.side, p.entry_price, bo if p.side is Side.BUY else bo + spread, p.ounces)
                          for p in open_pos), Decimal(0))
            if balance + unreal - used - commission(lots, costs) < margin:
                reject("insufficient_margin", e, o_t, lots=format(lots, "f"), margin_required=_f(margin),
                       free_margin=_f(balance + unreal - used))
                continue
            seq += 1
            snap = e["snap"]
            vol_tf = (snap["tf"].get(step_tf.value) or snap["tf"].get("M5") or {}).get("vol") or {}
            open_pos.append(_Position(
                id=seq, side=side, cutoff=snap["cutoff"], entry_time=o_t, entry_price=entry_price, entry_spread=spread,
                lots=lots, ounces=spec.ounces(lots), stop=stop, stop_source=stop_source, margin=margin,
                target=plan_target(side, entry_price, stop, snap), risk_per_oz=risk_per_oz, atr=_d(atr) if atr else None,
                mfe=Decimal(0), mae=Decimal(0), ambiguous=False, exit_next_open=False,
                session=" / ".join(snap["sessions"]) or "none", volatility=vol_tf.get("condition") or "unknown",
                conditions=e["evaluation"]["conditions"], signal_id=(snap.get("signal") or {}).get("id"), sized=sized,
                stop_initial=stop, be_active=False, be_time=None, trail_moved=False, trail_time=None))
            diag["entries"] += 1
        # c) intrabar stop / target; excursions
        for p in list(open_pos):
            if p.side is Side.BUY:
                fav, adv = bh - p.entry_price, p.entry_price - bl
                hit_stop = p.stop is not None and bl <= p.stop
                hit_target = p.target is not None and bh >= p.target
            else:
                fav, adv = p.entry_price - (bl + spread), (bh + spread) - p.entry_price
                hit_stop = p.stop is not None and bh + spread >= p.stop
                hit_target = p.target is not None and bl + spread <= p.target
            p.mfe, p.mae = max(p.mfe, fav), max(p.mae, adv)
            both = hit_stop and hit_target
            if both:
                diag["ambiguous_intrabar"] += 1
                first = "stop" if assumptions.intrabar == "stop_first" else "target"
            else:
                first = "stop" if hit_stop else "target" if hit_target else None
            if first == "stop":
                close(p, i, p.stop if p.side is Side.BUY else p.stop - spread, stop_reason(p), c_t, ambiguous=both)
            elif first == "target":
                close(p, i, p.target if p.side is Side.BUY else p.target - spread, "target", c_t, ambiguous=both)
            elif breakeven is not None or trail is not None:
                manage(p, c_t)
        last_i, last_bid, last_spread = i, _d(c_arr[i]), spread
    # cut-offs after the last candle that was processed (no execution possible any more)
    while k < len(cuts) and last_i is not None and cuts[k] <= times[last_i] + bar:
        decide_at(cuts[k], last_bid, last_spread)
        k += 1

    # end of window / data: close what is still open at the last processed close
    if open_pos and last_i is not None:
        at = times[last_i] + bar
        reason = "window_end" if force_close_at_end else "end_of_data"
        for p in list(open_pos):
            close(p, last_i, _d(c_arr[last_i]), reason, at)
    diag["skipped"]["window_end"] += len(pending)
    public = [{k: v for k, v in t.items() if not k.startswith("_")} for t in trades]
    return {"definition": VERSION, "strategy": {"name": config["name"], "fingerprint": builder.fingerprint(config)},
            "trades": public, "_trades": trades, "closed_equity": closed_curve, "marked_equity": marked_curve,
            "final_balance": _f(balance), "diagnostics": diag,
            "result_hash": hashlib.sha256(json.dumps(public, sort_keys=True, default=str).encode()).hexdigest()}


def holding_extension(config: dict[str, Any]) -> pd.Timedelta:
    """Candles needed after the window to manage the last positions (max holding, else one day)."""
    minutes = config["exit"]["max_holding_minutes"] or 24 * 60
    return pd.Timedelta(minutes=math.ceil(minutes * 1.5) + 60)


# ------------------------------------------------------------------------------- planners (shared with live)
def plan_stop_level(stop_cfg: dict[str, Any], side: Side, snap: dict[str, Any], entry_bid: Decimal,
                    entry_ask: Decimal) -> tuple[Decimal | None, str, float | None]:
    """The stop for a position opened at entry_bid/entry_ask from the snapshot's levels (known at the cut-off)."""
    item = snap["tf"].get(stop_cfg["timeframe"]) or {}
    atr = (item.get("vol") or {}).get("atr")
    ref = item.get("ref_low") if side is Side.BUY else item.get("ref_high")
    if stop_cfg["type"] == "none":                     # the rule has no stop: time limit / target / flip exits only
        return None, "no stop (the rule has none)", atr
    if stop_cfg["type"] == "fixed":
        d = _d(stop_cfg["distance"])
        return (entry_bid - d if side is Side.BUY else entry_ask + d), f"fixed {stop_cfg['distance']}", atr
    if stop_cfg["type"] == "structure" and ref is not None and atr:
        buf = _d(stop_cfg["buffer_atr"] * atr)
        level = _d(ref) - buf if side is Side.BUY else _d(ref) + buf
        if (side is Side.BUY and level < entry_bid) or (side is Side.SELL and level > entry_ask):
            return level, f"structure: {stop_cfg['timeframe']} swing {'low' if side is Side.BUY else 'high'} {ref}", atr
    if atr:
        d = _d(stop_cfg["atr_multiple"] * atr)
        return (entry_bid - d if side is Side.BUY else entry_ask + d), \
            f"ATR: {stop_cfg['atr_multiple']} x ATR({stop_cfg['timeframe']})" + \
            (" (structure level unusable)" if stop_cfg["type"] == "structure" else ""), atr
    return None, "no stop (ATR unavailable)", atr


def plan_target_level(target_cfg: dict[str, Any], side: Side, entry: Decimal, stop: Decimal | None,
                      snap: dict[str, Any]) -> Decimal | None:
    t = target_cfg["type"]
    if t == "none":
        return None
    if t == "r_multiple":
        if stop is None:
            return None
        dist = abs(entry - stop) * _d(target_cfg["r"])
    elif t == "atr":
        atr = ((snap["tf"].get(target_cfg["timeframe"]) or {}).get("vol") or {}).get("atr")
        if not atr:
            return None
        dist = _d(target_cfg["atr_multiple"] * atr)
    else:
        dist = _d(target_cfg["distance"])
    return entry + dist if side is Side.BUY else entry - dist


#: Phase 5.1 hypothetical stress-test sizing (monthly research only; never used by live monitoring or the paper
#: simulator): risk a percentage of CURRENT simulated equity, with the planned loss at the stop including costs.
RISK_EQUITY = "risk_percent_equity"
RISK_EQUITY_MAX_PERCENT = Decimal(10)


def size_risk_of_equity(*, equity: Decimal, risk_percent: Decimal, entry_price: Decimal, stop: Decimal | None,
                        slippage: Decimal, commission_per_lot_per_side: Decimal, spec: InstrumentSpec,
                        max_lots: Decimal | None = None) -> dict[str, Any]:
    """Position size for a monetary risk budget = equity x risk %.

    Planned loss per lot if the stop is reached = (|entry - stop| + exit slippage) x contract size + round-trip
    commission. The entry price already contains the entry spread and slippage (ask for buys; a sell's stop is
    triggered on the ask), so spread, commission and slippage are all inside the planned risk. Volume is rounded
    DOWN to the lot step, never up; it is capped at the broker maximum and at ``max_lots`` (the strategy's own
    exposure limit). Leverage plays no part here: margin is checked separately. Returns ``lots`` = None with a
    ``reason`` when the entry must be rejected (no stop, or the minimum volume would exceed the budget)."""
    budget = equity * risk_percent / Decimal(100)
    out: dict[str, Any] = {"equity": equity, "risk_percent": risk_percent, "budget": budget, "lots": None,
                           "per_lot_loss": None, "planned_risk": None, "capped_by": None, "reason": None}
    if stop is None:
        out["reason"] = "no_stop"
        return out
    per_lot = (abs(entry_price - stop) + slippage) * spec.contract_size + commission_per_lot_per_side * 2
    out["per_lot_loss"] = per_lot
    if budget <= 0 or per_lot <= 0:
        out["reason"] = "risk_budget_below_min_volume"
        return out
    lots = ((budget / per_lot / spec.lot_step).to_integral_value(rounding=ROUND_DOWN) * spec.lot_step).quantize(spec.lot_step)
    for cap, label in ((spec.max_lot, f"broker maximum {spec.max_lot} lots"),
                       (max_lots, f"strategy exposure limit {max_lots} lots")):
        if cap is not None and lots > cap:
            lots = (cap / spec.lot_step).to_integral_value(rounding=ROUND_DOWN) * spec.lot_step
            lots = lots.quantize(spec.lot_step)
            out["capped_by"] = label
    if lots < spec.min_lot:
        out["reason"] = "risk_budget_below_min_volume"
        out["min_lot_planned_risk"] = spec.min_lot * per_lot
        return out
    out["lots"] = lots
    out["planned_risk"] = lots * per_lot
    return out


def size_lots(risk: dict[str, Any], equity: Decimal, risk_per_oz: Decimal | None, spec: InstrumentSpec) -> Decimal | None:
    """fixed_lots, or risk_percent of equity / (risk per oz x contract size), floored to the lot step."""
    if risk["sizing"] == "fixed_lots":
        lots = Decimal(str(risk["lots"]))
    else:
        if not risk_per_oz:
            return None
        cash = equity * Decimal(str(risk["risk_percent"])) / Decimal(100)
        raw = cash / (risk_per_oz * spec.contract_size)
        lots = ((raw / spec.lot_step).to_integral_value(rounding=ROUND_DOWN) * spec.lot_step).quantize(spec.lot_step)
        lots = min(lots, spec.max_lot)
    return lots if lots >= spec.min_lot else None
