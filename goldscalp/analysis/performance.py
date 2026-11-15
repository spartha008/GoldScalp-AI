"""Historical signal performance evaluation (definition perf@1). PHASE2_ARCHITECTURE.md section 12.1.

Two SEPARATE results per stream (one stream per step timeframe):

A. Analytical signal evaluation (no costs): what price did after each directional signal over a
   fixed horizon (signed move, MFE, MAE in price and ATR(M5) units) and a favourable / adverse / flat
   classification. This describes the signals, not a trading result.

B. Hypothetical simulated strategy (with costs): one hypothetical position per directional signal,
   priced with the PAPER SIMULATOR'S OWN formulas (open/close fill prices, gross P/L, commission).
   Nothing is executed: no orders, no paper accounts, no positions are created.
   - entry: open of the first base candle after the signal's cut-off; long at ask + slippage,
     short at bid - slippage (ask = bid candle + that candle's spread, else the configured spread)
   - stop: the signal's first invalidation level (else stop_atr_fallback x ATR(M5)); target: target_r x
     the initial risk (0 = none); exit: first of stop / target / horizon. Stop and target in the same
     candle -> stop first (conservative); a gap through the stop fills at the open; slippage on exits
   - one position at a time per stream; signals while a position is open are counted as skipped
   - closed-trade equity from starting_balance, maximum drawdown, profit factor (only when meaningful)

A signal is a CHANGE of classification into BULLISH/BEARISH (a persisting classification is one
signal). Every step candle of the chosen range is evaluated (no sampling, no period selection).
Hypothetical past results do not predict future results.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis import multi_timeframe, signals
from goldscalp.analysis import sessions as sessions_mod
from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.replay import ReplayDataset, frames_at
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample
from goldscalp.simulation.costs import CostModel, commission
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.positions import Side, close_fill_price, gross_pnl, open_fill_price
from goldscalp.simulation.pricing import ExecutionQuote

VERSION = "perf@1"
DISCLAIMER = ("Hypothetical results computed on past data with the stated assumptions. They are not trading "
              "results, they include no live execution, and they do not predict future performance.")
ANALYTICAL_LABEL = "Analytical signal evaluation (no costs): what price did after each signal."
STRATEGY_LABEL = ("Hypothetical simulated strategy (with spread, commission and slippage), priced with the paper "
                  "simulator's formulas. No orders were placed.")
STEP_TIMEFRAMES = ("M1", "M5", "M15")


class EvaluationError(ValueError):
    pass


def _d(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def _f(value: Decimal | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


# ------------------------------------------------------------------------------- steps and signals
def step_cutoffs(ds: ReplayDataset, step_tf: Timeframe, start: datetime, end: datetime) -> list[pd.Timestamp]:
    """Close times of every step candle in [start, end] that exists in the base data (same as replay)."""
    window = ds.base[(ds.base.index >= pd.Timestamp(start)) & (ds.base.index < pd.Timestamp(end))]
    if window.empty:
        return []
    bars = window if step_tf is ds.base_tf else resample(window, ds.base_tf, step_tf)
    step = pd.Timedelta(seconds=step_tf.seconds)
    return [ts + step for ts in bars.index if ts + step <= pd.Timestamp(end)]


def gate_category(reason: str) -> str:
    for key, label in (("not LIVE", "market data not LIVE"), ("stale", "stale data"), ("spread", "spread too wide"),
                       ("required", "too few candles"), ("OHLC", "invalid candles"), ("non-finite", "invalid candles"),
                       ("increasing", "invalid candles")):
        if key in reason:
            return label
    return "other"


def active_session_label(defs: tuple[Any, ...], at: pd.Timestamp) -> str:
    names = sessions_mod.active_names(defs, at.to_pydatetime())
    if not names:
        return "none"
    return names[0] if len(names) == 1 else "/".join(names) + " overlap"


def generate_signals(ds: ReplayDataset, step_tf: Timeframe, cutoffs: list[pd.Timestamp], cfg: AnalysisConfig, *,
                     stream_id: str, generated_at: datetime,
                     progress: Callable[[int], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run the as-of signal at every cut-off. Only candles closed by each cut-off are visible."""
    events: list[dict[str, Any]] = []
    neutral: list[dict[str, Any]] = []
    gated: Counter[str] = Counter()
    by_step: Counter[str] = Counter()
    previous = None
    for n, cut in enumerate(cutoffs, 1):
        if cancelled is not None and cancelled():
            raise EvaluationError("cancelled")
        cutoff = cut.to_pydatetime()
        frames = frames_at(ds, cutoff)
        results = multi_timeframe.build(frames, cutoff, cfg, detail=signals.DETAIL_TIMEFRAMES)
        base_visible = frames.get(ds.base_tf.value)
        spread = None
        if base_visible is not None and len(base_visible):
            value = base_visible["spread"].iloc[-1]
            spread = float(value) if value is not None and np.isfinite(value) and value > 0 else None
        sig = signals.evaluate(results, cutoff, cfg, mode="replay", series=ds.series_key, market_state=None,
                               spread=spread, spread_source="historical candle spread" if spread is not None
                               else "unknown in this dataset", source=ds.source_id, generated_at=generated_at,
                               replay_id=stream_id)
        direction = sig["direction"]
        by_step[direction] += 1
        if direction == "NO SIGNAL":
            for reason in {gate_category(g) for g in sig["gates_failed"]}:
                gated[reason] += 1
        if direction != previous:
            if direction in ("BULLISH", "BEARISH"):
                events.append(sig)
            elif direction == "NEUTRAL":
                neutral.append(sig)
        previous = direction
        if progress is not None:
            progress(n)
    return {"events": events, "neutral": neutral, "gated_steps": dict(gated), "steps_by_direction": dict(by_step)}


# ------------------------------------------------------------------------------- A. analytical evaluation
def analytical_outcome(event: dict[str, Any], outcome_base: pd.DataFrame, base_tf: Timeframe, horizon_m5: int,
                       flat_band_atr: float) -> dict[str, Any]:
    after = outcome_base[outcome_base.index >= pd.Timestamp(event["cutoff"])]
    fo = signals.forward_outcome(event, after, base_tf, [horizon_m5])
    h = fo["horizons"][0] if fo.get("horizons") else {"status": "pending"}
    if h.get("status") != "complete":
        return {"classification": "insufficient_data", "horizon_m5": horizon_m5,
                "note": "the dataset does not cover the full horizon after this signal"}
    atr = event.get("atr_m5")
    band = flat_band_atr * atr if atr else 0.0
    if h["move"] > band:
        cls = "favourable"
    elif h["move"] < -band:
        cls = "adverse"
    else:
        cls = "flat"
    return {"classification": cls, "horizon_m5": horizon_m5, "until": h["until"], "reference_price": fo["reference_price"],
            "move": h["move"], "mfe": h["mfe"], "mae": h["mae"], "move_atr": h.get("move_atr"),
            "mfe_atr": h.get("mfe_atr"), "mae_atr": h.get("mae_atr"), "flat_band": round(band, 5)}


def neutral_abs_move(event: dict[str, Any], outcome_base: pd.DataFrame, base_tf: Timeframe,
                     horizon_m5: int) -> float | None:
    """|close at the horizon - reference price| in ATR(M5) units after a NEUTRAL observation (no direction)."""
    ref, atr = event.get("reference_price"), event.get("atr_m5")
    need = max(1, math.ceil(horizon_m5 * Timeframe.M5.seconds / base_tf.seconds))
    after = outcome_base[outcome_base.index >= pd.Timestamp(event["cutoff"])]
    if ref is None or not atr or len(after) < need:
        return None
    return round(abs(float(after["close"].iloc[need - 1]) - float(ref)) / float(atr), 3)


# ------------------------------------------------------------------------------- B. hypothetical trade
def simulate_trade(event: dict[str, Any], outcome_base: pd.DataFrame, base_tf: Timeframe, *, horizon_m5: int,
                   stop_atr_fallback: float, target_r: float, lots: Decimal, spec: InstrumentSpec,
                   costs: CostModel) -> dict[str, Any]:
    """One hypothetical position for a directional signal, priced with the paper simulator's formulas."""
    side = Side.BUY if event["direction"] == "BULLISH" else Side.SELL
    pos = int(outcome_base.index.searchsorted(pd.Timestamp(event["cutoff"]), side="left"))
    need = max(1, math.ceil(horizon_m5 * Timeframe.M5.seconds / base_tf.seconds))
    window = outcome_base.iloc[pos:pos + need]
    if len(window) < need:
        return {"outcome": "insufficient_data", "note": "the dataset does not cover the full holding horizon"}
    o, h, low, c = (window[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    raw_spread = window["spread"].to_numpy(dtype="float64") if "spread" in window else np.full(len(window), np.nan)
    fallback = costs.configured_spread or Decimal(0)

    def spread_at(i: int) -> tuple[Decimal, str]:
        value = raw_spread[i]
        return (_d(value), "candle") if np.isfinite(value) and value > 0 else (fallback, "configured")

    slip = costs.slippage
    s0, s0_source = spread_at(0)
    bid0 = _d(o[0])
    entry_quote = ExecutionQuote(bid=bid0, ask=bid0 + s0, spread=s0, spread_source=s0_source,
                                 quote_time=window.index[0].to_pydatetime(), state=MarketState.HISTORICAL,
                                 source_id="historical-candles")
    entry = open_fill_price(side, entry_quote, slip)
    stop: Decimal | None = None
    stop_source = None
    inv = (event.get("invalidation") or [None])[0]
    if inv is not None:
        level = _d(inv["price"])
        if (side is Side.BUY and level < entry_quote.bid) or (side is Side.SELL and level > entry_quote.ask):
            stop, stop_source = level, f"invalidation: {inv['condition']}"
    if stop is None and event.get("atr_m5") and stop_atr_fallback > 0:
        dist = _d(stop_atr_fallback * float(event["atr_m5"]))
        stop = entry_quote.bid - dist if side is Side.BUY else entry_quote.ask + dist
        stop_source = f"fallback: {stop_atr_fallback} x ATR(M5)"
    risk = abs(entry - stop) if stop is not None else None
    target = None
    if risk and target_r > 0:
        target = entry + _d(target_r) * risk if side is Side.BUY else entry - _d(target_r) * risk

    reason, exit_index, exit_side_price = "horizon", len(window) - 1, None
    for i in range(len(window)):
        s_i, _ = spread_at(i)
        bo, bh, bl = _d(o[i]), _d(h[i]), _d(low[i])
        if side is Side.BUY:
            if stop is not None and bl <= stop:
                reason, exit_index, exit_side_price = "stop", i, (bo if bo <= stop else stop)
                break
            if target is not None and bh >= target:
                reason, exit_index, exit_side_price = "target", i, target
                break
        else:
            ao, ah, al = bo + s_i, bh + s_i, bl + s_i
            if stop is not None and ah >= stop:
                reason, exit_index, exit_side_price = "stop", i, (ao if ao >= stop else stop)
                break
            if target is not None and al <= target:
                reason, exit_index, exit_side_price = "target", i, target
                break
    s_x, s_x_source = spread_at(exit_index)
    if exit_side_price is None:                       # horizon exit at the last candle's close
        bid_x = _d(c[exit_index])
    else:
        bid_x = exit_side_price if side is Side.BUY else exit_side_price - s_x
    exit_quote = ExecutionQuote(bid=bid_x, ask=bid_x + s_x, spread=s_x, spread_source=s_x_source,
                                quote_time=window.index[exit_index].to_pydatetime(), state=MarketState.HISTORICAL,
                                source_id="historical-candles")
    exit_price = close_fill_price(side, exit_quote, slip)
    ounces = spec.ounces(lots)
    gross = gross_pnl(side, entry, exit_price, ounces)
    commissions = commission(lots, costs) * 2
    net = gross - commissions
    spread_cost = (s0 if side is Side.BUY else s_x) * ounces
    if reason == "horizon":
        outcome = "horizon_win" if net > 0 else "horizon_loss"
    else:
        outcome = reason
    exit_time = window.index[exit_index] + pd.Timedelta(seconds=base_tf.seconds)
    return {
        "outcome": outcome, "side": side.value, "lots": str(lots), "ounces": str(ounces),
        "entry_time": iso(window.index[0].to_pydatetime()), "entry_price": _f(entry, 3),
        "entry_spread": _f(s0, 3), "entry_spread_source": s0_source,
        "stop": _f(stop, 3), "stop_source": stop_source, "target": _f(target, 3), "risk_per_oz": _f(risk, 3),
        "exit_time": iso(exit_time.to_pydatetime()), "exit_price": _f(exit_price, 3), "exit_reason": reason,
        "gross_pnl": _f(gross), "commission": _f(commissions), "spread_cost": _f(spread_cost),
        "slippage_cost": _f(slip * ounces * 2), "net_pnl": _f(net),
        "r_multiple": _f(net / (risk * ounces), 2) if risk else None,
        "_net": net, "_exit_ts": exit_time,
    }


# ------------------------------------------------------------------------------- aggregation
def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def profit_factor(nets: list[Decimal], min_trades: int) -> dict[str, Any]:
    gp = sum((n for n in nets if n > 0), Decimal(0))
    gl = -sum((n for n in nets if n < 0), Decimal(0))
    if len(nets) < min_trades:
        return {"value": None, "gross_profit": _f(gp), "gross_loss": _f(gl),
                "reason": f"not meaningful: {len(nets)} trade(s), at least {min_trades} required"}
    if gl == 0:
        return {"value": None, "gross_profit": _f(gp), "gross_loss": _f(gl), "reason": "not meaningful: no losing trades"}
    return {"value": round(float(gp / gl), 3), "gross_profit": _f(gp), "gross_loss": _f(gl), "reason": None}


def equity_curve(trades: list[dict[str, Any]], starting_balance: Decimal) -> dict[str, Any]:
    equity, peak = starting_balance, starting_balance
    max_dd, max_dd_pct = Decimal(0), Decimal(0)
    points = []
    for t in trades:
        equity += t["_net"]
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if peak > 0 and dd / peak > max_dd_pct:
            max_dd_pct = dd / peak
        points.append({"time": t["exit_time"], "equity": _f(equity)})
    return {"starting_balance": _f(starting_balance), "final_equity": _f(equity), "points": points,
            "max_drawdown": _f(max_dd), "max_drawdown_pct": _f(max_dd_pct * 100, 2),
            "basis": "closed-trade equity (updated when each hypothetical position closes)"}


def analytical_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [i for i in items if i["analytical"]["classification"] != "insufficient_data"]
    cls = Counter(i["analytical"]["classification"] for i in items)
    a = [i["analytical"] for i in evaluated]
    return {"signals": len(items), "evaluated": len(evaluated),
            "favourable": cls.get("favourable", 0), "adverse": cls.get("adverse", 0), "flat": cls.get("flat", 0),
            "insufficient_data": cls.get("insufficient_data", 0),
            "avg_move_atr": _mean([x.get("move_atr") for x in a]),
            "avg_favourable_atr": _mean([x.get("mfe_atr") for x in a]),
            "avg_adverse_atr": _mean([x.get("mae_atr") for x in a]),
            "avg_favourable": _mean([x["mfe"] for x in a]), "avg_adverse": _mean([x["mae"] for x in a]),
            "max_favourable": max((x["mfe"] for x in a), default=None),
            "max_adverse": max((x["mae"] for x in a), default=None),
            "max_favourable_atr": max((x["mfe_atr"] for x in a if x.get("mfe_atr") is not None), default=None),
            "max_adverse_atr": max((x["mae_atr"] for x in a if x.get("mae_atr") is not None), default=None)}


def strategy_summary(trades: list[dict[str, Any]], skipped: int, insufficient: int, min_trades: int,
                     starting_balance: Decimal) -> dict[str, Any]:
    nets = [t["_net"] for t in trades]
    outcomes = Counter(t["outcome"] for t in trades)
    curve = equity_curve(trades, starting_balance)
    wins = sum(1 for n in nets if n > 0)
    return {"trades": len(trades), "wins": wins, "losses": len(trades) - wins, "outcomes": dict(outcomes),
            "skipped_overlap": skipped, "insufficient_data": insufficient,
            "net_pnl": _f(sum(nets, Decimal(0))), "avg_net_pnl": _f(sum(nets, Decimal(0)) / len(nets)) if nets else None,
            "total_commission": _f(sum((_d(t["commission"]) for t in trades), Decimal(0))),
            "total_spread_cost": _f(sum((_d(t["spread_cost"]) for t in trades), Decimal(0))),
            "total_slippage_cost": _f(sum((_d(t["slippage_cost"]) for t in trades), Decimal(0))),
            "profit_factor": profit_factor(nets, min_trades), "equity": curve,
            "max_drawdown": curve["max_drawdown"], "max_drawdown_pct": curve["max_drawdown_pct"]}


def group_table(items: list[dict[str, Any]], key: str, min_trades: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item[key]), []).append(item)
    rows = []
    for name, members in sorted(groups.items()):
        a = analytical_summary(members)
        traded = [m["trade"] for m in members if m.get("trade") and m["trade"]["outcome"] != "insufficient_data"
                  and not m.get("skipped")]
        nets = [t["_net"] for t in traded]
        rows.append({"group": name, "signals": a["signals"], "favourable": a["favourable"], "adverse": a["adverse"],
                     "flat": a["flat"], "avg_move_atr": a["avg_move_atr"], "avg_favourable_atr": a["avg_favourable_atr"],
                     "avg_adverse_atr": a["avg_adverse_atr"], "trades": len(traded),
                     "net_pnl": _f(sum(nets, Decimal(0))), "profit_factor": profit_factor(nets, min_trades)})
    return rows


def evaluate_stream(ds: ReplayDataset, outcome_base: pd.DataFrame, step_tf: Timeframe, generated: dict[str, Any],
                    cfg: AnalysisConfig, params: dict[str, Any], spec: InstrumentSpec, costs: CostModel) -> dict[str, Any]:
    """Outcomes for one stream's events (analytical + hypothetical strategy)."""
    pcfg = params
    defs = sessions_mod.definitions(cfg)
    lots = _d(pcfg["lots"])
    items = []
    busy_until: pd.Timestamp | None = None
    trades, skipped, insufficient = [], 0, 0
    for ev in generated["events"]:
        cut = pd.Timestamp(ev["cutoff"])
        item = {"signal": ev, "timeframe": step_tf.value, "direction": ev["direction"],
                "session": active_session_label(defs, cut),
                "volatility": ((ev.get("context") or {}).get("volatility") or {}).get("M5", {}).get("condition") or "unknown",
                "analytical": analytical_outcome(ev, outcome_base, ds.base_tf, int(pcfg["horizon_m5"]),
                                                 float(pcfg["flat_band_atr"]))}
        if busy_until is not None and cut < busy_until:
            item["skipped"] = True
            item["trade"] = {"outcome": "skipped_overlap", "note": "a hypothetical position was still open"}
            skipped += 1
        else:
            trade = simulate_trade(ev, outcome_base, ds.base_tf, horizon_m5=int(pcfg["horizon_m5"]),
                                   stop_atr_fallback=float(pcfg["stop_atr_fallback"]),
                                   target_r=float(pcfg["target_r"]), lots=lots, spec=spec, costs=costs)
            item["trade"] = trade
            if trade["outcome"] == "insufficient_data":
                insufficient += 1
            else:
                trades.append(trade)
                busy_until = trade["_exit_ts"]
        items.append(item)
    abs_moves = [m for m in (neutral_abs_move(n, outcome_base, ds.base_tf, int(pcfg["horizon_m5"]))
                             for n in generated["neutral"]) if m is not None]
    min_trades = int(pcfg["min_trades_for_ratios"])
    return {
        "timeframe": step_tf.value,
        "counts": {"total_signals": len(generated["events"]) + len(generated["neutral"]),
                   "bullish": sum(1 for e in generated["events"] if e["direction"] == "BULLISH"),
                   "bearish": sum(1 for e in generated["events"] if e["direction"] == "BEARISH"),
                   "neutral_observations": len(generated["neutral"]),
                   "steps_by_direction": generated["steps_by_direction"], "gated_steps": generated["gated_steps"]},
        "analytical": {**analytical_summary(items), "label": ANALYTICAL_LABEL,
                       "neutral_avg_abs_move_atr": _mean(abs_moves)},
        "strategy": {**strategy_summary(trades, skipped, insufficient, min_trades, _d(pcfg["starting_balance"])),
                     "label": STRATEGY_LABEL},
        "by_session": group_table(items, "session", min_trades),
        "by_volatility": group_table(items, "volatility", min_trades),
        "by_direction": group_table(items, "direction", min_trades),
        "items": items,
    }


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    """A compact, JSON-safe row for one evaluated signal."""
    ev = item["signal"]
    trade = {k: v for k, v in item["trade"].items() if not k.startswith("_")}
    return {"signal_id": ev["id"], "cutoff": ev["cutoff"], "timeframe": item["timeframe"], "direction": ev["direction"],
            "score": ev["score"], "session": item["session"], "volatility": item["volatility"],
            "analytical": item["analytical"], "trade": trade}


def assumptions(params: dict[str, Any], costs: CostModel, spec: InstrumentSpec) -> dict[str, Any]:
    return {
        "signal": "a change of classification into BULLISH or BEARISH (signal@1); a persisting classification "
                  "counts once",
        "horizon": f"{params['horizon_m5']} M5 candles ({params['horizon_m5'] * 5} minutes) after the signal cut-off",
        "analytical": f"signed close-to-close move from the signal's reference price; flat when within "
                      f"±{params['flat_band_atr']} x ATR(M5); MFE/MAE from candle highs and lows",
        "entry": "open of the first base candle after the cut-off; long at ask + slippage, short at bid - slippage",
        "stop": f"the signal's first invalidation level, else {params['stop_atr_fallback']} x ATR(M5); a gap "
                "through the stop fills at the candle open",
        "target": f"{params['target_r']} x the initial risk" if float(params["target_r"]) > 0 else "none",
        "exit": "first of stop, target or the horizon close; stop assumed first when both are touched in one candle",
        "costs": {"spread": "each candle's stored spread; the configured spread when the dataset has none",
                  "configured_spread": str(costs.configured_spread),
                  "commission_per_lot_per_side": str(costs.commission_per_lot_per_side),
                  "slippage_per_fill": str(costs.slippage)},
        "size": f"{params['lots']} lots x {spec.contract_size} {spec.unit} per lot (instrument {spec.name})",
        "positions": "one hypothetical position at a time per timeframe stream",
        "equity": f"closed-trade equity from {params['starting_balance']} USD",
    }


def evaluate(ds: ReplayDataset, outcome_base: pd.DataFrame, timeframes: list[Timeframe], start: datetime,
             end: datetime, cfg: AnalysisConfig, params: dict[str, Any], spec: InstrumentSpec, costs: CostModel, *,
             run_id: str, generated_at: datetime, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Evaluate every step of [start, end] for each step timeframe. ``ds`` must not contain candles after
    ``end`` (signals); ``outcome_base`` may (outcomes only)."""
    start, end = ensure_aware(start), ensure_aware(end)
    plans = [(tf, step_cutoffs(ds, tf, start, end)) for tf in timeframes]
    total = sum(len(c) for _, c in plans)
    if total == 0:
        raise EvaluationError("no complete step candles between the chosen start and end")
    if total > int(params["max_steps"]):
        raise EvaluationError(f"{total} steps exceed the limit of {params['max_steps']}; choose a shorter range or "
                              "a coarser step timeframe (the range is never shortened automatically)")
    done = 0
    streams = []
    for tf, cutoffs in plans:
        def tick(n: int, offset: int = done, last: int = len(cutoffs)) -> None:
            if progress is not None and (n % 10 == 0 or n == last):
                progress(offset + n, total)
        generated = generate_signals(ds, tf, cutoffs, cfg, stream_id=f"{run_id}:{tf.value}", generated_at=generated_at,
                                     progress=tick, cancelled=cancelled)
        done += len(cutoffs)
        stream = evaluate_stream(ds, outcome_base, tf, generated, cfg, params, spec, costs)
        stream["steps"] = len(cutoffs)
        stream["first_step"] = iso(cutoffs[0].to_pydatetime()) if cutoffs else None
        stream["last_step"] = iso(cutoffs[-1].to_pydatetime()) if cutoffs else None
        streams.append(stream)
    all_items = [i for s in streams for i in s["items"]]
    min_trades = int(params["min_trades_for_ratios"])
    directional = sum(s["counts"]["bullish"] + s["counts"]["bearish"] for s in streams)
    warnings = []
    if directional < int(params["min_signals_warning"]):
        warnings.append(f"small sample: {directional} directional signal(s); fewer than {params['min_signals_warning']} "
                        "signals say little about how the rules behave")
    return {
        "definition": VERSION, "disclaimer": DISCLAIMER, "range": {"start": iso(start), "end": iso(end)},
        "series": ds.series_key, "base_tf": ds.base_tf.value, "steps": total,
        "timeframes": [tf.value for tf in timeframes], "assumptions": assumptions(params, costs, spec),
        "params": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in params.items()},
        "streams": [{k: v for k, v in s.items() if k != "items"} for s in streams],
        "by_timeframe": group_table(all_items, "timeframe", min_trades),
        "by_session": group_table(all_items, "session", min_trades),
        "by_volatility": group_table(all_items, "volatility", min_trades),
        "items": [public_item(i) for i in all_items], "warnings": warnings,
        "definitions": signals.DEFINITIONS | {"performance": VERSION}, "config_fingerprint": cfg.fingerprint,
        "_items": all_items,
    }
