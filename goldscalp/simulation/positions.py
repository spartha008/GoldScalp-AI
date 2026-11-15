"""Simulated positions and their profit/loss formulas.

Formulas (tested in tests/test_simulation_formulas.py), with Q = ounces = lots x contract_size:
    open fill   BUY  = ask + slippage            SELL = bid - slippage
    close fill  BUY  = bid - slippage            SELL = ask + slippage
    unrealized  BUY  = (bid - open_price) x Q    SELL = (open_price - ask) x Q      (mark at the closing side, no slippage)
    gross P/L   BUY  = (close - open) x Q        SELL = (open - close) x Q
    net P/L          = gross - open_commission - close_commission
    margin           = Q x open_price / leverage                                   (fixed at open)
SL/TP triggers (evaluated on each new quote):
    BUY : stop loss when bid <= SL, take profit when bid >= TP
    SELL: stop loss when ask >= SL, take profit when ask <= TP
    A triggered position closes at the CURRENT bid/ask (minus/plus slippage), not at the SL/TP level:
    price can move past a level between quotes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from goldscalp.simulation.pricing import ExecutionQuote


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class CloseReason(StrEnum):
    MANUAL = "manual"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_EXIT = "time_exit"            # Phase 4 live validation: the strategy's maximum holding time elapsed
    STRATEGY_EXIT = "strategy_exit"    # Phase 4 live validation: the strategy's structure-flip exit


@dataclass
class Position:
    id: int | None
    account_id: int
    side: Side
    lots: Decimal
    ounces: Decimal
    open_price: Decimal
    open_time: datetime
    open_quote_time: datetime
    open_price_state: str
    open_spread: Decimal
    open_spread_source: str
    open_slippage: Decimal
    open_commission: Decimal
    margin: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    status: str = "open"
    close_price: Decimal | None = None
    close_time: datetime | None = None
    close_quote_time: datetime | None = None
    close_price_state: str | None = None
    close_commission: Decimal | None = None
    close_reason: str | None = None
    realized_gross: Decimal | None = None
    realized_net: Decimal | None = None
    open_source: str | None = None      # market-data provider that priced the open
    close_source: str | None = None     # market-data provider that priced the close
    signal_id: str | None = None        # the stored signal that prefilled the ticket (Phase 2), if any


def open_fill_price(side: Side, quote: ExecutionQuote, slippage: Decimal) -> Decimal:
    return quote.ask + slippage if side is Side.BUY else quote.bid - slippage


def close_fill_price(side: Side, quote: ExecutionQuote, slippage: Decimal) -> Decimal:
    return quote.bid - slippage if side is Side.BUY else quote.ask + slippage


def gross_pnl(side: Side, open_price: Decimal, close_price: Decimal, ounces: Decimal) -> Decimal:
    diff = close_price - open_price if side is Side.BUY else open_price - close_price
    return diff * ounces


def unrealized_pnl(position: Position, quote: ExecutionQuote) -> Decimal:
    mark = quote.bid if position.side is Side.BUY else quote.ask
    return gross_pnl(position.side, position.open_price, mark, position.ounces)


def required_margin(ounces: Decimal, price: Decimal, leverage: int) -> Decimal:
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise ValueError("leverage must be a whole number >= 1")
    return ounces * price / Decimal(leverage)


def validate_protection(side: Side, quote: ExecutionQuote, stop_loss: Decimal | None,
                        take_profit: Decimal | None) -> None:
    """SL/TP must sit on the losing/winning side of the current closing price."""
    mark = quote.bid if side is Side.BUY else quote.ask
    for name, level in (("stop loss", stop_loss), ("take profit", take_profit)):
        if level is not None and (not level.is_finite() or level <= 0):
            raise ValueError(f"{name} must be a positive price")
    if side is Side.BUY:
        if stop_loss is not None and stop_loss >= mark:
            raise ValueError(f"a BUY stop loss must be below the current bid {mark}")
        if take_profit is not None and take_profit <= mark:
            raise ValueError(f"a BUY take profit must be above the current bid {mark}")
    else:
        if stop_loss is not None and stop_loss <= mark:
            raise ValueError(f"a SELL stop loss must be above the current ask {mark}")
        if take_profit is not None and take_profit >= mark:
            raise ValueError(f"a SELL take profit must be below the current ask {mark}")


def protection_trigger(position: Position, quote: ExecutionQuote) -> CloseReason | None:
    if position.side is Side.BUY:
        if position.stop_loss is not None and quote.bid <= position.stop_loss:
            return CloseReason.STOP_LOSS
        if position.take_profit is not None and quote.bid >= position.take_profit:
            return CloseReason.TAKE_PROFIT
    else:
        if position.stop_loss is not None and quote.ask >= position.stop_loss:
            return CloseReason.STOP_LOSS
        if position.take_profit is not None and quote.ask <= position.take_profit:
            return CloseReason.TAKE_PROFIT
    return None
