# GoldScalp AI Phase 4 Architecture: Research Assistant, Vantage MT5 Demo Integration and Live Strategy Evaluation

Status: design, written before implementation (14 November 2026). Section 14 records what was actually
built. This document extends [PHASE3_ARCHITECTURE.md](PHASE3_ARCHITECTURE.md); nothing in Phases 1-3 is
removed.

> **Guiding rule (from the brief).** Phase 4 is built around a *research assistant*, not an AI that
> changes strategies on recent results:
> - nothing alters a strategy without the user's explicit approval;
> - every proposed change is versioned and validated on unseen historical data before it can be adopted;
> - every answer is computed from stored GoldScalp records.

---

## 1. Audit (state at the start of Phase 4)

| Area | Finding | Consequence for Phase 4 |
|------|---------|-------------------------|
| Tests | Full suite green (444 at the end of Phase 3). | Regression baseline. |
| MT5 terminal | `terminal64.exe` not running; the market is in its weekly close until Sun 15 Nov 23:00 UTC. | Actual Vantage verification needs the user's terminal. Live ticks need the market open. Code is built and tested with a fake terminal first, and the report keeps the two apart (§12). |
| MT5 provider | Read-only allowlist proxy (`ALLOWED`). `terminal_info()` already gives `company`, `connected`, `build` and `trade_allowed`. `account_info` is forbidden by the safety tests. | The broker is identified from `terminal_info().company`. The demo environment cannot come from `account_info`, so it is read from the terminal's own configuration file (server name only, §3.2). |
| Paper engine | One *active* account. `close_reason` CHECK allows only `manual / stop_loss / take_profit`. | Accounts become addressable by id (validation accounts). Close reasons are extended with `time_exit` and `strategy_exit` by a table rebuild (§5). |
| Phase 3 live monitor | `live_setup()` evaluates at "now", using the dashboard loaders. | The Phase 4 monitor instead uses the **backtest's own as-of pass** (`features.compute_timeline`) at each step-candle close, so a live signal is exactly the decision the backtest would make at that cut-off (§4). |
| AI | No LLM dependency. The official `anthropic` SDK is not installed. | The assistant is deterministic and grounded by default. Claude (Messages API, read-only tools) is an optional, explicitly enabled mode with a lazy import (§9). |

## 2. Principles

1. **MT5 stays read-only.** The allowlist is unchanged. There is no `account_info`, no orders, no positions and no history, and GoldScalp never sees a login or password. Disconnect and reconnect only detach and re-attach the Python IPC link; the terminal is never started, stopped or logged in.
2. **The paper simulator is the only execution environment.** Live validation opens SIMULATED positions in dedicated paper accounts through `PaperTradingEngine` (same formulas, same cost model). Every record carries the label `SIMULATED`.
3. **Automatic simulation, never automatic adoption.**
   - When the user opts a strategy *version* into paper validation, every qualifying signal is simulated, with no cherry-picking.
   - Strategy configurations change only through the approve endpoint (a user action).
   - The monitor never switches versions by itself.
4. **Grounded output.** Evaluations, findings, proposals and assistant answers are computed from stored rows and cite their ids. The assistant says "no stored data" instead of guessing. The optional Claude mode may only read through the same tools, and its answers are checked for numbers that do not appear in the tool results.
5. **Evidence honesty.**
   - A verification record is written only by a run against a terminal, and it stores the adapter that produced it (`MetaTrader5` for the real package).
   - Tests use a fake module and a temporary database, so they can never produce a record in the user's database.
   - "Verified" is shown only for a record from the real adapter whose checks passed.
6. **No look-ahead.** Live decisions use only candles closed by the cut-off. Signal outcomes and replicas use only candles after the cut-off. Improvement proposals are validated on a window disjoint from all the evidence that produced them.

> **Later addition (14 November 2026, after Phase 4).** Principles 1 and 2 describe Phase 4 as designed, and they
> still hold for the market-data provider and the Phase 4 packages. An optional **MT5 demo-account execution**
> feature was added afterwards (`goldscalp/execution/`, migration `0007`, a *Demo Execution* tab on `/live`). It is
> off by default (`GOLDSCALP_DEMO_EXECUTION_ENABLED`). When switched on and linked, it mirrors the SIMULATED
> validation trades of linked versions as market orders on an account that reports itself as DEMO, re-checked
> before every order. There is no real-money order path. See README, "MT5 demo-account execution".

## 3. Part 1: Vantage MT5 demo integration

### 3.1 Terminal identity (`data/providers/mt5_identity.py`, pure functions)

- **Captured from `terminal_info()`:** `company`, `name`, `path`, `data_path`, `connected`, `build`, `trade_allowed` and `ping_last`. No account fields are captured.
- **`broker_identity(company)`.** `is_vantage` is true when the company matches `\bvantage\b` (case-insensitive). The evidence is the company string exactly as the terminal reports it.

### 3.2 Demo environment

`environment(data_path)` reads `<data_path>/config/common.ini`, which is UTF-16 or UTF-8 depending on the build.

- **What is read.** The parser returns only the `Server=` value. Other keys, including `Login=`, are never returned, stored or logged. A test plants a login and asserts that it appears in no output.
- **Classification:**
  - `DEMO` when the server name contains "demo";
  - `LIVE` when it contains "live" or "real", in which case a warning is shown and GoldScalp still only reads market data;
  - otherwise `UNKNOWN`.
- **Evidence** records the server name and the file it came from. The file holds the server of the terminal's most recent login, so the evidence is labelled that way.
- **Opt-out.** `GOLDSCALP_MT5_DETECT_ENVIRONMENT=false` disables the read; the environment is then `UNKNOWN`.

### 3.3 Verification checklist (`data/mt5_verify.py`, `mt5v@1`)

The checklist runs on the MT5 thread through the provider. It uses the active MT5 provider when MT5 is active; otherwise a temporary provider is created and shut down afterwards.

| # | Check | Pass rule |
|---|-------|-----------|
| 1 | terminal | process found and `initialize()` succeeds (no credentials); version and build recorded |
| 2 | broker_connection | `terminal_info().connected` |
| 3 | broker_is_vantage | company matches Vantage |
| 4 | environment | `DEMO` passes; `UNKNOWN` warns; `LIVE` warns |
| 5 | symbol | gold symbol discovered by `rank_gold_symbols` (or the configured one); candidates and the contract specification recorded (nothing hardcoded) |
| 6 | live_tick | bid/ask valid, `ask >= bid`, tick age ≤ 60 s, time basis established. With the market closed: warn "stale". |
| 7 | candles_{M1,M5,M15,H1} | ≥ 100 bars; integrity report clean (§3.4) |
| 8 | timeframe_consistency | M5/M15/H1 rebuilt from M1 match the native bars (OHLC within one point) on fully covered windows |
| 9 | reconnect | `shutdown()` then re-`initialize()`, and a new tick is read; the elapsed time is recorded |

The overall result is one of:
- `VERIFIED`: every check passes, including Vantage, DEMO and a fresh tick;
- `PARTIAL`: no failures, but some warnings (for example, market closed);
- `FAILED`: any check fails.

Records are stored in `mt5_verifications` with the adapter name (`MetaTrader5` or `tests.fake_mt5`) and the start and finish times.

### 3.4 Candle integrity (`data/integrity.py`)

The same validator is used by verification and by `/api/live/integrity`, which runs it on stored candles. It checks:
- OHLC validity: positive prices, low ≤ open/close ≤ high;
- strictly increasing, duplicate-free timestamps;
- alignment to the timeframe;
- no closed bar ending in the future, and at most one forming bar;
- gaps measured against the trading schedule (reusing `gaps.gap_report`);
- freshness of the last closed bar during market hours;
- zero or negative spreads.

### 3.5 Feed health (`live/feed.py`)

A hub listener keeps a rolling window of quotes and records:
- spread statistics (median, p95 and maximum over 1 hour);
- tick age;
- state transitions (LIVE / STALE / DISCONNECTED / CLOSED) as `feed_events`;
- one `feed_samples` row per minute: state, bid, ask, spread, tick age and source.

The samples feed the dashboard, per-trade spread context and the spread-sensitivity research. Disconnects and reconnects already exist (hub and service). Verification check 9 exercises them against the terminal.

## 4. Part 2: Live strategy monitor (`live/monitor.py`, `live@1`)

### 4.1 Monitored versions

`monitored_strategies` holds one row per strategy *version*: `mode` (`observe` or `paper`), `status` (`active`, `paused` or `stopped`), the validation paper account, and its eligibility.

- **Eligibility ("validated through Phase 3").** The version's fingerprint must appear in at least one completed research run (backtest, comparison or walk-forward). The runs are listed with their trade counts and a sample-sufficiency flag; an insufficient sample is shown, not hidden.
- **No monitoring without such a run.** Monitoring is refused when no research run exists for the version.

### 4.2 Loop

The loop is an asyncio task, run every 2 s. For each active monitored version and its step timeframe:
1. The due cut-off is `floor(now, step)`. It is evaluated once, when the M1 candle ending at the cut-off is stored and closed. After a 120 s grace period it is recorded as `missed` with the reason.
2. **Gate.** The hub state must be LIVE and the quote age must not exceed `live_max_quote_age_seconds` (default 15). Otherwise the cut-off is recorded as `skipped` with the reason, and no signal is generated.
3. **Snapshot = the backtest's.** `load_dataset(series, step, cutoff - step, cutoff, lookback_days)` is passed to `features.compute_timeline(ds, step, [cutoff])` and then to `conditions.decide`. The entry trigger (`on_change` / `every_step`) uses the persisted previous decision, exactly like the engine's `last_pass`.
4. **Signal.** A setup that is not ambiguous creates a `strategy_signals` row with:
   - **id:** `ls_ + sha256(fingerprint|cutoff|side)`, unique, so a restart never duplicates it;
   - **strategy:** name, version and fingerprint;
   - **direction and timing:** direction; the step timeframe and the condition timeframes; the cut-off; `generated_at` and the latency;
   - **supporting evidence:** the passed conditions;
   - **conflicting evidence:**
     - conditions that pass for the opposite side;
     - higher-timeframe structure against the trade;
     - an opposite Phase 2 signal;
     - an extreme volatility regime;
     - a live spread above the 1-hour p95;
   - **invalidation:**
     - the stop level and its source;
     - the time limit;
     - a structure flip, if configured;
     - the maximum entry delay;
   - **price at generation:** bid, ask, mid, spread and its source, quote time and age;
   - **data source:** provider, broker company, series and symbol;
   - **freshness:** quote age, the last closed candle per timeframe, and the market state;
   - **context:** sessions, the volatility regime and ATR per timeframe, structure states, and the Phase 2 signal;
   - **plan:** side, stop, target and lots.
5. **Live spread gate.** Conditions use the candle spread, as in the backtest, which keeps parity. If the strategy has a spread condition and the *live* quote spread exceeds its maximum, the signal is stored as `blocked` with that reason.
6. In `paper` mode a non-blocked signal opens a SIMULATED position (§5). Its status becomes `opened`, or `blocked` with the paper engine's rejection reason (exposure, minimum lot, margin, quote).

### 4.3 Signal outcome (comparison with subsequent real prices)

Once the horizon has passed (the maximum holding time, or 60 minutes when unlimited) and the M1 candles exist, each signal gets:
- price change at +5, +15 and +60 minutes;
- MFE and MAE from the generation price;
- whether the stop or the target was touched first on M1, with an ambiguity flag;
- a **backtest replica**: `engine.run(config, [snapshot], later candles)`, the trade the backtest would have modelled for the same decision.

The replica is what makes per-trade backtest-vs-live comparison possible. Its limitation: a structure-flip exit is not replicated, because it needs later snapshots; this is flagged.

## 5. Part 3: Live paper validation

- **Accounts.** `paper_accounts.purpose` is `manual` (the existing account) or `validation`.
  - `create_account(..., purpose="validation")` does not deactivate the manual account.
  - Each monitored version in paper mode gets its own account. It uses the paper settings' balance, leverage and costs; LIVE quotes only; and a quote age of at most `live_max_quote_age_seconds`. Equity curves are therefore isolated and comparable.
- **Engine.** `PaperTradingEngine` methods take an optional `account_id`; the default remains the active manual account, so existing behaviour is unchanged. `on_quote()` checks SL/TP for the manual account and for every validation account with open positions.
- **Close reasons.** `CloseReason` gains `TIME_EXIT` and `STRATEGY_EXIT`.
  - Migration `0006` rebuilds `paper_positions` with the extended CHECK. The migration runner learns a `-- goldscalp: foreign_keys=off` directive, which runs the migration with foreign keys off and requires an empty `PRAGMA foreign_key_check` before commit (the documented SQLite 12-step procedure).
- **Time exit.** `now >= opened_at + max_holding` → `close_position(..., TIME_EXIT)`. This needs a LIVE quote; a delay (for example, the market closed) is recorded.
- **Structure-flip exit.** At each step cut-off, if the flip timeframe's state is against the position, the position closes with `STRATEGY_EXIT`. This mirrors the engine's `exit_next_open`.
- **`live_trades` row.** Each trade records:
  - links: signal, position and account;
  - the plan;
  - entry facts: fill, bid/ask/spread, slippage, commission, quote age, and latency from the cut-off;
  - entry and exit context;
  - exit facts;
  - holding minutes;
  - **MFE/MAE tracked from every quote** (BUY: bid against the fill; SELL: ask against the fill, matching the engine's definition), in price units, R and ATR;
  - a candle-based cross-check at close;
  - the checks at each later cut-off while open (conditions still passing or reversed).
- **Unrealized P/L** comes from the paper engine's open-position view.

## 6. Part 4: Journal

`GET /api/live/journal` returns one entry per signal, joined with its trade, position and evaluation.

- **Filters:** `start`, `end`, `strategy_id`, `version`, `session`, `timeframe`, `direction`, `outcome`.
- **Outcome values:**
  - `win`, `loss`, `breakeven`, `open`;
  - `invalidated`: stopped out or strategy exit;
  - `not_traded`: observe mode or blocked.
- **Fields:**
  - timestamp; strategy and version; sessions; timeframe; direction;
  - entry assumptions: plan and actual fill;
  - exit assumptions: stop, target, time and flip;
  - outcome and net P/L; MFE/MAE;
  - spread and costs: spread at signal and at fill, commission, slippage and total;
  - whether invalidated, and how;
  - what went well and what went wrong (from the evaluation);
  - market conditions;
  - user notes, the only editable field.
- **Export:** CSV at `/api/live/journal/export.csv`.

## 7. Part 5: Per-trade evaluation (`live/evaluation.py`, `eval@1`)

When a validation trade closes, the monitor generates an evaluation and inserts it into `trade_evaluations`. There is one row per (trade, definition), with no update or delete route: the evaluations are permanent. Each of the seven answers has a verdict (yes / no / partly / unknown), a sentence, and evidence facts with values and record ids.

1. **Why the signal was generated:**
   - the supporting conditions and their evidence;
   - the strategy description.
2. **Were the original conditions satisfied:**
   - all conditions passed at the cut-off;
   - entry latency and quote freshness;
   - fill versus plan;
   - re-checks at later cut-offs while open (still passing, no setup, or reversed).
3. **Did the higher timeframes agree:**
   - H1 and M15 structure states (and M5 when it is not the step) relative to the side, at entry;
   - the Phase 2 signal direction.
4. **Did market conditions change:** entry versus exit for sessions, volatility regime, HTF structure, ATR change and spread change.
5. **Did spread or volatility affect the outcome:**
   - spread and commission against |gross|, and whether costs flipped the sign;
   - spread at fill against the 1-hour median and p95;
   - stop distance in ATR, where < 1 ATR means "inside normal noise";
   - an extreme volatility regime.
6. **Did the strategy behave as expected:**
   - the exit reason against the planned exits;
   - R against the validating backtest's average win and average loss R;
   - the backtest replica's outcome against the live result;
   - slippage against the assumption;
   - holding time against the plan.
7. **What could have been improved:** rule-based hypotheses tied to tags (`htf_against`, `spread_wide`, `stop_inside_noise`, `regime_change`, `late_fill`, `costs_flipped_sign`, …). They are explicitly labelled "a hypothesis for the improvement engine: one trade is not evidence".

The tags, "what went well" and "what went wrong" feed the journal and the improvement engine.

## 8. Part 6: Strategy improvement engine (`research/improvement.py`, `improve@1`)

A **study** is started by the user for one strategy version and runs as a background job (the shared JobRunner).

### 8.1 Windows (fixed when the study starts, stored, checked for disjointness)

`[discovery | embargo | holdout]` over stored history, with a default 60 / 5 / 35 % split of the available step-candle range.

- **Live-paper evidence** (signals and trades) is used only if every item lies outside the holdout.
- **The holdout ends before the first live item used.** If that is impossible, the live evidence is excluded and the study says so.

### 8.2 Diagnostics (findings)

Diagnostics come from the discovery backtest's trades, annotated per trade with session, volatility regime, HTF states relative to the side and entry spread, plus the live-paper trades and signal outcomes:

| Finding | Rule (each reports n, the metric and the evidence ids) |
|---------|-------------------------------------------------------|
| repeated false signals | trades or signals stopped out before reaching +0.5 R; rate overall and by group |
| weak session | group n ≥ `min_group` (8) and expectancy < 0, while the rest of the strategy is better |
| poor volatility regime | same rule, per vol@2 regime |
| unreliable timeframe combination | same rule, per (H1, M15) structure relation to the side (with / against / ranging) |
| spread sensitivity | discovery backtest repeated with spreads × 1.5 and × 2 (new `spread_multiplier` assumption, default 1, omitted from the mapping when 1, so Phase 3 hashes are unchanged); flagged when net falls by > 50 % or changes sign |
| recurring invalidation | exit-reason mix; stop-outs within the first quarter of the holding time; stops < 1 ATR |
| backtest vs live-paper | live metrics against the validating run's metrics (Wilson intervals); replica differences in entry slippage, spread at fill, latency and exit |

### 8.3 Proposals

Each finding maps to one change in a fixed catalogue built from existing `strategy@1` condition types. No new logic is invented.

| Finding | Proposed change |
|---------|-----------------|
| weak session | `session` condition without that session |
| poor volatility regime | `volatility_regime` excluding it |
| HTF combination | `structure_state` H1 (or M15) `not_against` |
| spread sensitivity | `spread` filter with `max_spread` = median entry spread |
| stop inside noise | ATR stop multiple + 0.5, or structure buffer + 0.1 ATR |

Each proposal records: the base version and fingerprint, the full candidate configuration, a readable diff, the explanation, the evidence (finding metrics, window and ids) and its limitations.

### 8.4 Validation on unseen data

Base and candidate are backtested on the **holdout** with identical assumptions and one shared timeline (`lab.run_strategies`). The holdout is also split into three sub-windows. The verdict (`validate@1`) is:

- `insufficient_sample`: either version has fewer than `min_trades_for_ratios` holdout trades. Shown as **inconclusive**.
- `improved`: higher net **and** higher expectancy R, better in ≥ 2 of 3 sub-windows, and max drawdown no worse than 25 %.
- `not_improved`: otherwise.

### 8.5 Decision (user only)

- `approve`:
  - allowed for `improved`;
  - allowed for `inconclusive` only with `acknowledge_insufficient_sample: true`;
  - refused for `not_improved`.
- **Approving** saves the candidate as a **new version** through `strategy_repo.save`, with a note citing the proposal, holdout and result. It does **not** start monitoring the new version; the user adds it.
- `reject` records a reason.

Nothing else writes `strategy_versions`.

## 9. Part 7: AI research assistant (`assistant/`)

### 9.1 Tools (`tools.py`, read-only)

Each tool returns data plus `sources` (table:id) and `as_of`:
- `connection_status`;
- `latest_signal(strategy?)`;
- `signal(id)`;
- `trade(id)`, including its evaluation;
- `strategy_performance(period)`: live-paper metrics per version (`metrics.compute`, ratios withheld below the minimum sample);
- `false_signals_by_session(period)`;
- `backtest_vs_live(strategy, version)`;
- `proposals(status)`;
- `evaluated_versions()`;
- `journal(filters)`.

### 9.2 Deterministic mode (default)

An intent router covers the seven questions in the brief and close variants. It calls the tools and fills templates from their output only. With no data, it says so and names what is missing (for example, "no closed live-paper trade is stored").

### 9.3 Claude mode (optional, off by default)

- **Enabling it.** The mode needs `GOLDSCALP_ASSISTANT_LLM_ENABLED=true`, the `anthropic` package (`requirements-ai.txt`, imported lazily) and a key (`GOLDSCALP_ASSISTANT_API_KEY`, or the SDK's own credential resolution). When it is on, the UI states that excerpts of stored GoldScalp data are sent to Anthropic. It is off by default to honour "no broker data to external services without explicit configuration and permission".
- **The request.** Model `claude-opus-5-5`, adaptive thinking, effort `medium`, server-side refusal fallback (`fallbacks: "default"`). The same tools are exposed as `strict` JSON-schema tools with `tool_choice: auto`. The tool loop is manual, with at most 8 rounds and full content appended each round. Stop reasons, including `refusal`, are handled.
- **Grounding guard.** Numbers in the answer are compared with the tool outputs, and any that are not found are listed as "unverified".

### 9.4 Audit log

Every question and answer is stored in `assistant_log` with the mode, the tools called and the sources.

## 10. Part 8: Live dashboard (`/live`)

The page has these tabs:

- **Overview:**
  - connection: provider; broker company with a Vantage badge; environment; terminal build; symbol; state; last verification, or "not verified against the actual terminal"; a Run verification button;
  - price: bid, ask, spread and mid, tick age, current session, spread statistics;
  - latest signals.
- **Monitor:** eligible strategies; monitored versions (mode, status, account equity, open position); add, pause and stop controls.
- **Positions:** active simulated positions with unrealized P/L; closed positions.
- **Demo Execution** (added after Phase 4; optional, off by default): the MT5 demo account's facts, per-version links to the demo account, mirrored demo orders, and the *Stop all demo execution* kill switch (see the note in §2).
- **Performance:**
  - live-paper equity curves, one line per version;
  - a strategy comparison with sample sizes and withheld ratios;
  - historical against live-paper results, including replica differences.
- **Journal:** filters, entries, the evaluation view and CSV export.
- **Improvement:** run a study; findings; proposals with validation; approve or reject; version history.
- **Assistant:** a question box, the supported questions, and answers with their sources.

Every simulated item is labelled SIMULATED.

## 11. Storage: migration `0006_live.sql`

- **Rebuilt:** `paper_positions`, with the CHECK extended by `time_exit` and `strategy_exit` and data copied verbatim.
- **Changed:** `paper_accounts` gains `purpose`.
- **New tables:**
  - `mt5_verifications`, `feed_samples`, `feed_events`;
  - `monitored_strategies`, `strategy_signals`, `live_trades`, `trade_evaluations`;
  - `research_studies`, `strategy_proposals`;
  - `assistant_log`.
- **What is never stored:** broker logins, passwords, or account data.

## 12. Part 9: Tests, mocked against actual

| File | Scope |
|------|-------|
| `tests/test_phase4_mt5.py` (mocked terminal) | Vantage identification; demo / live / unknown environments, with the login never surfacing; symbol discovery including suffixed names; ticks; all four timeframes; integrity checks catching planted defects; stale data; disconnect and reconnect; verification verdicts; adapter labelling |
| `tests/test_phase4_live.py` | signal generation at cut-offs; parity with the backtest decision; no look-ahead (appending future candles changes no stored signal); dedupe; live spread gate; validation-account isolation; SL/TP/time/flip exits; MFE/MAE; journal persistence and filters; evaluation answers and immutability |
| `tests/test_phase4_research.py` | findings on recorded MT5 data; proposal catalogue; holdout disjointness; validation verdicts (improved / not / insufficient); approval creates a version; reject; no automatic promotion |
| `tests/test_phase4_assistant.py` | each question answered from stored rows; the empty database says "no data"; Claude mode with a fake client (tool loop, refusal, unverified-number guard); disabled by default |
| `tests/test_safety.py` | extended: the assistant and live packages never import providers or broker SDKs; the forbidden list is unchanged |

**Actual terminal verification** is not a test. It is the `POST /api/live/verifications` run against the user's terminal, stored with adapter `MetaTrader5`. The report quotes that stored record, separately from the automated test results.

## 13. Security (Part 10)

- The MT5 allowlist is unchanged; `account_info` and trading functions stay forbidden by the tests.
- No password or login field exists; the environment detection reads the server name only.
- Execution is simulation only, through `PaperTradingEngine`.
- CSRF headers on every state change; binding to 127.0.0.1; CSP unchanged (the dashboard uses no external hosts).
- The Claude mode is off by default, needs explicit settings, discloses its transmission, and its tools are read-only (no tool can change data).

## 14. As built

Built as designed, with these specifics.

| Design item | As built |
|-------------|----------|
| §3.1-3.2 identity | `data/providers/mt5_identity.py`. **Changed after testing on the actual terminal:** the generic MetaTrader 5 build reports `company = "MetaQuotes Ltd."` (its distributor). `identify_broker` therefore also accepts the trade server: the `Server=` value must name Vantage (`^Vantage`) and be the trade-server data folder under `bases/` the terminal writes (the only one, or the most recently written; metadata only), while connected. The basis is stated in the record. `terminal_info` facts gain `name`, `path`, `data_path` and `ping_last`, and no account fields (safety test). The INI parser returns only the `[Common] Server=` value; implausible values are dropped. |
| §3.3 checklist | `data/mt5_verify.py`: 14 checks, including three cross-timeframe consistency checks, plus an `adapter` flag on any module other than `MetaTrader5`. Only `MetaTrader5` can be VERIFIED. Home-folder paths are shown as `%USERPROFILE%`. |
| §3.4 integrity | `data/integrity.py`, shared with `GET /api/live/integrity` (stored candles). A cross-timeframe mismatch above 2 % of compared bars is FAIL; at or below, WARN. |
| §3.5 feed | `live/feed.py`, as a hub listener and on every monitor cycle. It stores one sample per minute and every state transition. |
| §4 monitor | `live/monitor.py`, an asyncio task every 2 s. One `monitor_evaluations` row per cut-off: `no_setup`, `setup`, `repeat`, `ambiguous`, `gated`, `skipped` or `missed`. A version is never evaluated for a candle that closed before monitoring began. Additional block reasons are the maximum entry delay and the minimum lot. |
| §4.3 outcome | `outcome@1`: moves at +5/+15/+60 minutes, MFE/MAE, first touch, and a false-signal flag (stop first, or less than 0.5 R of favourable movement), plus the `bt@1` replica. |
| §5 validation | Exposure limits use the strategy's `max_open_positions` and `max_exposure_lots`. Paper SL/TP levels are quantized to the instrument's price decimals. The spread at close is recorded when the closing quote is still the latest. |
| §6 journal | `live/journal.py`; `GET /api/live/journal`, `/export.csv`; `PATCH /api/live/signals/{id}/notes`. |
| §7 evaluation | `live/evaluation.py`, inserted with `INSERT OR IGNORE`. No update or delete function exists (tested). |
| §8 improvement | `research/improvement.py` and `api/routers/improvement.py`. Studies run on the shared JobRunner. `decide_verdict` is a pure function. The engine assumption `spread_multiplier` (default 1, omitted from the mapping) supports the stress test. Approval refuses a stale base version. |
| §9 assistant | `assistant/tools.py` (9 read-only tools), `answer.py` (8 intents) and `llm.py` (Claude mode with strict tools, a manual loop and the number guard). The `anthropic` package is imported lazily, only in `llm.py` (safety test). |
| §10 dashboard | `web/live.html` and `web/js/live.js`, with 7 tabs at the end of Phase 4; "Live Validation" is linked from every page. An eighth tab, **Demo Execution**, was added afterwards by the optional MT5 demo-account execution feature, so `/live` now has 8 tabs. |
| §11 storage | `0006_live.sql`, verified on a copy of the real database first: rows identical, foreign-key check empty. A backup was taken before the first start (`var/backup-before-phase4.sqlite3`). |
