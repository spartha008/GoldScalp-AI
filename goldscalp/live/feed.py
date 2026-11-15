"""Feed health (PHASE4_ARCHITECTURE.md section 3.5): spread and tick-age statistics, market-state transitions
and one stored sample per minute. Called as a market-hub listener (after accepted quotes) and from the live
monitor's cycle (so transitions to STALE / DISCONNECTED are seen even when no quote arrives)."""
from __future__ import annotations

import statistics
import threading
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from goldscalp.core.clock import Clock, iso
from goldscalp.core.kinds import MarketState
from goldscalp.storage import live_repo
from goldscalp.storage.db import Database

WINDOW = timedelta(hours=1)
SAMPLE_EVERY = timedelta(seconds=60)


def _q(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 5)


class FeedMonitor:
    def __init__(self, db: Database, clock: Clock, market: Any, *, window: timedelta = WINDOW) -> None:
        self.db = db
        self.clock = clock
        self.market = market
        self.window = window
        self._spreads: deque[tuple[datetime, float]] = deque()
        self._last_quote_time: datetime | None = None
        self._last_sample: datetime | None = None
        self._state: str | None = None
        self._lock = threading.Lock()

    def _provider(self) -> str:
        return str(getattr(self.market, "active", None) or "none")

    def on_quote(self) -> None:
        """Hub listener: record the latest quote's spread once per new quote time."""
        self.tick(self.clock.now())

    def tick(self, now: datetime) -> None:
        quote = self.market.market_quote()
        state, reason = self.market.state()
        with self._lock:
            if quote is not None and quote.bid is not None and quote.ask is not None \
                    and quote.quote_time != self._last_quote_time and state is MarketState.LIVE:
                self._last_quote_time = quote.quote_time
                self._spreads.append((quote.quote_time, float(quote.ask - quote.bid)))
            while self._spreads and self._spreads[0][0] < now - self.window:
                self._spreads.popleft()
            transition = state.value != self._state
            old, self._state = self._state, state.value
            sample = self._last_sample is None or now - self._last_sample >= SAMPLE_EVERY
            if sample:
                self._last_sample = now
        if transition:
            with self.db.transaction() as con:
                live_repo.insert_feed_event(con, now, self._provider(), old, state.value, reason)
        if sample:
            spread = None
            if quote is not None and quote.bid is not None and quote.ask is not None:
                spread = str(quote.ask - quote.bid)
            with self.db.transaction() as con:
                live_repo.insert_feed_sample(con, {
                    "ts": iso(now.replace(microsecond=0)), "provider": self._provider(), "state": state.value,
                    "bid": None if quote is None or quote.bid is None else str(quote.bid),
                    "ask": None if quote is None or quote.ask is None else str(quote.ask), "spread": spread,
                    "quote_time": None if quote is None else iso(quote.quote_time),
                    "tick_age_seconds": None if quote is None else round((now - quote.quote_time).total_seconds(), 1)})

    def stats(self) -> dict[str, Any]:
        with self._lock:
            values = [s for _, s in self._spreads]
        return {"window_minutes": int(self.window.total_seconds() // 60), "quotes": len(values),
                "median": round(statistics.median(values), 5) if values else None, "p95": _q(values, 0.95),
                "max": round(max(values), 5) if values else None, "min": round(min(values), 5) if values else None,
                "state": self._state}

    def spread_context(self, spread: Decimal | float | None) -> dict[str, Any]:
        """How a spread compares with the last hour of LIVE quotes."""
        st = self.stats()
        if spread is None:
            return {**st, "spread": None, "vs_median": None, "above_p95": None}
        s = float(spread)
        return {**st, "spread": s, "vs_median": round(s / st["median"], 2) if st["median"] else None,
                "above_p95": None if st["p95"] is None else s > st["p95"]}
