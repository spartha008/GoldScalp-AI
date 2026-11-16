# GoldScalp AI

Educational XAUUSD (gold / US dollar) market analysis and **lot-based paper trading**.

> **No real-money trading.** GoldScalp has no real-money order path and never asks for or stores broker
> logins. MetaTrader 5 is used as a **read-only source of prices and candles** from a terminal you have
> already logged in to, and every strategy position is **simulated** and labelled as such. The one
> exception is an optional feature, **off by default**, that can mirror simulated trades as orders on an
> MT5 **demo** account only ([MT5 demo-account execution](#mt5-demo-account-execution-optional-off-by-default)).
> Nothing here is financial advice.

## What it does (Phase 1)

- **Live XAUUSD prices.**
  - The **LIVE / DELAYED / HISTORICAL / DISCONNECTED** state is shown, with the reason.
  - Also shown: source, price time and age, bid/ask and the spread (when the source provides them),
    and connection and reconnect status.
- **Candlestick chart** (1m / 5m / 15m / 1h) with swing and BOS/CHoCH markers, gap (missing-candle)
  reporting and a data-table view.
- **Sessions** (Asian / London / New York, DST-correct) and **volatility** (ATR, ADR, realized).
- **Paper Trading page**, sized in lots:
  - Standard / Mini / Micro / Custom lot sizes, with a **configurable contract size** in troy ounces.
  - Bid/ask, spread, commission and slippage.
  - Balance, equity, used and free margin, unrealized and realized P/L.
  - Open and closed positions, a balance ledger and an equity curve.
- **Historical CSV import:** generic UTC CSV, MetaTrader 5 bar exports and HistData M1 files.
- **MetaTrader 5 live data (Phase 1.1):** read-only bid/ask ticks and M1–H1 candles from your local
  terminal, plus a provider selector, a Reconnect button and an MT5 status panel.
- **Optional TradingView chart:** display only, loads on click, and never feeds GoldScalp's data pipeline.

## Monthly Backtest (Phase 5)

Monthly multi-timeframe research on historical XAUUSD candles. It runs in a **separate research service**, so the
live Phase 4 monitors are never restarted or slowed:

```
python research.py        # or: python -m goldscalp.monthly   ->  http://127.0.0.1:8001/monthly-backtest
```

- **Workflow.** Select a completed month and review data availability per timeframe. Research nine strategy
  candidates on data **strictly before** the month (1:1.5 and 1:2 risk-to-reward variants, chosen on that earlier
  data). Freeze the exact strategy versions, then run every strategy on **M15, M5 and M1 independently**.
- **Simulation.** Each run is its own hypothetical **$1,000 account** with **1:500 leverage** (margin calculation
  only). It uses the strategy's configured sizing, stop and take-profit, and the Phase 3 engine: next-open fills,
  candle spread, commission, slippage, stop-first collisions, gaps and margin rejections.
- **Reports.** Nine run reports, three timeframe comparisons and one combined report, exported as PDF, CSV and
  JSON. Any stored run can be reproduced from its configuration.
- **Isolation.** The service reads the application database **read-only**, writes only to
  `var/monthly/monthly.sqlite3`, never connects to MT5 and runs at below-normal priority. Months without
  sufficient data are reported as INCOMPLETE, never substituted.

- **Historical data import (Phase 5.1).** Section 1 of the page uploads MT5 bar exports into the research store
  only. Export them with *View → Symbols → Bars → Export Bars* (tab-separated, spread in points); use point size
  0.01 and time basis `rule:ny_close`.
  - *Preview and validate* checks the file without storing anything: timestamps (including DST), OHLC, duplicates,
    timeframe, spread, missing bars, the month's coverage and the five-day warm-up.
  - Importing the same file twice adds nothing.
  - Use one series for a month's M1, M5, M15 and H1 files: an evaluation reads every timeframe from one series.
  - Section 2 shows a month's coverage, missing dates and warm-up for any series.
- **Percentage-risk scenarios (Phase 5.1, hypothetical).** Section 4 creates a risk study from a finished
  fixed-lot baseline evaluation: three new evaluations that size every entry at 5 %, 7.5 % or 10 % of the simulated
  account's current equity.
  - Sizing uses the cost-inclusive planned loss at the stop, rounded down to 0.01 lot; 1:500 leverage is used only
    for the margin check.
  - That makes 27 A/B/C runs, each its own $1,000 account, with per-run, per-strategy, per-scenario and master
    reports.
  - **High-risk hypothetical simulation scenarios — not real-account settings or trading recommendations.** They
    are never applied to MT5, a broker or the live paper monitor.

- **Frozen pre-June validation (Phase 5.3).** The four Phase 5.2 candidates (R2, R1, R5 and R7 v2) and their
  originals are re-run, unchanged, on January-May 2026 history. The protocol is versioned and frozen
  (`validation@1`), and nothing is selected. Results are reported separately from Phase 5.2 and do not change its
  classifications. **Result:** none held up. Three are rejected and R1 v2 is about break-even (inconclusive); see
  PHASE_5_3_VALIDATION_REPORT.md and PHASE5_ARCHITECTURE.md §14.
- **New strategy discovery (Phase 6).** Three pre-registered research hypotheses: volatility regime, higher-timeframe
  structure and session behaviour. Each runs a simple baseline on development data (January–November 2026), gets at
  most three tool-built refinements, and is validated once on a sealed November 2023 – October 2025 block that
  development code cannot read. See PHASE_6_PROTOCOL.md.
  - **Outcome:** H6.1 not supported, H6.2 rejected and H6.3 inconclusive on development data. H6.4 (VOL-C outside
    contracting M15 volatility, registered after development with its origin disclosed) ran once on the sealed block
    and was **rejected**: −0.006 R/trade over 663 trades (`var/monthly/reports/h64-report.md`). Nothing is promoted.
- **Forward-data guard.** Research cannot read candles from 14 November 2026 00:00 UTC onward. Strategy Lab,
  Performance, improvement studies, Monthly Backtest and every research job refuse such a request with HTTP 409;
  nothing is ever truncated. Charts, the dashboard and live MT5 collection are unaffected. Unlocking needs a registered,
  versioned `forward-unlock@1` protocol. See FORWARD_DATA_GUARD_REPORT.md and `GET /api/research/forward-guard`.
- **Pre-Phase 7 screening tools (research only).** `goldscalp/research/indicators.py` (moving averages, RSI, ATR,
  Bollinger and Keltner bands, squeeze momentum, clock-time and trading-day helpers), `rulesim.py` (a rule simulator with
  clock-time entries and exits, stop-entry orders and reversals, using the bt@1 fills and costs) and
  `external_rules.py` (15 published rule sets). `python -m goldscalp.research.screening_ext --run` re-runs the stage 2
  screen; it can read neither the sealed block nor forward data. See PRE_PHASE_7_STAGE_2_SCREENING_REPORT.md.
- **Research candidates.** Step 6 of the page lists the published rule sets registered for a proper test later
  (`config/research/candidates/`, `GET /api/monthly/research-candidates`): P7-FIX and P7-TOD, each with its source, its
  rules exactly as screened, a fingerprint, its evidence and its caveats. A candidate is not validated.
  `python -m goldscalp.research.candidates --check` confirms the rules are unchanged; `--reproduce` re-runs the stored
  evidence.
  - **Strategy Lab form (16 November 2026, at the owner's request).** The builder gained a **clock-time** condition
    (times in UTC, New York or London time, so daylight saving is followed) and a stop type **none**. With them the
    candidates exist as Strategy Lab strategies (`config/research/candidates/LAB_STRATEGIES.json`): P7-FIX v1 as
    screened, P7-FIX v2 with a protective stop of 3.0 x ATR(14) on M15, and the two legs of P7-TOD. On the screen
    window the Lab forms place exactly the screened trades (20 of 20 each, `research/pre_phase7/lab-parity.json`).
    Existing strategy fingerprints did not change.
  - **Longer history.** P7-FIX as registered, over the 2026 history research had already used (1 January to 29 May and
    22 June to 13 November): 420 trades, +0.063 nominal R per trade, profit factor 1.17, +$607. It is uneven:
    -$168 over 224 trades in January to June, +$774 over 196 trades from July. The morning fix carries most of it.
    Exploratory, not validation (`research/pre_phase7/candidates-2026-history.json`; shown on the candidate card).
  - **Monitored and on demo.** At the owner's request (16 November 2026) P7-FIX **v3** is monitored in paper mode
    (monitor 5) and linked to MT5 demo execution: 5% of equity at risk per trade, stop 1.0 x ATR(14) on M15,
    target 2R, break-even at +1R, same entries and 30-minute exit. **v4** (the same with a 1.5R target) is monitored in
    paper mode only (monitor 6). v2 (3 x ATR stop, 0.02 lots) was unlinked from demo; its paper monitor 4 still
    runs as a baseline. This is observation on a demo account, not a validation and not a promotion.
- **Research library.** Step 5 of the page collects every Phase 5 and Phase 6 study, read-only, from the research
  store and the frozen report files (`GET /api/monthly/library`).
  - Pick up to eight results from any study to compare them side by side: pooled metrics, cumulative R by month, a
    monthly R heatmap, execution stress (costs ×1/×2/×3) and, for Phase 6, regime breakdowns.
  - "across" follows one strategy through every study, and the written research reports open in a built-in reader.
  - Classes are shown as each study recorded them. Incomplete and failed studies stay listed, and a sealed test
    appears only once it has finished.
- **Phase 5 strategy search closed.** Phase 5.3 is frozen. The four candidates are retired from promotion, and none
  goes to paper-monitor observation. Phase 4 is unchanged. The next research question is regime dependence, not
  more variants. See PHASE5_ARCHITECTURE.md §15 (`python -m goldscalp.monthly.closure --check`).

- **Strategy redesign and robustness research (Phase 5.2).** Section 10 runs every strategy (A, B, C and R1-R9)
  over every stored month (June to November 2026; M1 only where M1 history exists).
  - **Benchmarks:** the originals, unchanged.
  - **Exit experiments:** one exit dimension changed at a time.
  - **Redesigned research versions:** Example A v3 (trend continuation), B v3 (breakout acceptance), C v5
    (liquidity sweep reversal), and R1-R9 v2.
  - **Protocol:** variants are chosen on the development months only, checked with walk-forward folds, and
    evaluated once on hold-out months; every month is a separate $1,000 account.
  - **Outputs:** a verdict per strategy (consistent / positive but inconsistent / fails / insufficient sample), the
    weakness study, a PDF report and a CSV of every result.
  - Research versions are never written to the saved strategies: adopting one is a separate decision.
  - Details: [PHASE_5_2_RESEARCH_REPORT.md](PHASE_5_2_RESEARCH_REPORT.md).

Details: [PHASE_5_IMPLEMENTATION_REPORT.md](PHASE_5_IMPLEMENTATION_REPORT.md).

## Live Validation and Research Assistant (Phase 4)

Validate Phase 3 strategies against your Vantage MT5 demo terminal's live prices at
**http://127.0.0.1:8000/live**. Market data stays read-only and every trade is SIMULATED in the paper
simulator. No broker order is placed unless you switch on the optional demo-account execution below, which
works only with an MT5 demo account. Strategies change only when you approve a validated proposal.
Details: [PHASE_4_IMPLEMENTATION_REPORT.md](PHASE_4_IMPLEMENTATION_REPORT.md).

> **Archived on 15 November 2026.** The Phase 4 monitors (A v2, B v2, C v4) were stopped before they recorded any
> live evaluation, and the strategies are archived. Phases 1–6 are preserved read-only in
> `research_archive/phase_1_to_6/`; see [PHASE_1_TO_6_ARCHIVE_REPORT.md](PHASE_1_TO_6_ARCHIVE_REPORT.md) and verify
> with `python -m goldscalp.research_archive --check`.
>
> **Since 16 November 2026** one version is monitored again: P7-FIX v2 (monitor 4, paper mode, linked to MT5 demo
> execution), a pre-Phase 7 research candidate added at the owner's request. Monitors 1–3 stay stopped and archived.

- **Terminal verification.** A read-only checklist runs against the running terminal and stores its evidence:
  - the broker must be Vantage: from `terminal_info().company` on a Vantage-branded terminal, or, on the generic
    MetaQuotes build (which reports "MetaQuotes Ltd."), from the connected trade server (for example
    `VantageMarkets-Demo`);
  - the demo server name, read only from the `Server=` line of the terminal's own `config/common.ini` (no
    account data is read);
  - the gold symbol is discovered automatically;
  - a live tick, M1/M5/M15/H1 candles with integrity and cross-timeframe checks, and a disconnect / reconnect.

  Only a record produced by the real `MetaTrader5` package can be VERIFIED.
- **Strategy monitor.** You choose which strategy *versions* to monitor; each needs a completed Strategy Lab
  run. At every step-candle close the monitor uses the backtest's own as-of snapshot. Each signal is
  timestamped and records:
  - supporting and conflicting evidence, and the invalidation;
  - bid/ask/spread at generation, the source and its freshness.
- **Live paper validation.** In *paper* mode, every qualifying signal opens one SIMULATED position in that
  version's own validation paper account.
  - It uses the same fills and costs as the paper simulator, and exits on stop, target, time or a structure flip.
  - MFE and MAE are tracked from live quotes.
  - Each signal is also measured against the prices that followed, and against the backtest engine's replica
    of the same decision.
- **Journal and evaluations.**
  - One journal entry per signal, with filters (date, strategy, session, timeframe, outcome) and CSV export.
  - Every closed trade gets a permanent seven-question evaluation.
- **Improvement engine.** A study splits history into discovery | embargo | unseen holdout.
  - It finds weaknesses: false signals, sessions, volatility regimes, timeframe combinations, spread sensitivity,
    invalidations, and backtest-vs-live differences.
  - It proposes catalogue changes and validates each against the current version on the holdout.
  - Proposals are never adopted automatically; *Approve* saves a new version.
- **Research assistant.** Answers come from stored records and cite them. The optional Claude mode is off by
  default; it needs `GOLDSCALP_ASSISTANT_LLM_ENABLED=true` and `pip install -r requirements-ai.txt`, and it
  sends excerpts of stored data to Anthropic.

## MT5 demo-account execution (optional, off by default)

Monitored strategy versions can also trade on your **MT5 demo account**, so you can see real broker fills
next to the simulated ones. It is off until you switch it on, and it works only per version you link.

- **What happens.** Each SIMULATED validation trade of a linked version is also sent as a market order to the
  MT5 account your terminal is logged in to. The order uses the same side, volume, stop-loss and take-profit,
  and carries magic number `26111400` and the comment `GoldScalp T<trade>`. When the simulated trade closes
  (time exit, structure flip, simulated stop or target), the demo position is closed. If the broker closes it
  first (its stop-loss or take-profit), the closing deal is recorded.
- **Demo only.** Before **every** order, GoldScalp re-reads `account_info()` and refuses unless:
  - the account's trade mode is DEMO;
  - the trade server's name says demo;
  - it is the same server and login that you linked (the login is stored only as a hash).

  A real-money account is always refused, and the refusal disables every link.
- **Never late, never twice.** An entry is not sent more than 90 s after the simulated entry. A failed or
  rejected order is recorded and never retried. If an order's reply is lost, the position is adopted by its
  comment instead of being sent again.
- **Always a stop-loss.** A simulated trade without a stop-loss is never sent: it is recorded as refused. A
  strategy whose stop type is `none` can be backtested and monitored, but not mirrored.
- **Limits** (`.env`): 0.10 lots per order by default, 3 open GoldScalp demo positions, 30 points deviation. This
  installation allows 2.00 lots per order (`GOLDSCALP_DEMO_EXECUTION_MAX_LOTS=2.00`, set on 16 November 2026 for the
  owner's 5%-risk P7-FIX version) and gives simulated accounts the demo account's 1:500 leverage
  (`GOLDSCALP_PAPER_LEVERAGE=500`; it affects new accounts and Lab runs, not existing accounts).
- **Break-even stops.** A monitored version with break-even moves its SIMULATED stop to the entry once the trade has
  gone +trigger R in its favour, as the backtest does (one monitor cycle later instead of at a bar close). The demo
  position keeps its original stop-loss at the broker; GoldScalp closes it when the simulated trade closes.
- **Kill switch.** *Stop all demo execution* on the Demo Execution tab disables every link and can close every
  open GoldScalp demo position.
- GoldScalp still never asks for or stores MT5 credentials.

**To turn it on:**
1. In `.env`, set `GOLDSCALP_DEMO_EXECUTION_ENABLED=true`, then restart GoldScalp. The restart also applies
   database migration 0007, which only adds tables.
2. In MetaTrader 5, switch on the **Algo Trading** button. The terminal refuses orders without it.
3. Open **Live → Demo Execution**. The account must show `DEMO` and `RETAIL_HEDGING`. Then click **Link to demo**
   on each paper-mode monitored version and type `DEMO` to confirm.

### Dashboard order ticket (MT5 demo account)

The Dashboard has an **MT5 demo order ticket** under the candle chart, for your own market orders on the same demo
account (16 November 2026).

- **Set it up on the chart.** Choose Buy or Sell and the lots. The stop-loss and take-profit are lines on the candle
  chart: drag them, or type the prices.
- **P&L before you send.** Each line's label shows the USD result and the R multiple at that price, and the ticket
  shows the risk as a share of the balance. A BUY fills at the ask and its levels are hit by the bid (a SELL the other
  way round), so the spread is included; the broker's commission and swap are not.
- **Confirm.** *Review order* opens a summary (account, side, lots, entry, both levels and their results); only
  *Send ... to MT5 demo* in that dialog sends the order.
- **P&L after you send.** The open position's entry, SL and TP lines stay on the chart with its live P/L. Drag its SL or
  TP and click *Apply SL/TP*, or close it at market.
- **Safety.** It uses the demo-only gateway: the account is re-read before every order, and anything that is not the
  DEMO account shown on the ticket is refused. A stop-loss is required. It needs demo execution switched on and MT5's
  Algo Trading button, and it respects the lot limit.
- **Kept apart from strategy orders.** Ticket orders carry magic number `26111401` and the comment
  `GoldScalp manual M<id>`. The strategy mirror (magic `26111400`) never counts, adopts or closes them, and the ticket
  can change or close only its own positions. The kill switch on the Demo Execution tab does not close them.
- **Recorded.** Every request, result and refusal is stored (`manual_orders`, migration 0008).
- **API.** `GET /api/live/demo/manual`, `POST /api/live/demo/manual/orders`,
  `POST /api/live/demo/manual/positions/{ticket}/protection` and `POST /api/live/demo/manual/positions/{ticket}/close`.
  Code: `goldscalp/execution/manual.py`, `web/js/ticket.js`; tests: `tests/test_manual_ticket.py` (fake broker, no real
  order).

## Strategy Lab (Phase 3)

Build strategy configurations from Phase 2 observations and evaluate them on stored history at
**http://127.0.0.1:8000/lab**. Hypothetical research only: no orders are placed, and nothing is
automated. Details: [PHASE_3_IMPLEMENTATION_REPORT.md](PHASE_3_IMPLEMENTATION_REPORT.md).

- **Strategy Builder.**
  - Combine BOS/CHoCH, HH/HL/LH/LL, liquidity sweeps, FVG, order-block candidates, timeframe
    alignment, sessions, volatility regime, ATR range, spread and the Phase 2 signal.
  - A **clock-time** condition is true in the minutes after listed local times (UTC, New York or London), for rules
    that trade by the clock.
  - Conditions can be switched on or off, and evaluation timeframes and windows are configurable.
  - Strategies are versioned and fingerprinted, and can be exported or imported as JSON.
  - Three example templates are included; they are examples, not claims of profitability.
- **Backtesting Lab.** Deterministic and free of look-ahead: decisions use only candles closed at each
  step.
  - Entries fill at the next candle open, with spread, commission, adverse slippage and margin (the
    paper simulator's formulas).
  - Stops (structure, ATR, fixed or none), targets (R, ATR or fixed), time and structure-flip exits;
    exposure limits; fixed or risk-% sizing.
  - Gaps are handled. When one candle touches both stop and target, the ambiguity is explicit
    (stop-first by default, and flagged).
  - Results are reproducible, with dataset and result hashes.
- **Performance dashboard.**
  - Trades, win rate (with a 95% interval), gross profit/loss, net, profit factor, expectancy (R),
    maximum drawdown, MFE/MAE, holding time;
  - breakdowns by timeframe, session and volatility;
  - equity and drawdown charts.
  - Ratios are **withheld** below 30 trades.
- **Walk-Forward Lab.** Development, embargo and unseen evaluation windows, rolling or anchored.
  - Fixed mode, or selection among your strategies using development data only.
  - Every fold is shown, with out-of-sample totals and fold-to-fold variation.
- **Strategy Comparison.** Several strategies under identical assumptions on one shared timeline, with
  saved reports.
- **Trade Journal.**
  - A live strategy monitor (needs LIVE data), with a **user-confirmed** simulated paper entry through
    the existing paper simulator.
  - A journal with the strategy, signal, entry and exit assumptions, costs, outcome and invalidation
    reason.
- **Exports.** CSV trade reports, CSV summaries, JSON strategies and PDF research reports. Each states
  its dataset, range, assumptions and limitations.

## Smart Market Intelligence (Phase 2)

Phase 2 adds deterministic, explainable analysis. Every result is a rule-based **observation**
computed only from **closed** candles, and it shows the time it became knowable. Design notes:
[PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md). What was built, how it was tested and its limits:
[PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md).

- **Dashboard sections:**
  - Market structure;
  - Liquidity map;
  - Price action (FVG and order blocks);
  - Multi-timeframe (M1 / M5 / M15 / H1 side by side);
  - Session intelligence;
  - Signal centre;
  - Volatility;
  - a Performance analytics summary.
- **Chart annotations.** Each one can be toggled, and the settings are saved **per timeframe**:
  - HH/HL/LH/LL swings and BOS/CHoCH breaks;
  - liquidity levels: equal highs/lows, previous day and week high/low, session highs/lows;
  - "sweep?" markers;
  - fair value gaps, with partial or full mitigation;
  - order-block **candidates**;
  - developing (unconfirmed) items, drawn dimmed with "?".
- **Readable charts.**
  - The 1-minute chart starts with only BOS/CHoCH and liquidity, and at most 6 labels.
  - *Max labels* limits how many annotations are drawn; only those in view count.
  - Text is hidden when you zoom far out, and swing markers are hidden when you zoom out further.
  - *Swing sensitivity* changes the chart's swings only. The signal always uses the configuration.
- **Liquidity sweeps vs breaks.**
  - A level is a *potential sweep* only if price closes back inside it, on the same candle or within
    3 candles.
  - A close beyond the level with no return is a *break*, not a sweep.
- **Multi-timeframe summary** (H1 / M15 / M5 / M1). Shows each timeframe's structure, last break,
  volatility condition, open zones and data freshness, with plain-language explanations where
  timeframes disagree. A higher-timeframe candle that is still forming is excluded.
- **Sessions** (Asian / London / New York, each in its own timezone, DST-correct):
  - the current session and any overlap;
  - each session's range compared with recent sessions;
  - range breakouts.
- **Volatility.** ATR, ADR, rolling volatility, expansion or contraction, candle-range percentile, and
  body-to-range ratio, in price units and % of price.
- **Signal Centre.**
  - Classifies the current evidence as BULLISH, BEARISH, NEUTRAL or NO SIGNAL.
  - Lists the supporting and conflicting evidence, the invalidation levels, the spread and volatility
    context, the source and freshness, and the exact rules and config fingerprint used.
  - The number shown is an **evidence agreement score, not a probability**.
  - There is **no signal** when the data is not LIVE, is stale, has invalid candles or has too few
    candles, or when the spread is too wide.
  - A directional live signal can prefill a **simulated** paper ticket. You can still change everything
    on the ticket.
- **Performance analytics** (**http://127.0.0.1:8000/performance**). Evaluates the rule-based signals
  over stored history: every candle in the range, which defaults to everything stored. The results
  have two separate parts:
  - an **analytical evaluation**, with no costs: favourable / adverse / flat outcomes, average and
    maximum favourable and adverse movement (MFE/MAE);
  - a **hypothetical simulated strategy**, using the paper simulator's spread, commission and adverse
    slippage: trades, net P/L, profit factor (only when meaningful), maximum drawdown and an equity
    curve.

  Both are broken down by timeframe, session and volatility condition. The page lists the entry and
  exit assumptions and the horizon. Hypothetical past results do not predict future performance.
- **Replay** (**http://127.0.0.1:8000/replay**).
  - Choose a stored dataset (imported CSV or recorded MT5 data), a step timeframe, a UTC start and end,
    and how many days of history to load before the start.
  - Then Play, Pause, Step 1 or Step 10 candles, Restart, or change the speed.
  - At every step the analysis sees **only candles that had closed by the replay time**.
  - Signals found during a replay get forward outcomes (move, MFE and MAE after 3 and 12 M5 candles,
    in ATR units), labelled **historical observation**.

All thresholds and weights are in [config/analysis.json](config/analysis.json). The file is validated
strictly at start-up, and an invalid file stops the server with a clear error. Its fingerprint is
stored with every signal.

Data sources and their limits: see [MARKET_DATA_PROVIDERS.md](MARKET_DATA_PROVIDERS.md).

**Provider priority:**
1. **MetaTrader 5.** This is your broker's real-time bid/ask from your local terminal (Windows), and
   it is the preferred source.
2. **Tiingo.** Real-time, if you set a free data key.
3. **gold-api.com.** Keyless, but it publishes indicative ~30 s snapshots with no bid/ask, so it is
   always shown as **DELAYED**.
4. **Stored history only.**

The dashboard always shows which source is active. **Providers never switch silently:** fallback is
manual unless you enable `GOLDSCALP_PROVIDER_FALLBACK=auto`, and every switch appears as a
"Data source changed" banner with the time and reason.

## Run it on Windows (PowerShell)

Requires Python 3.12 (`py -0` lists installed versions).

```powershell
cd C:\Spicy\GoldScalpAI
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt
.\.venv\Scripts\python.exe run.py
```

Then open **http://127.0.0.1:8000/** (the dashboard). The other pages are:
- Paper Trading: **http://127.0.0.1:8000/paper**
- Replay: **http://127.0.0.1:8000/replay**
- Performance: **http://127.0.0.1:8000/performance**
- Strategy Lab: **http://127.0.0.1:8000/lab**
- Live Validation: **http://127.0.0.1:8000/live**
- Monthly Backtest (separate research service, `python research.py`): **http://127.0.0.1:8001/monthly-backtest**
- Project overview (every phase so far: what it built and what it found): **http://127.0.0.1:8000/static/project.html**
- API documentation: http://127.0.0.1:8000/api/docs

Stop the server with `Ctrl+C`.

On the first start after an update, the database is migrated automatically:
- `0003` adds the `signals` table and a `signal_id` column on paper positions;
- `0004` adds `performance_runs` and `analysis_configs`;
- `0005` adds `strategies`, `strategy_versions`, `research_runs` and `strategy_journal`;
- `0006` rebuilds `paper_positions` (close reasons `time_exit`, `strategy_exit`), adds `paper_accounts.purpose` and the
  live-validation, verification, evaluation, improvement and assistant tables;
- `0007` adds `demo_links` and `demo_trades` for the optional demo-account execution (new tables only).

Existing accounts, positions and simulated fills are kept. The server binds to 127.0.0.1 only.

## MetaTrader 5 (read-only market data)

> The one exception is the optional demo-account execution described above: it is off by default, and it can
> send orders only to an account that reports itself as DEMO.

### What it does
GoldScalp attaches to the MT5 terminal **already running and logged in** on this PC. It reads the
gold symbol's live ticks (bid, ask, spread, tick time) once per second, and M1/M5/M15/H1 candles.
These are converted to UTC and validated like every other source; invalid, duplicate and missing bars
are rejected or reported, never invented. The paper simulator then prices fills from those
bid/ask quotes.

**Market data is read-only by construction:**
- The market-data provider wraps the `MetaTrader5` package in an allowlist that exposes only
  market-data functions.
- Through that allowlist, calling `order_send`, `order_check`, `positions_get`, `history_deals_get`,
  `account_info` or any other trading/account function raises an error.
- `tests/test_safety.py` checks that none of these names appears anywhere in GoldScalp's code except
  the demo-only gateway `goldscalp/execution/mt5_demo.py`. That gateway has its own allowlist, re-checks
  that the account is DEMO before every order, and does nothing unless demo execution is switched on.
- GoldScalp **never asks for, stores or logs your MT5 login or password**.
- It does not start the terminal for you (`GOLDSCALP_MT5_REQUIRE_RUNNING_TERMINAL=true`).

**The one change it makes in your terminal:** if your broker's gold symbol is hidden, GoldScalp adds
it to *Market Watch* (`symbol_select`), because MT5 only streams ticks for symbols there. Turn this
off with `GOLDSCALP_MT5_SELECT_SYMBOL=false`.

### Required software
- Windows 10/11 with the **MetaTrader 5 terminal** installed from your broker or metatrader5.com,
  logged in to your **demo** account.
- Python 3.12 (64-bit) and the optional package: `pip install -r requirements-mt5.txt`
  (installs MetaQuotes' official `MetaTrader5` package; the MT5 terminal is likewise 64-bit).

### Install and connect (Windows 11, PowerShell)

```powershell
cd C:\Spicy\GoldScalpAI
.\.venv\Scripts\python.exe -m pip install -r requirements-mt5.txt
```

Then:
1. Start MetaTrader 5 and log in to your demo account. The bottom-right status should show a
   connection (not "No connection" or "Invalid account").
2. Start GoldScalp: `.\.venv\Scripts\python.exe run.py`. With `GOLDSCALP_LIVE_PROVIDER=auto` (the
   default), MT5 is selected automatically when the package is installed.
3. **No "Algo Trading" switch and no Expert Advisor setting is needed.** The Python API reads market
   data without them, so leave algorithmic trading **off**. Only the optional demo-account execution
   needs it (see above).

### Find and configure your broker's gold symbol
- GoldScalp finds it automatically. It ranks names such as `XAUUSD`, `XAUUSDm`, `XAUUSD.r` and
  `GOLD`, and checks the symbol's base/quote currencies (XAU/USD).
- Symbols the broker describes as **not for trading** (for example currency-conversion symbols such
  as `XAUUSD.crp`) are pushed down the list.
- The chosen symbol is shown in **MetaTrader 5 details** on the dashboard, along with its contract
  size, lot limits, digits and the other candidates.
- **To choose another symbol:**
  - pick it in the dashboard's *Gold symbol* list and press **Use symbol** (this lasts until restart); or
  - set it permanently in `.env`: `GOLDSCALP_MT5_SYMBOL=XAUUSDm`.
- **To browse your broker's symbols:**
  - in MT5, open *View → Symbols* (Ctrl+U) and search "XAU" or "GOLD"; or
  - visit `http://127.0.0.1:8000/api/market/mt5/symbols?query=XAU`.
- **Contract sizes differ between brokers.** The Paper Trading page warns you if your MT5 symbol's
  contract size differs from the simulator's instrument file.

### Server time
MT5 reports times on the broker's **server clock** (often UTC+2, or UTC+3 during US summer time).
With `GOLDSCALP_MT5_TIME_BASIS=auto`:
- GoldScalp measures the offset from a fresh tick.
- A UTC+2/UTC+3 "New York close" server is mapped to the DST-aware rule `rule:ny_close`.
- The result is shown on the dashboard as "Server time … (detected)".

If the market is closed when GoldScalp starts, the offset is **assumed** (`rule:ny_close`) until the
first fresh tick. You can set it explicitly, for example `fixed:+02:00`.

**Note:** the suggested `MT5_TIMEZONE=UTC` would be wrong for most brokers. GoldScalp refuses a tick
that would end up in the future and tells you to fix the time basis.

### Verify that prices are updating
- **Dashboard:**
  - the badge reads **LIVE**;
  - *Price age* stays under the threshold (`GOLDSCALP_MT5_STALE_AFTER_SECONDS`, default 10 s) and
    the freshness bar stays green;
  - *Latest tick* and *Last successful update* advance every second;
  - *Provider* reads "MetaTrader 5".
- **API:** `http://127.0.0.1:8000/api/market/quote` returns `market_state`, `bid`, `ask`, `spread`,
  `quote_time` and `age_seconds`.

### What the states mean
| State | Meaning |
|-------|---------|
| **LIVE** | A fresh real-time tick (MT5 or Tiingo) arrived within the freshness threshold. |
| **DELAYED** | Data exists but is older than the threshold, or the source is not real-time (gold-api.com always). |
| **HISTORICAL** | The market is closed (weekend or daily break), or no live provider is active and you are looking at stored or imported candles. |
| **DISCONNECTED** | The terminal is not running, is not connected to its broker, the symbol is missing, or nothing has arrived for too long. The last price stays visible, greyed and labelled **STALE**, and the reason is shown. |

### Troubleshooting
| Message | Fix |
|---------|-----|
| "MetaTrader5 package is not installed" | `pip install -r requirements-mt5.txt` (Windows only). |
| "terminal … is not running" | Start MetaTrader 5 and log in. GoldScalp will not start it for you. |
| "could not connect to the MT5 terminal (-10003 …)" | Make sure only one terminal is running, or set `GOLDSCALP_MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe`. Run the terminal and GoldScalp as the same Windows user (both elevated, or neither). |
| "NOT connected to its broker" | The terminal is open but offline: check its login and internet connection. |
| "symbol … does not exist" | Choose a symbol from the dashboard list, or set `GOLDSCALP_MT5_SYMBOL`. |
| "tick time is … in the future" | The time basis is wrong: use `auto` or your broker's offset. |
| HISTORICAL at the weekend or during the 17:00–18:00 New York daily break | Normal: the market is closed. |

Use **Reconnect** on the dashboard to retry immediately. Your terminal is never restarted.

### Settings (in `.env`)
The suggested `MT5_*` names are prefixed with `GOLDSCALP_`: `GOLDSCALP_MT5_ENABLED`, `GOLDSCALP_MT5_SYMBOL`,
`GOLDSCALP_MT5_TIME_BASIS`, `GOLDSCALP_MT5_POLL_INTERVAL_SECONDS` (default 1),
`GOLDSCALP_MT5_STALE_AFTER_SECONDS` (default 10). The full list is in `.env.example`.
`GOLDSCALP_DISPLAY_TIMEZONE` (e.g. `Europe/London`) changes how times are *displayed*; storage stays UTC.

### Optional: real-time data with a free Tiingo key

```powershell
Copy-Item .env.example .env
notepad .env        # set GOLDSCALP_TIINGO_API_KEY=your-key, then restart run.py
```

Tiingo's free licence is **internal use only**. Keep the app on `127.0.0.1` and don't share the data.

### Run the tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check goldscalp tests
```

Tests never use the network or MT5. They use synthetic candles and the recorded MT5 fixtures in
`tests/fixtures/`. When Node.js is installed, pytest also runs the chart-logic tests in `tests/js/`.

## Importing history

On the dashboard, use **Import historical CSV**:

| File | Format | Time basis |
|------|--------|------------|
| Your own CSV: `timestamp,open,high,low,close[,tick_volume][,spread]` with ISO times **including an offset** | Generic CSV | UTC / own offsets |
| MetaTrader 5 → Symbols → Bars → Export | MetaTrader 5 bar export | Usually *MT5 server, New York close*; check your broker. Give the **point size** (e.g. `0.01`) to convert spreads. |
| HistData.com XAUUSD M1, "Generic ASCII" file (`YYYYMMDD HHMMSS;O;H;L;C;V`) | HistData.com M1 | Fixed UTC−5 |

If most bars fall in closed market hours, the import is **refused** with a hint about the likely
offset, and nothing is stored. Re-importing the same file changes nothing.

## Configuration

All settings are in [.env.example](.env.example). Highlights:

- **Contract specification:** [config/instruments/xauusd_demo.json](config/instruments/xauusd_demo.json).
  It is **hypothetical**, and every field is required. Copy it and enter your broker's published
  values to model a real contract.
- **Paper defaults:** $10,000, USD, 1:1 leverage, $0.30 configured spread, $3.50 commission per lot
  per side, $0.05 slippage. Start a new account with different settings from the Paper Trading page;
  old accounts are kept.

## Project documents

| Document | Contents |
|----------|----------|
| [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md) | Audit of the SpicyStock repository this project learned from |
| [REUSE_VS_REBUILD.md](REUSE_VS_REBUILD.md) | What was reused and what was rebuilt |
| [PROPOSED_ARCHITECTURE.md](PROPOSED_ARCHITECTURE.md) | The architecture, including revision 2 as built |
| [PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md) | Phase 2 (Smart Market Intelligence): design, rules and as-built notes |
| [PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md) | Phase 2 modules, rules, configuration, API, migrations, tests, assumptions and limits |
| [PHASE3_ARCHITECTURE.md](PHASE3_ARCHITECTURE.md) | Phase 3 (Strategy Intelligence and Backtesting): audit and design |
| [PHASE_3_IMPLEMENTATION_REPORT.md](PHASE_3_IMPLEMENTATION_REPORT.md) | Phase 3 strategy format, backtesting and walk-forward methodology, metric formulas, tests and limits |
| [PHASE4_ARCHITECTURE.md](PHASE4_ARCHITECTURE.md) | Phase 4 (live validation, Vantage MT5, research assistant): audit and design |
| [PHASE_4_IMPLEMENTATION_REPORT.md](PHASE_4_IMPLEMENTATION_REPORT.md) | Phase 4 terminal evidence, live evaluation, journal, improvement engine, assistant, tests and limits |
| [PHASE5_ARCHITECTURE.md](PHASE5_ARCHITECTURE.md) | Phase 5 (monthly multi-timeframe backtesting): isolation review and design |
| [PHASE_5_IMPLEMENTATION_REPORT.md](PHASE_5_IMPLEMENTATION_REPORT.md) | Phase 5 data incident and repair, candidates, the October 2026 evaluation, tests and limits |
| [PHASE_5_2_RESEARCH_REPORT.md](PHASE_5_2_RESEARCH_REPORT.md) | Phase 5.2 strategy redesign and multi-month robustness research: protocol, verdicts and the weakness study |
| [PHASE_5_3_VALIDATION_REPORT.md](PHASE_5_3_VALIDATION_REPORT.md) | Phase 5.3 frozen January-May 2026 validation of the four Phase 5.2 candidates (none held up) |
| [MARKET_DATA_PROVIDERS.md](MARKET_DATA_PROVIDERS.md) | Provider research, selection, limits and the TradingView review |
| [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) | Phases and status |

## Contribution rules

- Prove a test can fail: break the rule and watch the test go red.
- Never add a real-money order path or broker credentials. Order calls are allowed only in the demo-only
  gateway `goldscalp/execution/mt5_demo.py`, which stays off by default; `tests/test_safety.py` and
  `tests/test_demo_execution.py` enforce this.
- Analysis and simulation never import market-data providers.
- TradingView widget data never enters Python.

## Licences

Charts use TradingView Lightweight Charts™ v5.2.1 (Apache-2.0, vendored in `web/vendor/`, with its
attribution logo kept). The optional TradingView widget is TradingView's own embed, with its
attribution untouched.
