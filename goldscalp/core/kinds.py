"""Labels that keep current, historical and simulated numbers apart (principle P1)."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from goldscalp.core.clock import iso


class DataKind(StrEnum):
    CURRENT = "current"        # latest prices from a market-data provider
    HISTORICAL = "historical"  # stored candles and statistics derived from them
    SIMULATED = "simulated"    # anything produced by the paper-trading simulator


class MarketState(StrEnum):
    LIVE = "LIVE"                  # fresh prices from a source documented as real-time
    DELAYED = "DELAYED"            # source documents a delay, or updates are stale
    HISTORICAL = "HISTORICAL"      # market closed, or only imported history is shown
    DISCONNECTED = "DISCONNECTED"  # no usable connection / no provider configured


def envelope(kind: DataKind, data: Any, *, generated_at: datetime, source: str | None = None,
             as_of: datetime | None = None, state: MarketState | None = None,
             notes: list[str] | None = None) -> dict[str, Any]:
    """The one response shape every API route uses."""
    out: dict[str, Any] = {"kind": kind.value, "source": source, "as_of": iso(as_of),
                           "generated_at": iso(generated_at), "data": data}
    if state is not None:
        out["market_state"] = state.value
    if notes:
        out["notes"] = notes
    return out
