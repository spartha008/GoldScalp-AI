# Known issues, limitations and audit findings (Phases 1-6)

Compiled from the stored records. Each item names its source. Nothing here changes a result.

## Incomplete or cancelled studies

- Phase 5 evaluation `me_cb6b197c4c6b` is INCOMPLETE (9 runs not completed). Recorded reasons: M1 execution candles cover 0% of the month (need 95%; stored M1 has none in the month); M1 analysis history before the month starts nowhere; 5 days are needed. Source: store `evaluations`, `runs`.
- Phase 5 evaluation `me_697122f9e14b` is INCOMPLETE (12 runs not completed). Recorded reasons: M1 execution candles cover 0% of the month (need 95%; stored M1 has none in the month); M1 analysis history before the month starts nowhere; 5 days are needed. Source: store `evaluations`, `runs`.
- Robustness study `rb_da267aa33110` (2026-11-14T16:16:35.391961Z): status FAILED, error 'cancelled'. Only `rb_7ca33b7b5a81` has final verdicts; its configurations are archived as variants of a superseded or cancelled study.
- Robustness study `rb_a732696e0c83` (2026-11-14T16:20:59.222884Z): status FAILED, error 'cancelled'. Only `rb_7ca33b7b5a81` has final verdicts; its configurations are archived as variants of a superseded or cancelled study.
- Robustness study `rb_825da1543f0f` (2026-11-14T16:43:54.446477Z): status COMPLETED. Only `rb_7ca33b7b5a81` has final verdicts; its configurations are archived as variants of a superseded or cancelled study.

## Bugs found and fixed during the research

- Phase 5.1 risk study re-run at 2026-11-14T15:53:18.301152Z: M15 timeline was served from an in-memory cache filled by an app-store reproduction (identical candles, other series); stored signal ids belonged to live:mt5:XAUUSD. Cache key fixed to include the series; re-run M15 only. Replaced result hashes are listed in `phases/phase_5_1_risk_scenarios/risk_studies.json`.

## Limitations of the evidence

- Phase 4 live validation produced no live evidence: the monitors were started on Saturday 14 November 2026 after the weekly close and stopped on 15 November before the market reopened, so they recorded no evaluation, signal or trade (`phases/phase_4_live_validation/final_state`).
- MT5 terminal verification: the first record FAILED and the second was PARTIAL (`mt5_verifications`); the fresh-tick VERIFIED record after a market open was never obtained.
- Profit factor is computed on money and expectancy on R, so a result can show PF above 1 with negative expectancy in R (for example H6.4 on the sealed block: PF 1.04, -0.006 R/trade).
- Small samples: several positive figures rest on fewer than 40 trades (R7, R7 v2, R4 v2, B v2, B v3).
- Development data was reused across Phase 5.2, 5.3 and Phase 6 design; only the Phase 5.3 months (January-May 2026) for its locked candidates and the sealed 2023-2025 block for H6.4 were unseen when used.
- The H6.4 report shows 'Half 1 / Half 2' as 0.00 for the development run, where halves do not apply; the report code is part of the frozen H6.4 files and was left unchanged.
- PDF and CSV exports are generated on demand from the stored data; apart from the report files listed in the phase folders they were not stored as files.

## Audit findings

- The Phase 0 audit of the earlier SpicyStock repository is preserved as `phases/phase_1_to_3_foundations/ARCHITECTURE_AUDIT.md` (with REUSE_VS_REBUILD.md); the repository itself was not modified or copied.
