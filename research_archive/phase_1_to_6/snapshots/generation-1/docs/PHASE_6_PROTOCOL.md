# GoldScalp AI: Phase 6 Protocol: New Strategy Discovery

**Pre-registered on 15 November 2026, before any Phase 6 rule was implemented or run.** Definition `discovery@1`.
Historical simulation and paper research only: no orders, no real money, MT5 never contacted by research code, and
Phase 4 unchanged. The machine-readable version is `config/discovery/phase6.json`. Both files are frozen by hash
(`python -m goldscalp.monthly.discovery_report --freeze`), and trials are refused while either has changed.

## 1. Starting point

- **Phase 5 strategy cycle: closed and preserved unchanged.**
  - R2 v2, R5 v2 and R7 v2 are rejected.
  - R1 v2 is inconclusive, about break-even.
  - The original strategies are kept as benchmarks.
  - Failed strategies and their results are kept as evidence.
  - Phase 4 is untouched.
- **Phase 6 is not another round of tuning.** It asks three genuinely different research questions. None of them is
  assumed to work.

## 2. The three hypotheses

| ID | Question | Hypothesis | Baselines |
|---|---|---|---|
| H6.1 Volatility regime | Do different market conditions require different strategy behaviours? | Trend continuation works in expanding M15 volatility and fails in contracting volatility; a liquidity-sweep reversal behaves the opposite way. The regime changes the **sign** of each behaviour's expectancy. | VOL-C (continuation), VOL-F (reversal) |
| H6.2 Market structure | Can structural changes add information beyond the existing entry conditions? | M15 continuation entries in the first 12 H1 candles after an H1 change of character do better than the same entries when H1 merely trends. | STR-X (after an H1 CHoCH), STR-0 (control: H1 trend only) |
| H6.3 Sessions | Are there repeatable session differences that persist across months? | Confirmed M15 breaks of session highs/lows around the London open keep going; the same breaks in the Asian session fail. The difference persists month after month. | SES-B (all sessions, each trade labelled by session) |

**Rules** (all baselines: spread at most $0.50, 0.02 lots, next-open fills, stop-first on ambiguous candles, and the
Phase 5 cost model):

| Config | Entry | Exit | Invalidation |
|---|---|---|---|
| VOL-C | H1 structure with the trade, plus an M15 BOS with it within 2 candles | Stop beyond the M15 swing (0.1 ATR buffer); 2R target; at most 240 min | Stop |
| VOL-F | M15 sweep of equal highs/lows, previous-day or session liquidity within 3 candles, plus an M5 CHoCH in the reversal direction within 3 | Stop beyond the M5 swing; 1.5R target; at most 120 min | Stop |
| STR-X | H1 CHoCH with the trade within 12 H1 candles, plus an M15 BOS with it within 2 | Stop beyond the M15 swing; 2R target; at most 480 min; exit when H1 flips against the trade | H1 flipping back, or the stop |
| STR-0 | As STR-X, but the H1 condition is only "H1 structure with the trade" | As STR-X | As STR-X |
| SES-B | A confirmed M15 break of a session high (long) or low (short) within 2 candles | Stop beyond the M15 swing; no target; at most 360 min | Stop |

**Predictions** (checked automatically on the baseline trial):

- **H6.1:**
  - VOL-C does better in expanding than in contracting volatility.
  - VOL-F does better in contracting than in expanding volatility.
  - The two favoured cells are each positive after costs, with at least 20 trades.
- **H6.2:**
  - STR-X does better than STR-0.
  - STR-X is positive after costs, with at least 60 trades.
- **H6.3:**
  - London-open trades (Asian/London overlap plus London) are positive after costs, with at least 60 trades.
  - They do better than Asian-session trades.
  - They are positive in at least 60 % of the development months that have at least 5 such trades.

## 3. Data: development, sealed and forward

| Purpose | Data | Rule |
|---|---|---|
| Development | January to 13 November 2026: January–May from the research-store export, 22 June–13 November from the app's MT5 history | Used for debugging and refinement only. It was already examined in Phases 5.2 and 5.3, so using it costs no independence. |
| Sealed validation | November 2023 to October 2025 (24 months) of Vantage MT5 M5, M15 and H1, plus one month of warm-up | Exported once into its own research-store series. No Phase 6 trial may read it, including look-back and higher-timeframe history; the code enforces this. It is opened once per frozen hypothesis. |
| Forward confirmation | Everything from the market open after 13 November 2026 | Never used for development; it is the final confirmation for anything that passes sealed validation. |

The H1 history from October 2021 in the application database has been viewed only as monthly and yearly candles on
the dashboard. No strategy has ever been run on November 2023 to October 2025.

## 4. Process for every hypothesis

1. The hypothesis is written down before implementation (this document).
2. Entry, exit and invalidation are defined (section 2).
3. **The simple baseline runs first**, on development data only.
4. **Refinement is limited:**
   - at most three refinements per hypothesis, so four trials in total;
   - each one is built by the tool from a parent configuration, using only that hypothesis's allowed kinds:
     - the filter the hypothesis itself names (a volatility regime for H6.1, a session for H6.3);
     - the stop timeframe;
     - the exit horizon ×0.5 or ×2;
   - each needs a written rationale that cites the development evidence;
   - there are no free-form edits, no parameter grids and no choosing among hundreds of variants;
   - every trial is recorded and counted in the report.
5. **Development gate.** A hypothesis may be frozen for sealed validation only if its prediction holds on the
   development data. Otherwise it is recorded as *not supported at development*, and the sealed data is not spent on it.
6. **Freeze** the final version and these criteria before the sealed data is opened.
7. **Evaluate once on the sealed data and accept the result.** Rejection is a valid outcome.

## 5. Sealed-validation criteria (fixed now, before any data exists in the sealed series)

- **Data:** at least 18 of the 24 months have sufficient data (95 % coverage plus warm-up). Other months are INCOMPLETE
  and excluded.
- **Expectancy:** pooled expectancy after costs > 0 R per trade.
- **Trade count:** at least 100 pooled trades (a screen, not proof).
- **Profit factor:** pooled profit factor of at least 1.1.
- **Consistency:**
  - positive in at least 60 % of valid months;
  - positive in each 12-month half (Nov 2023–Oct 2024 and Nov 2024–Oct 2025);
  - positive without its best month.
- **Prediction:** the hypothesis's own regime, context or session prediction also holds on the sealed data.
- **Execution:** still positive with spread and slippage doubled (tripled is reported only).
- **Timeframes:** M15 execution is positive as well as M5.
- **Classes:**
  - *promising*: every criterion passes;
  - *inconclusive*: positive, but a criterion fails;
  - *rejected*: pooled expectancy ≤ 0.

  These describe historical research candidates, not trading readiness.
- **Next step:** only a promising candidate may go on to read-only paper-monitor observation on forward data. Phase 4
  is never changed.
