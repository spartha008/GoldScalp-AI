# GoldScalp AI: Pre-Phase 7 External Strategy Research and Screening

**15 November 2026.** Research and historical simulation only.
- No strategy was promoted, monitored, linked to demo execution or labelled profitable.
- Phase 7 has not started.

> **Update, 16 November 2026:** stage 2 built the missing tools and screened the 11 concepts that could not be tested
> here. See [PRE_PHASE_7_STAGE_2_SCREENING_REPORT.md](PRE_PHASE_7_STAGE_2_SCREENING_REPORT.md). This report is left as
> written on 15 November.

**Short answer:** no externally documented concept qualified as WORTH FURTHER RESEARCH on the screen.
- 3 of 14 concepts could be built with GoldScalp's current tools and were screened. All 3 are INCONCLUSIVE.
- 9 more are INCONCLUSIVE because they need indicators GoldScalp does not have yet.
- 2 are REJECTED on their own sources' evidence or because the data does not exist.

The concepts most worth a proper look in Phase 7 depend on time-of-day and opening-range rules, which the engine
cannot express yet (section 9). A 10-day screen proves nothing either way.

## 1. Sources researched

A research pass read the following. The five PDFs marked as read were read in full. The books (Crabel, Connors &
Raschke, Connors & Alvarez, Bollinger, Carter) were **not** read: their rules come from secondary summaries and are
flagged as such.

| # | Concept | Main sources | Source type |
|---:|---|---|---|
| 1 | Asian-range London breakout | The Right Trader, "London Breakout Strategy" (therighttrader.com); S. Pinjori, "London Breakout on Gold: 3.7 Years of Backtests", MQL5 blog 2026 (mql5.com/en/blogs/post/777540) | educational (FX); promotional (sells an EA) |
| 2 | Crabel stretch ORB + NR4/NR7 | T. Crabel 1990 (book); Oxford Capital "Narrow Range" (oxfordstrat.com); Holmberg, Lönnbark & Lundström, *Finance Research Letters* 2013 (crude oil) | book (secondary); research blog; academic |
| 3 | 5-minute ORB in the first candle's direction | Zarattini & Aziz, SSRN 4416622 (2023); Zarattini, Barbon & Aziz, SSRN 4729284 (2024, read); T. Krueger replication, MQL5 blog 2026 | academic working papers; independent replication by a tool vendor |
| 4 | Noise-area intraday momentum | Zarattini, Aziz & Barbon, "Beat the Market" (SSRN 2024/25, read) | academic working paper (SPY) |
| 5 | First-half-hour to last-half-hour momentum | Gao, Han, Li & Zhou, *J. Financial Economics* 2018 (working paper read); Xu et al., *Resources Policy* 2020 (GLD) | peer-reviewed |
| 6 | VWAP trend | Zarattini & Aziz, SSRN 4631351 (2023), via Concretum summary and QuantConnect reproductions | academic working paper |
| 7 | Previous-day high/low false-breakout fade (and 80-20) | Unger Academy, "Intraday Strategies on Gold that Trade Against the Trend"; Connors & Raschke, *Street Smarts* (1995) | promotional; book (secondary) |
| 8 | Turtle Soup failed 20-bar breakout | Connors & Raschke, *Street Smarts*; StockCharts (2018) | book (secondary); educational |
| 9 | RSI(2) extreme mean reversion | Connors & Alvarez 2008 (book); StockCharts ChartSchool; Z.K. Chen, MQL5 article 2025 | book (secondary); educational |
| 10 | Bollinger %b mean reversion | Connors & Alvarez (book, secondary); Lento, Gradojevic & Wright, *Applied Financial Economics Letters* 2007 | book; academic |
| 11 | Bollinger / TTM squeeze breakout | Bollinger 2001; Carter 2005/2012 (books); StockCharts ChartSchool | books; educational |
| 12 | EMA trend-pullback scalp; 5/20/200 MA scalp | J. Stanley (DailyFX), IG; IG "Four simple scalping trading strategies" | broker education |
| 13 | 5-minute MA crossovers on gold; RSI and Keltner on gold futures | Urquhart et al., SSRN 2652637 (2015, read); G. Cohen, *Chaos, Solitons & Fractals* 2022 | academic; peer-reviewed |
| 14 | Gold time-of-day; short before the London fix | Donati & Jung, CBS MSc thesis 2019 (read); Caminschi & Heaney, *J. Futures Markets* 2014 | student thesis; peer-reviewed context |

**Evidence quality overall:**
- **Gold-specific intraday evidence is scarce.** It comes from #1 (vendor), #7 (vendor), #13 (data-mined or in-sample)
  and #14 (a thesis, with the effect decaying).
- **The strongest evidence is for US equity ETFs** (#3, #4, #5, #6).
- **Many published results leave out spread and slippage.** The one independent replication found (#3) shows the edge
  vanishing net of costs.

## 2. Strategies and their original rules

Full rules, sources and evidence are recorded per concept in `config/research/pre_phase7/candidates.json`. In short:

1. **Asian-range London breakout (FX source).**
   - Range: 00:00–07:00 UTC on M15. Skip the day if the range is outside 0.25–0.80× daily ATR(14).
   - Entry: the first M15 close beyond the range between 07:00 and 10:00 UTC.
   - Exits: stop at the opposite side of the range, target 1R, exit at 12:00 UTC; one trade a day.
   - The gold vendor version uses a 03:00–08:00 server-time range, a 2.5R target, break-even at +1.5R and a 21:00 exit.
2. **Crabel stretch breakout.**
   - Stretch = 10-day SMA of min(High−Open, Open−Low).
   - Entry: stops at Open ± Stretch; trade after NR4/NR7 days (daily bars).
3. **5-minute ORB.**
   - Direction: the first 5-minute bar from 09:30 ET; trade with its close direction.
   - Stop at that bar's opposite extreme; target 10R, otherwise exit at the close.
4. **Noise area.**
   - Bands = the open × (1 ± the 14-day average absolute move by time of day).
   - Checked every 30 minutes: long above the upper band, short below the lower.
5. **Intraday momentum.** The sign of the overnight-to-10:00 return decides a 15:30–16:00 trade.
6. **VWAP trend.** Long above session VWAP, short below; always in the market.
7. **Previous-day fade (Unger, M15).**
   - Rules: break below the previous day's low and close back above it → long; the mirror for shorts.
   - Flat by the end of the day.
   - Stop and target: **not specified by the source**.
8. **Turtle Soup.**
   - A new 20-bar low, with the previous 20-bar low at least 4 bars old.
   - Buy back above the prior low; stop below today's low.
   - Target: not specified.
9. **RSI(2).**
   - Long when RSI(2) ≤ 5–10 above the 200 SMA (shorts mirrored).
   - Exit on a close beyond the 5 SMA; no stop.
10. **Bollinger %b.**
    - Long when %b < 0.2 for 3 bars above the 200 SMA.
    - Exit when %b > 0.8.
11. **Squeeze.**
    - Bollinger bands inside the Keltner channel, then "fire"; direction from momentum.
    - Stop: not specified.
12. **EMA pullback (IG/DailyFX).**
    - Trend: H1 8 EMA above the 34 EMA, with price above both.
    - Entry: on M5, after a pullback to the 8 EMA, when momentum resumes.
    - Stop beyond the swing; target not specified.
13. **5-minute MA crossover on gold.**
    - Long when MA(short) is above MA(long), always in the market.
    - The standard (1, 50–200) settings are unprofitable even before costs.
14. **Gold time-of-day.**
    - Long 11:00→02:00 New York, short 02:00→11:00.
    - Short in the half hour before the London fixes.

## 3. Why each was selected or not

| # | Key | Decision before screening | Reason |
|---:|---|---|---|
| 1 | P7-ARB | **Screened** | Gold-relevant, with objective rules. Approximated with completed-session levels (deviations below) |
| 7 | P7-PDF | **Screened** | The trigger maps exactly onto the engine's previous-day liquidity sweep |
| 12 | P7-EMA | **Screened** | Objective enough after mapping; the nearest EMA pair is available |
| 2 | P7-CRB | not screened | Needs open, stretch and NR4/NR7 levels; the documented version is a multi-day swing system |
| 3 | P7-ORB5 | not screened | Needs a first-N-minute opening range. An independent replication found the net edge ≈ 0 |
| 4 | P7-NOISE | not screened | Needs time-of-day volatility bands; SPY-only evidence |
| 5 | P7-HALF | not screened | Needs time-of-day returns and clock-time entries; mixed results on GLD |
| 6 | P7-VWAP | not screened (REJECTED) | Needs real traded volume, which spot XAUUSD does not have; extremely cost-sensitive |
| 8 | P7-TSOUP | not screened | Needs 20-bar extremes. Its nearest analogues were already tested (VOL-F, R3, R8: rejected or not supported) |
| 9 | P7-RSI2 | not screened | Needs RSI(2) extremes and SMAs; the builder has only RSI(14) confirmation |
| 10 | P7-PCTB | not screened | Needs Bollinger %b. The academic evidence is negative after costs |
| 11 | P7-SQZ | not screened | Needs Bollinger and Keltner bands. The analogue R9 was rejected |
| 13 | P7-MAX | not screened (REJECTED) | Cannot be expressed (always-in-market crossovers). The source shows the standard rules unprofitable even before costs; the best rules are data-mined |
| 14 | P7-TOD | not screened | Needs clock-time entries and exits. The net effect is small and decaying |

## 4. Implementation

The three screened concepts are separate research configurations (`strategy@1`) in
`config/research/pre_phase7/candidates.json`.
- **Nothing was added to the application.** None qualified, so no strategy was created in Strategy Lab and nothing is
  monitored.
- **Harness:** `goldscalp/research/screening.py`, using the same bt@1 engine, the monthly runner (so the forward-data
  guard applies) and the frozen Phase 5 cost model.
- **Tests:** `tests/test_prephase7_screening.py`.

| Key | Fingerprint | Conditions | Exit |
|---|---|---|---|
| P7-ARB | `1b9a0b61f1d1` | M15 confirmed break of a completed session high/low (within 1 candle); London without the NY overlap; spread ≤ 0.50 | stop beyond the last M15 swing (+0.1 ATR), 1R, 300 min |
| P7-PDF | `48dd720da76a` | M15 sweep of the previous-day high/low (confirmed within 1 candle); spread ≤ 0.50 | stop beyond the last M15 swing (+0.1 ATR), no target, 360 min |
| P7-EMA | `07b47aa69fc8` | H1 EMA 9 vs 50 (price and fast EMA beyond the slow one); M5 higher-low / lower-high pullback; fresh M5 BOS within 2 candles; spread ≤ 0.50 | stop beyond the last M5 swing (+0.1 ATR), 2R, 240 min |
| BASE-A2 | `c7bc3da5f422` | archived Example A v2 (documented benchmark, unchanged) | as archived |
| BASE-VOLC | `64d2a3b37538` | archived Phase 6 VOL-C (documented benchmark, unchanged) | as archived |

### Deviations from the source rules

**P7-ARB**
- The engine's "session" levels are the most recent *completed* Asian session (00:00–09:00 UTC), London session or
  New York session, not a 00:00–07:00 range. Asian levels therefore exist only from 09:00 UTC.
- The stop is at the last M15 swing, not the opposite side of the range.
- There is no range-size filter.
- Entries run until 12:00 UTC instead of 10:00, and there is no one-trade-per-day limit.

**P7-PDF**
- The source specifies no stop or target. A structure stop and a 6-hour limit stand in for "flat by the end of the day".
- Spot XAUUSD and the broker's previous day are used instead of CME futures.

**P7-EMA**
- EMAs 9/50 replace 8/34.
- "Pullback to the 8 EMA, then momentum returns" is expressed as an M5 pullback followed by a fresh M5 break of
  structure.
- The source specifies no target. GoldScalp's standard 2R and 240 minutes are used, for comparability with the
  baselines.

### Decision rules, declared before any result

| Class | Rule |
|---|---|
| INCONCLUSIVE | fewer than 15 M5 trades |
| REJECTED | M5 expectancy ≤ 0 after costs |
| WORTH FURTHER RESEARCH | M5 expectancy > 0, PF ≥ 1.1, still positive without its best day, positive with costs ×2, and M15 execution not negative |
| INCONCLUSIVE | any other positive result |

## 5. Screening data, dates and trade counts

- **Data:** research-store series `csv:vantage-mt5-xauusd` (Vantage MT5 exports), development data already used in
  Phases 5–5.2. Not the sealed 2023–2025 block, and not forward data.
- **Window:** **5 October 2026 00:00 UTC to 19 October 2026 00:00 UTC**: 14 calendar days and 10 trading days, plus
  the Sunday-evening open on 18 October.
- **Candles in the window:** M5 2,760; M15 920; M1 13,778; H1 230. A 5-day look-back provides indicator history.
- **Execution:** M5 and M15. Each is a separate hypothetical $1,000 account at 0.02 lots.
- **Costs:** market spread (0.30 configured fallback), commission $3.50 per lot per side, slippage $0.05.

| Key | M5 trades | M15 trades | Days with a trade (M5) |
|---|---:|---:|---:|
| P7-ARB | **0** | 0 | 0 |
| P7-PDF | 8 | 8 | 8 |
| P7-EMA | 22 | 16 | 11 |
| BASE-A2 | 30 | 33 | 11 |
| BASE-VOLC | 15 | 15 | 8 |

## 6. Backtest metrics (R after costs)

| Key | TF | Trades | Win rate | R/trade | PF | Sum R | Max DD (R) | Net USD | R/trade, costs ×2 | ×3 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P7-PDF | M5 | 8 | 37.5% | +0.355 | 1.41 | +2.84 | 3.04 | +42.35 | +0.323 | +0.291 |
| P7-PDF | M15 | 8 | 37.5% | +0.355 | 1.41 | +2.84 | 3.04 | +42.35 | +0.323 | +0.291 |
| P7-EMA | M5 | 22 | 40.9% | +0.013 | 1.13 | +0.29 | 4.48 | +27.68 | −0.024 | −0.061 |
| P7-EMA | M15 | 16 | 43.8% | +0.120 | 1.26 | +1.91 | 3.04 | +45.09 | +0.086 | +0.053 |
| BASE-A2 | M5 | 30 | 20.0% | −0.376 | 0.22 | −11.28 | 11.50 | −249.40 | −0.406 | −0.437 |
| BASE-A2 | M15 | 33 | 27.3% | −0.310 | 0.28 | −10.22 | 12.50 | −202.74 | −0.365 | −0.421 |
| BASE-VOLC | M5 | 15 | 40.0% | −0.034 | 0.72 | −0.51 | 1.86 | −81.02 | −0.050 | −0.067 |
| BASE-VOLC | M15 | 15 | 40.0% | −0.034 | 0.72 | −0.51 | 1.86 | −81.02 | −0.050 | −0.067 |

P7-ARB had no trades, so it has no metrics.

### By day (M5, R after costs)

| Key | 05 | 06 | 07 | 08 | 09 | 12 | 13 | 14 | 15 | 16 | 18 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P7-PDF | +2.75 | −1.01 | — | — | −1.01 | −1.01 | **+4.32** | −1.01 | +0.82 | −1.01 | — |
| P7-EMA | −1.01 | +0.96 | +1.83 | −1.05 | +0.97 | −0.29 | −2.04 | −0.45 | +0.25 | −0.86 | +1.97 |
| BASE-A2 | −0.97 | −1.76 | −1.06 | −1.01 | +0.34 | −1.29 | −3.45 | −0.15 | −0.60 | −1.51 | +0.18 |
| BASE-VOLC | — | −1.18 | +1.67 | — | +0.30 | +0.31 | −1.01 | +0.38 | −1.00 | +0.04 | — |

### Concentration

**P7-PDF**
- The best day (13 October, +4.32 R) is 55% of all its gains; without it the result is **−1.49 R**.
- 3 of 8 days were positive.
- Its entries cluster in the early Asian hours, right after the 17:00 New York rollover that defines the previous day.

**P7-EMA**
- The best day (18 October, +1.97 R) is 33% of all its gains; without it the result is **−1.68 R**.
- 5 of 11 days were positive.

## 7. Classifications

| Key | Concept | Classification | Why |
|---|---|---|---|
| P7-ARB | Asian-range London breakout | **INCONCLUSIVE** | 0 trades (see below) |
| P7-PDF | Previous-day false-breakout fade | **INCONCLUSIVE** | Only 8 trades. Positive, but entirely from two days; negative without its best day |
| P7-EMA | EMA trend-pullback scalp | **INCONCLUSIVE** | +0.013 R on 22 trades is break-even. It fails "positive without its best day" and "positive with costs ×2" |
| P7-CRB | Crabel stretch breakout | **INCONCLUSIVE** | Not screened: needs open, stretch and NR levels |
| P7-ORB5 | 5-minute ORB | **INCONCLUSIVE** | Not screened: needs an opening-range level |
| P7-NOISE | Noise-area momentum | **INCONCLUSIVE** | Not screened: needs time-of-day bands |
| P7-HALF | Intraday momentum | **INCONCLUSIVE** | Not screened: needs time-of-day returns |
| P7-TSOUP | Turtle Soup | **INCONCLUSIVE** | Not screened: needs 20-bar extremes (analogues already rejected) |
| P7-RSI2 | RSI(2) reversion | **INCONCLUSIVE** | Not screened: needs RSI(2) and SMAs |
| P7-PCTB | Bollinger %b reversion | **INCONCLUSIVE** | Not screened: needs Bollinger %b |
| P7-SQZ | Squeeze breakout | **INCONCLUSIVE** | Not screened: needs Bollinger and Keltner bands |
| P7-TOD | Gold time-of-day | **INCONCLUSIVE** | Not screened: needs clock-time entries and exits |
| P7-VWAP | VWAP trend | **REJECTED** | Unsuitable for the data: no real volume for spot gold |
| P7-MAX | 5-minute MA crossover on gold | **REJECTED** | Its own source: standard rules unprofitable even before costs; the best rules are data-mined |

**WORTH FURTHER RESEARCH: none.** No candidate was added to GoldScalp, promoted or monitored.

### P7-ARB's zero trades

I checked the wiring before classifying it.
- **The setup:** the engine's *confirmed, accepted* break of a completed session level is the trigger.
- **What happened:** that trigger fired only **4 times in the 10 days at any hour** (01:00, 04:00, 12:00 and 20:00 UTC),
  and never in London hours outside the New York overlap.
- **What it means:** this is a mismatch between the source rule ("first M15 close beyond a 00:00–07:00 range") and what
  the engine can express, not a performance result.
- **What I did not do:** loosen the rules to manufacture trades. A faithful test needs a clock-time range level.

## 8. Limitations and overfitting risk

- **Ten trading days prove nothing.** Even the best case here (8 or 22 trades) cannot tell an edge from noise, and these
  results must not be read as validation.
- **The data is spent.** The window had already been used in Phases 5–5.2, so it is development data, not independent
  evidence. One market regime (mid-October 2026) is all it covers.
- **Approximations change the strategies.**
  - The P7-ARB and P7-EMA mappings differ materially from their sources.
  - Exit rules the sources left unspecified were filled with GoldScalp defaults, not tuned.
  - A faithful implementation could behave differently.
- **Selection bias.** Only concepts the current builder could express were screened. That favours structure-based ideas
  similar to those already rejected in Phases 5–6.
- **Overfitting guard.** Rules and decision thresholds were fixed before any result. No parameter was changed after
  seeing results. The ARB diagnostic changed nothing and only located the failing condition.
- **Source quality.** Most gold evidence is from vendors or is in-sample. Published results often exclude spread and
  slippage, which for scalping is usually decisive.

## 9. Best candidates for further investigation (Phase 7 shortlist)

None earned this on its screening result. These are ranked on **source evidence and implementability**, all for a
longer test on spent development data first:

1. **Asian-range London breakout (#1).**
   - Why: the most gold-specific concept, with objective rules and a (vendor) out-of-sample claim.
   - What it needs: a *clock-time range* level (for example 00:00–07:00 UTC high and low), a range-size filter and an
     opposite-range stop. Today's approximation failed for implementation reasons, not performance.
2. **Gold time-of-day and intraday momentum (#14, #5).**
   - Why: gold-specific and peer-reviewed evidence, net of spread in #14, though the effect is decaying.
   - What it needs: clock-time entries and exits and time-of-day returns.
3. **Previous-day false-breakout fade (P7-PDF).**
   - Why: positive on 8 trades, but driven by two days.
   - What it needs: a longer screen on spent data to see whether it is more than noise. Its rules can be kept exactly as
     registered here.
4. **Opening-range breakout (#3).**
   - Lower priority: an independent replication found the net edge ≈ 0, and gold needs a chosen anchor.
5. **EMA trend pullback (P7-EMA).**
   - Lowest priority: break-even after costs, and close to the archived R1 (also break-even).

Supporting these needs a small set of new, tested building blocks:
- clock-time range levels;
- clock-time entry and exit;
- RSI(n) thresholds;
- SMA;
- Bollinger and Keltner bands.

Each should be added once and validated before any strategy uses it. That is a Phase 7 engineering decision and was
**not** started here.

## 10. Protections confirmed unchanged

| What | State |
|---|---|
| Forward-data guard | Unchanged and locked (`forward-guard@1`, boundary 2026-11-14T00:00:00Z). No unlock protocol was created (`config/research/forward-unlock/` does not exist). Every screen read stopped on 19 October 2026 |
| Sealed block | Not used: the Phase 6 2023–2025 data was not read |
| Frozen protocols | The Phase 5 closure, Phase 5.3, Phase 6 and H6.4 checks all report unchanged |
| Archives | `archive/generation-1` and `research_archive/phase_1_to_6` are unchanged and read-only |
| Phase 4 | Monitors 1–3 stopped; A, B and C archived; nothing reactivated. Demo execution unchanged (your setting: on, 0 links, 0 open) |
| Application and research code | Nothing existing was modified, only files added: `goldscalp/research/screening.py`, `config/research/pre_phase7/candidates.json`, `research/pre_phase7/screening-results.json`, `tests/test_prephase7_screening.py` and this report. Git shows no change to the guard, the API, the research service, the live monitor or the Phase 6 configuration |
| Tests | The new screening tests and the forward-guard tests: 20 passed |

**Stopping here, as instructed.** Phase 7 waits for your approval.
