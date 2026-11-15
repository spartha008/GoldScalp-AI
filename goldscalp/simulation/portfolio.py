"""The paper-trading engine: opens and closes SIMULATED positions on a virtual account.

There is no broker connection anywhere in this module or its imports. Prices
come from a callable that returns the latest MarketQuote (or None).

Accounts: the user's ACTIVE manual account is the default for every method. Phase 4 adds VALIDATION
accounts (one per monitored strategy version), addressed with ``account_id``: same formulas, same costs,
same ledger; they never become the active account.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from collections.abc import Callable

from goldscalp.core.clock import Clock, iso
from goldscalp.core.kinds import MarketState
from goldscalp.simulation import costs as cost_fns
from goldscalp.simulation.account import AccountConfig, margin_level
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.decimal_utils import dstr, money, to_decimal
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.positions import (
    CloseReason,
    Position,
    Side,
    close_fill_price,
    gross_pnl,
    open_fill_price,
    protection_trigger,
    required_margin,
    unrealized_pnl,
    validate_protection,
)
from goldscalp.simulation.pricing import ExecutionQuote, MarketQuote, PricingError, execution_quote
from goldscalp.storage import paper_repo as repo
from goldscalp.storage.db import Database

SNAPSHOT_INTERVAL = timedelta(seconds=60)
SIMULATED_LABEL = "SIMULATED - paper trading only, no real orders"


class OrderRejected(ValueError):
    """A simulated order that cannot be accepted, with a human-readable reason."""


class AccountState:
    """An account loaded from the database."""

    def __init__(self, row: Any) -> None:
        self.id: int = row["id"]
        self.config = AccountConfig(name=row["name"], currency=row["currency"],
                                    starting_balance=Decimal(row["starting_balance"]), leverage=row["leverage"],
                                    max_quote_age_seconds=row["max_quote_age_seconds"],
                                    allow_delayed_fills=bool(row["allow_delayed_fills"]))
        self.spec = InstrumentSpec.from_mapping(json.loads(row["spec_json"]))
        self.costs = CostModel.from_mapping(json.loads(row["cost_model_json"]))
        self.created_at: str = row["created_at"]
        self.purpose: str = row["purpose"] if "purpose" in row.keys() else "manual"

    @property
    def allowed_states(self) -> tuple[MarketState, ...]:
        if self.config.allow_delayed_fills:
            return (MarketState.LIVE, MarketState.DELAYED)
        return (MarketState.LIVE,)


class PaperTradingEngine:
    def __init__(self, db: Database, clock: Clock, quote_source: Callable[[], MarketQuote | None],
                 default_spec: InstrumentSpec, default_costs: CostModel, default_account: AccountConfig) -> None:
        self.db = db
        self.clock = clock
        self.quote_source = quote_source
        self.default_spec = default_spec
        self.default_costs = default_costs
        self.default_account = default_account
        self._lock = threading.RLock()

    # --------------------------------------------------------------- accounts

    def ensure_account(self) -> AccountState:
        with self._lock:
            with self.db.read() as con:
                row = repo.active_account_row(con)
            if row is not None:
                return AccountState(row)
            return self.create_account(self.default_account, self.default_spec, self.default_costs)

    def account(self, account_id: int | None = None) -> AccountState:
        """The active manual account (``None``) or the account with that id."""
        if account_id is None:
            return self.ensure_account()
        with self.db.read() as con:
            row = repo.account_row(con, account_id)
        if row is None:
            raise OrderRejected(f"paper account {account_id} does not exist")
        return AccountState(row)

    def create_account(self, config: AccountConfig, spec: InstrumentSpec, costs: CostModel, *,
                       purpose: str = "manual") -> AccountState:
        """Create a new account. ``manual``: it becomes the active account (the previous one is kept, inactive,
        with its history). ``validation``: an extra account addressed by id (live strategy validation)."""
        with self._lock:
            now = self.clock.now()
            with self.db.transaction() as con:
                spec_id = repo.upsert_spec(con, spec, now)
                account_id = repo.insert_account(con, config, spec_id, costs, now, purpose=purpose)
                repo.insert_snapshot(con, account_id, now, {
                    "balance": dstr(config.starting_balance), "equity": dstr(config.starting_balance),
                    "used_margin": "0", "free_margin": dstr(config.starting_balance), "open_positions": 0,
                    "price_state": "n/a"})
                row = repo.account_row(con, account_id)
            return AccountState(row)

    # ------------------------------------------------------------------ quotes

    def _execution_quote(self, account: AccountState) -> ExecutionQuote:
        try:
            return execution_quote(self.quote_source(), account.costs, now=self.clock.now(),
                                   max_age_seconds=account.config.max_quote_age_seconds,
                                   allowed_states=account.allowed_states)
        except PricingError as exc:
            raise OrderRejected(str(exc)) from exc

    def _mark_quote(self, account: AccountState) -> ExecutionQuote | None:
        """A quote for MARKING open positions. No freshness gate, but the
        returned quote keeps its state and time so the summary can label it."""
        quote = self.quote_source()
        if quote is None:
            return None
        try:
            return execution_quote(quote, account.costs, now=quote.quote_time, max_age_seconds=10**9,
                                   allowed_states=tuple(MarketState))
        except PricingError:
            return None

    # ------------------------------------------------------------------ orders

    def estimate(self, side: str, lots: Any, account_id: int | None = None) -> dict[str, Any]:
        """Everything a ticket shows before submitting: price, costs, margin, acceptance."""
        account = self.account(account_id)
        side_e = _side(side)
        lots_d = _lots(lots, account.spec)
        ounces = account.spec.ounces(lots_d)
        out: dict[str, Any] = {"side": side_e.value, "lots": dstr(lots_d), "ounces": dstr(ounces),
                               "contract_size": dstr(account.spec.contract_size), "label": SIMULATED_LABEL}
        try:
            quote = self._execution_quote(account)
        except OrderRejected as exc:
            out.update(accepted=False, reason=str(exc))
            return out
        price = open_fill_price(side_e, quote, account.costs.slippage)
        margin = required_margin(ounces, price, account.config.leverage)
        commission = cost_fns.commission(lots_d, account.costs)
        trip = cost_fns.round_trip_estimate(lots=lots_d, ounces=ounces, spread=quote.spread, model=account.costs)
        summary = self.summary(account_id)
        free_margin = Decimal(summary["free_margin"]) if summary["free_margin"] is not None else None
        accepted, reason = True, None
        if free_margin is None or free_margin - commission < margin:
            accepted = False
            reason = (f"insufficient free margin: needs {money(margin)} USD margin plus {money(commission)} "
                      f"USD commission, free margin is {money(free_margin) if free_margin is not None else 'unknown'}")
        out.update(accepted=accepted, reason=reason, source_id=quote.source_id, fill_price=dstr(price),
                   bid=dstr(quote.bid), ask=dstr(quote.ask),
                   spread=dstr(quote.spread), spread_source=quote.spread_source, price_state=quote.state.value,
                   quote_time=iso(quote.quote_time), notional=dstr(ounces * price), margin_required=dstr(margin),
                   leverage=account.config.leverage, open_commission=dstr(commission),
                   slippage_per_oz=dstr(account.costs.slippage),
                   round_trip={k: dstr(v) for k, v in trip.items()},
                   free_margin=dstr(free_margin) if free_margin is not None else None)
        return out

    def open_position(self, side: str, lots: Any, stop_loss: Any = None, take_profit: Any = None,
                      signal_id: str | None = None, account_id: int | None = None) -> Position:
        with self._lock:
            account = self.account(account_id)
            side_e = _side(side)
            lots_d = _lots(lots, account.spec)
            sl, tp = _opt_price(stop_loss, "stop_loss"), _opt_price(take_profit, "take_profit")
            quote = self._execution_quote(account)
            try:
                validate_protection(side_e, quote, sl, tp)
            except ValueError as exc:
                raise OrderRejected(str(exc)) from exc
            now = self.clock.now()
            ounces = account.spec.ounces(lots_d)
            price = open_fill_price(side_e, quote, account.costs.slippage)
            margin = required_margin(ounces, price, account.config.leverage)
            commission = cost_fns.commission(lots_d, account.costs)
            summary = self._summary(account, mark=quote)
            free_margin = Decimal(summary["free_margin"])
            if free_margin - commission < margin:
                raise OrderRejected(f"insufficient free margin: needs {money(margin)} USD margin plus "
                                    f"{money(commission)} USD commission; free margin is {money(free_margin)} USD")
            position = Position(id=None, account_id=account.id, side=side_e, lots=lots_d, ounces=ounces,
                                open_price=price, open_time=now, open_quote_time=quote.quote_time,
                                open_price_state=quote.state.value, open_spread=quote.spread,
                                open_spread_source=quote.spread_source, open_slippage=account.costs.slippage,
                                open_commission=commission, margin=margin, stop_loss=sl, take_profit=tp,
                                open_source=quote.source_id, signal_id=signal_id)
            with self.db.transaction() as con:
                position.id = repo.insert_position(con, position)
                if commission:
                    repo.append_ledger(con, account.id, now, "commission", -commission, position.id,
                                       f"open commission, {side_e.value} {dstr(lots_d)} lots (simulated)")
            self._snapshot(account, force=True)
            return position

    def close_position(self, position_id: int, reason: CloseReason = CloseReason.MANUAL,
                       quote: ExecutionQuote | None = None, account_id: int | None = None) -> Position:
        with self._lock:
            account = self.account(account_id)
            with self.db.read() as con:
                position = repo.position(con, position_id)
            if position is None or position.account_id != account.id:
                where = "the active account" if account_id is None else f"paper account {account_id}"
                raise OrderRejected(f"position {position_id} does not exist on {where}")
            if position.status != "open":
                raise OrderRejected(f"position {position_id} is already closed")
            quote = quote or self._execution_quote(account)
            now = self.clock.now()
            price = close_fill_price(position.side, quote, account.costs.slippage)
            gross = gross_pnl(position.side, position.open_price, price, position.ounces)
            commission = cost_fns.commission(position.lots, account.costs)
            position.status = "closed"
            position.close_price, position.close_time, position.close_quote_time = price, now, quote.quote_time
            position.close_price_state, position.close_commission = quote.state.value, commission
            position.close_reason, position.realized_gross = reason.value, gross
            position.close_source = quote.source_id
            position.realized_net = gross - position.open_commission - commission
            with self.db.transaction() as con:
                repo.close_position_row(con, position)
                repo.append_ledger(con, account.id, now, "realized_pnl", gross, position.id,
                                   f"realized P/L ({reason.value}), {position.side.value} {dstr(position.lots)} lots "
                                   "(simulated)")
                if commission:
                    repo.append_ledger(con, account.id, now, "commission", -commission, position.id,
                                       "close commission (simulated)")
            self._snapshot(account, force=True)
            return position

    def modify_protection(self, position_id: int, stop_loss: Any, take_profit: Any,
                          account_id: int | None = None) -> Position:
        with self._lock:
            account = self.account(account_id)
            with self.db.read() as con:
                position = repo.position(con, position_id)
            if position is None or position.account_id != account.id or position.status != "open":
                raise OrderRejected(f"position {position_id} is not an open position on the active account")
            sl, tp = _opt_price(stop_loss, "stop_loss"), _opt_price(take_profit, "take_profit")
            quote = self._execution_quote(account)
            try:
                validate_protection(position.side, quote, sl, tp)
            except ValueError as exc:
                raise OrderRejected(str(exc)) from exc
            with self.db.transaction() as con:
                repo.update_protection(con, position_id, sl, tp)
            position.stop_loss, position.take_profit = sl, tp
            return position

    def on_quote(self) -> list[Position]:
        """Check SL/TP against the latest quote and record a periodic equity snapshot, for the active manual
        account and for every validation account holding open positions.

        Only quotes that would be acceptable for a fill (per account) can trigger SL/TP: a
        stale or disconnected price never closes a simulated position.
        """
        closed: list[Position] = []
        with self._lock:
            accounts = [self.ensure_account()]
            with self.db.read() as con:
                accounts += [AccountState(r) for r in repo.validation_accounts_with_open_positions(con)]
            for account in accounts:
                try:
                    quote = self._execution_quote(account)
                except OrderRejected:
                    continue
                with self.db.read() as con:
                    open_positions = repo.positions(con, account.id, "open")
                explicit = None if account.purpose == "manual" else account.id
                for position in open_positions:
                    trigger = protection_trigger(position, quote)
                    if trigger is not None:
                        closed.append(self.close_position(position.id, trigger, quote, account_id=explicit))
                if open_positions:
                    self._snapshot(account, force=False)
        return closed

    # ----------------------------------------------------------------- reading

    def summary(self, account_id: int | None = None) -> dict[str, Any]:
        account = self.account(account_id)
        return self._summary(account, mark=self._mark_quote(account))

    def _summary(self, account: AccountState, mark: ExecutionQuote | None) -> dict[str, Any]:
        with self.db.read() as con:
            bal = repo.balance(con, account.id)
            open_positions = repo.positions(con, account.id, "open")
            closed = repo.positions(con, account.id, "closed", limit=100000)
        used = sum((p.margin for p in open_positions), Decimal(0))
        realized = sum((p.realized_net for p in closed if p.realized_net is not None), Decimal(0))
        open_commission = sum((p.open_commission for p in open_positions), Decimal(0))
        if open_positions and mark is None:
            unrealized = equity = free = level = None
        else:
            unrealized = sum((unrealized_pnl(p, mark) for p in open_positions), Decimal(0)) if mark else Decimal(0)
            equity = bal + unrealized
            free = equity - used
            level = margin_level(equity, used)
        now = self.clock.now()
        mark_info = None
        if mark is not None:
            mark_info = {"bid": dstr(mark.bid), "ask": dstr(mark.ask), "spread": dstr(mark.spread),
                         "spread_source": mark.spread_source, "price_state": mark.state.value,
                         "quote_time": iso(mark.quote_time),
                         "age_seconds": round((now - mark.quote_time).total_seconds(), 1),
                         "source_id": mark.source_id}
        return {
            "account_id": account.id, "purpose": account.purpose, "label": SIMULATED_LABEL,
            "config": account.config.to_mapping(),
            "created_at": account.created_at, "balance": dstr(bal),
            "equity": dstr(equity), "used_margin": dstr(used), "free_margin": dstr(free),
            "margin_level_pct": dstr(level), "unrealized_pnl": dstr(unrealized),
            "realized_pnl_net": dstr(realized), "open_commission_paid": dstr(open_commission),
            "open_positions": len(open_positions), "closed_positions": len(closed),
            "net_change": dstr(bal + (unrealized or Decimal(0)) - account.config.starting_balance)
            if equity is not None else None,
            "mark": mark_info,
            "mark_note": None if mark is None or mark.state is MarketState.LIVE else
            f"open P/L is marked at a {mark.state.value} price",
        }

    def positions_view(self, status: str | None, account_id: int | None = None) -> list[dict[str, Any]]:
        account = self.account(account_id)
        mark = self._mark_quote(account)
        with self.db.read() as con:
            rows = repo.positions(con, account.id, status)
        out = []
        for p in rows:
            item = {k: (dstr(v) if isinstance(v, Decimal) else iso(v) if isinstance(v, datetime) else v)
                    for k, v in asdict(p).items()}
            item["side"] = p.side.value
            if p.status == "open" and mark is not None:
                item["unrealized_pnl"] = dstr(unrealized_pnl(p, mark))
                item["mark_price"] = dstr(mark.bid if p.side is Side.BUY else mark.ask)
                item["mark_state"] = mark.state.value
            out.append(item)
        return out

    def ledger_view(self, limit: int = 200, account_id: int | None = None) -> list[dict[str, Any]]:
        account = self.account(account_id)
        with self.db.read() as con:
            return repo.ledger(con, account.id, limit)

    def equity_history(self, limit: int = 2000, account_id: int | None = None) -> list[dict[str, Any]]:
        account = self.account(account_id)
        with self.db.read() as con:
            return repo.snapshots(con, account.id, limit)

    def _snapshot(self, account: AccountState, *, force: bool) -> None:
        now = self.clock.now()
        with self.db.read() as con:
            last = repo.last_snapshot_time(con, account.id)
        if not force and last is not None and now - last < SNAPSHOT_INTERVAL:
            return
        mark = self._mark_quote(account)
        s = self._summary(account, mark=mark)
        if s["equity"] is None:
            return
        with self.db.transaction() as con:
            repo.insert_snapshot(con, account.id, now, {
                "balance": s["balance"], "equity": s["equity"], "used_margin": s["used_margin"],
                "free_margin": s["free_margin"], "open_positions": s["open_positions"],
                "price_state": mark.state.value if mark else "n/a"})


def _side(value: Any) -> Side:
    try:
        return Side(str(value).lower())
    except ValueError as exc:
        raise OrderRejected("side must be 'buy' or 'sell'") from exc


def _lots(value: Any, spec: InstrumentSpec) -> Decimal:
    try:
        lots = to_decimal(value, "lots")
        return spec.validate_lots(lots)
    except ValueError as exc:
        raise OrderRejected(str(exc)) from exc


def _opt_price(value: Any, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = to_decimal(value, name)
    except ValueError as exc:
        raise OrderRejected(str(exc)) from exc
    if out <= 0:
        raise OrderRejected(f"{name} must be a positive price")
    return out
