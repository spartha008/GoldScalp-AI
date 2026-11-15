# SpicyStock Architecture Audit

Audit of `C:\Spicy\SpicyStock` as the possible starting point for GoldScalp AI.
Audit date: 13 November 2026. Repository head: `96ef4732` ("Publish bound source
recovery"), with 1,031 commits.

Companion documents: [REUSE_VS_REBUILD.md](REUSE_VS_REBUILD.md) ·
[PROPOSED_ARCHITECTURE.md](PROPOSED_ARCHITECTURE.md) ·
[IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md)

---

## 0. Scope and method

- **Read-only.** No file in SpicyStock was created, modified or deleted. No tests,
  pipeline runs, package installs or network calls were made against it. Only
  read-only git commands were used (`log`, `ls-files`, `count-objects`).
- **Evidence standard.** Classifications rest on function bodies, not filenames or
  docstrings. The repository's own working notes say to "treat the code as evidence and
  the prose as suspect"; this audit applies that rule to the repository itself.
- **Coverage.**
  - **Read directly:** `market_data`, `sessions`, `clock`, `timing`, `charts`, `scans`,
    `allocation`, `intraday`, plus the dependency, Docker, environment and gitignore
    files.
  - **Covered by four parallel read-only reviews:** the remaining 38 `src` modules, the
    frontend, the tests/tools/CI, and a security sweep.
  - **Spot-checked against the source:** every claim from those reviews that this
    document relies on (identities, SQLite handling, digest/sizing/R-multiple bodies,
    frontend DOM safety, CI permissions, sizes).
  - **Not checked:** see §11.
- **Privacy.** The owner's personal email addresses, account sizes, broker account types
  and personal handles appear in SpicyStock. This audit describes them by type and
  location only, and does not reproduce them.

---

## 1. Executive summary

SpicyStock is a **single-owner, US-equity, end-of-day screening and trade-planning
pipeline**. It implements Pradeep Bonde's ("Stockbee") momentum-burst method.

- **Nightly run.** A GitHub Actions job:
  - downloads daily bars for about 4,800 US stocks from Alpaca;
  - runs the Stockbee scans;
  - asks an Anthropic model to read chart PNGs;
  - sizes whole-share stop-limit tickets for a real broker account;
  - commits the results as JSON into its own `docs/` folder for a GitHub Pages site;
  - emails a digest through Resend.
- **Morning service.** A Railway container serves pre-open observations from SQLite.

**Conclusion: a clean rebuild is the right approach, and the code supports it.**

- **No `src` module is reusable as a whole.** All 46 are bound to one or more of: the
  NYSE (XNYS) calendar, a stock-ticker universe, share volume, whole shares, split
  adjustment, Stockbee rules, Alpaca's stock endpoints, SEC filings, Fidelity order
  semantics, or the GitHub-Pages publication chain.
- **About 25 individual functions** (roughly 600–900 lines) are worth adapting. They are
  listed in [REUSE_VS_REBUILD.md](REUSE_VS_REBUILD.md).
- **The engineering discipline is the most valuable asset.**
  - Refuse rather than guess.
  - Treat "uncertain" as an honest outcome.
  - Keep every threshold as a named constant.
  - Write atomically.
  - Block the network in tests.
  - Prove that each test can fail.
- **The fill-ambiguity logic is directly relevant to a paper-trading simulator.** It
  resolves daily-bar uncertainty with minute bars and says "uncertain" when even those
  cannot decide (`intraday.resolve()`).

---

## 2. Repository inventory

| Path | Contents | Size / count |
|------|----------|--------------|
| `src/` | 46 Python modules (the application) | 23,903 lines |
| `tests/` | 121 `test_*.py` files plus `conftest.py`, `fakes.py`, `synthetic.py` | 55,198 lines; 2,911 `def test_`; fixtures 20 MB / 815 files |
| `tools/` | 59 Python tools (backtests, studies, publishers, auditors) and about 50 Node/Playwright harnesses | about 38k lines |
| `docs/` | GitHub Pages site (HTML/CSS/JS) **and** the published data store (JSON, gzip evidence, history) | 379 MB; most of the 27,079 tracked files |
| `knowledge/` | Method and strategy notes (Stockbee), Fidelity broker note, `event-risk.json` registry | 88 KB |
| `release-evidence/` | Dated release receipts (JSON/HTML captures) | 5.9 MB, 101 files |
| `data/symbols.txt` | Seed universe of US tickers | 322 lines |
| `.github/workflows/` | 9 workflows | 1,182 lines |
| `README.md` / `CLAUDE.md` / `PRODUCT_GOAL.md` | Prose documentation and agent working notes | 207 KB / **1.05 MB** / 44 KB |
| `Dockerfile`, `requirements*.txt`, `constraints.txt` | Railway morning-host image; pinned dependency sets | n/a |
| `.git/` | History | 310 MB pack, about 63.5k objects |

---

## 3. Architecture

### 3.1 Runtime topology
```
GitHub Actions (cron: 4 evening slots + morning/issuer/source-recovery schedules)
  └─ python -m src.pipeline evening
       ├─ universe.build()            Nasdaq directory → ~4.8k US common stocks
       ├─ market_data.download_bars() Alpaca StockHistoricalDataClient, daily, SIP, split-adjusted
       ├─ scans / quality / watchlist / breadth   Stockbee scans, 2LYNCH checklist, market regime
       ├─ charts.render_chart() → grader.grade_all()   mplfinance PNG → Anthropic Messages API (≤12 reads)
       ├─ plan / allocation / event_risk / earnings_risk   whole-share Fidelity tickets, SEC 8-K checks
       ├─ record / intraday           picks.json, outcome walk, minute-bar fill resolution
       ├─ provenance / reader / history / quality_ledger   digests, browser projection, archives
       └─ report.write() + report.send_digest()   docs/data.json + Resend email
  └─ git commit + push to main  ──▶  publish-dashboard.yml  ──▶  GitHub Pages (docs/)

Railway container (Dockerfile → src.morning_host, root launcher)
  ├─ gunicorn WSGI reader (uid 10002)  /morning.json /status.json /healthz  (CORS: owner's Pages origin)
  ├─ morning_service_worker (uid 10001) Alpaca IEX snapshots, Nasdaq halt RSS
  └─ SQLite on /srv/spicystock volume

Browser: static docs/index.html + app*.js reads the JSON files; the journal lives in localStorage.
```

### 3.2 Module structure (computed from the import statements)

**Hub module.** `pipeline.py` imports **34 of the other 45** modules.

**Cycle.** A **17-module strongly connected component** spans `earnings_risk`, `funnel`,
`grader`, `issuer_evidence`, `morning`, `pipeline`, `provenance`, `quality`,
`quality_ledger`, `reader`, `reader_authority`, `reader_coverage`, `record`, `report`,
`research_outcomes`, `review_selection` and `stop_research`. It is held together mostly
by imports inside function bodies. For example, `provenance` imports `pipeline`,
`report` and `reader` lazily, and `record` imports `provenance`.

**Most-imported modules.**

| Module | Imported by |
|--------|-------------|
| `sessions` (the XNYS calendar) | 19 modules |
| `plan` | 11 |
| `provenance` | 9 |
| `morning` | 9 |
| `market_data` | 9 |

**Modules with no internal imports:** `admission`, `charts`, `history`, `morning_halts`,
`morning_host`, `scans`, `sessions`, `universe`. They are self-contained, but every one
of them is stock-specific.

**Consequence.** Lifting any module out of the cycle pulls in most of the cycle.
Lifting anything that depends on `sessions` pulls in the NYSE calendar. In practice,
only individual functions can be extracted.

### 3.3 Functional groups

| Group | Modules | Lines | Domain binding |
|-------|---------|------:|----------------|
| Market data and inputs | `market_data`, `universe`, `inputs`, `input_diagnostics`, `intraday`, `followup`, `source_recovery` | 4,115 | Alpaca stock endpoints, ticker universe, SIP hold-back, split adjustment, the SPY benchmark |
| Calendar and time | `sessions`, `clock`, `timing`, `delivery` | 581 | XNYS sessions, 09:30–16:00 ET, publication cron slots |
| Strategy detection | `scans`, `discovery`, `admission`, `watchlist`, `breadth`, `quality`, `funnel` | 3,114 | Stockbee scans, the 2LYNCH checklist, cross-sectional breadth over thousands of stocks |
| LLM chart reader | `grader`, `reader_authority`, `reader_coverage`, `review_selection`, `charts` | 1,694 | Anthropic Messages API, grade bands, mplfinance PNG |
| Planning, sizing, outcomes | `plan`, `allocation`, `record`, `event_risk`, `earnings_risk` | 3,511 | Whole shares, Fidelity OTO/GTC, PDT/GFV/T+1 rules, day-3/day-5 exits, SEC 8-K |
| Publication and evidence | `pipeline`, `report`, `reader`, `provenance`, `history`, `quality_ledger` | 4,737 | `docs/*.json` for GitHub Pages, Resend email, content-addressed evidence |
| Morning service (Railway) | `morning`, `morning_halts`, `morning_service`, `_journal`, `_report`, `_worker`, `morning_host`, `morning_host_wsgi` | 3,113 | Pre-open IEX quotes, Nasdaq halts, SQLite leases, WSGI |
| SEC and research studies | `issuer_evidence`, `stop_research`, `research_observations`, `research_outcomes` | 3,038 | SEC EDGAR, frozen cohort studies with a hardcoded account and dates |
| **Total** | 46 modules | **23,903** | |

### 3.4 Misleading module names

These names suggest generic, reusable code. The code does not support that reading.

| Module | Sounds like | Actually is |
|--------|-------------|-------------|
| `record.py` | A trade journal | The Stockbee pick store (`picks.json`): XNYS sequence checks, a stop-limit fill model, whole shares, the SPY benchmark |
| `history.py` | Price history | A recovery catalog of published `data.json` files, keyed by git blob SHA-1 |
| `delivery.py` | Email delivery | Publication latency measured against ET cron slots and the next XNYS open |
| `reader.py` | The chart reader | The browser projection of `data.json`; the AI reader is `grader.py` |
| `morning_service_journal.py` | A trade journal | An append-only *operations* event log for the morning service |
| `morning_service_report.py` | Performance analytics | An operational delivery report |
| `followup.py` | Trade follow-up | Next-night accounting for late-arriving daily bars |
| `research_outcomes.py` | Outcome analytics (MFE/MAE, expectancy) | Status counts for a frozen cohort; the walk itself is in `record.py`. It has no MFE/MAE, win rate or expectancy |
| `stop_research.py` | Stop-loss research | A frozen 4%→5% sizing sensitivity study with a hardcoded account and dates |
| `quality.py` | Generic data or setup quality | The Stockbee 2LYNCH checklist on daily bars |
| `inputs.py` / `input_diagnostics.py` | Generic data-quality checks | Ticker-universe ledgers; the symbol rule `[A-Z]{1,5}` would reject `XAUUSD` |
| `discovery.py` | Pattern discovery | An LLM-prompt admission contract and a regex tripwire on model replies |
| `charts.py` | Interactive charts | Static mplfinance PNGs produced for the LLM reader |

---

## 4. Dependencies

| Package (bound in `requirements.txt`) | Role in SpicyStock | Relevance to GoldScalp |
|---------------------------------------|--------------------|------------------------|
| `pandas>=2.0,<4` (pinned 3.1.0) | Frames everywhere | **Keep** (pandas 3.x: copy-on-write and string dtype defaults) |
| `numpy>=1.24,<3` (pinned 2.5.4) | Vector math | **Keep** |
| `requests>=2.31,<3` | Nasdaq, SEC, Alpaca raw HTTP | Optional, only if an HTTP data provider is added (or use `httpx`) |
| `mplfinance` (+ matplotlib, pillow) | PNG charts for the LLM | **Drop**; the browser uses Lightweight Charts |
| `anthropic>=1.15,<2` | Chart reader | **Drop**; no AI feature is in scope |
| `resend` | Email digest | **Drop** |
| `alpaca-py` | Stock bars and snapshots | **Drop**; Alpaca's stock endpoints do not serve spot XAUUSD |
| `exchange_calendars==4.13.2` | XNYS sessions | **Drop**; XAUUSD needs a 24×5 schedule with a daily break, modeled directly |
| `gunicorn` (host only) | WSGI server | Replaced by `uvicorn` (FastAPI) |
| dev: `pytest`, `PyYAML`, `httpx2` | Tests, workflow parsing, transport tests | Keep `pytest`. **`httpx2`/`httpcore2` are unusual package names; verify their provenance before reusing them** (not checked: no network) |
| Node: `jsdom`, `playwright` (installed in CI) | Browser harnesses | Optional: Playwright for GoldScalp UI smoke tests |

**Dependency discipline worth keeping.** Direct requirements are bounded below their next
major version, and the full resolution is pinned in `constraints.txt`. The motivating
incident is documented in the file itself: `anthropic` 2.0 broke every review on
21 October 2026.

---

## 5. Data models and storage

SpicyStock has **no single database**. It stores state in four places.

### 5.1 Git-committed JSON (the main data store)
| File / folder | Writer | Shape |
|---------------|--------|-------|
| `docs/data.json` (9.1 MB) | `report.write()` (atomic: temp file, fsync, `os.replace`) | The nightly record: run block, rules, bursts, plans, scorecard, coverage, timing, calendar |
| `docs/picks.json` | `record.save()` | `{schema_version: 2, picks: [{ticker, date, kind, entry_ref, stop, shares, …, evidence_ref}]}`, capped at 260 |
| `docs/reader.json` and `reader-observations/` | `reader.install_publication()` | The browser projection plus a content-addressed sidecar |
| `docs/evidence/` (about 135 MB) | `provenance.write_objects()` | Content-addressed `<sha256>.json.gz` and `.png` |
| `docs/history/` (about 99 MB) | `history.publish()` | Immutable recovery catalog: 84 records or 128 MiB, oldest evicted first |
| `docs/intraday/` | `intraday.store()` | Content-addressed minute-bar evidence: 512 objects or 16 MiB |
| `docs/quality-ledger/v1/` | `quality_ledger.append()` | Append-once deterministic gzip |
| `docs/morning.json`, `research-*.json`, `stop-research*`, `issuer-evidence*`, `source-recovery.json` | Their modules or tools | Dated companions, each with a receipt |

**Cost.** About 148 bot commits (63 "run <session>", 85 "Publish bound …") over about
75 days have grown `.git` to 310 MB and `docs/` to 379 MB. Using the repository as a
database does not scale. GoldScalp should not repeat it.

### 5.2 SQLite (Railway morning service only)
- **Tables.**
  - `state`: a singleton row holding generation, fence, lease, quota and the
    publication BLOB.
  - Journal: `morning_journal_meta`, `morning_journal_events` (unique `event_key`,
    canonical payload and its SHA-256), `morning_journal_blobs` and
    `morning_journal_refs`.
  - Worker: `provider_policy`, `provider_attempts` and `provider_cache`.
- **Transactions** (`morning_service.Store._transaction()`):
  - Writes use `isolation_level=None` plus `BEGIN IMMEDIATE`, roll back on any
    `BaseException`, and always close the connection.
  - Reads use a `file:…?mode=ro` URI inside a snapshot transaction.
- **Gaps:**
  - No PRAGMAs: no WAL and no `foreign_keys`.
  - No foreign keys at all; integrity is enforced in code.
  - No migrations, only `CREATE TABLE IF NOT EXISTS` plus a policy pinned in a singleton
    row (a mismatch raises).
- **Fit for GoldScalp.** The transaction and read-snapshot code is solid and worth
  adapting. The tables themselves are not applicable to candles or trades.

### 5.3 Browser localStorage
- `spicystock:handoff:v1` holds a private trade journal: version 4, at most 100 items
  and 1 MiB.
  - **Semantics worth learning from:** null means unknown, never 0; a result stays
    "incomplete with reasons" until the trade is fully exited and fees are reported;
    money is integer micro-dollars (BigInt); writes are serialized with Web Locks and
    read back after writing; backups are previewed before restore.
- `spicystock:following:v1` holds saved setups, with `.previous`, `.corrupt` and
  `.rejected` recovery copies.
- Storage is browser-only, so the journal does not sync between devices or tabs beyond
  the `storage` event.

### 5.4 In-memory domain shapes
Daily OHLCV `DataFrame`s indexed by session date, with `Open/High/Low/Close/Volume`
columns. Volume is in **shares**, and prices are rounded to **cents** (`scans.CENTS`).
Plans and picks are dicts validated by hand-written fault functions
(`record.pick_problem`, `intraday.validate`, `*.faults`). There are no Pydantic or
dataclass schemas at the boundaries, apart from a few dataclasses (`plan.Account`,
`plan.Sizing`, `market_data.DownloadStats`, `quality.Check`).

---

## 6. Backend modules: what each is bound to

The per-module verdicts are in [REUSE_VS_REBUILD.md §2](REUSE_VS_REBUILD.md). The
binding evidence for the modules most likely to be mistaken as reusable:

- **`market_data.py`.**
  - Bound to Alpaca stocks: `StockHistoricalDataClient` and
    `StockBarsRequest(timeframe=TimeFrame.Day, adjustment=Adjustment.SPLIT, feed=SIP)`.
  - Tickers must match `^[A-Z]{1,5}$`.
  - Freshness and adjacency come from `sessions.previous_session()` (XNYS).
  - **Generic parts:**
    - the duplicate-timestamp handling in `_download_batch()` (stable sort, keep last);
    - `readable_pair()` OHLC sanity checks;
    - the timeout adapter;
    - the permanent-vs-transient refusal classification.
- **`sessions.py`.** `exchange_calendars` `XNYS`, pinned to version 4.13.2, plus
  `America/New_York`. It cannot describe a 24×5 market with a daily break. **Generic
  part:** offset-aware instants are required (`market_time()`).
- **`scans.py`.** Every function encodes a Stockbee screen with share-volume floors
  (`MIN_VOLUME = 100_000`) and cent rounding. **Generic parts:** the design (pure
  `(df, at)` functions that return `None` when a value cannot be read, and an archived
  `RULES` dict).
- **`intraday.py`.**
  - It is tied to XNYS hours, a 30-minute entry window, `plan.ENTRY_WINDOW_MINUTES` and
    long-only buy-stop-limit tickets.
  - **Generic and valuable:** the decision logic in `resolve()`:
    - first crossing minute;
    - fill at `max(trigger, open)`;
    - a gap over the limit means no fill, or uncertain;
    - a crossing minute that also reaches the stop means uncertain.
  - `_disagrees()`: minute bars must reproduce the coarse bar before they are used.
  - The atomic, content-addressed store (`_write`, `canonical`, `name_of`).
- **`plan.py`.**
  - Fidelity tickets, PDT/GFV/T+1 rules and Stockbee exit schedules.
  - **Generic:** `size()` (risk ÷ stop distance, in exact integer cents, with a cap).
    It is long-only and whole-share.
  - **Generic:** `follow()`'s intrabar rule that the stop wins when a bar's order is
    unknown.
- **`record.py`.**
  - Bound to XNYS session sequencing, whole shares and the SPY benchmark.
  - **Generic:** `r_multiple()`, which is long-only.
  - **Generic:** `summarize_scorecard()`, which uses exclusive buckets and hides rates
    until `SCORECARD_MIN_PLANS = 20`.
  - **Generic:** the `load()` / `set_aside()` / `save()` corruption handling.
- **`provenance.py`.** Most of it replays Stockbee stages. **Fully generic:** `_typed()`
  plus `digest()`, a lossless canonical SHA-256 where floats are hashed via
  `float.hex()` and None, False, 0 and 0.0 stay distinct.
- **`breadth.py`.** Market Monitor counts over thousands of stocks. **Generic:**
  `_lag()` and `_trailing()`, NaN-strict rolling windows built on
  `sliding_window_view`.
- **`morning_service*.py`.** Leases, fencing and quota for one Railway service.
  **Generic:**
  - `Store._transaction()` and the read-only snapshot;
  - worker `Ledger` quota and backoff;
  - `BoundedSession`: an endpoint allowlist, no redirects, a capped streamed body and a
    deadline per chunk.

**No MFE/MAE, profit factor or proper drawdown exists anywhere in `src`.**
Expectancy appears only as `avg_r`, and drawdown only in `tools/evidence_review.drawdown()`
(cumulative R). `tools/signal_outcomes.py` adds `_cost_r()` (costs in R) and
`bootstrap_nights()` (seeded resampling). These are the closest things to a pattern
evaluator.

---

## 7. Frontend

- **Shape.**
  - Static site with no modules and no bundler: classic `<script>` tags in a fixed
    order, all attached to a shared `window.SCStock` global.
  - `app.js` is **5,930 lines (458 KB) in one closure, with 268 functions**.
  - The other app files total about 9.4k lines; the vendored design system
    (`design-system/sc.css`, 1,783 lines / 132 KB) about 3.3k.
  - About 90% of the UI is SpicyStock-specific.
- **Charting.** A hand-built SVG daily candle chart (`app-chart.js`, 1,132 lines) with
  no library, no zoom and no pan. It has good accessibility (an aria summary, a table
  twin, keyboard bar-stepping) and a clean host lifecycle (`update`/`dispose`, a
  live-chart leak counter). **Lightweight Charts makes the rendering obsolete; the
  lifecycle and accessibility ideas remain relevant.**
- **Data loading.**
  - One loader handles boot and refresh, with a 15 s deadline over headers and body.
  - Sequence and epoch guards make late answers lose.
  - Byte caps and fatal UTF-8 decoding; SHA-256 verification of sidecars.
  - Named outcomes (`unchanged`/`older`/`revised`/`newer`/`invalid`/`timeout`/`failed`),
    and a failed refresh keeps the current state.
- **Labelling of data kinds.**
  - Strong. `availability()` separates data freshness from action timing; timing
    "narrows, never widens".
  - Wording such as "recorded snapshot, not live prices", "model plans" and "not
    personal P&L".
  - Fixture/demo flags, and a split between "at the signal" and "since the signal".
- **Theme.** Dark by default. Three token layers (primitive → semantic → component),
  with contrast ratios documented in comments.
- **DOM safety.** **Zero** uses of `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `document.write`, `eval` or `new Function` (verified by grep). All DOM is built
  through `SC.el()` with `textContent`.
- **Testing.**
  - Playwright `page_smoke.mjs` (4,661 lines): pinned clock, injected data, 1280/390/320 px
    viewports, both themes, collection of console and request errors.
  - jsdom continuity checks, with a `--baseline` run against an old commit to prove the
    checks fail there.

---

## 8. Security and configuration concerns

| # | Severity | Finding | Evidence | Implication for GoldScalp |
|---|----------|---------|----------|---------------------------|
| 1 | **High** | The owner's **real account size, broker and account type are committed and published** | `.github/workflows/evening.yml` (`ACCOUNT_EQUITY`), `.env.example`, `docs/app-morning.js` `REFERENCES`, the `docs/data.json` `account` block, `knowledge/broker-fidelity.md` | Paper-account settings go in the local DB or environment only, never in the repo or any public output |
| 2 | **High** | The owner's **personal email is hardcoded in a User-Agent**, and a test requires an email there | `src/issuer_evidence.py` `USER_AGENT` | Copy no identities. Any contact string comes from config and defaults to none |
| 3 | Medium | Personal emails appear in **git author metadata** and in **raw GitHub API captures** that are committed; the owner is named throughout `CLAUDE.md` | `docs/input-truthfulness/2026-09-30-delivery-readiness-evidence/` (11 files), `git log` | Start GoldScalpAI as a fresh repository with a noreply commit address; never import SpicyStock history; never commit raw API responses |
| 4 | Medium | **One job holds all provider secrets and has `contents: write`**, with default `persist-credentials` | `evening.yml` job `scan` (`permissions: contents: write`) | Separate read-only collect jobs from write-only persist jobs, as SpicyStock's own `morning.yml` does |
| 5 | Medium | **Most third-party actions use floating tags** (`actions/*@v4/@v5`, `gitleaks/gitleaks-action@v2`); SHA pins appear only in the historical jobs | `.github/workflows/*.yml` | Pin actions by SHA from day one |
| 6 | Medium | **A browser User-Agent is spoofed** to an upstream API | `src/universe.py` `DIRECTORY_HEADERS` (a Chrome UA for api.nasdaq.com) | Terms-of-service risk. Use honest, configurable client identification with gold data vendors |
| 7 | Medium | **The repository is used as a database** | `.git` 310 MB, `docs/` 379 MB, about 148 bot data commits in about 75 days | Persist in SQLite outside git; keep `var/` and `data/` gitignored |
| 8 | Medium | **No Content-Security-Policy**; **two links take their `href` straight from record data** with no scheme check | `docs/index.html` (no CSP); `docs/app.js` `href: c.row.chart` and `href: b.chart` | A tampered record could inject a `javascript:` URL. GoldScalp: a strict CSP header plus a URL scheme allowlist |
| 9 | Low | The browser calls the **unauthenticated GitHub API** (exposing visitor IPs, rate limits) and loads **Google Fonts twice** | `liveRunLog()` / `RUNS_API` in `app.js`; `index.html` and `sc.css` `@import` | No third-party calls from the browser; self-host fonts |
| 10 | Low | The container has **no `USER` directive**: the launcher runs as root by design, then drops to UIDs 10001/10002; gunicorn logs go to `/dev/null` | `Dockerfile`, `src/morning_host.py` | A plain non-root `USER`, a read-only app tree, structured logs |
| 11 | Low | **`SIGALRM` deadlines** only work on POSIX and only in the main thread | `issuer_evidence.py`, `research_observations.py`, `source_recovery.py` | Unusable inside FastAPI's thread pool or on Windows. Use client timeouts or `asyncio` deadlines |
| 12 | Low | **Hardcoded deployment identity**: the Pages origin, Railway URL and repo slug/ID | `report.PAGE_URL`, `morning_host_wsgi.ALLOWED_ORIGIN`, `docs/index.html` meta, `historical-input-proof.yml` | Parameterize every origin and URL |
| 13 | Low | **Unusual package names** `httpx2` / `httpcore2` | `requirements-dev.txt`, `requirements-host.txt`, `constraints.txt` | Not verified (no network). Do not carry them over unverified |
| 14 | Info | `CLAUDE.md` is 1.05 MB and grants owner-specific standing authorizations (for example a continuous merge loop) | `CLAUDE.md` | Do not copy it. GoldScalp gets its own short, project-specific notes |

**Positive controls observed:**
- No credential-shaped strings in the working tree (pattern sweep); no `.env` file.
- `.gitignore` blocks `.env*`.
- No `shell=True`, no pickle; YAML is loaded only with `safe_load`; `subprocess` is
  called with argument lists only.
- gitleaks runs with narrow value-and-path allowlists, plus canary tests that prove the
  allowlists cannot over-match (`tests/secret_scan_controls.py`).
- Sockets are blocked in every test (`tests/conftest.py` `_no_network`).
- The Docker base image is pinned by digest; `.dockerignore` is an allowlist; the code
  is read-only in the image.
- The WSGI app is GET-only, refuses query strings and request bodies, and allows a
  single exact CORS origin.

---

## 9. Testing and CI

- **Suite.**
  - 121 test files and 2,911 test functions. CLAUDE.md claims 6,040 collected tests with
    parametrization; this was not verified.
  - There is no `pytest.ini` or `pyproject.toml`.
- **Test infrastructure.**
  - Autouse fixtures: no network, an isolated working directory, a fake SEC.
  - Provider doubles patched where they are looked up (`fake_alpaca`, `fake_anthropic`,
    `fake_resend`).
  - Seeds from `synthetic.seed_for()` (crc32 of the test node id).
  - `FakeAlpaca` knobs for stale and duplicate bars, holes and failing symbols.
  - The clock is injected through `now=` parameters instead of freezegun.
- **Disciplines.**
  - An AST guard against bare numeric literals in function bodies (`test_plan.py`,
    `test_scans.py`).
  - Docs-versus-code tests (`test_docs.py`).
  - Parsed-YAML workflow tests.
  - Fixtures regenerated from the real pipeline with `--check` in CI.
  - A CI step that fails if the suite dirtied the working tree.
  - "Prove a test can fail" mutation practice (`CLAUDE.md`).
- **CI.**
  - `tests.yml`: pytest on Python 3.12 (no version matrix), actionlint, fixture checks,
    and a Playwright page job on Node 22.
  - Secret scanning on push and PR, plus a weekly full-history scan.
  - Data workflows commit results back to `main`.

---

## 10. Domain mismatch: US equities vs XAUUSD

| Dimension | SpicyStock assumes | XAUUSD needs |
|-----------|--------------------|--------------|
| Market hours | XNYS 09:30–16:00 ET sessions, holidays via `exchange_calendars` | About 24×5 (Sun ~18:00 to Fri 17:00 New York), a daily ~1 h break, venue-specific holidays |
| Bar resolution | Daily bars; minutes only to resolve first-day fills | M1 base series with M5/M15/H1 resampling |
| Instruments | A universe of about 4,800 tickers matching `[A-Z]{1,5}`, plus a seed list | One symbol, with broker aliases (`XAUUSD`, `GOLD`, `XAUUSD.m`) |
| Volume | Consolidated share volume with liquidity floors | Tick volume (activity proxy); no consolidated volume |
| Price adjustments | Split adjustment, corporate actions, earnings | None |
| Sizing | Whole shares, cents, position caps, PDT/GFV/T+1 | Lots (0.01 step), 100 oz contracts, leverage/margin, spread |
| Direction | Long-only | Long and short |
| Data provider | Alpaca stock SIP/IEX | FX/CFD/metal vendors, MT5 terminal exports, user CSV |
| Strategy | Stockbee momentum burst, breadth regime | Market structure, liquidity, FVG/OB, session behaviour, volatility |
| Output | Nightly static JSON plus email | Interactive API plus a terminal UI, a simulator, a journal |
| Persistence | Git-committed JSON; SQLite only on the host | SQLite as the system of record |

---

## 11. Not checked / limitations

- No code was executed. The test count, runtime behaviour and current CI health are
  unverified.
- **Git history** was not scanned for secrets; SpicyStock's weekly gitleaks job covers
  that.
- Not fully read:
  - `reader._retention` / `_prune_owned`;
  - most `quality._check_*` bodies;
  - the inner stages of `pipeline.run_evening`;
  - `report.build` / `validate` beyond their opening sections;
  - the internals of `sc-motion` / `sc-reading` / `sc-matrix-nav` / `sc-map`;
  - most of `tools/*.mjs`.

  None of these is a reuse candidate.
- The provenance of the `httpx2` package name could not be verified (no network).
- Docker is **not installed** on this machine, so container builds for GoldScalp cannot
  be verified locally until it is. Local tooling found: Python 3.12.10, Node 24.22,
  Git 2.56.
