"""Provider hub: chooses which market-data provider runs, and never switches silently.

Priority (when available): 1 MetaTrader 5 terminal, 2 Tiingo (if a key is set), 3 gold-api.com
(delayed), 4 stored historical candles only ("none").

- Start-up: the configured provider (``GOLDSCALP_LIVE_PROVIDER``) or, with ``auto``, the first
  available provider in priority order.
- Every change of source is recorded as an event (time, from, to, kind, reason) and shown on the
  dashboard: user selection, start-up choice, automatic fallback, automatic recovery.
- Fallback mode ``manual`` (default): a failing provider stays selected and shows DISCONNECTED;
  the user can reconnect or pick another provider. Mode ``auto``: after the active provider has been
  DISCONNECTED for ``fallback_after`` seconds the hub switches to the next available provider, and
  returns to the preferred provider once its probe succeeds - each switch visible as an event.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from goldscalp.core.clock import Clock, iso
from goldscalp.core.kinds import MarketState
from goldscalp.data.live import MarketDataService

log = logging.getLogger("goldscalp.hub")

PRIORITY = ("mt5", "tiingo", "gold-api")
NAMES = {"mt5": "MetaTrader 5", "tiingo": "Tiingo FX", "gold-api": "gold-api.com", "none": "None (stored history only)"}
SUPERVISE_SECONDS = 5.0


@dataclass(frozen=True)
class ProviderOption:
    id: str
    name: str
    available: bool
    reason: str | None


class MarketHub:
    def __init__(self, *, clock: Clock, options: list[ProviderOption],
                 build: Callable[[str, dict[str, Any]], MarketDataService], selected: str,
                 fallback_mode: str = "manual", fallback_after: int = 30, recover_every: int = 60) -> None:
        if fallback_mode not in ("manual", "auto"):
            raise ValueError("fallback mode must be 'manual' or 'auto'")
        self.clock = clock
        self.options = {o.id: o for o in options}
        self.build = build
        self.fallback_mode = fallback_mode
        self.fallback_after = fallback_after
        self.recover_every = recover_every
        self.preferred = self._initial(selected)
        self.active = self.preferred
        self.service: MarketDataService = build(self.active, {})
        self.listeners: list[Callable[[], Any]] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self._down_since: datetime | None = None
        self._last_probe: datetime | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        reason = (f"selected by configuration ({selected})" if selected != "auto"
                  else f"first available provider in priority order {' > '.join(PRIORITY)}")
        self._record("startup", None, self.active, reason)

    # ------------------------------------------------------------------ choice

    def _initial(self, selected: str) -> str:
        if selected != "auto":
            if selected not in self.options:
                raise ValueError(f"unknown provider {selected!r}")
            return selected
        for pid in PRIORITY:
            option = self.options.get(pid)
            if option and option.available:
                return pid
        return "none"

    def _record(self, kind: str, old: str | None, new: str, reason: str) -> None:
        self.events.append({"at": iso(self.clock.now()), "kind": kind, "from": old,
                            "from_name": NAMES.get(old, old) if old else None, "to": new,
                            "to_name": NAMES.get(new, new), "reason": reason})

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.service.listeners = list(self.listeners)
        await self.service.start()
        if self.fallback_mode == "auto" and self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="goldscalp-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.service.stop()

    async def select(self, provider_id: str, *, symbol: str | None = None, reason: str = "selected by the user",
                     kind: str = "user") -> None:
        if provider_id not in self.options:
            raise ValueError(f"unknown provider {provider_id!r}")
        option = self.options[provider_id]
        if not option.available:
            raise ValueError(f"{option.name} is not available: {option.reason}")
        async with self._lock:
            old = self.active
            await self.service.stop()
            self.service = self.build(provider_id, {"symbol": symbol} if symbol else {})
            self.service.listeners = list(self.listeners)
            self.active = provider_id
            if kind == "user":
                self.preferred = provider_id
            self._down_since = None
            detail = f" (symbol {symbol})" if symbol else ""
            self._record(kind, old, provider_id, reason + detail)
            await self.service.start()

    async def reconnect(self) -> None:
        await self.service.reconnect()

    # ------------------------------------------------------------------ automatic fallback

    def next_available(self, after: str) -> str:
        order = [p for p in PRIORITY if self.options.get(p) and self.options[p].available]
        if after in order:
            order = order[order.index(after) + 1:]
        return order[0] if order else "none"

    async def supervise_once(self) -> None:
        """One supervisor step (also called directly by tests). Manual mode never switches."""
        if self.fallback_mode != "auto":
            return
        now = self.clock.now()
        state, reason = self.service.state()
        if state is MarketState.DISCONNECTED and self.active != "none":
            self._down_since = self._down_since or now
            if (now - self._down_since).total_seconds() >= self.fallback_after:
                target = self.next_available(self.active)
                if target != self.active:
                    await self.select(target, reason=f"automatic fallback: {NAMES.get(self.active)} disconnected "
                                                     f"({reason})", kind="fallback")
            return
        self._down_since = None
        if self.active != self.preferred and self.preferred != "none":
            if self._last_probe is None or (now - self._last_probe).total_seconds() >= self.recover_every:
                self._last_probe = now
                ok, detail = await self._probe(self.preferred)
                if ok:
                    preferred = self.preferred
                    await self.select(preferred, reason=f"automatic recovery: {NAMES.get(preferred)} is healthy "
                                                        f"again ({detail})", kind="recovered")
                    self.preferred = preferred

    async def _probe(self, provider_id: str) -> tuple[bool, str]:
        candidate = self.build(provider_id, {})
        probe = getattr(candidate.provider, "probe", None)
        if probe is None:
            return False, "no health check available"
        return await asyncio.to_thread(probe)

    async def _supervise(self) -> None:
        while True:
            try:
                await self.supervise_once()
            except Exception:  # noqa: BLE001 - the supervisor must keep running
                log.exception("provider supervisor step failed")
            await asyncio.sleep(SUPERVISE_SECONDS)

    # ------------------------------------------------------------------ the service interface

    @property
    def series(self) -> Any:
        return self.service.series

    def market_quote(self) -> Any:
        return self.service.market_quote()

    def state(self) -> tuple[MarketState, str]:
        return self.service.state()

    def has_history(self) -> bool:
        return self.service.has_history()

    def providers_view(self) -> dict[str, Any]:
        return {"active": self.active, "active_name": NAMES.get(self.active, self.active),
                "preferred": self.preferred, "priority": list(PRIORITY), "fallback_mode": self.fallback_mode,
                "fallback_after_seconds": self.fallback_after,
                "options": [{"id": o.id, "name": o.name, "available": o.available, "reason": o.reason}
                            for o in self.options.values()],
                "events": list(self.events)[-10:]}

    def snapshot(self) -> dict[str, Any]:
        snap = self.service.snapshot()
        view = self.providers_view()
        snap["providers"] = view
        changes = [e for e in self.events if e["kind"] != "startup"]
        snap["latest_source_change"] = changes[-1] if changes else None
        return snap
