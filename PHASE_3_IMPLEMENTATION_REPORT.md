# GoldScalp AI: Phase 3 Implementation Report (Strategy Intelligence and Advanced Backtesting)

Date: 14 November 2026.
Design: [PHASE3_ARCHITECTURE.md](PHASE3_ARCHITECTURE.md), sections 1–13, with the as-built changes in
section 14.

Phase 3 is historical research and simulated paper trading only:
- there is no broker order execution and no automatic trading;
- there is no access to real MT5 orders, positions or account data;
- MT5 stays a read-only market-data source.

Backtests compute **hypothetical** outcomes with the paper simulator's own fill, cost and margin
formulas; they never create paper positions. The only path to a simulated paper position from a
strategy is a **user-confirmed** request.

---

## 1. Pre-implementation audit (Part 1)

| Item | Result |
|------|--------|
| **Baseline** | 372 tests passed; ruff clean |
| **Phase 1 report** | There is no standalone file. Phase 1 is documented in IMPLEMENTATION_ROADMAP.md, PROPOSED_ARCHITECTURE.md (revisions 2–3) and MARKET_DATA_PROVIDERS.md. |
| **Phase 2** | Complete and tested. Still open from Phase 2: interactive chart zoom has not been checked by hand, and live MT5 has not been re-checked since Parts 11–18. The market was in its weekly close, and the terminal was not running throughout this phase. |
| **Defect found and fixed** | The dataset loader took M1 as the base whenever any M1 candle fell in the window, so ranges starting before M1 coverage silently started late. It is replaced by `services.choose_base`, which uses the finest timeframe whose coverage reaches the window start and reports its choice (tested). |
| **Stored data** | MT5: M1 2.3 days, M5 15 days, M15 30 days, H1 4 months. Results on this history are small samples; every report states its coverage. |

## 2. New modules

| Module | Definition | Purpose |
|--------|-----------|---------|
| `goldscalp/strategies/builder.py` | `strategy@1` | Strict schema, defaults, 12 condition types, logic fingerprint, templates A/B/C (examples, not claims), catalogue |
| `goldscalp/strategies/conditions.py` | – | Direction-relative condition evaluation with evidence; data-quality gate; ambiguous-direction handling |
| `goldscalp/backtesting/features.py` | `features@1` | As-of feature snapshot per step cut-off; one shared, cached timeline per run |
| `goldscalp/backtesting/engine.py` | `bt@1` | Deterministic, event-driven backtest on base candles; costs, margin, exposure, sizing, intrabar rule |
| `goldscalp/backtesting/metrics.py` | `metrics@1` | Trade statistics, drawdowns, breakdowns, sample-size gates |
| `goldscalp/backtesting/walk_forward.py` | `wf@1` | Development / embargo / evaluation folds; fixed or selection mode; aggregation and variation |
| `goldscalp/backtesting/lab.py` | – | Runs backtests and comparisons under identical assumptions on one timeline |
| `goldscalp/reports/pdf.py`, `exports.py` | – | Dependency-free PDF writer; CSV / JSON / PDF exports |
| `goldscalp/storage/strategy_repo.py`, `research_repo.py`, `journal_repo.py` | – | Strategy versions, research runs, strategy journal |
| `goldscalp/api/jobs.py` | – | Shared background job runner (one heavy job at a time) |
| `goldscalp/api/routers/strategies.py`, `research.py` | – | Strategy Builder, live monitor, journal, research runs and exports |
| `web/lab.html`, `web/js/lab.js` | – | Strategy Lab UI |

## 3. Strategy configuration format (Part 2)

`strategy@1` is JSON:

```json
{"schema": "strategy@1", "name": "...", "description": "...", "direction": "long|short|both",
 "evaluation": {"step_timeframe": "M1|M5|M15|H1", "window": {"mode": "all|last_days|range", "days": 10, "start": null, "end": null}},
 "conditions": [{"id": "c1", "type": "...", "enabled": true, "...": "type parameters"}],
 "entry": {"trigger": "on_change|every_step", "max_entry_delay_minutes": 10},
 "exit": {"stop": {"type": "structure|atr|fixed", "timeframe": "M5", "buffer_atr": 0.1, "atr_multiple": 1.5, "distance": null},
          "target": {"type": "r_multiple|atr|fixed|none", "r": 2.0, "atr_multiple": null, "distance": null, "timeframe": "M5"},
          "max_holding_minutes": 60, "exit_on_structure_flip": {"enabled": false, "timeframe": "M5"}},
 "risk": {"sizing": "fixed_lots|risk_percent", "lots": "0.02", "risk_percent": 0.5, "max_open_positions": 1, "max_exposure_lots": "1.00"}}
```

**Condition types** ("with" means bullish for a long and bearish for a short):

| Type | Passes when (for a long; shorts mirror) |
|------|------------------------------------------|
| `structure_state` | The timeframe's structure@2 state is with / against / ranging / not against |
| `structure_event` | A BOS / CHoCH / any break, in or against the direction, became known within N candles |
| `swing_sequence` | trend: last high HH and last low HL. pullback: last low HL. |
| `liquidity_sweep` | A confirmed potential sweep in the direction (EQ / PD / PW / session levels) became known within N candles |
| `fvg` | Price is inside an open same-direction FVG, or one formed within N candles; partial mitigation optional |
| `order_block` | The same, for an order-block candidate; touched candidates optional |
| `htf_alignment` | At least *k* of the listed timeframes are in the direction (optionally none against) |
| `session` | One of the listed sessions is active, with the overlap rule any / required / excluded |
| `volatility_regime` | The vol@2 condition is among those allowed |
| `atr_range` | The ATR (price units or %) is within the bounds |
| `spread` | The spread is ≤ max_spread and ≤ max_spread_atr × ATR(M5); an unknown spread fails by default |
| `signal_agrees` | The Phase 2 signal@1 is in the direction with \|score\| ≥ the minimum |

**Rules.**
- **Validation** is strict: unknown keys or types are refused with the path, and omitted parameters
  get documented defaults.
- **Fingerprint** = a digest of the logic (not the name or description).
- **Versions** are immutable. Saving changed content adds a version; saving identical content adds
  none.
- **Deleting** archives the strategy, because runs and journal entries keep referring to its versions.
- **Export** includes the id, version, fingerprint and a disclaimer. Import creates a new strategy.

**Data-quality gate.** A strategy cannot trigger unless every timeframe it uses has enough closed
candles, fresh data and valid OHLC. The reasons are recorded.

## 4. Backtesting methodology (Parts 3–4)

1. **Dataset.** The base is the finest stored timeframe whose closed candles cover the window start,
   plus native higher-timeframe history that ended before the first base candle.
2. **Decisions.** At every step cut-off, the feature snapshot is built from candles closed by then
   (`replay.frames_at`, then `multi_timeframe.build`, then `signals.evaluate`). The strategy's
   conditions are evaluated on that snapshot.
   - `on_change` fires when the conditions turn from false to true; `every_step` fires on every
     passing step.
   - If both directions pass, the step is ambiguous and nothing is taken.
3. **Execution.** Entries fill at the next base candle open:
   - a long at ask + slippage, a short at bid − slippage (the paper simulator's `open_fill_price`);
   - ask = bid candle + that candle's stored spread, else the configured spread;
   - an entry is cancelled if a data gap delays it beyond `max_entry_delay_minutes`;
   - it is skipped if margin, exposure (`max_open_positions`, `max_exposure_lots`) or the minimum lot
     does not allow it.
4. **Exits** are the first of:
   - a stop: structure (unbroken swing ± buffer × ATR), ATR or fixed;
   - a target: R, ATR or fixed;
   - a time exit at the first open after `max_holding_minutes`;
   - a structure-flip exit at the next open;
   - end of data, or window end (walk-forward).

   Fill rules:
   - longs are checked on bid prices, shorts on ask = bid + spread;
   - a gap through a stop fills at the open; a gap through a target fills at the target (conservative);
   - every exit pays slippage (`close_fill_price`).
5. **Intrabar ambiguity** is explicit. When a candle touches both the stop and the target:
   - `stop_first` (default) assumes the stop; `target_first` is available for sensitivity checks only;
   - every affected trade is flagged and counted.
6. **Costs:**
   - commission per lot per side, on both fills;
   - spread cost = the entry spread (long) or exit spread (short) × ounces;
   - slippage on both fills;
   - margin = ounces × price ÷ leverage, checked from free margin at entry (paper-simulator rule).
7. **Missing or incomplete candles.**
   - Decisions are processed at the first base open at or after each cut-off, so a missing candle never
     drops a decision.
   - Gaps are reported in the coverage section.
   - Forming candles are refused by the engine (`BacktestError`).
8. **Reproducibility.** Each result stores the strategy id, version and fingerprint, the dataset key,
   base timeframe and SHA-256 candle hash, the analysis-config fingerprint, the assumptions and a
   SHA-256 result hash. The same inputs give the same hash (tested).

## 5. Metric formulas (Part 5, `metrics@1`)

- **net** = gross − commission (spread and slippage are inside the fill prices).
- **Counts:** win if net > 0, loss if net < 0, otherwise breakeven.
- **Win rate** = wins ÷ trades, with a Wilson 95% interval.
- **Gross profit** = Σ net of the winners; **gross loss** = \|Σ net of the losers\|.
- **Profit factor** = gross profit ÷ gross loss.
- **Average outcome** = Σ net ÷ trades.
- **Expectancy (R)** = mean R, where R = net ÷ (initial risk per oz × ounces).
- **Maximum drawdown** = the largest peak-to-trough fall of closed-trade equity and of marked-to-market
  equity at step closes, in USD and % of the peak.
- **MFE / MAE** = the largest favourable / adverse excursion from the fill while open (price, R, ATR),
  including the exit candle's range.
- **Average holding** = mean minutes from entry to exit.
- **Breakdowns:** by step timeframe, session at the signal, volatility regime at the signal, and side.
- **Edge cases:**
  - With fewer than `min_trades_for_ratios` trades (default 30), the win rate, profit factor,
    expectancy and every average are **withheld** with the reason; counts and net are always shown.
  - Profit factor is withheld when there are no losing trades.
  - Expectancy is withheld when no trade has an R.

## 6. Walk-forward methodology (Part 6, `wf@1`)

**Folds.**

```
development [s, s + D)  ->  embargo [s + D, s + D + E)  ->  evaluation [s + D + E, s + D + E + T)
rolling: s += step;  anchored: development always starts at the first start
```

**Rules.**
- **Independent windows.** Each window is backtested on its own, starts flat and force-closes at its
  end, so no trade spans two windows (tested).
- **Embargo.** It defaults to the longest maximum holding among the candidates.
- **Fixed mode:** one strategy is evaluated on every evaluation window (stability).
- **Selection mode:** the candidate with the best declared objective (expectancy R, net or profit
  factor) among those with at least `min_dev_trades` development trades is chosen using the
  **development window only**. It is then evaluated on the next unseen window.
  - A test changes the evaluation-window prices drastically and checks that the selections do not
    change.
- **Output:**
  - every fold (development metrics per candidate, the choice and the evaluation metrics);
  - out-of-sample metrics over all evaluation trades, with an equity and drawdown curve;
  - fold-to-fold variation (min / median / max / standard deviation, and how many folds were positive);
  - in-sample versus out-of-sample totals.
- **No cherry-picking.** No window is singled out, and default ranges cover all stored data.

## 7. Comparison lab and paper integration (Parts 7–8)

**Comparisons.**
- Up to 6 strategies run on **one** feature timeline under identical run assumptions (dataset, period,
  costs, intrabar rule, balance, leverage, optional sizing override).
- The comparison table covers:
  - conditions, period and observations;
  - win rate, profit factor and expectancy;
  - net, drawdown, MFE/MAE and holding time;
  - ambiguous candles.
- Also shown: session and volatility tables, and equity and drawdown charts.
- A run can be named and saved as a report (`research_runs.saved`).

**Paper integration.**
- `GET /api/strategies/{id}/live` evaluates the strategy now on LIVE data with the same snapshot,
  condition and planning code as the backtests.
- `POST /api/strategies/{id}/paper` is user-initiated (two clicks in the UI). It recomputes the setup
  on the server and opens one **simulated** paper position through the existing `PaperTradingEngine`,
  with the strategy's stop loss and take profit.
- The journal (`strategy_journal`) records the strategy id and version, setup id, signal id and time,
  the position id, the planned entry and exit assumptions, the entry costs and the conditions.
  - The outcome and invalidation reason come from `paper_positions`: stop loss means invalidated at the
    stop, take profit means target reached, manual means closed before the planned exit.
  - A position held past the planned time is flagged.
- **No automatic opening or closing** beyond the paper simulator's existing stop-loss / take-profit
  handling.

## 8. Exports (Part 10)

- **Files:**
  - trades CSV and summary CSV (`/api/research/runs/{id}/trades.csv`, `/summary.csv`);
  - PDF research report (`/report.pdf`);
  - strategy JSON (`/api/strategies/{id}/export.json`);
  - journal CSV (`/api/journal/export.csv`).
- **Contents of every file:**
  - CSVs start with `#` lines: report id, dataset (series, base timeframe, candle hash), UTC date range,
    analysis config, assumptions, strategies (name, version, fingerprint), each limitation and the
    disclaimer;
  - the PDF has a dataset/range/assumptions section, the metrics, equity and drawdown charts,
    breakdowns, the conditions and the limitations.

## 9. API and database

**New routes:**
- **Strategies:**
  - `GET` / `POST /api/strategies`;
  - `GET` / `PUT` / `DELETE /api/strategies/{id}`; `GET /api/strategies/{id}/versions`;
  - `GET /api/strategies/{id}/export.json`, `POST /api/strategies/import`;
  - `GET /api/strategies/catalogue`, `POST /api/strategies/validate`.
- **Live and paper:** `GET /api/strategies/{id}/live`, `POST /api/strategies/{id}/paper`,
  `GET /api/journal`, `GET /api/journal/export.csv`.
- **Research runs:**
  - `POST /api/research/backtests`, `/comparisons` and `/walk-forward` (202);
  - `GET /api/research/runs`, `GET` / `PATCH` / `DELETE /api/research/runs/{id}`,
    `POST /api/research/runs/{id}/cancel`;
  - exports (section 8); `GET /api/research/defaults`.
- **Page:** `/lab`.
- **Security:** every state-changing route needs the CSRF header (tested).

**Migration `0005_strategies.sql`:** `strategies`, `strategy_versions`, `research_runs` and
`strategy_journal`.
- Paper tables, provider-source columns and simulated-fill records are unchanged.
- Interrupted runs are marked failed at start-up.

## 10. Dashboard (Part 9)

**Strategy Lab** (`/lab`, linked from every page) has six tabs:
1. **Strategy Builder:** saved strategies, templates, the condition editor generated from the
   catalogue, validation, versions, import and export.
2. **Backtesting Lab:** dataset, range, costs, intrabar rule, balance and minimum trades; run list with
   progress, cancel, open, save and delete.
3. **Performance:** metric cards with withheld values marked, equity and drawdown charts, breakdowns
   and every trade.
4. **Walk-Forward Lab:** fold settings; fold table and out-of-sample equity.
5. **Strategy Comparison:** side-by-side table, session and volatility tables, equity and drawdown
   charts.
6. **Trade Journal:** the live strategy monitor with a two-step simulated entry, and the journal.

The dashboard, market-intelligence panels, replay, Performance and Paper Trading pages are unchanged.

## 11. Test results

**Totals.**
- **444 tests pass**, up from 372 at the start of Phase 3.
- That is 54 new Phase 3 tests, plus 18 new auto-generated safety cases for the new source files, plus
  every earlier test.
- Ruff is clean, and the Node chart-logic tests pass.

| File | Tests | Covers |
|------|-------|--------|
| `test_phase3_core.py` | 42 | Configuration validation (13 refusal cases) and defaults; fingerprint; condition evaluation in both directions; quality gate; ambiguous direction; hand-worked engine cases (next-open entry with exact costs, stop, target, ambiguous candle under both rules, gap through stop, time exit, structure-flip exit, short ask side, entry cancelled by a gap, decision kept when the cut-off candle is missing, on_change versus every_step, exposure, risk sizing, minimum lot, margin with and without leverage, window force-close, forming-candle refusal, MFE/MAE/holding); metrics against hand calculations (Wilson interval, drawdown, withholding); fold boundaries; selection that ignores evaluation data; no trade spanning windows; snapshot truncation invariance on recorded MT5 data; reproducible hashes and future candles ignored; CSV, JSON and PDF export accuracy |
| `test_phase3_api.py` | 12 | CRUD, versioning, export/import and archive; backtest, comparison and walk-forward through the API with exports and saved reports; refusals; coverage-aware loader; live monitor needs LIVE; user-initiated simulated entry journaled with costs, then closed by the paper stop loss with its invalidation reason; holding-time flag; safety (research never imports storage, data providers or the paper engine; only the paper ticket and the user route open simulated positions); CSRF; interrupted runs; navigation |

**Mutation checks.** 19 planted Phase 3 defects were all caught:
- snapshot look-ahead; intrabar rule ignored; gap filled at the stop;
- one-sided commission; no entry slippage; short stop checked on the bid;
- exposure ignored; margin ignored; on_change behaving like every_step; entry delay ignored;
- quality gate ignored; ratios shown for small samples; a wrong Wilson z;
- selection reading the evaluation window; windows not force-closed; embargo ignored;
- the fingerprint including the name; the live monitor without its LIVE gate; PDF xref offsets off by
  one.

The 21 Phase 2 mutation checks still pass.

**No network or MT5.** No test needs either. The fixtures are synthetic candles and the recorded MT5
candles in `tests/fixtures/`.

## 12. Verification in the application (14 November 2026; market closed, MT5 terminal not running)

- **Builder:** the three example strategies were saved through the UI; versioning and "no change"
  detection worked.
- **Comparison:** the examples were compared over **all stored M5 history**, 29 October to 13 November
  (3,010 steps, M5 base chosen by the loader fix, 0 missing candles). It ran through the Strategy Lab,
  and its charts, breakdowns and trade list rendered.
  - Final code: 0.02 lots, 10,000 USD, 1:1 leverage, paper costs.

  | Strategy | Trades | Net (USD) | Profit factor | Expectancy |
  |----------|--------|-----------|---------------|------------|
  | Example C | 53 | +80.68 | 1.23 | +0.014 R |
  | Example A | 37 | +49.63 | 1.13 | −0.029 R |
  | Example B | 2 | −22.72 | withheld | withheld |

  - Example B's ratios are withheld because it has fewer than 30 trades.
  - Example A shows how net USD and expectancy in R can disagree when stop distances vary.
- **Walk-forward:** selection mode, 5 folds, run through the API on the same history.
  - In-sample net was +106 USD (the selected candidates' development windows) against −56 USD out of
    sample, and 2 of 5 folds were positive.
  - That is the kind of degradation walk-forward is meant to reveal.
- **Saved reports:** both runs are saved in the Strategy Lab.
- **Exports:** the PDF (314 KB, 3 pages) and both CSVs downloaded from the application.
- **Live monitor:** it correctly reported "not LIVE / stale data" and offered no simulated entry. The
  simulated-entry path is verified by tests with a LIVE quote; it could not be exercised live this
  session.

## 13. Known limitations

- **Short history.** Stored MT5 history is short, so every result is a small sample. Import longer
  history (for example an MT5 bar export) for meaningful research.
- **Example strategies.** The three examples are saved in the Strategy Lab at 0.02 lots. Larger sizes
  need more margin than the default 10,000 USD, 1:1 paper account provides; the engine counts those
  skipped entries.
- **Run time.** Runs take about 0.07–0.25 s per step on this machine, depending on the timeframes in
  use. They are capped at 6,000 steps, run one at a time in the server process, and cannot resume after
  a restart (they are marked failed). The feature-timeline cache is in memory only.
- **Not modelled:** swap / financing, partial fills, requotes, broker stop distances, margin calls, and
  spread widening inside a candle.
- **MFE and MAE** include the exit candle's full range.
- **Walk-forward windows** start flat, so an `on_change` trigger can fire at a window's first step.
- **Fixed parameters.** Strategy parameters are not optimized automatically, by design: selection mode
  chooses only among strategies you define.
- **Unchecked by hand:** interactive chart zoom (Phase 2), and the live simulated entry against a
  running MT5 terminal (market closed during this phase).
- **Interpretation.** These are hypothetical results on past data under stated assumptions. They do not
  predict future performance.

## 14. Run it on Windows 11

```powershell
cd C:\Spicy\GoldScalpAI
.\.venv\Scripts\python.exe run.py
```

- **Pages:** open http://127.0.0.1:8000/lab for the Strategy Lab, http://127.0.0.1:8000/ for the
  dashboard and http://127.0.0.1:8000/paper for Paper Trading.
- **Live monitor:** start and log in to the MetaTrader 5 terminal first (read-only market data).
- **Tests and lint:**

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check goldscalp tests
```
