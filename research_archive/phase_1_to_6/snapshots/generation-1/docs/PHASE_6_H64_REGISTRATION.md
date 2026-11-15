# GoldScalp AI: Phase 6 H6.4 Registration

**Registered on 15 November 2026, after the Phase 6 development results and before any sealed-data result existed.**
Definition `sealed@1`; machine-readable version: `config/discovery/phase6-h64.json`. Historical simulation only: no
orders, no real money, and Phase 4 and its monitors are untouched. H6.1–H6.3 and their frozen files are preserved
unchanged, and none of them is sent to the sealed data.

## 1. Where the earlier hypotheses stand

| Hypothesis | Decision (development data, January – 13 November 2026) |
|---|---|
| H6.1 Volatility regimes | Not supported as originally predicted. |
| H6.2 H1 change of character | Rejected: the extra structure condition reduced performance (+0.062 vs +0.101 R/trade for the control). |
| H6.3 Trading sessions | Inconclusive: too few observations (22 London-open trades). |
| VOL-C, plain continuation | Strong development result (+0.102 R/trade, 294 trades, 9 of 11 months positive). It needs independent validation. |
| London-open pattern | An exploratory observation only. It is not part of H6.4. |

## 2. H6.4: Trend continuation outside contracting volatility

- **Hypothesis:** H1-trend continuation with an M15 break of structure produces positive expectancy when M15 volatility
  is not contracting.
- **Origin (disclosed):** H6.4 was generated from the Phase 6 development results after five configurations and their
  breakdowns had been seen. VOL-C's contracting-volatility trades made −0.171 R/trade over 31 trades. These figures are
  the reason for the hypothesis, not evidence for it. H6.4 is a new hypothesis, not a correction of H6.1.
- **Candidate `H6.4`:** exactly VOL-C plus one condition, "M15 volatility normal or expanding". The full rules:
  - entry: H1 structure in the trade direction, a fresh M15 BOS within 2 candles, spread ≤ $0.50, and M15 volatility
    not contracting;
  - exit: stop beyond the unbroken M15 swing (0.1 ATR buffer), 2R target, at most 240 minutes;
  - 0.02 lots;
  - invalidation: the stop.

  There is no London-session filter or any other development-derived change.
- **Control `VOL-C`:** the unfiltered baseline, fingerprint-identical to the frozen Phase 6 VOL-C. It is reported side
  by side as a **comparison only** and is never selected instead of the candidate.
- **Prediction:** on the sealed data,
  1. the candidate's pooled M5 expectancy after costs is above zero;
  2. the control's trades in contracting M15 volatility do worse than its trades outside contracting volatility.

## 3. Data

| Purpose | Data | Rule |
|---|---|---|
| Development | January – 13 November 2026 | One debugging run of the candidate and control. The result is disclosed and does **not** decide whether the sealed test runs. |
| Sealed validation | November 2023 – October 2025 (24 months), series `csv:vantage-mt5-xauusd-sealed-2023-2025` | Scored months only. Earlier candles, from mid-October 2023, are indicator warm-up. Runs on M5 and M15 execution. |
| Forward | From the market open after 13 November 2026 | Untouched; reserved for any later confirmation. |

## 4. Coverage rule and the December 2024 resolution

- **Threshold:** a month is sufficient at ≥ 95 % coverage plus the five-day warm-up. The threshold is unchanged.
- **Expected bars:** these follow the market calendar fixed in the registration. A *missing* bar inside a listed holiday
  closure is not expected; a present bar always counts.
  - Full closures (Christmas Day, New Year's Day, Good Friday): window [D−1 22:00, D+1 00:00) UTC.
  - Early closes (US holidays, Christmas Eve, New Year's Eve): window [D 17:00, D+1 00:00) UTC.
- **Scope:** the rule applies to every sealed month alike.
- **December 2024:** it measured 94.4 % (M15) and 94.5 % (H1) against the plain schedule.
  - Every missing bar falls in the Christmas Eve early close (from 18:45 UTC), Christmas Day (through the 26 December
    reopen hour) and the New Year's Eve early close. There are no other gaps.
  - Against the market calendar it is fully covered.
  - This was resolved from timestamps alone, before any result existed.

## 5. Evaluation (unchanged from the Phase 6 protocol)

These are the frozen Phase 6 sealed criteria:

- **Data:** at least 18 of 24 months valid.
- **Expectancy:** pooled > 0 R per trade after costs.
- **Trade count:** at least 100 trades.
- **Profit factor:** pooled PF ≥ 1.1.
- **Consistency:** positive in at least 60 % of valid months, positive in each 12-month half, and positive without the
  best month.
- **Prediction:** the H6.4 prediction holds.
- **Execution:** still positive with spread and slippage ×2.
- **Timeframes:** M15 positive as well.

**Classes:**
- *promising*: every criterion passes, leading to further research;
- *inconclusive*: positive, but a criterion fails;
- *rejected*: pooled expectancy ≤ 0.

**Fixed for this test:** costs are the frozen Phase 5 cost model. The test runs **once**; a technically failed run with
no results may be restarted, and every attempt is reported. H6.4 is never retuned on its sealed result.
