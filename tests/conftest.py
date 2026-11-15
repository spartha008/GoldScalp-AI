"""Shared fixtures. Network access is blocked for every test (loopback only, which
asyncio needs on Windows); no test touches a real data provider."""
from __future__ import annotations

import socket
from datetime import datetime, UTC
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.kinds import MarketState
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.pricing import MarketQuote
from goldscalp.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "config" / "instruments" / "xauusd_demo.json"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# A Wednesday, mid New York session: the market is open.
OPEN_TIME = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in LOOPBACK:
            raise RuntimeError(f"tests must not use the network (attempted {address!r})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def spec() -> InstrumentSpec:
    return InstrumentSpec.load(SPEC_PATH)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(OPEN_TIME)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def costs() -> CostModel:
    return CostModel.from_mapping({"spread_mode": "market_else_configured", "configured_spread": "0.30",
                                   "commission_per_lot_per_side": "3.50", "slippage": "0.05"})


@pytest.fixture
def account_config() -> AccountConfig:
    return AccountConfig.from_mapping({"name": "Test", "currency": "USD", "starting_balance": "10000",
                                       "leverage": 1, "max_quote_age_seconds": 30, "allow_delayed_fills": False})


def make_quote(bid: str | None, ask: str | None, *, at: datetime = OPEN_TIME, state: MarketState = MarketState.LIVE,
               last: str | None = None) -> MarketQuote:
    return MarketQuote(symbol="XAUUSD", bid=Decimal(bid) if bid else None, ask=Decimal(ask) if ask else None,
                       last=Decimal(last) if last else None, quote_time=at, received_at=at, source_id="test",
                       state=state, state_reason="test quote")


def synthetic_m1(start: str, end: str, schedule, seed: int = 7, price: float = 4000.0) -> pd.DataFrame:
    """Deterministic M1 candles inside trading hours only (synthetic data is for tests only)."""
    rng = np.random.default_rng(seed)
    rows = []
    p = price
    for t in pd.date_range(start, end, freq="min", inclusive="left"):
        if not schedule.is_open(t.to_pydatetime()):
            continue
        o = p
        p = p + rng.normal(0, 0.8)
        h = max(o, p) + abs(rng.normal(0, 0.3))
        low = min(o, p) - abs(rng.normal(0, 0.3))
        rows.append({"ts": t, "open": round(o, 2), "high": round(h, 2), "low": round(low, 2), "close": round(p, 2),
                     "tick_volume": int(rng.integers(5, 80)), "spread": 0.3, "closed": True})
    frame = pd.DataFrame(rows).set_index("ts")
    frame.index.name = "ts"
    frame["tick_volume"] = frame["tick_volume"].astype("Int64")
    return frame


def csv_bytes(frame: pd.DataFrame) -> bytes:
    lines = ["timestamp,open,high,low,close,tick_volume"]
    for ts, r in frame.iterrows():
        lines.append(f"{ts.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['tick_volume']}")
    return "\n".join(lines).encode()


FIXTURES = ROOT / "tests" / "fixtures"


def recorded(name: str) -> pd.DataFrame:
    """Recorded MT5 candles (tests/fixtures/README.md) as a canonical closed-candle frame."""
    frame = pd.read_csv(FIXTURES / name)
    frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True)
    frame.index.name = "ts"
    frame["tick_volume"] = frame["tick_volume"].astype("Int64")
    frame["spread"] = frame["spread"].astype("float64")
    frame["closed"] = True
    return frame[["open", "high", "low", "close", "tick_volume", "spread", "closed"]]
