# GoldScalp AI: Pre-Phase 7 Stage 2 Screening Report

**16 November 2026.** Research and historical simulation only. During the screen no strategy was promoted, monitored,
linked to demo execution or labelled profitable, and Phase 7 has not started. Stage 1 is in
[PRE_PHASE_7_EXTERNAL_STRATEGY_RESEARCH_REPORT.md](PRE_PHASE_7_EXTERNAL_STRATEGY_RESEARCH_REPORT.md).

**Changed later the same day, at your request:** the two candidates were given a Strategy Lab form, and P7-FIX (with a
protective stop) is now monitored and linked to MT5 demo execution. A longer run over 2026 also shows that P7-FIX lost
money in January to June. See [section 12](#12-addendum-strategy-lab-form-a-longer-history-and-the-demo-link-16-november-2026-after-the-screen).

**Short answer**

Stage 1 could test only 3 of 14 documented concepts. I built the missing tools and screened everything else: 15 rule
sets covering the 11 untested concepts, plus faithful versions of two that stage 1 had only approximated.

- **10-day screen (5–19 October 2026, the classification):** 2 rule sets are WORTH FURTHER RESEARCH, 8 are INCONCLUSIVE
  (fewer than 15 trades) and 5 are REJECTED.
- **11-week context run (17 August – 31 October, no classification)** changes the picture:
  - **Time-of-day rule (P7-TOD):** it passed the screen, but over 11 weeks it made about zero in R and lost $326.
  - **Short before the London fixes (P7-FIX):** it passed the screen and held: +0.20 R per trade over 110 trades. In a
    placebo test its two half hours rank 1st and 2nd of 46.
  - **Gold London breakout (P7-ARBG):** the best rule with real stops: +0.26 R per trade over 52 trades, positive on
    both the buy and sell side. It was flat in October and had only 10 trades in the screen.
  - **Last-half-hour momentum (P7-HALF) and Bollinger %b (P7-PCTB):** mildly positive.
  - **Everything else:** negative or noise.
- **Nothing here is validated.** Every number comes from development data that research had already used, in a market
  that fell about 11 %. A real test needs untouched data under a frozen protocol.

## 1. What was built

| Tool | File | What it does |
|---|---|---|
| Indicator library | `goldscalp/research/indicators.py` | Moving averages (SMA, EMA), RSI, ATR, Bollinger bands and %b, Keltner channels, squeeze momentum; bars of other sizes and daily bars (17:00 New York rollover); clock-time helpers in any timezone; a tick-volume VWAP |
| Rule simulator | `goldscalp/research/rulesim.py` | Runs a rule bar by bar with market orders, stop-entry orders (with one-cancels-other), protective stops, targets, break-even moves, clock-time exits, signal exits and reversals |
| Rule sets | `goldscalp/research/external_rules.py` | The 15 published rule sets, each built from its source's wording |
| Runner | `goldscalp/research/screening_ext.py` | Loads candles through the forward-data guard, runs the screen, the delay check, the context run and the placebo test |
| Declaration | `config/research/pre_phase7/candidates_stage2.json` | Every rule, parameter, window, convention and threshold, written before any result |

The strategy builder and the bt@1 engine were not changed. The simulator is separate because the builder cannot
express clock-time entries and exits, stop-entry orders, indicator exits or reversals.

**How the tools were checked**

- **Same fills and costs as bt@1.** The simulator uses the engine's conventions: bid candles, ask = bid + spread, buys
  fill at the ask plus slippage, commission per lot per side, stop first when a bar touches both stop and target. A test
  compares a trade's entry, exit, net result and cost with the engine's own pricing functions.
- **Replay against bt@1.** I took every bt@1 trade from the 11-week context run (436 trades across five
  configurations) and re-entered each one in the simulator at the same bar, with the same stop, target and time limit.
  All 436 came out with the same exit time and the same net result (largest difference $0.01). Record:
  `research/pre_phase7/stage2-parity.json`.
- **No look-ahead.** For every declared rule, removing all candles after a cut-off time leaves every earlier trade
  unchanged. Slower bars (M15, M30, H1, daily) become visible only after they close.
- **Indicators.** SMA, EMA, RSI, ATR, Bollinger, Keltner and the regression value are checked against hand-worked
  examples.
- **Tests:** `tests/test_prephase7_tools.py`, 16 tests, all passing; Ruff is clean. **Full project suite: 699 passed,
  0 failed.** It ran with the demo-execution default forced off, because your local `.env` turns demo execution on and
  two existing tests check the default.

## 2. What was screened

| Key | Concept | Source (type) | Rules as tested | Main deviations |
|---|---|---|---|---|
| P7-ARB2 | London breakout of the Asian range | The Right Trader (educational, FX) | Range 00:00–07:00 UTC; skip if outside 0.25–0.80× daily ATR(14); first M15 close beyond the range between 07:00 and 10:00 UTC; stop at the other side; 1R; exit 12:00 UTC; one trade a day | Spot gold instead of FX majors |
| P7-ARBG | London breakout on gold | S. Pinjori, MQL5 blog (promotional) | Range 03:00–08:00 server time; at 08:00 a buy stop above and a sell stop below (one cancels the other); stop at the other side, capped at 0.6 %; 2.5R; break-even at +1.5R; close at 21:00; skip if the range is outside 0.30–1.60 % of price | Server clock assumed (New York + 7 h); buffer of $0.10 assumed |
| P7-CRB | Crabel stretch breakout after an NR4 day | Crabel 1990 (book, secondary) | Stretch = 10-day average of min(High−Open, Open−Low); stop orders at Open ± Stretch the day after a narrow-range day; the other order is the stop; exit at the day's end | Exit not verified in the source |
| P7-ORB5 | 5-minute opening-range breakout | Zarattini & Aziz 2023 (academic, QQQ) | First 5-minute bar from 09:30 New York; trade its direction at 09:35; stop at its other end; 10R, else exit 16:00 | The paper's clock kept unchanged for gold |
| P7-NOISE | Noise-area intraday momentum | Zarattini, Aziz & Barbon 2024 (academic, SPY) | Bands = open × (1 ± 14-day average move by time of day); checked every half hour; long above, short below; reverse on the opposite band; exit 16:00 | No stop in the source |
| P7-HALF | First to last half-hour momentum | Gao et al., JFE 2018 (peer-reviewed, SPY) | Return from the previous 16:00 to 10:00 New York decides a 15:30–16:00 trade | No stop in the source |
| P7-VWAP | VWAP trend | Zarattini & Aziz 2023 (academic, QQQ) | Long above the session VWAP, short below, flipping on each cross, flat before the close; M1 bars | Tick volume as a proxy for traded volume |
| P7-TSOUP | Turtle Soup Plus One | Connors & Raschke 1995 (book, secondary) | Failed break of the prior 20-bar extreme (at least 4 bars old); stop order back through it on the next bar; stop beyond the setup bar | Daily rules applied to M15 bars; 2R and 240 minutes assumed |
| P7-RSI2 | RSI(2) mean reversion | Connors & Alvarez; Chen 2025 (book; MQL5 article) | M30: long above the 200 SMA when RSI(2) ≤ 5, short below it when ≥ 95; exit on a close beyond the 5 SMA; 0.15 % stop | Stock-index rules applied to gold |
| P7-PCTB | Bollinger %b mean reversion | Connors & Alvarez (book, secondary) | M15: with the 200 SMA trend, %b beyond 0.2 / 0.8 for 3 bars; exit at the other extreme | Daily ETF rules on M15; no stop in the source |
| P7-SQZ | TTM squeeze breakout | Carter (book, via StockCharts) | M15: Bollinger inside Keltner, then release; direction from the momentum histogram; exit after two bars against | Formula differs by platform; no stop in the source |
| P7-MAX | 5-minute SMA crossover on gold | Urquhart et al. 2015 (academic, gold) | Always in the market: SMA(44) vs SMA(332), the paper's best rule. Its standard rules (1, 50–200) are reported too | No stop in the source |
| P7-TOD | Gold time-of-day | Donati & Jung 2019 (MSc thesis, gold) | Long 11:00 → 02:00 New York, short 02:00 → 11:00 | Spot instead of futures; no stop |
| P7-FIX | Short before the London fixes | Donati & Jung 2019; Caminschi & Heaney 2014 | Short 05:05–05:35 and 09:35–10:05 New York | No stop in the source |
| P7-EMA2 | EMA trend pullback with the source's EMAs | J. Stanley, IG (broker education) | H1 8 EMA above 34 EMA with price above both; M5 close back above the 8 EMA after a pullback; stop beyond the pullback | "Momentum comes back" made objective; 2R and 240 minutes assumed |

**Not screened, with the reason:**
- **IG 5/20/200 MA scalp:** rules incomplete, and M3 bars are not stored.
- **Cohen 2022 RSI and Keltner systems:** parameters are not in the accessible abstract.
- **Connors & Raschke 80-20:** stop not stated; the previous-day fade from stage 1 covers the idea.
- **Relative-volume ORB (Zarattini 2024):** needs traded volume across a stock universe.

## 3. Method

- **Data:** research-store series `csv:vantage-mt5-xauusd`, development data already used in Phases 5–5.2. The sealed
  2023–2025 block and forward data were not read, and the loader refuses both.
- **Screen window (classification):** 5 October 00:00 UTC to 19 October 2026 00:00 UTC, the same 10 trading days as
  stage 1.
- **Context window (supplementary):** 17 August to 31 October 2026, about 11 weeks. I added it because a once-a-day
  rule cannot reach 15 trades in 10 trading days. It classifies nothing and is not validation; the screen window lies
  inside it.
- **Execution:** M5 bid candles (P7-VWAP: M1 in the screen), 0.02 lots, one position at a time.
- **Costs:** the frozen Phase 5 model: candle spread (0.30 fallback), $3.50 commission per lot per side, $0.05 slippage
  per fill. That averages about $0.76 a trade.
- **R:**
  - with a stop, R is the distance from entry to the initial stop;
  - where the source has no stop, R is a **nominal** unit of 1.5 × ATR(14) on M15 at entry (about $10–15 an ounce in
    October), so those R figures are not comparable with stop-based ones. USD results are shown for all.
- **Unspecified details:** target not given → 2R; time exit not given → 240 minutes; buffer not given → $0.10. Daily-bar
  rules without an intraday source run on M15 bars. Clock times are the source's own, daylight-saving aware.
- **No tuning:** no parameter was changed after a result was seen.

**Decision rules, declared before any result**

| Class | Rule (screen window only) |
|---|---|
| INCONCLUSIVE | fewer than 15 trades |
| REJECTED | expectancy after costs ≤ 0 |
| WORTH FURTHER RESEARCH | expectancy > 0, PF ≥ 1.1, still positive without its best day, positive with costs ×2, and not negative when every order is delayed by one bar |
| INCONCLUSIVE | any other positive result |

The one-bar delay replaces stage 1's M15-execution check. These rules are defined on fixed bar sizes and clock times,
so re-running them on M15 bars would change the rules themselves.

## 4. Screen results: 5–19 October 2026

R after costs. "Risk unit" says whether R is stop-based or nominal.

| Key | Trades | Win rate | R/trade | PF | Sum R | Max DD (R) | Net USD | R/trade, costs ×2 | R/trade, 1-bar delay | Risk unit | Positive days | Sum R without best day |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| P7-ARB2 | 6 | 66.7% | +0.036 | 1.30 | +0.21 | 0.40 | +17.42 | +0.028 | +0.039 | stop | 4 of 6 | −0.21 |
| P7-ARBG | 10 | 50.0% | +0.215 | 1.49 | +2.15 | 1.35 | +106.35 | +0.203 | +0.215 | stop | 5 of 10 | −0.34 |
| P7-CRB | 3 | 66.7% | +0.027 | 1.01 | +0.08 | 1.00 | +1.08 | +0.019 | +0.027 | stop | 2 of 3 | −0.84 |
| P7-ORB5 | 10 | 20.0% | −0.298 | 0.66 | −2.98 | 5.22 | −29.68 | −0.385 | −0.222 | stop | 2 of 10 | −6.40 |
| P7-NOISE | 3 | 0.0% | −0.978 | 0.00 | −2.93 | 2.93 | −66.10 | −1.006 | −0.849 | nominal | 0 of 3 | −2.48 |
| P7-HALF | 10 | 70.0% | +0.207 | 4.38 | +2.07 | 0.46 | +41.50 | +0.174 | +0.220 | nominal | 7 of 10 | +0.22 |
| P7-VWAP | 204 | 14.7% | −0.061 | 0.55 | −12.52 | 17.43 | −300.12 | −0.089 | −0.047 | nominal | 4 of 10 | −14.72 |
| P7-TSOUP | 17 | 5.9% | −0.881 | 0.07 | −14.98 | 14.98 | −85.46 | −1.030 | −0.450 | stop | 1 of 8 | −16.90 |
| P7-RSI2 | 20 | 30.0% | −0.019 | 0.98 | −0.38 | 7.18 | −4.15 | −0.069 | −0.012 | stop | 3 of 9 | −7.79 |
| P7-PCTB | 11 | 63.6% | +0.296 | 1.09 | +3.26 | 5.40 | +17.90 | +0.260 | +0.092 | nominal | 6 of 9 | −2.56 |
| P7-SQZ | 25 | 44.0% | −0.048 | 1.06 | −1.20 | 5.67 | +13.88 | −0.082 | −0.016 | nominal | 3 of 11 | −6.81 |
| P7-MAX | 12 | 41.7% | −0.350 | 0.74 | −4.21 | 5.85 | −67.82 | −0.381 | −0.273 | nominal | 4 of 9 | −8.80 |
| P7-TOD | 20 | 55.0% | +0.646 | 1.77 | +12.92 | 9.62 | +281.90 | +0.617 | +0.675 | nominal | 6 of 10 | +4.79 |
| P7-FIX | 20 | 80.0% | +0.216 | 2.11 | +4.31 | 2.02 | +86.04 | +0.185 | +0.227 | nominal | 7 of 10 | +2.08 |
| P7-EMA2 | 88 | 29.5% | −0.194 | 0.69 | −17.02 | 20.44 | −218.48 | −0.266 | −0.297 | stop | 1 of 11 | −19.86 |
| BASE-DRIFT (baseline) | 10 | 50.0% | +0.136 | 1.38 | +1.36 | 12.26 | +96.74 | +0.093 | +0.034 | nominal | 5 of 10 | −5.00 |

BASE-DRIFT is simply long from 00:00 to 21:00 UTC every day. It shows what the market itself did after costs.

The paper's own "standard" crossover rules, reported as its negative controls: SMA(1, 50) −0.077 R over 216 trades;
(1, 100) −0.165 over 167; (1, 150) −0.180 over 140; (1, 200) +0.058 over 72, which turns negative without its best day
and with a one-bar delay.

### By day (sum of R)

| Key | 05 | 06 | 07 | 08 | 09 | 11 | 12 | 13 | 14 | 15 | 16 | 18 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P7-ARB2 | −0.29 | +0.42 | +0.09 | — | — | — | +0.21 | — | +0.17 | — | −0.40 | — |
| P7-ARBG | −1.00 | +0.50 | +2.50 | −1.00 | +0.38 | — | +0.66 | −1.00 | +2.50 | −0.34 | −1.00 | — |
| P7-CRB | — | −1.00 | — | — | +0.92 | — | — | — | — | — | — | +0.16 |
| P7-ORB5 | +1.88 | −1.10 | −1.02 | −1.03 | −1.02 | — | −1.04 | +3.42 | −1.01 | −1.04 | −1.02 | — |
| P7-NOISE | −0.45 | — | — | — | — | — | — | −1.20 | — | — | −1.28 | — |
| P7-HALF | +0.11 | −0.31 | +0.45 | +0.23 | +0.04 | — | −0.46 | +0.16 | +0.00 | +1.85 | −0.02 | — |
| P7-VWAP | −2.35 | +0.77 | +1.12 | −2.51 | −2.83 | — | −6.04 | +0.84 | −1.92 | −1.79 | +2.21 | — |
| P7-TSOUP | — | −2.06 | −4.27 | — | −1.04 | — | +1.92 | −4.26 | −1.06 | −1.09 | −3.12 | — |
| P7-RSI2 | — | −2.04 | −1.02 | −3.06 | +0.74 | +1.49 | −2.28 | −1.02 | +7.41 | −0.61 | — | — |
| P7-PCTB | +1.44 | −1.08 | −4.32 | +0.94 | +0.39 | +2.68 | +0.47 | — | −3.07 | — | +5.82 | — |
| P7-SQZ | −1.20 | −1.02 | — | +5.62 | −0.03 | −1.48 | −2.44 | +1.45 | −1.44 | −0.30 | −1.13 | +0.76 |
| P7-MAX | −4.79 | −0.77 | +1.66 | +4.60 | — | — | −1.70 | −2.60 | — | +1.17 | −2.05 | +0.28 |
| P7-TOD | −3.32 | −6.29 | +4.14 | +4.50 | +2.27 | — | +5.89 | +1.09 | +8.13 | −0.70 | −2.78 | — |
| P7-FIX | +1.47 | +0.18 | −0.09 | +0.88 | +0.14 | — | +0.43 | +2.24 | +0.73 | −0.30 | −1.36 | — |
| P7-EMA2 | −0.60 | −2.31 | −7.33 | +2.83 | −0.58 | — | −0.14 | −3.24 | −2.37 | −0.17 | −2.76 | −0.35 |
| BASE-DRIFT | −0.04 | +5.14 | −7.92 | +2.92 | +6.36 | — | −2.72 | −1.61 | −7.93 | +6.13 | +1.04 | — |

Days are October 2026, by the UTC date of entry. The 11th and the 18th are Sunday-evening opens.

### Classifications

| Key | Classification | Why |
|---|---|---|
| P7-FIX | **WORTH FURTHER RESEARCH** | +0.216 R over 20 trades, PF 2.11, passes every check |
| P7-TOD | **WORTH FURTHER RESEARCH** | +0.646 R over 20 trades, PF 1.77, passes every check (the context run does not support it; see section 5) |
| P7-ARB2 | INCONCLUSIVE | 6 trades |
| P7-ARBG | INCONCLUSIVE | 10 trades |
| P7-CRB | INCONCLUSIVE | 3 trades |
| P7-ORB5 | INCONCLUSIVE | 10 trades |
| P7-NOISE | INCONCLUSIVE | 3 trades |
| P7-HALF | INCONCLUSIVE | 10 trades |
| P7-PCTB | INCONCLUSIVE | 11 trades |
| P7-MAX | INCONCLUSIVE | 12 trades |
| P7-VWAP | REJECTED | −0.061 R over 204 trades |
| P7-TSOUP | REJECTED | −0.881 R over 17 trades (see the caveat in section 7) |
| P7-RSI2 | REJECTED | −0.019 R over 20 trades |
| P7-SQZ | REJECTED | −0.048 R over 25 trades |
| P7-EMA2 | REJECTED | −0.194 R over 88 trades |

These classes come mechanically from the declared rules. I did not change any of them after seeing the context run.

## 5. Context run: 17 August – 31 October 2026

Supplementary, not a classification. Gold fell from about 4,370 to about 3,876 over this window (−11 %), so short
trades had the wind behind them.

| Key | Trades | Win rate | R/trade | PF | Sum R | Max DD (R) | Net USD | R/trade, costs ×2 | Risk unit | Positive days | Sum R without best day |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| P7-ARB2 | 24 | 50.0% | +0.027 | 1.38 | +0.65 | 1.54 | +99.18 | +0.020 | stop | 12 of 24 | −0.35 |
| P7-ARBG | 52 | 48.1% | +0.260 | 1.67 | +13.52 | 5.56 | +734.96 | +0.248 | stop | 25 of 52 | +11.02 |
| P7-CRB | 14 | 28.6% | −0.178 | 0.71 | −2.50 | 7.11 | −241.99 | −0.186 | stop | 4 of 13 | −4.74 |
| P7-ORB5 | 55 | 21.8% | +0.250 | 1.10 | +13.76 | 20.29 | +62.26 | +0.190 | stop | 12 of 55 | +3.79 |
| P7-NOISE | 30 | 46.7% | −0.186 | 1.03 | −5.58 | 12.95 | +13.78 | −0.208 | nominal | 14 of 30 | −9.38 |
| P7-HALF | 54 | 61.1% | +0.087 | 1.70 | +4.67 | 2.91 | +131.92 | +0.060 | nominal | 33 of 54 | +2.82 |
| P7-VWAP (M5) | 456 | 22.8% | −0.059 | 0.81 | −27.09 | 38.60 | −617.24 | −0.083 | nominal | 24 of 55 | −33.12 |
| P7-TSOUP | 93 | 11.8% | −0.693 | 0.30 | −64.44 | 64.44 | −523.80 | −0.816 | stop | 6 of 43 | −67.26 |
| P7-RSI2 | 87 | 35.6% | −0.123 | 0.81 | −10.69 | 17.06 | −140.56 | −0.172 | stop | 14 of 38 | −18.10 |
| P7-PCTB | 61 | 72.1% | +0.218 | 1.20 | +13.31 | 12.64 | +209.12 | +0.190 | nominal | 32 of 45 | +7.49 |
| P7-SQZ | 129 | 38.8% | −0.099 | 0.89 | −12.81 | 25.29 | −177.08 | −0.127 | nominal | 19 of 58 | −19.79 |
| P7-MAX | 72 | 31.9% | −0.084 | 0.92 | −6.07 | 43.71 | −204.12 | −0.112 | nominal | 20 of 46 | −18.75 |
| P7-TOD | 110 | 47.3% | +0.009 | 0.90 | +1.01 | 25.54 | −325.86 | −0.016 | nominal | 30 of 55 | −7.13 |
| P7-FIX | 110 | 63.6% | +0.202 | 2.17 | +22.21 | 3.60 | +526.40 | +0.175 | nominal | 37 of 55 | +19.60 |
| P7-EMA2 | 504 | 33.9% | −0.035 | 0.94 | −17.88 | 66.74 | −242.04 | −0.097 | stop | 23 of 63 | −28.19 |
| BASE-DRIFT (baseline) | 55 | 43.6% | −1.110 | 0.69 | −61.06 | 84.22 | −974.20 | −1.145 | nominal | 24 of 55 | −79.36 |

The paper's standard crossover rules over the context window: SMA(1, 50) −0.062 R over 1,177 trades; (1, 100) −0.069
over 777; (1, 150) −0.022 over 569; (1, 200) +0.026 over 492, which is negative with costs ×2. That matches the paper's
own finding that the standard rules do not pay.

**Stage 1 candidates and the archived benchmarks over the same window** (unchanged rules, bt@1 engine, M5, run in
monthly pieces):

| Key | Trades | Win rate | R/trade | PF | Sum R | Max DD (R) | Net USD | R/trade, costs ×2 | Positive days | Sum R without best day |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P7-ARB (stage 1 approximation) | 5 | 80.0% | +0.355 | 3.35 | +1.78 | 0.48 | +61.22 | +0.341 | 4 of 5 | +0.78 |
| P7-PDF (previous-day fade) | 46 | 28.3% | +0.127 | 0.87 | +5.84 | 18.13 | −95.14 | +0.083 | 12 of 42 | −9.66 |
| P7-EMA (9/50 approximation) | 138 | 41.3% | −0.010 | 0.98 | −1.40 | 13.46 | −43.26 | −0.039 | 22 of 54 | −6.53 |
| BASE-A2 (archived Example A v2) | 166 | 42.2% | −0.130 | 0.82 | −21.58 | 25.18 | −302.84 | −0.154 | 18 of 53 | −25.07 |
| BASE-VOLC (archived VOL-C) | 81 | 50.6% | +0.085 | 1.30 | +6.89 | 3.97 | +382.34 | +0.071 | 25 of 42 | +4.52 |

- **P7-PDF did not hold.** Its R is positive but it lost $95, with a profit factor below 1, an 18 R drawdown, and a
  negative total without its best day.
- **VOL-C is positive here**, as it was on this same 2026 data in Phase 6. It then failed on the sealed 2023–2025 block,
  which is a reminder of what a result on this window is worth.

### Month by month (sum of R, with net USD)

| Key | August (from the 17th) | September | October |
|---|---:|---:|---:|
| P7-FIX | +0.34 R (+$14), 22 trades | +12.33 R (+$300), 44 | +9.54 R (+$212), 44 |
| P7-ARBG | +4.22 R (+$223), 9 | +9.32 R (+$511), 21 | −0.02 R (+$1), 22 |
| P7-HALF | −0.06 R (+$6), 11 | +4.52 R (+$140), 21 | +0.21 R (−$14), 22 |
| P7-PCTB | −1.22 R (+$4), 15 | +11.05 R (+$191), 26 | +3.48 R (+$14), 20 |
| P7-TOD | −22.27 R (−$711), 22 | +22.15 R (+$402), 44 | +1.12 R (−$17), 44 |
| P7-ORB5 | −2.25 R (−$104), 11 | −3.24 R (−$85), 22 | +19.25 R (+$252), 22 |
| BASE-DRIFT | −3.99 R (+$1), 11 | −36.04 R (−$571), 22 | −21.03 R (−$404), 22 |

### Buys and sells, and the legs of the clock rules (context)

| Key | Group | Trades | R/trade | Net USD |
|---|---|---:|---:|---:|
| P7-ARBG | buys | 25 | +0.112 | +170 |
| P7-ARBG | sells | 27 | +0.397 | +565 |
| P7-HALF | buys | 24 | +0.206 | +138 |
| P7-HALF | sells | 30 | −0.009 | −6 |
| P7-PCTB | buys | 24 | +0.473 | +254 |
| P7-PCTB | sells | 37 | +0.053 | −45 |
| P7-ORB5 | buys | 26 | −0.161 | −122 |
| P7-ORB5 | sells | 29 | +0.619 | +184 |
| P7-TOD | long 11:00–02:00 | 55 | −0.301 | −588 |
| P7-TOD | short 02:00–11:00 | 55 | +0.319 | +262 |
| P7-FIX | AM fix (05:05–05:35) | 55 | +0.174 | +211 |
| P7-FIX | PM fix (09:35–10:05) | 55 | +0.230 | +315 |
| P7-CRB | after an NR7 day | 10 | +0.152 | +104 |
| P7-CRB | after an NR4-only day | 4 | −1.003 | −345 |

**Context without the screen days** (so the two windows are not counted twice):

| Key | Trades | R/trade | Net USD |
|---|---:|---:|---:|
| P7-FIX | 90 | +0.199 | +440 |
| P7-ARBG | 42 | +0.271 | +629 |
| P7-HALF | 44 | +0.059 | +90 |
| P7-PCTB | 50 | +0.201 | +191 |
| P7-TOD | 90 | −0.132 | −608 |

## 6. Placebo check on the pre-fix short

P7-FIX is short-only, and the market fell. To see whether its two half hours are special or just short trades in a
falling market, I ran the same 30-minute short starting at every half hour of the New York day (hh:05 and hh:35) over
the context window. This check was run after the screen. It is exploratory and selects nothing.

- **Ordinary half hours lose:** only 18 of the 46 tradable half hours made money short, despite the falling market,
  because costs outweigh half an hour of drift.
- **The two pre-fix half hours rank 1st and 2nd of 46:**

  | Rank | Start (New York) | Trades | Net USD per trade | t-statistic | Win rate |
  |---:|---|---:|---:|---:|---:|
  | 1 | 09:35 (before the PM fix) | 55 | +5.73 | 1.88 | 63.6% |
  | 2 | 05:05 (before the AM fix) | 55 | +3.84 | 2.92 | 63.6% |
  | 3 | 08:05 | 55 | +3.53 | 0.78 | 58.2% |
  | … | | | | | |
  | 45 | 10:05 (right after the PM fix) | 55 | −4.13 | −1.34 | 43.6% |
  | 46 | 02:35 | 55 | −4.23 | −2.57 | 32.7% |

- **What it suggests:** the two windows named by the source, before any result, came out on top of 46. That is unlikely
  to be chance alone, and it fits the published "dip into the fix" pattern. The half hour right after the PM fix was
  among the worst for a short, which fits a rebound.
- **What it does not show:** it is one 11-week period of development data. In August, when the market was flat, P7-FIX
  made almost nothing (+$14 over 22 trades). The thesis itself reports the effect in some years only.

Full list: `research/pre_phase7/stage2-fix-placebo.json`.

## 7. Reading the results

- **P7-FIX, short before the London fixes.** The most consistent result.
  - Positive in the screen, in the context run, in both the AM and PM legs, with costs ×3 (+0.15 R), and outside the
    screen days (+0.199 R over 90 trades).
  - Small drawdown (3.6 R) and 37 of 55 days positive.
  - Weak points: short-only in a falling market; nearly flat in the flat month; no stop in the source, so the risk per
    trade is undefined and R is nominal.
- **P7-ARBG, gold London breakout.** The best rule with real stops.
  - Context: +0.26 R per trade over 52 trades, PF 1.67, both sides positive, barely affected by doubled costs.
  - Weak points: only 10 trades in the screen, where it is negative without its best day; flat in October (−0.02 R over
    22 trades); a vendor source; an assumed server clock and buffer; stops were almost always the 0.6 % cap, about $25
    an ounce.
- **P7-TOD, time of day.** A warning about short screens.
  - It passed every screen check on 10 days, with both legs positive.
  - Over 11 weeks the long leg lost $588 and the short leg made $262. Outside the screen days it made −0.132 R per
    trade. Its screen result was those 10 days, not the rule.
- **P7-HALF, last-half-hour momentum.** Mildly positive in both windows (+0.207 and +0.087 nominal R), with 61 % of days
  positive. The profit is about $2.44 a trade, all of it from the buy side and from September.
- **P7-PCTB, Bollinger %b reversion.** Positive (+0.218 nominal R, 72 % winners), but it has no stop. Its drawdown
  reached 12.6 R, the sell side lost money, and September carries the result.
- **P7-ORB5, opening-range breakout.** Positive in R over the context (+0.25) but only +$62, with a 22 % win rate, a
  20 R drawdown, and October's one +10 R day doing the work. Sells made money and buys lost: a falling-market result.
- **P7-ARB2, educational London breakout.** About zero (+0.03 R). Its range filter removed more than half the days.
- **P7-CRB, P7-NOISE, P7-MAX.** Negative over the context run.
- **Rejected: P7-VWAP, P7-RSI2, P7-SQZ, P7-EMA2.** Negative in both windows. VWAP and the crossovers trade so often
  that costs decide the result.
- **Rejected with a caveat: P7-TSOUP.** Its stops are very tight, and 42 of its 93 context trades were stopped on the
  bar they were filled. That is the simulator's worst-case rule for a bar that touches both the entry and the stop.
  Candle data cannot say which came first, so this rule set cannot be judged fairly without tick data.

## 8. Shortlist for Phase 7

Ranked by the evidence here and in the sources. Each still needs a proper test.

1. **P7-FIX, short before the London fixes.** First question for Phase 7: does it survive a rising market? It also
   needs a defined stop, so that the risk per trade is known.
2. **P7-ARBG, gold London breakout.** Needs its two assumptions settled (server clock, buffer) and a longer test.
3. **P7-HALF, last-half-hour momentum.** Peer-reviewed idea with a small, steady result. The edge per trade is thin.
4. **P7-PCTB, Bollinger %b reversion.** Only with a stop; without one the drawdowns are too large.
Not recommended: P7-TOD (did not hold), P7-PDF from stage 1 (lost money over 11 weeks), P7-ORB5 (lottery profile,
regime-driven), P7-ARB2, P7-CRB, P7-NOISE, P7-MAX and the five rejected rule sets.

A sensible order of work, none of it started:
1. Re-run the shortlist on the other spent development data (January–May 2026), which is a different market regime.
2. Freeze the rules of whatever survives.
3. Test once on untouched forward data under a registered unlock protocol.

## 9. Limitations and overfitting risk

- **Ten trading days prove nothing.** P7-TOD shows it: a clean pass on the screen, nothing over 11 weeks.
- **The context run is not validation either.** It is spent development data, one regime (an 11 % fall), and it
  contains the screen days.
- **Many rule sets were tried.** With 15 rule sets and several sub-results, some will look good by chance. Two passes
  out of 15 is not far from what chance alone could produce.
- **Short bias.** A falling market flatters shorts: P7-FIX is short-only, and the sell sides of P7-ARBG and P7-ORB5
  carried most of their profit.
- **Nominal R.** Seven rule sets have no stop in their source. Their R is a yardstick, not a risk; a position without a
  stop can lose far more than 1 R.
- **Ports change strategies.** Stock and daily-bar rules were applied to intraday gold with the sources' clock times and
  assumed defaults where a source was silent. A rule built for gold could behave differently.
- **Candle limits.** Fills inside a bar are approximated (stop first; worst case on the entry bar). Tight-stop rules such
  as P7-TSOUP and P7-ORB5 are the most affected. VWAP used tick volume, which is not traded volume.
- **Costs.** Spread comes from the stored candles, with fixed slippage. News-time spreads are probably wider than
  modelled.

## 10. Added to GoldScalp as research candidates

At your request, the two rule sets classified WORTH FURTHER RESEARCH were added to GoldScalp on 16 November 2026 as
**research candidates**, exactly as screened.

| Candidate | Registered because | What the longer run says |
|---|---|---|
| P7-FIX, short before the London fixes | It met the declared 10-day screen rule | Supported: +0.20 R per trade over 110 trades, both windows |
| P7-TOD, gold time-of-day | It met the declared 10-day screen rule | Not supported: about zero in R, −$326 |

- **Where they live:** `config/research/candidates/P7-FIX.json` and `P7-TOD.json`, listed in `REGISTRY.json`. Each file
  holds the source, the original rules, the rule and parameters as screened, a fingerprint, the evidence at registration,
  caveats, open questions and restrictions.
- **Where you see them:** Monthly Backtest, step 6 "Research candidates". Each card shows the rules, both result
  windows, the legs, month-by-month results, a cumulative chart and, for P7-FIX, the placebo check. A button opens the
  same results in the Research library, which now also lists every pre-Phase 7 screen (filter "Pre-7 · Screens").
- **Rules preserved:** `python -m goldscalp.research.candidates --check` compares each candidate with its fingerprint and
  with the screen's declaration, and reports both unchanged. `--reproduce` re-ran both windows and matched the stored
  evidence exactly. A changed rule would be a new candidate with a new id.
- **What a candidate is not:** it is not a Strategy Lab strategy, so it cannot be backtested in the Lab, monitored in
  Live Validation or linked to demo execution. It is not validated and not labelled profitable or trading-ready.
  (This was true when the screen ended. A Strategy Lab form was added later the same day: section 12.)
- **Code:** `goldscalp/research/candidates.py` (registry, checks and a read-only endpoint), `web/js/candidates.js`, and
  `tests/test_research_candidates.py`. The research library gained the pre-Phase 7 studies
  (`goldscalp/monthly/library.py`). The research service was restarted to switch the view on.

## 11. Protections confirmed unchanged

| What | State |
|---|---|
| Forward-data guard | Unchanged and locked (boundary 2026-11-14T00:00:00Z); no unlock protocol exists. Every read here ended on or before 31 October 2026, and the stage 2 loader refuses forward dates |
| Sealed block | Not read; the stage 2 loader refuses the sealed series and its dates |
| Frozen protocols | The Phase 5 closure, Phase 5.3, Phase 6 and H6.4 checks all report unchanged |
| Archives | `archive/generation-1` and `research_archive/phase_1_to_6` unchanged and read-only |
| Phase 4 and strategies | Monitors 1–3 stopped; A, B and C archived; no active strategy. Nothing was added to Strategy Lab or monitored. Demo execution unchanged (your setting: on, 0 links, 0 open) |
| Existing code | The strategy builder, the bt@1 engine, the guard and the live monitor were not modified. New modules in `goldscalp/research/`, the declaration and candidate files in `config/research/`, the result files in `research/pre_phase7/`, two test files and this report. The research service gained two read-only additions (the candidates endpoint and the pre-Phase 7 entries in the library), and the README, the Monthly Backtest page and the Project page were extended |
| Forward collection | GoldScalp was restarted today at your request and is collecting live MT5 candles again. Those candles are forward data: research cannot read them |

The rows "Phase 4 and strategies" and "Existing code" describe the end of the screen. Section 12 records what was
added afterwards.

## 12. Addendum: Strategy Lab form, a longer history and the demo link (16 November 2026, after the screen)

Sections 1–11 describe the state when the screen ended. Later the same day you asked for the candidates to be usable
in the Strategy Lab and Live Validation, and for P7-FIX to be linked to demo execution. This section records what
changed. The screen, its classifications and the registered candidates are unchanged.

**What changed in the code**

- The strategy builder gained a **clock-time** condition (true in the minutes after listed times, in UTC, New York or
  London time) and a stop type **none**. Both are additions: the fingerprints of every existing strategy are unchanged,
  and the frozen-protocol and archive checks still report unchanged.
- A strategy with stop type `none` cannot use an R-multiple target, break-even or a trailing stop, because those need a
  stop distance.
- Demo execution was not changed. It still refuses any trade without a stop-loss.

**The Strategy Lab forms** (`config/research/candidates/LAB_STRATEGIES.json`)

| Strategy Lab strategy | Version | What it is | Same trades as the screened rule set (5–19 October) |
|---|---|---|---|
| P7-FIX: short before the London fixes | v1 | As screened: no stop | Yes, 20 of 20, same net (+$86.04) |
| P7-FIX: short before the London fixes | v2 | Adds a protective stop 3.0 × ATR(14) on M15 | Same 20 entries; 1 stopped; +$85.77; +0.106 R (a real R, half the nominal figure because the stop is twice as far) |
| P7-TOD long leg, 11:00 to 02:00 New York | v1 | As screened | Yes; both legs together 20 of 20, same net (+$281.90) |
| P7-TOD short leg, 02:00 to 11:00 New York | v1 | As screened | (see the long leg) |

P7-FIX v2 is **not** the screened rule set. It exists because demo execution never sends an order without a stop-loss.
Its entries, direction, size and 30-minute exit are the same; its result differs whenever the stop is reached.

**Strategy Lab runs** (bt@1, 19 October to 13 November 2026, 0.02 lots; development data, and it overlaps the 11-week run)

| Strategy | Trades | Wins | Net | Notes |
|---|---|---|---|---|
| P7-FIX v1 | 40 | 26 | +$296.90 | Profit factor 4.11, largest closed drawdown $23.32 |
| P7-FIX v2 | 40 | 26 | +$296.90 | Identical: the protective stop was never reached in this window |
| P7-TOD long leg | 20 | 7 | −$295.56 | Fewer than 30 trades: ratios withheld |
| P7-TOD short leg | 20 | 9 | +$474.44 | Fewer than 30 trades: ratios withheld |

These four weeks are the strongest part of the year for this rule. They are not representative; see the next table.

**P7-FIX over all of 2026 before the forward boundary** (rule as registered; 1 January to 29 May and 22 June to
13 November; data research had already used; run after registration, so exploratory)

| Period | Trades | Win rate | R per trade (nominal) | Profit factor | Net USD | R per trade, costs ×2 |
|---|---|---|---|---|---|---|
| Whole run | 420 | 54.5% | +0.063 | 1.17 | +$607 | +0.040 |
| Morning fix (05:05 New York) | 210 | 58.1% | +0.104 | 1.49 | +$436 | +0.079 |
| Afternoon fix (09:35 New York) | 210 | 51.0% | +0.023 | 1.06 | +$171 | +0.001 |
| January to June | 224 | | −0.038 | | −$168 | |
| July to 13 November | 196 | | +0.179 | | +$774 | |

- 8 of 11 months and 119 of 210 trading days made money. The largest drawdown was 16.1 R.
- **The edge seen in the screen is not stable across the year.** It lost money from January to June and made all of
  its profit from July. Gold rose from about 4,328 to 4,541 between 1 January and 29 May, and fell from about 4,147 to
  3,882 between 22 June and 13 November. A short-only rule doing well in a falling market is the simplest explanation,
  and this run cannot rule it out.
- The afternoon fix is about break-even once costs are doubled. Most of the result is the morning fix.

**Monitoring and the demo link** (done at your request)

- P7-FIX v2 is monitored in paper mode: monitor 4, validation account 5, eligible through Lab run
  `rr_7867c43273b1d379`.
- It is linked to MT5 demo execution since 2026-11-16 12:27:41 UTC on the demo server. The application confirmed a DEMO
  account before linking and re-checks it before every order. Size 0.02 lots; limits 0.10 lots and 3 positions.
- Entries are at 05:05 and 09:35 New York time (10:05 and 14:35 UTC while New York is on winter time), each closed 30
  minutes later, on trading days.
- The archived Phase 4 monitors 1–3 and their accounts were not touched.

**What this is and is not**

- It is observation of an unvalidated research candidate on a demo account. It is not a validation, not a promotion
  and not a Phase 7 result. P7-FIX is not shown to be profitable.
- Trades from 14 November 2026 onward are forward data for P7-FIX. To keep them usable as evidence later, its rules
  must stay exactly as they are; a changed rule is a new strategy version whose forward record starts again.
- Research still cannot read forward candles. The guard, the sealed block and the frozen protocols are unchanged.

**Later the same day: your risk rule (P7-FIX v3 and v4)**

You asked for 5% risk per trade, 1:1.5 to 1:2 reward to risk, and break-even at 1:1. With v2's stop (3 × ATR, about
$23) only 1 of 40 trades moved +1R within 30 minutes, so you chose a stop of 1 × ATR(14) on M15 (about $7.60), kept the
30-minute exit, linked the 2R version and kept 1.5R on paper. Both are new versions; v1 and v2 are unchanged.

| Version | Stop | Target | Break-even | Size | Where |
|---|---|---|---|---|---|
| v3 | 1.0 × ATR(14) M15 | 2R | at +1R, to the entry | 5% of equity at risk, at most 2.00 lots | Monitor 5, linked to MT5 demo |
| v4 | 1.0 × ATR(14) M15 | 1.5R | at +1R, to the entry | 5% of equity at risk, at most 2.00 lots | Monitor 6, paper only |

What else changed to make this work:

- **Live break-even.** The live monitor did not move stops to break-even; only the backtest did. It now does, the same
  way (`LiveMonitor._manage_breakeven`, three new tests in `tests/test_phase4_live.py`). Versions without break-even
  are unaffected.
- **Lot limit.** 5% of $10,000 with a stop of about $7.60 is about 0.66 lots, up to about 1 lot on quiet days. The
  demo limit was 0.10 lots, so every order would have been refused. At your choice it is now 2.00 lots per order.
- **Margin.** Simulated accounts used 1:1 leverage, which cannot hold a 0.66-lot position on $10,000. They now use the
  demo account's 1:500 (read from the MT5 terminal). Only new accounts and Lab runs use it.
- v2 was unlinked from demo execution so only one version sends orders. Its paper monitor 4 keeps running.

**Strategy Lab run `rr_4987dec860838c83`** (19 October to 13 November 2026, $10,000 start; development data, the strongest
stretch of the year for this rule; required for monitoring, not evidence)

| Version | Trades | Wins | Net | Profit factor | R per trade | Max drawdown | Exits | Lots |
|---|---|---|---|---|---|---|---|---|
| v3, 2R | 40 | 22 | +$8,784.95 | 3.05 | +0.339 | 12.5% | breakeven 4, stop 5, target 6, time 25 | 0.50–1.35 |
| v4, 1.5R | 40 | 23 | +$7,587.20 | 2.88 | +0.302 | 12.5% | breakeven 3, stop 5, target 7, time 25 | 0.49–1.20 |

- At 5% risk a run of losses costs a lot: ten straight losing trades would take about 40% of the account.
- The +$8,785 (+88% in four weeks) comes from compounding 5% risk in the stretch where P7-FIX does best: the
  same entries at 0.02 lots made +$297 here, while over January to June the screened rule lost money. Expect
  nothing like it as a rate.
- These versions were not screened, not run over the rest of 2026, and are not validated. Their trades from 16
  November onward are their only fresh evidence; changing their rules would restart that record.

**Checks after these changes**

- Full project suite: 721 passed, 0 failed (after the v3/v4 changes) (run with the demo-execution default forced off, as before); Ruff is clean.
- New tests: `tests/test_clock_time_strategies.py` (validation of the new condition and stop type, unchanged
  fingerprints of the existing strategies, daylight-saving behaviour, bt@1 against the rule simulator, the Lab forms
  against the candidates, and the demo refusal of a trade without a stop-loss) and one more in
  `tests/test_research_candidates.py` (the longer history is shown only when it was stored).
- The Phase 5 closure, Phase 6 protocol and sealed-run checks report unchanged. Both archives report unchanged and
  read-only. The forward guard is locked on both services, with no unlock protocol.
