# GoldScalp AI: Phase 2 Architecture (Smart Market Intelligence)

Status: **implemented** (14 November 2026, Parts 1–18).
- Sections 1–10 are the design, written before implementation.
- Section 11 records what Parts 1–10 built.
- Section 12 is the design of Parts 11–18, with an as-built note in 12.6.
- The full report is [PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md). It builds on PROPOSED_ARCHITECTURE.md (revisions 1–3) and does not replace working
Phase 1 components.

Phase 2 adds **explainable, deterministic** market analysis:
- market structure, liquidity, fair value gaps (FVG), order-block candidates;
- multi-timeframe and session intelligence, and volatility;
- a signal centre;
- a historical replay engine.

It is analysis only. There is **no broker execution, no automated trading and no machine-learning
model**. Every output is a rule-based observation with its evidence attached.

---

## 1. Findings from the Phase 2 inspection

| Finding | Consequence |
|---------|-------------|
| Baseline: 231 tests pass, ruff clean; files unchanged since Phase 1.1 | Safe to build on. |
| **Defect:** M1-derived higher-timeframe bars are marked `closed` once every *elapsed* M1 bar is closed (`data/resample.py`), so the current, unfinished hour looks like a closed H1 candle | **Fixed first:** a derived bar is closed only if all its sub-bars are closed **and** its end time ≤ the analysis cut-off (`as_of`). |
| `analysis/structure.py` (`structure@1`) has no HH/HL/LH/LL labels, no ranging state, no de-duplication and no developing observations | It is kept unchanged for compatibility (its tests stay). The new `market_structure.py` (`structure@2`) powers the dashboard. |
| `analysis/sessions.py` (`sessions@1`) and `analysis/volatility.py` (`vol@1`) exist with tested APIs | They are **extended** (configurable windows, overlap, breakouts, comparisons; rolling volatility, expansion/contraction, body analysis). Existing functions keep their behaviour. |
| MT5 native higher-timeframe bars include data from inside the bar | In **replay**, higher timeframes are always rebuilt from the base series up to the replay time, never read from native higher-timeframe bars. |

---

## 2. The one rule every engine follows: "as of"

```
analyse(candles, timeframe, as_of, config) -> observations
```

1. **Closed candles only.** A candle counts if `closed` is true **and** `open + timeframe ≤ as_of`.
   Engines never see later candles.
2. **Knowable time.** Every event carries `known_at`, the close time of the candle that made it
   knowable. Swings are knowable only `right` candles after the pivot.
3. **Developing observations** are allowed only from the currently forming candle, or from the most
   recent closed candles before their confirmation window completes. They are labelled
   `status: "developing"` and are **never** used for confirmed events, signals or statistics.
4. **Truncation invariance (tested).** Results computed with cut-off *t* must equal the full-history
   results filtered to `known_at ≤ t`. This is the formal "no look-ahead" test applied to every engine.

## 3. Common observation schema

```json
{
  "id": "M5:BOS:bullish:2026-11-13T18:35:00Z",
  "kind": "structure | liquidity | fvg | order_block | session | volatility",
  "type": "BOS | CHoCH | swing_high | swing_low | EQH | EQL | PDH | ... | sweep | break | fvg | ob",
  "label": "HH | HL | LH | LL | ...",
  "direction": "bullish | bearish | neutral",
  "timeframe": "M1 | M5 | M15 | H1",
  "time": "2026-11-13T18:35:00Z",
  "price": 3881.5,
  "zone": {"top": 3882.1, "bottom": 3880.9},
  "status": "confirmed | developing",
  "known_at": "2026-11-13T18:40:00Z",
  "explanation": "Close 3884.20 above the last confirmed swing high 3883.40 (HH, swing at 18:20) ...",
  "candles": ["M5@2026-11-13T18:20:00Z", "M5@2026-11-13T18:35:00Z"],
  "definition": "structure@2"
}
```

- Candle IDs (`"M5@<UTC open time>"`) identify the exact source candles. The series key travels in
  the response envelope.
- Every response includes the **definitions and parameters** used, so stored results are reproducible.

## 4. Configuration: `config/analysis.json`

- Validated strictly: unknown or missing fields are errors.
- The file is versioned, and its fingerprint is stored with every signal.
- Settings by area:

| Area | Settings |
|------|----------|
| Structure | swing strength per timeframe; minimum break buffer (fraction of ATR); de-duplication distance |
| Liquidity | equal-level tolerance (fraction of ATR, with a USD floor); minimum separation (bars); sweep return window (bars) |
| FVG | minimum size (fraction of ATR, with a USD floor) |
| Order blocks | displacement (fraction of ATR); zone type (`range` or `body`); search window |
| Sessions | name, IANA timezone, start and end per session |
| Volatility | ATR period; ADR days; rolling window; expansion and contraction thresholds |
| Signals | maximum spread; freshness; weights and thresholds |

## 5. Modules (`goldscalp/analysis/`)

| Module | Definition | Purpose |
|--------|-----------|---------|
| `candles.py` | – | `closed_view(frame, tf, as_of)`, `forming_bar(...)`, candle IDs, ATR helper. The single place that enforces §2. |
| `market_structure.py` | `structure@2` | Confirmed swings with HH/HL/LH/LL labels; BOS/CHoCH from closes beyond the last confirmed unbroken swing (with a break buffer and de-duplication); state `bullish / bearish / ranging` with reasons; developing swing candidates. |
| `liquidity.py` | `liquidity@1` | Equal highs/lows (clusters of confirmed swings within tolerance); PDH/PDL (17:00 New York day), PWH/PWL, Asian/London/New York session highs/lows; sweep classification (below). |
| `fvg.py` | `fvg@2` | Three-candle gaps ≥ minimum size; overlap de-duplication; mitigation (`unmitigated`, `partially_mitigated` with %, `fully_mitigated`) updated only with candles after formation and up to `as_of`. |
| `order_blocks.py` | `ob@1` | Last opposite-colour candle before a displacement leg that produced a BOS/CHoCH. It is known only at the break candle's close. Mitigation: `untouched / touched / invalidated`. Always labelled **"algorithmic candidate"**. |
| `sessions.py` | `sessions@2` | Configurable windows in their own IANA zones (DST-correct); London/New York overlap; current session; session H/L/range, range vs ATR; volatility comparison by session; breakout observations (e.g. London closes beyond the Asian range). |
| `volatility.py` | `vol@2` | ATR (Wilder), ADR, rolling volatility of log returns, expansion/contraction (fast ATR ÷ slow ATR), candle range percentile, average body and body/range ratio. Values in price units **and** % of price. |
| `multi_timeframe.py` | `mtf@1` | Per timeframe: structure, last events, volatility condition, nearest liquidity, open FVG/OB, data freshness. Includes a summary and **plain-language explanations of disagreements** (for example "M5 bearish against bullish H1/M15: a pullback within the higher-timeframe up-trend"). |
| `signals.py` | `signal@1` | Deterministic classification (§6). |
| `replay.py` | – | Replay sessions (§7). |

### Liquidity sweep classification (never "every breakout is a sweep")

| Observation | Rule (for a level above price; mirror for levels below) |
|-------------|----------------------------------------------------------|
| **Potential bearish liquidity sweep: confirmed rejection** | A candle's high goes above the level and the **same candle closes back below** it. |
| **Unconfirmed level break** | A candle **closes above** the level. Status `developing` until resolved. |
| **Sweep with delayed rejection** | After an unconfirmed break, a close back below the level within `sweep_return_bars` candles. |
| **Confirmed break (acceptance)** | No close back below within `sweep_return_bars`. This is a break, not a sweep. |

## 6. Signal centre (`signal@1`)

**Gates.** Any failure gives **NO SIGNAL**, with the reasons listed.
- The market state is not LIVE in live mode. In replay, the replay candle stream is the clock.
- The spread is above `max_spread`.
- There are fewer than the required closed candles for H1/M15/M5/M1.
- There are invalid or incomplete candles at the cut-off.

**Evidence.** Each item is a named, weighted rule that is either listed as **supporting** or
**conflicting**:
- H1 structure (±2), M15 structure (±2) and M5 structure (±1);
- an M1 BOS/CHoCH within the last 15 M1 candles (±1);
- a recent liquidity sweep (±1);
- price inside an unmitigated same-direction FVG or order-block candidate (±1).

Context notes carry no score: session, volatility expansion or contraction, spread relative to ATR.

**Classification.**
- **BULLISH** when the agreement score is ≥ +4 and neither H1 nor M15 is bearish.
- **BEARISH** when the score is ≤ −4 and neither H1 nor M15 is bullish.
- **NEUTRAL** otherwise.

The score is labelled an **"evidence agreement score (not a probability)"**.

**Output.** Each signal contains:
- direction, timestamp (`generated_at`) and data cut-off (`as_of`), and the timeframe;
- structure per timeframe;
- supporting and conflicting observations;
- invalidation conditions (the price levels that would contradict it);
- spread and volatility context;
- data source and freshness;
- an explanation;
- the definitions and config fingerprint, and the input candle range.

**Persistence.** Signals are stored in a `signals` table (mode `live` or `replay`), so historical
evaluation can reproduce them.

**Evaluation.**
- **Replay:** after the replay passes each signal's horizon, forward outcomes (move after N candles,
  MFE/MAE in ATR units) are computed from later candles and shown **separately, labelled
  "historical observation"**.
- **Paper simulator:** a signal can prefill the paper ticket (side, a stop at the invalidation
  level). The position records `signal_id`.

## 7. Replay engine

- **Session setup:** dataset (any stored series), base timeframe (M1, or the coarsest available),
  start and end. Candles inside the window are loaded once.
- **Cursor:** the cut-off is the close time of the last revealed base candle. Higher timeframes are
  **rebuilt from base candles up to the cursor**. A partially elapsed higher-timeframe bar is shown
  as *forming* and is excluded from confirmed analysis.
- **Controls:** start, pause, resume, step 1 or N candles, and speed (candles per second). The
  browser drives the clock; the server holds the cursor and computes as-of analysis per step.
- **Tests:** analysis at step *k* must equal analysis of the dataset truncated at that cut-off, and
  adding future candles to the dataset must not change any step's output.

## 8. API additions

| Route | Returns |
|-------|---------|
| `GET /api/analysis/intelligence?tf=&series=` | Overlays for one timeframe (structure, liquidity, FVG, OB) plus multi-timeframe, sessions, volatility and the current signal |
| `GET /api/signals?mode=&limit=` | Signal history |
| `POST /api/replay`, `GET /api/replay/{id}`, `POST /api/replay/{id}/step`, `DELETE /api/replay/{id}` | Replay sessions |
| `GET /api/analysis/config` | Active definitions and parameters |

## 9. UI additions

- **Chart overlays:**
  - FVG and order-block zones drawn as shaded rectangles by a Lightweight Charts *series primitive*,
    which never moves the price scale;
  - liquidity levels as labelled price lines;
  - structure labels (HH/HL/LH/LL, BOS/CHoCH) as markers, limited to the most recent N to avoid
    clutter.
- **Panels:** a multi-timeframe summary, a Signal Centre (with supporting and conflicting evidence,
  invalidation and context), liquidity, session and volatility.
- **Replay page** (`/replay`).
- **Wording:** every zone and signal is described as an *observation* or *candidate*. Wording such
  as "guaranteed" or "probability of profit" never appears; a test enforces this.

## 10. Test plan (additions)

- Truncation invariance for structure, liquidity, FVG, order blocks and replay.
- Golden hand-worked cases for each detector.
- De-duplication; sweep versus break classification; FVG mitigation progression; the order-block
  link to its BOS.
- Session DST weeks and overlap; breakout observations.
- Volatility expansion and contraction.
- Multi-timeframe disagreement wording.
- Exclusion of incomplete higher-timeframe bars (the resample fix).
- Signal gates (stale data, wide spread, invalid candles), classification and wording.
- API and replay flows.

---

## 11. As built

### Modules and definitions

| Module | Definition | Notes |
|--------|-----------|-------|
| `candles.py` | – | `closed_view`, `forming_bar`, candle ids, per-bar Wilder ATR. |
| `config.py` | – | Strict validator for `config/analysis.json`; its fingerprint is stored with every signal. |
| `market_structure.py` | `structure@2` | As designed. The first break in a sample is labelled BOS, explained as "the first structure break in the available candles". "Mixed" structure means an opposing HH/LL confirmed **after** the last break. |
| `liquidity.py` | `liquidity@1` | As designed. EQH/EQL also require that no candle between the two swings traded beyond the level. If a timeframe's history starts after a level formed, the gap is checked on H1 first. Older equal-level pairs within tolerance of a newer untouched pair are folded into it (`touches`). |
| `fvg.py` | `fvg@2` | As designed. `mitigation` is reported separately from `status` (the gap itself is always `confirmed`). |
| `order_blocks.py` | `ob@1` | Displacement is measured from the zone to the break candle's close. There is one candidate per source candle. |
| `sessions.py` | `sessions@2` (`sessions@1` kept) | Breakout rule: the first close beyond the range of another session's latest window that started before the target window and is complete. At most one breakout per direction. |
| `volatility.py` | `vol@2` (`vol@1` kept) | `timeframe_profile()` per timeframe, plus ADR from H1. The per-session comparison comes from `sessions@2`. |
| `multi_timeframe.py` | `mtf@1` | `build()` runs every engine; `summarise()` and `explain_disagreements()` produce the wording. Freshness: the last closed candle must have ended within one bar plus 90 s of the cut-off. |
| `signals.py` | `signal@1` | `classify()` is the pure rule. There are two spread gates: absolute (`max_spread`) **and** relative (`max_spread_atr_m5`). Ids are deterministic (mode, replay, series, cut-off, config fingerprint, direction). |
| `intelligence.py` | – | The single as-of pass shared by live mode and replay. |
| `replay.py` | – | Dataset, cursor, `frames_at()`, and an in-memory session store (8 sessions). |

### Storage and API

- Migration `0003_signals.sql` adds a `signals` table and `paper_positions.signal_id`.
- `signal_repo.record_if_changed()` stores a row only when the classification **changes**. The table
  records transitions, not every request.
- New routes:
  - `GET /api/analysis/intelligence` and `GET /api/analysis/config`;
  - `GET /api/signals` and `GET /api/signals/{id}`;
  - `POST /api/replay`, `GET /api/replay`, `GET /api/replay/{id}`, `POST /api/replay/{id}/step`,
    `POST /api/replay/{id}/seek` (added for Restart) and `DELETE /api/replay/{id}`.
- `GET /api/analysis/overview` (Phase 1) is kept for compatibility.
- `/api/market/candles` and `/overview` now pass `as_of = now`, so a partially elapsed derived candle
  is always marked forming.
- `POST /api/sim/orders` accepts an optional `signal_id`. It must be a stored **live** signal; replay
  signals are historical observations and are refused.

### Replay data

- **Base candles:** M1 when the series has M1 inside the range, otherwise the step timeframe. They
  load from `start - lookback` (1–20 days, aligned to the hour) to `end`.
- **History:** native higher-timeframe candles that **ended before the first base candle**. A derived
  bar that would straddle the start of the base data is dropped.
- **Cursor:** moves in step-timeframe candles that exist in the data, so weekends are skipped.
- **Spread:** the signal gates use the last base candle's stored spread, when the dataset has one.
- **Forward outcomes:** each directional replay signal gets them after 3 and 12 M5 candles (move,
  MFE and MAE, in price and ATR(M5) units). They come only from candles closed by the cursor and are
  labelled **historical observation**.

### UI

- **Dashboard:**
  - overlay toggles for BOS/CHoCH, swings, liquidity, FVG and order blocks, remembered per browser;
  - FVG and order-block zones drawn by a series primitive whose `autoscaleInfo()` returns null;
  - liquidity levels as price lines (the 8 nearest untouched or testing levels);
  - "sweep?" markers;
  - panels: Signal Centre, multi-timeframe summary, liquidity, sessions and volatility;
  - a screen-reader announcement only when the signal changes.
- **Replay page (`/replay`):**
  - setup: dataset and its coverage, step timeframe, UTC start and end, lookback;
  - controls: Play/Pause, Step 1, Step 10, Restart and speed (0.5–4 candles/s); Space and → keys;
  - a replay clock and progress bar;
  - the chart, every panel, and the signal-change history with forward outcomes.
- **Paper ticket:** prefills from `?signal=<id>`. It sets the side and puts the stop loss at the first
  invalidation level, with a "simulated, rule-based observation" notice.

### Verification

Verified on 14 November 2026. The system clock read Friday 13 November, about 19:00–19:20 UTC.

- **Tests:** 306 passed at the end of Part 10 (231 before Phase 2) and ruff was clean. After Parts
  11–18 there are 372; see section 12.6.
- **Mutation checks:** all eight planted defects were caught:
  - replay peeks one base candle;
  - replay resample ignores `as_of`;
  - swing confirmation peeks one candle;
  - `closed_view` ignores `as_of`;
  - a close beyond a level counted as a sweep;
  - no LIVE gate;
  - an order block known before its break;
  - FVG mitigation sees future candles.
- **Live MT5 series (`live:mt5:XAUUSD`, LIVE):**
  - `/api/analysis/intelligence` returned every overlay, panel and the signal in about 0.2 s once the
    server was warm (1–2 s just after a restart, during the MT5 backfill);
  - the signal was NEUTRAL with score −1 and no failed gates;
  - the disagreements were explained ("M15 bullish against bearish H1 …");
  - the classification change was stored once.
- **Replay on recorded MT5 data:**
  - 72 M5 steps over 3,044 M1 base candles, with native M5/M15/H1 history before them;
  - about 0.2 s per step once warm;
  - no candle after the replay time appears in any frame (tested);
  - play, pause, step and restart were exercised in the browser.
- **Paper prefill:** checked in the browser; the side and stop loss were filled and the notice shown.
  No order was placed. Replay signals were then restricted from linking.

### Not done / limits

- Signal history records changes only; per-candle signal logs are not kept.
- There is no bulk report yet that runs a whole replay and summarises every signal. Outcomes
  accumulate as the replay is stepped.
- Replay sessions live in memory and are lost on restart; their stored signals remain until deleted.
- Equal-level de-duplication is presentation-only.
- Weights and thresholds are untuned defaults. Change them in `config/analysis.json`; the fingerprint
  changes and is stored with each signal.
- **The Phase 2 brief arrived cut off** after Part 10's "critical requirement". Parts 11–18 arrived
  later; they are designed in section 12.

---

## 12. Parts 11–18 design (written before implementing them)

### 12.1 Performance evaluation: `analysis/performance.py` (`perf@1`)

**Signals evaluated.**
- A run evaluates **every** step candle in the chosen range for each chosen step timeframe stream
  (M1, M5 or M15). It never samples or skips periods.
- Each step uses the same as-of pass as replay (`replay.frames_at`), so the signals are identical to
  replay signals.
- A **signal event** is a change of classification into BULLISH or BEARISH. A classification that
  persists is one signal, not one per candle, which would inflate counts with overlapping,
  autocorrelated samples.
- A change into NEUTRAL is a **neutral observation**. NO SIGNAL steps are counted together with
  their gate reasons.

**A. Analytical evaluation (no costs).** For each directional signal:
- from the closed base candles after its cut-off: the signed close-to-close move at `horizon_m5`,
  plus MFE and MAE, in price and ATR(M5) units;
- classification: **favourable** (move ≥ +`flat_band_atr` × ATR), **adverse**
  (≤ −`flat_band_atr` × ATR) or **flat**;
- **insufficient data** if the horizon is not covered by the dataset.

**B. Hypothetical strategy (with costs).** A separate section, using the paper simulator's own
formulas (`open_fill_price`, `close_fill_price`, `gross_pnl`, `commission`). No orders, accounts or
paper positions are created; the paper simulator stays the only trade-execution environment.
- **Entry:** at the open of the first base candle after the cut-off. A long fills at ask + slippage
  and a short at bid − slippage. The ask is the bid candle plus that candle's stored spread; when the
  dataset has no spread, the configured paper spread is used and labelled as such.
- **Stop:** the signal's first invalidation level. If it is missing or on the wrong side,
  `stop_atr_fallback` × ATR(M5) is used.
- **Target:** `target_r` × the initial risk; 0 disables it.
- **Exit:** the first of stop, target or horizon. Long stops and targets are checked on bid prices;
  short ones on ask (bid + spread).
  - If the stop and target are both touched in one candle, the **stop is assumed first**.
  - A gap through the stop fills at the candle open.
  - Each exit pays slippage.
- **Size and costs:** fixed `lots` × the instrument's contract size; commission per lot per side on
  both fills.
- **One position at a time per stream:** signals during an open position are counted as
  `skipped_overlap`.
- **Equity:** closed-trade equity from `starting_balance`; maximum drawdown in USD and %.
- **Profit factor:** gross profit ÷ gross loss, reported only with at least
  `min_trades_for_ratios` trades and a gross loss above 0. Otherwise it is null, with the reason.

**Breakdowns:** by step timeframe stream, by session at the signal time (Asian, London, New York, a
London/New York overlap, or none), and by M5 volatility condition.

**No cherry-picking.**
- The UI defaults to the **entire** stored range of the base timeframe.
- The report states the range, candles, steps and gated steps, and warns when the sample is small.
- Parameters are never optimized on results.
- Every report carries the statement that hypothetical past results do not predict future results.

**Execution.**
- One background job at a time, with progress and cancel, capped at `max_steps` per run.
- Outcome candles may extend past `end` (loaded separately). Signal generation never sees them; this
  is tested.

### 12.2 Storage (migration `0004`)

- `performance_runs`: parameters, status, progress, config fingerprint and the result JSON.
- `analysis_configs(fingerprint, data)`: every config used by a stored signal or run, so a stored
  fingerprint can always be resolved to its exact parameters.
- Run signals are stored in the existing `signals` table (mode `replay`, `replay_id = "<run>:<tf>"`),
  with their outcome in `signals.evaluation`. There is no new signal table.
- Paper tables, provider-source columns and simulated-fill records are unchanged.

### 12.3 API

- `GET /api/analysis/structure | liquidity | fvg | order-blocks` (`tf`, `series`, `swing_strength`)
- `GET /api/analysis/multi-timeframe | sessions | volatility` (`series`)
- `GET /api/signals/current` (`series`)
- `GET /api/performance/defaults`
- `POST /api/performance/runs`; `GET /api/performance/runs` and `/{id}`;
  `POST /api/performance/runs/{id}/cancel`; `DELETE /api/performance/runs/{id}`

### 12.4 Chart (Part 13)

- **Settings per timeframe** (saved per browser): BOS/CHoCH, swings, liquidity lines, sweeps, FVG,
  order blocks, developing items, maximum labels and display-only swing sensitivity.
  - M1 defaults are sparse: swings, sweeps, zones and developing items are off; 6 labels.
  - The sensitivity changes only the chart overlays. The signal always uses `config/analysis.json`.
- **Zoom-aware labels:**
  - only annotations inside the visible time range count toward the maximum;
  - marker text is hidden above 160 visible bars and swing markers above 300.
- **Developing items** are drawn dimmed, with "?" labels and dashed outlines.

### 12.5 Dashboard (Part 12)

- **A. Market Structure:** trend, swings with labels, breaks, and confirmed versus developing items.
- **B. Liquidity Map:** level table with status and distance, plus sweep and break observations.
- **C. Price Action:** FVG and order-block tables with mitigation.
- **D. Multi-timeframe:** side-by-side M1 / M5 / M15 / H1 cards.
- **E. Session Intelligence:** current session, range, high and low, volatility versus recent
  sessions, and London/New York overlap status.
- **F. Signal Centre:** timestamp and freshness shown first.
- **G. Replay page:** as built, with the same panels.
- **H. Performance page** (`/performance`): run setup, progress, metrics, breakdowns, equity curves
  per stream, and the signal list.

### 12.6 As built (Parts 11–18)

**Built as designed.** See [PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md) for the
details.

**Additions:**
- **Speed.** The analysis pass fell from 0.37 s to 0.15 s per step: cached ISO candle stamps,
  vectorized swing tests and array-based level scans. The truncation-invariance tests guard the result.
- **Light build.** `multi_timeframe.build(detail=...)` lets bulk evaluation skip liquidity, FVG and
  order-block analysis on H1 and M1, which the rules never read. A test proves the signal is
  identical.
- **Session context.** The signal now takes its session context directly from the session definitions,
  so every path (dashboard, `/api/signals/current`, replay, evaluation) produces the same payload.
- **Chart-label logic** lives in `web/js/annotation-select.js` and is unit-tested with Node
  (`tests/js/`).
- **Recorded fixtures.** MT5 candles in `tests/fixtures/` support the no-look-ahead, replay, symmetry
  and evaluation tests.

**Tests.** 372 pass. The 21 planted defects across both rounds were all caught.

**Limits:**
- Interactive zoom was not exercised by hand (the browser pane was hidden). The selection logic is
  unit-tested.
- The live-data path was not re-checked on 14 November because the MT5 terminal was not running.

