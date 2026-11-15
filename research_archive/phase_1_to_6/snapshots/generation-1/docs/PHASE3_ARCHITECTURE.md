# GoldScalp AI: Phase 3 Architecture (Strategy Intelligence and Advanced Backtesting)

Status: **implemented** (14 November 2026). Sections 1–13 are the design, written before implementation;
section 14 lists the changes made while building it. Full report: [PHASE_3_IMPLEMENTATION_REPORT.md](PHASE_3_IMPLEMENTATION_REPORT.md).
It builds on Phase 2
([PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md), [PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md)).

Phase 3 is historical research and simulated paper trading only:
- no broker order execution;
- no automatic trading;
- no access to real MT5 orders, positions or account data.

The paper simulator stays the only trade-execution simulation environment. Backtests compute
hypothetical outcomes with the simulator's own formulas; they never create paper positions.

---

## 1. Pre-implementation audit (Part 1)

| Item | Finding |
|------|---------|
| **Baseline** | 372 tests pass; ruff is clean (14 November 2026, 05:11 UTC). |
| **Phase 1 report** | There is no standalone file. Phase 1 is documented in IMPLEMENTATION_ROADMAP.md (Phase 1 and 1.1 tables), PROPOSED_ARCHITECTURE.md (revisions 2–3) and MARKET_DATA_PROVIDERS.md. |
| **Phase 2 status** | Complete and tested. Not yet hand-checked: interactive chart zoom, and a live MT5 check of Parts 11–18. The market is in its weekly close until Sunday 23:00 UTC, and the terminal is not running. |
| **Defect found** | `services.load_dataset` picks M1 as the base whenever *any* M1 candle falls in the window. A replay or performance range that starts before M1 coverage therefore silently starts late. **Fix:** coverage-aware base selection (section 6). |
| **Stored data** | `live:mt5:XAUUSD`: M1 from 11 November (2.3 days), M5 from 29 October (15 days), M15 from 15 October, H1 from 15 July. Longer backtests need M5/M15 bases or imported history; reports state their coverage. |
| **Reuse** | The as-of pass (`replay.frames_at`, `multi_timeframe.build`, `closed_view`); the paper formulas (`open_fill_price`, `close_fill_price`, `gross_pnl`, `commission`); `InstrumentSpec`; the job runner pattern; the `signals` table; the envelope, CSRF and CSP conventions. `perf@1` (Phase 2 signal evaluation) is kept as it is. |

## 2. Module layout

```
goldscalp/strategies/
  builder.py      strategy@1 schema: strict validation, defaults, fingerprint, condition catalogue, templates
  conditions.py   evaluate a strategy's conditions on a feature snapshot for a direction (with evidence)
  live.py         evaluate a saved strategy on live data now (paper-trading integration)
goldscalp/backtesting/
  features.py     features@1: a compact, as-of feature snapshot per step cut-off (cached timeline)
  engine.py       bt@1: deterministic, event-driven backtest over base candles
  metrics.py      metrics@1: trade statistics, with formulas and sample-size gates
  walk_forward.py wf@1: development / embargo / evaluation folds, fixed or selection mode
  lab.py          orchestration: one feature timeline shared by backtest, comparison and walk-forward
goldscalp/reports/
  pdf.py          a minimal, dependency-free PDF writer (text, tables, line charts)
  exports.py      CSV trades, CSV summary, JSON strategy, PDF research report
goldscalp/storage/  strategy_repo.py, research_repo.py, journal_repo.py; migration 0005
goldscalp/api/      jobs.py (shared job runner), routers/strategies.py, routers/research.py
web/lab.html + js/lab.js   Strategy Lab: Builder, Backtest, Performance, Walk-forward, Compare, Journal
```

## 3. Strategy configuration (`strategy@1`, Part 2)

```json
{
  "schema": "strategy@1",
  "name": "Strategy A",
  "description": "H1 and M15 bullish structure, M5 bullish BOS, acceptable spread",
  "direction": "long | short | both",
  "evaluation": {"step_timeframe": "M5", "window": {"mode": "all | last_days | range", "days": 10,
                 "start": null, "end": null}},
  "conditions": [
    {"id": "htf", "type": "structure_state", "enabled": true, "timeframe": "H1", "state": "with"},
    {"id": "bos", "type": "structure_event", "enabled": true, "timeframe": "M5", "event": "BOS",
     "direction": "with", "within_candles": 3},
    {"id": "spr", "type": "spread", "enabled": true, "max_spread": 0.5, "max_spread_atr": null,
     "if_unknown": "fail"}
  ],
  "entry": {"trigger": "on_change | every_step", "max_entry_delay_minutes": 10},
  "exit": {"stop": {"type": "structure | atr | fixed", "timeframe": "M5", "buffer_atr": 0.1,
                    "atr_multiple": 1.5, "distance": null},
           "target": {"type": "r_multiple | atr | fixed | none", "r": 2.0, "atr_multiple": null,
                      "distance": null, "timeframe": "M5"},
           "max_holding_minutes": 60,
           "exit_on_structure_flip": {"enabled": false, "timeframe": "M5"}},
  "risk": {"sizing": "fixed_lots | risk_percent", "lots": "0.10", "risk_percent": 0.5,
           "max_open_positions": 1, "max_exposure_lots": "1.00"}
}
```

**Direction.** Every condition is direction-relative: "with" means bullish for a long candidate and
bearish for a short. With `direction: both`, each step evaluates both candidates. If both pass, which
is contradictory, no entry is taken and the step is counted as ambiguous.

**Condition catalogue.**

| Type | Parameters | Passes when (for a long; shorts mirror) |
|------|-----------|------------------------------------------|
| `structure_state` | timeframe, state: with / against / ranging / not_against | The timeframe's structure@2 state matches |
| `structure_event` | timeframe, event: BOS / CHoCH / any, direction, within_candles | That event became known within the last N candles of the timeframe |
| `swing_sequence` | timeframe, pattern: trend / pullback | trend: last high HH **and** last low HL. pullback: last low HL. |
| `liquidity_sweep` | timeframe, within_candles, level_types | A confirmed bullish potential sweep (of sell-side liquidity) became known within N candles |
| `fvg` | timeframe, relation: price_inside / recent, within_candles, allow_partial | Price is inside an open bullish FVG, or one formed within N candles |
| `order_block` | timeframe, relation, within_candles, allow_touched | The same, for an order-block candidate |
| `htf_alignment` | timeframes, min_agree, none_against | At least `min_agree` of the timeframes are bullish (and none bearish if `none_against`) |
| `session` | sessions, overlap: any / required / excluded | At least one listed session is active, under the overlap rule |
| `volatility_regime` | timeframe, allowed | The vol@2 condition is in the allowed list |
| `atr_range` | timeframe, min, max, unit: price / percent | The ATR is within the bounds |
| `spread` | max_spread, max_spread_atr, if_unknown | The spread (base candle; live: provider bid/ask) is within limits |
| `signal_agrees` | min_abs_score | The Phase 2 signal@1 is BULLISH with \|score\| ≥ min |

**Data-quality gate (always applied).** Every timeframe a strategy uses must have:
- at least `signals.min_bars` closed candles;
- a fresh last closed candle;
- valid OHLC.

Otherwise the strategy cannot trigger, and the reason is recorded.

**Storage.**
- `strategies(id, name, created_at, updated_at, current_version, archived)`.
- `strategy_versions(strategy_id, version, config, fingerprint, created_at, note)`.
- Saving an unchanged config creates no version.
- Every run references `(strategy_id, version, fingerprint)`.
- Export and import use the JSON above, plus metadata.

**Templates.** Strategies A, B and C from the brief ship as **examples**, not as claims of
profitability.

## 4. Feature snapshots (`features@1`)

At every step cut-off *t*:

```
frames_at(dataset, t)  ->  multi_timeframe.build(detail = timeframes the strategies need ∪ {M5, M15})
                       ->  signals.evaluate (light)  ->  snapshot(t)
```

Each snapshot is compact and JSON-serializable:
- **Per timeframe:** bars, fresh, data problems, state, the last swing labels, unbroken reference
  high/low, the last 6 structure events with `known_at`, and the vol@2 condition, ATR, ATR % and
  fast/slow ratio.
- **Detail timeframes only:** the last 6 confirmed liquidity interactions, and the open FVG and
  order-block zones nearest price (8 each).
- **Market and context:** active sessions, base close, base spread and the Phase 2 signal (direction,
  score, gates).

**One timeline, every strategy.**
- The timeline is computed once per (dataset, base timeframe, step timeframe, range, detail set,
  config fingerprint, candle hash) and cached in memory.
- Every strategy in a comparison or walk-forward evaluates on the same timeline, so comparisons share
  identical inputs.
- A snapshot uses only candles closed by *t*. The truncation and future-candle tests from Phase 2 are
  extended to snapshots.

## 5. Backtesting engine (`bt@1`, Parts 3–4)

**Run-level assumptions.** Identical for every strategy in a run:
- costs: spread = the candle's stored spread, else the configured spread; commission per lot per side;
  adverse slippage per fill;
- the intrabar rule: `stop_first` (default, conservative) or `target_first` (sensitivity only);
- starting balance;
- `min_trades_for_ratios`;
- an optional sizing override.

**Event-driven loop over base candles** (M1 when it covers the window, else M5, and so on):

```
for each base candle b (open time o, close time c):
  a) at o: gap check. If o is beyond a position's stop (or target), exit at o.
           Pending condition exits and time exits (held >= max_holding) exit at o.
  b) at o: pending entries. Cancel when o - signal cut-off > max_entry_delay (a data gap).
           Otherwise check exposure (max_open_positions, max_exposure_lots) and sizing
           (risk_percent -> lots floored to the lot step; below the minimum lot -> skipped).
           Then open at o: long at ask + slippage, short at bid - slippage.
  c) intrabar: for every open position, stop/target against bid (long) or ask (short) high/low.
           Both inside one candle -> the intrabar rule decides (default: stop) and it is counted
           as AMBIGUOUS. Fills at the level, with slippage.
  d) at c, if c is a step cut-off: evaluate the snapshot ->
           new setups (on_change: when the conditions turn from false to true) -> pending entry at
           the next base open;
           exit_on_structure_flip -> pending exit at the next open;
           marked-to-market equity point.
after end: positions are managed on later candles until they exit, else closed at the last close
           (end_of_data). Walk-forward windows force-close at the window end instead.
```

**Missing candles.**
- Entries never fill across a gap longer than `max_entry_delay_minutes`; they are cancelled and
  counted.
- Stops through a gap fill at the next open.
- Gaps are listed in the run's coverage report.

**Incomplete candles.** Only closed candles are loaded, and a forming candle never reaches the engine.

**Stops and targets.**
- Structure stops use the unbroken swing low (long) or high (short) on the stop timeframe, minus or
  plus a `buffer_atr` × ATR buffer.
- A stop that is missing or on the wrong side falls back to `atr_multiple` × ATR.
- The R target is `r` × the initial risk.

**Trade record.**
- Setup cut-off, conditions passed (with evidence), entry and exit (time, price, reason), side, lots
  and ounces.
- Stop and target, gross P/L, spread cost, commission, slippage and net P/L.
- R multiple, MFE and MAE (price, R and ATR), holding minutes, the ambiguous flag, and session and
  volatility at entry.

**Reproducibility.**
- Each result stores the strategy fingerprint and version, the dataset key, base timeframe and range,
  a SHA-256 hash of the base candles used, the analysis config fingerprint, the assumptions and a
  result hash.
- The same inputs always give the same result hash (tested).

## 6. Dataset selection fix

The base timeframe is the finest stored timeframe whose closed candles cover the window start (the
first candle within one step of `start`) and whose step evenly divides the step timeframe. The choice
and the reason are reported.

## 7. Metrics (`metrics@1`, Part 5)

| Metric | Definition |
|--------|------------|
| **net** | gross P/L − commission (spread and slippage are already in the fill prices) |
| **Trades** | Count of closed trades. win: net > 0; loss: net < 0; breakeven: net = 0 |
| **Win rate** | wins ÷ trades, with a Wilson 95% interval |
| **Gross profit / loss** | Σ net of the winners; \|Σ net of the losers\| |
| **Profit factor** | gross profit ÷ gross loss |
| **Average outcome / expectancy** | Average outcome = Σ net ÷ trades (USD). Expectancy (R) = mean R multiple = win rate × average win R − loss rate × \|average loss R\| |
| **Maximum drawdown** | The largest peak-to-trough fall of closed-trade equity and of marked-to-market equity, in USD and % of the peak |
| **MFE / MAE** | Means and maxima, in price, R and ATR |
| **Average holding** | Mean minutes from entry to exit |
| **Breakdowns** | By step timeframe, by session at entry, by volatility regime at entry |

**Sample-size gate.** With fewer than `min_trades_for_ratios` trades (default 30), win rate, profit
factor, expectancy and averages are **withheld** (null, with the reason); counts and net are still
shown. Profit factor is also withheld when there is no losing trade.

## 8. Walk-forward validation (`wf@1`, Part 6)

**Folds.**

```
dev [s, s+D)  ->  embargo [s+D, s+D+E)  ->  eval [s+D+E, s+D+E+T);   then s += T (rolling) or
dev grows from the first start (anchored)
```

- E (embargo) defaults to the longest maximum holding among the candidates, rounded up to the step.
  No trade can span development and evaluation, and every window force-closes at its end.
- **Fixed mode:** one strategy is evaluated on every evaluation window (stability).
- **Selection mode:** in each development window, the candidate with the best declared objective
  (expectancy R, net or profit factor) among those with at least `min_dev_trades` trades is chosen.
  Only that candidate is evaluated on the next unseen window. Selection never reads an evaluation
  window.
- **Output:**
  - every fold (dev metrics per candidate, the choice and the evaluation metrics);
  - out-of-sample results aggregated over all evaluation trades;
  - fold-to-fold variation (min / median / max, the share of positive folds);
  - in-sample versus out-of-sample degradation.
- All folds are shown; the best window is never singled out.

## 9. Comparison lab (Part 7)

- N strategies run under identical run-level assumptions on one feature timeline.
- Output: a metrics table, equity and drawdown curves, session and volatility breakdowns, and
  optionally walk-forward per strategy.
- Comparisons and other runs are stored in `research_runs`; a user can name and **save** them as
  reports.

## 10. Paper-trading integration (Part 8)

- **Strategy monitor.** `GET /api/strategies/{id}/live` evaluates the saved strategy now, on the active
  live series, with the same condition code and snapshot builder. It needs LIVE data and returns the
  setup (or why not), with the planned stop, target, size and holding time.
- **User-initiated simulated entry.** `POST /api/strategies/{id}/paper` recomputes the setup on the
  server and opens a **simulated** paper position through `PaperTradingEngine`, with the strategy's
  stop loss and take profit. Nothing opens automatically, and nothing reaches MT5.
- **Journal.** `strategy_journal` records the strategy id and version, setup id, the Phase 2 signal id
  (if stored), signal time, the position id, the planned entry and exit assumptions and the entry
  costs.
  - The outcome (exit, net, close reason) is read from `paper_positions`, so nothing is duplicated.
  - The invalidation reason is derived: a stop loss means "invalidated at <stop source>", a take profit
    "target reached", a manual close "closed manually". The planned holding time being exceeded is
    flagged.
- The journal page also lists any backtest's hypothetical trades.

## 11. Exports (Part 10)

- **Files:**
  - CSV trade report;
  - CSV performance summary;
  - JSON strategy config;
  - PDF research report: summary, assumptions, metrics, equity and drawdown charts, breakdowns and
    limitations.
- Every file states its dataset, date range, assumptions and limitations.
- The PDF writer is dependency-free (PDF 1.4: standard Helvetica, text, tables and line charts). Its
  structure (xref offsets) is checked by tests.

## 12. API (new)

- **Strategies:**
  - `GET /api/strategies`, `POST /api/strategies`;
  - `GET`, `PUT` and `DELETE /api/strategies/{id}`; `GET /api/strategies/{id}/versions`;
  - `GET /api/strategies/{id}/export.json`, `POST /api/strategies/import`;
  - `GET /api/strategies/catalogue` (condition types, parameters, templates);
  - `POST /api/strategies/validate`.
- **Live and paper:** `GET /api/strategies/{id}/live`, `POST /api/strategies/{id}/paper`,
  `GET /api/journal`, `GET /api/journal/export.csv`.
- **Research runs:** `POST /api/research/backtests`, `/comparisons` and `/walk-forward` (202);
  `GET /api/research/runs`, `GET /api/research/runs/{id}`; `POST /api/research/runs/{id}/cancel`;
  `PATCH /api/research/runs/{id}` (name, saved); `DELETE /api/research/runs/{id}`.
- **Exports:** `GET /api/research/runs/{id}/trades.csv`, `/summary.csv` and `/report.pdf`.

## 13. Tests (Part 11)

- Configuration validation, condition evaluation (each type, both directions) and the quality gate.
- Hand-worked engine cases: entry at the next open, stop and target, an ambiguous candle under both
  intrabar rules, a gap through the stop, a time exit, a structure-flip exit, entry cancelled by a gap,
  exposure limits, risk sizing and the minimum lot, costs, and end of data.
- Metrics against hand calculations, including the gates.
- Walk-forward fold boundaries, the embargo, selection using only development data, and aggregation.
- Feature snapshot and backtest truncation invariance; a result that is identical with future candles
  appended; reproducible hashes.
- Comparison under identical assumptions; journal linkage through the paper engine; CSV / JSON / PDF
  content.
- API flows, CSRF, and the safety checks: no order placement, no MT5 trading calls, and research never
  writes paper tables.

## 14. As built: changes made during implementation

- **Margin.** The engine checks margin at entry, like the paper simulator: required = ounces × price ÷
  leverage, from free margin (closed equity + unrealized − used margin − commission).
  - Entries without enough margin are skipped and counted (`insufficient_margin`).
  - `leverage` is a run assumption; the default is the paper account's (`GOLDSCALP_PAPER_LEVERAGE`, 1).
  - The default strategy size is therefore **0.02 lots**. 0.10 lots of gold at about 4,000 USD needs
    about 40,000 USD of margin at 1:1.
- **Fixed stops.** A fixed-distance stop does not make its (unused) timeframe a data requirement.
- **Missing cut-off candles.** Decisions are processed at the first base candle open at or after each
  cut-off. A missing base candle at a cut-off therefore never drops a decision.
- **Shared planners.** `engine.plan_stop_level`, `plan_target_level` and `size_lots` are module-level
  and shared by backtests and the live strategy monitor.
- **Job runner.** It was generalized (`api/jobs.py`). Performance evaluations (Phase 2) and research
  runs share one background job at a time.
- **Journal CSV.** The route is `/api/journal/export.csv`.

