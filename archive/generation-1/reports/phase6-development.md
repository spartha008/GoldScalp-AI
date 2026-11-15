# GoldScalp AI: Phase 6 development report

> Historical simulation on development data only (January to 13 November 2026). Separate hypothetical $1,000 accounts per configuration and month; no orders. Nothing here is validated: the sealed block (November 2023 to October 2025) and forward data are untouched by every trial below.

Protocol: discovery-protocol-frozen.json (frozen 2026-11-15T09:39:26.763129+00:00, unchanged)

## H6.1 Volatility regime decides the behaviour

**Question.** Do different market conditions require different strategy behaviours?

**Hypothesis.** Trend continuation works when M15 volatility is expanding and fails when it is contracting; a liquidity-sweep reversal behaves the opposite way. The sign of each behaviour's expectancy depends on the regime (an interaction), not just its size.

Trials used: 1 of 4.

### Trial 1: baseline `dt_82de40984a99` (COMPLETED)

| Configuration | Trades | R/trade | PF | Sum R | Max DD R | x2 costs | Months valid |
|---|---:|---:|---:|---:|---:|---:|---:|
| `VOL-C` | 294 | +0.102 | 1.53 | 30.02 | 6.98 | +0.090 | 11 |
| `VOL-F` | 129 | -0.012 | 0.93 | -1.55 | 8.48 | -0.033 | 11 |

By M15 volatility regime (trades, R/trade):

| Configuration | contracting | expanding | normal |
|---|---:|---:|---:|
| `VOL-C` | 31, -0.171 | 64, +0.065 | 199, +0.157 |
| `VOL-F` | 17, +0.088 | 34, -0.106 | 78, +0.007 |

By session (trades, R/trade):

| Configuration | Asian | London open | New York | none |
|---|---:|---:|---:|---:|
| `VOL-C` | 101, +0.084 | 57, +0.319 | 122, +0.010 | 14, +0.155 |
| `VOL-F` | 35, -0.114 | 25, -0.135 | 46, -0.005 | 23, +0.264 |

By H1 structure (trades, R/trade):

| Configuration | against | ranging | with |
|---|---:|---:|---:|
| `VOL-C` | – | – | 294, +0.102 |
| `VOL-F` | 76, -0.081 | 25, +0.062 | 28, +0.110 |

**Prediction (does not hold on development data).** Expectancy of VOL-C in expanding > in contracting, AND expectancy of VOL-F in contracting > in expanding; the favoured cells (VOL-C expanding, VOL-F contracting) are each positive after costs with at least 20 trades. Observed: VOL-C expanding +0.065 vs contracting -0.171; VOL-F contracting +0.088 vs expanding -0.106 R/trade; favoured cells [64, 17] trades.

_prediction does not hold on development data: refinements may still be tried within the limits; if it never holds, record the hypothesis as not supported and keep the sealed data unspent._

## H6.2 A higher-timeframe change of character adds information

**Question.** Can structural changes provide useful information beyond the existing entry conditions?

**Hypothesis.** An H1 change of character (CHoCH) starts a new directional leg. M15 continuation entries in the new direction during the first 12 H1 candles after it do better than the same M15 entries when H1 merely trends (the condition the earlier strategies used).

Trials used: 1 of 4.

### Trial 1: baseline `dt_be1617009277` (COMPLETED)

| Configuration | Trades | R/trade | PF | Sum R | Max DD R | x2 costs | Months valid |
|---|---:|---:|---:|---:|---:|---:|---:|
| `STR-0` | 241 | +0.101 | 1.56 | 24.35 | 10.96 | +0.089 | 11 |
| `STR-X` | 111 | +0.062 | 1.30 | 6.88 | 4.13 | +0.050 | 11 |

By M15 volatility regime (trades, R/trade):

| Configuration | contracting | expanding | normal |
|---|---:|---:|---:|
| `STR-0` | 25, -0.065 | 58, +0.166 | 158, +0.103 |
| `STR-X` | 11, +0.214 | 32, -0.013 | 68, +0.072 |

By session (trades, R/trade):

| Configuration | Asian | London open | New York | none |
|---|---:|---:|---:|---:|
| `STR-0` | 87, +0.095 | 42, +0.330 | 100, +0.007 | 12, +0.124 |
| `STR-X` | 35, +0.077 | 22, +0.338 | 50, -0.082 | 4, +0.206 |

By H1 structure (trades, R/trade):

| Configuration | against | ranging | with |
|---|---:|---:|---:|
| `STR-0` | – | – | 241, +0.101 |
| `STR-X` | 1, -0.032 | 8, +0.028 | 102, +0.066 |

**Prediction (does not hold on development data).** Expectancy of STR-X > expectancy of STR-0, and STR-X is positive after costs with at least 60 trades. Observed: STR-X +0.062 (111 trades) vs STR-0 +0.101 (241 trades) R/trade.

_prediction does not hold on development data: refinements may still be tried within the limits; if it never holds, record the hypothesis as not supported and keep the sealed data unspent._

## H6.3 Session breaks behave differently and persistently

**Question.** Are there repeatable differences between trading sessions that persist across months?

**Hypothesis.** A confirmed M15 break of a session high or low that happens around the London open (Asian/London overlap or London) tends to keep going for several hours, while the same breaks during the Asian session tend to fail. The difference persists month after month.

Trials used: 1 of 4.

### Trial 1: baseline `dt_fc4b881df136` (COMPLETED)

| Configuration | Trades | R/trade | PF | Sum R | Max DD R | x2 costs | Months valid |
|---|---:|---:|---:|---:|---:|---:|---:|
| `SES-B` | 127 | +0.110 | 1.11 | 14.01 | 9.02 | +0.103 | 11 |

By M15 volatility regime (trades, R/trade):

| Configuration | contracting | expanding | normal |
|---|---:|---:|---:|
| `SES-B` | 7, +0.530 | 57, +0.077 | 63, +0.093 |

By session (trades, R/trade):

| Configuration | Asian | London open | New York | none |
|---|---:|---:|---:|---:|
| `SES-B` | 32, +0.117 | 22, +0.715 | 60, -0.023 | 13, -0.315 |

By H1 structure (trades, R/trade):

| Configuration | against | ranging | with |
|---|---:|---:|---:|
| `SES-B` | 13, +0.132 | 2, +0.637 | 112, +0.098 |

**Prediction (does not hold on development data).** London-open trades are positive after costs with at least 60 trades, better than Asian-session trades, and positive in at least 60 % of the development months that have at least 5 such trades. Observed: London open +0.715 (22 trades) vs Asian +0.117 (32 trades) R/trade; positive in 0 of 0 months with >= 5 trades.

_prediction does not hold on development data: refinements may still be tried within the limits; if it never holds, record the hypothesis as not supported and keep the sealed data unspent._

