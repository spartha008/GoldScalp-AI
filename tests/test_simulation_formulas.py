"""Explicit, hand-checked monetary formulas of the lot-based simulator."""
from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from goldscalp.core.kinds import MarketState
from goldscalp.simulation.account import AccountConfig, margin_level
from goldscalp.simulation.costs import CostModel, commission, round_trip_estimate
from goldscalp.simulation.instrument import InstrumentSpec, SpecError
from goldscalp.simulation.positions import (
    CloseReason, Position, Side, close_fill_price, gross_pnl, open_fill_price, protection_trigger,
    required_margin, unrealized_pnl, validate_protection,
)
from goldscalp.simulation.pricing import PricingError, execution_quote
from tests.conftest import OPEN_TIME, SPEC_PATH, make_quote


def spec_with(**changes) -> InstrumentSpec:
    data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    data.update(changes)
    return InstrumentSpec.from_mapping(data)


# ------------------------------------------------------------------ lots and ounces

@pytest.mark.parametrize("lots, ounces", [("1.00", "100"), ("0.10", "10"), ("0.01", "1"), ("2.37", "237")])
def test_lots_to_ounces_with_100_oz_contract(spec, lots, ounces):
    assert spec.ounces(D(lots)) == D(ounces)


def test_contract_size_is_configuration_not_a_constant():
    small = spec_with(contract_size="10")
    assert small.ounces(D("0.10")) == D("1.0")
    assert small.ounces(D("1")) == D("10")


def test_lot_presets_are_standard_mini_micro(spec):
    assert spec.lot_presets == {"standard": D("1.00"), "mini": D("0.10"), "micro": D("0.01")}


@pytest.mark.parametrize("lots, message", [("0", "positive"), ("-0.01", "positive"), ("0.005", "below the minimum"),
                                           ("0.015", "not a multiple"), ("51", "above the maximum")])
def test_invalid_lot_sizes_are_refused(spec, lots, message):
    with pytest.raises(ValueError, match=message):
        spec.validate_lots(D(lots))


def test_missing_broker_specification_fields_are_never_assumed():
    data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    del data["contract_size"]
    with pytest.raises(SpecError, match="missing required fields: contract_size"):
        InstrumentSpec.from_mapping(data)


def test_spec_refuses_float_values_and_unknown_fields():
    with pytest.raises(SpecError, match="string or integer"):
        spec_with(contract_size=100.0)
    data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    data["leverage"] = 100
    with pytest.raises(SpecError, match="unknown fields"):
        InstrumentSpec.from_mapping(data)


# ------------------------------------------------------------------ spread and pricing

def model(mode="market_else_configured", spread="0.30", commission_="3.50", slippage="0.05") -> CostModel:
    return CostModel.from_mapping({"spread_mode": mode, "configured_spread": spread,
                                   "commission_per_lot_per_side": commission_, "slippage": slippage})


def test_spread_from_provider_bid_ask():
    q = execution_quote(make_quote("4000.10", "4000.40"), model(), now=OPEN_TIME, max_age_seconds=30,
                        allowed_states=(MarketState.LIVE,))
    assert (q.bid, q.ask, q.spread, q.spread_source) == (D("4000.10"), D("4000.40"), D("0.30"), "market")


def test_configured_spread_is_centred_on_the_last_price_when_no_bid_ask():
    q = execution_quote(make_quote(None, None, last="4000.00"), model(spread="0.40"), now=OPEN_TIME,
                        max_age_seconds=30, allowed_states=(MarketState.LIVE,))
    assert (q.bid, q.ask, q.spread, q.spread_source) == (D("3999.80"), D("4000.20"), D("0.40"), "configured")


def test_configured_mode_ignores_provider_spread():
    q = execution_quote(make_quote("4000.00", "4000.10"), model(mode="configured", spread="0.50"), now=OPEN_TIME,
                        max_age_seconds=30, allowed_states=(MarketState.LIVE,))
    assert (q.bid, q.ask) == (D("3999.80"), D("4000.30"))


def test_market_mode_refuses_without_bid_ask():
    with pytest.raises(PricingError, match="needs bid and ask"):
        execution_quote(make_quote(None, None, last="4000"), model(mode="market", spread=None), now=OPEN_TIME,
                        max_age_seconds=30, allowed_states=(MarketState.LIVE,))


# ------------------------------------------------------------------ fills, P/L, margin

def test_fill_prices_apply_adverse_slippage_on_both_sides():
    q = execution_quote(make_quote("4000.10", "4000.40"), model(), now=OPEN_TIME, max_age_seconds=30,
                        allowed_states=(MarketState.LIVE,))
    assert open_fill_price(Side.BUY, q, D("0.05")) == D("4000.45")    # ask + slippage
    assert open_fill_price(Side.SELL, q, D("0.05")) == D("4000.05")   # bid - slippage
    assert close_fill_price(Side.BUY, q, D("0.05")) == D("4000.05")   # bid - slippage
    assert close_fill_price(Side.SELL, q, D("0.05")) == D("4000.45")  # ask + slippage


def test_gross_pnl_long_and_short():
    assert gross_pnl(Side.BUY, D("4000.00"), D("4010.00"), D("10")) == D("100.00")
    assert gross_pnl(Side.SELL, D("4000.00"), D("4010.00"), D("10")) == D("-100.00")
    assert gross_pnl(Side.SELL, D("4000.00"), D("3990.50"), D("1")) == D("9.50")


def test_commission_per_lot_per_side():
    assert commission(D("0.10"), model(commission_="7.00")) == D("0.700")
    assert commission(D("2.00"), model(commission_="3.50")) == D("7.00")


@pytest.mark.parametrize("leverage, margin", [(1, D("40000.00")), (20, D("2000.00")), (100, D("400.00"))])
def test_margin_is_notional_over_leverage(leverage, margin):
    # 0.10 lot x 100 oz = 10 oz at 4000.00 -> notional 40,000
    assert required_margin(D("10"), D("4000.00"), leverage) == margin


def test_margin_level():
    assert margin_level(D("10000"), D("4000")) == D("250")
    assert margin_level(D("10000"), D("0")) is None


def test_round_trip_cost_estimate():
    est = round_trip_estimate(lots=D("0.10"), ounces=D("10"), spread=D("0.30"), model=model())
    assert est == {"spread_cost": D("3.00"), "commission": D("0.70"), "slippage_cost": D("1.00"), "total": D("4.70")}


def _position(side: Side, sl=None, tp=None) -> Position:
    return Position(id=1, account_id=1, side=side, lots=D("0.10"), ounces=D("10"), open_price=D("4000.00"),
                    open_time=OPEN_TIME, open_quote_time=OPEN_TIME, open_price_state="LIVE", open_spread=D("0.30"),
                    open_spread_source="market", open_slippage=D("0"), open_commission=D("0.35"),
                    margin=D("40000"), stop_loss=sl, take_profit=tp)


def test_unrealized_pnl_marks_at_the_closing_side():
    q = execution_quote(make_quote("4005.00", "4005.30"), model(), now=OPEN_TIME, max_age_seconds=30,
                        allowed_states=(MarketState.LIVE,))
    assert unrealized_pnl(_position(Side.BUY), q) == D("50.00")    # (bid - open) x 10
    assert unrealized_pnl(_position(Side.SELL), q) == D("-53.00")  # (open - ask) x 10


def test_sl_tp_triggers_on_the_correct_side():
    def eq(bid, ask):
        return execution_quote(make_quote(bid, ask), model(), now=OPEN_TIME, max_age_seconds=30,
                               allowed_states=(MarketState.LIVE,))
    long = _position(Side.BUY, sl=D("3990"), tp=D("4010"))
    assert protection_trigger(long, eq("3990.00", "3990.30")) is CloseReason.STOP_LOSS
    assert protection_trigger(long, eq("4010.00", "4010.30")) is CloseReason.TAKE_PROFIT
    assert protection_trigger(long, eq("4000.00", "4000.30")) is None
    short = _position(Side.SELL, sl=D("4010"), tp=D("3990"))
    assert protection_trigger(short, eq("4009.80", "4010.10")) is CloseReason.STOP_LOSS   # ask >= SL
    assert protection_trigger(short, eq("3989.60", "3989.90")) is CloseReason.TAKE_PROFIT  # ask <= TP
    assert protection_trigger(short, eq("4009.70", "4009.99")) is None


def test_protection_levels_must_be_on_the_right_side():
    q = execution_quote(make_quote("4000.00", "4000.30"), model(), now=OPEN_TIME, max_age_seconds=30,
                        allowed_states=(MarketState.LIVE,))
    with pytest.raises(ValueError, match="BUY stop loss must be below"):
        validate_protection(Side.BUY, q, D("4001"), None)
    with pytest.raises(ValueError, match="SELL take profit must be below"):
        validate_protection(Side.SELL, q, None, D("4001"))
    validate_protection(Side.BUY, q, D("3990"), D("4010"))


def test_account_config_requires_every_field_and_usd():
    good = {"name": "x", "currency": "USD", "starting_balance": "10000", "leverage": 1,
            "max_quote_age_seconds": 30, "allow_delayed_fills": False}
    AccountConfig.from_mapping(good)
    with pytest.raises(ValueError, match="missing: leverage"):
        AccountConfig.from_mapping({k: v for k, v in good.items() if k != "leverage"})
    with pytest.raises(ValueError, match="only USD"):
        AccountConfig.from_mapping({**good, "currency": "EUR"})
    with pytest.raises(ValueError, match="leverage must be"):
        AccountConfig.from_mapping({**good, "leverage": 0})
