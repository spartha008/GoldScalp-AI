"""Live strategy monitor (definition live@1). PHASE4_ARCHITECTURE.md sections 4-5.

For every ACTIVE monitored strategy VERSION, at each close of its step candle (the cut-off):

  1. wait until the M1 candle ending at the cut-off is stored and closed (grace 120 s, else 'missed');
  2. gate: market state LIVE and a fresh quote (else 'skipped', no signal);
  3. snapshot = the BACKTEST's as-of pass on stored closed candles (load_dataset -> features.compute_timeline
     for exactly this cut-off) -> conditions.decide; the entry trigger uses the persisted previous decision
     like the engine's last_pass. A live signal is therefore the decision the backtest makes at that cut-off;
  4. a fired setup becomes a timestamped, explained strategy signal (evidence, conflicts, invalidation, price,
     bid/ask/spread, source, freshness, context, plan);
  5. paper mode: the signal opens ONE SIMULATED position in the version's own validation paper account
     through PaperTradingEngine (the only execution simulator). Nothing reaches a broker.

Every cycle also manages open SIMULATED trades (excursions from quotes, time exits, structure-flip exits at
cut-offs, closes detected from the paper engine), writes per-trade evaluations and measures each signal
against the real prices that followed (plus the backtest replica of the same decision)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from goldscalp.api.services import AppServices, active_series_key, clean, load_dataset, pick_series
from goldscalp.backtesting import engine as bt
from goldscalp.backtesting import features
from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.live import evaluation as trade_eval
from goldscalp.live.feed import FeedMonitor
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.portfolio import OrderRejected
from goldscalp.simulation.positions import CloseReason, Side, open_fill_price
from goldscalp.storage import candle_repo, live_repo, research_repo, strategy_repo
from goldscalp.strategies import builder, conditions

log = logging.getLogger("goldscalp.live")

VERSION = "live@1"
GRACE = timedelta(seconds=120)
POLL_SECONDS = 2.0
OUTCOME_POINTS = (5, 15, 60)
LABEL = "SIMULATED analytical signal - an observation, not advice; no order is placed"


# ------------------------------------------------------------------------------------------------ eligibility
def eligibility(con: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 validation of this exact strategy version: completed research runs containing its fingerprint."""
    runs = []
    for run in research_repo.list_runs(con, limit=500):
        if run["status"] != "complete":
            continue
        refs = run["params"].get("strategies") or []
        if not any(r.get("fingerprint") == record["fingerprint"] for r in refs):
            continue
        full = research_repo.get(con, run["id"]) or {}
        res = full.get("result") or {}
        metrics: dict[str, Any] = {}
        if run["kind"] == "walk_forward" and res.get("mode") == "select":
            # selection runs: only the folds in which THIS version was selected belong to it
            name = next(r.get("name") for r in refs if r.get("fingerprint") == record["fingerprint"])
            mine = [f for f in res.get("folds") or [] if f.get("selected") == name and f.get("evaluation")]
            trades = sum(f["evaluation"]["trades"] for f in mine)
            min_trades = int((run["params"].get("assumptions") or {}).get("min_trades_for_ratios", 30))
            metrics = {"trades": trades, "net": round(sum(f["evaluation"]["net"] for f in mine), 2), "win_rate": None,
                       "profit_factor": None, "expectancy_r": None, "sufficient": trades >= min_trades,
                       "max_drawdown": None,
                       "scope": f"out-of-sample folds where this version was selected ({len(mine)} of "
                                f"{len(res.get('folds') or [])})"}
        elif run["kind"] == "walk_forward":
            oos = res.get("out_of_sample") or {}
            metrics = {k: oos.get(k) for k in ("trades", "net", "win_rate", "profit_factor", "expectancy_r",
                                               "sufficient", "max_drawdown")}
            metrics["scope"] = "out-of-sample folds"
        else:
            for row in res.get("comparison") or []:
                if (row.get("strategy") or {}).get("fingerprint") == record["fingerprint"]:
                    metrics = {k: row.get(k) for k in ("trades", "net", "win_rate", "profit_factor", "expectancy_r",
                                                       "sufficient", "max_drawdown")}
                    metrics["scope"] = "full range"
                    break
        runs.append({"id": run["id"], "kind": run["kind"], "name": run["name"], "created_at": run["created_at"],
                     "range": {"start": run["params"].get("start"), "end": run["params"].get("end")},
                     "series": run["series_key"], **metrics})
    sufficient = any(r.get("sufficient") for r in runs)
    reason = None if runs else ("no completed Phase 3 backtest, comparison or walk-forward run contains this exact "
                                "strategy version (fingerprint); run one in the Strategy Lab first")
    return {"eligible": bool(runs), "runs": runs[:20], "sufficient_sample": sufficient, "reason": reason,
            "note": None if sufficient or not runs else
            "validated runs exist, but none has the minimum sample for ratio metrics: treat results as preliminary"}


# ------------------------------------------------------------------------------------------------ helpers
def signal_id(strategy_id: int, version: int, cutoff: str, side: str) -> str:
    return "ls_" + hashlib.sha256(f"{strategy_id}|{version}|{cutoff}|{side}".encode()).hexdigest()[:20]


def floor_time(now: datetime, tf: Timeframe) -> datetime:
    return datetime.fromtimestamp(tf.floor(int(now.timestamp())), tz=UTC)


def against_state(side: str) -> str:
    return "bearish" if side == "long" else "bullish"


def with_state(side: str) -> str:
    return "bullish" if side == "long" else "bearish"


def context_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    return {"cutoff": snap["cutoff"], "sessions": snap["sessions"], "price": snap["price"],
            "candle_spread": snap["spread"], "signal": snap.get("signal"),
            "timeframes": {tf: {"state": item.get("state"), "volatility": (item.get("vol") or {}).get("condition"),
                                "atr": (item.get("vol") or {}).get("atr"), "atr_pct": (item.get("vol") or {}).get("atr_pct"),
                                "last_high": item.get("high_label"), "last_low": item.get("low_label")}
                           for tf, item in snap["tf"].items()}}


def conflicts(config: dict[str, Any], snap: dict[str, Any], side: str, live_spread: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence AGAINST the signal: opposite-side conditions, higher timeframes against, an opposite Phase 2
    signal, a live spread above the last hour's 95th percentile."""
    other = "short" if side == "long" else "long"
    out = []
    opposite = conditions.evaluate(config, snap, other)
    passed_other = [c for c in opposite["conditions"] if c["passed"] and c["type"] not in ("session", "spread")]
    if passed_other:
        out.append({"kind": "opposite_conditions",
                    "evidence": f"{len(passed_other)} condition(s) also hold for a {other}: "
                                + "; ".join(c["evidence"] for c in passed_other[:4])})
    step = config["evaluation"]["step_timeframe"]
    for tf in ("H1", "M15", "M5"):
        if tf == step or tf not in snap["tf"]:
            continue
        state = snap["tf"][tf].get("state")
        if state == against_state(side):
            out.append({"kind": "htf_against", "timeframe": tf, "evidence": f"{tf} structure is {state} (against the {side})"})
        elif state == "ranging":
            out.append({"kind": "htf_ranging", "timeframe": tf, "evidence": f"{tf} structure is ranging (no trend support)"})
    sig = snap.get("signal") or {}
    opposite_dir = "BEARISH" if side == "long" else "BULLISH"
    if sig.get("direction") == opposite_dir:
        out.append({"kind": "signal_opposite", "evidence": f"Phase 2 signal@1 is {opposite_dir} (score {sig.get('score')})"})
    if live_spread.get("above_p95"):
        out.append({"kind": "spread_wide", "evidence": f"live spread {live_spread['spread']} is above the last hour's "
                                                       f"95th percentile {live_spread['p95']}"})
    return out


def plan_trade(config: dict[str, Any], side: str, snap: dict[str, Any], bid: Decimal, ask: Decimal,
               slippage: Decimal, equity: Decimal, spec: Any) -> dict[str, Any]:
    """Stop / target / size with the BACKTEST's planners (engine.plan_stop_level etc.)."""
    side_e = Side.BUY if side == "long" else Side.SELL
    q = execution_quote_like(bid, ask)
    entry = open_fill_price(side_e, q, slippage)
    stop, stop_source, atr = bt.plan_stop_level(config["exit"]["stop"], side_e, snap, bid, ask)
    target = bt.plan_target_level(config["exit"]["target"], side_e, entry, stop, snap)
    risk_per_oz = abs(entry - stop) if stop is not None else None
    lots = bt.size_lots(config["risk"], equity, risk_per_oz, spec)
    tick = Decimal(10) ** -int(spec.price_decimals)            # paper SL/TP levels on the instrument's price grid
    stop = None if stop is None else stop.quantize(tick)
    target = None if target is None else target.quantize(tick)
    return {"side": side_e.value, "entry_estimate": str(round(entry, 3)),
            "entry_assumption": f"simulated market fill at the {'ask' if side_e is Side.BUY else 'bid'} plus the paper "
                                f"slippage {slippage} when the cut-off is processed",
            "stop": None if stop is None else str(stop), "stop_source": stop_source,
            "target": None if target is None else str(target),
            "risk_per_oz": None if risk_per_oz is None else str(round(risk_per_oz, 3)),
            "atr": atr, "lots": None if lots is None else str(lots), "sizing": config["risk"]["sizing"],
            "max_holding_minutes": config["exit"]["max_holding_minutes"],
            "exit_on_structure_flip": config["exit"]["exit_on_structure_flip"],
            "max_entry_delay_minutes": config["entry"]["max_entry_delay_minutes"]}


class _Q:
    def __init__(self, bid: Decimal, ask: Decimal) -> None:
        self.bid, self.ask = bid, ask


def execution_quote_like(bid: Decimal, ask: Decimal) -> Any:
    return _Q(bid, ask)


def invalidation(plan: dict[str, Any], side: str) -> list[dict[str, Any]]:
    out = []
    if plan.get("stop"):
        px = "bid" if side == "long" else "ask"
        cmp = "at or below" if side == "long" else "at or above"
        out.append({"kind": "stop", "evidence": f"a {px} {cmp} {plan['stop']} invalidates the setup ({plan['stop_source']})"})
    if plan.get("max_holding_minutes"):
        out.append({"kind": "time", "evidence": f"unresolved after {plan['max_holding_minutes']} minutes: time exit"})
    flip = plan.get("exit_on_structure_flip") or {}
    if flip.get("enabled"):
        out.append({"kind": "structure_flip",
                    "evidence": f"{flip['timeframe']} structure turning {against_state(side)} at a later cut-off: "
                                "strategy exit"})
    out.append({"kind": "entry_delay",
                "evidence": f"no entry later than {plan['max_entry_delay_minutes']} minutes after the cut-off"})
    return out


# ------------------------------------------------------------------------------------------------ monitor
class LiveMonitor:
    def __init__(self, gs: AppServices, feed: FeedMonitor) -> None:
        self.gs = gs
        self.feed = feed
        self._task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self.last_cycle: str | None = None
        self.last_error: str | None = None
        self.cycles = 0
        # Optional MT5 DEMO-account mirror (goldscalp/execution/mirror.py), attached by the app. It only FOLLOWS the
        # simulated validation trades below; nothing in this monitor depends on it.
        self.demo: Any = None

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="goldscalp-live-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.cycle)
            except Exception as exc:  # noqa: BLE001 - the monitor must keep running
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("live monitor cycle failed")
            await asyncio.sleep(POLL_SECONDS)

    def status(self) -> dict[str, Any]:
        return {"definition": VERSION, "running": self._task is not None, "last_cycle": self.last_cycle,
                "cycles": self.cycles, "last_error": self.last_error}

    # -------------------------------------------------------------- one cycle
    def cycle(self, now: datetime | None = None) -> None:
        with self._lock:
            now = now or self.gs.clock.now()
            self.feed.tick(now)
            with self.gs.db.read() as con:
                active = live_repo.monitored_list(con, status="active")
            snaps: dict[tuple, Any] = {}
            for m in active:
                try:
                    self._process(m, now, snaps)
                except Exception as exc:  # noqa: BLE001 - one strategy must not stop the others
                    self.last_error = f"monitored {m['id']}: {type(exc).__name__}: {exc}"
                    log.exception("monitor step failed for monitored strategy %s", m["id"])
            self.on_quote_excursions(now)
            self._manage_open_trades(now)
            if self.demo is not None:
                try:
                    self.demo.reconcile(now)
                except Exception as exc:  # noqa: BLE001 - demo execution must never stop the monitor
                    self.demo.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("demo execution step failed")
            self._signal_outcomes(now)
            self.last_cycle = iso(now)
            self.cycles += 1

    def on_quote(self) -> None:
        """Market-hub listener (registered after the paper engine): excursions on every accepted quote."""
        self.on_quote_excursions(self.gs.clock.now())

    # -------------------------------------------------------------- per monitored version
    def _series_row(self) -> Any:
        key = active_series_key(self.gs)
        if key is None:
            return None
        try:
            return pick_series(self.gs.db, key)
        except Exception:  # noqa: BLE001
            return None

    def _last_closed_m1_end(self, row: Any) -> datetime | None:
        with self.gs.db.read() as con:
            r = con.execute("SELECT MAX(ts) FROM candles WHERE series_id = ? AND tf = 'M1' AND closed = 1",
                            (row["id"],)).fetchone()
        return None if r is None or r[0] is None else datetime.fromtimestamp(int(r[0]) + 60, tz=UTC)

    def snapshot_at(self, row: Any, step: Timeframe, cutoff: datetime, detail: set[str],
                    cache: dict[tuple, Any] | None = None) -> dict[str, Any] | None:
        """The backtest's snapshot for exactly this cut-off (only candles closed by it)."""
        key = (row["key"], step.value, iso(cutoff), tuple(sorted(detail)))
        if cache is not None and key in cache:
            return cache[key]
        lookback = int(self.gs.settings.live_lookback_days)
        ds = load_dataset(self.gs.db, row, step, cutoff - timedelta(seconds=step.seconds), cutoff, lookback)
        snap = None
        if not ds.base.empty:
            cuts = features.step_cutoffs(ds, step, cutoff - timedelta(seconds=step.seconds), cutoff)
            if cuts and cuts[-1] == pd.Timestamp(cutoff):
                snap = features.compute_timeline(ds, step, [cuts[-1]], self.gs.analysis_cfg, detail=detail)[0]
        if cache is not None:
            cache[key] = snap
        return snap

    def _record(self, m: dict[str, Any], cutoff: datetime, now: datetime, outcome: str, reason: str | None,
                state: dict[str, Any], sig_id: str | None = None) -> None:
        state = {**state, "last_cutoff": iso(cutoff), "last_outcome": outcome, "last_reason": reason}
        with self.gs.db.transaction() as con:
            live_repo.insert_evaluation(con, m["id"], iso(cutoff), now, outcome, reason, sig_id)
            live_repo.update_monitored(con, m["id"], now, state=state)

    def _process(self, m: dict[str, Any], now: datetime, snaps: dict[tuple, Any]) -> None:
        config = m["config"]
        step = Timeframe.parse(config["evaluation"]["step_timeframe"])
        due = floor_time(now, step)
        state = dict(m["state"])
        last = state.get("last_cutoff")
        if last and parse_iso_aware(last) >= due:
            return
        if parse_iso_aware(m["started_at"]) > due:          # never evaluate a candle that closed before monitoring began
            return
        if not self.gs.spec.schedule.is_open(due - timedelta(seconds=1)):
            return      # the step candle ending at `due` lies outside trading hours (weekly close): nothing to evaluate
        row = self._series_row()
        if row is None:
            self._record(m, due, now, "skipped", "no live candle series for the active provider", state)
            return
        ready = self._last_closed_m1_end(row)
        if ready is None or ready < due:
            if now - due < GRACE:
                return
            self._record(m, due, now, "missed", f"the M1 candle closing at {iso(due)} was not stored within "
                                                f"{int(GRACE.total_seconds())} s", state)
            return
        market_state, reason = self.gs.market.state()
        quote = self.gs.market.market_quote()
        max_age = float(self.gs.settings.live_max_quote_age_seconds)
        if market_state is not MarketState.LIVE or quote is None or quote.bid is None or quote.ask is None:
            self._record(m, due, now, "skipped", f"market data is {market_state.value}: {reason}", state)
            return
        age = (now - quote.quote_time).total_seconds()
        if age > max_age:
            self._record(m, due, now, "skipped", f"latest quote is {age:.0f} s old (limit {max_age:.0f} s)", state)
            return
        detail = builder.detail_timeframes(config)
        snap = self.snapshot_at(row, step, due, detail, snaps)
        if snap is None:
            self._record(m, due, now, "skipped", f"the {step.value} candle closing at {iso(due)} is not in stored data",
                         state)
            return
        decision = conditions.decide(config, snap)
        self._recheck_open_trades(m, config, snap, decision, now)
        last_pass = dict(state.get("last_pass") or {"long": False, "short": False})
        trigger = config["entry"]["trigger"]
        fire = None
        for side in ("long", "short"):
            passing = decision["setup"] == side
            if passing and (trigger == "every_step" or not last_pass.get(side)):
                fire = side
            last_pass[side] = passing
        state["last_pass"] = last_pass
        if fire is None:
            gates = [g for e in decision["evaluations"].values() for g in e["gate"]]
            outcome = ("ambiguous" if decision["ambiguous"] else "repeat" if decision["setup"] else
                       "gated" if gates and all(e["gate"] for e in decision["evaluations"].values()) else "no_setup")
            why = ("; ".join(gates[:3]) if outcome == "gated" else
                   f"{decision['setup']} setup still passing (trigger on_change)" if outcome == "repeat" else
                   "both directions passed" if outcome == "ambiguous" else None)
            self._record(m, due, now, outcome, why, state)
            return
        sig = self._make_signal(m, row, snap, decision, fire, quote, now)
        with self.gs.db.transaction() as con:
            live_repo.insert_signal(con, sig)
        if sig["status"] != "blocked" and m["mode"] == "paper":
            self._open_trade(m, sig, snap, now)
        self._record(m, due, now, "setup", f"{fire} signal {sig['id']}", state, sig["id"])

    # -------------------------------------------------------------- signals
    def _source(self) -> dict[str, Any]:
        provider = getattr(getattr(self.gs.market, "service", None), "provider", None)
        details = provider.details() if provider is not None and hasattr(provider, "details") else {}
        term = (details or {}).get("terminal") or {}
        active = getattr(self.gs.market, "active", None)
        view = self.gs.market.providers_view() if hasattr(self.gs.market, "providers_view") else {}
        return {"provider": active, "provider_name": view.get("active_name", active), "broker": term.get("broker"),
                "symbol": (details or {}).get("symbol"), "series": active_series_key(self.gs)}

    def _equity(self, m: dict[str, Any]) -> Decimal:
        if m.get("account_id"):
            s = self.gs.engine.summary(m["account_id"])
            return Decimal(s.get("equity") or s["balance"])
        return Decimal(str(self.gs.settings.paper_starting_balance))

    def _make_signal(self, m: dict[str, Any], row: Any, snap: dict[str, Any], decision: dict[str, Any], side: str,
                     quote: Any, now: datetime) -> dict[str, Any]:
        config = m["config"]
        bid, ask = Decimal(str(quote.bid)), Decimal(str(quote.ask))
        spread = ask - bid
        spread_ctx = self.feed.spread_context(spread)
        slippage = self.gs.paper_costs.slippage
        plan = plan_trade(config, side, snap, bid, ask, slippage, self._equity(m), self.gs.spec)
        plan["step"] = config["evaluation"]["step_timeframe"]
        ev = decision["evaluations"][side]
        cutoff = snap["cutoff"]
        latency = (now - parse_iso_aware(cutoff)).total_seconds()
        status, status_reason = "observed", None
        spread_conds = [c for c in config["conditions"] if c["enabled"] and c["type"] == "spread"]
        for c in spread_conds:
            if c["max_spread"] is not None and float(spread) > c["max_spread"]:
                status, status_reason = "blocked", (f"live spread {spread} exceeds the strategy's max_spread "
                                                    f"{c['max_spread']} (conditions used the candle spread "
                                                    f"{snap['spread']}, as the backtest does)")
        if status == "observed" and latency > config["entry"]["max_entry_delay_minutes"] * 60:
            status, status_reason = "blocked", f"processed {latency:.0f} s after the cut-off: beyond the maximum entry delay"
        if status == "observed" and m["mode"] == "paper" and plan["lots"] is None:
            status, status_reason = "blocked", "the planned size is below the minimum lot for this risk setting"
        sid = signal_id(m["strategy_id"], m["version"], cutoff, side)
        freshness = {"market_state": "LIVE", "quote_age_seconds": round((now - quote.quote_time).total_seconds(), 1),
                     "processed_after_cutoff_seconds": round(latency, 1),
                     "last_closed": {tf: (snap["tf"][tf].get("fresh"), snap["tf"][tf].get("bars")) for tf in snap["tf"]}}
        return clean({
            "id": sid, "definition": VERSION, "label": LABEL, "monitored_id": m["id"],
            "strategy": {"id": m["strategy_id"], "name": m["strategy_name"], "version": m["version"],
                         "fingerprint": m["fingerprint"]},
            "direction": side, "timeframe": config["evaluation"]["step_timeframe"],
            "condition_timeframes": sorted(builder.timeframes_used(config)),
            "cutoff": cutoff, "generated_at": iso(now), "status": status, "status_reason": status_reason,
            "mode": m["mode"],
            "supporting": [{"id": c["id"], "type": c["type"], "evidence": c["evidence"]} for c in ev["conditions"]
                           if c["passed"]],
            "conflicting": conflicts(config, snap, side, spread_ctx),
            "invalidation": invalidation(plan, side),
            "price": {"bid": str(bid), "ask": str(ask), "mid": str((bid + ask) / 2), "spread": str(spread),
                      "spread_source": "live quote", "quote_time": iso(quote.quote_time),
                      "candle_close": snap["price"], "candle_spread": snap["spread"]},
            "spread_context": spread_ctx, "source": self._source(), "freshness": freshness,
            "context": context_from_snapshot(snap), "plan": plan, "description": builder.describe(config),
            "snapshot": snap})

    # -------------------------------------------------------------- SIMULATED validation trades
    def _open_trade(self, m: dict[str, Any], sig: dict[str, Any], snap: dict[str, Any], now: datetime) -> None:
        plan = sig["plan"]
        account_id = m["account_id"]
        risk = m["config"]["risk"]
        with self.gs.db.read() as con:
            open_positions = [t for t in live_repo.trades(con, status="open", monitored_id=m["id"])]
        exposure = sum((Decimal(t["lots"]) for t in open_positions), Decimal(0))
        reason = None
        if len(open_positions) >= int(risk["max_open_positions"]) or \
                exposure + Decimal(plan["lots"]) > Decimal(str(risk["max_exposure_lots"])):
            reason = (f"exposure limit: {len(open_positions)} open position(s), {exposure} lots "
                      f"(max {risk['max_open_positions']} positions / {risk['max_exposure_lots']} lots)")
        position = None
        if reason is None:
            try:
                position = self.gs.engine.open_position(plan["side"], plan["lots"], plan["stop"], plan["target"],
                                                        account_id=account_id)
            except OrderRejected as exc:
                reason = f"paper simulator rejected the entry: {exc}"
        if position is None:
            with self.gs.db.transaction() as con:
                live_repo.set_signal_status(con, sig["id"], "blocked", reason)
            return
        quote_age = (position.open_time - position.open_quote_time).total_seconds()
        entry = {"fill_price": str(position.open_price), "bid_ask_at_signal": [sig["price"]["bid"], sig["price"]["ask"]],
                 "spread": str(position.open_spread), "spread_source": position.open_spread_source,
                 "slippage_per_oz": str(position.open_slippage), "commission": str(position.open_commission),
                 "quote_time": iso(position.open_quote_time), "quote_age_seconds": round(quote_age, 1),
                 "price_state": position.open_price_state, "source": position.open_source,
                 "latency_from_cutoff_seconds": round((position.open_time - parse_iso_aware(sig["cutoff"])).total_seconds(), 1),
                 "lots": str(position.lots), "ounces": str(position.ounces), "margin": str(position.margin),
                 "planned_entry_estimate": plan["entry_estimate"]}
        context = {**sig["context"], "spread_context": sig["spread_context"]}
        with self.gs.db.transaction() as con:
            live_repo.insert_trade(con, signal_id=sig["id"], monitored_id=m["id"], strategy_id=m["strategy_id"],
                                   version=m["version"], account_id=account_id, position_id=position.id,
                                   opened_at=position.open_time, plan=plan, entry=entry, context=context)
            live_repo.set_signal_status(con, sig["id"], "opened", f"SIMULATED paper position {position.id} "
                                                                  f"in validation account {account_id}")

    def _recheck_open_trades(self, m: dict[str, Any], config: dict[str, Any], snap: dict[str, Any],
                             decision: dict[str, Any], now: datetime) -> None:
        with self.gs.db.read() as con:
            open_trades = live_repo.trades(con, status="open", monitored_id=m["id"])
        flip = config["exit"]["exit_on_structure_flip"]
        for t in open_trades:
            side = "long" if t["side"] == "buy" else "short"
            ev = decision["evaluations"].get(side) or conditions.evaluate(config, snap, side)
            flip_state = (snap["tf"].get(flip["timeframe"]) or {}).get("state") if flip["enabled"] else None
            check = {"cutoff": snap["cutoff"], "conditions_still_pass": ev["passed"],
                     "failed": [c["id"] for c in ev["conditions"] if not c["passed"]],
                     "setup_now": decision["setup"], "flip_state": flip_state}
            with self.gs.db.transaction() as con:
                live_repo.append_check(con, t["id"], check)
            if flip["enabled"] and flip_state == against_state(side) and t["position_status"] == "open":
                self._close(t, CloseReason.STRATEGY_EXIT, now,
                            f"{flip['timeframe']} structure turned {flip_state} at {snap['cutoff']}")

    def _close(self, t: dict[str, Any], reason: CloseReason, now: datetime, why: str) -> bool:
        try:
            self.gs.engine.close_position(t["position_id"], reason, account_id=t["account_id"])
        except OrderRejected as exc:
            with self.gs.db.transaction() as con:
                live_repo.append_check(con, t["id"], {"at": iso(now), "exit_delayed": f"{reason.value}: {exc}"})
            return False
        with self.gs.db.transaction() as con:
            live_repo.append_check(con, t["id"], {"at": iso(now), "exit": reason.value, "why": why})
        return True

    def on_quote_excursions(self, now: datetime) -> None:
        """MFE / MAE of open SIMULATED trades from the current quote (BUY: bid vs the fill; SELL: ask vs the fill),
        the same definition as the backtest engine."""
        quote = self.gs.market.market_quote()
        if quote is None or quote.bid is None or quote.ask is None or quote.state is not MarketState.LIVE:
            return
        with self.gs.db.read() as con:
            open_trades = live_repo.trades(con, status="open")
        for t in open_trades:
            if t["position_status"] != "open":
                continue
            entry = Decimal(t["open_price"])
            if t["side"] == "buy":
                fav, adv = Decimal(quote.bid) - entry, entry - Decimal(quote.bid)
            else:
                fav, adv = entry - Decimal(quote.ask), Decimal(quote.ask) - entry
            mfe, mae = Decimal(t["mfe"]), Decimal(t["mae"])
            mfe_at, mae_at = t["mfe_at"], t["mae_at"]
            changed = False
            if fav > mfe:
                mfe, mfe_at, changed = fav, iso(quote.quote_time), True
            if adv > mae:
                mae, mae_at, changed = adv, iso(quote.quote_time), True
            if changed:
                with self.gs.db.transaction() as con:
                    live_repo.update_excursions(con, t["id"], str(mfe), str(mae), mfe_at, mae_at)

    def _closing_excursion(self, t: dict[str, Any]) -> None:
        """The quote that CLOSED the position (paper SL/TP run before this listener) also counts: its bid (BUY) or
        ask (SELL) is recovered from the close fill, which is that price minus / plus the slippage."""
        if not t["close_price"]:
            return
        entry, close, slip = Decimal(t["open_price"]), Decimal(t["close_price"]), Decimal(t["open_slippage"])
        mark = close + slip if t["side"] == "buy" else close - slip
        fav, adv = (mark - entry, entry - mark) if t["side"] == "buy" else (entry - mark, mark - entry)
        mfe, mae = Decimal(t["mfe"]), Decimal(t["mae"])
        if fav > mfe or adv > mae:
            with self.gs.db.transaction() as con:
                live_repo.update_excursions(con, t["id"], str(max(mfe, fav)), str(max(mae, adv)),
                                            t["close_time"] if fav > mfe else t["mfe_at"],
                                            t["close_time"] if adv > mae else t["mae_at"])

    def _manage_open_trades(self, now: datetime) -> None:
        with self.gs.db.read() as con:
            open_trades = live_repo.trades(con, status="open")
        for t in open_trades:
            if t["position_status"] == "open":
                hold = t["plan"].get("max_holding_minutes") or 0
                if hold and now >= parse_iso_aware(t["opened_at"]) + timedelta(minutes=int(hold)):
                    self._close(t, CloseReason.TIME_EXIT, now, f"maximum holding time of {hold} minutes elapsed")
        with self.gs.db.read() as con:
            closing = live_repo.open_trades_with_closed_positions(con)
        for t in closing:
            self._finalize(t, now)

    def _finalize(self, t: dict[str, Any], now: datetime) -> None:
        quote = self.gs.market.market_quote()
        close_spread = None
        if quote is not None and quote.bid is not None and quote.ask is not None and t["close_time"]:
            with self.gs.db.read() as con:
                pos = con.execute("SELECT close_quote_time FROM paper_positions WHERE id = ?",
                                  (t["position_id"],)).fetchone()
            if pos and pos["close_quote_time"] == iso(quote.quote_time):
                close_spread = str(quote.ask - quote.bid)
        self._closing_excursion(t)
        with self.gs.db.read() as con:
            t = live_repo.trade(con, t["id"])
        holding = (parse_iso_aware(t["close_time"]) - parse_iso_aware(t["open_time"])).total_seconds() / 60
        exit_info = {"reason": t["close_reason"], "fill_price": t["close_price"], "time": t["close_time"],
                     "commission": t["close_commission"], "price_state": t["close_price_state"],
                     "source": t["close_source"], "spread_at_close": close_spread,
                     "holding_minutes": round(holding, 2), "gross_pnl": t["realized_gross"], "net_pnl": t["realized_net"]}
        exit_info["candle_excursions"] = self._candle_excursions(t)
        context = self._context_at(t, parse_iso_aware(t["close_time"]))
        with self.gs.db.transaction() as con:
            live_repo.close_trade(con, t["id"], exit_info, context, t["close_time"])
        self.evaluate(t["id"], now)

    def evaluate(self, trade_id: int, now: datetime) -> dict[str, Any] | None:
        with self.gs.db.read() as con:
            t = live_repo.trade(con, trade_id)
            if t is None or t["status"] != "closed":
                return None
            sig = live_repo.signal(con, t["signal_id"])
            baseline = (live_repo.monitored(con, t["monitored_id"]) or {}).get("eligibility") or {}
        ev = trade_eval.evaluate_trade(t, sig, baseline=baseline, feed=self.feed.stats())
        with self.gs.db.transaction() as con:
            live_repo.insert_trade_evaluation(con, trade_id, ev, now)
        return ev

    def _context_at(self, t: dict[str, Any], at: datetime) -> dict[str, Any]:
        row = self._series_row()
        config = (live_repo_monitored_config(self.gs, t["monitored_id"])) or {}
        if row is None or not config:
            return {}
        step = Timeframe.parse(config["evaluation"]["step_timeframe"])
        cutoff = floor_time(at, step)
        try:
            snap = self.snapshot_at(row, step, cutoff, builder.detail_timeframes(config))
        except Exception:  # noqa: BLE001
            snap = None
        if snap is None:
            return {"note": f"no snapshot available at {iso(cutoff)}"}
        quote = self.gs.market.market_quote()
        spread = None if quote is None or quote.bid is None else quote.ask - quote.bid
        return {**context_from_snapshot(snap), "spread_context": self.feed.spread_context(spread)}

    def _candle_excursions(self, t: dict[str, Any]) -> dict[str, Any] | None:
        """Cross-check from stored M1 candles overlapping the holding period (an upper bound: the first candle
        includes prices from before the fill)."""
        row = self._series_row()
        if row is None or not t["close_time"]:
            return None
        start = pd.Timestamp(t["open_time"]).floor("min").to_pydatetime()
        end = parse_iso_aware(t["close_time"])
        with self.gs.db.read() as con:
            m1 = candle_repo.load_candles(con, row["id"], row["price_decimals"], Timeframe.M1, start=start, end=end,
                                          limit=20000)
        if m1.empty:
            return {"bars": 0}
        entry = float(t["open_price"])
        spread = m1["spread"].fillna(float(t["open_spread"])).to_numpy()
        if t["side"] == "buy":
            mfe, mae = float(m1["high"].max()) - entry, entry - float(m1["low"].min())
        else:
            mfe, mae = entry - float((m1["low"] + spread).min()), float((m1["high"] + spread).max()) - entry
        return {"bars": int(len(m1)), "mfe": round(max(mfe, 0.0), 3), "mae": round(max(mae, 0.0), 3),
                "note": "from M1 bid candles (ask = bid + candle spread) overlapping the holding period: an upper bound"}

    # -------------------------------------------------------------- signal outcomes vs subsequent real prices
    def _signal_outcomes(self, now: datetime) -> None:
        with self.gs.db.read() as con:
            pending = live_repo.signals_pending_outcome(con, iso(now))
        row = self._series_row()
        for sig in pending:
            horizon = min(int(sig["plan"].get("max_holding_minutes") or 60), 24 * 60)
            due = parse_iso_aware(sig["cutoff"]) + timedelta(minutes=horizon)
            if now < due + timedelta(minutes=2) or row is None:
                continue
            outcome = signal_outcome(self.gs, row, sig, horizon, now)
            if outcome is None:
                continue
            with self.gs.db.transaction() as con:
                live_repo.set_signal_outcome(con, sig["id"], outcome, now)


def live_repo_monitored_config(gs: AppServices, mid: int) -> dict[str, Any] | None:
    with gs.db.read() as con:
        m = live_repo.monitored(con, mid)
    return None if m is None else m["config"]


def signal_outcome(gs: AppServices, row: Any, sig: dict[str, Any], horizon: int, now: datetime) -> dict[str, Any] | None:
    """What the market did after the signal: stored M1 candles AFTER the cut-off only (never before)."""
    cutoff = parse_iso_aware(sig["cutoff"])
    end = cutoff + timedelta(minutes=horizon)
    with gs.db.read() as con:
        m1 = candle_repo.load_candles(con, row["id"], row["price_decimals"], Timeframe.M1, start=cutoff, end=end,
                                      limit=20000)
    m1 = m1[m1["closed"]]
    expected = horizon
    if len(m1) < max(1, int(expected * 0.5)):
        if now - end > timedelta(days=1):
            return {"status": "incomplete", "bars": int(len(m1)),
                    "detail": "too few stored M1 candles after the signal (market closed or feed gap)"}
        return None
    long = sig["direction"] == "long"
    ref = float(sig["plan"]["entry_estimate"])
    stop = None if sig["plan"].get("stop") is None else float(sig["plan"]["stop"])
    target = None if sig["plan"].get("target") is None else float(sig["plan"]["target"])
    risk = abs(ref - stop) if stop is not None else None
    spread = m1["spread"].fillna(float(sig["price"]["spread"])).to_numpy()
    hi, lo = m1["high"].to_numpy(), m1["low"].to_numpy()
    if long:
        fav, adv = hi.max() - ref, ref - lo.min()
    else:
        fav, adv = ref - (lo + spread).min(), (hi + spread).max() - ref
    first, ambiguous, at = None, False, None
    for i, ts in enumerate(m1.index):
        if long:
            s_hit = stop is not None and lo[i] <= stop
            t_hit = target is not None and hi[i] >= target
        else:
            s_hit = stop is not None and hi[i] + spread[i] >= stop
            t_hit = target is not None and lo[i] + spread[i] <= target
        if s_hit or t_hit:
            first = "stop" if s_hit and not t_hit else "target" if t_hit and not s_hit else "both"
            ambiguous, at = s_hit and t_hit, iso(ts.to_pydatetime())
            break
    moves = {}
    for k in OUTCOME_POINTS:
        if k > horizon:
            continue
        bar = m1[m1.index <= pd.Timestamp(cutoff + timedelta(minutes=k - 1))]
        if len(bar):
            close = float(bar["close"].iloc[-1])
            moves[f"+{k}m"] = round((close - ref) if long else (ref - close), 3)
    verdict = ("target first" if first == "target" else "stop first (false signal)" if first == "stop" else
               "stop and target in the same minute (ambiguous)" if first == "both" else
               f"neither stop nor target within {horizon} minutes")
    out = {"status": "complete", "definition": "outcome@1", "horizon_minutes": horizon, "bars": int(len(m1)),
           "reference_price": ref, "mfe": round(max(fav, 0.0), 3), "mae": round(max(adv, 0.0), 3),
           "mfe_r": round(max(fav, 0.0) / risk, 3) if risk else None,
           "mae_r": round(max(adv, 0.0) / risk, 3) if risk else None,
           "first_touch": first, "first_touch_at": at, "ambiguous": ambiguous, "moves": moves, "verdict": verdict,
           "false_signal": first == "stop" or (first is None and fav < (0.5 * risk if risk else 0)),
           "note": "measured on stored M1 bid candles after the cut-off (ask = bid + candle spread); minute "
                   "resolution, so the order inside a minute is unknown"}
    out["replica"] = backtest_replica(gs, row, sig, horizon)
    return out


def backtest_replica(gs: AppServices, row: Any, sig: dict[str, Any], horizon: int) -> dict[str, Any] | None:
    """The trade the Phase 3 engine models for the SAME decision (snapshot at the cut-off), on the stored
    candles that followed. Structure-flip exits are not replicated (they need later snapshots)."""
    snap = sig.get("snapshot")
    if not snap:
        return None
    with gs.db.read() as con:
        record = strategy_repo.get(con, sig["strategy"]["id"], sig["strategy"]["version"])
    if record is None:
        return None
    config = dict(record["config"])
    config["direction"] = sig["direction"]
    config["exit"] = {**config["exit"], "exit_on_structure_flip": {**config["exit"]["exit_on_structure_flip"],
                                                                   "enabled": False}}
    cutoff = parse_iso_aware(sig["cutoff"])
    with gs.db.read() as con:
        candles = candle_repo.load_candles(con, row["id"], row["price_decimals"], Timeframe.M1,
                                           start=cutoff - timedelta(minutes=1),
                                           end=cutoff + timedelta(minutes=horizon + 60), limit=20000)
    candles = candles[candles["closed"]]
    if candles.empty:
        return None
    costs = gs.paper_costs
    assumptions = bt.Assumptions.from_mapping({
        "configured_spread": str(costs.configured_spread or "0.30"),
        "commission_per_lot_per_side": str(costs.commission_per_lot_per_side), "slippage": str(costs.slippage),
        "starting_balance": gs.settings.paper_starting_balance, "leverage": gs.settings.paper_leverage})
    step = Timeframe.parse(config["evaluation"]["step_timeframe"])
    result = bt.run(config, [snap], candles, Timeframe.M1, start=cutoff, end=cutoff + timedelta(seconds=step.seconds),
                    assumptions=assumptions, spec=gs.spec)
    if not result["trades"]:
        return {"trade": None, "diagnostics": result["diagnostics"]["skipped"]}
    tr = result["trades"][0]
    return {"trade": {k: tr[k] for k in ("side", "entry_time", "entry_price", "stop", "target", "exit_time",
                                         "exit_price", "exit_reason", "net_pnl", "r_multiple", "mfe", "mae", "lots",
                                         "ambiguous_bar", "holding_minutes")},
            "definition": bt.VERSION, "note": "Phase 3 engine on the same snapshot; structure-flip exit not replicated"}


def validation_account_config(gs: AppServices, name: str) -> AccountConfig:
    return AccountConfig.from_mapping({"name": name[:80], "currency": "USD",
                                       "starting_balance": gs.settings.paper_starting_balance,
                                       "leverage": gs.settings.paper_leverage,
                                       "max_quote_age_seconds": gs.settings.live_max_quote_age_seconds,
                                       "allow_delayed_fills": False})


