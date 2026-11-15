"""Mode A: current market data.

MarketDataService runs ONE read-only provider:
- polling providers: gold-api.com (documented ~30 s cadence) and MetaTrader 5 (local terminal,
  ~1 s cadence, plus native candle refresh);
- streaming providers: Tiingo (WebSocket with reconnect/backoff, REST candles under a request
  budget, bandwidth guard).
It classifies the data as LIVE / DELAYED / HISTORICAL / DISCONNECTED, stores candles, and notifies
listeners (the paper-trading engine) on new prices. The provider HUB (data/hub.py) chooses which
provider runs and records every change of source.

It never substitutes an old price for a current one: every quote keeps its provider timestamp,
and the state says how fresh it is.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import pandas as pd

from goldscalp.core.clock import Clock, iso
from goldscalp.core.kinds import MarketState
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data.ingestion import store_live_candles
from goldscalp.data.providers.base import Freshness, ProviderError, ProviderInfo, RateLimited, RawQuote
from goldscalp.simulation.decimal_utils import dstr
from goldscalp.simulation.pricing import MarketQuote
from goldscalp.storage.candle_repo import SeriesInfo
from goldscalp.storage.db import Database

log = logging.getLogger("goldscalp.live")

STREAM_MAX_BACKOFF_SECONDS = 60
FALLBACK_POLL_SECONDS = 120
FLUSH_INTERVAL_SECONDS = 1.0
REFRESH_EVERY = {Timeframe.M1: timedelta(minutes=5), Timeframe.M5: timedelta(minutes=30),
                 Timeframe.M15: timedelta(minutes=30), Timeframe.H1: timedelta(minutes=30)}


class StreamPause(Exception):
    """Internal signal: leave the stream (shutdown, market closed or bandwidth budget reached)."""


@dataclass
class ConnectionStatus:
    enabled: bool
    transport: str | None
    requests_made: int = 0
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error_kind: str | None = None
    last_error_message: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None
    reconnects: int = 0              # successful recoveries after a failure
    reconnect_attempts: int = 0      # attempts made while the link was down
    link_down_reason: str | None = None
    stream_connected: bool = False
    note: str | None = None


class RequestBudget:
    """Charge BEFORE sending; refuse when a window is full (provider limits, with margin)."""

    def __init__(self, per_hour: int, per_day: int, clock: Callable[[], float] = _time.monotonic) -> None:
        self.per_hour, self.per_day = per_hour, per_day
        self.clock = clock
        self.calls: deque[float] = deque()

    def _trim(self) -> None:
        now = self.clock()
        while self.calls and now - self.calls[0] > 86400:
            self.calls.popleft()

    def used(self) -> dict[str, int]:
        self._trim()
        now = self.clock()
        return {"last_hour": sum(1 for t in self.calls if now - t <= 3600), "last_day": len(self.calls),
                "per_hour": self.per_hour, "per_day": self.per_day}

    def try_reserve(self) -> bool:
        used = self.used()
        if used["last_hour"] >= self.per_hour or used["last_day"] >= self.per_day:
            return False
        self.calls.append(self.clock())
        return True


def classify(*, info: ProviderInfo | None, enabled: bool, quote: RawQuote | None, status: ConnectionStatus,
             market: dict[str, Any], now: datetime, live_window: int, disconnect_after: int,
             has_history: bool) -> tuple[MarketState, str]:
    """The market-data state and a plain-language reason. Pure and tested."""
    if not enabled or info is None:
        if has_history:
            return MarketState.HISTORICAL, "no live provider is active; showing stored historical candles"
        return MarketState.DISCONNECTED, "no live data provider is configured"
    if status.link_down_reason:
        tail = f"; last known price from {iso(quote.quote_time)} is STALE" if quote is not None else ""
        return MarketState.DISCONNECTED, f"{info.name}: {status.link_down_reason}{tail}"
    if quote is None:
        if not market["open"]:
            return MarketState.HISTORICAL, "market closed and no price received yet"
        if status.last_error_message:
            return MarketState.DISCONNECTED, f"cannot get prices from {info.name}: {status.last_error_message}"
        return MarketState.DISCONNECTED, f"connecting to {info.name}"
    contact = status.last_message_at or status.last_success_at
    since_contact = (now - contact).total_seconds() if contact else float("inf")
    if since_contact > disconnect_after:
        detail = f" ({status.last_error_message})" if status.last_error_message else ""
        return (MarketState.DISCONNECTED,
                f"no contact with {info.name} for {int(since_contact)} s{detail}; "
                f"last price shown is from {iso(quote.quote_time)} and is STALE")
    age = int((now - quote.quote_time).total_seconds())
    fresh_stream = info.freshness is Freshness.STREAMING_REALTIME and age <= live_window
    if not market["open"] and not fresh_stream:
        why = "weekly close" if market["reason"] == "weekly_close" else "daily break"
        return MarketState.HISTORICAL, f"market closed ({why}); showing the last price from {iso(quote.quote_time)}"
    if info.freshness is Freshness.STREAMING_REALTIME:
        if fresh_stream:
            return MarketState.LIVE, f"real-time prices from {info.name} (latest {age} s old)"
        return MarketState.DELAYED, (f"no new price from {info.name} for {age} s (freshness threshold "
                                     f"{live_window} s)")
    if info.freshness is Freshness.PERIODIC_SNAPSHOT:
        return (MarketState.DELAYED,
                f"{info.name} publishes indicative snapshots about every {info.refresh_seconds} s; this price is "
                f"{age} s old. Not a real-time stream.")
    if info.freshness is Freshness.DELAYED:
        minutes = (info.documented_delay_seconds or 0) // 60
        return MarketState.DELAYED, f"{info.name} documents a delay of about {minutes} min"
    return MarketState.DELAYED, f"{info.name} does not document whether prices are real-time"


class TickCandleBuilder:
    """M1 candles built from ticks or snapshots (mid price, or the single price).

    From sparse snapshots (gold-api, ~30 s) highs/lows are APPROXIMATE. Tick
    volume is reported as the number of price updates GoldScalp received.
    """

    def __init__(self) -> None:
        self.bar: dict[str, Any] | None = None

    def add(self, at: datetime, price: float) -> list[dict[str, Any]]:
        start = pd.Timestamp(Timeframe.M1.floor(int(at.timestamp())), unit="s", tz="UTC")
        out: list[dict[str, Any]] = []
        if self.bar is not None and self.bar["ts"] != start:
            if start < self.bar["ts"]:
                return out  # an older price never rewrites a newer bar
            self.bar["closed"] = True
            out.append(dict(self.bar))
            self.bar = None
        if self.bar is None:
            self.bar = {"ts": start, "open": price, "high": price, "low": price, "close": price,
                        "tick_volume": 1, "spread": float("nan"), "closed": False}
        else:
            self.bar["high"] = max(self.bar["high"], price)
            self.bar["low"] = min(self.bar["low"], price)
            self.bar["close"] = price
            self.bar["tick_volume"] += 1
        out.append(dict(self.bar))
        return out

    def close_if_due(self, now: datetime) -> list[dict[str, Any]]:
        if self.bar is not None and pd.Timestamp(now) >= self.bar["ts"] + pd.Timedelta(minutes=1):
            self.bar["closed"] = True
            done = dict(self.bar)
            self.bar = None
            return [done]
        return []


class MarketDataService:
    def __init__(self, *, provider: Any | None, db: Database, clock: Clock, schedule: TradingSchedule,
                 symbol: str, price_decimals: int, live_window: int, disconnect_after: int,
                 budget: RequestBudget | None = None, monthly_stream_mb: int | None = None) -> None:
        self.provider = provider
        self.info: ProviderInfo | None = provider.info if provider else None
        self.db = db
        self.clock = clock
        self.schedule = schedule
        self.symbol = symbol
        self.default_decimals = price_decimals
        self.live_window = live_window
        self.disconnect_after = disconnect_after
        self.budget = budget
        self.monthly_stream_bytes = monthly_stream_mb * 1024 * 1024 if monthly_stream_mb else None
        self.status = ConnectionStatus(enabled=provider is not None,
                                       transport=self.info.transport if self.info else None)
        self.latest: RawQuote | None = None
        self.listeners: list[Callable[[], Any]] = []
        self.builder = TickCandleBuilder()
        self.candle_status: dict[str, Any] = {}
        self.last_candle_error: str | None = None
        self._series: SeriesInfo | None = None
        self._pending_rows: list[dict[str, Any]] = []
        self._last_flush = 0.0
        self._history_cache: tuple[float, bool] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------- series

    @property
    def series(self) -> SeriesInfo | None:
        """The live series for the CURRENT symbol (MT5 resolves its symbol at run time)."""
        if self.info is None:
            return None
        symbol = getattr(self.provider, "resolved_symbol", None) or (
            None if self.info.source_symbol == "auto" else self.info.source_symbol)
        if not symbol:
            return None
        decimals = getattr(self.provider, "price_decimals", None) or self.default_decimals
        key = f"live:{self.info.id}:{symbol}"
        if self._series is None or self._series.key != key or self._series.price_decimals != decimals:
            built = ("ticks from the stream" if self.info.build_candles_from_ticks else "the provider's own candles")
            if not self.info.supports_candles:
                built = f"price snapshots ~{self.info.refresh_seconds} s apart (highs/lows approximate)"
            self._series = SeriesInfo(
                key=key, symbol=self.symbol, kind="live", source_id=self.info.id, source_symbol=symbol,
                candle_side=self.info.candle_side, price_decimals=decimals,
                description=f"Candles for {self.symbol} ({symbol}) from {self.info.name}; recent M1 bars from {built}.")
        return self._series

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.provider is None or self._tasks:
            return
        self._close_stale_live_bars()
        self._stop.clear()
        if self.info and self.info.transport == "websocket":
            self._tasks.append(asyncio.create_task(self._stream_loop(), name="goldscalp-stream"))
        else:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="goldscalp-poll"))
        if self.info and self.info.supports_candles:
            self._tasks.append(asyncio.create_task(self._candle_loop(), name="goldscalp-candles"))

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
        self._tasks = []
        await asyncio.to_thread(self._flush, True)
        close = getattr(self.provider, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                log.exception("provider close failed")

    async def reconnect(self) -> None:
        """Manual reconnect: drop the provider connection and retry immediately."""
        reset = getattr(self.provider, "reset", None)
        if reset is not None:
            reset()
        self.status.note = "manual reconnect requested"
        self.status.next_attempt_at = self.clock.now()
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        self.status.next_attempt_at = self.clock.now() + timedelta(seconds=seconds)
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass
        if not self._stop.is_set():
            self._wake.clear()

    @staticmethod
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)

    # --------------------------------------------------------------- polling

    async def _poll_loop(self) -> None:
        async with self._client() as client:
            while not self._stop.is_set():
                await self._sleep(await self.poll_once(client))

    async def poll_once(self, client: httpx.AsyncClient) -> float:
        """One quote request. Returns seconds to wait before the next one."""
        assert self.provider is not None and self.info is not None
        if self.budget is not None and not self.budget.try_reserve():
            self.status.note = "request budget reached; waiting before the next request"
            return 300.0
        now = self.clock.now()
        self.status.last_attempt_at = now
        self.status.requests_made += 1
        if self.status.link_down_reason:
            self.status.reconnect_attempts += 1
        try:
            quote = await self.provider.fetch_quote(client)
        except ProviderError as exc:
            return self._failed(exc, now)
        except Exception as exc:  # noqa: BLE001 - never crash the loop
            log.exception("unexpected market-data failure")
            return self._failed(ProviderError(f"unexpected {type(exc).__name__}"), now)
        self._succeeded(now)
        await self._accept(quote, flush_now=True)
        return self._next_poll_delay(quote)

    def _next_poll_delay(self, quote: RawQuote) -> float:
        assert self.info is not None
        now = self.clock.now()
        if self.info.poll_seconds:
            fresh = (now - quote.quote_time).total_seconds() <= self.live_window
            if not self.schedule.is_open(now) and not fresh:
                return float(self.info.closed_market_poll_seconds)
            return float(self.info.poll_seconds)
        if not self.schedule.is_open(now):
            return float(self.info.closed_market_poll_seconds)
        cadence = self.info.refresh_seconds or FALLBACK_POLL_SECONDS
        if quote.next_update_seconds is not None:
            delay = quote.next_update_seconds + 2   # just after the provider's next update...
            while delay < cadence:                  # ...but never sooner than its documented cadence
                delay += cadence
            return float(delay)
        return float(cadence)

    # ------------------------------------------------------------- streaming

    async def _stream_loop(self) -> None:
        assert self.provider is not None
        failures = 0
        async with self._client() as client:
            while not self._stop.is_set():
                now = self.clock.now()
                market = self.schedule.status(now)
                if not market["open"]:
                    self.status.stream_connected = False
                    self.status.note = "market closed; stream paused until the next open"
                    wait = max(5.0, min((market["next_change"] - now).total_seconds() + 5, 900.0))
                    await self._sleep(wait)
                    continue
                if self._over_bandwidth():
                    self.status.note = ("monthly streaming bandwidth budget reached; falling back to REST "
                                        f"snapshots every {FALLBACK_POLL_SECONDS} s")
                    await self.poll_once(client)
                    await self._sleep(FALLBACK_POLL_SECONDS)
                    continue
                self.status.last_attempt_at = now
                try:
                    if failures:
                        self.status.reconnect_attempts += 1
                    await self.provider.stream(self._on_stream_quote, self._on_stream_event)
                    failures = 0
                except StreamPause:
                    self.status.stream_connected = False
                    continue
                except ProviderError as exc:
                    failures += 1
                    self.status.stream_connected = False
                    delay = self._failed(exc, self.clock.now(), streaming=True, attempt=failures)
                    await self._sleep(delay)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    self.status.stream_connected = False
                    log.exception("unexpected stream failure")
                    await self._sleep(self._failed(ProviderError(f"unexpected {type(exc).__name__}"),
                                                   self.clock.now(), streaming=True, attempt=failures))

    async def _on_stream_quote(self, quote: RawQuote) -> None:
        now = self.clock.now()
        self.status.last_message_at = now
        self._succeeded(now)
        await self._accept(quote, flush_now=False)
        if self._stop.is_set() or not self.schedule.is_open(now) or self._over_bandwidth():
            raise StreamPause()

    def _on_stream_event(self, kind: str, detail: str) -> None:
        self.status.last_message_at = self.clock.now()
        if kind in ("connected", "subscribed"):
            self.status.stream_connected = True
            self.status.note = detail

    def _over_bandwidth(self) -> bool:
        if self.monthly_stream_bytes is None or self.provider is None:
            return False
        return getattr(self.provider, "bytes_received", 0) >= self.monthly_stream_bytes

    # --------------------------------------------------------------- candles

    def _refresh_every(self) -> dict[Timeframe, timedelta]:
        assert self.info is not None
        if self.info.candle_refresh_seconds:
            return {Timeframe.parse(tf): timedelta(seconds=s) for tf, s in self.info.candle_refresh_seconds}
        return REFRESH_EVERY

    async def _candle_loop(self) -> None:
        assert self.provider is not None
        last: dict[Timeframe, datetime] = {}
        done_first: set[Timeframe] = set()
        basis_seen = getattr(self.provider, "basis_version", 0)
        refresh = self._refresh_every()
        tick = min(60.0, min(d.total_seconds() for d in refresh.values()))
        async with self._client() as client:
            while not self._stop.is_set():
                version = getattr(self.provider, "basis_version", 0)
                if version != basis_seen:           # MT5 time basis changed: re-backfill everything
                    basis_seen, done_first, last = version, set(), {}
                now = self.clock.now()
                for tf in ALL_TIMEFRAMES:
                    due = tf not in last or now - last[tf] >= refresh[tf]
                    if not due or (tf in done_first and not self.schedule.is_open(now)):
                        continue
                    if self.budget is not None and not self.budget.try_reserve():
                        self.candle_status[tf.value] = {"status": "skipped: request budget reached", "at": iso(now)}
                        continue
                    last[tf] = now
                    if await self._refresh_candles(client, tf, now, first=tf not in done_first):
                        done_first.add(tf)
                await self._sleep_candles(tick)

    async def _sleep_candles(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _refresh_candles(self, client: httpx.AsyncClient, tf: Timeframe, now: datetime, *, first: bool) -> bool:
        assert self.provider is not None
        self.status.requests_made += 1
        try:
            frame = await self.provider.fetch_candles(client, tf, first=first, now=now)
        except ProviderError as exc:
            self.candle_status[tf.value] = {"status": f"failed: {exc}", "at": iso(now)}
            return False
        except Exception as exc:  # noqa: BLE001
            self.candle_status[tf.value] = {"status": f"failed: {type(exc).__name__}", "at": iso(now)}
            return False
        series = self.series
        if series is None:
            self.candle_status[tf.value] = {"status": "waiting for the symbol to resolve", "at": iso(now)}
            return False
        if not frame.empty:
            forming = frame["ts"] + pd.Timedelta(seconds=tf.seconds) > pd.Timestamp(now)
            frame.loc[forming, "closed"] = False
        try:
            result = await asyncio.to_thread(store_live_candles, self.db, self.clock, series, tf, frame)
            self.candle_status[tf.value] = {"status": "ok", "at": iso(now), "backfill": first, **result}
            self._history_cache = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.candle_status[tf.value] = {"status": f"store failed: {type(exc).__name__}", "at": iso(now)}
            return False

    # ---------------------------------------------------------------- shared

    def _succeeded(self, now: datetime) -> None:
        if self.status.consecutive_failures and self.info and self.info.transport != "websocket":
            self.status.reconnects += 1
        self.status.last_success_at = now
        self.status.consecutive_failures = 0
        self.status.link_down_reason = None

    def _failed(self, exc: ProviderError, now: datetime, *, streaming: bool = False, attempt: int = 1) -> float:
        self.status.last_error_kind = exc.kind
        self.status.last_error_message = str(exc)
        self.status.last_error_at = now
        self.status.consecutive_failures += 1
        if getattr(exc, "link_down", False):
            self.status.link_down_reason = str(exc)
        info = self.info
        if isinstance(exc, RateLimited):
            return float(min(max(exc.retry_after or 300.0, 60.0), 3600.0))
        if exc.permanent:
            return float(info.permanent_backoff_seconds if info else 900)
        if streaming:
            base = min(2 ** (attempt - 1), STREAM_MAX_BACKOFF_SECONDS)
            return float(base) * (0.8 + 0.4 * random.random())
        cadence = (info.poll_seconds or info.refresh_seconds or 30) if info else 30
        cap = info.max_backoff_seconds if info else 300
        return float(min(cadence * (2 ** (self.status.consecutive_failures - 1)), cap))

    async def _accept(self, quote: RawQuote, *, flush_now: bool) -> None:
        if self.latest is not None and quote.quote_time < self.latest.quote_time:
            return  # never let an older price replace a newer one
        is_new = self.latest is None or quote.quote_time > self.latest.quote_time
        self.latest = quote
        if not is_new:
            return
        if self.info is not None and self.info.build_candles_from_ticks:
            price = quote.last if quote.last is not None else (
                (quote.bid + quote.ask) / 2 if quote.bid is not None and quote.ask is not None else None)
            if price is not None and self.schedule.is_open(quote.quote_time):
                self._pending_rows.extend(self.builder.add(quote.quote_time, float(price)))
        due = flush_now or _time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS
        if due:
            await asyncio.to_thread(self._flush, False)
            for listener in list(self.listeners):
                try:
                    await asyncio.to_thread(listener)
                except Exception:  # noqa: BLE001 - a listener must not stop market data
                    log.exception("market-data listener failed")

    def _flush(self, final: bool) -> None:
        self._last_flush = _time.monotonic()
        rows = self._pending_rows
        self._pending_rows = []
        rows.extend(self.builder.close_if_due(self.clock.now()))
        if final and self.builder.bar is not None:
            rows.append(dict(self.builder.bar))
        series = self.series
        if not rows or series is None:
            return
        latest: dict[Any, dict[str, Any]] = {}
        for row in rows:
            latest[row["ts"]] = row  # last state of each bar wins
        frame = pd.DataFrame(list(latest.values()))
        try:
            store_live_candles(self.db, self.clock, series, Timeframe.M1, frame)
            self.last_candle_error = None
        except Exception as exc:  # noqa: BLE001 - candles must not stop prices
            self.last_candle_error = f"{type(exc).__name__}: {exc}"
            log.exception("could not store live candle")

    def _close_stale_live_bars(self) -> None:
        if self.info is None:
            return
        cutoff = Timeframe.M1.floor(int(self.clock.now().timestamp()))
        with self.db.transaction() as con:
            con.execute("UPDATE candles SET closed = 1 WHERE closed = 0 AND ts < ? AND series_id IN "
                        "(SELECT id FROM series WHERE kind = 'live' AND source_id = ?)", (cutoff, self.info.id))

    # ---------------------------------------------------------------- views

    def has_history(self) -> bool:
        now = _time.monotonic()
        if self._history_cache and now - self._history_cache[0] < 30:
            return self._history_cache[1]
        with self.db.read() as con:
            value = con.execute("SELECT 1 FROM candles LIMIT 1").fetchone() is not None
        self._history_cache = (now, value)
        return value

    def state(self) -> tuple[MarketState, str]:
        now = self.clock.now()
        return classify(info=self.info, enabled=self.provider is not None, quote=self.latest, status=self.status,
                        market=self.schedule.status(now), now=now, live_window=self.live_window,
                        disconnect_after=self.disconnect_after, has_history=self.has_history())

    def market_quote(self) -> MarketQuote | None:
        """The latest quote, labelled with its current state (for the simulator)."""
        if self.latest is None or self.info is None:
            return None
        state, reason = self.state()
        q = self.latest
        return MarketQuote(symbol=self.symbol, bid=q.bid, ask=q.ask, last=q.last, quote_time=q.quote_time,
                           received_at=q.received_at, source_id=self.info.id, state=state, state_reason=reason)

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        state, reason = self.state()
        q = self.latest
        quote = None
        if q is not None:
            spread = (q.ask - q.bid) if q.bid is not None and q.ask is not None else None
            mid = (q.bid + q.ask) / 2 if q.bid is not None and q.ask is not None else None
            quote = {"bid": dstr(q.bid), "ask": dstr(q.ask), "last": dstr(q.last), "mid": dstr(mid),
                     "spread": dstr(spread), "spread_source": "provider" if spread is not None else None,
                     "quote_time": iso(q.quote_time), "received_at": iso(q.received_at),
                     "age_seconds": round((now - q.quote_time).total_seconds(), 1),
                     "stale": state is not MarketState.LIVE}
        s = self.status
        market = self.schedule.status(now)
        bandwidth = None
        if self.monthly_stream_bytes is not None and self.provider is not None:
            bandwidth = {"bytes_received_this_process": getattr(self.provider, "bytes_received", 0),
                         "budget_bytes": self.monthly_stream_bytes}
        details = None
        if self.provider is not None and hasattr(self.provider, "details"):
            details = self.provider.details()
        series = self.series
        return {
            "state": state.value, "reason": reason, "quote": quote, "stale_after_seconds": self.live_window,
            "market_hours": {"open": market["open"], "reason": market["reason"],
                             "next_change": iso(market["next_change"])},
            "provider": None if self.info is None else {
                "id": self.info.id, "name": self.info.name, "homepage": self.info.homepage,
                "docs_url": self.info.docs_url, "attribution": self.info.attribution,
                "terms_note": self.info.terms_note, "freshness": self.info.freshness.value,
                "refresh_seconds": self.info.refresh_seconds, "poll_seconds": self.info.poll_seconds,
                "supports_bid_ask": self.info.supports_bid_ask, "supports_candles": self.info.supports_candles,
                "transport": self.info.transport, "price_kind": self.info.price_kind,
                "source_symbol": series.source_symbol if series else self.info.source_symbol,
                "candle_side": self.info.candle_side},
            "provider_details": details,
            "connection": {
                "enabled": s.enabled, "transport": s.transport, "stream_connected": s.stream_connected,
                "requests_made": s.requests_made, "last_attempt_at": iso(s.last_attempt_at),
                "last_success_at": iso(s.last_success_at), "last_message_at": iso(s.last_message_at),
                "last_error_kind": s.last_error_kind, "last_error_message": s.last_error_message,
                "last_error_at": iso(s.last_error_at), "consecutive_failures": s.consecutive_failures,
                "next_attempt_at": iso(s.next_attempt_at), "reconnects": s.reconnects,
                "reconnect_attempts": s.reconnect_attempts, "link_down_reason": s.link_down_reason,
                "note": s.note},
            "request_budget": self.budget.used() if self.budget else None,
            "bandwidth": bandwidth,
            "candles": self.candle_status,
            "live_series": series.key if series else None,
            "candle_error": self.last_candle_error,
            "checked_at": iso(now),
        }
