"""The provider interface. A provider is READ-ONLY market data.

Error taxonomy separates "fix the key" from "fix the plan" from "try again"
(pattern from SpicyStock src/market_data.py _is_permanent_refusal()).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import httpx


class Freshness(StrEnum):
    """How the provider itself describes its prices (from its documentation)."""
    STREAMING_REALTIME = "streaming_realtime"   # tick-level real-time stream
    PERIODIC_SNAPSHOT = "periodic_snapshot"     # prices refreshed every N seconds
    DELAYED = "delayed"                         # documented delay
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    homepage: str
    docs_url: str
    terms_note: str
    attribution: str | None
    freshness: Freshness
    refresh_seconds: int | None          # documented snapshot cadence (PERIODIC_SNAPSHOT)
    documented_delay_seconds: int | None  # documented delay (DELAYED)
    supports_bid_ask: bool
    supports_candles: bool
    transport: str                       # "polling" | "websocket"
    source_symbol: str
    price_kind: str                      # e.g. "indicative spot (single price)"
    needs_api_key: bool
    # Optional per-provider behaviour (defaults keep the Phase 1 providers unchanged).
    candle_side: str = "unknown"                  # bid | ask | mid | last | unknown
    closed_market_poll_seconds: int = 300         # how often to poll while the market is closed
    max_backoff_seconds: int = 300                # cap for transient-failure backoff
    permanent_backoff_seconds: int = 900          # wait after a permanent refusal
    build_candles_from_ticks: bool = True         # False when the provider's own candles are refreshed often
    candle_refresh_seconds: tuple[tuple[str, int], ...] | None = None  # ((tf, seconds), ...) or default cadence
    poll_seconds: float | None = None             # fixed polling cadence (overrides refresh_seconds)


@dataclass(frozen=True)
class RawQuote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    quote_time: datetime         # provider timestamp for the price (UTC)
    received_at: datetime        # GoldScalp receipt time (UTC)
    next_update_seconds: int | None = None


class ProviderError(Exception):
    kind = "provider_error"
    permanent = False
    #: True when the error means the data LINK itself is down (e.g. MT5 terminal not running or
    #: not connected to its broker). The state becomes DISCONNECTED immediately.
    link_down = False


class CredentialsRejected(ProviderError):
    kind = "credentials_rejected"
    permanent = True


class NotEntitled(ProviderError):
    kind = "not_entitled"
    permanent = True


class RateLimited(ProviderError):
    kind = "rate_limited"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailable(ProviderError):
    kind = "source_unavailable"


class MalformedResponse(ProviderError):
    kind = "malformed_response"


class QuoteProvider(Protocol):
    info: ProviderInfo

    async def fetch_quote(self, client: httpx.AsyncClient) -> RawQuote: ...


def classify_http(response: httpx.Response, provider: str) -> None:
    """Raise the right ProviderError for a non-2xx response."""
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 401:
        raise CredentialsRejected(f"{provider} rejected the credentials (HTTP 401)")
    if status == 403:
        raise NotEntitled(f"{provider} refused access (HTTP 403)")
    if status == 429:
        retry = response.headers.get("retry-after")
        try:
            seconds = float(retry) if retry is not None else None
        except ValueError:
            seconds = None
        raise RateLimited(f"{provider} rate limit reached (HTTP 429)", seconds)
    if status >= 500:
        raise SourceUnavailable(f"{provider} is unavailable (HTTP {status})")
    raise MalformedResponse(f"{provider} answered HTTP {status}")
