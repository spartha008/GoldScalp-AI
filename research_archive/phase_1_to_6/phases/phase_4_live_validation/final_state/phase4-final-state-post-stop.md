# GoldScalp AI: Phase 4 final state (post-stop)

Recorded 2026-11-15T18:21:31.504910Z. Read-only record; nothing was changed to produce it.

**Conclusion.** Phase 4 did not establish a reliable edge. A and C were rejected; B was inconclusive because of its small sample. This is not a claim that every strategy was proven ineffective.

**Monitor loop:** running=True, cycles=914, last cycle 2026-11-15T18:21:30.487093Z, last error None.

## Monitors

| Id | Strategy | Version | Fingerprint | Mode | Status | Account | Started | Stopped |
|---:|---|---:|---|---|---|---:|---|---|
| 1 | Example A: aligned structure + M5 BOS | 2 | `c7bc3da5f4228b37…` | paper | **stopped** | 2 | 2026-11-14T11:57:52.728593Z | 2026-11-15T18:21:04.646639+00:00 |
| 2 | Example B: sweep + CHoCH + FVG in session | 2 | `5d8d81c697949510…` | paper | **stopped** | 3 | 2026-11-14T11:57:52.751064Z | 2026-11-15T18:21:04.720676+00:00 |
| 3 | Example C: alignment + confirmation + volatility | 4 | `14ceea2a1c9e8997…` | paper | **stopped** | 4 | 2026-11-14T11:57:52.768948Z | 2026-11-15T18:21:04.799355+00:00 |

## Research verdicts of the monitored rules

| Strategy | Version | Class | Evidence | Same rules as the Phase 5 original |
|---|---:|---|---|---|
| A | 2 | rejected | Phase 5.2 out of sample -0.005 R/trade over 157 trades | True |
| B | 2 | inconclusive | Phase 5.2 out of sample +0.029 R/trade over only 24 trades: too small a sample | True |
| C | 4 | rejected | Phase 5.2 out of sample -0.005 R/trade over 249 trades | True |

## Activity

| Table | Rows |
|---|---:|
| monitored_strategies | 3 |
| monitor_evaluations | 0 |
| strategy_signals | 0 |
| live_trades | 0 |
| trade_evaluations | 0 |
| strategy_journal | 0 |
| demo_links | 0 |
| demo_trades | 0 |

## Accounts

| Id | Name | Purpose | Phase 4 | Balance | Open positions |
|---:|---|---|---|---:|---:|
| 1 | Demo paper account | manual | no | 9999.9650 | 1 |
| 2 | Validation: Example A: aligned structure + M5 BOS v2 (SIMULATED) | validation | yes | 10000 | 0 |
| 3 | Validation: Example B: sweep + CHoCH + FVG in session v2 (SIMULATED) | validation | yes | 10000 | 0 |
| 4 | Validation: Example C: alignment + confirmation + volatility v4 (SIMULATED) | validation | yes | 10000 | 0 |

## Outstanding simulated positions

- Position 1 (account 1): buy 0.01 lots at 3886.70 since 2026-11-13T18:07:32.164117Z.

## MT5 verification records

- mv_8ab8d3c470f5930a (2026-11-14T11:48:53.203529Z): FAILED, DEMO.
- mv_214ba65715fb0f86 (2026-11-14T11:52:37.706999Z): PARTIAL, DEMO.

## Notes

- The monitors were started on Saturday 14 November 2026 at 11:57 UTC, after the weekly close, so no step candle closed in trading hours while they were active: they recorded no evaluation, signal or trade. The research verdicts come from the Phase 5.2 backtests of the same rules.
- Account 1 (manual Phase 1 paper trading) is outside Phase 4; it is recorded and left unchanged.
