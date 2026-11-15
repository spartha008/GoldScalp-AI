"""A fake MetaTrader5 module for tests: documented function names and return shapes, no terminal.

Tick and bar times are produced the way real MT5 terminals report them: the broker's
trade-server wall clock counted as if it were UTC (``server_offset_hours`` ahead of UTC).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import numpy as np

RATE_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])


def symbol(name: str, *, base: str = "XAU", profit: str = "USD", description: str = "Gold vs US Dollar",
           digits: int = 2, contract: float = 100.0, visible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, currency_base=base, currency_profit=profit,
                           digits=digits, point=10.0 ** -digits, trade_contract_size=contract, volume_min=0.01,
                           volume_max=100.0, volume_step=0.01, visible=visible)


def server_epoch(utc: datetime, offset_hours: float) -> float:
    return utc.timestamp() + offset_hours * 3600


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1 = 1, 5, 15, 16385

    def __init__(self, *, symbols: list[SimpleNamespace] | None = None, server_offset_hours: float = 2,
                 connected: bool = True, init_ok: bool = True,
                 init_error: tuple[int, str] = (-10003, "IPC initialize failed, MetaTrader 5 x64 not found"),
                 company: str = "Demo Broker Ltd", data_path: str | None = None) -> None:
        self.company = company
        self.data_path = data_path
        self.symbols = symbols if symbols is not None else [symbol("XAUUSD")]
        self.server_offset_hours = server_offset_hours
        self.connected = connected
        self.init_ok = init_ok
        self.init_error = init_error
        self.initialized = False
        self.calls: list[tuple[str, Any]] = []
        self.ticks: dict[str, Any] = {}
        self.rates: dict[tuple[str, int], np.ndarray] = {}
        self._error: tuple[int, str] = (1, "Success")

    # ---------------------------------------------------------------- helpers for tests

    def set_tick(self, name: str, utc: datetime, bid: Any, ask: Any) -> None:
        epoch = server_epoch(utc, self.server_offset_hours)
        self.ticks[name] = SimpleNamespace(time=int(epoch), bid=bid, ask=ask, last=0.0, volume=0,
                                           time_msc=int(round(epoch * 1000)), flags=6, volume_real=0.0)

    def set_rates(self, name: str, tf: int, rows: list[tuple[datetime, float, float, float, float]],
                  spread_points: int = 25) -> None:
        arr = np.zeros(len(rows), dtype=RATE_DTYPE)
        for i, (utc, o, h, low, c) in enumerate(rows):
            arr[i] = (int(server_epoch(utc, self.server_offset_hours)), o, h, low, c, 100 + i, spread_points, 0)
        self.rates[(name, tf)] = arr

    # ---------------------------------------------------------------- documented API

    def initialize(self, **kwargs: Any) -> bool:
        self.calls.append(("initialize", kwargs))
        if self.init_ok:
            self.initialized, self._error = True, (1, "Success")
            return True
        self._error = self.init_error
        return False

    def shutdown(self) -> None:
        self.calls.append(("shutdown", None))
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._error

    def version(self) -> tuple[int, int, str]:
        return (500, 6317, "11 Nov 2026")

    def terminal_info(self) -> Any:
        if not self.initialized:
            self._error = (-10004, "No IPC connection")
            return None
        return SimpleNamespace(connected=self.connected, build=6317, company=self.company, trade_allowed=False,
                               name="MetaTrader 5", path="C:\\Program Files\\MetaTrader 5", data_path=self.data_path,
                               ping_last=42000)

    def symbols_get(self, group: str | None = None) -> tuple[Any, ...]:
        return tuple(self.symbols)

    def symbol_info(self, name: str) -> Any:
        return next((s for s in self.symbols if s.name == name), None)

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self.calls.append(("symbol_select", name))
        info = self.symbol_info(name)
        if info is None:
            return False
        info.visible = enable
        return True

    def symbol_info_tick(self, name: str) -> Any:
        tick = self.ticks.get(name)
        if tick is None:
            self._error = (-1, "Terminal: Call failed")
        return tick

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        arr = self.rates.get((name, timeframe))
        if arr is None:
            self._error = (-1, "Terminal: Call failed")
            return None
        return arr[-count:] if count < len(arr) else arr

    # ---------------------------------------------------------------- trading functions (must never be reached)

    def order_send(self, request: Any) -> Any:
        raise AssertionError("order_send must never be called")

    def account_info(self) -> Any:
        raise AssertionError("account_info must never be called")

    def positions_get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("positions_get must never be called")
