# Recorded test fixtures

| File | Contents |
|------|----------|
| `mt5_xauusd_m1_recorded.csv` | 2,068 closed XAUUSD **M1** candles, 2026-11-12 08:00 → 2026-11-13 19:29 UTC |
| `mt5_xauusd_h1_recorded.csv` | 400 closed native **H1** candles that end before the first M1 candle (2026-10-19 22:00 → 2026-11-12 07:00 UTC) |

- **Source.** Recorded by GoldScalp's read-only MetaTrader 5 provider from a demo terminal. They are
  bid prices with the broker's spread per candle, converted to UTC on ingestion.
- **Contents.** Market prices only: no account, order or credential data.
- **Use.** Tests use these files so the engines, replay and performance evaluation are exercised on
  real market behaviour without a live MT5 connection or network access.
- **Do not edit by hand.** Tests check properties (no look-ahead, consistent totals), not values that
  depend on the rules' parameters.
