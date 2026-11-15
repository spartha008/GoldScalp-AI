"""gold-api.com - free, keyless indicative spot gold price.

Documented behaviour (https://gold-api.com/llms.txt, read 13 Nov 2026):
- GET https://api.gold-api.com/price/XAU  -> {"price", "symbol", "currency", "updatedAt", ...}
- No API key and no rate limit on /price, but "Live prices update every 30
  seconds. Cache /price responses for 30 seconds rather than polling faster,
  and don't send multiple requests per second."
- Cache-Control max-age gives the seconds until the next update.
- ``updatedAt`` is when the provider last fetched the price from ITS source
  (UTC). It is used as the quote time; the true market time may be earlier.
- Spot price per troy ounce, single price: NO bid/ask, NO intraday candles on
  the free tier. "Commercial use: Always permitted."

Because prices are 30-second snapshots rather than a real-time stream,
GoldScalp classifies them as DELAYED (never LIVE).
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from goldscalp.core.clock import UTC, parse_iso_aware
from goldscalp.data.providers.base import (
    Freshness,
    MalformedResponse,
    ProviderInfo,
    RawQuote,
    SourceUnavailable,
    classify_http,
)
from goldscalp.simulation.decimal_utils import price_from_float

BASE_URL = "https://api.gold-api.com"
_MAX_AGE = re.compile(r"max-age=(\d+)")

INFO = ProviderInfo(
    id="gold-api",
    name="gold-api.com",
    homepage="https://gold-api.com/",
    docs_url="https://gold-api.com/docs",
    terms_note=("Free /price endpoint, no key. Provider asks clients to cache 30 s and never send multiple "
                "requests per second. Commercial use permitted per provider documentation."),
    attribution="Spot price data: gold-api.com",
    freshness=Freshness.PERIODIC_SNAPSHOT,
    refresh_seconds=30,
    documented_delay_seconds=None,
    supports_bid_ask=False,
    supports_candles=False,
    transport="polling",
    source_symbol="XAU",
    price_kind="indicative spot price (single price, no bid/ask)",
    needs_api_key=False,
)


class GoldApiProvider:
    info = INFO

    def __init__(self, base_url: str = BASE_URL, user_agent: str = "GoldScalpAI/0.1 (educational paper trading)"):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": user_agent}

    async def fetch_quote(self, client: httpx.AsyncClient) -> RawQuote:
        try:
            response = await client.get(f"{self.base_url}/price/XAU", headers=self.headers)
        except httpx.TimeoutException as exc:
            raise SourceUnavailable(f"gold-api.com timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"gold-api.com unreachable ({type(exc).__name__})") from exc
        received = datetime.now(UTC)
        classify_http(response, "gold-api.com")
        return parse_price(response.json() if _is_json(response) else None, response.headers.get("cache-control"),
                           received)


def parse_price(body: object, cache_control: str | None, received_at: datetime) -> RawQuote:
    if not isinstance(body, dict):
        raise MalformedResponse("gold-api.com did not return a JSON object")
    if body.get("symbol") != "XAU" or body.get("currency", "USD") != "USD":
        raise MalformedResponse(f"unexpected symbol/currency {body.get('symbol')!r}/{body.get('currency')!r}")
    try:
        price = price_from_float(body["price"], "price")
        quote_time = parse_iso_aware(str(body["updatedAt"]), "updatedAt")
    except (KeyError, ValueError) as exc:
        raise MalformedResponse(f"gold-api.com response is missing a usable price/updatedAt: {exc}") from exc
    next_update = None
    if cache_control:
        match = _MAX_AGE.search(cache_control)
        if match:
            next_update = int(match.group(1))
    return RawQuote(bid=None, ask=None, last=price, quote_time=quote_time, received_at=received_at,
                    next_update_seconds=next_update)


def _is_json(response: httpx.Response) -> bool:
    return "json" in response.headers.get("content-type", "")
