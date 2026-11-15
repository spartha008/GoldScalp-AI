"""Turn a market quote into simulated execution prices.

Rules:
- A simulated fill needs a quote whose market state is allowed (LIVE; DELAYED
  only if the account enables it) and that is no older than max_quote_age.
  Otherwise the order is REFUSED. Old prices are never substituted silently.
- Spread comes from provider bid/ask, or from the configured spread around the
  mid/last price. The source of the spread is recorded on every fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from goldscalp.core.clock import ensure_aware
from goldscalp.core.kinds import MarketState
from goldscalp.simulation.costs import CostModel, SpreadMode

TWO = Decimal(2)


class PricingError(ValueError):
    """No acceptable price for a simulated fill."""


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    quote_time: datetime        # provider's own timestamp for the price (UTC)
    received_at: datetime       # when GoldScalp received it (UTC)
    source_id: str
    state: MarketState
    state_reason: str


@dataclass(frozen=True)
class ExecutionQuote:
    bid: Decimal
    ask: Decimal
    spread: Decimal
    spread_source: str          # "market" | "configured"
    quote_time: datetime
    state: MarketState
    source_id: str


def execution_quote(quote: MarketQuote | None, model: CostModel, *, now: datetime, max_age_seconds: int,
                    allowed_states: tuple[MarketState, ...]) -> ExecutionQuote:
    if quote is None:
        raise PricingError("no market price is available; simulated orders need a current price")
    if quote.state not in allowed_states:
        allowed = "/".join(s.value for s in allowed_states)
        raise PricingError(f"market data is {quote.state.value} ({quote.state_reason}); "
                           f"simulated fills require {allowed} prices")
    age = (ensure_aware(now) - ensure_aware(quote.quote_time)).total_seconds()
    if age > max_age_seconds:
        raise PricingError(f"the latest price is {int(age)} s old (limit {max_age_seconds} s); "
                           "refusing to fill on an old price")
    has_bid_ask = quote.bid is not None and quote.ask is not None and quote.ask >= quote.bid > 0
    if model.spread_mode is SpreadMode.MARKET:
        if not has_bid_ask:
            raise PricingError("spread mode 'market' needs bid and ask from the provider, which supplied none")
        return _from_bid_ask(quote)
    if model.spread_mode is SpreadMode.MARKET_ELSE_CONFIGURED and has_bid_ask:
        return _from_bid_ask(quote)
    if has_bid_ask:
        mid = (quote.bid + quote.ask) / TWO
    elif quote.last is not None and quote.last > 0:
        mid = quote.last
    else:
        raise PricingError("the quote carries neither bid/ask nor a last price")
    assert model.configured_spread is not None  # guaranteed by CostModel.from_mapping
    half = model.configured_spread / TWO
    return ExecutionQuote(bid=mid - half, ask=mid + half, spread=model.configured_spread,
                          spread_source="configured", quote_time=quote.quote_time, state=quote.state,
                          source_id=quote.source_id)


def _from_bid_ask(quote: MarketQuote) -> ExecutionQuote:
    assert quote.bid is not None and quote.ask is not None
    return ExecutionQuote(bid=quote.bid, ask=quote.ask, spread=quote.ask - quote.bid, spread_source="market",
                          quote_time=quote.quote_time, state=quote.state, source_id=quote.source_id)
