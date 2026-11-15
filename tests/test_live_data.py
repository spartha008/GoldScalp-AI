"""Live market data: state classification, provider failures, rate limits, stale data, parsing."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, UTC
from decimal import Decimal as D

import httpx
import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.kinds import MarketState
from goldscalp.data.live import ConnectionStatus, MarketDataService, RequestBudget, TickCandleBuilder, classify
from goldscalp.data.providers import gold_api, tiingo
from goldscalp.data.providers.base import (
    CredentialsRejected, MalformedResponse, NotEntitled, RateLimited, RawQuote, SourceUnavailable,
)
from tests.conftest import OPEN_TIME

UTC = UTC
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
OPEN = {"open": True, "reason": "open", "next_change": OPEN_TIME}
CLOSED = {"open": False, "reason": "weekly_close", "next_change": OPEN_TIME}


def quote(age_s: int = 0, now: datetime = OPEN_TIME, bid="4000.00", ask="4000.30") -> RawQuote:
    t = now - timedelta(seconds=age_s)
    return RawQuote(bid=D(bid) if bid else None, ask=D(ask) if ask else None, last=None, quote_time=t, received_at=t)


def status(success_ago: int | None = 0, now: datetime = OPEN_TIME, error: str | None = None) -> ConnectionStatus:
    s = ConnectionStatus(enabled=True, transport="websocket")
    if success_ago is not None:
        s.last_success_at = now - timedelta(seconds=success_ago)
    s.last_error_message = error
    return s


def state_of(info, q, st, market=OPEN, enabled=True, has_history=False):
    return classify(info=info, enabled=enabled, quote=q, status=st, market=market, now=OPEN_TIME,
                    live_window=15, disconnect_after=180, has_history=has_history)[0]


TIINGO = tiingo.info_for("xauusd")
GOLDAPI = gold_api.INFO


# ------------------------------------------------------------------ the four states

def test_fresh_stream_is_live():
    assert state_of(TIINGO, quote(2), status(2)) is MarketState.LIVE


def test_stale_stream_is_delayed_never_live():
    assert state_of(TIINGO, quote(40), status(1)) is MarketState.DELAYED


def test_snapshot_provider_is_delayed_even_when_fresh():
    assert state_of(GOLDAPI, quote(1), status(1)) is MarketState.DELAYED


def test_no_contact_for_too_long_is_disconnected_even_with_an_old_price():
    assert state_of(TIINGO, quote(500), status(500, error="timeout")) is MarketState.DISCONNECTED


def test_closed_market_is_historical():
    # Stale data while the schedule says closed -> HISTORICAL (snapshot providers always are).
    assert state_of(TIINGO, quote(60), status(2), market=CLOSED) is MarketState.HISTORICAL
    assert state_of(GOLDAPI, quote(2), status(2), market=CLOSED) is MarketState.HISTORICAL


def test_fresh_realtime_data_beats_the_generic_schedule():
    # The broker's own session is the authority: a 2-second-old real-time tick is LIVE even if
    # GoldScalp's approximate schedule says the market is closed.
    assert state_of(TIINGO, quote(2), status(2), market=CLOSED) is MarketState.LIVE


def test_no_provider_is_disconnected_or_historical():
    assert state_of(None, None, status(None), enabled=False) is MarketState.DISCONNECTED
    assert state_of(None, None, status(None), enabled=False, has_history=True) is MarketState.HISTORICAL


def test_first_connection_failure_is_disconnected():
    assert state_of(TIINGO, None, status(None, error="refused")) is MarketState.DISCONNECTED


# ------------------------------------------------------------------ parsing

def test_gold_api_parse_uses_provider_timestamp_and_has_no_bid_ask():
    q = gold_api.parse_price({"price": 3884.320068, "symbol": "XAU", "currency": "USD",
                              "updatedAt": "2026-11-13T17:26:25Z"}, "public, max-age=3", OPEN_TIME)
    assert q.last == D("3884.320068") and q.bid is None and q.ask is None
    assert q.quote_time == datetime(2026, 11, 13, 17, 26, 25, tzinfo=UTC)
    assert q.next_update_seconds == 3


@pytest.mark.parametrize("body", [None, {"symbol": "XAG", "price": 1, "updatedAt": "2026-11-13T17:26:25Z"},
                                  {"symbol": "XAU", "price": -5, "updatedAt": "2026-11-13T17:26:25Z"},
                                  {"symbol": "XAU", "price": 4000, "updatedAt": "2026-11-13T17:26:25"}])
def test_gold_api_malformed_responses_are_refused(body):
    with pytest.raises(MalformedResponse):
        gold_api.parse_price(body, None, OPEN_TIME)


def test_tiingo_messages():
    msg = json.dumps({"service": "fx", "messageType": "A",
                      "data": ["Q", "xauusd", "2026-11-11T15:00:00.123+00:00", 1e6, 4000.1, 4000.25, 1e6, 4000.4]})
    kind, q = tiingo.parse_message(msg, "xauusd", OPEN_TIME)
    assert kind == "quote" and (q.bid, q.ask, q.last) == (D("4000.1"), D("4000.4"), D("4000.25"))
    assert tiingo.parse_message(json.dumps({"messageType": "H"}), "xauusd", OPEN_TIME)[0] == "heartbeat"
    assert tiingo.parse_message(json.dumps({"messageType": "I", "data": {"subscriptionId": 1}}), "xauusd",
                                OPEN_TIME)[0] == "info"
    other = json.dumps({"messageType": "A", "data": ["Q", "eurusd", "2026-11-11T15:00:00Z", 1, 1.1, 1.1, 1, 1.1]})
    assert tiingo.parse_message(other, "xauusd", OPEN_TIME)[0] == "ignore"
    crossed = json.dumps({"messageType": "A", "data": ["Q", "xauusd", "2026-11-11T15:00:00Z", 1, 4001, 4000.5, 1, 4000]})
    assert tiingo.parse_message(crossed, "xauusd", OPEN_TIME)[0] == "ignore"
    error = json.dumps({"messageType": "E", "response": {"code": 401, "message": "Invalid token"}})
    assert tiingo.parse_message(error, "xauusd", OPEN_TIME)[0] == "error"


def test_tiingo_candles_parse():
    frame = tiingo.parse_prices([{"date": "2026-11-11T15:00:00.000Z", "ticker": "xauusd", "open": 4000, "high": 4001,
                                  "low": 3999, "close": 4000.5}])
    assert frame["ts"].iloc[0] == datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


# ------------------------------------------------------------------ connection failures and rate limits

def run_poll(db, spec, handler, clock=None):
    clock = clock or FixedClock(OPEN_TIME)
    service = MarketDataService(provider=gold_api.GoldApiProvider(), db=db, clock=clock, schedule=spec.schedule,
                                symbol="XAUUSD", price_decimals=5, live_window=15, disconnect_after=180)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await service.poll_once(client)
    return service, asyncio.run(go())


def ok_body(updated="2026-11-11T14:59:55Z", price=4000.12):
    return httpx.Response(200, json={"price": price, "symbol": "XAU", "currency": "USD", "updatedAt": updated},
                          headers={"cache-control": "public, max-age=12"})


def test_successful_poll_records_quote_and_schedules_after_next_update(db, spec):
    service, delay = run_poll(db, spec, lambda req: ok_body())
    assert service.latest.last == D("4000.12")
    assert delay == 44.0          # next update in 12 s + 2 s, pushed past the 30 s cadence
    assert service.state()[0] is MarketState.DELAYED


@pytest.mark.parametrize("response, error, kind", [
    (httpx.Response(401), CredentialsRejected, "credentials_rejected"),
    (httpx.Response(403), NotEntitled, "not_entitled"),
    (httpx.Response(429, headers={"retry-after": "120"}), RateLimited, "rate_limited"),
    (httpx.Response(503), SourceUnavailable, "source_unavailable"),
    (httpx.Response(200, text="<html>"), MalformedResponse, "malformed_response"),
])
def test_provider_failures_are_classified(db, spec, response, error, kind):
    service, delay = run_poll(db, spec, lambda req: response)
    assert service.status.last_error_kind == kind
    assert service.latest is None
    assert service.state()[0] is MarketState.DISCONNECTED
    if error is RateLimited:
        assert delay == 120.0     # Retry-After is honoured
    if error in (CredentialsRejected, NotEntitled):
        assert delay == 900.0     # permanent problems back off for 15 minutes


def test_network_failure_backs_off_exponentially(db, spec):
    def boom(req):
        raise httpx.ConnectError("refused")
    clock = FixedClock(OPEN_TIME)
    service = MarketDataService(provider=gold_api.GoldApiProvider(), db=db, clock=clock, schedule=spec.schedule,
                                symbol="XAUUSD", price_decimals=5, live_window=15, disconnect_after=180)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            return [await service.poll_once(client) for _ in range(5)]
    delays = asyncio.run(go())
    assert delays == [30.0, 60.0, 120.0, 240.0, 300.0]
    assert service.status.consecutive_failures == 5


def test_reconnect_after_failures_restores_prices(db, spec):
    responses = iter([httpx.Response(503), ok_body()])
    clock = FixedClock(OPEN_TIME)
    service = MarketDataService(provider=gold_api.GoldApiProvider(), db=db, clock=clock, schedule=spec.schedule,
                                symbol="XAUUSD", price_decimals=5, live_window=15, disconnect_after=180)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))) as client:
            await service.poll_once(client)
            await service.poll_once(client)
    asyncio.run(go())
    assert service.status.reconnects == 1 and service.status.consecutive_failures == 0
    assert service.latest is not None


def test_old_price_never_replaces_a_newer_one(db, spec):
    responses = iter([ok_body("2026-11-11T14:59:55Z", 4000.0), ok_body("2026-11-11T14:59:00Z", 3990.0)])
    service = MarketDataService(provider=gold_api.GoldApiProvider(), db=db, clock=FixedClock(OPEN_TIME),
                                schedule=spec.schedule, symbol="XAUUSD", price_decimals=5, live_window=15,
                                disconnect_after=180)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))) as client:
            await service.poll_once(client)
            await service.poll_once(client)
    asyncio.run(go())
    assert service.latest.last == D("4000.0")


def test_data_goes_stale_then_disconnected_as_time_passes(db, spec):
    clock = FixedClock(OPEN_TIME)
    service, _ = run_poll(db, spec, lambda req: ok_body(), clock=clock)
    assert service.state()[0] is MarketState.DELAYED
    clock.advance(seconds=181)
    state, reason = service.state()
    assert state is MarketState.DISCONNECTED and "no contact" in reason
    q = service.market_quote()
    assert q.state is MarketState.DISCONNECTED     # the simulator sees the same downgrade


def test_request_budget_refuses_when_full():
    t = [0.0]
    budget = RequestBudget(per_hour=3, per_day=5, clock=lambda: t[0])
    assert [budget.try_reserve() for _ in range(4)] == [True, True, True, False]
    t[0] = 3601
    assert budget.try_reserve() and budget.try_reserve()
    assert not budget.try_reserve()               # 5 per day reached


def test_tick_candle_builder():
    b = TickCandleBuilder()
    base = datetime(2026, 11, 11, 15, 0, 5, tzinfo=UTC)
    b.add(base, 4000.0)
    b.add(base + timedelta(seconds=20), 4002.0)
    rows = b.add(base + timedelta(seconds=30), 3999.0)
    assert rows[-1] | {} == {**rows[-1], "open": 4000.0, "high": 4002.0, "low": 3999.0, "close": 3999.0,
                             "tick_volume": 3, "closed": False}
    rows = b.add(base + timedelta(seconds=60), 4001.0)    # next minute closes the previous bar
    assert rows[0]["closed"] is True and rows[1]["open"] == 4001.0
    assert b.add(base, 3000.0) == []                       # an older tick never rewrites a newer bar


def test_snapshot_reports_spread_and_source(db, spec):
    service, _ = run_poll(db, spec, lambda req: ok_body())
    snap = service.snapshot()
    assert snap["quote"]["spread"] is None and snap["provider"]["id"] == "gold-api"
    assert snap["state"] == "DELAYED" and "Not a real-time stream" in snap["reason"]
