# GoldScalp AI: Phase 6 H6.4 report

> Historical simulation only: separate hypothetical $1,000 accounts per configuration, timeframe and month; no orders; Phase 4 untouched. Classes describe historical research candidates, not trading readiness.

## 1. Registration

**H6.4 Trend continuation outside contracting volatility.** H1-trend continuation with an M15 break of structure produces positive expectancy when M15 volatility is not contracting.

**Origin (disclosed).** Development-derived, disclosed: generated on 15 November 2026 from the Phase 6 development results on January-13 November 2026, after five configurations and their breakdowns had been seen. The VOL-C baseline made +0.102 R/trade over 294 trades (9 of 11 months positive); its contracting-volatility trades made -0.171 R/trade over 31 trades. These development figures are the reason for the hypothesis and are NOT evidence for it. H6.4 is a new hypothesis, not a correction of H6.1, which stays recorded as not supported as predicted.

**Candidate** `H6.4`: Exactly VOL-C (H1 structure in the trade direction, a fresh M15 BOS in that direction within 2 candles, spread at most 0.50) plus one condition: M15 volatility normal or expanding (not contracting). **Control** `VOL-C`: comparison baseline only: reported side by side, never selected instead of the candidate.

**Prediction.** On the sealed data: (1) the candidate's pooled M5 expectancy after costs is above zero; (2) the control's trades taken in contracting M15 volatility have a lower expectancy than its trades taken outside contracting volatility.

**Protocol:** h64-protocol-frozen.json frozen 2026-11-15T10:52:18.332396+00:00 (unchanged), 0 sealed results at freeze.

## 2. Sealed data verification (before any result)

- Months with sufficient data (calendar-aware 95 % rule plus warm-up), of 24: M5 24, M15 24, H1 24.
- Duplicate timestamps: {'M5': 0, 'M15': 0, 'H1': 0}; misaligned bars: {'M5': 0, 'M15': 0, 'H1': 0}.
- M5 aggregated to M15 vs the M15 export: {'compared': 48445, 'mismatched': 0}; M15 to H1 vs the H1 export: {'compared': 12106, 'mismatched': 0}.
- Timezone: sealed H1 vs the application's own MT5 H1 history: {'compared': 12606, 'mismatched': 0}.
- December 2024: December 2024 measured 94.4 % (M15) and 94.5 % (H1) against the plain schedule. Every missing bar falls in the Christmas Eve early close (from 18:45 UTC on 24 Dec), Christmas Day (through the 26 Dec reopen hour) and the New Year's Eve early close; there are no other gaps. The threshold is kept; the expected count follows the market calendar.

| Month | M5 share (plain) | M15 share (plain) | H1 share (plain) | Holiday bars excused (M5/M15/H1) |
|---|---:|---:|---:|---:|
| 2023-11 | 99.9% (98.7%) | 100.0% (98.8%) | 100.0% (99.0%) | 69/23/5 |
| 2023-12 | 100.0% (95.0%) | 100.0% (95.0%) | 100.0% (95.0%) | 288/96/24 |
| 2024-01 | 100.0% (95.4%) | 100.0% (95.4%) | 100.0% (95.5%) | 294/98/24 |
| 2024-02 | 100.0% (99.4%) | 100.0% (99.4%) | 100.0% (99.6%) | 33/11/2 |
| 2024-03 | 100.0% (95.2%) | 100.0% (95.2%) | 100.0% (95.2%) | 276/92/23 |
| 2024-04 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |
| 2024-05 | 100.0% (99.3%) | 100.0% (99.3%) | 100.0% (99.4%) | 45/15/3 |
| 2024-06 | 100.0% (99.2%) | 100.0% (99.2%) | 100.0% (99.4%) | 45/15/3 |
| 2024-07 | 100.0% (99.3%) | 100.0% (99.3%) | 100.0% (99.4%) | 45/15/3 |
| 2024-08 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |
| 2024-09 | 100.0% (99.3%) | 100.0% (99.3%) | 100.0% (99.4%) | 42/14/3 |
| 2024-10 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |
| 2024-11 | 100.0% (99.0%) | 100.0% (99.0%) | 100.0% (99.2%) | 57/19/4 |
| 2024-12 | 100.0% (94.4%) | 100.0% (94.4%) | 100.0% (94.5%) | 339/113/28 |
| 2025-01 | 100.0% (95.4%) | 100.0% (95.4%) | 100.0% (95.5%) | 294/98/24 |
| 2025-02 | 100.0% (99.4%) | 100.0% (99.4%) | 100.0% (99.6%) | 33/11/2 |
| 2025-03 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |
| 2025-04 | 100.0% (95.5%) | 100.0% (95.5%) | 100.0% (95.5%) | 276/92/23 |
| 2025-05 | 100.0% (99.5%) | 100.0% (99.5%) | 100.0% (99.6%) | 33/11/2 |
| 2025-06 | 100.0% (99.2%) | 100.0% (99.2%) | 100.0% (99.4%) | 45/15/3 |
| 2025-07 | 100.0% (99.2%) | 100.0% (99.2%) | 100.0% (99.2%) | 48/16/4 |
| 2025-08 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |
| 2025-09 | 100.0% (99.5%) | 100.0% (99.5%) | 100.0% (99.6%) | 33/11/2 |
| 2025-10 | 100.0% (100.0%) | 100.0% (100.0%) | 100.0% (100.0%) | 0/0/0 |

## 3. Development run `sr_7c063d1e50d8` (attempt 1, COMPLETED)

Debugging run on January–13 November 2026. Disclosed; it did not decide whether the sealed test ran.

| Configuration | TF | Months valid | Trades | R/trade | PF | Sum R | Max DD R | ×2 | ×3 | Half 1 R | Half 2 R |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `H6.4` | M5 | 11 | 269 | +0.113 | 1.55 | 30.46 | 8.27 | +0.102 | +0.090 | 0.00 | 0.00 |
| `VOL-C` | M5 | 11 | 294 | +0.102 | 1.53 | 30.02 | 6.98 | +0.090 | +0.078 | 0.00 | 0.00 |

Month by month (M5, sum of R and trades):

| Configuration | 2026-01 | 2026-02 | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 | 2026-10 | 2026-11 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `H6.4` | 1.9 (25) | 0.2 (23) | 11.6 (31) | 5.1 (23) | -4.6 (24) | 1.6 (12) | -3.1 (29) | 5.7 (35) | 6.6 (29) | 0.6 (28) | 5.0 (10) |
| `VOL-C` | 1.8 (26) | 0.2 (25) | 10.4 (34) | 3.9 (25) | -4.3 (26) | 1.5 (13) | -0.7 (33) | 7.0 (38) | 5.3 (31) | 0.9 (32) | 4.0 (11) |

By M15 volatility regime (M5; trades, R/trade):

- `H6.4`: expanding 64, +0.065; normal 205, +0.128
- `VOL-C`: contracting 31, -0.171; expanding 64, +0.065; normal 199, +0.157

## 4. Sealed run `sr_ed2c472a7559` (attempt 1, COMPLETED)

| Configuration | TF | Months valid | Trades | R/trade | PF | Sum R | Max DD R | ×2 | ×3 | Half 1 R | Half 2 R |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `H6.4` | M5 | 24 | 663 | -0.006 | 1.04 | -4.24 | 29.45 | -0.036 | -0.066 | -8.07 | 3.83 |
| `H6.4` | M15 | 24 | 659 | -0.014 | 1.03 | -9.42 | 33.53 | -0.043 | -0.073 | -13.27 | 3.85 |
| `VOL-C` | M5 | 24 | 762 | -0.029 | 1.03 | -21.82 | 43.82 | -0.061 | -0.093 | -18.50 | -3.31 |
| `VOL-C` | M15 | 24 | 758 | -0.036 | 1.02 | -26.96 | 48.96 | -0.067 | -0.099 | -23.68 | -3.28 |

Month by month (M5, sum of R and trades):

| Configuration | 2023-11 | 2023-12 | 2024-01 | 2024-02 | 2024-03 | 2024-04 | 2024-05 | 2024-06 | 2024-07 | 2024-08 | 2024-09 | 2024-10 | 2024-11 | 2024-12 | 2025-01 | 2025-02 | 2025-03 | 2025-04 | 2025-05 | 2025-06 | 2025-07 | 2025-08 | 2025-09 | 2025-10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `H6.4` | 4.7 (27) | -6.2 (19) | -3.5 (26) | -5.2 (18) | 6.1 (28) | 4.5 (32) | 0.3 (38) | 3.5 (24) | -0.5 (35) | 0.4 (22) | -7.8 (19) | -4.2 (26) | -1.9 (38) | -2.5 (23) | 4.7 (35) | -2.2 (21) | -3.3 (27) | -2.2 (28) | -1.7 (28) | 2.7 (25) | 5.4 (32) | -4.6 (28) | 0.3 (29) | 9.1 (35) |
| `VOL-C` | 4.0 (32) | -8.0 (23) | -1.9 (28) | -5.4 (21) | 1.6 (33) | 3.9 (37) | -2.0 (46) | -0.2 (28) | -2.6 (39) | -0.4 (24) | -7.0 (23) | -0.5 (28) | 2.6 (45) | -3.7 (27) | -0.2 (42) | -1.5 (25) | -7.2 (33) | -2.8 (30) | -2.0 (32) | 6.0 (28) | 0.5 (34) | -3.8 (33) | -1.7 (32) | 10.6 (39) |

By M15 volatility regime (M5; trades, R/trade):

- `H6.4`: expanding 154, +0.048; normal 509, -0.023
- `VOL-C`: contracting 129, -0.153; expanding 154, +0.048; normal 479, -0.020

### Verdict: H6.4 is **rejected** (fails: expectancy, profit_factor, consistency, prediction, execution, timeframes)

| Criterion | Result | Detail |
|---|---|---|
| data | pass | 24 of 24 months with sufficient data (at least 18 needed) |
| expectancy | **fail** | pooled -0.006 R/trade after costs |
| trade_count | pass | 663 trades (screen 100) |
| profit_factor | **fail** | pooled PF +1.04 |
| consistency | **fail** | 11 of 24 months positive (need 15); halves -8.07 / +3.83 R; without the best month (2025-10) -13.33 R |
| prediction | **fail** | candidate -0.006 R/trade; control in contracting volatility -0.153 (129 trades) vs outside -0.003 (633 trades) R/trade |
| execution | **fail** | x2 -0.036; x3 -0.066 R/trade |
| timeframes | **fail** | M5 -0.006; M15 -0.014 R/trade (659 trades) |

Control (comparison only, never selected): rejected. The control is reported for comparison only and is never selected instead of H6.4.

