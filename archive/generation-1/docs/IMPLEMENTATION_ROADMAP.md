# GoldScalp AI: Implementation Roadmap

This is the build order for [PROPOSED_ARCHITECTURE.md](PROPOSED_ARCHITECTURE.md).
Reuse items `R#` refer to the carry list in [REUSE_VS_REBUILD.md §3](REUSE_VS_REBUILD.md).
Section references `§n` point into PROPOSED_ARCHITECTURE.md.

> Phase 0 (audit) was approved. Phase 1 was revised on 13 Nov 2026 to include live data and lot-based paper trading, and is implemented.

---

## Definition of done (every phase)

1. `pytest` passes with sockets blocked. `ruff` is clean. The working tree is clean after the test run.
2. **Prove each new rule's test can fail.** Break the rule and watch its test go red.
   Then break an unrelated rule and confirm the test still passes.
3. Every new API route returns the `kind` envelope (P1). The route-iteration test covers it automatically.
4. Every new detector has golden cases plus the truncation-invariance (no look-ahead) property test (P2).
5. Boundary tests still pass:
   - no provider imports from `analysis`, `sim` or `research`;
   - no `MetaTrader5` import outside its one module;
   - no order-placement names anywhere except the demo-only gateway `goldscalp/execution/mt5_demo.py`
     (optional MT5 demo-account execution, off by default; see Phase M).
6. `README.md` and `.env.example` match the code. New settings are documented.
7. No personal identifiers, real account values or raw third-party responses are committed.

Sizes are relative: **S** ≈ a few sessions, **M** ≈ a week of focused work, **L** ≈ more than that.

---

## Phase 0: Audit and architecture review

**Status: done.** The four audit documents were delivered and the architecture was approved.

---

## Phase 1 (revised): live data, lot-based paper trading, initial dashboard

**Status: implemented on 13 November 2026.** This revision moved live market data and lot-based
paper trading forward into Phase 1. The old "Terminal UI shell" phase is folded in. Each status
below is checked against the code and tests.

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Project foundation | Done | `pyproject.toml`, `requirements*.txt`, `constraints.txt`, `goldscalp/core/` (strict settings, UTC clock, timeframes, trading schedule, kinds), SQLite with WAL and migrations |
| 2 | Market-data provider research | Done | [MARKET_DATA_PROVIDERS.md](MARKET_DATA_PROVIDERS.md) |
| 3 | Free XAUUSD provider integration | Done (gold-api.com verified live; Tiingo built from docs and **not verified live**, no key) | `data/providers/gold_api.py`, `data/providers/tiingo.py`, `data/live.py` |
| 4 | Historical CSV import | Done | `data/providers/csv_file.py` (`generic_utc`, `mt5_export`), `data/ingestion.py`, `POST /api/data/import`, dashboard form |
| 5 | Normalized candle data engine | Done | `data/normalization.py`, `validation.py`, `gaps.py`; one schema for live and historical series |
| 6 | Timeframe handling | Done | M1/M5/M15/H1; stored ("native") or derived from M1 with `sub_bars` |
| 7 | Lot-based paper account | Done | `simulation/account.py`, `portfolio.py`; balance ledger, equity snapshots |
| 8 | Instrument specification configuration | Done | `config/instruments/xauusd_demo.json` (hypothetical, every field required), `simulation/instrument.py` |
| 9 | Simulated position and cost calculations | Done: market orders plus SL/TP, both sides | `simulation/positions.py`, `pricing.py`, `costs.py` |
| 10 | Initial dashboard | Done | `/` (market, chart, structure, sessions, volatility, account, optional TradingView, import) and `/paper` |
| 11 | Automated tests | Done: 167 tests; mutation checks caught all 4 planted faults | `tests/` |

**Also delivered:**
- Basic `structure@1` (swings, BOS/CHoCH), `sessions@1` and `vol@1`, all with no look-ahead (originally Phases 3 and 5).
- The optional TradingView display component (PROPOSED_ARCHITECTURE.md §16).
- MT5 integration point A.

**Not done in Phase 1** (carried forward):
- **Simulator:** pending (limit/stop) orders; replay mode on historical bars; stop-out/margin call.
  The simulator only trades on current prices, so it can't be used when no provider is reachable.
- **Data:** a holiday calendar (holidays currently show as gaps); persistent request/bandwidth
  counters (they reset on restart).
- **Tiingo:** live verification with a real key; confirming the gold ticker.
- **Interface:** session overlays on the chart; configurable session definitions; editing the
  instrument file from the UI.
- **Deployment:** Docker image build verification (Docker is not installed on this machine); a
  Playwright UI smoke test in CI.

---

## Phase 1.1: MetaTrader 5 demo data (read-only)

**Status: implemented and verified against the local MT5 terminal on 13 November 2026, with the
market open.**

| Acceptance criterion | Result |
|----------------------|--------|
| 1. Detect the local MT5 terminal | Verified live (build 6317, broker connected); mocked tests also cover not running, failed initialize and broker disconnected |
| 2. Identify the gold symbol or request manual selection | Verified live (`XAUUSD`, conversion symbol `XAUUSD.crp` pushed down); manual override in the dashboard and `.env` |
| 3. Current bid/ask ticks | Verified live: LIVE, 0.4-0.5 s old, spread 0.28-0.29 |
| 4. M1/M5/M15/H1 candles | Verified live: 3,000 / 3,000 / 2,000 / 2,000 bars |
| 5. Normalize and validate | Verified live: 0 rejected, 0 duplicates, 0 bars outside the schedule after server-time conversion |
| 6. Dashboard shows provider and status | Verified in the browser: provider, symbol, state, tick time, age, freshness, last update, reconnects, timeframe, MT5 details |
| 7. Paper trading still works | Verified: estimate priced from the MT5 ask plus slippage, `source=mt5`; existing simulator tests unchanged |
| 8. Disconnections handled safely | Mocked tests (DISCONNECTED immediately, last price labelled STALE, fills refused); live manual Reconnect verified |
| 9. Phase 1 features intact | All 169 earlier tests still pass; 3 updated for intentional changes (CSRF header, fresh-tick rule, MT5 preference) |
| 10. All tests pass | 231 passed; planted mutations of the new rules were caught |

**Not done:** MT5 integration points B/D/E (MQL5 export script, MT5-driven forward-paper extras,
showing GoldScalp levels in MT5); persisting the dashboard's symbol override across restarts (use
`.env`); verifying a real broker disconnect live (mocked only).

---

## Phase 2: Smart Market Intelligence

**Status: implemented on 14 November 2026 (Parts 1–18).** Full report:
[PHASE_2_IMPLEMENTATION_REPORT.md](PHASE_2_IMPLEMENTATION_REPORT.md). Design:
[PHASE2_ARCHITECTURE.md](PHASE2_ARCHITECTURE.md).

Phase 2 delivers the analysis parts of Phases 3, 4 and 5 below. It also delivers the replay part of
Phase 6, and a historical signal evaluation with a hypothetical cost-aware strategy (part of
Phase 8).

| Part | Result |
|------|--------|
| 1. Inspect and document first | `PHASE2_ARCHITECTURE.md` was written before any code. It found and fixed a look-ahead defect: derived higher-timeframe candles were marked closed before their period ended (`resample(..., as_of)`). |
| 2. Market structure | `structure@2`. Golden case, wick-versus-close, de-duplication, developing-only forming breaks and truncation-invariance tests. |
| 3. Liquidity | `liquidity@1`. EQH/EQL, PDH/PDL, PWH/PWL and session levels. Sweep, delayed sweep, unconfirmed break and acceptance are classified separately (7 parametrized cases). |
| 4. Fair value gaps | `fvg@2`. Golden mitigation progression (0% → 33.3% → 100%), minimum size and de-duplication. |
| 5. Order blocks | `ob@1`. Candidates linked to their BOS/CHoCH and known at the break close; touch and invalidation; displacement filter. |
| 6. Multi-timeframe | `mtf@1`. Excludes forming higher-timeframe candles; disagreements explained in words. |
| 7. Sessions | `sessions@2`. Configurable windows in their own timezones, DST-correct, with overlap, current and next session, range comparison and breakouts. |
| 8. Volatility | `vol@2`. ATR, ADR, rolling volatility, expansion or contraction, range percentile and body/range, in price and %. |
| 9. Signal Centre | `signal@1`. Gates, weighted evidence, the "evidence agreement score (not a probability)", invalidation, context, deterministic ids, stored transitions and paper-ticket prefill. |
| 10. Replay | `/replay` with play, pause, step, speed and restart. Analysis uses only candles closed by the replay time; tested against the truncated dataset and with future candles added. |
| 11. Performance evaluation | `perf@1` on `/performance`, as a background run. Two separate parts: an analytical evaluation (favourable/adverse/flat, MFE/MAE, no costs) and a hypothetical strategy using the paper simulator's formulas (spread, commission, slippage, drawdown, profit factor only when meaningful). Broken down by timeframe, session and volatility. Every step of the range is evaluated, with the full stored range as the default. |
| 12. Dashboard | Sections A–H: market structure, liquidity map, price action, side-by-side multi-timeframe, session intelligence, signal centre, replay page, performance page and card. The existing dashboard and Paper Trading page are kept. |
| 13. Chart | Per-timeframe annotation settings (sparse M1 defaults), max labels, zoom-aware labels, display-only swing sensitivity, a separate developing style, and separate FVG and order-block toggles. |
| 14. API and storage | Granular analysis routes, `/api/signals/current`, performance routes. Migration `0004` adds `performance_runs` and `analysis_configs`; run signals reuse the `signals` table. |
| 15. Testing | 372 tests pass (231 Phase 1 tests unchanged); recorded MT5 fixtures; mirror symmetry; Node chart-logic tests; 21 planted defects all caught. |
| 16. Safety | MT5 allowlist unchanged; no credential fields; localhost binding; CSRF on every new route (tested); the evaluation never touches paper tables (tested). |
| 17. Documentation | README, PROPOSED_ARCHITECTURE (revision 4), this roadmap, PHASE2_ARCHITECTURE and `PHASE_2_IMPLEMENTATION_REPORT.md`. |

**Final acceptance criteria**

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Phase 1 still works | All Phase 1 tests pass, plus new regression tests (overview route, candles `as_of`, CSV import, paper engine) |
| 2 | MT5 provides live data | Verified LIVE on 13 November. On 14 November (Saturday) the terminal was not running and the app correctly showed DISCONNECTED. The provider code is unchanged since that verification. |
| 3–7 | Structure, liquidity, FVG/OB, multi-timeframe, sessions and volatility | Implemented for M1, M5, M15 and H1; tested on synthetic and recorded MT5 data |
| 8 | Explainable signals on the dashboard | Signal centre with evidence, conflicts, invalidation, timestamp and freshness |
| 9–10 | Replay without look-ahead | Implemented; equality with truncated datasets tested on synthetic and recorded data |
| 11 | Historical signal evaluation | Implemented; run on all recorded MT5 data (816 steps) through the UI |
| 12 | Readable, configurable charts | Implemented. The selection logic is unit-tested; interactive zoom was not exercised by hand because the browser pane was hidden. |
| 13 | All tests pass | 372 passed; ruff clean |
| 14 | Documentation updated | Yes |
| 15 | SpicyStock untouched | Yes; all work is in `C:\Spicy\GoldScalpAI` |

**Not done:**
- A per-candle signal log.
- Tuning the weights and thresholds.
- Persistent replay sessions.

(Walk-forward and out-of-sample evaluation were delivered in Phase 3.)

---

## Phase 3 (brief): Strategy Intelligence and Advanced Backtesting

**Status: complete on 14 November 2026.** The full report is
[PHASE_3_IMPLEMENTATION_REPORT.md](PHASE_3_IMPLEMENTATION_REPORT.md); the design is
[PHASE3_ARCHITECTURE.md](PHASE3_ARCHITECTURE.md). This delivers the backtesting and evaluation parts
of Phases 6–8 below for rule-based strategies.

| # | Acceptance criterion | Result |
|---|----------------------|--------|
| 1 | Strategy Builder works | 12 condition types; strict validation; versions, fingerprint, JSON export/import; the UI editor, verified in the browser |
| 2 | Historical backtesting works | `bt@1` on all stored M5 history (3,010 steps) through the UI; 15 hand-worked engine tests |
| 3 | Transaction costs included | Spread (candle or configured), commission per lot per side, adverse slippage per fill, margin; exact-cost tests |
| 4 | No-look-ahead protection tested | Snapshot truncation invariance on recorded MT5 data; future candles appended without changing decisions; a planted peek is caught |
| 5 | Performance metrics correct | Hand-calculated tests (PF, expectancy, Wilson interval, drawdown); withheld below the sample threshold |
| 6 | Walk-forward evaluation works | Folds, embargo, force-closed windows; selection proven independent of evaluation data; UI run |
| 7 | Strategy comparison works | One shared timeline and identical assumptions (tested); UI table, charts, saved report |
| 8 | Paper simulation integration works | User-confirmed simulated entry through `PaperTradingEngine`, journaled and closed by the paper stop loss (tested with a LIVE quote). It needs LIVE data in the app, and the market was closed during this phase. |
| 9 | Reports can be exported | Trades CSV, summary CSV, strategy JSON, PDF (structure checked), journal CSV |
| 10 | All previous tests pass | 444 passed (372 before Phase 3); ruff clean |
| 11 | Documentation updated | README, PROPOSED_ARCHITECTURE (revision 5), this roadmap, PHASE3_ARCHITECTURE and the report |

**Not done / limits:**
- Long-history research needs imported data: stored MT5 history is 2.3 days of M1 and 15 days of M5.
- Runs are capped at 6,000 steps and run one at a time.
- No parameter optimization (by design).
- No live check of the simulated entry this session (market closed).

---

## Phase 4 (brief): AI Research Assistant, Vantage MT5 Demo Integration and Live Strategy Evaluation

**Status: built and tested on 14 November 2026. Actual Vantage demo terminal checked (record `mv_214ba65715fb0f86`, PARTIAL: market closed); a fresh-tick VERIFIED run is pending until the open.** The report is
[PHASE_4_IMPLEMENTATION_REPORT.md](PHASE_4_IMPLEMENTATION_REPORT.md); the design is
[PHASE4_ARCHITECTURE.md](PHASE4_ARCHITECTURE.md). Criteria 1-2 (and the live part of 3-7 and 11) need your
running Vantage MT5 demo terminal, with the market open from Sunday 23:00 UTC. They are not claimed until a
stored verification record from the real `MetaTrader5` package shows them.

| # | Acceptance criterion | Result |
|---|----------------------|--------|
| 1 | Vantage MT5 terminal detected and verified | Actual terminal: attached, connected, Vantage demo server `VantageMarkets-Demo`, symbol, candles and reconnect all PASS. PARTIAL until a fresh tick (market closed). |
| 2 | Live XAUUSD prices received | **Pending** the market open (the terminal's last Friday tick was read) |
| 3 | All supported timeframes work | Done on the actual terminal: M1, M5, M15 and H1 clean, and M5, M15 and H1 equal to M1 rebuilt |
| 4 | Phase 3 strategies evaluated against live data | Monitor uses the backtest's own snapshot (parity tested); live run pending |
| 5 | Signals timestamped and explained | Tested: evidence, conflicts, invalidation, bid/ask/spread, source, freshness |
| 6 | Every simulated trade recorded | Tested: validation accounts, SL / time / structure-flip exits, MFE/MAE, journal |
| 7 | Per-trade evaluations generated | Tested: seven answers, permanent |
| 8 | Strategy weaknesses identified | Study on the stored MT5 history (see the report) |
| 9 | Improvement proposals validated historically | Unseen-holdout validation; verdict rules tested; user-only adoption |
| 10 | Strategy versions tracked | Approval creates a new version; monitoring stays pinned to versions |
| 11 | Dashboard presents live-paper performance | `/live` built and checked in the browser; live data pending |
| 12 | All previous tests pass | See the report |
| 13 | Documentation updated | README, PROPOSED_ARCHITECTURE (revision 6), this roadmap, PHASE4_ARCHITECTURE and the report |

---

## Phase 5 (brief): Monthly multi-timeframe strategy research and backtesting

**Status: built and tested on 14 November 2026.** See [PHASE_5_IMPLEMENTATION_REPORT.md](PHASE_5_IMPLEMENTATION_REPORT.md)
and [PHASE5_ARCHITECTURE.md](PHASE5_ARCHITECTURE.md).
- Built as a separate research service, so the Phase 4 monitors kept running.
- The October 2026 evaluation completed M15 and M5 for A v2, B v2 and C v4 plus nine research candidates (R1-R9,
  chosen at 1:1.5 or 1:2 on data before October). Evaluations: `me_cb6b197c4c6b` (R1-R6) and `me_697122f9e14b`
  (R1-R9). The 27 runs they share reproduced identical result hashes.
- **October result (one month, not a profitability finding):** A, B and C lost on every completed run
  (−5.8 % to −24.7 % of $1,000). Most trades ended at the 60-minute time exit. Among the candidates, only R6 had
  a positive net, with a negative mean R. R7 and R8 traded too rarely to judge.
- **M1 is INCOMPLETE** in those evaluations: the terminal returned no October M1 history.
- **Phase 5.1 (14 Nov 2026):** October M1 imported from the user's MT5 export; the research-store evaluation
  `me_23932a71a56a` completed all nine A/B/C runs including M1. Hypothetical percentage-risk scenarios
  (5 / 7.5 / 10 % of current simulated equity) ran as 27 separate accounts (risk study `rs_d9e49bedf913`).
  The original fixed-lot results are unchanged. See the report, §12.

## Phase 5.2: Strategy redesign and multi-month robustness research

**Status: completed on 15 November 2026 (simulation only).** See [PHASE_5_2_RESEARCH_REPORT.md](PHASE_5_2_RESEARCH_REPORT.md).
- **Two-stage study.** First, diagnostics on development months only (study `rb_825da1543f0f`): why the 12 originals
  lose and what each exit change does. Then redesign, then the full study `rb_7ca33b7b5a81`: 565 configurations,
  June-November 2026, walk-forward, and a November 1-13 hold-out. October is secondary.
- **New capabilities (additive, off by default):** break-even and trailing stops, and a `level_retest` condition.
- **Final classification** (`verdicts@2`, frozen before any hold-out result existed):
  - **promising:** R2 v2 only, a thin +0.05 R/trade that depends on October being secondary;
  - **inconclusive:** B v3, R1 v2, R4 v2, R5 v2, R7 v2;
  - **rejected:** A v3, C v5, R3 v2, R6 v2, R8 v2, R9 v2;
  - no overfit.
- **Originals:** kept unchanged as benchmarks. R6 is not fixed: all 48 variants are negative on development, and its
  October dollar profit was a sizing artefact (−0.66 R).
- **Next step (the user's decision):** test the frozen candidates on unseen pre-June history, or forward in the paper
  monitor. No further redesign until there is more evidence.

## Phase 5.3: Frozen pre-June historical validation

**Status: completed on 15 November 2026 (simulation only).** See [PHASE_5_3_VALIDATION_REPORT.md](PHASE_5_3_VALIDATION_REPORT.md) and PHASE5_ARCHITECTURE.md §14.
- **Result:** none of the four candidates held up. R2 v2, R5 v2 and R7 v2 are rejected (negative before and after
  costs); R1 v2 is inconclusive at +0.006 R/trade (2 of 5 months, negative with costs x2). No candidate
  qualifies for paper-monitor observation; January-May 2026 must not be used to re-tune.
- **Locked shortlist:** R2 v2, R1 v2, R5 v2 and R7 v2, exactly as chosen in Phase 5.2, plus their four originals as
  benchmarks. No new variant and no selection.
- **Period:** January to May 2026, five separate monthly $1,000 accounts, on M5 and M15. Costs are tested at baseline,
  ×2 and ×3.
- **Protocol:** `validation@1`, frozen before any January-May candle was imported. It uses the Phase 5.2 thresholds
  with explicit five-month rules (4 of 5 months). Results are reported separately; Phase 5.2 classifications do not
  change.
- **Afterwards:** only convincing candidates go on to longer, read-only paper-monitor observation. No orders, and no
  change to Phase 4.

## Phase 5 strategy search: closed (15 November 2026)

- **Phase 5.3 frozen** by `var/monthly/reports/phase5-research-cycle-closed.json`. Its report, protocol, manifests,
  fingerprints, results and classifications are recorded by hash and will not be revised. No further validation@1
  run is accepted.
- **Candidates retired from promotion:** R2 v2, R5 v2 and R7 v2 (rejected) and R1 v2 (inconclusive, not promising).
  None goes to paper-monitor observation.
- **Phase 4 is preserved independently:** A v2, B v2 and C v4 keep their monitoring and read-only safeguards. No
  research version is activated.
- **What the cycle leaves behind:** a pipeline that keeps failed experiments, benchmarks redesigns against the
  originals, freezes criteria before results and validates out of sample. It kept a weak candidate from being
  promoted on a limited sample.
- **Next (not started):** a regime-dependence study, asking why performance changes across market regimes and how
  the methodology can detect this earlier. It is not another round of strategy variants. Any new hypothesis is
  tested on fresh data outside the spent range (2 Nov 2025 to 13 Nov 2026).

---

## Phase 6: New strategy discovery

**Status: concluded (simulation only). No hypothesis held up; nothing is promoted.** See
[PHASE_6_PROTOCOL.md](PHASE_6_PROTOCOL.md), [PHASE_6_H64_REGISTRATION.md](PHASE_6_H64_REGISTRATION.md) and
PHASE5_ARCHITECTURE.md §16.
- **Outcome:**
  - on development data, H6.1 was not supported as predicted, H6.2 was rejected and H6.3 was inconclusive; none went
    to the sealed data;
  - H6.4 (VOL-C outside contracting M15 volatility, registered after development with its origin disclosed) ran once
    on the sealed November 2023 – October 2025 block and was **rejected**: −0.006 R/trade over 663 M5 trades, 11 of 24
    months positive, negative at ×2 costs and on M15 (`var/monthly/reports/h64-report.md`);
  - the development edge of plain continuation (+0.102 R/trade) did not appear on the sealed months (−0.029 R/trade).
- **Research library:** Monthly Backtest step 5 compares every Phase 5 and Phase 6 study (PHASE5_ARCHITECTURE.md §16.2).
- **Goal:** test three genuinely different market hypotheses, not make earlier strategies profitable:
  - H6.1: does the volatility regime decide which behaviour works?
  - H6.2: does a higher-timeframe change of character add information?
  - H6.3: do session breaks behave differently, and persistently?
- **Process:**
  - the hypotheses, rules and predictions were written before implementation;
  - simple baselines run first;
  - at most 3 tool-built refinements per hypothesis, each with a written rationale;
  - a hypothesis is frozen only if its prediction holds on development data;
  - it is then evaluated once on the sealed data, and rejection is accepted.
- **Data:**
  - development uses January–13 November 2026 (already spent);
  - the **sealed** block is November 2023–October 2025 (24 months), which the code refuses to read during development;
  - **forward** data from 14 November 2026 is reserved for final confirmation.

---

## Phase 3: Market structure (M)

> **After Phase 1:** a basic `structure@1` (swings, BOS/CHoCH, no look-ahead, golden and mutation-tested) is live on the dashboard. What remains: richer UI (labels, a swing-strength control) and the full golden-case set.

**Deliverables**
- `analysis/swings.py` (`swings@1`) and `structure.py` (`structure@1`): BOS, CHoCH, sweeps and trend state.
- `GET /api/analysis/structure`.
- Chart markers for swings, BOS and CHoCH, with labels.
- Right panel: current trend state, last BOS and CHoCH, and the defending swing.

**Acceptance criteria**
- Golden hand-built candle cases for each rule, including ties, wick-only breaks and a trend flip.
- **Truncation invariance** holds on 10 000 synthetic bars for every timeframe.
- Every result carries `confirmed_at` and the definition version.

**Reuse:** the `scans` as-of pattern; `breadth._lag()` and `_trailing()` (R14).

---

## Phase 4: Liquidity, fair value gaps and order blocks (M)

**Deliverables**
- `liquidity@1`:
  - equal highs and lows (ATR tolerance);
  - previous day and week highs and lows on the 17:00 New York day;
  - Asian range high and low;
  - round numbers;
  - sweeps.
- `fvg@1`, with mitigation states and invalidation.
- `ob@1`, tied to displacement plus BOS. It becomes known only at the BOS bar.
- API routes for all three.
- Chart primitives: zones as rectangles and levels as rays. They are toggleable, do not autoscale, and show their version on hover.

**Acceptance criteria**
- Golden cases for each detector.
- The order-block look-ahead test passes: no order block is visible before its BOS bar closes.
- A test proves overlays never change the price scale.

**Depends on:** Phase 3 (swings and BOS).

---

## Phase 5: Session and volatility analysis (M)

> **After Phase 1:** the session panel (`sessions@1`, DST-correct) and ATR, ADR and realized volatility (`vol@1`) are live. What remains: session bands on the chart, the hour-of-day heatmap and the macro-event registry.

**Deliverables**
- `analysis/sessions.py`: zone-aware Asian, London and New York windows, their overlaps, and per-session OHLC, range, time of high and low, sweep of the prior session, and range versus ATR.
- `analysis/volatility.py`:
  - ATR(14) Wilder and ADR(20);
  - realized volatility per session and day;
  - hour-of-day median range profile;
  - volatility regime percentiles;
  - range boxes and compression (R15).
- `/api/sessions` and `/api/sessions/stats`.
- `/api/volatility`.
- Panels: session statistics table and an hour-of-day heatmap.
- **Macro-event registry** (`event_risk` PATTERN): a manual, versioned JSON file with UTC instants. It is used to label bars and to filter research.

**Acceptance criteria**
- Session boundaries are correct in the weeks when the UK and US change clocks on different dates.
- Session statistics reconcile with the underlying candles.
- Statistics are labelled "historical observation".

---

## Phase 6: Paper-trading simulator (L)

> **After Phase 1:** the lot-based account, market orders with SL/TP, costs, the ledger and equity snapshots are done, all on current prices. What remains: pending orders, M1 drill-down fill ambiguity and **replay mode** on historical bars.

**Goal:** paper trading in replay mode on historical data. No connection to any broker.

**Deliverables**
- `sim/orders.py`: market, limit, stop and stop-limit orders, plus SL/TP brackets.
- `sim/costs.py`: fixed spread or a per-hour spread profile, per-bar spread when present, and slippage.
- `sim/fill_model.py`, implementing the §6 rules, with M1 drill-down and the `ambiguous` flag (R1, R2).
- `sim/account.py`: balance, equity, margin, and risk-based lot sizing (R8, R9).
- `sim/replay.py`: reveals bars one at a time; orders fill only on later bars.
- Paper API routes.
- UI:
  - a ticket with risk-based sizing preview;
  - positions and orders tabs;
  - a replay toolbar (step, play, speed);
  - the permanent "SIMULATED — no real orders" strip.

**Acceptance criteria**
- A scenario table covers every fill rule, long and short:
  - gap through a stop or limit;
  - stop-limit with no fill;
  - SL and TP hit in the same bar;
  - entry and SL hit in the same bar;
  - a pending order across the weekend gap.
- The conservation identity holds after every step: balance + open P&L − costs = equity.
- Ambiguous fills are counted and shown.

**Tests:** scenario table, property tests on random synthetic paths, and an API flow test.

---

## Phase 7: Trade journal and performance analytics (M)

**Deliverables**
- Journal create/read/update/delete with tags. Each entry can link to a paper trade and records session, setup, thesis, review and rating.
- `journal/analytics.py` (R10, R11, R13):
  - win rate, average R and median R;
  - expectancy, profit factor and maximum drawdown in cents and in R;
  - streaks and MAE/MFE;
  - breakdowns by session, setup, tag, weekday and hour.
- Rates are hidden below a minimum sample size.
- Analytics UI: equity curve, R histogram, MAE/MFE scatter and breakdown tables.
- JSON export and preview-then-restore for backups (R19, R20).

**Acceptance criteria**
- Analytics recompute exactly from stored fills.
- An unknown value stays null and is never treated as 0.
- A trade that has not fully closed has no R.
- A backup round trip reproduces the same data, verified by fingerprint.

---

## Phase 8: Historical pattern detection and evaluation (L)

**Deliverables**
- `research/patterns.py`: a registry of patterns, each with a `name@version`, parameters, and entry, stop, target and time-exit rules.
- Two or three starter patterns, for example "Asian range sweep and London reversal" and "FVG retest after BOS".
- `research/events.py`: as-of detection, where every input has `confirmed_at ≤ signal_at`.
- `research/evaluate.py`: runs **only through `sim.fill_model`**, with an explicit cost model.
- `research/stats.py` (R12):
  - Wilson and bootstrap confidence intervals;
  - in-sample/out-of-sample split by date;
  - a gate for small samples;
  - a count of parameter variants tried, with a multiple-testing warning.
- Evaluations run as background jobs and are stored (§8).
- Research UI: summary with CIs, an event list that jumps to the chart, and breakdowns.

**Acceptance criteria**
- A spy test proves that evaluation uses `fill_model`.
- No event uses information from after `signal_at`.
- In-sample and out-of-sample periods never overlap.
- Results are labelled "historical simulation, not a forecast".

**Depends on:** Phases 3–6.

---

## Phase 9: Current market data and forward paper trading (M, optional)

> **After Phase 1:** mostly done. The read-only providers, LIVE/DELAYED/HISTORICAL/DISCONNECTED states, SSE to the browser and user-placed forward paper orders are all in place. What remains: live verification of Tiingo with a key.

**Deliverables**
- One read-only current or delayed `CandleSource`, either an HTTP vendor or later MT5 point D. It includes:
  - the error taxonomy (R7);
  - timeouts (R6);
  - a quota ledger (R22);
  - quote age and spread labelling (R23).
- Forward paper mode: the **user** places orders on `kind: "current"` bars. Nothing places orders automatically.
- A WebSocket or SSE stream with a sequence guard on the client.

**Acceptance criteria**
- Current data always shows its source, `as_of` and delay.
- A provider failure degrades to historical mode with a visible notice.
- Credentials never reach the browser or the logs.

**Depends on:** Phase 6 and open decision §15.2.

---

## Phase 10: Hardening and deployment (M)

**Deliverables**
- Docker image verified on a machine with Docker installed: non-root, read-only application files, a writable `/data` volume only, and a `HEALTHCHECK`.
- `docker-compose.yml` bound to `127.0.0.1`.
- Optional `GOLDSCALP_AUTH_TOKEN` for any non-local exposure.
- Scheduled SQLite backup using the `sqlite3` backup API.
- Performance check with ten years of M1 data (about 3.6 M rows): candle range queries return in under 300 ms, and analysis results are cached by fingerprint.
- Accessibility pass: keyboard-only use, contrast, reduced motion.
- Full Playwright smoke in CI.

**Acceptance criteria**
- `docker compose up` on a clean machine serves the app.
- The health check passes.
- Data survives a container restart.

---

## Phase M: MetaTrader 5 read-only integration (optional, independent track)

This phase can start any time after Phase 1. Each step is separately optional. None of
them adds MT5 to the core install, Docker image or CI (§14).

| Step | Integration (§14.2) | Deliverables | Acceptance criteria |
|------|---------------------|--------------|---------------------|
| M1 | **A**: terminal bar export | *(delivered in Phase 1)* the `mt5_export` dialect and a broker profile (symbol alias, server-time rule, digits, contract size) | A real export from your broker imports cleanly, and the schedule-fit check passes |
| M2 | **B**: MQL5 export script | `integrations/mt5/GoldScalpExport.mq5`: a script, not an EA; writes the `mt5_export` format plus a metadata header; README with install steps | The source-scan test finds no trade functions; the output imports through `ingest` |
| M3 | **C**: Python adapter | `providers/mt5_terminal.py` behind an allowlist proxy; `goldscalp[mt5]` extra with a Windows-only marker; `python -m goldscalp.tools.mt5_pull` running on the Windows host | Tests with a fake `MetaTrader5` module pass on Linux CI; a call to `order_send` raises; no credentials are stored; `account_info` is never called |
| M4 | **E**: show analysis in MT5 | Zone and level CSV export endpoint, plus an MQL5 indicator that draws objects from the file | Display only; the source scan stays clean |
| M5 | **D**: MT5 prices for forward paper trading | A current-data source that polls ticks; labelled `current` | Same criteria as Phase 9; orders remain local simulations |

**Never in scope:** a real-money order path, trading Expert Advisors, or storing MT5 credentials
(§14.3). The read-only market-data adapter never places orders, manages positions or reads account
information.

**The one exception (added 14 November 2026, outside this track):** the optional **MT5 demo-account
execution** feature (`goldscalp/execution/`, migration `0007_demo_execution.sql`, `/api/live/demo`, the
*Demo Execution* tab on `/live`). It is off unless `GOLDSCALP_DEMO_EXECUTION_ENABLED=true`. When switched on
and linked per strategy version, it sends market orders, closes its own positions and reads `account_info()`,
but only for an account that reports itself as DEMO, re-checked before every order; a real-money account is
always refused. It still never handles MT5 credentials. See README, "MT5 demo-account execution".

---

## Dependency overview

```
Phase 0 ─▶ 1 ─▶ 2 ─▶ 3 ─▶ 4 ─┐
              │         └──▶ 5 ─┼─▶ 6 ─▶ 7
              │                 │    └─▶ 8 (needs 3–6)
              │                 │    └─▶ 9 ─▶ 10
              └─▶ M (optional, independent; M1 ships with Phase 1)
```

Phases 3 and 5 can proceed in parallel once Phase 2 is in place. Phase 4 needs Phase 3.

## Suggested next increment

1. Evaluation robustness: walk-forward and out-of-sample splits, confidence intervals for the
   favourable/adverse rates, and comparison against a no-signal baseline.
2. Phase 6 remainder: paper trading inside a replay (simulated fills at replay prices); pending orders.
3. Verify Tiingo with your free key: set `GOLDSCALP_TIINGO_API_KEY`, confirm the `xauusd` ticker and the LIVE state.
