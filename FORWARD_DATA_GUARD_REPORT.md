# GoldScalp AI: Forward-Data Guard Implementation Report

**15 November 2026, finished at 19:03 UTC, before the market reopened at 23:00 UTC.** Definition `forward-guard@1`.

Research can no longer read candles from **14 November 2026 00:00:00 UTC** onward. Live MT5 collection, raw charts,
the dashboard and the (archived) Phase 4 monitor loop are unaffected. No strategy, evaluation criterion, historical
result, frozen protocol, database row or candle was changed.

## 1. Where the candles come from

Strategy Lab, Performance and improvement studies do **not** read from MT5. They read only from the application
database (`var/goldscalp.sqlite3`, table `candles`). The research service reads that database read-only plus its own
store, and never connects to MT5.

The main application's MT5 connection writes into that same database: live candles and a history backfill on every
start. So forward candles do arrive in the database research reads, and that is why the guard is in code.

## 2. Boundary and convention

- **Boundary:** `FORWARD_FROM = 2026-11-14T00:00:00Z` (epoch 1794614400). It is identical to the frozen Phase 6
  constant `discovery.FORWARD_FROM`, and a test checks that.
- **Convention:** times are UTC; candles are keyed by their open time (`ts`); research windows are half-open,
  `[start, end)`.
- **When a window counts as forward:** it includes forward-period candles if `end > FORWARD_FROM` or
  `start >= FORWARD_FROM`. A window ending exactly at the boundary is allowed: its last candle opens before midnight.

## 3. Design

One central module, `goldscalp/core/forward.py`, checks at two layers. Both raise `ForwardDataLocked`, which both
applications turn into **HTTP 409** with the reason and the boundary.

1. **Request layer (`check_range`).** Every research request is checked before any data is read. That includes
   ranges computed by the server: the improvement study's discovery-to-hold-out window, computed from the stored
   coverage, and the robustness study's default range.
2. **Read layer (`check_dataset`, `check_frame`, `check_index`).** Every candle frame a research path loads is checked
   after reading. This covers the look-back history, the outcome candles after the window and the execution candles.
   - In the main app, research routers can read candles only through `goldscalp/api/research_data.py`; a test parses
     the routers to enforce this.
   - In the research service, every background job passes through `runner.prepare`, which applies both layers.

A request that touches the forward period is refused with its reason. **Nothing is truncated.**

**There is no switch to turn the guard off.** No setting, environment variable or flag disables it, and a test checks
that. Unlocking needs a separate, versioned `forward-unlock@1` protocol:
- the protocol file lives in `config/research/forward-unlock/<id>.json`;
- it must be listed with its SHA-256 in that folder's `REGISTRY.json`;
- it covers only its declared scope and purposes.

Changing the file after registration voids it. **No protocol is registered,** and no endpoint accepts one yet. A
future one-time forward validation would add that, under a frozen protocol.

## 4. Changed files

| File | Change |
|---|---|
| `goldscalp/core/forward.py` | **New.** Boundary, the two check layers, unlock-protocol validation, status |
| `goldscalp/api/research_data.py` | **New.** The guarded research loaders for the main app (the raw loaders are unchanged) |
| `goldscalp/api/routers/research.py` | Strategy Lab (backtests, comparisons, walk-forward) reads only through the guarded loaders; `GET /api/research/forward-guard` |
| `goldscalp/api/routers/performance.py` | Performance runs read only through the guarded loaders |
| `goldscalp/api/routers/improvement.py` | Improvement studies: guarded loaders for the computed range, and Phase 4 live evidence refused if it contains forward-period records |
| `goldscalp/api/app.py` | `ForwardDataLocked` handled as HTTP 409 |
| `goldscalp/monthly/runner.py` | `prepare` checks the requested window and every loaded frame (all research-service jobs pass here) |
| `goldscalp/monthly/service.py` | Monthly evaluation, risk study and robustness study requests checked; `GET /api/monthly/forward-guard` |
| `goldscalp/monthly/standalone.py` | `ForwardDataLocked` handled as HTTP 409 |
| `tests/test_forward_guard.py` | **New.** 17 tests |

No strategy, criterion, frozen file (Phase 5.3, the closure, Phase 6, H6.4), report or database was modified.

## 5. Endpoint coverage

**Main application (port 8000)**

| Endpoint | Guard | A forward request gets |
|---|---|---|
| `POST /api/research/backtests` | request range, history, outcome extension | 409 |
| `POST /api/research/comparisons` | the same | 409 |
| `POST /api/research/walk-forward` | the same | 409 |
| `POST /api/performance/runs` | request range, history, outcome extension | 409 |
| `POST /api/research/studies` (improvement) | computed range (coverage preset), history, outcomes, live evidence | 409 |
| Lab and Performance background jobs | they run only on data validated when the job was created | — |

**Research service (port 8001)**

| Endpoint | Guard | A forward request gets |
|---|---|---|
| `POST /api/monthly/evaluations` | month range (after the "completed month" check) | 409 (an incomplete month: 422) |
| `POST /api/monthly/evaluations/{id}/research` and `/run` | `runner.prepare` (background jobs) | job refused or failed with the reason |
| `POST /api/monthly/runs/{id}/reproduce` | `runner.prepare` | 409 |
| `POST /api/monthly/risk-studies` | baseline month, then `runner.prepare` | 409 |
| `POST /api/monthly/robustness-studies` | requested or default range, then `runner.prepare` | 409 |
| `POST /api/monthly/validation-studies` | permanently refused by the Phase 5 closure, and `runner.prepare` | 409 |
| `POST /api/monthly/discovery/*` | the frozen Phase 6 guard, and `runner.prepare` | 409 |
| `POST /api/monthly/h64/runs` | sealed or development windows only, and `runner.prepare` | refused |

**Raw monitoring, deliberately not guarded:**
- `/api/market/*` (candles, stream, status), the dashboard, `/api/signals` (live), the live monitor and demo execution;
- `/api/replay` (candle-by-candle viewing);
- data import, and the monthly availability and coverage counts (they store or count candles, they do not evaluate a
  strategy);
- the research library (stored results only).

**CLI:** no command runs research. `run.py` and `research.py` start the guarded services. The report and archive
commands only read stored results or count candles.

## 6. Tests

`tests/test_forward_guard.py` (17 tests) covers:

| Area | What is checked |
|---|---|
| Ranges | historical ranges; the exact boundary; ranges crossing it; entirely forward ranges; November 2026; December 2026 (research service) |
| Dynamic presets | an improvement study computed from coverage that reaches forward data; a robustness range |
| Direct API calls | every guarded endpoint above |
| Background jobs | `runner.prepare` and the main-app loaders called directly |
| Raw monitoring | forward candles are still served by `/api/market/candles` |
| Switching off | no setting or environment variable disables the guard |
| Unlocking | only a registered, unchanged protocol within its scope and purpose unlocks; no protocol is registered in the project |
| Bypass | the research routers cannot call the raw loaders |

**Full suite: 675 passed, 0 failed** (Ruff clean).

The suite ran with `GOLDSCALP_DEMO_EXECUTION_ENABLED=false`. Your local `.env` turns demo execution on, and two existing
tests that check the default read that file. They fail whenever it is on, which is unrelated to the guard.

One existing test (`test_completed_months_only_and_security_headers`) expects an unfinished month to be refused as "not
completed". The forward check therefore runs after that check; both still refuse the request.

## 7. Restart and verification

**Before restarting:**
- no Strategy Lab, Performance or research-service job was running;
- monitors 1–3 were stopped with 0 open trades;
- demo execution had 0 links and 0 open positions;
- MT5 was connected and the market closed until 23:00 UTC.

**Research service:** restarted at 19:01 UTC.
- `/api/monthly/forward-guard` reports locked.
- A robustness request to 1 December 2026 was refused with 409, and no study was created.

**Main application:** restarted at 19:02 UTC.
- The process another Claude session had started at 17:50 UTC was stopped. GoldScalp then started again under this
  session's preview.
- `/api/health` is ok.
- **MT5 reconnected:** terminal connected (build 6317, DEMO environment), provider MetaTrader 5, market HISTORICAL
  (weekly close).
- The monitor loop is running with no error; monitors 1–3 are stopped with 0 open trades.
- Demo execution is enabled (your setting), with 0 links, 0 open trades and no error.
- The dashboard renders.
- `/api/market/candles` serves the latest MT5 candles.

**Live refusals after the restart:**
- a Strategy Lab backtest to 16 November and a Performance run to 20 November were refused with 409;
- no run was created (research runs 2, Performance runs 1, as before).

**Preserved:**
- the Phase 5 closure, Phase 5.3, Phase 6 and H6.4 checks all report unchanged;
- `archive/generation-1` (46 files) and `research_archive/phase_1_to_6` (358 files) are unchanged and read-only;
- 8 strategy versions, A, B and C still archived;
- 0 forward candles stored at the time of verification.

## 8. Limitations

- Replay is treated as raw viewing and is not guarded. Opening a replay of forward dates shows the candles and the
  Phase 2 analysis, but runs no strategy.
- The unlock mechanism is validated and tested, but no endpoint passes a protocol yet. A future forward validation must
  wire it into that one endpoint under its frozen protocol.
- The MT5 history backfill on each start stores forward candles in the application database. This is intended raw
  collection, and the guard keeps them out of research.
