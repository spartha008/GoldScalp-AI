"""Provider hub: priority, switching, fallback and recovery - every change visible."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.kinds import MarketState
from goldscalp.data.hub import MarketHub, ProviderOption
from goldscalp.data.live import MarketDataService
from goldscalp.data.providers.base import Freshness, ProviderInfo, RawQuote
from tests.conftest import OPEN_TIME


class FakeProvider:
    def __init__(self, pid: str, freshness: Freshness = Freshness.STREAMING_REALTIME, healthy: bool = True) -> None:
        self.info = ProviderInfo(id=pid, name=pid.upper(), homepage="", docs_url="", terms_note="", attribution=None,
                                 freshness=freshness, refresh_seconds=30, documented_delay_seconds=None,
                                 supports_bid_ask=True, supports_candles=False, transport="polling",
                                 source_symbol="XAUUSD", price_kind="test", needs_api_key=False)
        self.healthy = healthy

    async def fetch_quote(self, client):
        now = OPEN_TIME
        return RawQuote(bid=D("4000"), ask=D("4000.3"), last=None, quote_time=now, received_at=now)

    def probe(self):
        return self.healthy, "probe"


def make_hub(db, spec, *, options, selected="auto", mode="manual", clock=None, healthy=True):
    clock = clock or FixedClock(OPEN_TIME)
    built: list[MarketDataService] = []

    def build(pid, opts):
        provider = None if pid == "none" else FakeProvider(pid, healthy=healthy)
        svc = MarketDataService(provider=provider, db=db, clock=clock, schedule=spec.schedule, symbol="XAUUSD",
                                price_decimals=5, live_window=10, disconnect_after=180)
        built.append(svc)
        return svc
    hub = MarketHub(clock=clock, options=options, build=build, selected=selected, fallback_mode=mode,
                    fallback_after=30, recover_every=60)
    return hub, clock, built


ALL = [ProviderOption("mt5", "MetaTrader 5", True, None), ProviderOption("tiingo", "Tiingo FX", False, "no key"),
       ProviderOption("gold-api", "gold-api.com", True, None), ProviderOption("none", "None", True, None)]


def test_auto_selection_uses_priority_and_skips_unavailable(db, spec):
    hub, _, _ = make_hub(db, spec, options=ALL)
    assert hub.active == "mt5"
    no_mt5 = [ProviderOption("mt5", "MetaTrader 5", False, "package missing")] + ALL[1:]
    hub2, _, _ = make_hub(db, spec, options=no_mt5)
    assert hub2.active == "gold-api"
    assert hub2.events[0]["kind"] == "startup" and "priority" in hub2.events[0]["reason"]


def test_user_switch_is_recorded_and_visible(db, spec):
    hub, _, _ = make_hub(db, spec, options=ALL)
    asyncio.run(hub.select("gold-api"))
    assert hub.active == "gold-api" and hub.preferred == "gold-api"
    change = hub.snapshot()["latest_source_change"]
    assert change["kind"] == "user" and change["from"] == "mt5" and change["to"] == "gold-api"
    assert hub.series.key == "live:gold-api:XAUUSD"


def test_unavailable_provider_cannot_be_selected(db, spec):
    hub, _, _ = make_hub(db, spec, options=ALL)
    with pytest.raises(ValueError, match="not available: no key"):
        asyncio.run(hub.select("tiingo"))


def test_manual_mode_never_switches_on_its_own(db, spec):
    hub, clock, built = make_hub(db, spec, options=ALL, mode="manual")
    built[0].status.link_down_reason = "terminal not running"
    for _ in range(5):
        clock.advance(seconds=60)
        asyncio.run(hub.supervise_once())
    assert hub.active == "mt5" and hub.state()[0] is MarketState.DISCONNECTED
    assert [e["kind"] for e in hub.events] == ["startup"]


def test_auto_fallback_then_recovery(db, spec):
    hub, clock, built = make_hub(db, spec, options=ALL, mode="auto")
    built[0].status.link_down_reason = "terminal not running"
    asyncio.run(hub.supervise_once())                      # down, but not for long enough
    assert hub.active == "mt5"
    clock.advance(seconds=31)
    asyncio.run(hub.supervise_once())
    assert hub.active == "gold-api"
    fallback = hub.snapshot()["latest_source_change"]
    assert fallback["kind"] == "fallback" and "MetaTrader 5 disconnected" in fallback["reason"]
    assert hub.preferred == "mt5"                           # still the preference
    clock.advance(seconds=61)
    asyncio.run(hub.supervise_once())                      # probe of MT5 succeeds -> back to MT5
    assert hub.active == "mt5"
    assert [e["kind"] for e in hub.events] == ["startup", "fallback", "recovered"]


def test_auto_fallback_stays_when_preferred_is_still_unhealthy(db, spec):
    hub, clock, built = make_hub(db, spec, options=ALL, mode="auto", healthy=False)
    built[0].status.link_down_reason = "terminal not running"
    clock.advance(seconds=31)
    asyncio.run(hub.supervise_once())
    clock.advance(seconds=61)
    asyncio.run(hub.supervise_once())
    assert hub.active == "gold-api"


def test_delayed_fallback_price_is_never_live(db, spec):
    """gold-api.com (periodic snapshots) must show DELAYED even right after a switch."""
    from goldscalp.data.providers import gold_api
    clock = FixedClock(OPEN_TIME)
    svc = MarketDataService(provider=gold_api.GoldApiProvider(), db=db, clock=clock, schedule=spec.schedule,
                            symbol="XAUUSD", price_decimals=5, live_window=10, disconnect_after=180)
    svc.latest = RawQuote(bid=None, ask=None, last=D("4000"), quote_time=OPEN_TIME, received_at=OPEN_TIME)
    svc.status.last_success_at = OPEN_TIME
    assert svc.state()[0] is MarketState.DELAYED


def test_no_provider_shows_historical_with_history(db, spec, clock):
    hub, _, built = make_hub(db, spec, options=ALL, selected="none")
    assert hub.active == "none"
    assert hub.state()[0] in (MarketState.DISCONNECTED, MarketState.HISTORICAL)
    built[0]._history_cache = (10**12, True)
    assert hub.state()[0] is MarketState.HISTORICAL
    _ = timedelta
