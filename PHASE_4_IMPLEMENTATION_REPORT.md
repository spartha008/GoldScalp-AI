# GoldScalp AI Phase 4 Implementation Report: AI Research Assistant, Vantage MT5 Demo Integration and Live Strategy Evaluation

**Date:** 14 November 2026 (Saturday; XAUUSD weekly close until Sunday 15 November 23:00 UTC).

**Status: built and tested. The actual Vantage demo terminal was checked: PARTIAL, 11 PASS, 3 WARN, 0 FAIL (§2).**
- The terminal is attached, connected to the Vantage demo server `VantageMarkets-Demo`, the symbol was discovered, all four timeframes are clean and reconnect works.
- The newest tick is from Friday's close, because the market is closed until Sunday 15 November 23:00 UTC.
- A fresh live tick, and therefore a VERIFIED record, can only come after the open. It is not claimed here.

Design: [PHASE4_ARCHITECTURE.md](PHASE4_ARCHITECTURE.md) (§14 is the as-built record).

> **Guiding rule.** Phase 4 is a research assistant, not an AI that changes strategies on recent results.
> - Strategy configurations change only through the user's **Approve** action on a proposal that was validated
>   on unseen historical data, which creates a **new version**.
> - Market data is read-only.
> - Every trade is SIMULATED in GoldScalp's paper simulator.

> **Note added 15 November 2026.** This report describes Phase 4 as delivered on 14 November 2026. Later that day an
> optional **MT5 demo-account execution** feature was added. It lives in `goldscalp/execution/`, with migration
> `0007` and an eighth `/live` tab, *Demo Execution*, and it is off by default (`GOLDSCALP_DEMO_EXECUTION_ENABLED`).
> When switched on and linked, it mirrors SIMULATED validation trades as market orders on an account that reports
> itself as DEMO, re-checked before every order. There is no real-money order path. The statements below about the
> read-only allowlist and `account_info` remain true for the market-data provider and the Phase 4 packages. See
> README, "MT5 demo-account execution".

---

## 1. Summary

| Part | Delivered |
|------|-----------|
| 1. Vantage MT5 integration | Broker identification from `terminal_info().company`, or for a generic MetaQuotes build, from the connected trade server. Demo/live detection from the terminal's own `config/common.ini` (`Server=` only; no account data). Automatic gold-symbol discovery. A 14-check verification record, plus an adapter flag, that only the real `MetaTrader5` package can make VERIFIED. Candle integrity and cross-timeframe checks. Feed health (spread statistics, state transitions). Disconnect and reconnect. |
| 2. Live strategy monitor | Opt-in per strategy **version** (must have a completed Phase 3 run). Each step-candle close is evaluated with the **backtest's own as-of snapshot**. Signals carry: strategy name and version, direction, timeframe, supporting and conflicting evidence, invalidation, price, bid/ask/spread, source and freshness. |
| 3. Live paper validation | One SIMULATED position per qualifying signal in that version's own validation paper account (`PaperTradingEngine`, same formulas and costs). Exits: stop, target, time, structure flip. Recorded: MFE/MAE from quotes, realized and unrealized P/L, holding time, costs, strategy and signal ids. |
| 4. Journal | One entry per signal, joined with its trade and evaluation. Filters: date, strategy, session, timeframe, direction, outcome. CSV export; user notes. |
| 5. Per-trade evaluation | `eval@1`: seven answers with verdicts and evidence, inserted once and never updated. |
| 6. Improvement engine | Discovery, embargo and unseen-holdout windows. Findings: false signals, sessions, volatility regimes, HTF combinations, spread stress, invalidation patterns, backtest vs live. Proposals come from a fixed catalogue. Holdout validation against the current version. Adoption by the user only. |
| 7. Research assistant | Nine read-only tools and a deterministic answerer for the seven listed questions, plus connection status, with sources and an audit log. Optional Claude mode: off by default, strict tools, number guard. |
| 8. Dashboard | `/live`, with tabs: Overview, Strategy Monitor, Simulated Positions, Performance, Journal, Improvement, Research Assistant. (An eighth tab, Demo Execution, was added after this report; see the note above.) |
| 9. Tests | 505 passed (444 from Phases 1-3 plus 61 new); ruff clean. Every non-equivalent mutant is caught (§4.3). |
| 10. Security | MT5 allowlist unchanged. No credential fields. Simulation only. CSRF on every new state change. Localhost binding unchanged. Claude mode needs explicit opt-in. |

## 2. Actual Vantage MT5 connection evidence

**Record `mv_214ba65715fb0f86`**, stored in `mt5_verifications` by `POST /api/live/verifications`.
- Run 14 November 2026, 11:52:37 UTC, against the running terminal `C:\Program Files\MetaTrader 5\terminal64.exe`.
- Adapter: **`MetaTrader5`**, the actual package. This is not a test.
- **Overall: PARTIAL**: 11 PASS, 3 WARN, 0 FAIL.
- **The PARTIAL is expected.** The market is in its weekly close, so the newest tick is from Friday. VERIFIED needs a fresh tick after the open (Sunday 23:00 UTC).

| Check | Result | Evidence (from the stored record) |
|-------|--------|-----------------------------------|
| MT5 terminal detected and attached | PASS | MetaTrader 5 build 6317, terminal version 500 / 6317 (11 Nov 2026); `initialize()` attached without any login, password or server. Ping 244 ms; algorithmic trading disabled in the terminal (`trade_allowed` false). |
| Terminal connected to its broker | PASS | `terminal_info().connected` is true |
| Broker is Vantage | PASS (by trade server) | `terminal_info().company` = "MetaQuotes Ltd.", the distributor of this generic MetaTrader 5 build (see the note below). The trade server of the most recent login is `VantageMarkets-Demo`. It is the terminal's only trade-server data folder and the one it wrote last (11:48:44 UTC, at the terminal start). |
| Demo environment | PASS | Server name `VantageMarkets-Demo` contains "demo". Only the `Server=` line of the terminal's `config/common.ini` was used; no account data was read. |
| Gold symbol discovered | PASS | `XAUUSD`, ranked best automatically. The broker's `XAUUSD.crp` ("Not for trading purposes, currency conversion displays only") was pushed down. Digits 2, contract 100 oz, lots 0.01-100 in steps of 0.01. |
| Live bid/ask tick | WARN | bid 3881.73 / ask 3882.07, spread 0.34. Tick time 2026-11-13 21:56:59.906 UTC, about 14 h old because the market is closed. Time basis `rule:ny_close`, *assumed* until a fresh tick allows detection. |
| M1 candles | WARN | 3,000 bars, 11 Nov 17:53 → 13 Nov 21:56; integrity clean. 4 bars "missing": 21:58-21:59 UTC on 11 and 12 Nov. Vantage's XAUUSD daily break starts at 21:58 UTC, two minutes before the 22:00 assumed by GoldScalp's shipped **hypothetical** instrument schedule. This is a schedule difference, not a data defect. |
| M5 candles | PASS | 3,000 bars, 30 Oct 01:00 → 13 Nov 21:55; integrity clean |
| M15 candles | PASS | 2,000 bars, 15 Oct 04:00 → 13 Nov 21:45; integrity clean |
| H1 candles | WARN | 2,000 bars, 15 Jul 20:00 → 13 Nov 21:00; integrity clean. 2 bars "missing" on 7 Sep 2026 (19:00 and 20:00 UTC): US Labor Day, an early close. |
| M5 / M15 / H1 match M1 | PASS ×3 | 598 M5, 197 M15 and 47 H1 bars rebuilt from M1 equal the terminal's own bars (0 differ by more than 0.01) |
| Disconnect and reconnect | PASS | `shutdown()` then `initialize()` again in 0.314 s; a new tick was read |

**Why "MetaQuotes Ltd." is not the broker.**
- `terminal_info().company` names whoever distributed the terminal *build*. A Vantage-branded installer reports Vantage; the generic MetaTrader 5 from MetaQuotes reports "MetaQuotes Ltd." even when logged in to a Vantage server.
- The account's broker is only available through `account_info()`, which GoldScalp deliberately never calls.
- So the identification uses, without opening any account data:
  - the trade server of the last login (the `Server=` line);
  - which trade-server data folder the terminal writes, from folder names and modification times only;
  - the live connection state.
- This was found against the actual terminal and fixed before the run above. The verification states its basis ("identified by trade server"), and two mocked tests cover it.

**Still open:** after the market opens, run the verification again for a fresh tick, then criteria 1 and 2 can be closed.

### 2.1 Live validation set up (14 November 2026, about 11:57 UTC, at the user's request)

| Monitored | Strategy version | Mode | Step | Validation paper account | Phase 3 sample |
|-----------|------------------|------|------|--------------------------|----------------|
| 1 | Example A v2 | paper | M5 | #2, 10,000 USD (SIMULATED) | sufficient (comparison, 37 trades) |
| 2 | Example B v2 | paper | M5 | #3, 10,000 USD (SIMULATED) | **insufficient** (2 trades): results are preliminary |
| 3 | Example C v4 | paper | M5 | #4, 10,000 USD (SIMULATED) | sufficient (comparison, 53 trades) |

- **The user's own paper account is unchanged.** It is still the active manual account, and validation accounts never become active.
- **Monitoring runs only while GoldScalp and the terminal are running.** Signals appear only at M5 closes while data is LIVE, from the Sunday 23:00 UTC open.
- **Weekend fix.** Cut-offs that fall in the weekly close are now skipped without a log entry. Found while setting up: before the fix, every closed-market cut-off would have been logged as a "missed" candle. The fix has a test that fails without it, and it went live before any such row was written.
- **Screenshots:**
  - `docs/phase4/live-overview-actual-terminal.jpg`: this record on the dashboard;
  - `docs/phase4/live-overview-terminal-not-running.jpg`: the state before the terminal was started.

| Acceptance criterion | Status |
|----------------------|--------|
| 1. Actual Vantage terminal detected and verified | **Mostly done**: actual-terminal record `mv_214ba65715fb0f86`, PARTIAL (no FAIL). Detected, attached, connected, Vantage demo server, symbol, candles, reconnect. VERIFIED needs a fresh tick after the open. |
| 2. Live XAUUSD prices received | **Open**: the terminal's last tick (Fri 21:56:59 UTC) was read; a live tick needs the market open |
| 3. All supported timeframes work | **Done on the actual terminal**: M1, M5, M15 and H1 retrieved with clean integrity; M5, M15 and H1 match M1 exactly |
| 4. Phase 3 strategies evaluated against live data | Implemented and tested with parity to the backtest; live run pending |
| 5. Signals timestamped and explained | Done (tests; example in §6) |
| 6. Every simulated trade recorded | Done (tests) |
| 7. Per-trade evaluations generated | Done (tests; example in §6) |
| 8. Strategy weaknesses identified | Done on the stored MT5 history (§5) |
| 9. Improvement proposals validated historically | Done (§5; one proposal, *inconclusive*) |
| 10. Strategy versions tracked | Done (approve creates a version; monitoring stays pinned) |
| 11. Dashboard presents live-paper performance | Built and checked in the browser; no live-paper trades exist yet |
| 12. All previous tests pass | Done: 444 earlier tests pass (one Phase 3 safety test extended, §4.1) |
| 13. Documentation updated | Done |

## 3. What was built

### 3.1 Vantage MT5 (Part 1)

- **`goldscalp/data/providers/mt5_identity.py`:**
  - Vantage is recognised by the whole word "Vantage" in the company ("Advantage …" is rejected).
  - The `common.ini` parser handles UTF-16 and UTF-8, returns only the `[Common] Server=` value, and drops implausible values.
  - Environment: `DEMO` when the server name contains "demo"; `LIVE` when it contains "live" or "real" (warned); otherwise `UNKNOWN`.
- **`goldscalp/data/mt5_verify.py`** (`mt5v@1`): terminal, broker connection, Vantage, environment, symbol, live tick, candles for each of four timeframes, M5/M15/H1-vs-M1 consistency, reconnect, adapter.
  - Overall is FAILED on any FAIL, VERIFIED only if all checks pass, and PARTIAL otherwise.
  - Home-folder paths are stored as `%USERPROFILE%`.
- **`goldscalp/data/integrity.py`:**
  - Errors: invalid OHLC, duplicate, non-increasing or misaligned timestamps, a closed bar in the future, several forming bars.
  - Warnings: missing bars against the schedule, a stale last bar, zero spreads.
  - Also used by `GET /api/live/integrity` on stored candles.
- **`goldscalp/live/feed.py`:** rolling one-hour spread statistics (median, p95, maximum), one `feed_samples` row per minute, and every state transition in `feed_events`.
- **Provider changes:** extra terminal facts (`name`, `path`, `data_path`, `ping_last`, with no account fields; safety test), `adapter_name()`, and the read-only `verification_facts()`. The allowlist (`ALLOWED`) is unchanged.

### 3.2 Monitor, validation, journal and evaluation (Parts 2-5)

- **`goldscalp/live/monitor.py`** (`live@1`) runs every 2 s. For each active monitored version:
  - **Wait** for the M1 candle that closes at the cut-off (120 s grace, else `missed`).
  - **Gates:** LIVE data and a quote at most 15 s old, else `skipped`.
  - **Snapshot:** `load_dataset` → `features.compute_timeline` for exactly that cut-off → `conditions.decide`. The entry trigger uses the persisted previous decision.
  - **Signal:** a fired setup becomes a signal; paper mode opens one SIMULATED position.
  - **Blocking:** a live spread above the strategy's `max_spread`, an entry later than the maximum entry delay, a size below the minimum lot, exposure limits, or a paper-simulator rejection blocks the entry. The reason is stored.
  - **Every cut-off** is recorded in `monitor_evaluations`.
- **Open trades:**
  - MFE and MAE are updated on every quote; the closing quote is included.
  - The time exit uses `time_exit`; the structure-flip exit uses `strategy_exit`.
  - Closes detected from the paper engine are finalized with exit context, a candle-based excursion cross-check and the evaluation.
- **Signal outcome** (`outcome@1`), from stored M1 candles **after** the cut-off only:
  - moves at +5, +15 and +60 minutes;
  - MFE and MAE;
  - which of stop and target was touched first (minute resolution, with an ambiguity flag);
  - a false-signal flag;
  - the **backtest replica**: `bt@1` on the same snapshot.
- **`goldscalp/live/journal.py`:** journal entries, CSV export, and live-paper `metrics@1` (ratios withheld below 30 trades).
- **`goldscalp/live/evaluation.py`** (`eval@1`): the seven answers (§6).
- **Paper engine:**
  - accounts are addressable by `account_id`;
  - `purpose = 'validation'` accounts never become the active account;
  - `on_quote` checks SL/TP on the manual account and on every validation account with open positions;
  - the close reasons add `time_exit` and `strategy_exit`.

### 3.3 Improvement engine and assistant (Parts 6-7)

- **`goldscalp/research/improvement.py`** (`improve@1`, `validate@1`). Windows are fixed when the study starts and checked for disjointness. Live-paper evidence is used only when it lies after the holdout.
  - Proposal catalogue:
    - remove a weak session;
    - exclude a volatility regime;
    - require H1 or M15 structure `not_against`;
    - add a spread filter at the median entry spread;
    - widen the stop.
  - Verdicts:
    - `insufficient_sample` when either version has fewer than the minimum holdout trades;
    - `improved` only with higher net **and** higher expectancy R, better in at least two of three holdout sub-windows, and drawdown at most 1.25 times the base;
    - `not_improved` otherwise.
  - Approval rules: `not_improved` cannot be approved; `inconclusive` needs an explicit acknowledgement; a proposal whose base version is no longer current is refused.
- **`goldscalp/assistant/`:**
  - `tools.py`: read-only; a safety test checks there is no write SQL.
  - `answer.py`: an intent router and templates.
  - `llm.py`: Claude via the official SDK, imported lazily. Model `claude-opus-5-5`, adaptive thinking, effort `medium`, server-side refusal fallback, strict tools with `tool_choice` auto, a manual loop of at most 8 rounds, and an unverified-number guard.
  - The `anthropic` package is **not installed**, so Claude mode is only tested with a fake client.

### 3.4 Storage and API

- **Migration `0006_live.sql`** rebuilds `paper_positions`. It runs under a new runner directive: foreign keys off, then `PRAGMA foreign_key_check` must come back empty before commit.
  - It was tested first on a copy of the real database: rows identical, foreign-key check empty.
  - A backup was taken before the first start: `var/backup-before-phase4.sqlite3`.
  - The real database is now at schema version 6.
- **Routes:**
  - `/api/live/*`: status, verifications, feed, integrity, eligibility, monitor, signals, trades, equity, performance, journal;
  - `/api/research/studies`;
  - `/api/proposals` (approve / reject);
  - `/api/assistant/*`.
- **Caching:** pages and app scripts are now served with `Cache-Control: no-cache`. Found in the browser check: an upgraded page ran a stale cached script.

## 4. Tests: mocked vs actual

### 4.1 Results

- **Suite:** `pytest`: **505 passed**. That is the 444 tests from Phases 1-3 plus 61 new ones: 36 in the Phase 4 files, and 25 safety and boundary tests, including the per-file scans of the new code.
- **Lint:** `ruff check goldscalp tests`: clean.
- **Changed test:** one Phase 3 test changed on purpose. `test_only_the_user_initiated_route_opens_simulated_positions` now lists the live monitor as a caller, and asserts that the monitor only ever opens positions with `account_id=account_id` (a validation account).

| File | Kind | Proves |
|------|------|--------|
| `tests/test_phase4_mt5.py` (13) | **Mocked terminal** (`tests/fake_mt5.py`) | Vantage identification, by branded company and by trade server for a generic MetaQuotes build, including the inconsistent-evidence WARN; demo detection in UTF-16 and UTF-8, with the planted login never surfacing; symbol discovery (`XAUUSD+` chosen, `XAUEUR` rejected); ticks; four timeframes; every check passing still gives only PARTIAL with a fake adapter; VERIFIED logic with the real adapter name; other broker, terminal not running, closed market; failed reconnect; planted integrity defects; feed transitions LIVE → DELAYED → DISCONNECTED → LIVE; the verification route and CSRF |
| `tests/test_phase4_live.py` (13) | **Mocked feed over recorded MT5 candles** | Eligibility; weekly close not logged as missed candles; signal fields; validation-account isolation; duplicate prevention; MFE/MAE including the closing quote; stop exit with evaluation and journal filters; time exit; pause; gates (DELAYED, stale quote, missed candle); live spread block; signal outcome and replica from later candles only; snapshot parity with the backtest at every tested cut-off; future candles change nothing; the 0006 migration preserves data and extends close reasons; selection walk-forward attribution; CSRF and pages |
| `tests/test_phase4_research.py` (5) | Unit, plus a **full study on recorded MT5 data** | Disjoint windows and live-evidence placement; finding → proposal catalogue; verdict rules; the adoption workflow (acknowledgement, new version, stale base, `not_improved` refused, reject); no automatic promotion; the research and assistant code cannot execute or adopt |
| `tests/test_phase4_assistant.py` (5) | Stored records; **fake Claude client** | All listed questions answered from data with sources; honest "no data"; audit log; Claude off by default; request shape, tool loop, number guard, refusal fallback, invalid tool input |
| `tests/test_safety.py` (+6 Phase 4 tests) | Static | Phase 4 packages never import providers, the hub, network or SDK modules; only `llm.py` loads `anthropic`, lazily; assistant tools are read-only; terminal facts contain no account fields |

**Actual terminal verification:** record `mv_214ba65715fb0f86` (§2), produced by the app against the running terminal, not by a test. No test writes to the real database; each one uses a temporary database.

### 4.2 Part 9 checklist

| Required test | Where |
|---------------|-------|
| Vantage terminal identification | `test_vantage_is_recognised…`, `test_fake_vantage_terminal…`, `test_other_broker…` |
| Symbol discovery | `test_fake_vantage_terminal…` (`XAUUSD+` among suffixed and non-USD symbols) and the existing `test_mt5.py` |
| Real-time tick retrieval | `live_tick` check (fresh and closed-market), plus `test_mt5.py` |
| Historical candles | `candles_M1..H1` and the consistency checks |
| Stale data | `test_gates…` (stale quote), closed-market tick, `stale_last_bar` integrity |
| Disconnect / reconnect | `reconnect` check, `test_failed_reconnect_is_reported`, feed transitions |
| Signal generation | `test_signal_simulated_trade…`, `test_gates…` |
| Live-paper evaluation | `test_signal_simulated_trade…`, `test_time_exit…` |
| Journal persistence | the journal filters and CSV in `test_signal_simulated_trade…` |
| MFE / MAE | 1.74 / 5.56 exact, including the closing quote |
| Per-trade evaluation | seven answers; permanent |
| Strategy versioning | `test_study_on_recorded_data_and_user_only_adoption` |
| Improvement proposals | `test_findings_map_to_catalogue_proposals…` |
| Historical validation | verdict rules and the full study |
| No-look-ahead protection | snapshot parity, future candles, outcome-only-after-cut-off |

### 4.3 Mutation checks (proving the tests can fail)

Each mutant was applied, its targeted tests were run, and the file was restored.

| Mutant | Result |
|--------|--------|
| `on_change` trigger ignored | caught |
| Live spread gate removed | caught |
| BUY excursion measured on the ask | caught |
| Time exit never fires | caught |
| Quote-age gate removed | caught |
| Validation trade opened on the manual account | caught |
| Outcome reads candles before the signal | caught |
| Vantage matched as a substring | caught |
| INI parser also returns the login line | caught |
| Fake adapter allowed to be VERIFIED | caught |
| Holdout ignores live evidence | caught |
| Verdict ignores sub-windows | caught |
| `not_improved` can be approved | caught |
| Number guard disabled | caught |
| Assistant invents an answer without data | caught |
| Snapshot loads candles one step past the cut-off | **equivalent**: `replay.frames_at` already drops every candle not closed by the cut-off, so loading extra data cannot leak. This is the as-of guard working. |
| Snapshot **evaluated** one step late (a genuine look-ahead) | caught (parity test) |

**Result: 16 of 16 non-equivalent mutants caught.**

## 5. Strategy evaluations on the stored MT5 history (actual data, no live trades)

### 5.1 Phase 3 validation of the existing strategies (`GET /api/live/eligibility`)

| Strategy version | Comparison run (full M5 history) | Walk-forward (selection; only the folds that selected it) |
|------------------|----------------------------------|----------------------------------------------------------|
| Example A v2 | 37 trades, +49.63 USD, expectancy −0.029 R, sufficient | 2 of 5 folds: 10 trades, −100.53 USD (insufficient) |
| Example B v2 | 2 trades, −22.72 USD (insufficient) | 0 of 5 folds |
| Example C v4 | 53 trades, +80.68 USD, expectancy +0.014 R, sufficient | 3 of 5 folds: 29 trades, +44.24 USD (insufficient) |

The per-fold attribution reconciles with the stored walk-forward total: −100.53 + 44.24 + 0 = −56.29.

### 5.2 Improvement study of Example C v4 (study `st_fbfd96f516100827`, run from the dashboard)

**Data.** Series `live:mt5:XAUUSD`, M5, 3,016 step candles. Stored coverage: 29 Oct 20:40 to 13 Nov 19:35 UTC.

**Windows:**
- discovery 29 Oct 20:40 → 7 Nov 20:01;
- embargo to 8 Nov 13:57;
- **holdout 8 Nov 13:57 → 13 Nov 19:35**.

No live-paper evidence exists yet.

**Discovery backtest:** 33 trades, net **+51.96 USD**, win rate 45.5 %, profit factor 1.205, expectancy +0.048 R, maximum drawdown 107.78 USD.

**Findings:**

| Finding | Evidence |
|---------|----------|
| Repeated false signals | 9 of 33 discovery trades were stopped out before reaching +0.5 R |
| Spread sensitivity (not flagged) | net +51.96 at recorded spreads, +48.18 at ×1.5, +44.40 at ×2 |
| Recurring invalidation pattern | 13 stop-outs: 8 within the first quarter of the holding time, 4 with the stop closer than 1 ATR |

No session, volatility regime or HTF combination reached the minimum group size of 8 trades with a negative expectancy.

**Proposal `sp_b29a787a2e52f3b4`.** Widen the structure stop buffer from 0.1 to 0.2 ATR. Holdout comparison against v4:

| Holdout (unseen) | Trades | Net | Max drawdown |
|------------------|--------|-----|--------------|
| Base v4 | 20 | +28.72 | 49.12 |
| Candidate | 19 | +54.09 | 33.24 |

- **Sub-windows:** the candidate was better in 2 of 3.
- **Verdict:** `insufficient_sample`, status **inconclusive**. At least 30 holdout trades per version are needed before ratios can support a decision.
- **Not adopted.** Example C is still v4, and the proposal awaits the user's decision.
- The stored limitations state the holdout size, the single discovery window and series, and the erosion of the holdout if the same history is studied again.

Screenshots: `docs/phase4/live-improvement.jpg` and `docs/phase4/live-monitor-eligibility.jpg`.

## 6. Journal and evaluation example

**Source:** the scenario of `tests/test_phase4_live.py`. It uses **recorded MT5 candles with a mocked quote feed**: a TEST record, not a live trade.

**Signal** `ls_71cd16fa8b11305ae128`:
- **Strategy:** "NY session long" v1, LONG, M5 cut-off 2026-11-13 19:30:00 UTC, generated 19:30:30.
- **Supporting:** "active: New York".
- **Conflicting:** "H1 structure is bearish (against the long)".
- **Invalidation:** "a bid at or below 3873.30 invalidates the setup (fixed 5.0)"; time exit after 30 minutes; no entry later than 10 minutes.
- **Price:** bid 3878.30 / ask 3878.51, spread 0.21 (live quote); candle close 3878.37.
- **Freshness:** quote 1.0 s old, LIVE.
- **Plan:** fill at the ask plus 0.05 slippage (3878.56), stop 3873.30, target 3888.56, 0.02 lots. **Status:** opened.

**Journal entry:**

| Field | Value |
|-------|-------|
| Outcome | loss, net −11.36 USD, `stop_loss` |
| Invalidated | yes: stop loss hit |
| MFE / MAE | 1.74 / 5.56 |
| Costs | commission 0.14, spread 0.42, slippage 0.20 |
| Went well | costs small relative to the move; conditions held while open |
| Went wrong | higher timeframe against the trade (H1); stopped out after 0.52 minutes |

**Evaluation** (`eval@1`, summary "LOSS −11.36 USD (−1.08 R) via stop_loss after 0.52 min - SIMULATED"):

1. *Why generated:* all enabled conditions held at the M5 close (the backtest's as-of snapshot).
2. *Conditions satisfied:* **yes**. Entry 30 s after the cut-off on a 1-second-old LIVE quote; fill 3878.56 as estimated.
3. *HTF agreed:* **no**. H1 bearish (against), M15 bullish; Phase 2 signal NEUTRAL, score −2.
4. *Conditions changed:* **no**. Session, structure and volatility were unchanged.
5. *Spread or volatility:* **no**. Costs were 0.76 USD (7 % of gross); the spread equalled the 1-hour median; the stop was 1.86 ATR; regime contracting.
6. *Behaved as expected:* **yes**. A planned stop exit at −1.08 R; the validating run's expectancy was 0.05 R.
7. *Improvement:* "Require higher-timeframe agreement (structure_state H1 not_against, or htf_alignment). A hypothesis for the improvement engine to test on historical data: one trade is not evidence."

## 7. Security (Part 10)

- **MT5:**
  - the allowlist is unchanged and checked by a test;
  - `account_info`, orders, positions and history stay forbidden by `FORBIDDEN_CODE`, which now scans the new files too;
  - `initialize()` never receives a login, password or server (asserted for the reconnect as well).
- **Credentials:**
  - no password or login setting exists (settings safety test);
  - the environment detection extracts only the server name, and a planted login is asserted absent from every output.
- **Execution:**
  - simulation only, through `PaperTradingEngine`;
  - the monitor opens positions only in validation accounts, and only for versions the user opted in;
  - research and assistant code cannot open, close or adopt (tests).
- **External transmission:** Claude mode is off by default and needs `GOLDSCALP_ASSISTANT_LLM_ENABLED=true` plus the package. The UI states what is sent. All tests run with the network blocked.
- **Web:** CSRF on every new POST and PATCH (tested); the strict CSP is unchanged; the server still binds to 127.0.0.1; no `innerHTML` is used.
- **SpicyStock:** untouched (HEAD `96ef4732`, clean).

## 8. Known limitations

- **Fixed after this report (14 Nov 2026, 12:2x UTC): MT5 time-basis incident.** While the market was closed,
  automatic time-basis detection treated the stale Friday tick as fresh and re-downloaded candles at timestamps
  shifted by 12-14 hours. The detection now needs a new tick, and `.env` pins `rule:ny_close`. With the user's
  approval the database was backed up, the app restarted once (monitors idle) and only the `live:mt5:XAUUSD`
  candles were re-downloaded; they match the recorded fixture exactly. No signal or simulated trade had been
  produced from the affected data. Details: PHASE_5_IMPLEMENTATION_REPORT.md §2.

- **Not yet VERIFIED** (§2): the actual-terminal record is PARTIAL because the market is closed. No live-paper trade, live signal or live journal entry exists yet; the Performance, Positions and Journal tabs are empty on the real database.
- **Broker identification for a generic terminal** relies on the trade-server name and the terminal's data folders, not on `account_info()`. A terminal used with several brokers' servers can only be confirmed when the server of the last login is also the folder written most recently; otherwise the check WARNs.
- **Instrument schedule:** GoldScalp's shipped instrument schedule is hypothetical. Vantage's XAUUSD daily break starts at 21:58 UTC, so the integrity check reports two "missing" M1 bars per day. Holidays (for example US Labor Day) also show up as missing bars.
- **Demo detection** relies on the server name in the terminal's configuration file. That names the most recent login, and a server without "demo" or "live" in its name stays UNKNOWN. `account_info` is deliberately not used.
- **Live and backtest fills differ by design.** Live fills use the quote when the cut-off is processed (about 2-30 s after the close, with the latency recorded); the backtest fills at the next open. The replica measures the difference per trade.
- **Polled excursions.** MFE and MAE come from polled quotes (about 1 s) plus the closing quote. Prices between polls are not seen. The candle cross-check is an upper bound.
- **Signal outcomes** use M1 bid candles (ask = bid + candle spread) at minute resolution. Structure-flip exits are not replicated in the backtest replica.
- **Monitor restarts.** After a restart, cut-offs that closed while the app was down are not back-filled. The `on_change` state continues from the last stored decision.
- **Small samples.** Stored history is about 15 days of M5 and 2.3 days of M1, so most proposals will be *inconclusive* until more history is collected or imported. The study above is an example of that.
- **Claude mode** is untested against the real API: the `anthropic` package is not installed and no key is configured. Only the deterministic mode was run in the app.

## 9. Screenshots (`docs/phase4/`)

| File | Shows |
|------|-------|
| `live-overview-actual-terminal.jpg` | Overview with the actual terminal: MetaTrader 5, Vantage broker by trade server, DEMO `VantageMarkets-Demo`, `XAUUSD`, last price, and the stored record `mv_214ba65715fb0f86` with its checks |
| `live-overview-terminal-not-running.jpg` | Overview before the terminal was started: DISCONNECTED, "Not verified", no signals |
| `live-monitor-eligibility.jpg` | Example C v4's Phase 3 validation (comparison and selection walk-forward) |
| `live-improvement.jpg` | The study of Example C v4: windows, findings, and the inconclusive proposal with Approve (acknowledgement) and Reject |
| `live-assistant.jpg` | Assistant answers from stored data with sources (connection status; proposals) |

Screenshots of a VERIFIED record (fresh tick), live signals and live-paper positions will be added after the market opens.

## 10. Files

- **New:**
  - `goldscalp/live/` (`feed`, `monitor`, `evaluation`, `journal`)
  - `goldscalp/research/improvement.py`
  - `goldscalp/assistant/` (`tools`, `answer`, `llm`)
  - `goldscalp/data/integrity.py`, `goldscalp/data/mt5_verify.py`, `goldscalp/data/providers/mt5_identity.py`
  - `goldscalp/storage/live_repo.py`, `goldscalp/storage/improvement_repo.py`, `goldscalp/storage/migrations/0006_live.sql`
  - `goldscalp/api/routers/live.py`, `goldscalp/api/routers/improvement.py`
  - `web/live.html`, `web/js/live.js`
  - `requirements-ai.txt`
  - `tests/test_phase4_*.py` (4 files)
  - `docs/phase4/*.jpg`
  - `PHASE4_ARCHITECTURE.md`, this report
- **Changed:**
  - `mt5_provider.py`: terminal facts and verification facts
  - `portfolio.py` and `paper_repo.py`: accounts by id, validation purpose
  - `positions.py`: close reasons
  - `engine.py`: the `spread_multiplier` assumption, omitted when 1
  - `db.py`: the foreign-keys-off migration directive
  - `config.py` and `.env.example`: six live/assistant settings plus `mt5_detect_environment`
  - `services.py`, `app.py`: wiring, the `/live` page, cache headers
  - `app.css`, and the navigation on every page
  - `tests/fake_mt5.py`, `tests/test_safety.py`, `tests/test_phase3_api.py`
  - README, PROPOSED_ARCHITECTURE (revision 6), IMPLEMENTATION_ROADMAP

## 11. To close the acceptance

1. ~~Start the Vantage MT5 terminal and run the verification.~~ Done: `mv_214ba65715fb0f86`, PARTIAL (market closed).
2. After Sunday 23:00 UTC, with the terminal running, click **Run terminal verification** again; VERIFIED needs a fresh tick.
3. ~~Start the versions to validate.~~ Done: Examples A v2, B v2 and C v4 in paper mode (§2.1).
4. After signals and simulated trades accumulate, update §2, §6 and §9 of this report from the stored records only.
