"""MetaTrader 5 provider, fully mocked (no terminal, no broker, no network)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import gaps
from goldscalp.data.ingestion import store_live_candles
from goldscalp.data.live import MarketDataService
from goldscalp.data.providers.mt5_provider import (
    BrokerDisconnected, InitializeFailed, InvalidTick, MT5Provider, NoTick, PackageMissing, ReadOnlyMT5,
    SymbolNotFound, TerminalNotRunning, basis_for_offset, detect_offset, rank_gold_symbols,
)
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.storage import candle_repo
from tests.conftest import OPEN_TIME
from tests.fake_mt5 import FakeMT5, symbol

SUMMER = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)   # US DST: New-York-close servers are UTC+3


def make(fake: FakeMT5, *, clock: FixedClock | None = None, running: bool = True, **kwargs) -> MT5Provider:
    return MT5Provider(clock=clock or FixedClock(OPEN_TIME), module_loader=lambda: ReadOnlyMT5(fake),
                       process_check=lambda image: running, **kwargs)


def quote(provider: MT5Provider):
    return asyncio.run(provider.fetch_quote())


def service(db, spec, provider: MT5Provider, clock: FixedClock) -> MarketDataService:
    return MarketDataService(provider=provider, db=db, clock=clock, schedule=spec.schedule, symbol="XAUUSD",
                             price_decimals=5, live_window=10, disconnect_after=180)


def poll(svc: MarketDataService) -> float:
    return asyncio.run(svc.poll_once(None))


# ------------------------------------------------------------------ initialisation

def test_initialize_success_attaches_without_credentials():
    fake = FakeMT5()
    fake.set_tick("XAUUSD", OPEN_TIME - timedelta(seconds=1), 4000.10, 4000.35)
    provider = make(fake)
    q = quote(provider)
    assert (q.bid, q.ask) == (D("4000.1"), D("4000.35"))
    assert q.quote_time == OPEN_TIME - timedelta(seconds=1)       # server UTC+2 converted back to UTC
    init_kwargs = next(kw for name, kw in fake.calls if name == "initialize")
    assert set(init_kwargs) <= {"timeout", "path"}                 # never login / password / server
    details = provider.details()
    assert details["terminal"]["connected"] is True and details["symbol"] == "XAUUSD"
    assert details["time_basis"] == "rule:ny_close" and details["time_basis_source"] == "assumed"   # one tick: no proof
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.20, 4000.45)          # a NEW tick proves the stream is live
    quote(provider)
    details = provider.details()
    assert details["time_basis"] == "rule:ny_close" and details["server_offset_hours"] == 2
    assert details["time_basis_source"] == "detected"


def test_initialize_failure_is_a_link_down_error_with_a_hint():
    provider = make(FakeMT5(init_ok=False))
    with pytest.raises(InitializeFailed, match="Is MetaTrader 5 running") as err:
        quote(provider)
    assert err.value.link_down is True


def test_authorization_failure_never_asks_for_a_password():
    provider = make(FakeMT5(init_ok=False, init_error=(-6, "Terminal: Authorization failed")))
    with pytest.raises(InitializeFailed, match="never asks for or stores your password"):
        quote(provider)


def test_terminal_not_running_does_not_launch_it():
    fake = FakeMT5()
    with pytest.raises(TerminalNotRunning, match="not running"):
        quote(make(fake, running=False))
    assert fake.calls == []                                        # initialize() was never called


def test_missing_package_is_reported():
    def missing():
        raise PackageMissing("the MetaTrader5 package is not installed")
    provider = MT5Provider(clock=FixedClock(OPEN_TIME), module_loader=missing, process_check=lambda i: True)
    with pytest.raises(PackageMissing):
        quote(provider)


def test_broker_disconnected():
    with pytest.raises(BrokerDisconnected, match="NOT connected to its broker"):
        quote(make(FakeMT5(connected=False)))


# ------------------------------------------------------------------ symbols

def test_configured_symbol_that_does_not_exist():
    fake = FakeMT5(symbols=[symbol("XAUUSDm"), symbol("EURUSDm", base="EUR")])
    with pytest.raises(SymbolNotFound, match="'XAUUSD' does not exist.*XAUUSDm"):
        quote(make(fake, symbol="XAUUSD"))


def test_no_gold_symbol_requests_manual_selection():
    fake = FakeMT5(symbols=[symbol("EURUSD", base="EUR"), symbol("XAUEUR", profit="EUR")])
    with pytest.raises(SymbolNotFound, match="set GOLDSCALP_MT5_SYMBOL"):
        quote(make(fake))


@pytest.mark.parametrize("symbols, expected", [
    # the real broker seen during development: a visible conversion symbol and the hidden tradable one
    ([symbol("XAUUSD.crp", description="Not for trading purposes, currency conversion", digits=3),
      symbol("XAUUSD", visible=False)], "XAUUSD"),
    ([symbol("XAUEURm", profit="EUR"), symbol("XAUUSDm")], "XAUUSDm"),
    ([symbol("SILVER", base="XAG"), symbol("GOLD")], "GOLD"),
    ([symbol("XAUUSDT", profit="USDT"), symbol("XAUUSD.r")], "XAUUSD.r"),
    ([symbol("GOLD#", base="GOLD", profit="USD"), symbol("XAUUSD_i")], "XAUUSD_i"),
    # the penalty alone decides: an exactly-named conversion symbol must lose to the tradable one
    ([symbol("XAUUSD", description="Not for trading purposes, currency conversion"),
      symbol("XAUUSD.r", description="Gold Spot")], "XAUUSD.r"),
])
def test_gold_symbol_discovery_handles_broker_variations(symbols, expected):
    assert rank_gold_symbols(symbols)[0]["name"] == expected


def test_hidden_symbol_is_added_to_market_watch():
    fake = FakeMT5(symbols=[symbol("XAUUSD", visible=False)])
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.3)
    quote(make(fake))
    assert ("symbol_select", "XAUUSD") in fake.calls


def test_manual_symbol_override_wins_over_auto_detection():
    fake = FakeMT5(symbols=[symbol("XAUUSD"), symbol("XAUUSD.ecn")])
    fake.set_tick("XAUUSD.ecn", OPEN_TIME, 4000.0, 4000.1)
    provider = make(fake, symbol="XAUUSD.ecn")
    quote(provider)
    assert provider.details()["symbol"] == "XAUUSD.ecn" and provider.details()["symbol_source"] == "configured"


# ------------------------------------------------------------------ ticks

def test_missing_tick():
    fake = FakeMT5()
    with pytest.raises(NoTick, match="no tick for XAUUSD"):
        quote(make(fake))


@pytest.mark.parametrize("bid, ask", [(0.0, 4000.3), (4000.3, 4000.0), (float("nan"), 4000.3), (None, 4000.3),
                                      (4000.0, float("inf"))])
def test_invalid_bid_ask_is_refused(bid, ask):
    fake = FakeMT5()
    fake.set_tick("XAUUSD", OPEN_TIME, bid, ask)
    with pytest.raises(InvalidTick):
        quote(make(fake))


def test_valid_tick_spread_and_live_state(db, spec):
    fake = FakeMT5()
    clock = FixedClock(OPEN_TIME)
    fake.set_tick("XAUUSD", OPEN_TIME - timedelta(seconds=2), 4000.10, 4000.31)
    svc = service(db, spec, make(fake, clock=clock), clock)
    assert poll(svc) == 1.0                                        # polls every second
    snap = svc.snapshot()
    assert snap["state"] == "LIVE" and D(snap["quote"]["spread"]) == D("0.21")
    assert snap["provider"]["id"] == "mt5" and snap["provider_details"]["symbol"] == "XAUUSD"


def test_stale_tick_is_delayed_never_live(db, spec):
    fake = FakeMT5()
    clock = FixedClock(OPEN_TIME)
    fake.set_tick("XAUUSD", OPEN_TIME - timedelta(seconds=45), 4000.10, 4000.31)
    svc = service(db, spec, make(fake, clock=clock), clock)
    poll(svc)
    state, reason = svc.state()
    assert state is MarketState.DELAYED and "freshness threshold 10 s" in reason


def test_closed_market_with_old_tick_is_historical(db, spec):
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    fake = FakeMT5()
    clock = FixedClock(saturday)
    fake.set_tick("XAUUSD", datetime(2026, 11, 13, 21, 59, 58, tzinfo=UTC), 4000.1, 4000.4)   # Friday close
    svc = service(db, spec, make(fake, clock=clock, time_basis="rule:ny_close"), clock)
    poll(svc)
    assert svc.state()[0] is MarketState.HISTORICAL


# ------------------------------------------------------------------ timestamps

def test_server_offset_detection_follows_us_dst():
    assert detect_offset(OPEN_TIME.timestamp() + 7200 - 3, OPEN_TIME) == 7200
    assert basis_for_offset(7200, OPEN_TIME) == "rule:ny_close"     # November: NY -5 + 7
    assert basis_for_offset(10800, SUMMER) == "rule:ny_close"       # July: NY -4 + 7
    assert basis_for_offset(3600, OPEN_TIME) == "fixed:+01:00"
    assert basis_for_offset(0, OPEN_TIME) == "utc"
    assert detect_offset(OPEN_TIME.timestamp() + 7200 - 600, OPEN_TIME) is None   # 10-minute-old tick: no guess


def test_summer_server_time_is_converted_correctly():
    fake = FakeMT5(server_offset_hours=3)
    fake.set_tick("XAUUSD", SUMMER - timedelta(seconds=1), 4000.0, 4000.2)
    q = quote(make(fake, clock=FixedClock(SUMMER)))
    assert q.quote_time == SUMMER - timedelta(seconds=1)


def test_wrong_configured_time_basis_is_detected():
    fake = FakeMT5(server_offset_hours=2)
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    with pytest.raises(InvalidTick, match="in the future.*GOLDSCALP_MT5_TIME_BASIS"):
        quote(make(fake, time_basis="utc"))


# ------------------------------------------------------------------ candles

def _bars(start: datetime, n: int, step: int = 60, price: float = 4000.0):
    rows = []
    for i in range(n):
        o = price + i * 0.1
        rows.append((start + timedelta(seconds=i * step), o, o + 0.5, o - 0.5, o + 0.2))
    return rows


def _candles(fake: FakeMT5, tf: Timeframe, clock: FixedClock, first: bool = True) -> pd.DataFrame:
    provider = make(fake, clock=clock)
    return asyncio.run(provider.fetch_candles(None, tf, first=first, now=clock.now()))


@pytest.mark.parametrize("tf, mt5_tf", [(Timeframe.M1, 1), (Timeframe.M5, 5), (Timeframe.M15, 15), (Timeframe.H1, 16385)])
def test_historical_candles_for_every_timeframe(tf, mt5_tf):
    fake = FakeMT5()
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    start = OPEN_TIME - timedelta(seconds=tf.seconds * 10)
    fake.set_rates("XAUUSD", mt5_tf, _bars(start, 11, tf.seconds))
    frame = _candles(fake, tf, FixedClock(OPEN_TIME + timedelta(seconds=30)))
    assert len(frame) == 11
    assert frame["ts"].iloc[0] == pd.Timestamp(start)              # UTC, not server time
    assert frame["closed"].tolist() == [True] * 10 + [False]       # the current bar is still forming
    assert frame["spread"].iloc[0] == pytest.approx(0.25)          # 25 points x 0.01


def test_candles_are_validated_deduplicated_and_gap_checked(db, spec):
    fake = FakeMT5()
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    start = OPEN_TIME - timedelta(minutes=30)
    rows = _bars(start, 20)
    rows.insert(5, rows[5])                                         # duplicate timestamp
    rows[12] = (rows[12][0], 4000.0, 3990.0, 4001.0, 4000.0)        # invalid OHLC (high below low)
    del rows[15:17]                                                 # two missing minutes
    fake.set_rates("XAUUSD", 1, rows)
    clock = FixedClock(OPEN_TIME)
    provider = make(fake, clock=clock)
    frame = asyncio.run(provider.fetch_candles(None, Timeframe.M1, first=True, now=clock.now()))
    svc = service(db, spec, provider, clock)
    result = store_live_candles(db, clock, svc.series, Timeframe.M1, frame)
    assert result["duplicates_dropped"] == 1
    assert result["rejected_by_reason"] == {"ohlc_inconsistent": 1}
    with db.read() as con:
        row = candle_repo.series_row(con, "live:mt5:XAUUSD")
        stored = candle_repo.load_candles(con, row["id"], row["price_decimals"], Timeframe.M1)
    assert row["candle_side"] == "bid"
    report = gaps.gap_report(stored.index, Timeframe.M1, spec.schedule)
    assert report["missing"] == 3                                   # 2 deleted + 1 rejected; nothing invented
    assert stored.index.is_monotonic_increasing


# ------------------------------------------------------------------ read-only guarantee

@pytest.mark.parametrize("name", ["order_send", "order_check", "account_info", "positions_get", "orders_get",
                                  "history_deals_get", "history_orders_get", "login", "order_calc_margin"])
def test_trading_and_account_functions_are_blocked(name):
    with pytest.raises(PermissionError, match="read-only"):
        getattr(ReadOnlyMT5(FakeMT5()), name)


# ------------------------------------------------------------------ reconnection and status

def test_reconnection_after_terminal_failure(db, spec):
    fake = FakeMT5(init_ok=False)
    clock = FixedClock(OPEN_TIME)
    svc = service(db, spec, make(fake, clock=clock), clock)
    delays = [poll(svc), poll(svc)]
    assert delays == [1.0, 2.0]                                     # backoff starts at the 1 s poll cadence
    state, reason = svc.state()
    assert state is MarketState.DISCONNECTED and "Is MetaTrader 5 running" in reason
    fake.init_ok = True
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    poll(svc)
    s = svc.status
    assert s.reconnect_attempts == 2 and s.reconnects == 1 and s.link_down_reason is None
    assert svc.state()[0] is MarketState.LIVE


def test_disconnect_keeps_last_price_but_labels_it_stale(db, spec):
    fake = FakeMT5()
    clock = FixedClock(OPEN_TIME)
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    svc = service(db, spec, make(fake, clock=clock), clock)
    poll(svc)
    fake.connected = False
    clock.advance(seconds=1)
    poll(svc)
    snap = svc.snapshot()
    assert snap["state"] == "DISCONNECTED" and "STALE" in snap["reason"]
    assert snap["quote"]["bid"] == "4000.0" and snap["quote"]["stale"] is True


def test_manual_reconnect_reinitializes(db, spec):
    fake = FakeMT5()
    clock = FixedClock(OPEN_TIME)
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    svc = service(db, spec, make(fake, clock=clock), clock)
    poll(svc)
    asyncio.run(svc.reconnect())
    poll(svc)
    assert [name for name, _ in fake.calls].count("initialize") == 2


# ------------------------------------------------------------------ paper simulator compatibility

def test_paper_simulator_uses_mt5_bid_ask(db, spec, costs, account_config):
    fake = FakeMT5()
    clock = FixedClock(OPEN_TIME)
    fake.set_tick("XAUUSD", OPEN_TIME - timedelta(seconds=1), 4000.10, 4000.31)
    svc = service(db, spec, make(fake, clock=clock), clock)
    poll(svc)
    engine = PaperTradingEngine(db, clock, svc.market_quote, spec, costs, account_config)
    est = engine.estimate("buy", "0.01")
    assert est["spread_source"] == "market" and D(est["spread"]) == D("0.21") and est["source_id"] == "mt5"
    position = engine.open_position("buy", "0.01")
    assert position.open_price == D("4000.36")                      # MT5 ask 4000.31 + slippage 0.05
    assert position.open_source == "mt5" and position.open_price_state == "LIVE"
    fake.connected = False
    poll(svc)
    from goldscalp.simulation.portfolio import OrderRejected
    with pytest.raises(OrderRejected, match="DISCONNECTED"):
        engine.open_position("buy", "0.01")                         # never fills on a stale/disconnected price



def test_a_stale_weekend_tick_never_changes_the_time_basis():
    """Regression (14 Nov 2026, actual terminal): with the market closed, the Friday tick periodically matched a
    15-minute-rounded offset (-12:15 at Saturday 12:12 UTC) and was taken as fresh; each new 'basis' re-backfilled
    every timeframe at shifted timestamps. Only a NEW tick may now set the basis."""
    friday = datetime(2026, 11, 13, 21, 56, 59, 906000, tzinfo=UTC)
    fake = FakeMT5(server_offset_hours=2)
    fake.set_tick("XAUUSD", friday, 3881.73, 3882.07)
    clock = FixedClock(datetime(2026, 11, 14, 11, 52, tzinfo=UTC))
    provider = make(fake, clock=clock)
    first = quote(provider)
    assert first.quote_time == friday and provider.basis_source == "assumed"
    for minutes in range(0, 140, 1):                         # Saturday 12:00 .. 14:19, every minute (all windows)
        clock.set(datetime(2026, 11, 14, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes, seconds=13))
        q = quote(provider)
        assert q.quote_time == friday, clock.now()           # the tick is never re-dated
    assert provider.basis.text == "rule:ny_close" and provider.basis_source == "assumed"
    assert provider.basis_version == 1                       # never changed: no re-backfill was triggered



def test_deep_backfill_depth_is_configurable_and_falls_back_when_refused():
    fake = FakeMT5()
    fake.set_tick("XAUUSD", OPEN_TIME, 4000.0, 4000.2)
    start = OPEN_TIME - timedelta(minutes=50)
    fake.set_rates("XAUUSD", 1, _bars(start, 50))
    asked = []
    real = fake.copy_rates_from_pos

    def limited(name, timeframe, pos, count):
        asked.append(count)
        if count > 5000:
            fake._error = (-2, "Terminal: Invalid params")
            return None
        return real(name, timeframe, pos, count)
    fake.copy_rates_from_pos = limited
    provider = MT5Provider(clock=FixedClock(OPEN_TIME), module_loader=lambda: ReadOnlyMT5(fake),
                           process_check=lambda image: True, backfill_bars={Timeframe.M1: 100000})
    frame = asyncio.run(provider.fetch_candles(None, Timeframe.M1, first=True, now=OPEN_TIME))
    assert asked == [100000, 3000] and len(frame) == 50
    assert "100000 bars refused" in provider.backfill_notes["M1"]
