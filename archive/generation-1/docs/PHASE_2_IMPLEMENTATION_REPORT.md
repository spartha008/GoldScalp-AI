# GoldScalp AI: Phase 2 Implementation Report (Smart Market Intelligence)

Date: 14 November 2026.
Design: [PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md), sections 1–10 for Parts 1–10 and section 12
for Parts 11–18.

Phase 2 is analysis and simulation only:
- there is no broker execution, no order placement and no automated trading;
- MetaTrader 5 stays a read-only market-data source;
- the paper simulator is still the only place where simulated trades are executed.

---

## 1. Summary

| Part | Result |
|------|--------|
| 1. Inspect and document | `PHASE2_ARCHITECTURE.md` was written before code. It found and fixed a look-ahead defect: derived higher-timeframe candles were marked closed before their period ended. |
| 2. Market structure | `structure@2`: confirmed swings, HH/HL/LH/LL, BOS/CHoCH, bullish/bearish/ranging, developing items |
| 3. Liquidity | `liquidity@1`: EQH/EQL, PDH/PDL, PWH/PWL, session levels, and sweep-versus-break classification |
| 4. FVG | `fvg@2`: gaps, minimum size, de-duplication, partial/full mitigation |
| 5. Order blocks | `ob@1`: algorithmic candidates linked to their BOS/CHoCH |
| 6. Multi-timeframe | `mtf@1`: H1/M15/M5/M1 side by side, with disagreement explanations |
| 7. Sessions | `sessions@2`: own-timezone windows (DST-correct), overlap, ranges, comparison, breakouts |
| 8. Volatility | `vol@2`: ATR, ADR, rolling volatility, expansion/contraction, ranges, in price and % |
| 9. Signal centre | `signal@1`: gates, weighted evidence, invalidation, context, deterministic ids |
| 10. Replay | `/replay`: play, pause, step, speed and restart, with no look-ahead (tested) |
| 11. Performance | `perf@1`: analytical evaluation and a hypothetical cost-aware strategy, shown separately |
| 12. Dashboard | Sections A–H added; the existing dashboard and Paper Trading page are kept |
| 13. Chart | Per-timeframe annotation settings, label limit, zoom-aware labels, display sensitivity, developing style |
| 14. API and storage | Granular routes; migrations `0003` and `0004`; signals stored with their full analysis and config |
| 15. Testing | 372 tests pass, up from 231. Synthetic and recorded MT5 fixtures are used; 21 planted defects were all caught. |
| 16. Safety | Read-only MT5 allowlist, no credentials, localhost binding, CSRF on every new route, TradingView isolated |
| 17. Documentation | This report, plus README, PROPOSED_ARCHITECTURE, IMPLEMENTATION_ROADMAP and PHASE2_ARCHITECTURE |

## 2. New and changed modules

**New in `goldscalp/analysis/`:**

| Module | Definition | Purpose |
|--------|-----------|---------|
| `candles.py` | – | `closed_view` and `forming_bar`: the as-of rule. Also cached ISO candle stamps (`Stamps`) and per-bar Wilder ATR. |
| `config.py` | – | Strict loader and validator for `config/analysis.json`; fingerprint; `with_swing_strength()` for display sensitivity |
| `market_structure.py` | `structure@2` | Swings, labels, breaks, state, developing items |
| `liquidity.py` | `liquidity@1` | Levels and interactions |
| `fvg.py` | `fvg@2` | Fair value gaps |
| `order_blocks.py` | `ob@1` | Order-block candidates |
| `multi_timeframe.py` | `mtf@1` | Runs every engine per timeframe, summary, disagreement wording |
| `signals.py` | `signal@1` | Gates, evidence, classification, invalidation, forward outcomes |
| `intelligence.py` | – | The single as-of pass shared by live mode, replay and the routes |
| `replay.py` | – | Dataset, cursor, `frames_at()` (the only way replay data reaches analysis), session store |
| `performance.py` | `perf@1` | Historical evaluation |

**Extended, keeping the Phase 1 API:**
- `sessions.py` adds `sessions@2`; `sessions@1` is kept.
- `volatility.py` adds `vol@2`; `vol@1` is kept.

**Elsewhere:**
- `data/resample.py`: `as_of` support (the look-ahead fix).
- `api/routers/analysis.py`: granular routes.
- `api/routers/replay.py`, `api/routers/performance.py`.
- `api/services.py`: shared dataset loaders.
- `storage/signal_repo.py`, `storage/performance_repo.py`.
- Migrations `0003_signals.sql` and `0004_performance.sql`.
- `simulation/positions.py` and `simulation/portfolio.py`: an optional `signal_id` on positions.

**Web:**
- Changed: `index.html`, `paper.html`.
- New pages: `replay.html`, `performance.html`.
- New scripts: `js/intel-panels.js`, `js/annotations.js`, `js/annotation-select.js`, `js/replay.js`,
  `js/performance.js`.
- `js/chart.js` gains a zone primitive, zoom-aware markers and a multi-line chart.

## 3. Algorithms and detection rules

**The as-of rule (applies everywhere).**
- A candle is used only if it is `closed` **and** `open + timeframe ≤ as_of`.
- Every event carries `known_at`: the close of the candle that made it knowable.
- **Developing** items come only from the forming candle, or from swings whose confirmation window is
  still incomplete. They are never used for events, signals or statistics.

**Swings.** A swing high at bar *i* requires:
- `high[i] > max(high[i−k … i−1])`;
- `high[i] ≥ max(high[i+1 … i+k])`.

It is known at the close of bar *i+k*, where *k* is the per-timeframe swing strength. Lows mirror
this; the tie handling is symmetric and tested. Labels:
- **HH/LH:** a high above or below the previous swing high;
- **HL/LL:** a low above or below the previous swing low.

**BOS and CHoCH.**
- A candle must **close** beyond the latest confirmed, unbroken swing by more than
  `break_buffer_atr × ATR`. Wick-only pierces never count.
- A break in the prevailing direction is a BOS; a break against it is a CHoCH.
- A same-direction BOS of nearly the same level shortly after another is folded into it
  (`dedupe_atr`, `dedupe_bars`).
- **State:**
  - bullish or bearish follows the last break;
  - it becomes ranging if an opposing HH/LL is confirmed after the break, or if there has been no
    break for `ranging_after_bars` candles.

**Liquidity levels.**
- **EQH/EQL:** two confirmed swings at least `min_separation_bars` apart, within
  `max(equal_tolerance_atr × ATR, equal_tolerance_min)`, with no candle beyond the level between them.
- **PDH/PDL:** the previous complete trading day, which rolls at 17:00 New York.
- **PWH/PWL:** the previous complete trading week, known at Friday 17:00 New York.
- **Session highs and lows:** the latest completed window of each session.

**Sweeps versus breaks** (first interaction only):
- **Potential sweep, confirmed rejection:** price pierces the level and the same candle closes back
  inside.
- **Unconfirmed break:** a close beyond the level. It resolves as follows:
  - a close back inside within `sweep_return_bars` makes it a **potential sweep with delayed
    rejection**;
  - otherwise it is a **confirmed break (acceptance)**.

**FVG.**
- **Gap:** bullish if `low(c) > high(a)`, bearish if `high(c) < low(a)`.
- **Minimum size:** `max(min_size_atr × ATR, min_size_abs)`.
- **De-duplication:** a gap that overlaps an earlier open gap by at least `overlap_dedupe` is folded
  into it.
- **Mitigation:** depth = how far later candles traded into the gap. It is unmitigated, partially
  mitigated (%) or fully mitigated.

**Order-block candidates.**
- **Candidate:** for each break, the last opposite-colour candle at or before the leg's extreme,
  within `search_bars`.
- **Zone:** the candle's range or body.
- **Requirement:** the break close is at least `displacement_atr × ATR` beyond the zone.
- **Known at:** the break close.
- **Lifecycle:** untouched, then touched, then invalidated.

**Sessions.**
- Each window is defined in its own IANA zone and converted to UTC per date, so DST is handled.
- Also reported: overlaps, the current and next session, the range compared with ATR and with the
  previous sessions, and the first close beyond another session's completed range.

**Volatility.**
- ATR is Wilder's.
- Expansion/contraction = ATR(fast) ÷ ATR(slow), compared with the thresholds.
- Rolling volatility = the standard deviation of log returns.
- Also reported: range percentile, body/range, and ADR over complete trading days.

**Signal (`signal@1`).**
- **Gates:** the data is not LIVE (live mode); stale data; spread above the absolute limit or above
  `max_spread_atr_m5 × ATR`; too few candles; invalid candles.
- **Evidence:**
  - H1 ±2, M15 ±2, M5 ±1;
  - an M1 break within 15 M1 candles ±1;
  - an M5 sweep within 12 M5 candles ±1;
  - price inside an open M5/M15 zone ±1.
- **Classification:**
  - BULLISH when the score is ≥ +4 and neither H1 nor M15 is bearish;
  - BEARISH is the mirror;
  - otherwise NEUTRAL.
- **The score** is an "evidence agreement score (not a probability)".
- **Ids** are deterministic.

## 4. Configuration (`config/analysis.json`)

- The file is validated strictly: unknown or missing keys are errors.
- Its fingerprint is stored with every signal and run.
- Every fingerprint is also stored in `analysis_configs`, so stored signals stay reproducible.

| Section | Settings (defaults) |
|---------|---------------------|
| `structure` | `swing_strength` per timeframe (3); `break_buffer_atr` 0.05; `dedupe_atr` 0.25; `dedupe_bars` 10; `ranging_after_bars` 60; `max_events_returned` 40 |
| `liquidity` | `equal_tolerance_atr` 0.10; `equal_tolerance_min` 0.10; `min_separation_bars` 5; `sweep_return_bars` 3; `max_levels_returned` 30 |
| `fvg` | `min_size_atr` 0.15; `min_size_abs` 0.10; `overlap_dedupe` 0.5; `max_returned` 30 |
| `order_blocks` | `displacement_atr` 1.0; `zone` "range"; `search_bars` 20; `max_returned` 20 |
| `sessions` | Asian (Asia/Tokyo 09:00–18:00), London (Europe/London 08:00–17:00), New York (America/New_York 08:00–17:00); `compare_days` 10 |
| `volatility` | `atr_period` 14; `adr_days` 20; `rolling_window` 20; `fast_atr` 5; `slow_atr` 50; `expansion_ratio` 1.3; `contraction_ratio` 0.75; `range_lookback` 100 |
| `signals` | `max_spread` 0.60; `max_spread_atr_m5` 0.5; `min_bars` 60 per timeframe; `m1_event_lookback` 15; `sweep_lookback_m5` 12; `classify_threshold` 4; `weights`; `evaluation_horizons_m5` [3, 12] |
| `performance` | `horizon_m5` 12; `flat_band_atr` 0.25; `stop_atr_fallback` 1.5; `target_r` 2.0; `lots` 0.10; `starting_balance` 10000; `min_trades_for_ratios` 20; `min_signals_warning` 30; `max_steps` 6000 |

**Overrides.**
- A performance run may override the horizon, target, fallback stop, flat band, lots, spread
  fallback, commission and slippage. The values used are stored with the run.
- Cost defaults come from the paper simulator settings (`GOLDSCALP_PAPER_*`).

## 5. API changes

All routes return the labelled envelope (`kind`, `source`, `as_of`, `generated_at`). State-changing
routes require the `X-GoldScalp-Request: 1` header (CSRF).

| Route | Purpose |
|-------|---------|
| `GET /api/analysis/intelligence?tf=&series=&swing_strength=` | Everything for one chart timeframe; `swing_strength` changes the chart overlays only |
| `GET /api/analysis/structure`, `/liquidity`, `/fvg`, `/order-blocks` (`tf`, `series`, `swing_strength`, `limit`) | One engine each |
| `GET /api/analysis/multi-timeframe`, `/sessions`, `/volatility` (`series`) | One engine each |
| `GET /api/analysis/config` | Active configuration, fingerprint and definitions |
| `GET /api/analysis/overview` | Phase 1 summary (unchanged) |
| `GET /api/signals/current` | Signal centre; stored when the classification changes |
| `GET /api/signals?mode=&series=&replay_id=&limit=`, `GET /api/signals/{id}` | Signal history and one signal |
| `POST /api/replay`, `GET /api/replay`, `GET/DELETE /api/replay/{id}`, `POST /api/replay/{id}/step`, `POST /api/replay/{id}/seek` | Replay; each accepts a display `swing_strength` |
| `GET /api/performance/defaults` | Evaluation defaults (config, paper costs, instrument) |
| `POST /api/performance/runs` (202), `GET /api/performance/runs`, `GET /api/performance/runs/{id}`, `POST /api/performance/runs/{id}/cancel`, `DELETE /api/performance/runs/{id}` | Evaluation runs |
| `GET /api/performance/configs/{fingerprint}` | The exact configuration behind a stored fingerprint |
| `POST /api/sim/orders` | Optional `signal_id`; it must be a stored **live** signal |

## 6. Database migrations

They are applied automatically at start-up; existing data is kept.

| Migration | Changes |
|-----------|---------|
| `0003_signals.sql` | `signals` table: id, mode, replay_id, series, as_of, cut-off, generated_at, direction, score, definition, config fingerprint, full JSON payload, evaluation. Adds `paper_positions.signal_id`. |
| `0004_performance.sql` | `analysis_configs(fingerprint, data, first_seen)` and `performance_runs` (parameters, status, progress, config fingerprint, result). |

**Unchanged:** the provider-source columns (`open_source` / `close_source`) and all simulated-fill
records.

**Signals table.**
- It stores live and replay signals **only when the classification changes**.
- Performance runs store each signal they find, with its outcome, under `replay_id = "<run>:<timeframe>"`.
- Each payload carries everything needed to reproduce it: series, cut-off, input candle range,
  definitions, config fingerprint and frame limits.

## 7. Dashboard and chart changes

**Dashboard:**
- Chart, with the annotation controls.
- **D. Multi-timeframe:** M1, M5, M15 and H1 cards side by side, with alignment and disagreement
  sentences.
- **A. Market structure:** trend; unbroken swing high/low; recent confirmed breaks and swings, with
  candle and confirmation times; developing items.
- **C. Price action:** FVG and order-block tables with mitigation status.
- **F. Signal centre:** direction and score; timestamp, computation time, freshness and source first;
  supporting, conflicting and neutral evidence; invalidation; context; paper-ticket prefill for live
  directional signals.
- **B. Liquidity map:** a level table (price, distance, status, known at), developing session levels,
  and sweep/break observations.
- **E. Session intelligence:** current session, London/New York overlap status, next session, range,
  high/low, range ÷ ATR and range ÷ the recent average, and breakouts.
- **Volatility** panel.
- **H. Performance analytics:** a card with the latest run, linking to `/performance`.
- The Paper Trading page is unchanged apart from the navigation links and the prefill notice.

**Replay page (G):**
- the same chart controls and all panels;
- the signal-change history with forward outcomes.

**Performance page (H):**
- run setup, with the full stored range as the default;
- progress and cancel, and the run list;
- per stream: the signal counts, the analytical evaluation and the hypothetical strategy, in separate
  blocks;
- equity curves per stream;
- tables by timeframe, session and volatility;
- the assumptions, and every evaluated signal.

**Chart (Part 13):**
- **Toggles:** BOS/CHoCH, swings, liquidity lines, sweeps, FVG, order blocks and developing items.
- **Controls:** maximum labels (4–40); display-only swing sensitivity (2–8).
- **Saved per timeframe:** M1 defaults to BOS/CHoCH and liquidity only, with 6 labels.
- **Zoom-aware labels:**
  - only annotations in the visible range count toward the maximum;
  - they are filled by priority: breaks, then developing items, then sweeps, then swings;
  - marker text is hidden above 160 visible candles;
  - swing markers are hidden above 300.
- **Developing items** are dimmed gold, with "?" labels and dashed zones.
- **Zones** never change the price scale.

## 8. Historical replay assumptions

- **Base candles.** M1 is the base when the series has M1 in the range; otherwise the step timeframe.
- **Higher timeframes** are rebuilt from base candles up to the cursor. The forming bar is marked and
  excluded.
- **History.** Native higher-timeframe candles are used only if they **ended before** the first base
  candle.
- **Cursor.** It moves in step candles that exist in the data, so weekends are skipped.
- **Spread.** It is the stored spread of the last base candle visible at the cursor.
- **Forward outcomes** use candles closed by the cursor and are labelled *historical observation*.
- **Sessions** live in memory: at most 8, and they are lost on restart.

## 9. Performance evaluation assumptions

- **Signals evaluated.**
  - Every step candle of the chosen range, for each chosen step timeframe stream (M1, M5, M15).
  - A signal is a **change** into BULLISH or BEARISH; a change into NEUTRAL is a neutral observation.
  - Gated steps are counted by reason.
- **Analytical evaluation (no costs).**
  - The signed close-to-close move from the signal's reference price after `horizon_m5` M5 candles,
    plus MFE and MAE, in price and ATR(M5) units.
  - Classified favourable, adverse or flat (± `flat_band_atr` × ATR).
- **Hypothetical strategy.** Priced with the paper simulator's formulas:
  - **Entry:** the open of the next base candle; ask + slippage for a long, bid − slippage for a short.
  - **Spread:** the candle's stored spread, or the configured spread when the dataset has none.
  - **Stop:** the invalidation level (fallback: × ATR).
  - **Target:** × the initial risk.
  - **Exit:** the first of stop, target or horizon. If both stop and target fall in one candle, the
    **stop is assumed**; a gap through the stop fills at the open; each exit pays slippage.
  - **Costs:** commission per lot per side.
  - **One position at a time per stream;** overlaps are counted.
  - **Equity:** closed-trade equity from the starting balance, with maximum drawdown.
  - **Profit factor:** only with at least `min_trades_for_ratios` trades and at least one loss.
- **Not modelled:** swap/financing, partial fills, requotes, broker stop levels, or a spread that
  widens inside a candle.
- **No cherry-picking.** The default range is everything stored, and no step is skipped. The step limit
  is a refusal, never a silent cut. Small samples are flagged, and parameters are never optimized on
  results.
- **Disclaimer.** Every result carries: "Hypothetical results … do not predict future performance."

## 10. Test coverage

**Totals.** 372 tests pass, up from 231 before Phase 2. All 231 Phase 1 tests still pass, together
with 20 new auto-generated safety cases (the safety scan is parametrized over the new source files).
Ruff is clean.

| File | Tests | Covers |
|------|-------|--------|
| `test_phase2_engines.py` | 34 | Config strictness; as-of rule and resample fix; structure golden case and wick-versus-close; developing-only forming breaks; de-duplication; equal highs; 7 sweep/break cases; PDH/PDL/PWH/PWL; FVG formation, mitigation, size and de-duplication; order-block link, touch/invalidation and displacement; sessions (DST, overlap, next, breakouts); volatility; MTF wording; truncation invariance per engine |
| `test_phase2_api.py` | 27 | Classification rule; required fields; determinism; gates (not LIVE, spread, stale, too few candles, invalid candles); forward outcomes; storage on change; paper `signal_id`; replay with no look-ahead and future candles; routes; wording scan |
| `test_performance.py` | 20 | Hand-worked trades (stop, target, same-candle stop first, gap, horizon, short ask side, fallback stop, configured spread) with exact costs; profit factor; drawdown; flat band; change-only signals; end-to-end totals on recorded data; signals independent of later candles and spreads; light pass equals the full pass; step-limit refusal; jobs; API flow; refusals; interrupted runs; paper tables untouched |
| `test_phase2_extra.py` | 39 | Swing confirmation delay (k = 2, 3, 5); equal-value ties; HH/HL/LH/LL; trading-day close across US DST; London/New York overlap across UK and US DST changes; session boundaries; Wilder ATR golden values; MTF synchronization; incomplete candles; mirror symmetry of swings and signals; conflicting H1/M15 never directional; recorded-data no look-ahead and replay equality; granular routes; display sensitivity; safety; Phase 1 regressions |
| `test_frontend_logic.py` + `tests/js/annotation_select.test.mjs` | 1 (5 JS) | Chart label selection: priority, visible range, zoom thresholds, off-chart candles |

**Fixtures.**
- Synthetic candles (`tests/conftest.py`).
- **Recorded MT5 candles** (`tests/fixtures/`): 2,068 M1 and 400 H1 bid candles with spreads.
- No test needs MT5 or network access; sockets are blocked.

**Mutation checks.** Each of these planted defects made a test fail:
- *Part 10:* replay peeking; resample ignoring `as_of`; early swing confirmation; `closed_view`
  ignoring `as_of`; a close beyond a level treated as a sweep; no LIVE gate; an order block known early;
  FVG mitigation seeing the future.
- *Parts 11–16:*
  - target checked before stop;
  - a gap filled at the stop;
  - commission on one side only;
  - entry before the signal;
  - no slippage on a short exit;
  - flat band ignored;
  - every step counted as a signal;
  - the bulk pass peeking one candle (spread leak);
  - the light pass dropping M15 zones;
  - display sensitivity leaking into the signal;
  - profit factor shown for tiny samples;
  - chart labels ignoring the visible range;
  - an asymmetric swing-low tie rule.

## 11. Verification against the real application

- **Live, 13 November 2026 (market open, MT5 LIVE).**
  - The dashboard analysis ran on `live:mt5:XAUUSD` in about 0.2 s (0.15 s after the Part 11 speed
    work).
  - The signal was NEUTRAL with no failed gates, and the disagreements were explained.
  - Replay on the recorded MT5 candles: about 0.2 s per step.
- **14 November 2026 (Saturday).**
  - The MT5 terminal was not running, so the dashboard correctly showed DISCONNECTED and the Signal
    Centre "NO SIGNAL" (stale, not LIVE).
  - Every panel rendered from the stored MT5 history.
  - A full-range evaluation of the recorded MT5 data ran through the Performance page: 816 steps (M5
    and M15 streams) in about 40 s.
  - During that run the API stayed responsive: health 21 ms, analysis about 0.5 s.
  - The server log showed no errors.
- **Not repeated live.** The live-data path for Parts 11–18 was not re-checked against a running
  terminal on 14 November. The MT5 provider code did not change after the 13 November verification.

## 12. Known limitations

- **Interactive zoom.** It was not exercised by hand: the browser pane was hidden during the Part 13
  checks. The selection logic is unit-tested in Node, and the label counts were checked on page load.
- **Untuned defaults.** Signal weights, thresholds and swing strengths are untuned. Change them in
  `config/analysis.json`; every result records the fingerprint.
- **Signal history** stores changes of classification only, not a log per candle.
- **Performance runs.**
  - They run one at a time in the server process, and a run is marked failed if the server stops.
  - They are capped at `max_steps` (6,000).
  - There is no walk-forward or out-of-sample split yet.
- **Analysis windows.** The analysis uses fixed windows (M1 1,500, M5 1,000, M15 600 and H1 400
  candles), so very old swings are outside the window.
- **Equal levels.** Equal-level de-duplication is presentation-only.
- **Replay sessions** are in memory.
- **Not modelled by the hypothetical strategy:** financing/swap, partial fills, broker stop distances,
  or intra-candle spread widening.
- **Session windows** are configurable conventions, not exchange facts.
- **Interpreting results.** Hypothetical results on a few days of data (the recorded MT5 history) say
  very little; the page warns about small samples.

## 13. Run it on Windows 11

```powershell
cd C:\Spicy\GoldScalpAI
.\.venv\Scripts\python.exe run.py
```

Open:
- the dashboard: http://127.0.0.1:8000/
- Paper Trading: http://127.0.0.1:8000/paper
- Replay: http://127.0.0.1:8000/replay
- Performance: http://127.0.0.1:8000/performance

For live data, start the MetaTrader 5 terminal and log in to the demo account first.

Tests and lint:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check goldscalp tests
```

`node --test tests\js\annotation_select.test.mjs` runs the chart-logic tests directly. Pytest also
runs them when Node.js is installed.
