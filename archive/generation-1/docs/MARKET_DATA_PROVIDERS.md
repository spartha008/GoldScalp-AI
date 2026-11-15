# Market-Data Providers: Research and Selection (Phase 1)

Research date: **13 November 2026**. Sources were official documentation, pricing pages and
terms only. No accounts were created and no keys were used. The only API calls were to documented,
keyless public endpoints, made to confirm their response format.

> **Bottom line (Phase 1 research; for MetaTrader 5 see section 0)**
> - **No free source** gives genuinely real-time **spot** XAUUSD bid/ask **without an account of
>   some kind**.
> - The best free real-time option is **Tiingo FX**. It needs a free, data-only API key: you sign
>   up, and the key cannot place orders. GoldScalp supports it and labels it **LIVE** while the
>   stream is fresh.
> - With **no key at all**, GoldScalp uses **gold-api.com**. It publishes an indicative spot
>   price about every 30 s and has no bid/ask. GoldScalp therefore labels it **DELAYED**, never LIVE.
> - Historical candles come from **CSV import** (generic CSV, MetaTrader 5 bar exports and
>   fixed-offset files such as HistData), plus Tiingo's REST candles when a key is configured.

---

## 0. Phase 1.1: MetaTrader 5 is now the preferred source

**What it is.** Your broker's real-time bid/ask, read from your own **already logged-in** MT5
terminal through MetaQuotes' official `MetaTrader5` Python package (5.0.6231, Windows only, optional:
`requirements-mt5.txt`). There is no API key, no account, nothing to store, and no request limits
(the data comes over a local connection). Prices are labelled **LIVE** while the latest tick is within
`GOLDSCALP_MT5_STALE_AFTER_SECONDS` (default 10 s). Candles are bid-based.

**Read-only guarantees.**
- An allowlist proxy exposes only: `initialize` (no credentials), `shutdown`, `last_error`, `version`,
  `terminal_info`, `symbols_total`, `symbols_get`, `symbol_info`, `symbol_select`,
  `symbol_info_tick`, `copy_rates_from_pos` and the M1/M5/M15/H1 constants.
- Orders, positions, history, account and login functions raise `PermissionError`.
- A source scan in `tests/test_safety.py` fails if any of those names appears in GoldScalp code.

**Priority and switching.** The order is MT5 > Tiingo (if a key is set) > gold-api.com > stored
history. Fallback is **manual by default**: no silent switching. Opt-in auto fallback records every
switch, and the dashboard shows it as "Data source changed ... reason".

**Verified against a real terminal (13 Nov 2026, about 18:36 UTC, market open)** on this PC's MT5
terminal (build 6317, connected to its broker; terminal trading permission off):
- **Symbol:** auto-detected `XAUUSD` and ranked it above the broker's "not for trading" conversion
  symbol `XAUUSD.crp`. `XAUUSD` was hidden, so GoldScalp added it to Market Watch.
- **Server time:** detected as UTC+2 and mapped to `rule:ny_close`.
- **Ticks:** 5 consecutive polls were LIVE (tick age 0.4-0.5 s), with spread 0.28-0.29 USD.
- **Candles:** M1 3,000 / M5 3,000 / M15 2,000 / H1 2,000 fetched, 0 rejected, 0 duplicates.
  **0 bars outside the trading schedule**, including H1 back to 15 July, which confirms the
  DST-aware time conversion across the 1 November US change. The broker data is missing 4 of 3,003
  expected M1 bars and 2 of 2,001 H1 bars; these are reported, not filled.
- **Dashboard and simulator:** showed LIVE MT5 bid/ask. A paper estimate priced at the MT5 ask plus
  slippage, with `source=mt5` and `spread_source=market`. A manual Reconnect re-initialized and was
  back to LIVE within 2.5 s.
- **Not verified live:** a broker disconnect or terminal crash. Those paths are covered by mocked
  tests only.

---

## 1. Comparison

| Source | XAU support | Real-time / delayed (provider's own words) | Bid/ask | Free 1m/5m/15m/1h candles | Free limits | Streaming | Auth (can it trade?) | Licence / attribution |
|--------|-------------|---------------------------------------------|---------|---------------------------|-------------|-----------|----------------------|------------------------|
| **Tiingo FX** | Gold listed among FX commodities; ticker **`xauusd`** (unconfirmed without a key) | "Real-time – Prices Updated Instantly"; WebSocket top-of-book updates | **Yes** | **Yes**: `resampleFreq` 1min / 5min / 15min / 1hour | 50 req/hour, 1,000/day, 500 symbols/month, 1 GB/month | **WebSocket included on the free plan** ("firehose") | Free **data** API key; Tiingo is not a broker | "**Internal Use Only**" (personal; do not show or share the data with others) |
| **gold-api.com** | `XAU` | "Live prices update every 30 seconds" | No (single price) | No (history/OHLC need a key; minute/hour grouping is Premium) | `/price`: no key and no rate limit; cache 30 s; never several requests per second | No | **None** | "Commercial use: Always permitted"; attribution not required |
| goldapi.io | `XAU/USD` | Free tier at a 2 s interval; data "indicative" | Yes | No (daily history only) | 150 requests/month | WebSocket "evaluation" (each token lasts 15 min and counts as a request, so about 37 h/month) | Data key | Personal use allowed; no resale |
| metals.dev | `gold` | "maximum delay of 60 seconds" | Yes | No | 100 requests/month | No | Data key | No resale |
| metalpriceapi.com | `XAU` | Free tier: daily | n/d | Hourly on paid plans | 100 requests/month | No | Data key | Free plan for "personal exploration and evaluation" |
| Twelve Data | `XAU/USD`, but commodities are tagged as a paid plan (Grow) | Basic real-time for stocks, FX and crypto; XAU free access unconfirmed | REST no; WS example yes | Paid | 8 credits/min, 800/day | Trial only (8 symbols) | Data key | Basic plan is "**internal non-display usage**", which rules out showing prices in a UI |
| Finnhub | `OANDA:XAU_USD` style | n/a | n/a | Forex candles marked **Premium** | 60/min | Forex WS on paid plans | Data key | Personal; no redistribution |
| Massive (formerly Polygon.io) | `C:XAUUSD` | **End-of-day** on the free tier | No | Minute history, 2 years (bid-based) | 5 calls/min | No (paid) | Data key | Individual use |
| Alpha Vantage | `GOLD_SILVER_SPOT` | "live" spot (freshness unconfirmed) | n/d | Intraday FX is Premium | 25 requests/day | No | Data key | Personal |
| EODHD / FMP / iTick / Currencylayer | Varies | Delayed or end-of-day on free tiers | — | No intraday on free | 20–250/day | No | Data key | Various personal-use terms |
| Alpaca | **No XAUUSD** (equities, options, crypto only) | — | — | — | — | — | — | — |
| **OANDA v20 (practice)** | `XAU_USD` | Real-time | Yes | Yes | Generous | HTTP stream | **Account token that can place orders** | **Excluded**: broker credentials can trade |
| IG / Saxo / FXCM / MT5 demo | — | — | — | — | — | — | **Broker logins / trading tokens** | **Excluded** |
| Swissquote | — | Only an undocumented public endpoint is widely cited | — | — | — | — | — | **Excluded**: undocumented endpoint, not used |
| Dukascopy | XAU/USD history | Historical only | Ticks have both | Web export | — | JForex needs an account | — | Terms forbid building "a database"; **not used** |
| HistData.com | XAUUSD M1 (bid) | Historical only | No | M1 files from 2009 | — | No | None | No licence stated; use at your own risk. Format: `YYYYMMDD HHMMSS;O;H;L;C;V`, **fixed UTC−5 (EST, no DST)** |
| Binance / OKX / Kraken / Bybit | PAXG, XAUT, gold perpetuals | Live | Yes | Yes | Generous | Yes | None | **Not XAUUSD**: tokenised-gold or derivative proxies that trade 24/7, carry premiums and funding. **Not used** |
| Yahoo / Google / Investing.com | — | Google: "may be delayed up to 20 minutes" | — | — | — | — | — | No permitted programmatic API. **Not used** (no scraping) |

Sources (official pages): tiingo.com/documentation/forex, /documentation/websockets/forex,
/products/forex-api, /about/pricing · gold-api.com/llms.txt, /docs, /terms · goldapi.io/websockets ·
metals.dev/pricing · metalpriceapi.com/pricing · twelvedata.com/pricing, /terms ·
finnhub.io/pricing · massive.com/pricing · alphavantage.co/documentation ·
developer.oanda.com/rest-live-v20 · histdata.com/f-a-q · dukascopy.com legal pages ·
tradingview.com/widget-docs.

---

## 2. Selection and why

1. **Tiingo FX: the real-time source** (`GOLDSCALP_TIINGO_API_KEY`).
   - It is the only free plan with **real-time bid/ask, intraday candles and a WebSocket**, and its
     key is a *data* key that cannot place orders, so it fits the "no broker credentials" rule.
   - Its licence is "Internal Use Only". GoldScalp binds to `127.0.0.1` by default, which keeps the
     data on your own computer, as that licence requires. **Do not deploy GoldScalp publicly with a
     free Tiingo key.**
2. **gold-api.com: the keyless default.**
   - It needs no signup and commercial use is permitted. The price is genuine spot, but it is an
     **indicative ~30-second snapshot with no bid/ask**.
   - GoldScalp labels it **DELAYED** at all times, with the reason shown. Paper fills on it use the
     **configured spread**, and every fill is labelled.
3. **History (Mode B): CSV import.** Supported formats:
   - `generic_utc`: ISO timestamps with offsets;
   - `mt5_export`: MetaTrader 5 terminal "Export bars", in server time;
   - fixed-offset files, e.g. HistData at fixed UTC−5.

   The free real-time providers above do not offer deep free intraday history (Tiingo excepted,
   within its limits).

**Automatic choice** (`GOLDSCALP_LIVE_PROVIDER=auto`): Tiingo if a key is set, otherwise gold-api.com.

---

## 3. Real-time vs delayed: how GoldScalp labels data

| State | When |
|-------|------|
| **LIVE** | A **streaming real-time** provider (Tiingo WebSocket) delivered a price within `GOLDSCALP_LIVE_WINDOW_SECONDS` (default 15 s). |
| **DELAYED** | Any of: the provider is a periodic snapshot (gold-api.com, always); it documents a delay; or a real-time stream has gone quiet for longer than the live window. The reason and the price age are always shown. |
| **HISTORICAL** | The market is closed (weekly close or daily break, per the instrument schedule), or no live provider is configured and only stored history exists. |
| **DISCONNECTED** | No price received yet with errors, or no contact for `GOLDSCALP_DISCONNECT_AFTER_SECONDS` (default 180 s). The last price stays visible, greyed, with its timestamp. |

**Rules that are enforced and tested:**
- An older price never replaces a newer one.
- Simulated fills refuse prices older than the account's `max_quote_age_seconds`.
- Simulated fills refuse DISCONNECTED and HISTORICAL states.
- Fills on DELAYED prices happen only when the account explicitly allows them, and each is labelled.

---

## 4. Provider limits and how GoldScalp respects them

| Provider | Limit | GoldScalp behaviour |
|----------|-------|---------------------|
| gold-api.com | Cache 30 s; never several requests per second | Polls just after the provider's next update, using `Cache-Control: max-age`, and **never more often than every 30 s**. Polls every 5 min while the market is closed. Errors back off 30 → 60 → 120 → 240 → 300 s; HTTP 429 honours `Retry-After`. |
| Tiingo REST | 50/hour, 1,000/day | A request budget of 40/hour and 800/day by default is charged **before** each request. On start-up, one backfill request per timeframe. Refreshes: M1 every 5 min, M5/M15/H1 every 30 min. |
| Tiingo WebSocket | 1 GB/month bandwidth (whether WS traffic counts is unconfirmed; assumed yes) | The stream pauses while the market is closed. A bandwidth guard (`GOLDSCALP_TIINGO_MONTHLY_STREAM_MB`, default 700) falls back to REST snapshots every 120 s once reached. Reconnects use exponential backoff with jitter (1 → 60 s); a heartbeat timeout of 90 s forces a reconnect. |

**Known limitations (honest list):**
- **Tiingo is not verified against the live service.** No API key was available during
  development. The adapter is built from Tiingo's documentation and tested against recorded
  message formats. The gold ticker (`xauusd`) and whether REST candles are mid- or bid-based are
  unconfirmed. If no gold updates arrive, the status shows it; change `GOLDSCALP_TIINGO_TICKER` if
  needed.
- **gold-api.com has no bid/ask and no free intraday candles.** GoldScalp builds M1 candles from
  ~30 s snapshots, so their highs and lows are **approximate**, and it labels them as such.
- Request and bandwidth counters live in memory and reset on restart. The budgets keep a margin
  below provider limits for this reason.
- Holidays are not in the trading schedule yet; a holiday shows up as missing candles.

---

## 5. Optional TradingView Advanced Chart (display only)

Reviewed on 13 Nov 2026: widget-docs (Advanced Chart, widget formats, Web Component
configuring/styling/sizing, General FAQ, Data FAQ, What's new).

- **Embed.** It uses TradingView's **official Web Component**: an ES module from
  `widgets.tradingview-widget.com` plus `<tv-advanced-chart symbol=… interval=… theme="dark">`.
  The documented `--tv-widget-*` CSS tokens tint it black and gold. Attribution and branding
  are **unchanged**; TradingView's FAQ says changing them requires contacting TradingView.
- **Symbol and source.** The default is `OANDA:XAUUSD` (`GOLDSCALP_TRADINGVIEW_SYMBOL`). The panel
  names the symbol and says the chart and data come from TradingView. The Data FAQ states that forex
  data in widgets is real-time, and that there is **no data export and no data API**.
- **Separation (verified in the browser).**
  - The widget runs on its own page (`/tv-widget`) with a CSP that allows only TradingView hosts.
    A request from that page to the GoldScalp API is blocked by the browser ("Failed to fetch").
  - GoldScalp never reads widget data: there is no code path from the widget into Python, analysis,
    alerts or the simulator, and tests enforce this.
  - Intervals (1/5/15/60) are sent one way, as allow-listed `postMessage` values.
- **Optional and fail-safe.**
  - Click-to-load: there are no TradingView requests until you click, and auto-load is opt-in.
  - `GOLDSCALP_TRADINGVIEW_WIDGET=false` removes the panel and its route.
  - A failure or a 25 s timeout shows a message only in that panel.
- **Not verified:** `GOLDSCALP_TRADINGVIEW_SANDBOX=true` (an opaque-origin sandbox) is implemented
  but could not be tested, because the development browser pane blocks every sandboxed iframe. The
  default (unsandboxed, CSP-isolated) **was** verified.
