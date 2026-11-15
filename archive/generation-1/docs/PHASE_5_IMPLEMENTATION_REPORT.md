# GoldScalp AI: Phase 5 Implementation Report

**Monthly multi-timeframe strategy research and backtesting.** 14 November 2026.

Phase 5 is **historical and simulation-only**. It places no orders, never connects to MT5, and never touches a
paper account, validation account, ledger or position. Every number below comes from stored historical candles
and hypothetical $1,000 backtest accounts. One historical month **does not establish that any strategy is
profitable or reliable**.

Design: [PHASE5_ARCHITECTURE.md](PHASE5_ARCHITECTURE.md). User guide: README, "Monthly Backtest (Phase 5)".

---

## 1. Summary

| Item | Result |
|------|--------|
| Phase 4 monitors | **Never interrupted by Phase 5.** A v2 (account #2), B v2 (#3) and C v4 (#4) stayed active in paper mode. The only restart of the main app was the one the user approved for the data repair (§2), while the market was closed. |
| Isolation | Separate research service (port 8001, below-normal priority). It reads the main database read-only, writes only to `var/monthly/monthly.sqlite3`, and never imports the paper engine, the live monitor or the MT5 provider. |
| Page | `http://127.0.0.1:8001/monthly-backtest`, linked from every page of the main app as "Monthly Backtest" |
| Evaluation month | **October 2026** (1 Oct 00:00 UTC to 1 Nov 00:00 UTC), the latest completed month with stored data |
| Frozen versions | Example A **v2** (`c7bc3da5f422`), Example B **v2** (`5d8d81c69794`), Example C **v4** (`14ceea2a1c9e`) |
| Research candidates | **Nine** (R1-R9), each with pre-declared 1:1.5 and 1:2 risk-to-reward variants chosen on data before October |
| Nine runs (A/B/C × M15/M5/M1) | Application store (`me_cb6b197c4c6b`, `me_697122f9e14b`): **6 COMPLETED** (M15 and M5), **3 INCOMPLETE** (M1: no stored October M1 candles). **Phase 5.1:** with the user's MT5 exports, `me_23932a71a56a` completed **all nine**, M1 included (§12). |
| October outcome (§6) | A v2, B v2 and C v4 **lost on every completed run** (−5.8 % to −24.7 %). Of the candidates, only R6 had a positive net, with a negative mean R and the result driven by one wide-stop trade. R7 and R8 traded too rarely to judge. One month; no profitability claim. |
| Evaluations | `me_cb6b197c4c6b` (A/B/C + R1-R6) and `me_697122f9e14b` (A/B/C + R1-R9). The 27 shared runs reproduced identical result hashes. |
| Tests | **534 passed** at Phase 5 completion; **583 passed** after Phase 5.1 (full suite, which also contains another session's new tests); Ruff clean |
| Phase 5.1 (§12) | M1 import with preview / validation / coverage on the page; percentage-risk scenarios (5 / 7.5 / 10 % of current simulated equity, hypothetical) as 27 separate A/B/C runs from the fixed-lot baseline; original fixed-lot results preserved byte for byte. |

---

## 2. Incident found during the isolation review, and its repair

While reviewing whether Phase 5 could disturb the Phase 4 monitors, a separate, pre-existing problem was found in
the running app.

- **Cause.** With the market closed, the MT5 provider's automatic time-basis detection compared the stale Friday
  tick with the wall clock every polling cycle. It "detected" a new offset roughly every 15 minutes and re-downloaded
  candles under it. The stored `live:mt5:XAUUSD` candles ended up shifted (the last re-download by +14 h 15 min);
  1,936 of the 2,068 recorded fixture bars had been overwritten at wrong timestamps.
- **Impact.** No live signal, simulated trade or journal entry had been produced from the affected data: the
  market was closed and the monitors were idle.
- **Fix (approved by the user: "Fix now").**
  1. `mt5_provider._update_basis` detects an offset **only on a new tick** (a changed server timestamp). A
     regression test replays the 14 November sequence exactly.
  2. `.env` pins `GOLDSCALP_MT5_TIME_BASIS=rule:ny_close` and deeper backfill depths (M1 100,000, M5 30,000,
     M15 10,000, H1 3,000). A refused deep request falls back to the default depth (tested).
  3. Backups: `var/backup-before-phase4.sqlite3` and `var/backup-before-candle-repair-20261114.sqlite3`.
  4. The app was restarted **once**, with the monitors idle, then only the `live:mt5:XAUUSD` candles were purged
     and re-downloaded.
- **Verification.** The re-downloaded candles match the recorded fixture exactly (2,068 of 2,068 bars, zero
  shift), with no weekend bars. The monitors resumed with no errors.
- **M1 depth.** The terminal refused the 100,000-bar M1 request; the fallback returned 3,000 bars (11-13 November).
  This is why October M1 is unavailable (§6).

Stored history after the repair (`live:mt5:XAUUSD`, bid candles):

| Timeframe | Bars | From (UTC) | To (UTC) |
|-----------|------|------------|----------|
| H1 | 3,000 | 2026-05-14 23:00 | 2026-11-13 21:00 |
| M15 | 10,000 | 2026-06-15 17:30 | 2026-11-13 21:45 |
| M5 | 30,000 | 2026-06-15 17:05 | 2026-11-13 21:55 |
| M1 | 3,000 | 2026-11-11 17:53 | 2026-11-13 21:56 |

---

## 3. Protecting Phase 4 monitoring

| Rule from the brief | How it was met |
|---------------------|----------------|
| Inspect restarts, migrations, background processes | Isolation review, PHASE5_ARCHITECTURE.md §1. The app runs without auto-reload, so code edits cannot reach it. |
| Do not stop or restart the app or MT5 | Phase 5 itself never restarted either. The single restart was the separate data repair the user approved (§2). |
| Do not modify the scheduler, accounts, positions, journal or ledger | Phase 5 has no write path to the main database (`ReadOnlySource` exposes only `read()`, `mode=ro`). An end-to-end test shows every application table count and the schema version unchanged after research, freeze and runs. |
| Isolated services and storage | `goldscalp/monthly/` service on port 8001; its own SQLite file and migrations |
| Separate $1,000 accounts | Each run is an in-memory hypothetical $1,000 account inside the backtest engine. No paper or validation account is read for execution or written. |
| Frozen A v2, B v2, C v4 | Read from the application database, fingerprints re-checked, copied into the evaluation at freeze time. Phase 5 never writes to `strategies` or `strategy_versions`. |
| No migrations or reloads while monitoring | No migration to the main database. The research service's own migrations run on its own file. |
| Historical and simulation-only | No MT5 import in `goldscalp/monthly`, no `order_send` anywhere (safety tests; see the note below). |

> **Clarification added 15 November 2026.** "No `order_send` anywhere" holds for the Phase 5 code (`goldscalp/monthly`)
> and every other package, except one file. The optional MT5 demo-account execution feature, added on 14 November
> 2026, has a gateway `goldscalp/execution/mt5_demo.py`, and the safety test allows `order_send` in that file only.
> That feature is off by default (`GOLDSCALP_DEMO_EXECUTION_ENABLED`) and refuses any account that does not report
> itself as DEMO. There is no real-money order path, and the research service never imports `goldscalp.execution`
> (safety test).

Checked again at 13:24 UTC on 14 November: monitor `live@1` running, 1,754 cycles, `last_error` none, three
monitors active in paper mode, no open simulated trades (market closed until 15 Nov 23:00 UTC).

---

## 4. What was built

### Pages and features

- **`/monthly-backtest`** (research service):
  - month selection, with the latest completed month as the default;
  - data availability per timeframe: execution coverage, analysis base, history before the month, research window;
  - evaluation list with status (NOT STARTED, RESEARCHING, READY, RUNNING, COMPLETED, INCOMPLETE, FAILED) and progress;
  - actions: **Research**, **Freeze**, **Run**, **Cancel**;
  - the runs grid (strategy × M15/M5/M1) with status and the reason for INCOMPLETE runs;
  - run report: performance, risk and execution, costs, equity and drawdown charts, trades, rejected entries,
    interpretation;
  - strategy comparison (one strategy across M15/M5/M1) and the combined report (all strategies and timeframes);
  - research candidates in a separate section labelled "research period" and "evaluation month".
- **Exports:**
  - per run: PDF report, CSV ledger, JSON;
  - per strategy: PDF comparison;
  - combined: PDF and summary CSV.
- **Reproduction.** `POST /api/monthly/runs/{id}/reproduce` re-runs a stored run from its frozen configuration and
  compares the result, dataset and execution hashes.
- **CSV import** into the research store (`POST /api/monthly/import`) for history the terminal cannot supply.

### API (`/api/monthly`, research service)

`GET /defaults`, `/availability`, `/strategies`; `POST /evaluations`; `GET /evaluations`, `/evaluations/{id}`;
`POST /evaluations/{id}/research`, `/freeze`, `/run`, `/cancel`; `GET /runs/{id}` with `report.pdf`, `ledger.csv`,
`run.json`; `POST /runs/{id}/reproduce`; `GET /evaluations/{id}/strategies/{key}` (+ `report.pdf`), `/combined`,
`/report.pdf`, `/summary.csv`; `POST /import`.

### Simulation settings (per run)

| Setting | Value |
|---------|-------|
| Starting balance | $1,000 (hypothetical, one account per run) |
| Leverage | 1:500, used **only** for margin (required margin = price × 100 oz × lots / 500) |
| Sizing, stop, take-profit | Each strategy's configured values. A, B and C are configured with **fixed 0.02 lots**, an M5 structure stop, a 2 R take-profit and a 60-minute time exit. No risk percentage is invented; an explicit override is possible per evaluation and is recorded. |
| Contract | From the actual-terminal verification record `mv_214ba65715fb0f86`: 100 oz, lots 0.01-100 in steps of 0.01, 2 digits; tick value derived ($1 per 0.01 move per lot) |
| Costs | Candle spread where present (100 % of October candles had a spread), otherwise $0.30; commission $3.50 per lot per side; adverse slippage $0.05 |
| Engine | `bt@1`: next-open fills, stop-first intrabar collisions, gap handling, margin checks from free margin, open trades closed at the month end |
| Decisions | `features@1` as-of snapshots at each close of the run timeframe; analysis base M5 for M15 and M5 runs, M1 for M1 runs |
| Execution candles | The run timeframe's own candles (an M15 run executes on M15 candles; tested) |

### Risk-to-reward (user requirement: aim for 1:1.5 to 1:2)

- A v2, B v2 and C v4 are configured at **1:2** and were not changed (they are frozen).
- Every research candidate has **two pre-declared variants, 1:1.5 and 1:2**. The research stage chooses one on
  development data from before the month. The month is never used to choose.
- Every run reports its planned risk-to-reward. Trades closed by time, structure or the month end have a realised
  R different from the plan, and the report says how many.

---

## 5. Research candidates

All candidates were built and chosen **only on data before October** (research window 2 Aug 22:00 to
1 Oct 00:00 UTC: development to 13 Sep 06:36, check from 13 Sep 06:36). They are stored as research candidates in
the monthly store and never written to the application's strategies.

All candidates use 0.02 lots, one open trade at a time, a 60-minute time exit and an R-multiple take-profit
(1:1.5 or 1:2). "Structure" stops sit beyond the M5 structure point with a 0.1 ATR buffer; "ATR" stops are
1.5 × ATR(M5).

| Key | Concept | Entry idea | Stop |
|-----|---------|------------|------|
| R1 | Trend-following with moving-average structure | M15 and H1 EMA structure with the trade, an M5 pullback, momentum back in the trend direction | Structure |
| R2 | Momentum breakout | A fresh M5 break of structure with RSI momentum in London or New York, volatility normal or expanding | ATR |
| R3 | Range reversion after a liquidity sweep | In an H1 range, trade back after a confirmed M5 sweep while momentum is not stretched | Structure |
| R4 | Session breakout and retest | An M15 break of structure in London or New York, entered on an M5 retest of a same-direction fair value gap, no higher timeframe against | Structure |
| R5 | Volatility-filtered trend | H1 and M15 structure aligned, price beyond the M15 EMA50, M5 ATR in a moderate band | ATR |
| R6 | Structure-shift pullback into an order block | A recent M15 CHoCH, entered on a pullback into a same-direction M5 order-block candidate, acceptable spread | Structure |
| **R7** | **New York overlap acceptance breakout** | A *confirmed* break of a previous-day or session level during the London/NY overlap, M5 RSI ≥ 55, H1 not against | ATR |
| **R8** | **London sweep reversal (before New York)** | London-only hours: sweep of previous-day, session or equal highs/lows plus an M5 CHoCH, M5 RSI not stretched (≤ 65 for longs) | Structure |
| **R9** | **Volatility squeeze breakout** | M5 BOS with M5 RSI ≥ 58 while M15 volatility is still contracting, London or NY | ATR |

Each candidate's documentation (entry, exit, stop, take-profit, what it investigates, limitations, data needs) is
stored with it and shown on the page.

**R7-R9 were added on 14 November at the user's request** ("look for 3 more good profitable strategies"). They
are grounded in a profile of GoldScalp's own pre-October M5 history (15 June to 30 September):

- The mean M5 range peaks between 12:00 and 15:00 UTC (6.9-8.0 USD) against 3.6-4.5 USD in the London morning and
  2.6 USD at 20:00 UTC. That is why R7 trades the overlap.
- The Asian range (00:00-07:00 UTC) was broken on 72 of 77 days (94 %). After the first close beyond it, price
  first extended by another range height on 24 days and first returned to the range middle on 21. A plain
  breakout has no edge here, which is why R7 requires an *accepted* break and R8 tests the reversal side.
- R9 tests volatility clustering: breaks out of a squeeze.

R7 needed a new **additive** condition type, `level_break`: a confirmed break event from the liquidity engine
(closed beyond the level and stayed beyond it for the acceptance candles), filtered by direction, level group and
recency. Existing strategies' configurations, fingerprints and decisions are unchanged (all earlier tests pass).

---

## 6. October 2026 results

> **One historical month on hypothetical $1,000 accounts.** None of these numbers shows that a strategy is
> profitable or will be. Most samples are small (under 30 trades), and nothing here is a recommendation to
> trade.

### Evaluations

| Evaluation | Contents | Status |
|------------|----------|--------|
| `me_cb6b197c4c6b` | A v2, B v2, C v4 + R1-R6 (frozen 14 Nov 13:02 UTC) | INCOMPLETE (M1 only) |
| `me_697122f9e14b` | A v2, B v2, C v4 + R1-R9, after the research service was restarted to load R7-R9 | INCOMPLETE (M1 only) |

**Reproducibility.** The 27 runs common to both evaluations (A, B, C and R1-R6 on M15, M5 and M1) have identical
status, configuration fingerprint and result hash, and R1-R6 made the same research choices. The restart of the
research service and the added code did not change any earlier result. The tables below are from
`me_697122f9e14b`.

### Data availability (`live:mt5:XAUUSD`, application store)

| Timeframe | Execution coverage in October | Result |
|-----------|-------------------------------|--------|
| M15 | 2,016 of 2,016 expected bars (100 %), 1 Oct 00:00 to 30 Oct 20:45 UTC | Sufficient |
| M5 | 6,048 of 6,048 (100 %), 1 Oct 00:00 to 30 Oct 20:55 UTC | Sufficient |
| M1 | 0 of 30,240 (0 %); no M1 history before the month either | **Insufficient → INCOMPLETE** |

- Every October candle carried the broker's spread (spread known for 100 % of bars).
- Research window: 2 Aug 22:00 to 1 Oct 00:00 UTC (59 days, 99.7 % coverage). Development part to 13 Sep
  06:36 UTC, check part from 13 Sep 06:36 UTC.

### The nine runs: A v2, B v2, C v4 × M15 / M5 / M1

All runs: $1,000 start, 1:500 margin only, fixed 0.02 lots as configured, M5 structure stop, 1:2 take-profit,
60-minute maximum hold.

| Strategy | TF | Status | Trades | Win rate | Net USD | Return | PF | Avg R | Max DD USD (%) | Costs USD | Planned RR |
|----------|----|--------|-------:|---------:|--------:|-------:|---:|------:|---------------:|----------:|-----------:|
| A v2 | M15 | COMPLETED | 72 | 31.9 % | −227.31 | −22.7 % | 0.62 | −0.20 | 323.96 (31.2 %) | 54.82 | 1:2 |
| A v2 | M5 | COMPLETED | 69 | 30.4 % | −247.18 | −24.7 % | 0.63 | −0.22 | 392.18 (38.4 %) | 52.54 | 1:2 |
| A v2 | M1 | INCOMPLETE | | | | | | | | | |
| B v2 | M15 | COMPLETED | 15 | 26.7 % | −95.06 | −9.5 % | 0.45 | −0.22 | 121.13 (12.1 %) | 11.40 | 1:2 |
| B v2 | M5 | COMPLETED | 15 | 40.0 % | −57.90 | −5.8 % | 0.64 | −0.14 | 96.44 (9.6 %) | 11.40 | 1:2 |
| B v2 | M1 | INCOMPLETE | | | | | | | | | |
| C v4 | M15 | COMPLETED | 90 | 35.6 % | −240.81 | −24.1 % | 0.65 | −0.07 | 322.58 (32.3 %) | 68.50 | 1:2 |
| C v4 | M5 | COMPLETED | 97 | 36.1 % | −233.77 | −23.4 % | 0.69 | −0.08 | 322.40 (32.1 %) | 73.82 | 1:2 |
| C v4 | M1 | INCOMPLETE | | | | | | | | | |

**M1 reason, as stored:** "M1 execution candles cover 0% of the month (need 95%; stored M1 has none in the
month); M1 analysis history before the month starts nowhere; 5 days are needed." §8 explains how to add it.

How the trades ended:

| Run | Stop-loss | Take-profit (2 R) | Time exit (60 min) | Rejected entries |
|-----|----------:|------------------:|-------------------:|------------------|
| A M15 | 23 | 3 | 46 | 9 (one trade already open) |
| A M5 | 22 | 1 | 46 | 14 (one trade already open) |
| B M15 | 5 | 1 | 9 | 0 |
| B M5 | 4 | 1 | 10 | 0 |
| C M15 | 29 | 11 | 50 | 3 (one trade already open) |
| C M5 | 34 | 14 | 49 | 19 (one trade already open) |

Margin was never a constraint: the highest used margin was $16.82, at most 1.8-2.6 % of equity per run. No
entry of A, B or C was rejected for margin or minimum lot.

### Research candidates: choice made before October

| Key | Development 1:1.5 (trades / net USD / exp. R) | Development 1:2 | Chosen | Check part (trades / net / exp. R) | Flag |
|-----|------------------|------------------|--------|------------------|------|
| R1 | 211 / +304.96 / +0.021 | 205 / +549.33 / +0.100 | 1:2 | 88 / −90.32 / −0.055 | |
| R2 | 108 / −46.38 / −0.021 | 105 / −36.01 / −0.007 | 1:2 | 53 / +1.62 / +0.097 | |
| R3 | 26 / −94.33 / −0.243 | 26 / −55.28 / −0.128 | 1:2 | 7 / +35.79 / +0.178 | |
| R4 | 28 / +94.61 / +0.113 | 27 / +104.84 / +0.123 | 1:2 | 8 / −115.38 / −0.616 | |
| R5 | 101 / −217.98 / −0.129 | 100 / −168.56 / −0.079 | 1:2 | 36 / +80.71 / +0.123 | |
| R6 | 31 / −96.90 / −0.143 | 30 / −85.88 / −0.147 | 1:1.5 | 16 / −43.38 / −0.279 | |
| R7 | 6 / +131.95 / +1.071 | 6 / +77.29 / +0.988 | 1:2 (default) | 2 / −39.41 / −0.860 | insufficient research sample |
| R8 | 2 / +1.29 / +0.089 | 2 / +1.29 / +0.089 | 1:2 (default) | 1 / −13.64 / −0.693 | insufficient research sample |
| R9 | 25 / +23.57 / +0.065 | 25 / +8.59 / +0.008 | 1:1.5 | 8 / +3.36 / +0.033 | |

The variant is chosen by development expectancy with at least 10 development trades; otherwise the pre-declared
1:2 default is used and flagged. Several development results reversed sign on the check part (R1, R4, and the
other way round R2, R3, R5), which already showed that two months of history cannot separate an edge from noise.

### Research candidates in October

| Key | TF | Trades | Win rate | Net USD | Return | PF | Avg R | Max DD USD (%) | Planned RR |
|-----|----|-------:|---------:|--------:|-------:|---:|------:|---------------:|-----------:|
| R1 | M15 | 125 | 40.8 % | −115.38 | −11.5 % | 0.89 | −0.07 | 195.90 (19.0 %) | 1:2 |
| R1 | M5 | 158 | 39.2 % | −211.20 | −21.1 % | 0.84 | −0.09 | 308.70 (29.9 %) | 1:2 |
| R2 | M15 | 55 | 32.7 % | −121.56 | −12.2 % | 0.71 | −0.25 | 226.93 (21.6 %) | 1:2 |
| R2 | M5 | 80 | 38.8 % | −52.44 | −5.2 % | 0.91 | −0.06 | 196.58 (18.9 %) | 1:2 |
| R3 | M15 | 12 | 33.3 % | −32.44 | −3.2 % | 0.59 | −0.31 | 69.03 (6.7 %) | 1:2 |
| R3 | M5 | 12 | 33.3 % | −16.07 | −1.6 % | 0.81 | −0.35 | 82.00 (7.7 %) | 1:2 |
| R4 | M15 | 10 | 40.0 % | −0.06 | 0.0 % | 1.00 | +0.10 | 78.52 (7.7 %) | 1:2 |
| R4 | M5 | 15 | 40.0 % | −33.11 | −3.3 % | 0.80 | +0.16 | 162.72 (15.6 %) | 1:2 |
| R5 | M15 | 69 | 31.9 % | −185.10 | −18.5 % | 0.68 | −0.16 | 253.95 (24.6 %) | 1:2 |
| R5 | M5 | 70 | 32.9 % | −194.90 | −19.5 % | 0.68 | −0.17 | 275.15 (26.5 %) | 1:2 |
| R6 | M15 | 21 | 33.3 % | +33.17 | +3.3 % | 1.31 | −0.27 | 33.97 (3.2 %) | 1:1.5 |
| R6 | M5 | 39 | 43.6 % | +121.89 | +12.2 % | 1.72 | −0.02 | 51.16 (5.0 %) | 1:1.5 |
| R7 | M15 | 4 | 25.0 % | −40.54 | −4.1 % | 0.30 | −0.47 | 57.88 (5.8 %) | 1:2 |
| R7 | M5 | 4 | 25.0 % | −38.84 | −3.9 % | 0.31 | −0.47 | 71.97 (7.1 %) | 1:2 |
| R8 | M15 | 0 | | 0.00 | 0.0 % | | | 0.00 | |
| R8 | M5 | 0 | | 0.00 | 0.0 % | | | 0.00 | |
| R9 | M15 | 6 | 16.7 % | −30.16 | −3.0 % | 0.35 | −0.53 | 41.54 (4.1 %) | 1:1.5 |
| R9 | M5 | 16 | 43.8 % | −0.32 | 0.0 % | 1.00 | +0.02 | 37.28 (3.6 %) | 1:1.5 |

All candidate M1 runs are INCOMPLETE for the same reason as A, B and C.

### Interpretation

- **A v2, B v2 and C v4 all lost money on M15 and M5 in October**, between −5.8 % (B on M5) and −24.7 % (A on
  M5), with drawdowns up to 38 % for A. B traded only 15 times per timeframe, too few to judge.
- **The 60-minute time exit dominates.** Most trades ended by time (46 of 72 for A on M15, 50 of 90 for C on
  M15). The 1:2 target was reached on only 1-14 trades per run, so the realised reward-to-risk was well below the
  planned 1:2. The frozen versions were not changed; any change would be a new version, validated separately.
- **Costs mattered:** 22-34 % of the absolute gross result for A and C (spread, $3.50 per lot per side
  commission, slippage).
- **Session pattern (A):** London/New York trades were positive (+$141 on M15, +$194 on M5 over 11 trades each)
  while Asian-session trades lost (−$139 and −$190 over 26-27 trades). This is one month; it is a question for a
  future validated version, not a finding.
- **Candidates:** R6 is the only one with a positive October net (+$33 on M15, +$122 on M5). **Its mean R is
  negative** (−0.27 and −0.02). With fixed 0.02 lots, monetary risk varies with the stop distance. One M15 trade
  had a $105 risk (a $52.49 stop distance) and closed by time at +0.31 R (+$32.88); the other 20 trades together
  made +$0.29. R6 also lost on both research parts before October. It is not evidence of an edge.
- R1 had the strongest development result (+0.10 R over 205 trades) and then lost on the check part and in
  October. R4 and R9 were near zero; R2, R3 and R5 lost.
- **R7 and R8 are too selective to judge:** 6 and 2 development trades, then 4 and 0 trades in October. R9
  traded 6-16 times. Judging them needs much more history (or looser rules, which would be new candidates,
  researched before the evaluation month).

---

## 7. Tests and quality

| Check | Result |
|-------|--------|
| Full suite | **534 passed** (`python -m pytest`), 1 third-party deprecation warning |
| Ruff | `ruff check .`: all checks passed |
| Phase 5 tests | `tests/test_phase5_monthly.py`: 19 tests (17 at first; two added on 14 Nov with the M1 import fixes, §8) |
| Mutation check | 10 hand-made mutations of the Phase 5 code: research reading month candles, an M15 run executing on M5 candles, freeze skipping the fingerprint check, an insufficient timeframe still run, risk without the contract size, margin without leverage, a risk override applied silently, an incomplete month accepted, a widened RR range, drawdown from balance only. **10 of 10 caught.** |

Phase 5 tests cover:
- research uses only data before the month (a test mutates every month candle and gets identical research);
- freeze re-checks fingerprints and refuses unready strategies;
- the nine runs, their statuses, and INCOMPLETE with the data reason;
- a run on M15 executes on M15 candles (candle count and exit-time alignment);
- $1,000 balance, 1:500 margin, configured sizing, the RR check;
- reports, PDF/CSV/JSON exports, reproduction hashes;
- the application database is unchanged after a full evaluation (table counts and schema version);
- the service runs at below-normal priority (subprocess test);
- CSRF header and month validation (only completed months);
- the new `level_break` condition reads only confirmed break events;
- an MT5 export with a `<SPREAD>` column is refused without a point size and imported with one, into the research
  store only;
- the history rule counts from the first trading bar when the lookback starts on a weekend, and still fails a
  history that starts later.

Phase 4 tests adjusted (none weakened): the migration test now passes the explicit `migrations_dir`; the MT5
first-tick test now expects detection only after a new tick, and two regression tests were added (stale weekend
tick, configurable backfill depth with fallback).

---

## 8. Incomplete criteria

| Criterion | Status |
|-----------|--------|
| M1 runs for A v2, B v2, C v4 (and the candidates) | INCOMPLETE in the application-store evaluations (the stored M1 history starts on 11 November). **Completed in Phase 5.1** from the user's MT5 export: evaluation `me_23932a71a56a` (§12). |
| Everything else in the brief | Done |

**How to complete M1 for October.** An evaluation reads every timeframe from one store and series (runs never
mix sources), so the research-store series needs M5, M15 and H1 as well as M1:

1. In MetaTrader 5, *View > Symbols > Bars*, symbol XAUUSD, export each to CSV with *Export Bars*:

   | Timeframe | From | To | Why |
   |-----------|------|----|-----|
   | M1 | 21 Sep 2026 | 1 Nov 2026 | October runs, and history that starts **before** the weekend of 26-27 Sep (5-day rule) |
   | M5 | 20 Jul 2026 | 1 Nov 2026 | research window (from 2 Aug) and the M5 runs |
   | M15 | 20 Jul 2026 | 1 Nov 2026 | M15 runs and native history |
   | H1 | 1 May 2026 | 1 Nov 2026 | native higher-timeframe history before the analysis base |

   A start date on a weekend gives a file that begins at the Monday open, which can fail the 5-day rule.
2. Import each file into the research store with `POST /api/monthly/import` (multipart form: `file`,
   `dialect=mt5_export`, `timeframe`, `time_basis=rule:ny_close`, `series_label` (the same for all four files),
   `point=0.01`, and the `X-GoldScalp-Request: 1` header). It writes only to `var/monthly/monthly.sqlite3`; the
   page has no upload form yet.
   - `point` converts the export's `<SPREAD>` column from points to dollars. It is required for MT5 exports and
     never assumed. The endpoint did not accept it at first; this was fixed on 14 Nov and covered by a test.
3. Create a new October evaluation with store `research` and that series, then research, freeze and run. M1 runs
   become COMPLETED when execution coverage reaches 95 % and the history rule is met.

**Done on 14 Nov.** The user exported four files from MT5. They were imported into the research store as
`csv:vantage-mt5-xauusd`, with no rows rejected and no bar outside trading hours:

| File | Bars | From (UTC) | To (UTC) | Missing / expected |
|------|-----:|------------|----------|-------------------:|
| `XAUUSD_M1_202609280100_202610302356.csv` | 34,403 | 2026-09-27 22:00 | 2026-10-30 20:56 | 94 / 34,497 |
| `XAUUSD_M5_202607200100_202610302355.csv` | 20,665 | 2026-07-19 22:00 | 2026-10-30 20:55 | 35 / 20,700 |
| `XAUUSD_M15_202607200100_202610302345.csv` | 6,890 | 2026-07-19 22:00 | 2026-10-30 20:45 | 10 / 6,900 |
| `XAUUSD_H1_202605010100_202610302300.csv` | 3,001 | 2026-04-30 22:00 | 2026-10-30 20:00 | 12 / 3,013 |

- **Same data as the application.** Every exported M5, M15 and H1 bar in the range both stores hold is identical
  to the app's stored MT5 candles, open, high, low, close and spread (20,665, 6,890 and 2,495 bars).
- **October coverage on this series:** M15 2,016 of 2,016, M5 6,048 of 6,048, **M1 30,190 of 30,240 (99.8 %)**.
  The research window is the same as the app store's (2 Aug 22:00 to 1 Oct, 59 days, 99.7 %).

Two fixes were needed, each with a test:
1. **Import point size.** The research service's import endpoint did not accept `point`, so every MT5 export
   (which has a `<SPREAD>` column in points) was refused. It now takes `point` (0.01 for XAUUSD) and still refuses
   a spread column without one.
2. **History rule on weekends.** The M1 file starts at the Sunday-evening reopen, 27 Sep 22:00 UTC. The 5-day rule
   asked for data from Saturday 26 Sep 00:00, i.e. Friday bars that a run never loads, because a run's warm-up
   starts at (month start − 5 days) and the first bar it can load after a weekend is the reopen. The rule now
   requires history from the **first scheduled bar** at or after that point. A history that starts later still
   fails. The app store's results are unaffected: its history starts in June.

---

## 9. Assumptions

- October 2026 is the evaluation month because it is the latest completed month with M15 and M5 coverage of
  100 %. November is not complete. No month was substituted.
- Fixed 0.02 lots is each frozen strategy's **configured** sizing, used as configured. The research candidates use
  the same 0.02 lots so that they compare like for like with A, B and C. Monetary risk therefore varies per trade
  with the stop distance. The per-run average was $19-34 per trade for A, B and C and $9-36 for the candidates
  (0.9-3.6 % of $1,000); single trades can risk much more (§10).
- The contract comes from the actual terminal's verification record; tick value is derived, not read from an
  account.
- Costs: the candle spread (known for 100 % of October candles), $3.50 per lot per side commission, $0.05 adverse
  slippage. Vantage's real commission depends on the account type.
- Candles are bid candles; ask is bid plus the candle spread.
- The development/check split of the research window is 70/30, and a variant needs at least 10 development trades
  to be chosen on merit (otherwise the 1:2 default is used and flagged).

## 10. Limitations

- **One month.** All results are from a single historical month: small samples (often fewer than 30 trades),
  no statistical significance, and no evidence of future performance.
- **Research window.** Only about two months of M5 history before October (59 days) were available for research.
  The check part (30 %) is about 18 days.
- **Fixed lots with variable stops** make dollar results and R results diverge: a trade with a wide stop risks
  more dollars. One R6 trade on M15 risked $105 (10.5 % of the account). Mean R and net USD can have different
  signs, and the report shows both.
- **Time exits dominate** the frozen strategies (60-minute maximum hold): for A on M15, 46 of 72 trades closed
  by time. Their realised R is not the planned 1:2.
- **Exposure rejections.** With one open trade allowed, later signals are rejected while a trade is open; the
  run report lists them.
- **Bar-level execution.** Intrabar order is unknown; stop-first is assumed when a candle touches both stop and
  target.
- **M1** is missing for October (§8).
- **Data source.** One broker's demo feed (Vantage, `VantageMarkets-Demo`). Other brokers' spreads and prices
  differ.

## 11. Files

- **New:**
  - `goldscalp/monthly/`: `__init__.py`, `__main__.py`, `store.py`, `data.py`, `candidates.py`, `runner.py`,
    `report.py`, `service.py`, `standalone.py`, `migrations/0001_monthly.sql`
  - `research.py` (launcher)
  - `web/monthly.html`, `web/js/monthly.js`
  - `tests/test_phase5_monthly.py`
  - `.env` (time basis and backfill depths)
  - `PHASE5_ARCHITECTURE.md`, `PHASE_5_IMPLEMENTATION_REPORT.md`
  - launch configuration `goldscalp-research` in `C:\Spicy\.claude\launch.json`
- **Modified:**
  - `goldscalp/data/providers/mt5_provider.py`: new-tick rule for time-basis detection; configurable backfill
    depths with fallback
  - `goldscalp/core/config.py`, `.env.example`: backfill depths, `monthly_port`
  - `goldscalp/api/app.py`: passes the backfill depths to the provider
  - `goldscalp/storage/db.py`: `migrations_dir` argument; foreign-keys-off migration directive
  - `goldscalp/backtesting/engine.py`: rejected-entry diagnostics; balance and used margin on the equity points
    (result hashes unchanged)
  - `goldscalp/backtesting/features.py`: `ind` block (EMA 9/20/50/100/200, RSI 14)
  - `goldscalp/strategies/builder.py`, `conditions.py`: `ema_trend`, `momentum`, `level_break`
  - `web/css/app.css`; navigation link in `web/index.html`, `paper.html`, `performance.html`, `replay.html`,
    `lab.html`, `live.html`
  - `tests/test_mt5.py`, `tests/test_phase4_live.py`
  - `README.md`, `PROPOSED_ARCHITECTURE.md` (revision 7), `IMPLEMENTATION_ROADMAP.md`,
    `PHASE_4_IMPLEMENTATION_REPORT.md` (§8 incident note)

---

## 12. Phase 5.1: October M1, historical import, percentage-risk scenarios (14 November 2026)

> **High-risk hypothetical simulation scenarios — not real-account settings or trading recommendations.** The 5 %,
> 7.5 % and 10 % scenarios are stress tests on one historical month in separate hypothetical $1,000 accounts. They
> are never applied to MT5, Vantage, the live paper monitor or any account, and no scenario is recommended.

Design: PHASE5_ARCHITECTURE.md §12. The original fixed-lot results (§6) are kept unchanged as the
**Baseline — Fixed 0.02 lots**; everything below is in new evaluation records.

### 12.1 Acceptance criteria

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Import M1 CSV history through the web interface | Done: page section 1 (preview / validate, import with progress, coverage, history). Checked in the browser with a test file (preview only). |
| 2 | Verify M1 coverage and warm-up history | Done: section 2 and `GET /api/monthly/coverage`. October M1 30,190 of 30,240 bars (99.8 %), warm-up satisfied. |
| 3 | October M1 runs complete when coverage meets the threshold | Done: `me_23932a71a56a` completed A/B/C on M15, M5 **and M1** (12.3) |
| 4 | Fixed-lot baseline results unchanged | Verified byte for byte (12.5) |
| 5 | Percentage sizing at 5 %, 7.5 % and 10 % | Done (engine mode `risk_percent_equity`) |
| 6 | All 27 A/B/C risk-based runs complete | **27 of 27 COMPLETED** (risk study `rs_d9e49bedf913`) |
| 7 | A separate $1,000 account per run | Yes: every run has its own balance, equity curve, sizing, ledger and margin |
| 8 | All reports and comparisons generated | 27 run reports (PDF / CSV / JSON); per strategy: timeframes and scenarios (JSON + PDF); master (JSON, PDF, CSV) |
| 9 | Sizing includes spread, commission, slippage and stop distance | Yes (formula in 12.4) |
| 10 | Rejected entries and budget violations recorded | Yes: 0 budget violations in 27 runs; rejections by reason in every report |
| 11 | Reproducible | Yes; see 12.5, including a cache defect found and fixed during verification |
| 12 | Phase 4 monitoring, MT5 and paper accounts untouched | Yes (12.6) |
| 13 | All tests pass, Ruff clean | **584 passed** (full suite, which also contains another session's tests; `tests/test_phase51_risk.py` 18 cases, `tests/test_phase5_monthly.py` 19); Ruff: all checks passed |

### 12.2 M1 import status and coverage

The user's MT5 exports (M1, M5, M15, H1) are in the research store as `csv:vantage-mt5-xauusd` (§8 lists the
files). They were imported through the API, as the page's upload section did not exist yet; the page now offers the
same import with validation. October coverage on that series:

| Timeframe | Expected | Present | Coverage | Missing | Pre-month history | Warm-up |
|-----------|---------:|--------:|---------:|--------:|-------------------|---------|
| M15 | 2,016 | 2,016 | 100 % | 0 | from 2026-07-19 22:00 | satisfied |
| M5 | 6,048 | 6,048 | 100 % | 0 | from 2026-07-19 22:00 | satisfied |
| **M1** | **30,240** | **30,190** | **99.8 %** | **50** (22 dates, 23 intervals; never filled) | from 2026-09-27 22:00 (Sunday reopen) | satisfied |

The imported M5, M15 and H1 bars are identical to the application's own MT5 candles: open, high, low, close and
spread, for every bar in the range both stores hold.

### 12.3 Fixed-lot baseline with M1 (`me_23932a71a56a`, research store)

| Strategy | TF | Status | Trades | Win rate | Net | Return | PF | Max DD | Costs | Execution candles |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | M15 | COMPLETED | 72 | 31.9 % | −$227.31 | -22.7 % | 0.62 | 31.2 % | $54.82 | 2,016 |
| A | M5 | COMPLETED | 69 | 30.4 % | −$247.18 | -24.7 % | 0.63 | 38.4 % | $52.54 | 6,048 |
| A | M1 | COMPLETED | 69 | 30.4 % | −$247.18 | -24.7 % | 0.63 | 38.4 % | $52.54 | 30,190 |
| B | M15 | COMPLETED | 15 | 26.7 % | −$95.06 | -9.5 % | 0.45 | 12.1 % | $11.40 | 2,016 |
| B | M5 | COMPLETED | 15 | 40.0 % | −$57.90 | -5.8 % | 0.64 | 9.6 % | $11.40 | 6,048 |
| B | M1 | COMPLETED | 15 | 40.0 % | −$57.90 | -5.8 % | 0.64 | 9.8 % | $11.40 | 30,190 |
| C | M15 | COMPLETED | 90 | 35.6 % | −$240.81 | -24.1 % | 0.65 | 32.3 % | $68.50 | 2,016 |
| C | M5 | COMPLETED | 97 | 36.1 % | −$233.77 | -23.4 % | 0.69 | 32.1 % | $73.82 | 6,048 |
| C | M1 | COMPLETED | 97 | 36.1 % | −$233.77 | -23.4 % | 0.69 | 32.2 % | $73.82 | 30,190 |

- **Cross-check with the application store.** On M15 and M5 every run (A/B/C and R1-R9) has the same trades and the
  same net result as `me_697122f9e14b`. The result hashes differ only because each trade's `signal_id` embeds
  the data series' name.
- **M1 equals M5 for A, B and C.** M1 and M5 have the same setups and entry times: A 69 of 69, B 15 of 15, C 97 of 97.
  The net results are identical. The frozen rules react only to closed M5, M15 and H1 candles, so deciding every
  minute fires at the same moments. M1 execution changed only some exit timestamps within the bar (A 16 of 69, B 4 of 15,
  C 39 of 97); every exit price and every trade's net result is identical, and drawdowns marked to market slightly differently. For these rule sets, M1 adds execution
  detail, not independent evidence.
- Candidates on M1: R6 +$45.54 (59 trades, but mean R −0.27: again the size of a few wide-stop trades, not the
  average trade), R9 −$0.32, R4 −$110.05, the others negative; R8 again no trade.

### 12.4 Percentage-risk scenarios (risk study `rs_d9e49bedf913`)

**Sizing, per entry:**

```
risk budget          = current simulated equity x risk %
planned loss per lot = (|entry - stop| + 0.05 slippage) x 100 oz + 2 x 3.50 commission
lots                 = budget / planned loss per lot, rounded DOWN to 0.01; capped at 100 (broker) and 1.00 (strategy)
rejected             if 0.01 lot would exceed the budget, if there is no stop, or if 1:500 margin does not fit
```

Worked example (A, M5, 10 %, first trade): equity $1,000.00, so the budget is $100.00. The stop was $10.78 from the
entry, so the planned loss is $1,090.12 per lot. That gives 0.09 lots, a planned risk of $98.11. The trade lost
$13.59 at the time exit.

**Ending balance of each separate $1,000 account (return):**

| Strategy | TF | Baseline fixed 0.02 lots | 5% of equity | 7.5% of equity | 10% of equity |
|---|---|---|---|---|---|
| A | M15 | $772.69 (−22.7 %) | $551.62 (−44.8 %) | $372.85 (−62.7 %) | $281.11 (−71.9 %) |
| A | M5 | $752.82 (−24.7 %) | $510.31 (−49.0 %) | $330.34 (−67.0 %) | $245.66 (−75.4 %) |
| A | M1 | $752.82 (−24.7 %) | $510.31 (−49.0 %) | $330.34 (−67.0 %) | $245.66 (−75.4 %) |
| B | M15 | $904.94 (−9.5 %) | $870.47 (−13.0 %) | $774.84 (−22.5 %) | $695.80 (−30.4 %) |
| B | M5 | $942.10 (−5.8 %) | $876.91 (−12.3 %) | $854.85 (−14.5 %) | $804.65 (−19.5 %) |
| B | M1 | $942.10 (−5.8 %) | $876.91 (−12.3 %) | $854.85 (−14.5 %) | $804.65 (−19.5 %) |
| C | M15 | $759.19 (−24.1 %) | $784.20 (−21.6 %) | $654.22 (−34.6 %) | $551.09 (−44.9 %) |
| C | M5 | $766.23 (−23.4 %) | $661.87 (−33.8 %) | $505.33 (−49.5 %) | $369.16 (−63.1 %) |
| C | M1 | $766.23 (−23.4 %) | $661.87 (−33.8 %) | $505.33 (−49.5 %) | $369.16 (−63.1 %) |

**Risk detail (every row a separate account):**

| Strategy | TF | Sizing | Trades | PF | Max DD | Longest losing streak | Avg / max lots | Avg budget | Max planned risk | Min-volume rej. | Margin rej. |
|---|---|---|---:|---:|---:|---|---|---:|---:|---:|---:|
| A | M15 | Baseline fixed 0.02 lots | 72 | 0.62 | 31.2 % | 8 (−$113.80) | 0.020 / 0.02 | - | $97.95 | 0 | 0 |
| A | M15 | 5% of equity | 68 | 0.54 | 53.8 % | 8 (−$153.42) | 0.044 / 0.43 | $36.55 | $49.79 | 5 | 0 |
| A | M15 | 7.5% of equity | 68 | 0.51 | 71.5 % | 8 (−$170.95) | 0.057 / 0.52 | $45.58 | $82.98 | 5 | 0 |
| A | M15 | 10% of equity | 68 | 0.53 | 80.2 % | 8 (−$186.17) | 0.060 / 0.31 | $54.03 | $116.17 | 4 | 1 |
| A | M5 | Baseline fixed 0.02 lots | 69 | 0.63 | 38.4 % | 11 (−$189.29) | 0.020 / 0.02 | - | $97.95 | 0 | 0 |
| A | M5 | 5% of equity | 63 | 0.40 | 56.3 % | 10 (−$230.09) | 0.026 / 0.08 | $33.61 | $45.00 | 7 | 0 |
| A | M5 | 7.5% of equity | 63 | 0.35 | 73.5 % | 10 (−$281.31) | 0.032 / 0.11 | $39.92 | $69.80 | 7 | 0 |
| A | M5 | 10% of equity | 63 | 0.36 | 82.0 % | 10 (−$297.98) | 0.038 / 0.14 | $46.15 | $98.11 | 7 | 0 |
| A | M1 | Baseline fixed 0.02 lots | 69 | 0.63 | 38.4 % | 11 (−$189.29) | 0.020 / 0.02 | - | $97.95 | 0 | 0 |
| A | M1 | 5% of equity | 63 | 0.40 | 56.3 % | 10 (−$230.09) | 0.026 / 0.08 | $33.61 | $45.00 | 7 | 0 |
| A | M1 | 7.5% of equity | 63 | 0.35 | 73.5 % | 10 (−$281.31) | 0.032 / 0.11 | $39.92 | $69.80 | 7 | 0 |
| A | M1 | 10% of equity | 63 | 0.36 | 82.0 % | 10 (−$297.98) | 0.038 / 0.14 | $46.15 | $98.11 | 7 | 0 |
| B | M15 | Baseline fixed 0.02 lots | 15 | 0.45 | 12.1 % | 4 (−$62.18) | 0.020 / 0.02 | - | $127.07 | 0 | 0 |
| B | M15 | 5% of equity | 14 | 0.45 | 15.6 % | 3 (−$110.26) | 0.031 / 0.06 | $45.91 | $49.28 | 1 | 0 |
| B | M15 | 7.5% of equity | 15 | 0.39 | 25.1 % | 4 (−$96.22) | 0.043 / 0.09 | $64.73 | $72.07 | 0 | 0 |
| B | M15 | 10% of equity | 15 | 0.37 | 34.3 % | 4 (−$116.56) | 0.056 / 0.11 | $81.13 | $98.56 | 0 | 0 |
| B | M5 | Baseline fixed 0.02 lots | 15 | 0.64 | 9.6 % | 3 (−$64.93) | 0.020 / 0.02 | - | $48.33 | 0 | 0 |
| B | M5 | 5% of equity | 15 | 0.51 | 15.1 % | 3 (−$97.39) | 0.029 / 0.05 | $45.51 | $49.28 | 0 | 0 |
| B | M5 | 7.5% of equity | 15 | 0.59 | 20.0 % | 3 (−$143.90) | 0.045 / 0.08 | $66.30 | $70.22 | 0 | 0 |
| B | M5 | 10% of equity | 15 | 0.57 | 25.9 % | 3 (−$183.10) | 0.057 / 0.10 | $84.71 | $98.56 | 0 | 0 |
| B | M1 | Baseline fixed 0.02 lots | 15 | 0.64 | 9.8 % | 3 (−$64.93) | 0.020 / 0.02 | - | $48.33 | 0 | 0 |
| B | M1 | 5% of equity | 15 | 0.51 | 15.3 % | 3 (−$97.39) | 0.029 / 0.05 | $45.51 | $49.28 | 0 | 0 |
| B | M1 | 7.5% of equity | 15 | 0.59 | 20.4 % | 3 (−$143.90) | 0.045 / 0.08 | $66.30 | $70.22 | 0 | 0 |
| B | M1 | 10% of equity | 15 | 0.57 | 26.5 % | 3 (−$183.10) | 0.057 / 0.10 | $84.71 | $98.56 | 0 | 0 |
| C | M15 | Baseline fixed 0.02 lots | 90 | 0.65 | 32.3 % | 8 (−$82.32) | 0.020 / 0.02 | - | $97.95 | 0 | 0 |
| C | M15 | 5% of equity | 89 | 0.83 | 46.4 % | 8 (−$172.89) | 0.062 / 0.62 | $37.11 | $42.81 | 1 | 0 |
| C | M15 | 7.5% of equity | 88 | 0.78 | 61.7 % | 8 (−$208.66) | 0.068 / 0.54 | $44.92 | $66.55 | 1 | 1 |
| C | M15 | 10% of equity | 87 | 0.74 | 73.0 % | 8 (−$232.22) | 0.069 / 0.57 | $48.79 | $92.89 | 1 | 2 |
| C | M5 | Baseline fixed 0.02 lots | 97 | 0.69 | 32.1 % | 7 (−$78.77) | 0.020 / 0.02 | - | $77.53 | 0 | 0 |
| C | M5 | 5% of equity | 95 | 0.75 | 49.0 % | 7 (−$122.06) | 0.049 / 0.31 | $33.25 | $45.93 | 2 | 0 |
| C | M5 | 7.5% of equity | 97 | 0.72 | 63.9 % | 7 (−$161.07) | 0.059 / 0.35 | $40.56 | $66.38 | 0 | 0 |
| C | M5 | 10% of equity | 97 | 0.66 | 76.0 % | 7 (−$169.63) | 0.061 / 0.32 | $42.83 | $91.86 | 0 | 0 |
| C | M1 | Baseline fixed 0.02 lots | 97 | 0.69 | 32.2 % | 7 (−$78.77) | 0.020 / 0.02 | - | $77.53 | 0 | 0 |
| C | M1 | 5% of equity | 95 | 0.75 | 49.0 % | 7 (−$122.06) | 0.049 / 0.31 | $33.25 | $45.93 | 2 | 0 |
| C | M1 | 7.5% of equity | 97 | 0.72 | 63.9 % | 7 (−$161.07) | 0.059 / 0.35 | $40.56 | $66.38 | 0 | 0 |
| C | M1 | 10% of equity | 97 | 0.66 | 76.0 % | 7 (−$169.63) | 0.061 / 0.32 | $42.83 | $91.86 | 0 | 0 |

**Reading the results (one month; not a recommendation):**
- **All 27 percentage-risk runs lost money, and higher risk magnified the losses in every strategy and timeframe.**
  At 10 %, A ended at $245.66 (−75.4 %) on M5 and M1 with an 82 % maximum drawdown. B, the smallest loser, still
  lost 19.5 % at 10 % on M5 and M1.
- **Losing streaks drive the damage under compounding:** A had 10 losses in a row on M5. Each loss shrinks the
  next budget, but a 10 % budget still lost $297.98 over that streak.
- **No entry exceeded its budget** (0 violations), and **no loss went beyond the planned stop risk** in October:
  stops filled within their bars, not through gaps.
- **Minimum-volume rejections (19-25 per scenario across A/B/C):** when the structure stop is wide, even 0.01 lot
  would risk more than the budget, so the entry is refused. The fixed-lot baseline took those trades. Its planned
  risk on a single wide-stop trade reached $97.95 (A) and $127.07 (B), i.e. 9.8-12.7 % of the account.
- **Margin rejections:** 0 at 5 %, 1 at 7.5 % and 3 at 10 % (narrow stops make large lots). 1:500 never sized a
  position; it only refused these entries.
- **Fixed lots vs percentage sizing:** only C on M15 at 5 % ended above its fixed-lot baseline ($784.20 vs
  $759.19). Percentage sizing takes small positions on wide stops and larger ones on narrow stops; on C's M15
  trades that helped slightly. Every other scenario ended below its baseline.
- Trade counts differ slightly between scenarios (e.g. C M15: 89 / 88 / 87) because sizing rejections and margin
  change which later signals can be taken. The strategies' signals themselves are identical (tested).
- Costs grow with position size: C on M15 paid $68.50 at fixed lots, $211-229 in the scenarios.

### 12.5 Reproducibility and preservation of the original results

- **Byte-for-byte preservation.** Every run of `me_cb6b197c4c6b` (27), `me_697122f9e14b` (36) and
  `me_23932a71a56a` (36) was snapshotted (status, result hash, digest of the stored result and dataset). They were
  compared after each research-service restart, after the study and after the M15 re-run: **identical** every time.
- **New code, old results.** `me_697122f9e14b_A_M15` (Phase 5, application store) was re-run from scratch with the
  Phase 5.1 engine: identical result hash `7d7a00cc…`.
- **Defect found during verification, fixed and re-run.** A from-scratch reproduction of `me_86208dc6607e_B_M15`
  (5 %) gave a different hash.
  - **Cause:** the in-memory timeline cache (Phase 3) keyed timelines by candle content, not by series. The study's
    M15 timeline had been served from the cache filled by the application-store reproduction above, because the
    exported candles are identical. The study's M15 trades therefore carried the application series' signal IDs.
    Prices, sizes and P/L were identical.
  - **Fix:** the cache key now includes the series and source. A test proves that two series with identical candles
    no longer share a timeline.
  - **Re-run:** the 9 M15 runs were re-run through a recorded re-run (time, reason and every replaced hash are
    stored in the study). Only those 9 runs changed. Every M15 figure in the tables above is identical before and
    after; the stored B M15 5 % hash now equals the independent from-scratch reproduction (`1e9cdeee…`). M5 and M1
    were unaffected: their timelines were computed fresh, and their signal IDs match the series.
- Reproduction inputs per run: frozen configuration and fingerprint, dataset and execution hashes, engine assumptions
  including `sizing_override`, and the code version (`goldscalp 0.1.0 / code …` in each evaluation).

### 12.6 Isolation and Phase 4 health

- The research service was restarted five times, each time only after confirming it had no running job. The
  restarts loaded the import fix, the history rule fix, Phase 5.1, the ledger columns and the cache fix. The main application
  (process started 17:55, never restarted), the MT5 terminal and the Phase 4 monitors were never touched.
- The last check before the final restart: monitor `live@1` running with no error. A v2 (#2), B v2 (#3) and C v4
  (#4) were active in paper mode with no open simulated trades, and MT5 was connected (market closed for the
  weekend).
- The monthly package still imports nothing from MT5, the paper engine or the live monitor, and contains no
  `order_send` (safety test over every module, including the new `risk.py` and `importing.py`). The end-to-end
  tests show the application database's table counts unchanged after a full study.
- Unrelated to Phase 5.1, at the user's request: the dashboard's quote panel overflowed under the chart, because the
  provider `<select>` refused to shrink below its longest option. Fixed in `web/css/app.css` (static file; no
  restart) and checked at 1440 px, 1000 px and phone width.

### 12.7 All run IDs

- **Baseline (fixed 0.02 lots, M1-complete):** evaluation `me_23932a71a56a`; runs `me_23932a71a56a_{A|B|C|R1-R9}_{M15|M5|M1}`
  (36, all COMPLETED).
- **S1 5 %**: evaluation `me_86208dc6607e` (COMPLETED): runs `me_86208dc6607e_A_M15`, `me_86208dc6607e_A_M5`, `me_86208dc6607e_A_M1`, `me_86208dc6607e_B_M15`, `me_86208dc6607e_B_M5`, `me_86208dc6607e_B_M1`, `me_86208dc6607e_C_M15`, `me_86208dc6607e_C_M5`, `me_86208dc6607e_C_M1`
- **S2 7.5 %**: evaluation `me_7553ae541419` (COMPLETED): runs `me_7553ae541419_A_M15`, `me_7553ae541419_A_M5`, `me_7553ae541419_A_M1`, `me_7553ae541419_B_M15`, `me_7553ae541419_B_M5`, `me_7553ae541419_B_M1`, `me_7553ae541419_C_M15`, `me_7553ae541419_C_M5`, `me_7553ae541419_C_M1`
- **S3 10 %**: evaluation `me_e62affa41605` (COMPLETED): runs `me_e62affa41605_A_M15`, `me_e62affa41605_A_M5`, `me_e62affa41605_A_M1`, `me_e62affa41605_B_M15`, `me_e62affa41605_B_M5`, `me_e62affa41605_B_M1`, `me_e62affa41605_C_M15`, `me_e62affa41605_C_M5`, `me_e62affa41605_C_M1`

### 12.8 Files (Phase 5.1)

- **New:** `goldscalp/monthly/risk.py`, `goldscalp/monthly/importing.py`,
  `goldscalp/monthly/migrations/0002_risk_scenarios.sql`, `tests/test_phase51_risk.py`.
- **Modified:**
  - `goldscalp/backtesting/engine.py`: the `risk_percent_equity` mode and `size_risk_of_equity`; unchanged for
    other modes.
  - `goldscalp/backtesting/features.py`: the cache key includes the series.
  - `goldscalp/monthly/`: `runner.py` (scenario assumptions, readiness), `report.py` (risk block, charts, losing
    streak, ledger columns, study PDFs/CSV), `service.py` (import preview / labels / coverage, risk studies, point
    size), `store.py` (studies, labels), `data.py` (history rule).
  - Web and tests: `web/monthly.html`, `web/js/monthly.js`, `web/css/app.css` (dashboard overflow),
    `tests/test_phase5_monthly.py` (point size, history rule).
  - Docs: `PHASE5_ARCHITECTURE.md` (§12), `README.md`, `IMPLEMENTATION_ROADMAP.md`, this report.

### 12.9 Limitations

- **One month, small samples.** Results are not evidence of future performance, and B traded only 15 times per run.
- **Compounding within a month only.** Each scenario restarts at $1,000 and closes positions at the month end.
- **Contract and costs:** tick value is derived (contract size × tick size), commission is an assumption
  ($3.50 per lot per side), and the margin rule is a hypothetical 1:500 of notional.
- **Gaps:** the engine sees bar opens, not ticks. A stop gapped through at a bar open is recorded beyond plan;
  slippage within a bar beyond the fixed $0.05 is not modelled.
- **M1 history depth:** the imported M1 starts at the 27 September reopen (enough under the warm-up rule). Earlier
  months need their own exports.
- **Earlier imports have no label:** the four files were imported before labels existed, so their history rows
  show no dataset label.
- **Research candidates** were not included in this study (optional; available per study).
