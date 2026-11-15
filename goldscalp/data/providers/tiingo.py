"""Tiingo FX - real-time XAUUSD bid/ask stream and intraday candles (free API key).

Documented behaviour (tiingo.com/documentation/forex, /documentation/websockets/forex,
/about/pricing - read 13 Nov 2026):
- WebSocket wss://api.tiingo.com/fx. Subscribe with
  {"eventName": "subscribe", "authorization": <key>, "eventData": {"thresholdLevel": "5", "tickers": [...]}}.
  Quote messages: messageType "A", data = ["Q", ticker, isoDate, bidSize, bidPrice, midPrice, askSize, askPrice].
  "I" = subscription info, "H" = heartbeat. thresholdLevel 5 = every top-of-book update.
- REST https://api.tiingo.com/tiingo/fx/<ticker>/top  -> quoteTimestamp, bidPrice, askPrice, midPrice ...
  REST https://api.tiingo.com/tiingo/fx/<ticker>/prices?startDate=YYYY-MM-DD&resampleFreq=1min|5min|15min|1hour
       -> [{date, ticker, open, high, low, close}]
- Free "Starter" plan: 50 requests/hour, 1,000/day, 500 symbols/month, 1 GB/month bandwidth,
  licence "Internal Use Only" (personal use; do not show or share the data with others).
- The API key is a DATA key. It cannot place orders (Tiingo is not a broker).

UNVERIFIED without a key: the exact gold ticker (configured, default "xauusd"),
whether REST candles are built from mid or bid prices, and whether WebSocket
traffic counts toward the 1 GB/month bandwidth limit (GoldScalp assumes it does
and stops streaming at a configurable budget).
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from collections.abc import Awaitable, Callable

import httpx
import pandas as pd
import websockets

from goldscalp.core.clock import parse_iso_aware
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.providers.base import (
    CredentialsRejected,
    Freshness,
    MalformedResponse,
    NotEntitled,
    ProviderInfo,
    RawQuote,
    SourceUnavailable,
    classify_http,
)
from goldscalp.simulation.decimal_utils import price_from_float

REST_BASE = "https://api.tiingo.com/tiingo/fx"
WS_URL = "wss://api.tiingo.com/fx"
RESAMPLE = {Timeframe.M1: "1min", Timeframe.M5: "5min", Timeframe.M15: "15min", Timeframe.H1: "1hour"}
HEARTBEAT_TIMEOUT_SECONDS = 90


def info_for(ticker: str) -> ProviderInfo:
    return ProviderInfo(
        id="tiingo", name="Tiingo FX", homepage="https://www.tiingo.com/products/forex-api",
        docs_url="https://www.tiingo.com/documentation/websockets/forex",
        terms_note=("Free Starter plan: 50 requests/hour, 1,000/day, 1 GB/month; licence 'Internal Use Only' - "
                    "personal use on this computer, do not show or share the data with others."),
        attribution="Real-time FX data: Tiingo",
        freshness=Freshness.STREAMING_REALTIME, refresh_seconds=None, documented_delay_seconds=None,
        supports_bid_ask=True, supports_candles=True, transport="websocket", source_symbol=ticker,
        price_kind="top-of-book bid/ask (real-time stream)", needs_api_key=True,
        candle_side="mid")  # UNVERIFIED: Tiingo does not document whether REST bars are mid or bid


class TiingoProvider:
    def __init__(self, api_key: str, ticker: str = "xauusd", threshold_level: str = "5") -> None:
        if not api_key:
            raise ValueError("Tiingo needs an API key (GOLDSCALP_TIINGO_API_KEY)")
        self._key = api_key
        self.ticker = ticker.lower()
        self.threshold_level = str(threshold_level)
        self.info = info_for(self.ticker)
        self.bytes_received = 0

    @staticmethod
    def backfill_start(tf: Timeframe, now: datetime) -> datetime:
        return backfill_start(tf, now)

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Token {self._key}"}

    # ------------------------------------------------------------------ REST

    async def fetch_quote(self, client: httpx.AsyncClient) -> RawQuote:
        body = await self._get(client, f"{REST_BASE}/{self.ticker}/top", {})
        if not isinstance(body, list) or not body:
            raise MalformedResponse(f"Tiingo returned no top-of-book for {self.ticker!r}; the ticker may be wrong")
        return parse_top(body[0], datetime.now(UTC))

    async def fetch_candles(self, client: httpx.AsyncClient, tf: Timeframe, *, first: bool,
                            now: datetime) -> pd.DataFrame:
        """First call per timeframe backfills; later calls refresh the last day."""
        start = backfill_start(tf, now) if first else now - timedelta(days=1)
        params = {"startDate": start.strftime("%Y-%m-%d"), "resampleFreq": RESAMPLE[tf], "format": "json"}
        body = await self._get(client, f"{REST_BASE}/{self.ticker}/prices", params)
        return parse_prices(body)

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, str]) -> Any:
        try:
            response = await client.get(url, params=params, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise SourceUnavailable(f"Tiingo timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"Tiingo unreachable ({type(exc).__name__})") from exc
        self.bytes_received += len(response.content)
        classify_http(response, "Tiingo")
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedResponse("Tiingo returned invalid JSON") from exc

    # ------------------------------------------------------------- WebSocket

    async def stream(self, on_quote: Callable[[RawQuote], Awaitable[None]],
                     on_event: Callable[[str, str], None]) -> None:
        """Run one WebSocket session until it ends. Raises ProviderError on failure."""
        subscribe = {"eventName": "subscribe", "authorization": self._key,
                     "eventData": {"thresholdLevel": self.threshold_level, "tickers": [self.ticker]}}
        try:
            async with websockets.connect(WS_URL, open_timeout=10, close_timeout=5, max_size=2**20,
                                          ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe))
                on_event("connected", f"subscribed to {self.ticker}")
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_TIMEOUT_SECONDS)
                    self.bytes_received += len(raw) if isinstance(raw, (bytes, str)) else 0
                    kind, payload = parse_message(raw, self.ticker, datetime.now(UTC))
                    if kind == "quote" and payload is not None:
                        await on_quote(payload)
                    elif kind == "error":
                        message = str(payload)
                        if "auth" in message.lower() or "token" in message.lower() or "401" in message:
                            raise CredentialsRejected(f"Tiingo rejected the API key: {message}")
                        raise NotEntitled(f"Tiingo refused the subscription: {message}")
                    elif kind == "info":
                        on_event("subscribed", str(payload))
        except TimeoutError as exc:
            raise SourceUnavailable(f"no Tiingo message or heartbeat for {HEARTBEAT_TIMEOUT_SECONDS} s") from exc
        except websockets.InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise CredentialsRejected(f"Tiingo refused the WebSocket (HTTP {status})") from exc
            raise SourceUnavailable(f"Tiingo WebSocket handshake failed (HTTP {status})") from exc
        except (OSError, websockets.ConnectionClosed, websockets.InvalidHandshake) as exc:
            raise SourceUnavailable(f"Tiingo WebSocket closed ({type(exc).__name__})") from exc


def parse_top(row: Any, received_at: datetime) -> RawQuote:
    if not isinstance(row, dict):
        raise MalformedResponse("Tiingo top-of-book row is not an object")
    try:
        quote_time = parse_iso_aware(str(row["quoteTimestamp"]), "quoteTimestamp")
        bid = price_from_float(row["bidPrice"], "bidPrice") if row.get("bidPrice") is not None else None
        ask = price_from_float(row["askPrice"], "askPrice") if row.get("askPrice") is not None else None
        mid = price_from_float(row["midPrice"], "midPrice") if row.get("midPrice") is not None else None
    except (KeyError, ValueError) as exc:
        raise MalformedResponse(f"Tiingo top-of-book is missing fields: {exc}") from exc
    if bid is not None and ask is not None and ask < bid:
        raise MalformedResponse("Tiingo top-of-book has ask below bid")
    return RawQuote(bid=bid, ask=ask, last=mid, quote_time=quote_time, received_at=received_at)


def parse_message(raw: Any, ticker: str, received_at: datetime) -> tuple[str, Any]:
    """Classify one WebSocket message: ("quote", RawQuote) | ("heartbeat"|"info"|"ignore", ...) | ("error", text)."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return "ignore", None
    if not isinstance(msg, dict):
        return "ignore", None
    response = msg.get("response")
    if isinstance(response, dict) and isinstance(response.get("code"), int) and response["code"] >= 400:
        return "error", f"{response.get('code')} {response.get('message')}"
    kind = msg.get("messageType")
    if kind == "H":
        return "heartbeat", None
    if kind == "I":
        return "info", msg.get("data")
    if kind == "E":
        return "error", msg.get("response") or msg.get("data")
    if kind != "A":
        return "ignore", None
    data = msg.get("data")
    if not isinstance(data, list) or len(data) < 8 or data[0] != "Q" or str(data[1]).lower() != ticker:
        return "ignore", None
    try:
        quote_time = parse_iso_aware(str(data[2]), "date")
        bid = price_from_float(data[4], "bidPrice") if data[4] is not None else None
        ask = price_from_float(data[7], "askPrice") if data[7] is not None else None
        mid = price_from_float(data[5], "midPrice") if data[5] is not None else None
    except ValueError:
        return "ignore", None
    if bid is not None and ask is not None and ask < bid:
        return "ignore", None
    return "quote", RawQuote(bid=bid, ask=ask, last=mid, quote_time=quote_time, received_at=received_at)


def parse_prices(body: Any) -> pd.DataFrame:
    if not isinstance(body, list):
        raise MalformedResponse("Tiingo prices response is not a list")
    rows = []
    for item in body:
        if not isinstance(item, dict):
            continue
        rows.append({"ts": item.get("date"), "open": item.get("open"), "high": item.get("high"),
                     "low": item.get("low"), "close": item.get("close")})
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce", format="ISO8601")
    frame["tick_volume"] = pd.NA
    frame["spread"] = float("nan")
    frame["closed"] = True
    return frame


def backfill_start(tf: Timeframe, now: datetime) -> datetime:
    """How far back to backfill each timeframe on start-up (one request each)."""
    days = {Timeframe.M1: 2, Timeframe.M5: 7, Timeframe.M15: 21, Timeframe.H1: 60}[tf]
    return now - timedelta(days=days)
