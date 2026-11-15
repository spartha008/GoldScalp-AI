"""MetaTrader 5 - READ-ONLY market data from your locally running, already logged-in MT5 terminal.

Official package: ``MetaTrader5`` (https://www.mql5.com/en/docs/python_metatrader5). Optional and
Windows-only (requirements-mt5.txt); it is imported lazily here and nowhere else, so GoldScalp runs
without it.

Read-only by construction
-------------------------
The package is wrapped in ``ReadOnlyMT5``, an allowlist proxy exposing ONLY:
initialize, shutdown, last_error, version, terminal_info, symbols_total, symbols_get, symbol_info,
symbol_select, symbol_info_tick, copy_rates_from_pos and the four TIMEFRAME constants. Any other
attribute (orders, positions, history, account or login functions) raises PermissionError.
This provider never trades. The ONE exception in GoldScalp is the opt-in demo-execution gateway
(goldscalp/execution/mt5_demo.py, off by default), which receives the package through
``call_on_mt5_thread`` and wraps it in its own DEMO-account-only allowlist.
``initialize()`` is called WITHOUT login, password or server: GoldScalp attaches to the terminal
you already logged in to and never handles MT5 credentials. ``symbol_select`` only adds a hidden
symbol to Market Watch so the terminal streams its ticks (configurable).

Timestamps
----------
MT5 reports tick and bar times as the broker's TRADE-SERVER wall clock counted as if it were UTC
(often UTC+2, UTC+3 in US summer). GoldScalp converts them with a time basis: configured explicitly
(GOLDSCALP_MT5_TIME_BASIS = utc | fixed:+HH:MM | rule:ny_close | tz:<zone>) or detected ("auto")
from a FRESH tick against the system clock. A detected offset equal to New York + 7 h is mapped to
the DST-aware ``rule:ny_close``; anything else becomes a fixed offset.
"""
from __future__ import annotations

import asyncio
import importlib
import math
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import PurePath
from typing import Any
from collections.abc import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldscalp.core.clock import Clock, iso
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data.normalization import TimeBasis
from goldscalp.data.providers.base import Freshness, MalformedResponse, ProviderError, ProviderInfo, RawQuote

ALLOWED = frozenset({
    "initialize", "shutdown", "last_error", "version", "terminal_info", "symbols_total", "symbols_get",
    "symbol_info", "symbol_select", "symbol_info_tick", "copy_rates_from_pos",
    "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_H1",
})
TF_ATTR = {Timeframe.M1: "TIMEFRAME_M1", Timeframe.M5: "TIMEFRAME_M5", Timeframe.M15: "TIMEFRAME_M15",
           Timeframe.H1: "TIMEFRAME_H1"}
BACKFILL_BARS = {Timeframe.M1: 3000, Timeframe.M5: 3000, Timeframe.M15: 2000, Timeframe.H1: 2000}
REFRESH_BARS = {Timeframe.M1: 5, Timeframe.M5: 3, Timeframe.M15: 3, Timeframe.H1: 3}
DETECT_MAX_TICK_AGE_SECONDS = 120
FUTURE_TOLERANCE_SECONDS = 5
ASSUMED_BASIS = "rule:ny_close"
IPC_CODES = {-10000, -10001, -10002, -10003, -10004, -10005}
_GOLD_NAME = re.compile(r"^(XAUUSD|GOLD)([^A-Za-z0-9]?[A-Za-z0-9]{0,8})?$", re.IGNORECASE)
_NOT_TRADABLE_WORDS = ("not for trading", "conversion", "index", "indices")

# MetaTrader5 keeps ONE process-wide connection; every call goes through this single thread.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="goldscalp-mt5")


# ------------------------------------------------------------------ errors

class MT5Error(ProviderError):
    kind = "mt5_error"
    link_down = True


class PackageMissing(MT5Error):
    kind = "package_missing"
    permanent = True


class TerminalNotRunning(MT5Error):
    kind = "terminal_not_running"


class InitializeFailed(MT5Error):
    kind = "initialize_failed"


class BrokerDisconnected(MT5Error):
    kind = "broker_disconnected"


class SymbolNotFound(MT5Error):
    kind = "symbol_not_found"
    permanent = True


class NoTick(ProviderError):
    kind = "no_tick"


class InvalidTick(MalformedResponse):
    kind = "invalid_tick"


# ------------------------------------------------------------------ read-only proxy

class ReadOnlyMT5:
    """Exposes only market-data functions of the MetaTrader5 package."""

    __slots__ = ("_module",)

    def __init__(self, module: Any) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str) -> Any:
        if name not in ALLOWED:
            raise PermissionError(f"MetaTrader5.{name} is not available: GoldScalp's MT5 adapter is read-only "
                                  "market data")
        return getattr(object.__getattribute__(self, "_module"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise PermissionError("the read-only MT5 adapter cannot be modified")


def load_module() -> ReadOnlyMT5:
    try:
        module = importlib.import_module("MetaTrader5")
    except ImportError as exc:
        raise PackageMissing("the MetaTrader5 package is not installed (Windows only): "
                             "pip install -r requirements-mt5.txt") from exc
    return ReadOnlyMT5(module)


def terminal_running(image: str = "terminal64.exe") -> bool:
    """Is an MT5 terminal process running? (tasklist, fixed arguments, no shell)."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return True   # cannot check (non-Windows or restricted): let initialize() decide
    return image.lower() in out.stdout.lower()


# ------------------------------------------------------------------ symbols

def rank_gold_symbols(symbols: Any) -> list[dict[str, Any]]:
    """Gold-vs-USD candidates, best first. Handles XAUUSD, XAUUSDm, XAUUSD.r, GOLD, GOLD# ... and
    pushes down symbols the broker describes as not for trading (e.g. currency-conversion symbols)."""
    ranked = []
    for s in symbols or ():
        name = str(getattr(s, "name", ""))
        base = str(getattr(s, "currency_base", "") or "").upper()
        profit = str(getattr(s, "currency_profit", "") or "").upper()
        desc = str(getattr(s, "description", "") or "")
        match = _GOLD_NAME.match(name)
        xau_usd = base == "XAU" and profit == "USD"
        if not (xau_usd or (match and profit in ("USD", ""))):
            continue
        if profit and profit != "USD":
            continue
        score = 0
        if match:
            root, suffix = match.group(1).upper(), match.group(2)
            score += 50 if root == "XAUUSD" else 40
            if not suffix:
                score += 30 if root == "XAUUSD" else 20
        if xau_usd:
            score += 20
        if any(word in desc.lower() for word in _NOT_TRADABLE_WORDS):
            score -= 60
        if getattr(s, "visible", False):
            score += 5
        ranked.append({"name": name, "description": desc, "score": score, "visible": bool(getattr(s, "visible", False)),
                       "currency_base": base, "currency_profit": profit})
    ranked.sort(key=lambda r: (-r["score"], r["name"]))
    return ranked


def symbol_spec(info: Any) -> dict[str, Any]:
    return {"name": info.name, "description": getattr(info, "description", ""),
            "currency_base": getattr(info, "currency_base", None), "currency_profit": getattr(info, "currency_profit", None),
            "digits": int(info.digits), "point": float(info.point),
            "contract_size": float(getattr(info, "trade_contract_size", 0.0)),
            "volume_min": float(getattr(info, "volume_min", 0.0)), "volume_step": float(getattr(info, "volume_step", 0.0)),
            "volume_max": float(getattr(info, "volume_max", 0.0)), "visible": bool(getattr(info, "visible", False))}


# ------------------------------------------------------------------ time basis

def detect_offset(server_epoch: float, now: datetime) -> int | None:
    """Server-time offset in seconds (15-minute steps) from a FRESH tick, or None if the tick is too old."""
    delta = server_epoch - now.timestamp()
    offset = int(round(delta / 900.0)) * 900
    if abs(offset) > 14 * 3600:
        return None
    residual = delta - offset   # = -(tick age)
    if -DETECT_MAX_TICK_AGE_SECONDS <= residual <= FUTURE_TOLERANCE_SECONDS:
        return offset
    return None


def basis_for_offset(offset: int, now: datetime) -> str:
    if offset == 0:
        return "utc"
    ny = now.astimezone(ZoneInfo("America/New_York")).utcoffset()
    if ny is not None and offset == int(ny.total_seconds()) + 7 * 3600:
        return "rule:ny_close"
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset) // 60, 60)
    return f"fixed:{sign}{hours:02d}:{minutes:02d}"


def server_to_utc(epochs: Any, basis: TimeBasis, unit: str = "s") -> pd.Series:
    naive = pd.to_datetime(np.asarray(epochs, dtype="int64"), unit=unit)
    return basis.to_utc(pd.Series(naive))


# ------------------------------------------------------------------ provider

class MT5Provider:
    def __init__(self, *, clock: Clock, symbol: str | None = None, time_basis: str = "auto",
                 poll_seconds: float = 1.0, terminal_path: str | None = None, require_running: bool = True,
                 select_symbol: bool = True, init_timeout_ms: int = 10000,
                 module_loader: Callable[[], Any] = load_module,
                 process_check: Callable[[str], bool] = terminal_running,
                 backfill_bars: dict[Timeframe, int] | None = None) -> None:
        if time_basis != "auto":
            TimeBasis.parse(time_basis)   # refuse a typo at start-up
        self.clock = clock
        self.configured_symbol = (symbol or "").strip() or None
        self.time_basis_setting = time_basis
        self.terminal_path = (terminal_path or "").strip() or None
        self.require_running = require_running
        self.select_symbol = select_symbol
        self.init_timeout_ms = init_timeout_ms
        self.module_loader = module_loader
        self.process_check = process_check
        self.info = ProviderInfo(
            id="mt5", name="MetaTrader 5", homepage="https://www.metatrader5.com",
            docs_url="https://www.mql5.com/en/docs/python_metatrader5",
            terms_note=("Your broker's prices from your own logged-in MT5 terminal, read-only. Use of the data "
                        "is governed by your broker's terms."),
            attribution=None, freshness=Freshness.STREAMING_REALTIME, refresh_seconds=None,
            documented_delay_seconds=None, supports_bid_ask=True, supports_candles=True, transport="polling",
            source_symbol=self.configured_symbol or "auto", price_kind="broker bid/ask ticks from your MT5 terminal",
            needs_api_key=False, candle_side="bid", closed_market_poll_seconds=10, max_backoff_seconds=30,
            permanent_backoff_seconds=30, build_candles_from_ticks=False,
            candle_refresh_seconds=(("M1", 5), ("M5", 15), ("M15", 15), ("H1", 30)), poll_seconds=poll_seconds)
        self._mt5: Any = None
        self._initialized = False
        self.resolved_symbol: str | None = None
        self.symbol_source: str | None = None
        self.spec: dict[str, Any] | None = None
        self.candidates: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] = {}
        self.version: Any = None
        self.basis: TimeBasis | None = None if time_basis == "auto" else TimeBasis.parse(time_basis)
        self.basis_source = "configured" if time_basis != "auto" else None
        self.server_offset_seconds: int | None = None
        self.basis_version = 0
        self.price_decimals: int | None = None
        self.point: float | None = None
        self.last_init_error: str | None = None
        self._last_server_epoch: float | None = None
        self.backfill_bars = {**BACKFILL_BARS, **(backfill_bars or {})}
        self.backfill_notes: dict[str, str] = {}

    # -------------------------------------------------------------- async API

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, fn, *args)

    async def fetch_quote(self, client: Any = None) -> RawQuote:
        return await self._run(self._quote_sync)

    async def fetch_candles(self, client: Any, tf: Timeframe, *, first: bool, now: datetime) -> pd.DataFrame:
        return await self._run(self._candles_sync, tf, first, now)

    async def list_symbols(self, query: str = "") -> dict[str, Any]:
        return await self._run(self._symbols_sync, query)

    async def close(self) -> None:
        await self._run(self._shutdown_sync)

    def reset(self) -> None:
        """Force a fresh initialize() on the next request (manual reconnect)."""
        self._initialized = False

    def probe(self) -> tuple[bool, str]:
        """Health check used by automatic fallback recovery (runs on the MT5 thread)."""
        try:
            quote = _EXECUTOR.submit(self._quote_sync).result(timeout=self.init_timeout_ms / 1000 + 5)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        age = (self.clock.now() - quote.quote_time).total_seconds()
        return age <= 60, f"latest tick {int(age)} s old"

    # -------------------------------------------------------------- sync (MT5 thread)

    def _ensure_ready(self) -> Any:
        if self._mt5 is None:
            self._mt5 = self.module_loader()
        mt5 = self._mt5
        if not self._initialized:
            image = PurePath(self.terminal_path).name if self.terminal_path else "terminal64.exe"
            if self.require_running and not self.process_check(image):
                raise TerminalNotRunning(f"the MetaTrader 5 terminal ({image}) is not running: start it and log in "
                                         "to your demo account")
            kwargs: dict[str, Any] = {"timeout": self.init_timeout_ms}
            if self.terminal_path:
                kwargs["path"] = self.terminal_path
            if not mt5.initialize(**kwargs):   # no login/password/server: attach to the logged-in terminal
                code, desc = _last_error(mt5)
                try:
                    mt5.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                self.last_init_error = f"{code} {desc}"
                raise InitializeFailed(_init_hint(code, desc))
            self._initialized = True
            self.resolved_symbol = None
            self.version = mt5.version()
        terminal = mt5.terminal_info()
        if terminal is None:
            code, desc = _last_error(mt5)
            self._initialized = False
            raise InitializeFailed(f"lost the connection to the MT5 terminal ({code} {desc})")
        # Terminal facts only (no account fields): company identifies the broker (Phase 4), data_path locates
        # the terminal's own configuration for demo/live detection (mt5_identity.py).
        self.terminal = {"connected": bool(terminal.connected), "build": getattr(terminal, "build", None),
                         "broker": getattr(terminal, "company", None),
                         "trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
                         "name": getattr(terminal, "name", None), "path": getattr(terminal, "path", None),
                         "data_path": getattr(terminal, "data_path", None),
                         "ping_last_us": getattr(terminal, "ping_last", None)}
        if not terminal.connected:
            raise BrokerDisconnected("the MT5 terminal is running but NOT connected to its broker's server "
                                     "(check the terminal's login and internet connection)")
        if self.resolved_symbol is None:
            self._resolve_symbol(mt5)
        return mt5

    def _resolve_symbol(self, mt5: Any) -> None:
        self.candidates = rank_gold_symbols(mt5.symbols_get())
        wanted = self.configured_symbol
        if wanted:
            info = mt5.symbol_info(wanted)
            if info is None:
                names = ", ".join(c["name"] for c in self.candidates[:6]) or "none found"
                raise SymbolNotFound(f"symbol {wanted!r} does not exist in this MT5 terminal; gold candidates: {names}")
            source = "user" if self.symbol_source == "user" else "configured"
        else:
            if not self.candidates:
                raise SymbolNotFound("no gold/USD symbol found in this MT5 terminal; set GOLDSCALP_MT5_SYMBOL to your "
                                     "broker's gold symbol")
            info = mt5.symbol_info(self.candidates[0]["name"])
            if info is None:
                raise SymbolNotFound(f"symbol {self.candidates[0]['name']!r} could not be read")
            source = "auto"
        if not info.visible and self.select_symbol:
            if not mt5.symbol_select(info.name, True):
                code, desc = _last_error(mt5)
                raise SymbolNotFound(f"could not add {info.name} to Market Watch ({code} {desc})")
        self.resolved_symbol, self.symbol_source = info.name, source
        self.spec = symbol_spec(info)
        self.price_decimals, self.point = int(info.digits), float(info.point)

    def _quote_sync(self) -> RawQuote:
        mt5 = self._ensure_ready()
        tick = mt5.symbol_info_tick(self.resolved_symbol)
        now = self.clock.now()
        if tick is None:
            code, desc = _last_error(mt5)
            if code in IPC_CODES:
                self._initialized = False
                raise InitializeFailed(f"lost the connection to the MT5 terminal ({code} {desc})")
            raise NoTick(f"MT5 returned no tick for {self.resolved_symbol} ({code} {desc})")
        bid, ask = _price(getattr(tick, "bid", None)), _price(getattr(tick, "ask", None))
        if bid is None or ask is None or ask < bid:
            raise InvalidTick(f"invalid MT5 tick for {self.resolved_symbol}: bid={getattr(tick, 'bid', None)} "
                              f"ask={getattr(tick, 'ask', None)}")
        msc = getattr(tick, "time_msc", 0) or 0
        server_epoch = msc / 1000.0 if msc else float(tick.time)
        self._update_basis(server_epoch, now)
        assert self.basis is not None
        quote_time = server_to_utc([int(msc if msc else tick.time)], self.basis, "ms" if msc else "s").iloc[0]
        if pd.isna(quote_time):
            raise InvalidTick("MT5 tick time could not be converted to UTC")
        quote_time = quote_time.to_pydatetime()
        if (quote_time - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
            raise InvalidTick(f"MT5 tick time is {int((quote_time - now).total_seconds())} s in the future: the MT5 "
                              f"time basis ({self.basis.text}) is wrong; set GOLDSCALP_MT5_TIME_BASIS")
        last = _price(getattr(tick, "last", None))
        return RawQuote(bid=bid, ask=ask, last=last, quote_time=quote_time, received_at=now)

    def _update_basis(self, server_epoch: float, now: datetime) -> None:
        """Auto time basis. Detection uses only a NEW tick (its time differs from the previous poll's): a stale tick
        (weekend, daily break, holiday) never changes, and against the wall clock it would periodically look 'fresh'
        for some 15-minute-rounded offset (Phase 4 finding, 14 Nov 2026: a Friday tick was read as fresh with offsets
        of -12:15, -12:30, ... and every change re-backfilled the candles at shifted times)."""
        if self.time_basis_setting != "auto":
            return
        new_tick = self._last_server_epoch is not None and server_epoch != self._last_server_epoch
        self._last_server_epoch = server_epoch
        offset = detect_offset(server_epoch, now) if new_tick else None
        if offset is None:
            if self.basis is None:   # nothing fresh yet (e.g. weekend): assume the commonest server clock
                self.basis, self.basis_source = TimeBasis.parse(ASSUMED_BASIS), "assumed"
                self.basis_version += 1
            return
        text = basis_for_offset(offset, now)
        if self.basis is None or self.basis.text != text or self.basis_source != "detected":
            changed = self.basis is not None and self.basis.text != text
            self.basis, self.basis_source = TimeBasis.parse(text), "detected"
            if changed or self.basis_version == 0:
                self.basis_version += 1
        self.server_offset_seconds = offset

    def _candles_sync(self, tf: Timeframe, first: bool, now: datetime) -> pd.DataFrame:
        mt5 = self._ensure_ready()
        if self.basis is None:
            self._quote_sync()   # establishes the time basis
        assert self.basis is not None
        count = self.backfill_bars[tf] if first else REFRESH_BARS[tf]
        rates = mt5.copy_rates_from_pos(self.resolved_symbol, getattr(mt5, TF_ATTR[tf]), 0, count)
        if rates is None and first and count > BACKFILL_BARS[tf]:
            # a deep history request can exceed the terminal's limits: never end up without candles
            code, desc = _last_error(mt5)
            self.backfill_notes[tf.value] = (f"{count} bars refused ({code} {desc}); fell back to "
                                             f"{BACKFILL_BARS[tf]}")
            rates = mt5.copy_rates_from_pos(self.resolved_symbol, getattr(mt5, TF_ATTR[tf]), 0, BACKFILL_BARS[tf])
        if rates is None:
            code, desc = _last_error(mt5)
            raise ProviderError(f"copy_rates_from_pos({self.resolved_symbol}, {tf.value}) failed: {code} {desc}")
        return rates_to_frame(rates, self.basis, self.point or 0.0, tf, now)

    def _symbols_sync(self, query: str) -> dict[str, Any]:
        mt5 = self._ensure_ready()
        found = []
        q = query.strip().upper()
        if q:
            for s in mt5.symbols_get() or ():
                if q in s.name.upper() or q in str(getattr(s, "description", "")).upper():
                    found.append({"name": s.name, "description": getattr(s, "description", ""),
                                  "visible": bool(getattr(s, "visible", False))})
                    if len(found) >= 50:
                        break
        return {"candidates": self.candidates[:10], "matches": found, "selected": self.resolved_symbol,
                "symbol_source": self.symbol_source}

    async def run_sync(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` on the single MT5 thread (used by the Phase 4 terminal verification)."""
        return await self._run(fn, *args)

    def call_on_mt5_thread(self, fn: Callable[[Any], Any], timeout: float = 15.0) -> Any:
        """Run ``fn(module)`` synchronously on the single MT5 thread after the usual readiness checks.

        Only the opt-in demo-execution gateway (goldscalp/execution) calls this; it immediately wraps the module in
        its own demo-only allowlist (``DemoOnlyMT5``). This provider itself stays read-only market data."""
        def run() -> Any:
            self._ensure_ready()
            module = object.__getattribute__(self._mt5, "_module") if isinstance(self._mt5, ReadOnlyMT5) else self._mt5
            return fn(module)
        return _EXECUTOR.submit(run).result(timeout=timeout)

    def adapter_name(self) -> str | None:
        """'MetaTrader5' for the real package; a test double reports its own class (never 'MetaTrader5')."""
        if self._mt5 is None:
            return None
        module = object.__getattribute__(self._mt5, "_module") if isinstance(self._mt5, ReadOnlyMT5) else self._mt5
        name = getattr(module, "__name__", None)
        if isinstance(name, str):
            return name
        return f"{type(module).__module__}.{type(module).__name__}"

    def verification_facts(self) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
        """Read-only facts for the terminal verification (MT5 thread): initialize (no credentials), terminal and
        symbol, one tick, M1/M5/M15/H1 rates, then a disconnect (shutdown) and reconnect with a new tick."""
        image = PurePath(self.terminal_path).name if self.terminal_path else "terminal64.exe"
        facts: dict[str, Any] = {"process": {"image": image,
                                             "running": self.process_check(image) if self.require_running else None}}
        frames: dict[str, pd.DataFrame] = {}
        try:
            self._ensure_ready()
        except MT5Error as exc:
            facts.update(adapter=self.adapter_name(), terminal=dict(self.terminal),
                         init_error={"kind": exc.kind, "message": str(exc)})
            return facts, frames
        facts.update(adapter=self.adapter_name(), version=list(self.version) if self.version else None,
                     terminal=dict(self.terminal),
                     symbol={"name": self.resolved_symbol, "source": self.symbol_source,
                             "candidates": self.candidates[:6], "spec": self.spec})
        try:
            q = self._quote_sync()
            facts["tick"] = {"bid": str(q.bid), "ask": str(q.ask), "quote_time": iso(q.quote_time),
                             "received_at": iso(q.received_at), "time_basis": self.basis.text if self.basis else None,
                             "time_basis_source": self.basis_source,
                             "server_offset_hours": None if self.server_offset_seconds is None
                             else self.server_offset_seconds / 3600}
        except ProviderError as exc:
            facts["tick_error"] = {"kind": getattr(exc, "kind", "error"), "message": str(exc)}
        now = self.clock.now()
        errors: dict[str, str] = {}
        for tf in ALL_TIMEFRAMES:
            try:
                frame = self._candles_sync(tf, True, now)
                frames[tf.value] = frame.set_index("ts").sort_index() if "ts" in frame else frame
            except ProviderError as exc:
                errors[tf.value] = str(exc)
        facts["candle_errors"] = errors
        started = time.perf_counter()
        self._shutdown_sync()
        try:
            q2 = self._quote_sync()          # re-initializes (still no credentials) and reads a new tick
            facts["reconnect"] = {"ok": True, "seconds": round(time.perf_counter() - started, 3),
                                  "tick_time": iso(q2.quote_time), "symbol": self.resolved_symbol}
        except ProviderError as exc:
            facts["reconnect"] = {"ok": False, "seconds": round(time.perf_counter() - started, 3),
                                  "error": str(exc)}
        return facts, frames

    def _shutdown_sync(self) -> None:
        if self._mt5 is not None and self._initialized:
            try:
                self._mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._initialized = False

    # -------------------------------------------------------------- views

    def details(self) -> dict[str, Any]:
        return {"symbol": self.resolved_symbol, "symbol_source": self.symbol_source,
                "configured_symbol": self.configured_symbol,
                "candidates": [{"name": c["name"], "description": c["description"]} for c in self.candidates[:6]],
                "terminal": self.terminal, "version": list(self.version) if self.version else None,
                "time_basis": self.basis.text if self.basis else None, "time_basis_source": self.basis_source,
                "server_offset_hours": None if self.server_offset_seconds is None else self.server_offset_seconds / 3600,
                "symbol_spec": self.spec, "initialized": self._initialized, "last_init_error": self.last_init_error}


def rates_to_frame(rates: Any, basis: TimeBasis, point: float, tf: Timeframe, now: datetime) -> pd.DataFrame:
    """MT5 rate array -> raw candle frame in UTC (validated later by ingestion)."""
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "tick_volume", "spread", "closed"])
    ts = server_to_utc(rates["time"], basis)
    frame = pd.DataFrame({
        "ts": ts.to_numpy(),
        "open": np.asarray(rates["open"], dtype="float64"), "high": np.asarray(rates["high"], dtype="float64"),
        "low": np.asarray(rates["low"], dtype="float64"), "close": np.asarray(rates["close"], dtype="float64"),
        "tick_volume": np.asarray(rates["tick_volume"], dtype="int64"),
        "spread": np.asarray(rates["spread"], dtype="float64") * point,
    })
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame["closed"] = (frame["ts"] + pd.Timedelta(seconds=tf.seconds)) <= pd.Timestamp(now)
    return frame


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return Decimal(repr(f))


def _last_error(mt5: Any) -> tuple[int, str]:
    try:
        code, desc = mt5.last_error()
        return int(code), str(desc)
    except Exception:  # noqa: BLE001
        return 0, "unknown error"


def _init_hint(code: int, desc: str) -> str:
    if code in IPC_CODES:
        return (f"could not connect to the MT5 terminal ({code} {desc}). Is MetaTrader 5 running and logged in? "
                "With several terminals installed, set GOLDSCALP_MT5_TERMINAL_PATH to the terminal64.exe to use.")
    if code == -6:
        return (f"MT5 authorization failed ({code} {desc}): log in to your demo account inside the MT5 terminal. "
                "GoldScalp never asks for or stores your password.")
    return f"MT5 initialize() failed ({code} {desc})"
