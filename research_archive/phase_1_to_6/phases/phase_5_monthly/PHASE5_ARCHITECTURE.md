# GoldScalp AI Phase 5 Architecture: Monthly Multi-Timeframe Strategy Research and Backtesting

Status: design (14 November 2026), written before implementation. Section 11 records what was built. Phase 5
is **historical and simulation-only**: no orders, no live trading, no change to MT5's read-only design.

---

## 1. Isolation review: protecting Phase 4 live paper monitoring

The live monitors are: Example A v2 (validation account #2), Example B v2 (#3) and Example C v4 (#4), in the
running GoldScalp process.

| Risk | Finding | Decision |
|------|---------|----------|
| Code changes reaching the running app | The app was started as `uvicorn.run(app)` with an app object: **no auto-reload**. Python keeps the modules it has imported; editing files on disk cannot change the running process. Its lazily imported modules (MT5 provider, research helpers) are already loaded. | Development is safe. New code reaches the app only on a restart. |
| Restarts | A restart interrupts monitoring for its duration. | **No restart for Phase 5.** The monthly feature runs in its own process (§3). |
| Database migrations | The app migrates its database only at startup. | **Phase 5 adds no migration to the main database.** It has its own SQLite file with its own migrations, in a separate file with separate locks. |
| Writes to the main database | Writes could conflict with or alter the monitors' input. | Phase 5 **never writes** to `var/goldscalp.sqlite3`. It reads strategies, candles and the verification record through `mode=ro` connections only (WAL readers never block writers). Read transactions stay short. |
| CPU | Monthly backtests are CPU-heavy (tens of thousands of as-of snapshots). | They run in the separate research process at **below-normal priority**, never in the monitoring process. |
| MT5 terminal | A second IPC client is unproven to be harmless. | Phase 5 does not connect to MT5. It uses stored candles only. |
| Static web files | The running app serves `web/` from disk. | New files only. Existing pages get one navigation link, to the research service. |
| Strategy versions | Must stay frozen. | A, B and C are read and copied into each evaluation (configuration plus fingerprint, re-verified). Phase 5 has no write path to `strategies` or `strategy_versions`. |
| Accounts | Must stay separate. | Each run is its own in-memory **hypothetical $1,000 account** inside the backtest engine. No paper account, validation account, ledger or position is touched; Phase 5 never imports the paper engine (safety test). |

**Incident found during this review (fixed with the user's approval).** The app's automatic MT5 time-basis
detection treated the stale weekend tick as fresh every 15 minutes. Each time it re-downloaded all candles at
timestamps shifted by 12-14 hours. The fix:
- detection now needs a *new* tick;
- the basis is pinned to `rule:ny_close` in `.env`;
- the database was backed up, the app restarted once while the monitors were idle (market closed), and only
  the `live:mt5:XAUUSD` candles were purged and re-downloaded.

The repaired data matches the recorded fixture exactly (2,068 of 2,068 bars, zero shift) and has no weekend
bars. Report: PHASE_5_IMPLEMENTATION_REPORT.md §2.

## 2. Reuse

| Need | Reused component |
|------|------------------|
| Execution | `backtesting.engine` (`bt@1`): next-open fills, candle spread, commission, adverse slippage, stop-first intrabar collisions, gaps, `on_change` de-duplication, margin from free margin, `force_close_at_end`. **Additive** diagnostics: a list of rejected entries with reasons, plus balance and used margin on the marked-equity points. Trades and result hashes are unchanged. |
| Decisions | `features.compute_timeline` (`features@1`) on a `ReplayDataset` → `conditions.decide`, the same as backtests and the live monitor |
| Strategy format | `strategy@1`. The catalogue gains three **additive** condition types for the research candidates: `ema_trend` (moving-average structure), `momentum` (RSI) and `level_break` (a confirmed, accepted break of a liquidity level). Existing configurations, fingerprints and decisions are unchanged; their snapshots gain an `ind` block. |
| Metrics | `metrics@1`, computed raw (no withholding), with a prominent small-sample flag below 30 trades |
| Reports | `reports.pdf` (`PDF`, `Layout.chart`), CSV and JSON, as in Phase 3 |
| Storage | the `Database` class (WAL, numbered migrations), pointed at a second file through a new `migrations_dir` argument |
| Data | `candle_repo.load_candles`, `resample`, `gaps.gap_report` and `integrity.check_candles` over the main database (read-only) |

## 3. Components

```
goldscalp/monthly/
  store.py        isolated SQLite store var/monthly/monthly.sqlite3 (migrations/0001_monthly.sql)
  sources.py      read-only access to the main database (strategies, series, candles, verified MT5 spec)
  calendar.py     month ranges, data availability per timeframe, default month
  candidates.py   research concepts → candidate configurations (pre-declared variants) and their documentation
  research.py     research stage on data strictly before the month; variant choice on the earlier part; freezing
  runner.py       the nine runs (A/B/C x M15/M5/M1), candidate runs, reproducibility
  report.py       per-run metrics, interpretation, strategy comparisons, combined report; PDF / CSV / JSON exports
  service.py      FastAPI router /api/monthly/* and the job runner (one job at a time)
  standalone.py   the research service (127.0.0.1:8001, below-normal priority, same security headers and CSRF)
web/monthly.html, web/js/monthly.js         the /monthly-backtest page (served by the research service)
```

The research service is started with `python -m goldscalp.monthly` or the launch configuration
`goldscalp-research`. It never imports the market hub, the providers, the paper engine or the live monitor.

## 4. Month, data and readiness

- **Range.** The month is `[first day 00:00 UTC, first day of next month 00:00 UTC)`. Decisions fall only inside it; open positions close at the month end (`window_end`).
- **Default month.** The latest *completed* calendar month in which the data for M15 and M5 is sufficient.
- **Per-timeframe sufficiency.**
  - Execution candles cover at least 95 % of the bars the trading schedule expects in the month.
  - The analysis history has at least 5 days before the month start.
  - The research window has at least 20 trading days before the month.
- **Missing data.** A timeframe without sufficient data is **not run**: it is reported INCOMPLETE with the exact gap. The month is never substituted.
- **Source.** The candle source is recorded per run: database, series key, timeframe, first and last candle, count, origin and dataset hash.
- **Analysis base.** M15 and M5 runs use M5 analysis candles; M1 runs use M1. Strategies read M5 conditions, so the analysis base can't be coarser than M5. A finer run needs its own timeframe.
- **Execution candles.** Each run uses its own timeframe's **native** candles (M15, M5 or M1). Timeframes are never combined into one stream.

## 5. Research stage (before the month only)

- **Window.** `[month start − research days (default 60), month start)`, at the M5 decision cadence on the M5 analysis base. It is split into development (the first 70 %) and check (the last 30 %).
- **Concepts.** Each concept has two pre-declared variants. One variant is chosen by development expectancy (at least 10 trades, otherwise flagged), then reported on the check part. The rules are frozen with a timestamp and a fingerprint **before** any month data is touched. The window end is asserted to be at or before the month start, with a test.
- **Nine concepts** (R1-R6 at first; R7-R9 added on 14 November 2026 at the user's request, motivated by a
  profile of the stored pre-October M5 history):
  - R1 trend-following with moving-average structure;
  - R2 momentum breakout;
  - R3 range reversion after a liquidity sweep;
  - R4 session breakout and retest;
  - R5 volatility-filtered trend;
  - R6 structure-shift pullback;
  - R7 New York overlap acceptance breakout;
  - R8 London sweep reversal before New York;
  - R9 volatility squeeze breakout.

  Each records its entry, exit, SL/TP, the conditions it investigates, its limitations and its data requirements.
- **Storage.** Candidates are stored in the monthly store as **research candidates**. They are never written to `strategies` and never replace A, B or C.
- **Evaluation.** Frozen candidates are evaluated on the month in the same runs, shown in a separate research section labelled "research period" and "evaluation month".

## 6. Risk, sizing and contract

- **Sizing as configured.**
  - A v2, B v2 and C v4 are configured with `sizing = fixed_lots`, 0.02 lots, and are run that way.
  - The monetary risk per entry is computed and reported: |entry − stop| × ounces.
  - Nothing else is invented. An explicit risk-percent override can be set per evaluation by the user (recorded); it is empty by default.
  - A strategy without a usable risk configuration is marked NOT READY.
- **Risk-to-reward (user requirement, 14 Nov 2026): aim for 1:1.5 to 1:2.**
  - A v2, B v2 and C v4 already target 2.0 R (1:2) and stay frozen.
  - Every research candidate's take-profit is an R multiple with two pre-declared variants, **1.5 R and 2.0 R**. The research stage chooses between them on development data from before the month.
  - Reports show the planned RR and the realized R per trade, and flag any strategy whose planned RR is outside 1.5-2.0. Realized R is lower than planned after costs, time exits and month-end closes.
- **Contract.** From the actual terminal's verification record `mv_214ba65715fb0f86`: 100 oz, lots 0.01-100 in steps of 0.01, digits 2. The tick value (1 USD per 0.01 per lot) is **derived**; it was not read from the terminal. The trading schedule is the app's hypothetical one.
- **Margin.** Leverage 1:500 is a hypothetical margin parameter: notional / 500, checked against free margin, with the reason recorded on rejection. It is not the broker's margin rule.
- **Costs.**
  - Spread: the stored candle spread (MT5 bar spread), else 0.30.
  - Commission: 3.50 USD per lot per side.
  - Slippage: 0.05 USD per oz per fill.

  All are configurable and shown before the start.

## 7. Reports

- **Nine run reports** (A/B/C × M15/M5/M1). Each covers:
  - run information and assumptions;
  - performance, raw values with a small-sample flag;
  - risk and execution: average and total risk, exit mix, rejections, margin usage, cost split;
  - charts: equity, balance, drawdown, daily distribution, win/loss distribution;
  - the trade ledger;
  - a deterministic interpretation (§8).
- **Three strategy reports** (M15 vs M5 vs M1). They don't crown the highest return: drawdown, costs, sample size and consistency are weighed in.
- **One combined report.** It shows the separate accounts side by side. Any combined figure is labelled "sum of nine separate $1,000 accounts" and never called one account's balance.
- **Exports:** PDF per run, per strategy and combined; CSV ledger per run and combined summary; JSON per run (configuration and results).

## 8. Interpretation rules (deterministic)

- **What worked / what didn't:** session and volatility groups with positive or negative net, at least 3 trades each.
- **Dependence on a few trades:** the share of net from the largest 3 trades, or whether removing the best trade flips the sign.
- **Costs:** costs as a percentage of absolute gross.
- **Data:** gaps, the fallback spread, and derived execution candles.

Every report states that one month is not evidence of reliability or of future results.

## 9. Statuses

The evaluation moves through: `NOT STARTED → RESEARCHING → READY → RUNNING → COMPLETED`. It ends `INCOMPLETE` when a run could not be done, for example because data is missing, and `FAILED` on an error. Every run keeps its own status. Completed evaluations open from the store without recalculation.

## 10. Tests

New tests cover:
- month ranges and the default month;
- availability;
- research before the month only, with no look-ahead (future candles change nothing);
- frozen versions;
- nine independent runs;
- sizing, SL/TP, margin and insufficient margin;
- costs, gaps and collisions;
- equity and drawdown;
- reports and exports;
- reproducibility (the stored configuration re-runs to the same result hash);
- that nothing writes to the main database or imports the paper engine, providers or broker code.

The full suite and Ruff must pass.

## 11. As built

| Design item | As built |
|-------------|----------|
| §1 isolation | Research service `python research.py` / `python -m goldscalp.monthly` (launch configuration `goldscalp-research`, port 8001). `ReadOnlySource` exposes only `read()` (tested: no `transaction`, no `migrate`). The end-to-end test shows every application table count and the schema version unchanged after research, freeze and nine runs. **Priority:** `SetPriorityClass(BELOW_NORMAL)` with explicit 64-bit ctypes types. The first start failed silently (handle truncation); fixed and tested in a subprocess, and the running service was lowered externally. |
| §1 incident | `mt5_provider._update_basis` detects only on a *new* tick (regression test reproduces 14 Nov exactly). `.env` pins `rule:ny_close` and the backfill depths (M1 100k, M5 30k, M15 10k, H1 3k). A refused deep request falls back to the default depth (tested). The M1 request was refused (§9 of the report). |
| §2 engine | `diagnostics.rejections` (time, setup, side, reason, detail; cap 2,000). `marked_equity` points carry `balance` and `used_margin`. Phase 3 hashes are unchanged (all Phase 3 tests pass). |
| §2 catalogue | `ema_trend` (`fast` 9/20/50 < `slow` 50/100/200; modes `price_vs_slow`, `fast_vs_slow`, `both`) and `momentum` (RSI(14) `confirm` / `not_extreme`). The snapshot `ind` block: EMA needs 2× its period of closed bars; RSI needs 50. `level_break` (added with R7) reads the liquidity engine's confirmed `break` events (closed beyond the level and stayed beyond it for the acceptance candles), filtered by direction, level group and recency; it needs liquidity detail on its timeframe. |
| §3 store | `goldscalp/monthly/migrations/0001_monthly.sql`: evaluations, candidates and runs, plus a copy of the candle schema for CSV imports into the research store (`POST /api/monthly/import`). |
| §4 data | `monthly/data.py`: coverage = present / schedule-expected bars in the month (gap report). History ≥ lookback before the month. The research window is at least 20 days with at least 80 % coverage. `default_month` scans back up to 12 months and returns why each month was skipped. |
| §5 research | `runner.research`: one M5 timeline over the research window; 70 % development, 30 % check. A variant is chosen on development expectancy with at least 10 trades, otherwise the pre-declared 2.0 R with a flag. A look-ahead guard raises if any loaded candle reaches the month; a test mutates all month candles and gets identical research. |
| §6 risk | `runner.readiness`: configured sizing (fixed lots for A, B and C), an optional explicit risk-% override recorded per evaluation, the RR check against 1:1.5-1:2. The contract comes from the latest actual-terminal verification record; the tick value is derived. |
| §7 reports | `monthly/report.py`: `build_run` (performance, risk and execution, costs, charts, trades, rejections, interpretation), `strategy_comparison`, `combined`, `run_pdf`, `strategy_pdf`, `combined_pdf`, `ledger_csv`, `summary_csv`, `run_json`. Reproduction: `POST /api/monthly/runs/{id}/reproduce` compares the result hash and the dataset and execution hashes. |
| §9 statuses | NOT STARTED → RESEARCHING → (NOT STARTED, research done) → READY (frozen) → RUNNING → COMPLETED / INCOMPLETE / FAILED. Each run has NOT STARTED / COMPLETED / INCOMPLETE (with the data reason) / FAILED. |
| §10 tests | `tests/test_phase5_monthly.py` (17, including the `level_break` condition). A Phase 4 migration test was adjusted to the explicit `migrations_dir` argument. |


---

## 12. Phase 5.1 patch: M1 import, percentage-risk scenarios (14 November 2026)

A targeted, additive patch. Nothing in sections 1-11 changes: the fixed-lot evaluations `me_cb6b197c4c6b` and
`me_697122f9e14b` (and the research-store baseline `me_23932a71a56a`) stay as they are and remain the
**Baseline — Fixed 0.02 lots**. Isolation is unchanged: the research service reads the application database
read-only, writes only to `var/monthly/monthly.sqlite3`, never imports MT5, the paper engine or the live monitor,
and is restarted only while no job is running. The main app and MT5 are never touched.

### 12.1 Historical data import (page section 1) and coverage (section 2)

| Item | Design |
|------|--------|
| Upload | `POST /api/monthly/import/preview` validates and stores nothing; then `POST /api/monthly/import` runs the existing importer `ingestion.import_csv`, unchanged. The page uses `XMLHttpRequest` for upload progress and sends `X-GoldScalp-Request: 1`. |
| Formats | `mt5_export`: tab-separated `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`, spread in points, needs `point`. Also `generic_utc` and `histdata_m1`. |
| Time basis | `rule:ny_close` (server = New York + 7 h: UTC+3 in US summer, UTC+2 in winter) for Vantage, as for the October dataset. The preview lists every DST change inside the file. |
| Checks | CSV structure, date/time parsing, ordering, duplicate timestamps, OHLC consistency and positive finite prices, timeframe (bar spacing and alignment; the timeframe in the file name), instrument (file name), time basis (share of bars in closed market hours, with a suggested shift), spread, missing bars, month coverage and warm-up. |
| Idempotency | One import per (series, timeframe, file SHA-256); a repeat reports `already_imported` and stores nothing. |
| Labels | `import_labels` (migration `0002_risk_scenarios.sql`): a free-text dataset label and instrument per import. |
| Coverage | `GET /api/monthly/coverage`: expected vs present bars (schedule-based), every missing interval and date (up to 200 each), history before the month, earliest and latest bar, status READY or INCOMPLETE. |
| Rules | Execution coverage of at least 95 %, plus the warm-up rule: history from the **first scheduled bar at or after (month start − 5 days)**. That is exactly what a run loads; when the five days start on a weekend, the Sunday-evening reopen is enough. Missing bars are never filled or interpolated. |

### 12.2 Percentage-risk sizing (engine mode `risk_percent_equity`)

Hypothetical stress-test sizing, used only by Phase 5.1 scenario evaluations. It is never used by the paper
simulator, the live monitor, MT5 or any account. It is a separate mode: `fixed_lots` and the Phase 3
`risk_percent` are unchanged, and fixed-lot trades carry no new fields, so their result hashes are unchanged.

```
risk budget          = current simulated equity x risk %      (equity = balance + open P/L at the entry open)
planned loss per lot = (|entry - stop| + exit slippage) x contract size + 2 x commission per lot per side
                       (entry spread and entry slippage are already in the entry price; a sell's stop fills on the ask)
lots                 = budget / planned loss per lot, rounded DOWN to the lot step (0.01)
                       capped at the broker maximum (100) and the strategy's own max_exposure_lots (1.00)
reject               no stop -> no_stop;  0.01 lot would exceed the budget -> risk_budget_below_min_volume
margin (separate)    notional / 500 must fit in free margin -> otherwise insufficient_margin
```

- The contract comes from the actual-terminal verification record: 100 oz, lots 0.01-100 in steps of 0.01,
  tick 0.01, tick value derived. Without a verified record a scenario is NOT READY; nothing is invented.
- Each trade records `equity_at_entry`, `risk_budget_usd`, `planned_loss_per_lot`, `planned_risk_usd`,
  `size_capped_by`, `within_budget` and `loss_beyond_plan_usd` (the actual loss beyond the planned risk, e.g. a
  gap through the stop). The run report adds averages, maxima, compliance and rejection counts.
- Leverage never enters the size. The strategy's signals, stops, targets (the frozen R multiple of the actual stop
  distance), time exits and one-open-position rule are unchanged.

### 12.3 Risk studies

| Item | Design |
|------|--------|
| Creation | `POST /api/monthly/risk-studies {baseline_evaluation, scenarios = [5, 7.5, 10], candidates = []}`. The baseline must be a finished, frozen, fixed-lot evaluation; it is referenced, never modified. |
| Evaluations | One new evaluation per scenario, with its own ID and the params `risk_scenario`, `baseline_evaluation` and `risk_study`. A/B/C fingerprints are re-checked against the application database **and** the baseline's frozen versions. Candidates (optional) are copied from the baseline, so their chosen 1:1.5 or 1:2 variants cannot change. |
| Runs | 3 strategies × 3 timeframes × 3 scenarios = **27 runs**, each a separate $1,000 account, plus 3 × 3 per selected candidate. |
| Job | One job. Each timeframe's decision timeline is computed **once**, and every scenario × strategy runs on it: identical inputs, only the sizing differs. |
| Reports | Per run (with the percentage-risk block and charts). Per strategy: timeframes within each scenario, and scenarios within each timeframe. Master: every strategy × timeframe × (baseline + scenarios). Balances are never added together. PDFs and the CSV carry the banner. |
| Reproduction | Frozen configuration + dataset hash + engine assumptions (including `sizing_override`) + code version; `POST /api/monthly/runs/{id}/reproduce` compares result hashes. |
| Re-runs | `POST /api/monthly/risk-studies/{id}/run` with `timeframes` and a mandatory `reason` re-runs only those timeframes of a finished study. The time, reason and every replaced result hash are recorded in the study; other timeframes are untouched. |
| Timeline cache | `features.compute_timeline` caches by series and source as well as candle content (fixed in 5.1): snapshots carry series-specific signal IDs, so identical candles in two series must not share a timeline. |
| Statuses | Study: READY → RUNNING → COMPLETED / INCOMPLETE / FAILED; scenario evaluations follow it. Import (shown on the page): NOT STARTED → VALIDATING → IMPORTING → READY / INCOMPLETE / FAILED. |

### 12.4 Tests

`tests/test_phase51_risk.py` covers:
- sizing at 5 / 7.5 / 10 %, compounding, costs inside the planned risk, rounding down (500 random cases),
  minimum-volume rejection, maximum caps, a missing stop and the assumption limits;
- unchanged signals, budget / planned / actual per trade, leverage independence, margin rejections, a gap through
  the stop, and readiness without a verified contract;
- the 27-run study end to end: independent accounts, fingerprints, reproduction, an unchanged baseline, reports and
  an unchanged application database;
- the import preview (DST, bad rows, wrong timeframe or instrument), an idempotent labelled import, coverage and
  missing dates.


---

## 13. Phase 5.2: strategy redesign and multi-month robustness research (14 November 2026)

Simulation only, in the research service and research store. No saved strategy, monitor, account, ledger or
MT5 connection is touched. The originals stay unchanged as permanent benchmarks.

### 13.1 Protocol (fixed before any redesigned rule was run)

| Item | Design |
|------|--------|
| Data | `live:mt5:XAUUSD` (application store, read-only) for M15 and M5, 15 Jun - 13 Nov 2026. For M1 the user's MT5 export (`csv:vantage-mt5-xauusd`), October only. Costs: candle spread, $3.50 per lot per side, $0.05 slippage; 0.02 lots as configured. |
| Windows | Calendar months clipped to 22 Jun - 14 Nov 2026: W1 (22-30 Jun, partial), W2 Jul, W3 Aug, W4 Sep, W5 Oct, W6 (1-13 Nov, partial). Each window × timeframe × configuration is a separate hypothetical $1,000 account. Windows without 95 % coverage and five days of history are INCOMPLETE, never filled. |
| Development | W1-W4 (22 Jun - 30 Sep). The only data used to study weaknesses and to choose variants. |
| Hold-out | W5-W6 (1 Oct - 13 Nov), evaluated once with the development choice. Caveat: October's aggregate results for the *old* strategies were seen in Phase 5; the new rules' October results were not. |
| Walk-forward | Anchored monthly folds: test W3 after choosing on W1-W2, W4 on W1-W3, W5 on W1-W4, W6 on W1-W5. The selection function refuses any window outside its allowed set (tested). |
| Metric | Expectancy in **R after costs** per trade: net P/L ÷ planned price risk × size. It is independent of sizing; USD figures are shown alongside. |
| Choice rule | Per strategy, the redesign variant with the highest development expectancy (M5) among those with at least 30 development trades. If none qualifies, the best of those with at least 15 trades, flagged "insufficient sample". Ties are broken by more positive months, then a smaller drawdown in R. |
| Verdicts | **consistent**: out-of-sample (walk-forward) expectancy > 0 with at least 30 trades, PF ≥ 1.1, at least 75 % of test months positive, and a positive hold-out. **positive but inconsistent**: positive but missing one of those. **fails**: out-of-sample expectancy ≤ 0. **insufficient sample**: fewer than 30 out-of-sample trades. |

### 13.2 Configurations (`goldscalp/monthly/robustness.py`)

- **Benchmarks (12):**
  - Example A v2, B v2 and C v4, read-only from the application database and fingerprint-checked;
  - R1-R9 as frozen in the M1-complete baseline `me_23932a71a56a`.
- **Exit experiments (up to 8 per original):** the original entry is unchanged and one exit dimension changes. These
  are independent experiments, compared on development data only:
  - **E1, E2:** time limit 120 or 240 minutes;
  - **E3:** no time exit;
  - **E4:** structure-flip exit with no time limit;
  - **E5, E6:** target 1.5R or 1R;
  - **E7:** tight 1.0 × ATR(M5) stop;
  - **E8:** wide M15 structure stop.

  Experiments identical to the original are skipped (e.g. R1 already uses 120 minutes).
- **Redesigned versions:** entry variants × four family exits. The variant chosen on development becomes the research
  version.

  | Version | Hypothesis | Entry variants |
  |---------|-----------|----------------|
  | Example A v3 | trend continuation | H1+M15 aligned, M15 EMA 20/50 trend, M5 pullback (higher low / lower high), M5 RSI ≥ 55; with London/NY and/or M5 volatility filters |
  | Example B v3 | breakout acceptance | accepted M15 BOS (M15 structure in the direction) with M5 momentum and active volatility, London only vs New York; retest into the M5 breakout FVG with M15 momentum, London vs New York; retest of an accepted previous-day/session level break |
  | Example C v5 | liquidity sweep reversal | H1 ranging, M5 sweep of EQ/PD/PW/session liquidity, M5 CHoCH confirmation, M15 volatility not expanding; + momentum not stretched, + London/NY, or the sweep on M15 levels |
  | R1, R5, R6 v2 | trend | original; + clear higher-timeframe structure; + London/NY and active volatility |
  | R2, R4, R9 v2 | breakout | original; + higher-timeframe confirmation; + level acceptance, or a retest instead of the original trigger |
  | R7 v2 | breakout (too selective) | original; loosened (break within 6 candles, RSI ≥ 52); loosened London + New York |
  | R3, R8 v2 | reversal | R3: original, + M15 volatility not expanding, + London/NY. R8 (too selective): original, loosened windows, + New York |

  Family exits:
  - **trend:** M5 structure stop with 2R, no time exit and an M15 structure-flip exit; M5 structure stop, 1.5R,
    240 minutes; 1.5 × ATR stop, 2R, 240 minutes; M15 structure stop, 2R, M15 flip;
  - **breakout:** the same first three, plus a 1.0 × ATR stop with 2R and 120 minutes;
  - **reversal:** 1.5R in 240 minutes; 1R in 120 minutes; 1.5R with an M5 flip exit; 1.5 × ATR stop, 2R.
- **New condition `level_retest`** (additive): a confirmed level break in the strategy direction, between
  `min_age_candles` and `within_candles` old, with price back within `max_distance_atr` × ATR of the broken level while
  still on the breakout side.
- **Design iterations are recorded in the study:**
  1. The first grids (study `rb_da267aa33110`, cancelled after one window).
  2. The B v3 and C v5 entries were revised after a **frequency** check on development data (8-11 Sep). The first
     combinations fired less than once a week: an accepted level break within 3 M5 candles passed on 4 of 1,656
     side-snapshots, and an M5 sweep and an M5 CHoCH coincided 0 times. No result was looked at.

### 13.3 Job, storage and reports

- **Job.** One job computes each (timeframe, window) decision timeline **once** and runs every configuration on it.
  Each trade is stored compactly with:
  - R, net, MFE/MAE in R and exit reason;
  - session, M5/M15 volatility and H1/M15 structure relative to the trade at its setup;
  - stop width in ATR, risk and costs.
- **Storage** (migration `0003_robustness.sql`, research store only):
  - `robustness_studies` (protocol, progress, the stored analysis);
  - `robustness_configs` (every configuration's exact rules);
  - `robustness_results` (one row per configuration × timeframe × window);
  - `research_versions` (each strategy's development-chosen redesign, with its verdict and evidence; **research
    only**, never written to the application's strategies).
- **Analyses** (pure functions over the stored results; the analysis is stored, so opening it never re-runs anything):
  - the weakness study of each original on development data;
  - exit experiments, per strategy and across strategies;
  - the development choice, walk-forward folds and hold-out on M15 / M5 / M1;
  - every variant's hold-out results, to show selection bias;
  - verdicts for the original and the redesign.
- **API:**
  - `POST /api/monthly/robustness-studies` and `GET .../{id}`;
  - `.../configs`, `.../results.csv` and `.../report.pdf`;
  - `GET /api/monthly/research-versions`.

  The page section is "10. Strategy redesign & multi-month robustness".

### 13.5 Final classification and freeze (added 14-15 November 2026)

- **Strict choices.** Walk-forward folds choose on development windows only (`robustness.folds`). Studies stored earlier
  are re-analysed strictly with `robustness.strict_params`.
- **Final classification** (`goldscalp/monthly/verdicts.py`, `verdicts@2`):
  - seven criteria — expectancy (OOS and November), at least 30 trades (a screen), profit factor, drawdown,
    monthly consistency, M15 vs M5, and fills ×2;
  - four classes — overfit, rejected, promising, inconclusive;
  - October is secondary: reported, never deciding a class.
- **Report** (`goldscalp/monthly/verdicts_report.py`): writes the Markdown, JSON and CSV files under
  `var/monthly/reports/`, read-only on the research store. The integrity checks cover one dataset per timeframe and
  month, choices identical without hold-out rows, and the framework hash.
- **Freeze.** `python -m goldscalp.monthly.verdicts_report --freeze "<note>"` records the hashes of the criteria,
  selection and report code, plus the thresholds. Versioned manifests are never overwritten: v1 at 17:38 UTC, v2 (the
  October amendment) at 17:48 UTC on 14 November, both with zero hold-out rows. The report states whether the code
  still matches.

## 14. Phase 5.3: frozen pre-June historical validation (15 November 2026)

A confirmation round for the Phase 5.2 shortlist on history none of it has seen. **Simulation only.** It makes no
strategy change and selects no variant; its results are reported separately and never change a Phase 5.2
classification.

### 14.1 Protocol (`goldscalp/monthly/validation.py`, definition `validation@1`)

- **Candidates (locked):** `R2|v-d-Xb`, `R1|v-b-Xd`, `R5|v-d-Xd-tr`, `R7|v-c-Xe-tr`. Their rules are copied from study
  `rb_7ca33b7b5a81` and checked against fixed SHA-256 fingerprints, both when the study is created and again when it
  runs. **Benchmarks:** the four originals, unchanged and fingerprint-checked too. Nothing else exists in the study.
- **Period:** 1 January to 31 May 2026, as five calendar months (V1 to V5), each a separate hypothetical $1,000
  account.
- **Data:** Vantage MT5 XAUUSD bid candles exported by the user (M5, M15, H1). They go into their own research-store
  series, `csv:vantage-mt5-xauusd-2026-jan-may`. Coverage is checked per month: at least 95 % and five days of
  history. A month that fails is recorded INCOMPLETE and is never filled in.
- **Execution:** the same engine code, costs, lots, next-open fills, stop-first rule and month-end close as Phase 5.2.
  Each runs on M5 and on M15.
- **Criteria:** the Phase 5.2 numbers, with the five-month rules made explicit:
  - "Months" means the months with sufficient data, of which at least 4 are required.
  - "Most months" means ceil(0.75 × months), which is 4 of 5 (or 3 of 4).
  - A month with data but no trade counts as **not** positive.
  - **Expectancy:** pooled expectancy > 0.
  - **Trade count:** at least 30 trades (a screen).
  - **Profit factor:** pooled PF ≥ 1.1, and PF > 1 in most months.
  - **Drawdown:** recovery factor ≥ 1.
  - **Consistency:** most months positive, and still positive without the best month.
  - **Timeframes:** M15 > 0 as well as M5.
  - **Execution:** still > 0 with spread and slippage ×2.
- **Cost stress:** ×3 is reported but is not a criterion.
- **Classes:** promising, inconclusive or rejected. They describe historical research candidates, not trading
  readiness. Overfit does not apply, because nothing is developed in this round.

### 14.2 Freeze, job and reports

- **Freeze:** `python -m goldscalp.monthly.validation_report --freeze "<note>"` writes
  `var/monthly/reports/validation-protocol-frozen.json` (versioned; never overwritten). It records:
  - the hashes of `validation.py` and `validation_report.py`;
  - the locked fingerprints, period, series and thresholds;
  - the number of candles in the validation series at freeze (0 = frozen before the data);
  - a fingerprint of everything in Phase 5.2: its 3,894 stored results, configurations and analysis, its report
    files, the verdicts@2 manifests and the frozen framework code.
- **Job:** `POST /api/monthly/validation-studies` refuses to run unless the protocol is frozen and unchanged. It writes
  only the research-store tables `validation_studies` and `validation_results` (migration `0004_validation.sql`). It
  computes one timeline per timeframe and month, shared by all eight configurations.
- **Report:** `python -m goldscalp.monthly.validation_report <id>` writes `<id>-validation.md/.json/.csv`. Its sections
  are protocol, results, month by month, timeframes, cost stress, criteria, the questions asked of each candidate,
  and integrity. The integrity section re-checks the protocol hash and the Phase 5.2 fingerprint and states the
  result.
- **Tests:** `tests/test_phase53_validation.py` covers:
  - the windows;
  - the five-month rules;
  - the locked configurations only;
  - INCOMPLETE months;
  - refusal when unfrozen or when a fingerprint differs;
  - versioned freezes;
  - detection of any Phase 5.2 change;
  - report rendering.

### 14.3 Result (study `vl_747cce289048`, 15 November 2026)

- **Run:** 80 runs (8 configurations × M5 and M15 × 5 months). Every month has sufficient data; the January and
  April gaps are market holidays.
- **Rejected:** R2 v2 (−0.054 R/trade over 273 trades), R5 v2 (−0.053 R over 222) and R7 v2 (−0.107 R over 37).
- **Inconclusive:** R1 v2 at +0.006 R/trade over 368 trades. It fails profit factor, drawdown, consistency (2 of 5
  months) and execution (×2).
- **Benchmarks:** the originals are rejected, except R1, which is inconclusive at +0.003.
- **Not cost-driven:** costs are 0.012 to 0.025 R per trade.
- **Integrity:** the protocol was unchanged, the code version identical, the fingerprints verified and Phase 5.2 was
  verified untouched.
- **Report:** `PHASE_5_3_VALIDATION_REPORT.md`.

## 15. Closing the Phase 5 strategy search (15 November 2026)

The strategy-optimization cycle (Phases 5.1 to 5.3) is **closed**. Phase 5.2 found an apparently promising candidate;
Phase 5.3's independent validation showed that it did not generalize. Nothing is promoted.

- **Freeze:** `python -m goldscalp.monthly.closure --close "<note>"` writes
  `var/monthly/reports/phase5-research-cycle-closed.json` (versioned; never overwritten). It records hashes of:
  - the Phase 5.3 report files, the protocol manifest, the validation code and migration;
  - all 80 stored results of `vl_747cce289048`, with its parameters, configurations, analysis and classes;
  - the whole Phase 5.2 fingerprint.

  `python -m goldscalp.monthly.closure --check` reports any change.
- **Retired from promotion:**
  - R2 v2, R5 v2 and R7 v2 are rejected; R1 v2 stays inconclusive, not promising.
  - None proceeds to paper-monitor observation. None of them exists in the application database, and the research
    module has no path to put one there.
- **Phase 5.3 runs refused:** `POST /api/monthly/validation-studies` answers 409 once the cycle is closed.
- **Phase 4 is independent and unchanged:** the manifest records the three monitored versions at closure (A v2, B v2
  and C v4, paper, active) for reference only. Nothing here writes to the application database.
- **Spent data:**
  - Every candle stored so far is spent: 2 November 2025 to 13 November 2026 (research store and application
    database).
  - A new hypothesis must be designed without that data's results and must eventually be tested on fresh data:
    forward data recorded after it, or older history that was never imported or inspected.
- **Next research question (not started):** why does performance change across market regimes (volatility, trend,
  session, range expansion)? How can the methodology detect regime dependence before a candidate is promoted? This
  replaces "how to make these four strategies profitable"; no further parameter tuning.

## 16. Phase 6: new strategy discovery (definition `discovery@1`, 15 November 2026)

- **Pre-registration:**
  - `config/discovery/phase6.json` holds the three hypotheses with question, mechanism, entry, exit, invalidation,
    baseline rules and a checkable prediction;
  - `PHASE_6_PROTOCOL.md` is the readable version;
  - both are frozen with the code by `python -m goldscalp.monthly.discovery_report --freeze`, which writes
    `var/monthly/reports/discovery-protocol-frozen.json` (versioned) before any trial;
  - trials are refused while the protocol is unfrozen or changed.
- **Data guard (`discovery.guard`):** every development window is checked, together with its five-day look-back and
  a 31-day margin for coarser-timeframe history. A window is refused if it could read the sealed block
  (October 2023 – October 2025) or forward data (from 14 November 2026). The guard runs both when a trial is created
  and again when it runs.
- **Development windows:** D1–D5 cover January–May 2026 (research-store export) and D6–D11 cover 22 June –
  13 November 2026 (app MT5 history). Execution is on M5, and each window is a separate $1,000 account.
- **Trials (`discovery_trials`, `discovery_results`, migration `0005_discovery.sql`):**
  - a hypothesis gets at most 4 trials;
  - refinements are built by `discovery.refine` from a parent configuration, using only that hypothesis's allowed
    kinds (its own filter, the stop timeframe, or the exit horizon ×0.5 / ×2);
  - each refinement needs a written rationale;
  - `POST /api/monthly/discovery/baselines` runs every missing baseline as one job with a shared timeline per month.
- **Analysis:**
  - pooled results, month by month, and breakdowns by M15/M5 volatility regime, session group (London open / Asian /
    New York) and H1 relation;
  - the pre-registered prediction is checked, and the development gate is stated.
- **Report:** `python -m goldscalp.monthly.discovery_report` writes `var/monthly/reports/phase6-development.{md,json}`
  listing every trial, failures included.
- **Tests:** `tests/test_phase6_discovery.py`.

### 16.1 Phase 6 development outcome and H6.4 (definition `sealed@1`)

- **Development outcome** (baselines, 15 Nov 2026):
  - H6.1 is not supported as predicted;
  - H6.2 is rejected (the change-of-character context made results worse);
  - H6.3 is inconclusive (too few London-open trades);
  - none of them goes to the sealed data, and their frozen files are unchanged.
- **H6.4** (`config/discovery/phase6-h64.json`, `PHASE_6_H64_REGISTRATION.md`):
  - one development-derived hypothesis, with its origin disclosed;
  - candidate: VOL-C plus exactly one condition, "M15 volatility normal or expanding";
  - control: the frozen VOL-C, for comparison only.
- **Code:** `goldscalp/monthly/sealed.py` and `sealed_report.py`, with migration `0006_sealed.sql` (`sealed_runs`,
  `sealed_results`), and the endpoints `POST/GET /api/monthly/h64/runs`.
- **Coverage:** follows the market calendar fixed in the registration (listed full closures and early closes) with
  the 95 % threshold unchanged. December 2024's only gaps are the Christmas and New Year closures.
- **Data verification** (`sealed_report --verify`; reads timestamps and compares candles, never computes a result):
  - coverage per month;
  - duplicate and misaligned bars;
  - M5→M15 and M15→H1 aggregation compared with the separate exports;
  - sealed H1 compared with the application's own MT5 H1 (timezone).
- **Freeze:** `sealed_report --freeze` records the hashes of the registration, the code and the Phase 6 specification,
  the fingerprints and the verification. It is refused once any sealed result exists. The sealed run is refused unless
  H6.4 is frozen and unchanged, and it runs once.
- **Tests:** `tests/test_phase6_h64.py`.
- **Sealed outcome** (run `sr_ed2c472a7559`, attempt 1 of 1, 15 Nov 2026 11:19–17:08 UTC, all 96 units COMPLETED;
  report `var/monthly/reports/h64-report.md`):
  - **H6.4 is rejected.** Pooled M5 −0.006 R/trade after costs over 663 trades (PF 1.04); M15 −0.014 R/trade over
    659 trades.
  - 11 of 24 months were positive; the halves made −8.07 R and +3.83 R; with costs ×2 the result is −0.036 R/trade.
  - It fails expectancy, profit factor, consistency, prediction, execution and timeframes. It passes data (24 of 24
    months) and trade count.
  - The control VOL-C (comparison only) made −0.029 R/trade over 762 trades. Its contracting-volatility trades did
    worse than the rest (−0.153 vs −0.003 R/trade), so the filter removed weak trades as the mechanism suggested. But
    the continuation edge seen in development (+0.102 R/trade) did not appear in November 2023 – October 2025.
  - H6.4 is not retuned. The sealed block has now been seen, so it can no longer serve as unseen data for any
    hypothesis shaped by this result. Forward data from 14 November 2026 remains untouched.

### 16.2 Research library (definition `library@1`)

- **What:** Monthly Backtest step 5 lists every Phase 5 and Phase 6 study and compares any eight results side by side
  (pooled metrics, cumulative R by month, a monthly R heatmap, execution stress and, for Phase 6, regime breakdowns),
  with a reader for the written reports.
- **Code:** `goldscalp/monthly/library.py` (`GET /api/monthly/library`, `GET /api/monthly/library/documents/{id}`) and
  `web/js/library.js`.
- **Read-only:** it opens the research store through `read()` only and serves only listed report files. Results come
  from the frozen report files (Phase 5.2, 5.3) and the stored analyses (Phase 5, 5.1, 6, H6.4). Classes are copied,
  never recomputed. A run that has not COMPLETED contributes no results, and incomplete or failed studies stay listed.
- **Tests:** `tests/test_research_library.py`.
