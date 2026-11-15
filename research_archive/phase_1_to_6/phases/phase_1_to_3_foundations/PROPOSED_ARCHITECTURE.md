# GoldScalp AI: Proposed Architecture

Status: **approved. Revision 2 (13 November 2026) is implemented as Phase 1; Revisions 3-8 below record what
was built afterwards (Phases 1.1-5 and the optional MT5 demo-account execution).** Read it with:
- [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md): what SpicyStock is;
- [REUSE_VS_REBUILD.md](REUSE_VS_REBUILD.md): what moved across;
- [MARKET_DATA_PROVIDERS.md](MARKET_DATA_PROVIDERS.md): which data sources were chosen and why;
- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md): the build order.

### Revision 2: what changed and how Phase 1 was built
Revision 2 brings **live XAUUSD data** and **lot-based paper trading** into Phase 1, and adds an
optional **TradingView display component** (§16). Where the as-built code differs from the original
proposal below, the code and this list are authoritative.

- **Packages.**
  - `goldscalp/simulation/` (named `sim/` in the proposal) holds `instrument.py` (contract spec
    and trading schedule), `account.py`, `positions.py`, `pricing.py`, `costs.py` and
    `portfolio.py` (the engine).
  - `goldscalp/data/` holds `normalization.py`, `validation.py`, `gaps.py`, `resample.py`,
    `ingestion.py` (the only write path) and `live.py` (Mode A service). Its providers are
    `base.py`, `gold_api.py`, `tiingo.py` and `csv_file.py`.
  - `goldscalp/analysis/` holds Phase 1 versions of `structure.py`, `sessions.py` and `volatility.py`.
- **Data kinds.** `hypothetical` is renamed **`simulated`**. Every market payload also carries a
  **market state**: `LIVE` / `DELAYED` / `HISTORICAL` / `DISCONNECTED`
  (MARKET_DATA_PROVIDERS.md §3).
- **Contract specification is configuration.** `config/instruments/*.json` must state every field,
  and a missing field is an error. The shipped file is explicitly a **hypothetical demo**: 100 oz,
  0.01 to 50 lots, presets standard 1.00 / mini 0.10 / micro 0.01.
- **Simulator scope in Phase 1.**
  - **Market orders** with optional SL/TP, both directions, sized in lots. Pending orders come later.
  - Spread comes from provider bid/ask or the configured spread, labelled per fill.
  - Commission is charged per lot per side; slippage is adverse on every fill.
  - Margin = ounces × open price ÷ leverage, **fixed at open**.
  - Money is exact `Decimal`, stored as decimal strings.
  - Fills need a fresh, allowed-state price.
- **Storage (as built: `storage/migrations/0001_init.sql`).**
  - Candles live in **series**: one per live provider and one per CSV label, never mixed.
  - Each series stores prices as integers scaled by `10^price_decimals` (per series, because
    providers quote different precision).
  - Candles are stored per timeframe. Coarser timeframes that a series lacks are **derived from M1**
    and labelled as derived.
  - Paper tables: `paper_accounts`, `paper_positions`, `account_ledger` (balance = sum of the
    ledger) and `equity_snapshots`.
- **Synthetic data is used only in tests,** never as a stand-in for live data in the dashboard.
- **MT5 integration point A** (the terminal bar export, `mt5_export` dialect) shipped in Phase 1.

### Revision 3 (Phase 1.1): MetaTrader 5 market data, as built
- **MT5 provider.** `goldscalp/data/providers/mt5_provider.py` implements section 14.2 point **C**
  **inside the app process** (Windows only), rather than as a separate host-side tool. It is still
  optional: the package is lazily imported in that one module, so non-Windows installs, Docker and CI
  run without it (`tests/test_safety.py`). Its guarantees:
  - read-only allowlist proxy, with no credentials passed to `initialize()`;
  - process check before `initialize()`, so the terminal is never launched;
  - all calls on one dedicated thread, because the package keeps one process-wide connection;
  - gold-symbol ranking with suffix support, pushing down "not for trading" symbols;
  - server-time auto-detection mapped to `rule:ny_close` or a fixed offset;
  - native M1-H1 candles refreshed every 5-30 s.
- **Provider hub.** `goldscalp/data/hub.py` applies priority, user selection, and manual (default) or
  auto fallback with recovery. It keeps an event log of every source change, shown on the dashboard.
- **`live.py` additions.**
  - Providers can report "link down" (terminal or broker unavailable), which shows DISCONNECTED
    immediately and keeps the last price labelled STALE.
  - A fresh real-time tick shows LIVE even when the generic schedule says closed, because the broker's
    session is authoritative.
  - Per-provider cadence, backoff and candle refresh; manual reconnect; reconnect-attempt counting.
- **Paper fills record their pricing source.** Migration `0002_fill_source.sql` adds
  `open_source` / `close_source`.
- **API.**
  - New routes: `GET /api/market/providers`, `POST /api/market/provider`, `POST /api/market/reconnect`,
    `GET /api/market/mt5/symbols`.
  - **CSRF guard:** state-changing `/api/` calls require the `X-GoldScalp-Request: 1` header.
- **Display timezone.** `GOLDSCALP_DISPLAY_TIMEZONE` controls how the browser shows times; storage stays UTC.

### Revision 4 (Phase 2): Smart Market Intelligence

Phase 2 is specified and recorded as built in [PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md).

- **Analysis engines.** New engines in `goldscalp/analysis/` cover structure, liquidity, FVG, order
  blocks, sessions, volatility, multi-timeframe and signals.
  - Every engine works only on closed candles up to an explicit `as_of` (`candles.closed_view`).
  - Every event carries `known_at`.
  - Parameters live in `config/analysis.json`, which is strict and fingerprinted.
- **Replay engine** (`analysis/replay.py`). It rebuilds higher timeframes from base candles up to the
  cursor.
- **Fixed defect.** Derived higher-timeframe candles were marked closed before their period ended.
  `resample()` now takes `as_of`.
- **New migrations.**
  - `0003_signals.sql`: the `signals` table and `paper_positions.signal_id`.
  - `0004_performance.sql`: `performance_runs` and `analysis_configs`.
- **Performance evaluation** (`analysis/performance.py`, `perf@1`). Every evaluated signal is stored in
  `signals`, together with its outcome.
  - Its hypothetical strategy uses the paper simulator's own fill and cost formulas.
  - It creates no positions; the paper simulator stays the only trade-execution environment.
- **API.**
  - Granular routes: `/api/analysis/structure`, `/liquidity`, `/fvg`, `/order-blocks`,
    `/multi-timeframe`, `/sessions`, `/volatility`.
  - `/api/signals/current`.
  - `/api/performance/*`.
- **Web.**
  - New pages: `/replay` and `/performance`.
  - Annotation settings are saved per timeframe.
  - Chart labels are zoom-aware.
- **Report.** See [PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md).

### Revision 5 (Phase 3): Strategy Intelligence and Advanced Backtesting

Designed in [PHASE3_ARCHITECTURE.md](PHASE3_ARCHITECTURE.md); built as described in
[PHASE_3_IMPLEMENTATION_REPORT.md](PHASE_3_IMPLEMENTATION_REPORT.md).

- **New packages.**
  - `goldscalp/strategies/`: `strategy@1` configurations and condition evaluation.
  - `goldscalp/backtesting/`: `features@1` snapshots, `bt@1` engine, `metrics@1`, `wf@1` walk-forward,
    `lab`.
  - `goldscalp/reports/`: a dependency-free PDF writer, plus CSV and JSON exports.
- **Execution model.**
  - Backtests reuse the Phase 2 as-of pass: decisions at step cut-offs, with fills on later base
    candles.
  - They use the paper simulator's fill, commission and margin formulas, and never create positions.
  - The paper simulator stays the only trade-execution simulation environment. A strategy can open a
    simulated position only through a user-confirmed request (`POST /api/strategies/{id}/paper`).
- **Storage.** Migration `0005_strategies.sql`: versioned strategies, research runs and the strategy
  journal. Journal outcomes are read from `paper_positions`.
- **Jobs.** One background job runner (`api/jobs.py`) is shared by performance evaluations and
  research runs.
- **Fix.** Coverage-aware dataset base selection (`services.choose_base`), replacing the Phase 2 rule
  that started late ranges silently.

### Revision 6 (Phase 4): Research assistant, Vantage MT5 demo integration and live strategy evaluation

Designed in [PHASE4_ARCHITECTURE.md](PHASE4_ARCHITECTURE.md); built as described in
[PHASE_4_IMPLEMENTATION_REPORT.md](PHASE_4_IMPLEMENTATION_REPORT.md).

- **New packages.**
  - `goldscalp/live/`: `feed` (spread / freshness), `monitor` (`live@1`), `evaluation` (`eval@1`), `journal`.
  - `goldscalp/research/`: the improvement engine (`improve@1`, `validate@1`).
  - `goldscalp/assistant/`: read-only tools, a deterministic answerer, and an optional Claude mode.
  - `goldscalp/data/`: `integrity`, `mt5_verify` (`mt5v@1`), `providers/mt5_identity`.
- **MT5.**
  - The allowlist is unchanged.
  - The broker comes from `terminal_info().company`.
  - The demo server comes from the terminal's own `config/common.ini` (`Server=` only).
  - Verification records name their adapter, so only the real package can be VERIFIED.
- **Execution.** `PaperTradingEngine` accounts are addressable by id. Validation accounts (`purpose =
  'validation'`), one per monitored strategy version, never become the active account. The close reasons add
  `time_exit` and `strategy_exit`. The paper simulator remains the only execution simulator.
- **Live decisions** use the backtest's own as-of snapshot (`features.compute_timeline` for one cut-off).
  Parity with the backtest and the absence of look-ahead are tested.
- **Adoption.** Only `POST /api/proposals/{id}/approve` writes a strategy version from a proposal, after
  validation on an unseen holdout.
- **Storage.** Migration `0006_live.sql`. The runner supports a `foreign_keys=off` table rebuild with
  `foreign_key_check`.

### Revision 7 (Phase 5): Monthly multi-timeframe research and backtesting

Designed in [PHASE5_ARCHITECTURE.md](PHASE5_ARCHITECTURE.md); built as described in
[PHASE_5_IMPLEMENTATION_REPORT.md](PHASE_5_IMPLEMENTATION_REPORT.md).

- **New process.** The *research service* (`goldscalp/monthly/`, `python research.py`, port 8001).
  - It reads the application database read-only and owns `var/monthly/monthly.sqlite3`.
  - It never imports the hub, providers, paper engine or live monitor, and runs at below-normal priority.
- **Engine.** `bt@1` additions, diagnostics only (trades and result hashes unchanged): a rejected-entry log, and
  balance and used margin on the marked-equity points.
- **Strategy catalogue.** `strategy@1` gains `ema_trend` and `momentum`. Snapshots gain EMA(9-200) and RSI(14) per
  timeframe, computed from closed candles. Existing configurations and decisions are unchanged.
- **MT5 fix.** Automatic time-basis detection only uses a new tick. A stale weekend tick had re-dated stored
  candles; the basis is now pinned to `rule:ny_close` in `.env`. Backfill depth is configurable per timeframe.
- **Unchanged.** The section 5 definitions `structure@1`, `sessions@1` and `vol@1` remain available
  for compatibility.

### Revision 8 (14 November 2026): optional MT5 demo-account execution, as built

An unnumbered feature outside the phase plan, documented in README ("MT5 demo-account execution"). It is the
**only** GoldScalp code that may send orders, and it is **off by default** (`GOLDSCALP_DEMO_EXECUTION_ENABLED=false`).

- **Package `goldscalp/execution/`.**
  - `mt5_demo.py` is the gateway. It wraps the `MetaTrader5` module in its own allowlist proxy (`DemoOnlyMT5`).
    `order_check` and `order_send` re-read `account_info()` before **every** call and refuse unless the trade
    mode is DEMO, the server name says demo, and the server and hashed login match the link.
  - `mirror.py` follows the live monitor's SIMULATED validation trades of **linked** versions. It opens market
    orders with the same side, volume, stop-loss and take-profit, and closes them when the simulated trade
    closes. It never retries and never enters late, and its kill switch disables every link.
- **Storage.** Migration `0007_demo_execution.sql` adds `demo_links` and `demo_trades` (new tables only).
- **API and web.** `/api/live/demo` (status, per-version links, kill switch) and a *Demo Execution* tab on
  `/live`.
- **Unchanged.** The market-data provider's allowlist stays read-only. The simulator, the live monitor's
  decisions and the research service are unchanged and never import `goldscalp.execution` (safety tests).
  GoldScalp still never handles MT5 credentials.

GoldScalp AI is an **educational** XAUUSD research and **paper-trading**
application. It has **no real-money order path** and no automatic trading on a real
account. The one order feature is optional, off by default and limited to an MT5
demo account (Revision 8).

It is built **broker-agnostic**. Market-data providers sit behind one ingestion
interface (§13), outside the analysis and simulation engines. MetaTrader 5
market data was designed in §14 and is now implemented as a read-only adapter
(Revision 3). The application still runs with **no MT5 terminal and no
`MetaTrader5` package installed**.

---

## 1. Principles

These are design constraints, not aims. Each one has a test listed in §12.

| # | Principle | What it means in code |
|---|-----------|-----------------------|
| P1 | **Every number is labelled by kind.** | Every API payload carries `kind ∈ {historical, hypothetical, current}` plus `source`, `as_of` and `generated_at`. The UI renders the kind as a badge that is always visible. |
| P2 | **No look-ahead.** | Every detector is a function of `(candles, as_of)` and sees only **closed** bars up to `as_of`. Anything that needs future bars to confirm (swings, order blocks) carries a `confirmed_at` timestamp, and consumers may use it only after that time. |
| P3 | **One fill engine.** | Paper trading, replay and historical pattern evaluation all use the same `sim.fill_model`. A backtest can't be more optimistic than the simulator. |
| P4 | **Conservative when ambiguous.** | If a bar can't tell which happened first (stop or target, entry or stop), the engine first resolves it with finer bars. If it still can't tell, it records `ambiguous=True` and books the worse outcome. Analytics report how many fills were ambiguous. |
| P5 | **Gaps are gaps.** | The weekly close, the daily maintenance break and missing data are kept as distinct, explicit states. Bars are never spliced across a gap and missing minutes are never invented. |
| P6 | **Refuse unknown input.** | A malformed candle, a naive timestamp, an unknown timeframe or an unknown config value raises an error. The code never falls back silently. |
| P7 | **Read-only market data.** | Provider adapters can only *read* candles. No module may import a broker order API, and a test enforces this. *As built:* the one exception is the opt-in demo-only gateway `goldscalp/execution/mt5_demo.py` (Revision 8); the test allows order calls only there. |
| P8 | **Versioned parameters.** | Each detector and pattern definition has a `name@version` and a parameter dict, and these are stored with every result, so stored results stay reproducible. |
| P9 | **Providers at the edge.** | Only `data.providers` talks to a data source. `analysis`, `sim` and `research` read candles from the repository and never import a provider. Broker or terminal SDKs (e.g. `MetaTrader5`) are optional extras, lazily imported in one module, and absent from the Docker image and CI. |

P5, P6 and P8 come straight from SpicyStock practice (see REUSE_VS_REBUILD.md §4).
P4 adapts SpicyStock's minute-bar fill-ambiguity logic (`src/intraday.py` `resolve()`).

---

## 2. System overview

```
             ┌──────────────────────── Browser (static ES modules) ────────────────────────┐
             │  Terminal layout · Lightweight Charts · overlay primitives · panels · badges │
             └───────────────▲───────────────────────────────▲─────────────────────────────┘
                     REST (JSON envelopes)              WebSocket/SSE (replay & current ticks, later)
             ┌───────────────┴───────────────────────────────┴─────────────────────────────┐
             │ FastAPI app (goldscalp.api)  — routers, Pydantic schemas, security headers   │
             ├───────────────┬──────────────┬──────────────┬──────────────┬─────────────────┤
             │ data          │ analysis     │ research     │ sim          │ journal         │
             │ ingest/resamp │ structure,   │ pattern      │ orders, fill │ entries,        │
             │ quality       │ liquidity,   │ events &     │ model, acct, │ performance     │
             │ providers     │ FVG, OB,     │ evaluation   │ replay       │ analytics       │
             │               │ sessions, vol│              │              │                 │
             ├───────────────┴──────────────┴──────────────┴──────────────┴─────────────────┤
             │ core: config · clock · instrument spec · timeframes · kinds/provenance       │
             ├───────────────────────────────────────────────────────────────────────────────┤
             │ storage: SQLite (WAL) · numbered SQL migrations · repositories                │
             └───────────────────────────────────────────────────────────────────────────────┘
```

Market-data sources sit **outside** this stack. CSV files, the synthetic generator,
MT5 terminal exports and, later, any optional live adapter all enter through
`data.ingest`. Ingest normalizes and validates the candles and writes them to the
repository. Nothing else reads a source directly (§13).

Dependency direction is strictly downward. `analysis` imports nothing from `sim`,
`research` or `api`. `research` uses `analysis` and `sim`. `api` composes
everything. This avoids SpicyStock's hub-and-cycle problem: `pipeline.py` imports
34 of the other 45 modules, and 17 modules form a single import cycle, mostly
through function-level imports (ARCHITECTURE_AUDIT.md §3). An import-graph test
keeps GoldScalp acyclic.

---

## 3. Repository layout

```
GoldScalpAI/
├── pyproject.toml              # project metadata, ruff, pytest config
├── requirements.txt            # runtime direct deps, bounded below next major
├── requirements-dev.txt        # -r requirements.txt + pytest, ruff, httpx (TestClient)
├── constraints.txt             # full pinned resolution (SpicyStock pattern)
├── Dockerfile
├── docker-compose.yml
├── .env.example                # every variable documented; no real values
├── goldscalp/
│   ├── core/
│   │   ├── config.py           # Settings (pydantic-settings), GOLDSCALP_ prefix, strict parsing
│   │   ├── clock.py            # Clock protocol; SystemClock, FixedClock; refuses naive datetimes
│   │   ├── instrument.py       # XAUUSD spec: tick 0.01, contract 100 oz/lot, lot step 0.01, schedule
│   │   ├── timeframes.py       # M1, M5, M15, H1 (+ D1 context); bucket alignment rules
│   │   └── kinds.py            # DataKind enum, Envelope model, provenance helpers
│   ├── data/
│   │   ├── candles.py          # canonical frame schema + validate()/normalize()
│   │   ├── resample.py         # M1 → M5/M15/H1/D1 with completeness counts
│   │   ├── schedule.py         # expected trading minutes; weekly close & daily break
│   │   ├── quality.py          # coverage & gap report (expected vs unexpected gaps)
│   │   ├── ingest.py           # the ONLY write path: source → UTC → ticks → dedupe → validate → store
│   │   └── providers/
│   │       ├── base.py         # CandleSource protocol (read-only), SourceInfo, FetchReport, errors
│   │       ├── csv_file.py     # offline import; dialects: generic_utc, mt5_export, …
│   │       ├── synthetic.py    # deterministic generator for tests & demo mode
│   │       ├── (later) mt5_terminal.py  # OPTIONAL, Windows-only, read-only MetaTrader5 adapter (§14)
│   │       └── (later) <vendor>.py      # optional current/delayed HTTP feed, read-only
│   ├── analysis/
│   │   ├── swings.py           # pivot swings with confirmation lag
│   │   ├── structure.py        # BOS / CHoCH state machine
│   │   ├── liquidity.py        # equal highs/lows, PDH/PDL, session extremes, sweeps
│   │   ├── fvg.py              # fair value gaps + mitigation tracking
│   │   ├── order_blocks.py     # OB detection tied to displacement/BOS
│   │   ├── sessions.py         # Asian/London/NY windows (zone-aware), session stats
│   │   └── volatility.py       # ATR, ADR, realized vol, hour-of-day profile, regimes
│   ├── research/
│   │   ├── patterns.py         # pattern registry: name@version, params, rules
│   │   ├── events.py           # as-of event detection over history
│   │   ├── evaluate.py         # forward outcomes via sim.fill_model
│   │   └── stats.py            # Wilson/bootstrap CIs, sample-size gates, splits
│   ├── sim/
│   │   ├── orders.py           # market/limit/stop/stop-limit, SL/TP brackets
│   │   ├── costs.py            # spread & slippage models
│   │   ├── fill_model.py       # bar-based fills + intrabar ambiguity policy
│   │   ├── account.py          # balance, equity, margin, lot sizing
│   │   └── replay.py           # bar-by-bar replay session controller
│   ├── journal/
│   │   ├── service.py          # journal CRUD, tags, links to paper trades
│   │   └── analytics.py        # expectancy, PF, drawdown, breakdowns
│   ├── storage/
│   │   ├── db.py               # connection factory, PRAGMAs, transactions
│   │   ├── migrate.py          # applies migrations/NNNN_*.sql in order
│   │   ├── migrations/0001_init.sql
│   │   └── repositories/       # candles.py, paper.py, journal.py, research.py
│   └── api/
│       ├── app.py              # create_app(); static mount; middleware
│       ├── security.py         # CSP & security headers, optional token auth
│       ├── schemas.py          # Pydantic response/request models (all enveloped)
│       └── routers/            # health, market, analysis, sessions, research, paper, journal, data
├── integrations/
│   └── mt5/                    # (later, §14) README + read-only MQL5 export script; no trade calls
├── web/
│   ├── index.html
│   ├── css/tokens.css          # design tokens (black/gold)
│   ├── css/terminal.css
│   ├── js/
│   │   ├── main.js             # bootstrap, routing between panels
│   │   ├── api.js              # fetch wrapper: timeouts, envelope + kind validation
│   │   ├── chart/chart.js      # Lightweight Charts wrapper, timeframe switching
│   │   ├── chart/primitives/   # rectangle/zone/session-band primitives
│   │   ├── panels/             # structure, liquidity, sessions, volatility, paper, journal, analytics
│   │   └── ui/badges.js        # DataKind badges (single implementation)
│   └── vendor/lightweight-charts.standalone.production.js   # pinned exact version, vendored
└── tests/
    ├── conftest.py             # socket blocker, FixedClock, tmp SQLite, synthetic candles
    ├── unit/ property/ api/ fixtures/
    └── e2e/                    # optional Playwright smoke
```

---

## 4. Domain model: XAUUSD specifics

| Concern | Decision | Notes |
|---------|----------|-------|
| Symbol | `XAUUSD`, a single instrument at first | The schema keeps a `symbol` column so a second instrument (e.g. XAGUSD) is possible later. There is no universe logic. |
| Price precision | tick size `0.01` USD | Stored as **integer ticks** (`price × 100`) so float drift can't enter persistence. Some providers quote 3 decimals, so `price_scale` belongs to the instrument spec and is validated on import. |
| Contract | 1 standard lot = **100 troy oz** (configurable) | This is the common CFD convention, and it varies by venue. P&L per $1 move per 1.00 lot is $100. |
| Lot sizing | min 0.01, step 0.01 | Risk-based sizing rounds *down* to the step and refuses if the result is under the minimum. |
| Candle side | stored as metadata `bid` / `mid` / `ask` | Most free M1 history is bid-side. The fill engine derives ask = bid + spread for buys. |
| Volume | **tick volume** | It is labelled as such everywhere and never presented as traded volume. |
| Trading week | Sun ~18:00 to Fri 17:00 New York time, with a daily break of ~17:00–18:00 NY | Modeled on the COMEX/Globex-style schedule most spot and CFD venues follow. Configurable, because venue schedules differ. Holiday closures come from a small, versioned, reviewed list (a pattern from SpicyStock `knowledge/event-risk.json`). |
| Trading day | rolls at **17:00 America/New_York** | D1 context bars (PDH/PDL, ADR) use this rollover, DST-aware through `zoneinfo`. |
| Timestamps | UTC, offset-aware, bar **open** time | A naive datetime is refused (adapted from SpicyStock `sessions.market_time()`). |

### Timeframes and resampling
- The base series is **M1**. M5, M15 and H1 are resampled from M1 with UTC-aligned
  buckets (each divides the hour). Rules: `open=first`, `high=max`, `low=min`,
  `close=last`, `tick_volume=sum`, and `minutes_present` = count of M1 bars.
- A bucket with fewer minutes than the schedule expects is marked
  `complete=false`. It is shown, but detectors skip it by default (P5).
- The forming bar is `closed=false`. Detectors never read it (P2).
- Invariant: `resample(M1→H1) == resample(resample(M1→M15)→H1)`, held by a property test.

---

## 5. Analysis definitions (versioned, parameterized)

SMC/ICT terms are defined inconsistently across the trading community. Each
detector therefore pins **one documented definition**, exposes its parameters,
and stores `name@version` with every result. These are the v1 defaults:

| Detector | v1 definition | Known at (`confirmed_at`) |
|----------|---------------|---------------------------|
| **Swing high/low** (`swings@1`, params `left=k`, `right=k`, default k=3) | High[i] is strictly greater than the previous k highs and ≥ the next k highs (mirror for lows). Ties resolve to the earliest bar. | Close of bar `i+k` |
| **BOS** (`structure@1`, `break_on=close`) | In an established up-trend, a **close** above the last confirmed swing high continues the trend (mirror for down). Wick-only breaks are recorded as `sweep`, not BOS. | Close of the breaking bar |
| **CHoCH** | The first close beyond the last confirmed swing that *defends* the current trend (the most recent higher-low in an up-trend). It flips the trend state. | Close of the breaking bar |
| **FVG** (`fvg@1`, `min_size_atr=0.1`) | Bullish when `low[i] > high[i-2]`, zone `[high[i-2], low[i]]`. Bearish is the mirror. Mitigation states: `open → partially_filled → filled`; `invalidated` when a bar closes beyond the far edge. | Close of bar `i` |
| **Order block** (`ob@1`) | The last opposite-colour candle before a displacement leg (body ≥ `x·ATR`) that produces a BOS. Zone = that candle's range (option: body only). | Close of the BOS bar, **not** the OB candle |
| **Liquidity** (`liquidity@1`) | Equal highs/lows: ≥2 confirmed swings within `tol = 0.1·ATR`. Plus PDH/PDL, prior-week H/L, Asian range H/L and round numbers ($X,X00 / $X,X50). **Sweep**: a wick through the level and a close back inside within N bars. | Per component |
| **Sessions** (`sessions@1`) | Asian 09:00–18:00 `Asia/Tokyo`; London 08:00–17:00 `Europe/London`; New York 08:00–17:00 `America/New_York`. Overlaps are derived. | Session end (stats); live while open |
| **Volatility** (`vol@1`) | ATR(14, Wilder) per timeframe; ADR(20) on 17:00-NY days; realized vol of log returns per session/day; hour-of-day median range profile; regime = percentile of ATR vs trailing 90 days. | Bar close |

Sessions are defined in their **local zones**, not as fixed UTC hours, because the UK
and US change clocks on different dates (about 2–3 weeks apart in March and 1 week in
Oct/Nov). Fixed UTC windows would mislabel those weeks. All windows are
configurable, and the chosen definitions are shown in the UI.

---

## 6. Simulation engine (paper trading, replay, evaluation)

### Modes
| Mode | Data | Who places orders | Kind label |
|------|------|-------------------|------------|
| **Replay** | Historical bars revealed one at a time | The user | `hypothetical` (on `historical` bars) |
| **Forward paper** | Current/delayed feed (phase 9) | The user | `hypothetical` (on `current` bars) |
| **Pattern evaluation** | Historical bars | Pattern rules, offline only | `hypothetical` |

Nothing places orders automatically against current data. Rule-driven orders exist
only inside historical evaluation runs.

### Fill model (`sim.fill_model`, adapted from SpicyStock `intraday.resolve()`)
- **Market order** placed at time *t* fills at the **open of the next bar** ± spread/slippage. It never fills at the bar whose close the user saw.
- **Buy stop**: fills at `max(stop, bar.open_ask)`, so a gap through the trigger fills at the open, which is worse. **Sell stop** is the mirror.
- **Buy limit**: fills at `min(limit, bar.open_ask)`, so a gap through fills at the open, which is better. **Sell limit** is the mirror.
- **Stop-limit**: if the triggering bar opened beyond the limit and nothing trades back to it, there is no fill. If it trades back, the fill price is unknowable from the bar, so it is `ambiguous`.
- **SL and TP both reachable in one bar**: first drill down to M1 if the series is coarser. If still ambiguous, book **SL** and set `ambiguous=True`.
- **Entry and SL in the same bar**: same procedure.
- **Weekend/daily-break gap**: pending stops fill at the first post-gap open, and the slippage is recorded.
- Long positions close at bid and shorts at ask. Costs come from `sim.costs`: fixed spread, or a per-hour spread profile and optional slippage in ticks. Swap/financing defaults to 0 and can be configured.

### Account
- Balance, equity, used margin (`notional / leverage`), free margin, and an optional educational stop-out level.
- Risk-based sizing: `lots = floor_step(risk_usd / (|entry − stop| × contract_oz))`. This mirrors SpicyStock's conservative "an unknown commitment never becomes free capacity" rule (`src/allocation.py` `reservation()`).

---

## 7. Research: historical pattern detection and evaluation

1. A **pattern** is `name@version` plus params: a detection rule (built from §5
   detectors), plus entry, stop, target and time-exit rules.
2. **Events** are detected as-of: an event's `signal_at` is the close of the bar that
   completes it, and all its inputs have `confirmed_at ≤ signal_at`.
3. **Evaluation** runs each event through `sim.fill_model` with an explicit cost model.
   Per event it produces: filled?, R-multiple, MFE/MAE (in R and $), bars to exit, exit reason and an ambiguity flag.
4. **Statistics**: n, win rate with Wilson 95% CI, mean R with bootstrap CI, profit
   factor, expectancy, max losing streak, and breakdowns by session, weekday and
   volatility regime. **In-sample / out-of-sample split by date** is the default.
5. **Guards**: results with n below a configurable minimum (default 30) are shown as
   "insufficient sample". Every run stores the number of parameter variants tried
   for that pattern, so the UI can warn about multiple testing. Results are
   labelled "historical simulation, not a forecast".

---

## 8. Persistence (SQLite)

- One file at `GOLDSCALP_DB_PATH` (default `./var/goldscalp.sqlite3`; `/data/` in Docker).
- PRAGMAs on every connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`.
- Migrations: numbered `NNNN_description.sql` files, applied in order inside a transaction and recorded in `schema_migrations`. A migration is never edited after release.
- All SQL is parameterized. The repository layer returns plain dataclasses or DataFrames.
- Transactions follow SpicyStock `morning_service.Store._transaction()`. Writes open
  with `isolation_level=None` and an explicit `BEGIN IMMEDIATE`, roll back on any
  `BaseException`, and always close. Read-only views open a `file:…?mode=ro` URI.
  SpicyStock had no PRAGMAs and no migration scheme; both are added here.

```sql
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);

CREATE TABLE data_imports (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL,            -- e.g. 'csv:generic_utc', 'csv:mt5_export', 'synthetic'
  source_symbol TEXT NOT NULL,                             -- as the source spelled it ('XAUUSD.m', 'GOLD', …)
  time_basis TEXT NOT NULL,                                -- 'utc' | 'fixed:+02:00' | 'rule:ny_close' (§13)
  price_scale INTEGER NOT NULL,                            -- source decimals before conversion to ticks
  file_name TEXT, file_sha256 TEXT NOT NULL UNIQUE,
  candle_side TEXT NOT NULL CHECK (candle_side IN ('bid','mid','ask')),
  rows_read INTEGER NOT NULL, rows_stored INTEGER NOT NULL, rows_rejected INTEGER NOT NULL,
  first_ts INTEGER, last_ts INTEGER, report_json TEXT NOT NULL, imported_at TEXT NOT NULL);

CREATE TABLE candles_m1 (                 -- base series; higher TFs resampled (optionally cached)
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,           -- bar open, epoch seconds UTC
  o INTEGER NOT NULL, h INTEGER NOT NULL, l INTEGER NOT NULL, c INTEGER NOT NULL,  -- integer ticks
  tick_volume INTEGER, spread INTEGER,                 -- spread in ticks when the source records it (MT5 does)
  import_id INTEGER NOT NULL REFERENCES data_imports(id),
  CHECK (l <= o AND l <= c AND h >= o AND h >= c AND l > 0),
  PRIMARY KEY (symbol, ts)) WITHOUT ROWID;

CREATE TABLE pattern_definitions (id INTEGER PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
  params_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (name, version));
CREATE TABLE evaluation_runs (id INTEGER PRIMARY KEY, definition_id INTEGER NOT NULL REFERENCES pattern_definitions(id),
  timeframe TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL, split_ts INTEGER,
  cost_model_json TEXT NOT NULL, data_fingerprint TEXT NOT NULL, variants_tried INTEGER NOT NULL,
  summary_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE evaluation_events (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
  signal_ts INTEGER NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('long','short')),
  filled INTEGER NOT NULL, entry INTEGER, stop INTEGER, target INTEGER, exit INTEGER, exit_ts INTEGER,
  exit_reason TEXT, r_multiple REAL, mfe_r REAL, mae_r REAL, ambiguous INTEGER NOT NULL DEFAULT 0,
  sample TEXT NOT NULL CHECK (sample IN ('in','out')), features_json TEXT);

CREATE TABLE paper_accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
  starting_balance_cents INTEGER NOT NULL, leverage INTEGER NOT NULL, contract_oz INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL);
CREATE TABLE paper_orders (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  mode TEXT NOT NULL CHECK (mode IN ('replay','forward')), side TEXT NOT NULL CHECK (side IN ('buy','sell')),
  type TEXT NOT NULL CHECK (type IN ('market','limit','stop','stop_limit')),
  lots_x100 INTEGER NOT NULL CHECK (lots_x100 > 0), price INTEGER, limit_price INTEGER,
  stop_loss INTEGER, take_profit INTEGER, status TEXT NOT NULL, placed_at_ts INTEGER NOT NULL,
  data_kind TEXT NOT NULL CHECK (data_kind IN ('historical','current')), created_at TEXT NOT NULL);
CREATE TABLE paper_fills (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES paper_orders(id),
  ts INTEGER NOT NULL, price INTEGER NOT NULL, lots_x100 INTEGER NOT NULL, spread_cost_cents INTEGER NOT NULL,
  slippage_ticks INTEGER NOT NULL, policy TEXT NOT NULL, ambiguous INTEGER NOT NULL DEFAULT 0);
CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  entry_fill_id INTEGER NOT NULL REFERENCES paper_fills(id), exit_fill_id INTEGER REFERENCES paper_fills(id),
  pnl_cents INTEGER, r_multiple REAL, mfe_r REAL, mae_r REAL, exit_reason TEXT);

CREATE TABLE journal_entries (id INTEGER PRIMARY KEY, trade_id INTEGER REFERENCES paper_trades(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL, session TEXT, setup TEXT, thesis TEXT, review TEXT, rating INTEGER CHECK (rating BETWEEN 1 AND 5));
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE journal_entry_tags (entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id), PRIMARY KEY (entry_id, tag_id));
```

Size check: M1 XAUUSD is about 23 h × 60 × 5 days × 52 weeks ≈ **360 k rows/year**, so 10 years
is about 3.6 M rows. That is comfortable for SQLite with a `WITHOUT ROWID` primary key and
range scans. Higher timeframes are resampled on demand and cached in memory, keyed by
`(symbol, tf, range, data_fingerprint)`.

---

## 9. API surface (FastAPI)

Every response uses the same envelope:

```json
{ "kind": "historical", "source": "csv:histdata-2025", "as_of": "2026-11-13T21:00:00Z",
  "generated_at": "2026-11-13T21:00:03Z", "definitions": {"fvg": "fvg@1"}, "data": { ... } }
```

| Area | Endpoints |
|------|-----------|
| Health | `GET /api/health` (liveness only: says nothing about data freshness), `GET /api/status` (coverage, last import, DB version) |
| Market | `GET /api/instrument`, `GET /api/candles?tf=M5&start=&end=&limit=` |
| Analysis | `GET /api/analysis/{structure,liquidity,fvg,order-blocks}?tf=&start=&end=&as_of=` |
| Sessions & vol | `GET /api/sessions?date=`, `GET /api/sessions/stats?from=&to=`, `GET /api/volatility?tf=&from=&to=` |
| Research | `GET /api/patterns`, `POST /api/evaluations` (background job), `GET /api/evaluations/{id}` |
| Paper | `POST /api/paper/accounts`, `GET /api/paper/accounts/{id}`, `POST/DELETE /api/paper/orders`, `GET /api/paper/positions`, `POST /api/paper/replay/{id}/step` |
| Journal | CRUD `/api/journal/entries`, `GET /api/analytics/performance?account=` |
| Data | `POST /api/data/import` (CSV, size-capped), `GET /api/data/coverage` |
| Stream (later) | `WS /ws/replay/{id}`, `WS /ws/current` |

---

## 10. Frontend: premium dark trading terminal

- **No framework, no build step.** Native ES modules served by FastAPI `StaticFiles`.
  **Lightweight Charts is vendored at a pinned exact version** (v5 API: `addSeries`,
  panes, series primitives), and its attribution is kept as its licence requires.
- **Layout** (CSS grid): top bar (instrument, last price, **data-kind badge**, session
  clock); left rail (timeframes M1/M5/M15/H1, overlay toggles); centre chart; right
  analysis panel (trend state, last BOS/CHoCH, nearest liquidity, active FVG/OB, session
  stats, volatility regime); bottom dock tabs (Paper ticket · Positions · Orders ·
  Journal · Analytics · Research).
- **Overlays** are series primitives: rectangles for FVG/OB zones, horizontal rays for
  liquidity levels, background bands for sessions, and markers for BOS/CHoCH and swings.
  Each overlay shows its definition version on hover.
- **Design tokens** (`web/css/tokens.css`):

  | Token | Value | Use |
  |-------|-------|-----|
  | `--bg` | `#000000` | page |
  | `--surface-1/2/3` | `#0b0b0c` / `#121214` / `#1a1a1d` | panels, raised panels, inputs |
  | `--line` | `#26262a` | hairlines, grid |
  | `--gold-500` | `#d4af37` | primary accent, active state |
  | `--gold-300` | `#e6c868` | hover/focus, highlights |
  | `--gold-700` | `#9c7c1c` | muted accent, borders |
  | `--text-1/2/3` | `#f2efe6` / `#b9b4a6` / `#7d7869` | primary, secondary, tertiary |
  | `--up` / `--down` | `#2bb594` / `#e5484d` | candles, P&L |
  | `--kind-historical` / `--kind-hypothetical` / `--kind-current` | slate / violet / gold | data-kind badges |

  Typography: a self-hosted sans for UI and a monospace with `font-variant-numeric:
  tabular-nums` for prices. No external font CDN, so CSP stays `self`.
- **Kind labelling in the UI**: a badge on every panel header; the paper ticket and
  all simulated results carry a "SIMULATED — no real orders" strip; current data shows
  its provider delay and `as_of`; research results show the sample period, n and CI.
- **Rendering safety**: data-derived text goes through `textContent` and DOM
  construction only. No `innerHTML` with data. Any URL taken from data must pass a
  scheme allowlist (`https:` or same-origin). SpicyStock missed this in two places;
  see audit §8.
- **Lessons taken from the SpicyStock frontend** (learn-from, not copied):
  - **One `availability()`-style answer.** Data freshness and "is this actionable"
    are separate states, and the answer may narrow but never widen.
  - **Request discipline.** Every request gets an abort deadline. A sequence/epoch
    guard stops a late response from overwriting newer state. A failed refresh keeps
    the current view, and each outcome is announced in a `role=status` line.
  - **Chart lifecycle.** A chart host exposes `update()`/`dispose()` and a leak
    counter, uses namespaced ids so several charts (M1/M5/M15/H1) can coexist, and has
    an accessible summary label plus a **table twin**, because Lightweight Charts draws
    to a canvas.
  - **Overlays never move the price scale.** Zone primitives return no autoscale
    info ("a highlight is a marker, never a level").
  - **Figure-basis classes** for recorded vs estimated vs unreported values; a
    missing value is shown as missing, never as 0.
  - **ES modules with explicit imports.** This replaces SpicyStock's single
    5,930-line closure, a shared mutable global namespace and load-order coupling.

---

## 11. Configuration, security and deployment

| Area | Decision |
|------|----------|
| Config | `pydantic-settings`, prefix `GOLDSCALP_`, strict types; an unknown value raises (P6). `.env.example` documents every variable. |
| Secrets | MVP needs **none** (CSV + synthetic data). Optional provider keys live in env only, are read at call time, are never logged and are never sent to the browser. |
| Network exposure | Binds to `127.0.0.1` by default; compose publishes `127.0.0.1:8000:8000`. Exposing beyond localhost requires `GOLDSCALP_AUTH_TOKEN` (bearer) and is documented as out of scope for the MVP. |
| HTTP headers | CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. |
| Uploads | CSV import is size-capped and streamed; rows are validated; nothing is executed or unpickled; the file hash is recorded. |
| Outbound network | Off by default. Provider adapters use explicit connect/read timeouts and classify permanent refusals separately from transient errors (pattern from SpicyStock `market_data._is_permanent_refusal()`). |
| Container | `python:3.12-slim` pinned by digest; non-root user; read-only app files; writable `/data` volume only; `HEALTHCHECK` on `/api/health`; single uvicorn worker (single SQLite writer). |
| Supply chain | Direct deps bounded below their next major, with the full resolution pinned in `constraints.txt`. CI runs gitleaks with default rules. |
| Privacy | No personal identifiers in code, User-Agent strings, fixtures or docs (SpicyStock embeds an owner contact email in a User-Agent; see audit §8). |

---

## 12. Testing strategy

| Layer | Tests |
|-------|-------|
| Core | Naive datetime refused; config typo raises; instrument tick/lot rounding. |
| Data | OHLC invariants; duplicate timestamps keep the last copy (stable sort); gap classification (weekend vs break vs missing); resample composition property. |
| Analysis | Golden hand-built candle cases per detector; **truncation invariance**: results on `candles[:i+1]` equal results on the full series filtered to `confirmed_at ≤ t_i` (proves no look-ahead); DST-week session boundaries. |
| Sim | A scenario table for every fill rule in §6, including gaps, same-bar SL/TP and stop-limit no-fill. Conservation: balance + open P&L − costs = equity. |
| Research | Evaluation goes through `fill_model` (a monkeypatch spy proves it). In/out-of-sample split never overlaps. Small-n gate. |
| API | Every route's response model includes `kind` (iterates `app.routes`). CSP header present. 413 on oversize import. |
| Safety | **No broker execution**: a test scans `goldscalp/` and `integrations/` and fails on order-placement SDK calls or endpoints (`order_send`, `OrderSend`, `CTrade`, …). *As built:* the demo-only gateway `goldscalp/execution/mt5_demo.py` is the one file allowed MT5 order and account calls (Revision 8). **Sockets blocked** in the test session (autouse fixture, as in SpicyStock `tests/conftest.py` `_no_network`). |
| Boundaries | Import-graph test: `analysis`, `sim` and `research` never import `data.providers`. Importing `goldscalp.api.app` succeeds with `MetaTrader5` made unimportable (`sys.modules["MetaTrader5"] = None`). |
| MT5 (offline) | `mt5_export` dialect golden files, including a server-time file that spans a US DST change; the schedule-fit check refuses a file imported with the wrong `time_basis`; a fake `MetaTrader5` module (numpy structured arrays) exercises the future adapter with no terminal. |
| Frontend | Optional Playwright smoke: chart renders, timeframe switch, overlay toggles, kind badges present, no console errors. |

Discipline carried over from SpicyStock: **prove a test can fail**. Break the rule and
watch the test go red, then break an unrelated rule and confirm it stays green.

---

## 13. Market-data ingestion interfaces

This section covers requirements 1 and 2 of the MT5 brief: providers stay separate
from analysis, and ingestion has a clear interface.

### 13.1 The boundary
```
 CandleSource (read-only)  ──fetch──▶  data.ingest  ──upsert──▶  storage (candles_m1, data_imports)
   csv_file / synthetic /               normalize · validate ·          ▲
   (later) mt5_terminal / http          classify gaps · record          │ read-only
                                                                        │
                       analysis · research · sim · api  ────────────────┘   (never touch a source)
```

### 13.2 Interface (`goldscalp/data/providers/base.py`)
```python
class Side(StrEnum):
    BID = "bid"; MID = "mid"; ASK = "ask"

@dataclass(frozen=True)
class SourceInfo:
    source_id: str          # "csv:generic_utc", "csv:mt5_export", "synthetic:seed=7", later "mt5:<profile>"
    candle_side: Side       # MT5 FX/CFD bars are normally bid-based
    source_symbol: str      # as the source spells it: "XAUUSD", "XAUUSD.m", "GOLD", …
    time_basis: str         # "utc" | "fixed:+02:00" | "rule:ny_close" (UTC+2, UTC+3 while US DST is in force)
    price_scale: int        # decimals in the source; converted to instrument ticks on ingest
    licence_note: str | None

@dataclass(frozen=True)
class CandleRequest:
    symbol: str             # canonical "XAUUSD"; the source maps it to source_symbol
    timeframe: Timeframe    # M1 is the stored base; a source may supply M5+ for research-only imports
    start: datetime         # offset-aware UTC, inclusive
    end: datetime           # offset-aware UTC, exclusive

@dataclass
class FetchReport:          # cf. SpicyStock market_data.DownloadStats
    rows_read: int
    rows_rejected: int
    duplicates_dropped: int
    rejected_samples: list[str]          # bounded, first N reasons
    first_ts: datetime | None
    last_ts: datetime | None
    requested: tuple[datetime, datetime]
    received: tuple[datetime, datetime] | None

class CandleSource(Protocol):            # read-only by construction: there is no write, order or account method
    def info(self) -> SourceInfo: ...
    def fetch(self, request: CandleRequest) -> tuple[pd.DataFrame, FetchReport]: ...

class ProviderError(Exception): ...
class CredentialsRejected(ProviderError): ...   # permanent: fix credentials; do not retry
class NotEntitled(ProviderError): ...           # permanent: plan/symbol/feed not available
class SourceUnavailable(ProviderError): ...     # transient: bounded retry with backoff
class MalformedSource(ProviderError): ...       # permanent for this file/response
```
The error taxonomy separates "fix the key" from "fix the plan" from "try again",
following SpicyStock `market_data._is_permanent_refusal()` / `_refusal_error()`.

### 13.3 Ingest pipeline (`goldscalp/data/ingest.py`)
1. **To UTC** using `time_basis`. Local or server wall-clock times that are
   nonexistent or ambiguous across a DST change are refused (the rule from SpicyStock
   `morning_halts._eastern()`).
2. **To ticks** using `price_scale` and the instrument tick size. A series that mixes
   scales is refused.
3. **Stable sort, then drop duplicate timestamps keeping the last copy** (SpicyStock
   `market_data._download_batch()`).
4. **Validate** each row: finite, positive, and `l ≤ min(o,c) ≤ max(o,c) ≤ h`
   (SpicyStock `market_data.readable_pair()`, vectorized). Rejected rows are counted
   and sampled; nothing is repaired.
5. **Schedule-fit check.** After conversion, bars must sit inside the instrument's
   trading schedule (weekly open/close, daily break). If more than a small threshold
   of bars land inside the expected break, the import is refused with a message such
   as "time basis is probably wrong (bars appear offset by ~N hours)". A wrong MT5
   server offset is the most common import error, and this check catches it.
6. **Store** in one transaction: upsert `candles_m1` and record `data_imports`
   (source, alias, basis, scale, file SHA-256, report). The import is idempotent by
   file hash.

### 13.4 Sources at MVP
| Source | Dialects / notes |
|--------|------------------|
| `CsvFileSource` | `generic_utc`: `timestamp,open,high,low,close[,tick_volume][,spread]` with ISO-8601 offsets. `mt5_export`: the MT5 terminal's bar export (tab-separated `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`, server time). Further dialects are added per data vendor, each declaring its time basis. |
| `SyntheticSource` | Seeded, deterministic M1 generator with a 24×5 schedule, daily break, session-dependent volatility and planted patterns (FVG, sweep, BOS) for tests and the demo. The seeding approach follows SpicyStock `tests/synthetic.py` `seed_for()`. |

---

## 14. MetaTrader 5 compatibility (originally "future, not implemented")

This section covers requirements 3–7 of the MT5 brief. *Status after Phase 1.1:* point **A** (bar-export import) and point **C** (read-only Python adapter, in-process on Windows) are built; B, D and E are not. *Since 14 November 2026* the optional demo-account execution (Revision 8) is a deliberate, opt-in exception to §14.3; see the note there. GoldScalp must install, run and pass its tests on Linux, in Docker and in CI,
with no MT5 terminal and no `MetaTrader5` package.

### 14.1 Guarantees the MVP keeps so MT5 can be added later
1. **No hard dependency.** `MetaTrader5` is never in `requirements.txt`. If it is ever
   added, it is an optional extra (`goldscalp[mt5]` → `MetaTrader5; sys_platform == "win32"`).
2. **One import site.** Only `goldscalp/data/providers/mt5_terminal.py` may import
   `MetaTrader5`, and only lazily inside its functions. Tests enforce this (§12).
3. **The schema already fits MT5 data**: `tick_volume`, an optional per-bar `spread`,
   `candle_side='bid'`, `source_symbol` aliases, `time_basis` and `price_scale` (§8, §13).
4. **The instrument spec is data, not code.** A broker's symbol properties (digits,
   point, contract size, minimum volume, volume step) can populate a stored
   *instrument profile* without any change to the simulator.
5. **The simulator is broker-independent.** `sim` imports neither providers nor any
   broker SDK. Fills come only from stored candles plus the cost model, so a broker
   connection cannot influence a simulated fill.

### 14.2 Integration points, lowest risk first
| | Integration point | Direction | MT5 needed on the GoldScalp host? | Notes |
|---|---|---|---|---|
| **A** | Import an MT5 **terminal bar export** (`mt5_export` CSV dialect) | MT5 → GoldScalp (file) | **No** | MVP-ready through the CSV importer. The user exports bars from the terminal and declares the server time basis at import; the schedule-fit check verifies it. Covers requirement 4 with zero MT5 code. |
| **B** | **MQL5 export script** (`integrations/mt5/GoldScalpExport.mq5`) | MT5 → file | No (runs inside the user's terminal) | A *script*, not an Expert Advisor. It uses `CopyRates`/`CopyTicksRange` and `FileWrite` to write the `mt5_export` format, plus a header recording symbol, digits, point, contract size and the observed server-time offset. It calls no trade functions. |
| **C** | **Python `MetaTrader5` read-only adapter** (`providers/mt5_terminal.py`) | MT5 → GoldScalp | Yes: Windows, with the terminal running on the same machine | It runs **on the Windows host, outside Docker**, as `python -m goldscalp.tools.mt5_pull`. It calls only `initialize`, `shutdown`, `last_error`, `version`, `terminal_info`, `symbol_info`, `symbol_select`, `copy_rates_range`, `copy_rates_from_pos` and `copy_ticks_range`. It returns the same `(frame, FetchReport)` as any `CandleSource` and hands it to `ingest`, writing to the local SQLite file or posting to `POST /api/data/import`. |
| **D** | **Forward paper trading on MT5 prices** | MT5 → GoldScalp | Yes | Polls `symbol_info_tick` / `copy_ticks_from` for bid/ask, labelled `kind=current` with source and observed delay. Orders stay **local simulations** in `sim`. |
| **E** | **Show GoldScalp analysis inside MT5** | GoldScalp → MT5 (file) | Yes (terminal only) | GoldScalp exports levels and zones (liquidity, FVG, OB, session ranges) as CSV. An MQL5 *indicator* reads them with `FileOpen` and draws chart objects. MQL5 indicators cannot call `WebRequest`, so file exchange is the indicator-compatible route. Display only. |

### 14.3 Excluded in every phase
- **Python**: `order_send`, `order_check`, `order_calc_margin`, `order_calc_profit`,
  `positions_get`, `positions_total`, `orders_get`, `orders_total`,
  `history_orders_get`, `history_deals_get`.
- **MQL5**: `OrderSend`, `OrderSendAsync`, the `CTrade` class and any position or order
  management. No trading Expert Advisors.
- **`account_info()`.** Login, balance and equity are personal financial data and
  research does not need them.
- **Stored MT5 credentials.** GoldScalp never stores an MT5 login, password or server.
  The adapter attaches to a terminal the user has already logged in to, passing only an
  optional terminal `path` to `initialize()`.
- **Enforcement.** The adapter wraps the module in an allowlist proxy that raises on
  any attribute outside the read-only list in 14.2-C. Tests assert that touching
  `order_send` raises, and a source-scan test fails if any excluded name appears in
  `goldscalp/` or `integrations/`.

> **Exception (14 November 2026).** The optional MT5 demo-account execution (Revision 8) may use
> `order_send`, `order_check`, `positions_get`, `history_deals_get` and `account_info`, only in
> `goldscalp/execution/mt5_demo.py` and only through its demo-only proxy. It is off by default, and it refuses
> any account that does not report itself as DEMO, re-checked before every order. Real-money orders, MQL5
> trading code and stored MT5 credentials remain excluded. The market-data adapter above is unchanged.

### 14.4 MT5 data pitfalls and how the design handles them
| Pitfall | Handling |
|---------|----------|
| Bar times are the **broker's trade-server wall-clock time**, even where APIs call them UTC. Many brokers run UTC+2 / UTC+3 following US DST so the day rolls at 17:00 New York, but this varies. | `time_basis` is mandatory on every MT5 import; `rule:ny_close` is supported; the schedule-fit check refuses a wrong offset. |
| Symbol names vary by broker (`XAUUSD`, `XAUUSD.m`, `XAUUSDm`, `GOLD`, …). | `source_symbol` is mapped to canonical `XAUUSD` per instrument profile. |
| `digits`/`point` vary (2 vs 3 decimals). | `price_scale` comes from the export header or `symbol_info().digits` and is converted to ticks. Mixed scales are refused. |
| Contract size, minimum lot and lot step vary by broker. | They live in the instrument profile; the default is 100 oz, minimum 0.01, step 0.01. |
| Bars are bid-based and `spread` is per bar, in points. | `candle_side='bid'`; the cost model uses per-bar `spread` when present and the configured spread otherwise. |
| History depth depends on the terminal's max-bars setting and the broker's server history. | `FetchReport.requested` vs `received` is recorded; gaps are flagged and never filled. |
| `tick_volume` is not traded volume. | It is labelled tick volume everywhere. |
| The Python package is Windows-only, needs a running 64-bit terminal, and wheels can lag new Python versions. | The adapter is optional and host-side; CI uses a fake module; Python version compatibility is checked when roadmap Phase M3 starts. |
| Broker data terms may restrict redistribution. | Imports record `licence_note`; GoldScalp never republishes imported candles. |

---

## 15. Open decisions (needed before or during Phase 1)

1. **Historical data source and licence.** *Resolved:* user-supplied CSV import
   (generic UTC, MT5 export, fixed-offset files such as HistData). Licence terms stay the
   user's responsibility per file.
2. **Current-data provider.** *Resolved in revision 2:* Tiingo FX (free data key, real-time)
   when configured, otherwise gold-api.com (keyless, DELAYED). See MARKET_DATA_PROVIDERS.md.
3. **Session definitions.** The local-zone defaults in §5 are implemented (`sessions@1`). They
   are not yet configurable from the UI.
4. **Paper account defaults.** *Resolved:* $10,000, USD, 1:1, configured spread $0.30,
   commission $3.50 per lot per side, slippage $0.05, all overridable in `.env` and per account.
   The contract size comes from the instrument file.
5. **Deployment target.** Local-only by default (`127.0.0.1`), which the Tiingo free licence also requires.
6. **MT5 broker profile.** Still open: needed only to build a golden fixture from a real broker export.

---

## 16. Optional TradingView Advanced Chart (display only)

Details and the documentation review are in MARKET_DATA_PROVIDERS.md §5.

```
Dashboard (strict CSP: frame-src 'self')
  └─ <iframe src="/tv-widget?symbol=OANDA:XAUUSD&interval=5">    created only on click (or opt-in auto-load)
        /tv-widget page: CSP allows ONLY *.tradingview.com / *.tradingview-widget.com
        └─ official Web Component <tv-advanced-chart>            TradingView data stays in this document
  ◀── postMessage {status: loaded|failed}     ──▶ postMessage {interval: 1|5|15|60} (allow-listed)
```

- **Separation.**
  - No route, module or test fixture moves TradingView data into Python, analysis, alerts or the
    simulator. GoldScalp's own chart and analysis use only provider and CSV data.
  - Enforced by `tests/test_safety.py`, which scans for TradingView references outside the display
    route and checks that no frontend module other than `tradingview.js` touches the frame.
- **Isolation.** Default mode is a separate document whose CSP has no `'self'` in `connect-src`, so
  the widget page cannot call the GoldScalp API (verified). The opt-in `GOLDSCALP_TRADINGVIEW_SANDBOX`
  adds an opaque-origin sandbox; it is unverified.
- **Failure handling.** If the widget fails to load, only its panel shows a message; nothing else
  depends on it.
