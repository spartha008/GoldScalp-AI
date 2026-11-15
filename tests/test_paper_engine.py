"""Paper-trading engine: balance/equity bookkeeping, refusals, SL/TP and snapshots."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from goldscalp.core.kinds import MarketState
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.portfolio import OrderRejected, PaperTradingEngine
from goldscalp.storage import paper_repo
from tests.conftest import OPEN_TIME, make_quote


@pytest.fixture
def quote_box():
    return {"q": make_quote("4000.00", "4000.30")}


@pytest.fixture
def engine(db, clock, spec, costs, account_config, quote_box):
    return PaperTradingEngine(db, clock, lambda: quote_box["q"], spec, costs, account_config)


def balance_identity(engine) -> tuple[D, D]:
    """balance == start + sum(realized_net of closed) - sum(open_commission of open)."""
    s = engine.summary()
    account = engine.ensure_account()
    with engine.db.read() as con:
        positions = paper_repo.positions(con, account.id)
    expected = account.config.starting_balance + sum((p.realized_net for p in positions if p.status == "closed"), D(0)) \
        - sum((p.open_commission for p in positions if p.status == "open"), D(0))
    return D(s["balance"]), expected


def test_new_account_starts_with_the_configured_virtual_balance(engine):
    s = engine.summary()
    assert D(s["balance"]) == D("10000")
    assert D(s["equity"]) == D("10000")
    assert D(s["used_margin"]) == 0
    assert s["label"].startswith("SIMULATED")


def test_open_buy_updates_balance_margin_and_equity(engine):
    p = engine.open_position("buy", "0.02")
    assert p.ounces == D("2.00")
    assert p.open_price == D("4000.35")             # ask 4000.30 + slippage 0.05
    assert p.margin == D("8000.7000")               # 2 oz x 4000.35 / 1
    assert p.open_commission == D("0.0700")         # 0.02 x 3.50
    s = engine.summary()
    assert D(s["balance"]) == D("9999.93")
    assert D(s["unrealized_pnl"]) == D("-0.70")     # (bid 4000.00 - 4000.35) x 2
    assert D(s["equity"]) == D("9999.23")
    assert D(s["free_margin"]) == D("9999.23") - D("8000.70")


def test_close_realizes_pnl_net_of_both_commissions(engine, quote_box):
    p = engine.open_position("sell", "0.02")        # sell at bid 4000.00 - 0.05 = 3999.95, 2 oz
    assert p.open_price == D("3999.95")
    quote_box["q"] = make_quote("3990.00", "3990.30")
    closed = engine.close_position(p.id)
    assert closed.close_price == D("3990.35")       # ask + slippage
    assert closed.realized_gross == D("19.20")      # (3999.95 - 3990.35) x 2
    assert closed.realized_net == D("19.06")        # 19.20 - 0.07 - 0.07
    s = engine.summary()
    assert D(s["balance"]) == D("10019.06")
    assert D(s["used_margin"]) == 0
    actual, expected = balance_identity(engine)
    assert actual == expected


def test_balance_identity_holds_through_many_trades(engine, quote_box):
    import random
    rng = random.Random(3)
    price = D("4000.00")
    for i in range(25):
        price += D(rng.randint(-150, 150)) / D(100)
        quote_box["q"] = make_quote(str(price), str(price + D("0.25")))
        open_now = engine.positions_view("open")
        if open_now and rng.random() < 0.5:
            engine.close_position(open_now[0]["id"])
        else:
            try:
                engine.open_position(rng.choice(["buy", "sell"]), "0.01")
            except OrderRejected:
                pass
        actual, expected = balance_identity(engine)
        assert actual == expected, f"after step {i}"


def test_insufficient_margin_is_refused_with_reason(engine):
    with pytest.raises(OrderRejected, match="insufficient free margin"):
        engine.open_position("buy", "0.10")         # 10 oz x 4000 = 40,000 at 1:1 > 10,000


def test_leverage_changes_margin(db, clock, spec, costs, quote_box):
    cfg = AccountConfig.from_mapping({"name": "Lev", "currency": "USD", "starting_balance": "10000", "leverage": 100,
                                      "max_quote_age_seconds": 30, "allow_delayed_fills": False})
    engine = PaperTradingEngine(db, clock, lambda: quote_box["q"], spec, costs, cfg)
    p = engine.open_position("buy", "1.00")        # 100 oz x 4000.35 / 100
    assert p.margin == D("4000.35")


def test_no_price_means_no_fill(db, clock, spec, costs, account_config):
    engine = PaperTradingEngine(db, clock, lambda: None, spec, costs, account_config)
    with pytest.raises(OrderRejected, match="no market price"):
        engine.open_position("buy", "0.01")


def test_stale_price_is_never_used_for_a_fill(engine, quote_box, clock):
    quote_box["q"] = make_quote("4000.00", "4000.30", at=OPEN_TIME - timedelta(seconds=31))
    with pytest.raises(OrderRejected, match="31 s old"):
        engine.open_position("buy", "0.01")


@pytest.mark.parametrize("state", [MarketState.DISCONNECTED, MarketState.HISTORICAL])
def test_disconnected_or_closed_market_refuses_fills(engine, quote_box, state):
    quote_box["q"] = make_quote("4000.00", "4000.30", state=state)
    with pytest.raises(OrderRejected, match=state.value):
        engine.open_position("buy", "0.01")


def test_delayed_fills_need_explicit_permission(db, clock, spec, costs, account_config, quote_box):
    quote_box["q"] = make_quote("4000.00", "4000.30", state=MarketState.DELAYED)
    strict = PaperTradingEngine(db, clock, lambda: quote_box["q"], spec, costs, account_config)
    with pytest.raises(OrderRejected, match="DELAYED"):
        strict.open_position("buy", "0.01")
    allowed_cfg = AccountConfig.from_mapping({**account_config.to_mapping(), "allow_delayed_fills": True})
    allowed = PaperTradingEngine(db, clock, lambda: quote_box["q"], spec, costs, allowed_cfg)
    allowed.create_account(allowed_cfg, spec, costs)
    p = allowed.open_position("buy", "0.01")
    assert p.open_price_state == "DELAYED"          # every fill records the state it was made on


def test_stop_loss_closes_at_the_current_bid_not_the_level(engine, quote_box):
    p = engine.open_position("buy", "0.01", stop_loss="3995")
    quote_box["q"] = make_quote("3990.00", "3990.30")   # price gapped through the stop
    closed = engine.on_quote()
    assert [c.id for c in closed] == [p.id]
    assert closed[0].close_reason == "stop_loss"
    assert closed[0].close_price == D("3989.95")         # bid - slippage, below the 3995 level


def test_stale_quotes_never_trigger_sl_tp(engine, quote_box):
    engine.open_position("buy", "0.01", stop_loss="3995")
    quote_box["q"] = make_quote("3990.00", "3990.30", at=OPEN_TIME - timedelta(minutes=5))
    assert engine.on_quote() == []
    assert engine.summary()["open_positions"] == 1


def test_equity_snapshots_are_recorded_on_fills(engine, quote_box, clock):
    p = engine.open_position("buy", "0.01")
    clock.advance(seconds=5)
    quote_box["q"] = make_quote("4001.00", "4001.30", at=clock.now())
    engine.close_position(p.id)
    history = engine.equity_history()
    assert len(history) >= 2
    assert all(row["equity"] is not None for row in history)


def test_new_account_keeps_the_old_one(engine, spec, costs, account_config):
    first = engine.ensure_account().id
    second = engine.create_account(account_config, spec, costs).id
    assert second != first
    with engine.db.read() as con:
        assert len(paper_repo.list_accounts(con)) == 2
