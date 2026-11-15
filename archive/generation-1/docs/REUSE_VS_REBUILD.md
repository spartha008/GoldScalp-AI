# Reuse vs Rebuild

The decision for each part of SpicyStock: carry it into GoldScalp AI, or leave it.
The evidence is in [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md); where the reused
parts land is in [PROPOSED_ARCHITECTURE.md](PROPOSED_ARCHITECTURE.md).

## 1. Decision

**Clean rebuild. No SpicyStock module is copied whole.** About 25 functions are
adapted (roughly 600–900 lines), and a set of engineering patterns is carried over as
design rules.

Why the rebuild is the right call:

1. **Domain.** All 46 `src` modules assume a US-equity problem: XNYS sessions, a
   ticker universe, share volume, whole shares, long-only trades, Stockbee rules,
   Alpaca stock endpoints, SEC filings and Fidelity order types. None of these fit
   XAUUSD (audit §10).
2. **Coupling.**
   - `sessions` (the XNYS calendar) is imported by 19 modules.
   - `pipeline` imports 34 modules.
   - 17 modules form one import cycle.
   
   Moving any module across would drag most of the cycle with it (audit §3.2).
3. **Output model.** SpicyStock is a nightly batch job whose system of record is
   JSON committed to git and published to GitHub Pages. GoldScalp is an interactive
   FastAPI and SQLite application. The publication, provenance and evidence chain,
   about 30% of the code, has nothing to attach to.
4. **Frontend.** The user asked for a new design, and the SpicyStock UI is a
   5,930-line single closure on a shared global. Only its ideas transfer.

### Verdict legend
| Verdict | Meaning |
|---------|---------|
| **REUSE** | Copy the named function almost unchanged, with a provenance comment citing the SpicyStock source. |
| **ADAPT** | Copy the named function and change it as described. |
| **PATTERN** | Copy no code; reimplement the idea, which is named. |
| **DISCARD** | Nothing is carried over. |

Every copied function gets a header comment:
`# Adapted from SpicyStock src/<module>.py <function>() @ 96ef4732`.

---

## 2. Module-by-module verdicts (`src/`, 46 modules, 23,903 lines)

| Module | Lines | Verdict | What is carried, or why not |
|--------|------:|---------|-----------------------------|
| `admission.py` | 242 | DISCARD | Admission rules for the Stockbee dollar scan. |
| `allocation.py` | 66 | PATTERN | `reservation()`: an unknown commitment reserves the full equity and never becomes free capacity. |
| `breadth.py` | 507 | DISCARD module; **REUSE** `_lag()` and `_trailing()` | Cross-sectional breadth needs thousands of stocks. The two NaN-strict rolling-window helpers are generic. |
| `charts.py` | 150 | DISCARD | mplfinance PNGs for an LLM. Idea kept: a missing bar is blanked, not spliced (P5). |
| `clock.py` | 43 | DISCARD | XNYS adapter that reads `SCAN_SESSION_DATE`. |
| `delivery.py` | 193 | DISCARD | Publication latency against ET cron slots. |
| `discovery.py` | 122 | DISCARD | LLM prompt contract and regex tripwire. |
| `earnings_risk.py` | 1,112 | DISCARD | SEC 8-K earnings estimation. |
| `event_risk.py` | 180 | PATTERN | A reviewed JSON registry with active intervals, where "no match is not clearance" and overdue reviews keep blocking. It becomes a **macro-event registry** (FOMC/CPI/NFP) with UTC instants, used for labelling and filtering research. |
| `followup.py` | 465 | DISCARD | Late-bar accounting. Idea kept: side computations never raise and return a `failed` block instead. |
| `funnel.py` | 346 | PATTERN | Each candidate gets exactly one terminal stage, and the counts must reconcile. This becomes the pattern-event funnel in research. |
| `grader.py` | 708 | DISCARD | Anthropic chart reader. No AI feature is in scope. If AI commentary is added later, revisit `_extract_json()`, `_reply_text()` and the refusal classification. |
| `history.py` | 273 | PATTERN | Bounded storage with oldest-first eviction, under both a count cap and a byte cap. |
| `input_diagnostics.py` | 360 | ADAPT `value_cell()`, `timestamp_cell()` | They classify null/NaN/±inf/invalid cells without converting arbitrary objects to strings. Used for import rejection samples. |
| `inputs.py` | 635 | PATTERN | Conservation equations (intended = requested = received + missing…). These become the candle coverage ledger. |
| `intraday.py` | 599 | **ADAPT** the logic of `resolve()` and `_disagrees()`; ADAPT `_write()`, `canonical()`, `name_of()` | The fill-ambiguity rules and the "finer bars must reproduce the coarse bar" check go into `sim.fill_model`. Atomic, content-addressed writes are used for exports and backups. |
| `issuer_evidence.py` | 1,478 | PATTERN | `_Budget` and `_Transport` (request, byte and time budgets; minimum request spacing; no redirects). **Never copy `USER_AGENT`**: it contains the owner's email. |
| `market_data.py` | 684 | **ADAPT** 4 functions; discard the Alpaca client | See §3, items 4–7. |
| `morning.py` | 688 | ADAPT `quote_observation()`, `price_state()` (phase 9 only) | Spread and staleness of a current quote; classifying a price against stop/trigger/limit bands. |
| `morning_halts.py` | 430 | DISCARD module; **ADAPT** `_eastern()` | Refuses local times that are nonexistent or ambiguous across DST. Used to convert MT5 server time and session time. |
| `morning_host.py` | 393 | PATTERN | Root launcher that drops to separate reader and writer UIDs. GoldScalp uses a simpler non-root `USER`. |
| `morning_host_wsgi.py` | 32 | DISCARD | Hardcodes the owner's Pages origin. |
| `morning_service.py` | 574 | **ADAPT** `Store._transaction()` and the read-only snapshot | `BEGIN IMMEDIATE`, roll back on any `BaseException`, always close; read-only through a `mode=ro` URI. The WSGI hardening (GET-only, `no-store`, `nosniff`) is PATTERN. |
| `morning_service_journal.py` | 254 | PATTERN | An *operations* event log: exact fields per event kind, canonical payload plus SHA, idempotent `event_key`. This informs GoldScalp's audit and event table, **not** the trade journal. |
| `morning_service_report.py` | 388 | PATTERN | Reports disclose their own gaps and return fixed refusal codes, never exception text. |
| `morning_service_worker.py` | 354 | ADAPT `Ledger.reserve()`/`finish()`, `_retry_after()`, `BoundedSession` (phase 9 or later) | Provider quota is charged before sending. 401/403 back off for the day; 429 honours Retry-After. Hardened HTTP client. |
| `pipeline.py` | 1,402 | PATTERN | `RunReport` (stage-tagged problems, exit codes 0–3) and `preflight()` (check every required setting before spending anything). These shape the data-import CLI. |
| `plan.py` | 1,464 | **ADAPT** `size()`, `Account`, `_price()`, `_number()`; PATTERN from `follow()` | See §3, items 8–9. Everything else (Fidelity tickets, PDT/GFV, day-3/day-5 exits) is DISCARD. |
| `provenance.py` | 817 | **REUSE** `_typed()` and `digest()`; ADAPT `write_objects()` | Lossless canonical digest (floats via `float.hex()`, None, False, 0 and 0.0 kept distinct). Used for `data_fingerprint`, parameter-set hashes and import receipts. The verifier is PATTERN. |
| `quality.py` | 994 | PATTERN | The `Check` status model (PASS / PARTIAL / FAIL / UNMEASURED, plus value, threshold and note). The 2LYNCH checks are DISCARD. |
| `quality_ledger.py` | 203 | PATTERN | Append-once: create with no overwrite, and refuse when existing bytes differ. In SQLite this is an insert-only table. |
| `reader.py` | 561 | ADAPT `_decode()` | Bounded JSON decode that refuses NaN/Infinity. The stage, validate, install sequence is PATTERN. |
| `reader_authority.py` | 230 | DISCARD | LLM downgrade validator. |
| `reader_coverage.py` | 400 | DISCARD | LLM read accounting. |
| `record.py` | 689 | **ADAPT** `r_multiple()`, `summarize_scorecard()`; PATTERN `load()`/`set_aside()`/`save()`, `fill()`'s `uncertain` status | See §3, items 10–11. The pick store, XNYS sequencing and SPY benchmark are DISCARD. |
| `report.py` | 1,481 | ADAPT `_sanitise()`, `rules_version()` | `_sanitise()` turns NaN/Inf into null (recording their paths) and numpy scalars into Python ones; Starlette's `JSONResponse` raises on NaN. `rules_version()` is folded into `digest()`. Email, Resend and `PAGE_URL` are DISCARD. |
| `research_observations.py` | 422 | DISCARD | One Alpaca IEX snapshot. |
| `research_outcomes.py` | 621 | PATTERN | The outcome vocabulary pending / missing / uncertain / not_filled / open / resolved. |
| `review_selection.py` | 206 | DISCARD | Idea kept: deterministic sampling via `sha256(policy|key)`. |
| `scans.py` | 475 | PATTERN | Pure `(df, at)` functions that see only bars up to `at` and return `None` when a value cannot be read. Every threshold is a named constant archived in `RULES`, and a test proves it (P2, P8). The Stockbee scans are DISCARD. |
| `sessions.py` | 179 | ADAPT `market_time()`; PATTERN | The single session authority is PATTERN. `market_time()` refuses naive instants and is adapted. XNYS is DISCARD. |
| `source_recovery.py` | 801 | DISCARD | Alpaca re-request. Idea kept: count every attempt, even ones that time out. |
| `stop_research.py` | 517 | ADAPT `_decode()`, `_safe_read()` | Refuses duplicate JSON keys and NaN; refuses symlinks and `..` paths. Used to restore JSON backups. Hardcodes owner account values; do not copy those. |
| `timing.py` | 166 | ADAPT `_instant()`; PATTERN | `_instant()` refuses offset-less ISO strings. The phase enum includes `unknown`, which means "research only, never permission". |
| `universe.py` | 571 | DISCARD | Ticker universe. **Spoofs a Chrome User-Agent**; do not repeat that. |
| `watchlist.py` | 428 | ADAPT `_range_pcts()`, `_box()`, the compression ratio from `_stage_c()` | Range percentages, consolidation boxes and volatility contraction. They need per-timeframe thresholds, prices in points, and decoupling from `scans.anticipation`. |

**Tally (46 modules):**
- REUSE whole module: **0**
- Modules contributing REUSE or ADAPT functions: **17**
- PATTERN only: **14**
- DISCARD outright: **15**

---

## 3. Function-level carry list

Ordered by value to GoldScalp. "Target" is the module in the proposed layout.

| # | Source | Verdict | Target | Required changes |
|---|--------|---------|--------|------------------|
| 1 | `intraday.resolve()` and `_disagrees()` (logic) | ADAPT | `sim/fill_model.py` | Rewrite for long **and** short, all order types, bid/ask with spread, and any session (no 30-minute window). Keep the rules: fill at `max(trigger, open)`; gap past the limit means no fill or `ambiguous`; entry and stop in one bar means `ambiguous`; finer bars are used only if they reproduce the coarse bar. |
| 2 | `plan.follow()` (intrabar order) | PATTERN | `sim/fill_model.py` | When the order inside a bar is unknown, the stop wins. Generalize to SL/TP for both directions and record `ambiguous=True`. |
| 3 | `provenance._typed()`, `provenance.digest()` | REUSE | `core/fingerprint.py` | Drop the numpy-only imports or keep them; nothing else changes. |
| 4 | `market_data.readable_pair()` | ADAPT | `data/candles.py` `validate()` | Vectorize over the whole frame instead of the last two rows; integer ticks; tick volume ≥ 0 and nullable. |
| 5 | Duplicate handling in `market_data._download_batch()` | ADAPT | `data/candles.py` `normalize()` | `sort_index(kind="stable")`, then `~index.duplicated(keep="last")`, and count what was dropped. |
| 6 | `market_data._MarketDataTimeoutAdapter` | ADAPT (phase 9) | `data/providers/http.py` | Mount explicit connect and read timeouts; or use httpx's own timeouts. |
| 7 | `market_data._is_permanent_refusal()`, `_refusal_error()` | ADAPT | `data/providers/base.py` errors | Map to `CredentialsRejected`, `NotEntitled`, `SourceUnavailable`, `MalformedSource`. |
| 8 | `plan.size()`, `plan.Account`, `_price()`, `_number()` | ADAPT | `sim/account.py` | Two-sided (|entry − stop|); lots rounded **down** to a 0.01 step with a minimum lot; contract oz from the instrument; integer ticks and cents throughout; margin check instead of a share cap. |
| 9 | `allocation.reservation()` (rule) | PATTERN | `sim/account.py` | An unknown or unreadable position reserves the full margin. |
| 10 | `record.r_multiple()` | ADAPT | `journal/analytics.py` | Short side; partial exits weighted by lots; costs included; still `None` until the position is fully closed. |
| 11 | `record.summarize_scorecard()` | ADAPT | `journal/analytics.py`, `research/stats.py` | Keep exclusive buckets, keep rates hidden below a minimum n, keep `math.fsum`. Add profit factor, MFE/MAE, drawdown and Wilson/bootstrap CIs (none of which SpicyStock has). |
| 12 | `tools/signal_outcomes.py` `_cost_r()`, `bootstrap_nights()` | ADAPT | `research/stats.py` | Costs expressed in R; seeded bootstrap resampling by day or session instead of "night". |
| 13 | `tools/evidence_review.py` `drawdown()` | ADAPT | `journal/analytics.py` | Equity in cents as well as cumulative R. |
| 14 | `breadth._lag()`, `breadth._trailing()` | REUSE | `analysis/_windows.py` | None. A NaN anywhere in the window gives NaN, never a shorter window. |
| 15 | `watchlist._range_pcts()`, `_box()`, compression ratio | ADAPT | `analysis/volatility.py` | Bars instead of sessions; per-timeframe thresholds; true ATR (SpicyStock's "ADR" omits the gap term). |
| 16 | `morning_service.Store._transaction()` and the `mode=ro` snapshot | ADAPT | `storage/db.py` | Add the WAL, `foreign_keys` and `busy_timeout` PRAGMAs; add a migrations table. |
| 17 | `morning_halts._eastern()` | ADAPT | `core/clock.py` `local_to_utc()` | Parameterize the zone (`America/New_York`, `Europe/London`, an MT5 server rule). |
| 18 | `report._sanitise()` | ADAPT | `api/schemas.py` | NaN/Inf to null, with paths recorded; numpy scalars to Python. |
| 19 | `stop_research._decode()`, `reader._decode()`, `stop_research._safe_read()` | ADAPT | `storage/files.py` | Bounded JSON that refuses duplicate keys, NaN and symlinks; used for backup restore. |
| 20 | `intraday._write()` (or `report.write()` with fsync) | ADAPT | `storage/files.py` `atomic_write()` | mkstemp in the same directory, fsync, `os.replace`, then read back. |
| 21 | `input_diagnostics.value_cell()`, `timestamp_cell()` | ADAPT | `data/ingest.py` | Classify rejected CSV cells for import reports. |
| 22 | `morning_service_worker.Ledger`, `_retry_after()`, `BoundedSession` | ADAPT (phase 9) | `data/providers/http.py` | Decouple from `Store` and `Claim`; per-provider budget table. |
| 23 | `morning.quote_observation()`, `price_state()` | ADAPT (phase 9) | `data/current.py` | Bid/ask spread and quote age for current-data labelling. |
| 24 | `sessions.market_time()`, `timing._instant()` | ADAPT | `core/clock.py` | Refuse naive datetimes and offset-less ISO strings. |
| 25 | `tests/conftest.py` `_no_network`, `_isolated_cwd`, `seed`; `tests/synthetic.py` `seed_for()`, `frame_digest()`, the OHLC envelope | ADAPT | `tests/conftest.py`, `data/providers/synthetic.py` | Intraday 24×5 index with a daily break, gold-scale prices, tick volume and spread, planted FVG/sweep/BOS shapes. |

---

## 4. Patterns carried over as design rules

These match principles P1–P9 in PROPOSED_ARCHITECTURE.md §1.

| Pattern | SpicyStock exemplar | GoldScalp rule |
|---------|---------------------|----------------|
| Refuse unknown input; never fall back silently | `market_data.feed_from_env()`, `read_symbol_file()` | P6: config, timeframe, CSV and timestamps are strict. |
| "Uncertain" is an honest outcome, with a reason | `record.fill()`, `intraday.resolve()` | P4: ambiguous fills are flagged and counted. |
| Rates hidden below a minimum sample | `record.SCORECARD_MIN_PLANS` | Research and journal statistics are gated by n. |
| Every threshold a named constant, archived and hashed | `scans.RULES`, `report.rules_version()`; AST literal guard in `tests/test_plan.py` | P8: `name@version` plus a params hash on every result. |
| As-of slicing, no look-ahead | `scans._Bars.window()`, `tools/historical_backtest.as_of()` | P2: `confirmed_at` plus a truncation-invariance property test. |
| A gap is a gap | `charts.render_chart()` blanking, `market_data.drop_gapped()` | P5: expected vs unexpected gaps; no splicing. |
| Atomic, never-overwrite writes; corrupt files set aside | `intraday.store()`, `quality_ledger.append()`, `record.set_aside()` | File exports and backups; DB transactions. |
| Conservation checks | `inputs.faults()`, `intraday.faults()` | Import report: read = stored + rejected + duplicates. Account: balance + open P&L − costs = equity. |
| Separate data freshness from "may act" | `docs/app.js` `availability()`, `timing.phase()` | P1 kind badges; current data shows age and delay. |
| Tests that cannot touch the network or the tree | `tests/conftest.py` `_no_network`, `_isolated_cwd`; CI dirty-tree check | Same, from the first commit. |
| Fixtures produced by real code | `tools/make_fixture.py --check` | Synthetic fixtures generated by `SyntheticSource`, checked in CI. |
| Prove a test can fail | CLAUDE.md standing rule; `continuity_check.mjs --baseline` | Contribution rule in GoldScalp's README. |
| Read-only collect, then write-only persist (CI) | `morning.yml` job split | Applies if GoldScalp ever runs scheduled jobs in CI. |
| Narrow secret-scan allowlists, proven with canaries | `.gitleaks.toml`, `tests/secret_scan_controls.py` | gitleaks with default rules; allowlists only if needed. |

---

## 5. Explicitly not carried over

| Item | Reason |
|------|--------|
| Stockbee method (`scans`, `quality`, `watchlist` rules, `breadth`, `admission`, `knowledge/*.md`) | Different strategy domain. |
| Alpaca stock client, SIP hold-back, split adjustment, Nasdaq universe, seed tickers | Do not apply to XAUUSD. |
| `exchange_calendars` / XNYS | A 24×5 metal schedule is modeled directly. |
| Anthropic grader and its guardrails | No AI feature is in scope. |
| Resend email | Not in scope. |
| GitHub Pages publication, `docs/*.json` data store, evidence/history archives, receipts | The system of record is SQLite behind an API. |
| Railway host, leases and fencing | Single local process; SQLite with a single writer. |
| SEC/EDGAR, earnings and issuer evidence | Not applicable. |
| Fidelity tickets, PDT/GFV/T+1, whole-share feasibility | Not applicable; no broker execution at all. |
| `CLAUDE.md`, `PRODUCT_GOAL.md`, release evidence | Owner-specific, very large, standing authorizations. |
| Any identity: owner email, Pages origin, Railway URL, repo slug, account size | Privacy (audit §8). |
| Git history | The new repository starts clean. |

---

## 6. Frontend: learn from, do not reuse

| SpicyStock element | Verdict | Use in GoldScalp |
|--------------------|---------|------------------|
| `app.js` (5,930 lines) | DISCARD | Learn `availability()`, the `requestPublication()` deadline and sequence guards, the `applyUpdate()` named outcomes, and `repaint()` focus preservation. |
| `app-chart.js` SVG chart | DISCARD the rendering | Learn the host `update`/`dispose` contract, `liveCharts()` leak counter, `chartPanel({idPrefix})`, `ariaLabel()` and the table twin, and that a highlight never moves the scale. |
| `app-reader.js` | PATTERN | Deadline, byte cap and fatal decoding for large history pulls. Server-side versioning replaces client SHA checks. |
| `app-handoff.js` journal | PATTERN | Semantics: null ≠ 0, "incomplete with reasons", integer money, reconciliation warnings, preview-then-restore. The journal itself moves server-side into SQLite. |
| `app-follow.js` store | PATTERN | Back up before migrating; never touch a future-version payload. |
| `design-system/sc.css` | PATTERN | Three token layers and documented contrast. The palette and fonts are replaced with black and gold, and fonts are self-hosted. |
| `design-system/sc-charts.js` `SC.el()` | PATTERN | One DOM-builder helper over `textContent`; no `innerHTML`. |
| `tools/page_smoke.mjs`, `chart_check.mjs` | PATTERN | Playwright with a pinned clock and injected data at 1280/390/320 px; readiness attributes; console-error collection. |

---

## 7. Dependency carry-over

| Keep | Drop | Add |
|------|------|-----|
| `pandas` 3.x, `numpy` 2.x, `pytest`; the bounded-plus-constraints pinning scheme | `alpaca-py`, `exchange_calendars`, `anthropic`, `resend`, `mplfinance`/`matplotlib`, `gunicorn`, `PyYAML` (unless CI tests need it), `httpx2`/`httpcore2` (unverified names) | `fastapi`, `uvicorn`, `pydantic` v2, `pydantic-settings`, `httpx` (TestClient and optional providers), `ruff`; front-end: a vendored, pinned `lightweight-charts` standalone build; optional later: `MetaTrader5` as a Windows-only extra (PROPOSED_ARCHITECTURE.md §14) |

---

## 8. Phase 1: what was actually reused (13 November 2026)

Each reused function carries an "Adapted from SpicyStock ..." note at its new location.

| Carry item | Now lives in |
|------------|--------------|
| R3 `provenance._typed()` / `digest()` | `goldscalp/core/fingerprint.py` (Decimal support added) |
| R4 `market_data.readable_pair()` (vectorised) | `goldscalp/data/validation.py` |
| R5 stable sort, then keep the last duplicate | `goldscalp/data/normalization.py` `normalize()` |
| R7 permanent vs transient refusal taxonomy | `goldscalp/data/providers/base.py` |
| R16 `Store._transaction()` and `mode=ro` reads | `goldscalp/storage/db.py` (WAL, foreign keys, migrations added) |
| R17 `morning_halts._eastern()` | `goldscalp/core/clock.py` `local_to_utc()` (now separates "nonexistent" from "ambiguous") |
| R18 `report._sanitise()` | `goldscalp/api/services.py` `clean()` |
| R22 provider budget, charged before sending | `goldscalp/data/live.py` `RequestBudget` |
| R24 refusing naive datetimes and offset-less ISO | `goldscalp/core/clock.py` |
| R25 autouse socket blocker | `tests/conftest.py` |
| `allocation.reservation()` principle | an unknown position mark gives unknown equity, never an assumed value (`simulation/portfolio.py`) |
| Frontend lessons (`el()`, deadlines, sequence guards, chart lifecycle, table twin, kind badges) | `web/js/*.js` (rewritten, not copied) |

The fill-ambiguity logic (R1/R2) is **not used yet**: Phase 1 fills market orders on current
quotes, where no intrabar ambiguity arises. It becomes relevant with replay mode (Phase 6).
