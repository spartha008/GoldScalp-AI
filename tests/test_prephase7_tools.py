"""Pre-Phase 7 stage 2 tools: the indicator library, the rule simulator and the externally sourced rules.

Checks: indicator values against hand-worked examples, no look-ahead (slower bars are visible only after they close;
removing future candles never changes past trades), fills and costs identical to the bt@1 primitives, and that the
stage 2 screen can never read sealed or forward data. All market data here is SYNTHETIC."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from goldscalp.core import forward
from goldscalp.core.kinds import MarketState
from goldscalp.monthly import discovery
from goldscalp.research import external_rules, rulesim
from goldscalp.research import indicators as ind
from goldscalp.research import screening_ext as sx
from goldscalp.simulation.positions import Side, close_fill_price, gross_pnl, open_fill_price
from goldscalp.simulation.pricing import ExecutionQuote

COSTS = rulesim.Costs(configured_spread=0.30, commission_per_lot_per_side=3.50, slippage=0.05)
T0 = pd.Timestamp("2026-03-02T00:00:00Z")


def series(values) -> pd.Series:
    return pd.Series(values, dtype="float64")


# ------------------------------------------------------------------ indicators
def test_moving_averages_and_rsi_match_hand_worked_values():
    x = series([1, 2, 3, 4, 5])
    assert ind.sma(x, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert ind.ema(x, 3).tolist()[2:] == [2.0, 3.0, 4.0] and np.isnan(ind.ema(x, 3)[1])       # seed 2, alpha 0.5
    r = ind.rsi(series([10, 11, 10.5, 11.5, 11.0]), 2)
    assert np.isnan(r[1]) and r.round(3).tolist()[2:] == [66.667, 85.714, 54.545]
    assert ind.rsi(series([1, 2, 3, 4, 5]), 2).iloc[-1] == 100 and ind.rsi(series([5, 4, 3, 2, 1]), 2).iloc[-1] == 0


def test_bands_true_range_and_regression():
    c = series([10, 11, 12, 11, 10, 11, 13])
    mid, up, lo = ind.bollinger(c, 3, 2.0)
    w = np.array([10.0, 11.0, 13.0])
    assert mid.iloc[-1] == pytest.approx(w.mean()) and up.iloc[-1] == pytest.approx(w.mean() + 2 * w.std())
    assert ind.percent_b(c, 3, 2.0).iloc[-1] == pytest.approx((13 - lo.iloc[-1]) / (up.iloc[-1] - lo.iloc[-1]))
    h, lw, cl = series([11, 12, 15]), series([9, 10, 11]), series([10, 11, 14])
    assert ind.true_range(h, lw, cl).tolist() == [2.0, 2.0, 4.0]                     # the third bar gaps: 15 - 11
    assert ind.atr(h, lw, cl, 2).tolist()[1:] == [2.0, 3.0]
    assert ind.linreg_value(series([1, 2, 3, 4, 5]), 3).iloc[-1] == pytest.approx(5.0)          # a straight line
    _, ku, kl = ind.keltner(h, lw, cl, 2, 1.5)
    assert ku.iloc[-1] == pytest.approx(12.5 + 1.5 * 3.0) and kl.iloc[-1] == pytest.approx(12.5 - 1.5 * 3.0)


def bars_of(rows, start=T0, minutes=5, spread=0.2):
    idx = pd.date_range(start, periods=len(rows), freq=f"{minutes}min")
    f = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype="float64")
    f["spread"], f["tick_volume"] = spread, 100
    return f


def test_slower_bars_are_seen_only_after_they_close():
    b = bars_of([[i, i + 1, i - 1, i + 0.5] for i in range(1, 10)])          # nine M5 bars from 00:00
    m = ind.resample(b, 15)
    assert m["close"].tolist() == [3.5, 6.5, 9.5] and m["high"].tolist() == [4.0, 7.0, 10.0]
    seen = ind.known_at(m["close"], 15, b.index, 5)
    assert np.isnan(seen[:2]).all() and seen[2] == 3.5 and seen[4] == 3.5 and seen[5] == 6.5   # bar 3 closes at 00:15
    assert ind.closes_now(m.index, 15, b.index, 5).tolist() == [False, False, True] * 3


def test_trading_day_rollover_and_clock_helpers():
    idx = pd.DatetimeIndex(["2026-10-05T20:55:00Z", "2026-10-05T21:00:00Z", "2026-12-07T21:55:00Z", "2026-12-07T22:00:00Z"])
    assert [d.day for d in ind.trading_day(idx)] == [5, 6, 7, 8]              # 17:00 New York: 21:00 UTC in summer, 22:00 in winter
    assert ind.day_end(pd.Timestamp("2026-10-05")) == pd.Timestamp("2026-10-05T21:00:00Z")
    minute, day = ind.local_clock(pd.DatetimeIndex(["2026-10-05T13:30:00Z"]), ind.NY)
    assert minute[0] == 9 * 60 + 30 and ind.at_local(day[0], 16 * 60, ind.NY) == pd.Timestamp("2026-10-05T20:00:00Z")
    sm, sd = ind.local_clock(pd.DatetimeIndex(["2026-10-05T00:00:00Z"]), ind.NY, 7)             # server = New York + 7 h
    assert sm[0] == 3 * 60 and ind.at_local(sd[0], 8 * 60, ind.NY, 7) == pd.Timestamp("2026-10-05T05:00:00Z")


# ------------------------------------------------------------------ simulator
class Script(rulesim.Rule):
    """Calls the given actions at the given bar closes."""
    def on_close(self, i):
        if i in self.p:
            self.p[i](self.sim)


def sim(rows, script, *, delay=0, end=None, spread=0.2):
    b = bars_of(rows, spread=spread)
    end = end or (b.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime()
    return rulesim.simulate(b, Script(script), bar_minutes=5, start=T0.to_pydatetime(), end=end, costs=COSTS, lots=0.02,
                            ounces_per_lot=100.0, delay_bars=delay)


FLAT = [100, 100.5, 99.5, 100]


def test_long_fill_costs_and_r_match_the_engine_primitives():
    rows = [FLAT, FLAT, [100, 103, 99.8, 102.5], FLAT]
    t = sim(rows, {0: lambda s: s.market(1, stop=98.0, target=102.0)})["trades"][0]
    q = ExecutionQuote(bid=Decimal("100"), ask=Decimal("100.2"), spread=Decimal("0.2"), spread_source="candle",
                       quote_time=datetime(2026, 3, 2, tzinfo=UTC), state=MarketState.HISTORICAL, source_id="t")
    entry = open_fill_price(Side.BUY, q, Decimal("0.05"))
    xq = ExecutionQuote(bid=Decimal("102"), ask=Decimal("102.2"), spread=Decimal("0.2"), spread_source="candle",
                        quote_time=datetime(2026, 3, 2, tzinfo=UTC), state=MarketState.HISTORICAL, source_id="t")
    exit_price = close_fill_price(Side.BUY, xq, Decimal("0.05"))
    net = float(gross_pnl(Side.BUY, entry, exit_price, Decimal(2)) - Decimal("3.50") * Decimal("0.02") * 2)
    assert t["entry_price"] == float(entry) == 100.25 and t["exit_price"] == float(exit_price) == 101.95
    assert t["reason"] == "target" and t["net"] == pytest.approx(net) == pytest.approx(3.26)
    assert t["risk_usd"] == pytest.approx(4.5) and t["r"] == pytest.approx(3.26 / 4.5, abs=1e-3)
    assert t["cost_usd"] == pytest.approx(0.14 + 0.2 * 2 + 0.05 * 2 * 2) and t["entry"] == "2026-03-02T00:05:00Z"


def test_stop_counts_first_and_gaps_fill_at_the_open():
    both = sim([FLAT, FLAT, [100, 103, 97, 100]], {0: lambda s: s.market(1, stop=98.0, target=102.0)})["trades"][0]
    assert both["reason"] == "stop" and both["exit_price"] == pytest.approx(98.0 - 0.05)
    gap = sim([FLAT, FLAT, [96, 96.5, 95.5, 96]], {0: lambda s: s.market(1, stop=98.0, target=102.0)})["trades"][0]
    assert gap["reason"] == "stop" and gap["exit_price"] == pytest.approx(96.0 - 0.05) and gap["exit"] == "2026-03-02T00:10:00Z"


def test_short_stop_triggers_on_the_ask():
    rows = [FLAT, FLAT, [100, 101.85, 99.9, 100.5]]                          # bid high 101.85 + spread 0.2 >= 102
    t = sim(rows, {0: lambda s: s.market(-1, stop=102.0, target=95.0)})["trades"][0]
    assert t["entry_price"] == pytest.approx(99.95) and t["reason"] == "stop" and t["exit_price"] == pytest.approx(102.05)
    assert t["cost_usd"] == pytest.approx(0.14 + 0.2 * 2 + 0.2)              # a short pays the spread at the exit


def test_stop_entry_orders_fill_cancel_and_take_the_worst_case():
    end = T0 + pd.Timedelta(hours=1)

    def oco(s):
        s.stop_entry(1, 101.0, stop=99.0, target_r=1.0, expire_at=end, group="g")
        s.stop_entry(-1, 99.0, stop=101.0, target_r=1.0, expire_at=end, group="g")
    r = sim([FLAT, [100, 101.5, 99.9, 101.2], [101.2, 103.5, 101.0, 103.2]], {0: oco})
    t = r["trades"][0]
    assert t["side"] == "buy" and t["entry_price"] == pytest.approx(101.05)   # ask reaches 101: fill at 101 + slippage
    assert t["reason"] == "target" and t["exit"] == "2026-03-02T00:15:00Z"    # not on the entry bar, although touched later
    assert len(r["trades"]) == 1                                              # the sell order was cancelled (OCO)
    gap = sim([FLAT, [102, 102.5, 101.9, 102.2], FLAT], {0: oco})["trades"][0]
    assert gap["entry_price"] == pytest.approx(102 + 0.2 + 0.05)              # opened beyond the price: filled at the open
    whipsaw = sim([FLAT, [100, 101.2, 98.5, 100], FLAT], {0: oco})["trades"][0]
    assert whipsaw["reason"] == "stop" and whipsaw["r"] < -0.9                # both sides touched: counted as a loss
    expired = sim([FLAT, FLAT, [100, 105, 100, 104]], {0: lambda s: s.stop_entry(1, 103.0, stop=99.0, expire_at=T0 + pd.Timedelta(minutes=10))})
    assert expired["trades"] == []


def test_clock_exit_delay_break_even_and_window_end():
    rows = [FLAT] * 6
    at = T0 + pd.Timedelta(minutes=20)
    t = sim(rows, {0: lambda s: s.market(1, stop=90.0, exit_at=at)})["trades"][0]
    assert t["reason"] == "time" and t["exit"] == "2026-03-02T00:20:00Z" and t["hold"] == 15.0
    late = sim(rows, {0: lambda s: s.market(1, stop=90.0, exit_at=at)}, delay=1)["trades"][0]
    assert late["entry"] == "2026-03-02T00:10:00Z"                            # one bar later
    still_open = sim(rows, {0: lambda s: s.market(1, stop=90.0)})["trades"][0]
    assert still_open["reason"] == "window_end" and still_open["exit"] == "2026-03-02T00:30:00Z"
    be_rows = [FLAT, FLAT, [100, 103.5, 100, 103], [103, 103.2, 100.0, 100.1]]
    be = sim(be_rows, {0: lambda s: s.stop_entry(1, 100.3, stop=98.25, be_trigger_r=1.5, target_r=5.0, expire_at=T0 + pd.Timedelta(hours=1))})
    assert be["trades"][0]["reason"] == "breakeven" and be["trades"][0]["exit_price"] == pytest.approx(100.35 - 0.05)
    flip = sim(rows, {0: lambda s: s.market(1, stop=90.0), 2: lambda s: (s.exit("flip"), s.market(-1, stop=110.0))})["trades"]
    assert [x["side"] for x in flip] == ["buy", "sell"] and flip[0]["exit"] == flip[1]["entry"] == "2026-03-02T00:15:00Z"


def synthetic(days=40, seed=7):
    idx = pd.date_range(T0, periods=days * 288, freq="5min")
    ny = idx.tz_convert(ind.NY)
    hour = ny.hour + ny.minute / 60
    closed = (ny.day_of_week == 5) | ((ny.day_of_week == 4) & (hour >= 17)) | ((ny.day_of_week == 6) & (hour < 18)) | ((hour >= 17) & (hour < 18))
    idx = idx[~closed]
    rng = np.random.default_rng(seed)
    close = 3000 + np.cumsum(rng.normal(0, 0.7, len(idx)))
    opens = np.r_[close[0], close[:-1]]
    f = pd.DataFrame({"open": opens, "close": close}, index=idx)
    f["high"] = f[["open", "close"]].max(axis=1) + np.abs(rng.normal(0, 0.35, len(idx)))
    f["low"] = f[["open", "close"]].min(axis=1) - np.abs(rng.normal(0, 0.35, len(idx)))
    f["spread"], f["tick_volume"] = 0.2, rng.integers(50, 500, len(idx))
    return f[["open", "high", "low", "close", "spread", "tick_volume"]]


def test_positions_without_a_stop_use_the_nominal_unit():
    b = synthetic(days=6)
    rule = external_rules.make("clock_trades", {"tz": "UTC", "trades": [{"enter": "12:00", "side": "long", "exit": "13:00"}]})
    start = (T0 + pd.Timedelta(days=3)).to_pydatetime()
    r = rulesim.simulate(b, rule, bar_minutes=5, start=start, end=start + timedelta(days=2), costs=COSTS, lots=0.02, ounces_per_lot=100.0)
    t = r["trades"][0]
    m15 = ind.resample(b, 15)
    a = ind.atr(m15["high"], m15["low"], m15["close"], 14)
    unit = 1.5 * a[a.index + pd.Timedelta(minutes=15) <= pd.Timestamp(t["entry"])].iloc[-1]
    assert t["nominal_r"] and t["stop"] is None and t["risk_usd"] == pytest.approx(unit * 2, abs=0.01)
    assert t["entry"].endswith("T12:00:00Z") and t["exit"].endswith("T13:00:00Z") and t["reason"] == "time"


# ------------------------------------------------------------------ the declared rules
def declared():
    spec = sx.load2()
    items = [(c["key"], c["rule"], c["params"]) for c in spec["candidates"] + spec["baselines"]]
    items += [(e["key"], c["rule"], e["params"]) for c in spec["candidates"] for e in c.get("also_report", [])]
    return spec, items


def run_rule(bars, rule, params, start, end):
    return rulesim.simulate(bars, external_rules.make(rule, params), bar_minutes=5, start=start, end=end, costs=COSTS,
                            lots=0.02, ounces_per_lot=100.0)["trades"]


def test_every_declared_rule_runs_and_never_uses_future_candles():
    _, items = declared()
    b = synthetic()
    start = (T0 + pd.Timedelta(days=24)).to_pydatetime()
    cut = (T0 + pd.Timedelta(days=33, hours=13, minutes=35)).to_pydatetime()           # mid-session, mid-week
    total = 0
    for key, rule, params in items:
        with_future = run_rule(b, rule, params, start, cut)
        without = run_rule(b[b.index < pd.Timestamp(cut)], rule, params, start, cut)
        assert with_future == without, f"{key}: future candles changed past trades"
        for t in with_future:
            assert start <= datetime.fromisoformat(t["entry"].replace("Z", "+00:00")) < cut and t["risk_usd"] > 0
        total += len(with_future)
    assert total > 100                                                                 # the rules do trade on this data


def test_opening_range_rule_follows_its_source():
    rows = [[100, 100.2, 99.8, 100]] * 3 + [[100, 101.0, 99.7, 100.8]] + [[100.8, 100.9, 100.7, 100.8]] * 4
    b = bars_of(rows, start=pd.Timestamp("2026-10-05T13:15:00Z"))                      # 09:30 New York is the 4th bar
    rule = external_rules.make("orb_first_bar", {"tz": ind.NY, "open": "09:30", "exit": "16:00", "target_r": 10.0})
    t = rulesim.simulate(b, rule, bar_minutes=5, start=b.index[0].to_pydatetime(), end=(b.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime(),
                         costs=COSTS, lots=0.02, ounces_per_lot=100.0)["trades"][0]
    assert t["side"] == "buy" and t["entry"] == "2026-10-05T13:35:00Z" and t["stop"] == 99.7      # the first bar's low


def test_clock_range_breakout_follows_its_source():
    day = pd.Timestamp("2026-10-06T00:00:00Z")
    idx = pd.date_range(day - pd.Timedelta(days=30), day + pd.Timedelta(hours=13), freq="5min")
    f = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "spread": 0.2, "tick_volume": 10}, index=idx)
    today = f.index >= day
    f.loc[today, ["high", "low"]] = [100.5, 99.5]                                      # a 1.0 range against a daily ATR of 2.0
    brk = (f.index >= day + pd.Timedelta(hours=7, minutes=15)) & (f.index < day + pd.Timedelta(hours=7, minutes=30))
    f.loc[brk, ["open", "high", "low", "close"]] = [100.2, 100.9, 100.1, 100.8]         # M15 close above the range at 07:30
    after = f.index >= day + pd.Timedelta(hours=7, minutes=30)
    f.loc[after, ["open", "high", "low", "close"]] = [100.8, 100.9, 100.7, 100.8]
    params = next(c["params"] for c in sx.load2()["candidates"] if c["key"] == "P7-ARB2")
    trades = run_rule(f, "arb_clock", params, day.to_pydatetime(), (day + pd.Timedelta(hours=13)).to_pydatetime())
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "buy" and t["entry"] == "2026-10-06T07:30:00Z" and t["stop"] == 99.5       # range low
    assert t["reason"] == "time" and t["exit"] == "2026-10-06T12:00:00Z"


# ------------------------------------------------------------------ protections and the declared decision rules
def test_stage2_config_uses_only_spent_development_data():
    spec, items = declared()
    for name, w in spec["windows"].items():
        start, end = sx._t(w["start"]), sx._t(w["end"])
        assert sx._t(spec["history_from"]) < start < end, name
        assert not forward.includes_forward(sx._t(spec["history_from"]), end), name
        assert sx._t(spec["history_from"]) >= discovery.SEALED[1], name
    screen = spec["windows"]["screen"]
    assert (sx._t(screen["end"]) - sx._t(screen["start"])).days in range(10, 16)
    assert spec["series"]["key"] != sx.SEALED_SERIES and spec["written_before_results"] is True
    assert {rule for _, rule, _ in items} <= set(external_rules.RULES) and len(spec["candidates"]) == 15
    for c in spec["candidates"]:
        assert c["source"] and c["original_rules"] and c["implementation"] and isinstance(c["deviations"], list)


def test_screen_loader_refuses_forward_and_sealed_data():
    class NoData:
        def series_row(self, *a):
            raise AssertionError("no data may be read")
    with pytest.raises(forward.ForwardDataLocked):
        sx.load_bars(NoData(), "research", "csv:vantage-mt5-xauusd", sx.Timeframe.M5, datetime(2026, 11, 1, tzinfo=UTC),
                     datetime(2026, 11, 16, tzinfo=UTC))
    with pytest.raises(sx.ScreeningError):
        sx.load_bars(NoData(), "research", "csv:vantage-mt5-xauusd", sx.Timeframe.M5, datetime(2025, 10, 1, tzinfo=UTC),
                     datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(sx.ScreeningError):
        sx.load_bars(NoData(), "research", sx.SEALED_SERIES, sx.Timeframe.M5, datetime(2026, 1, 1, tzinfo=UTC),
                     datetime(2026, 2, 1, tzinfo=UTC))


def _m(n, e, pf=1.3, x2=0.05):
    return {"trades": n, "expectancy_r": e, "profit_factor": pf, "expectancy_r_costs_x2": x2}


def test_stage2_decision_rules_classify_mechanically():
    rules, ok = {"min_trades": 15, "min_profit_factor": 1.1}, {"sum_r_without_best_day": 1.0}
    assert sx.classify2(_m(9, 0.5), ok, _m(9, 0.5), rules)[0] == "INCONCLUSIVE"
    assert sx.classify2(_m(40, -0.02), ok, _m(40, 0.1), rules)[0] == "REJECTED"
    assert sx.classify2(_m(40, 0.1), ok, _m(40, 0.02), rules)[0] == "WORTH FURTHER RESEARCH"
    assert sx.classify2(_m(40, 0.1), ok, _m(40, -0.02), rules)[0] == "INCONCLUSIVE"            # fails with a one-bar delay
    assert sx.classify2(_m(40, 0.1), {"sum_r_without_best_day": -0.1}, _m(40, 0.1), rules)[0] == "INCONCLUSIVE"
    assert sx.classify2(_m(40, 0.1, pf=1.05), ok, _m(40, 0.1), rules)[0] == "INCONCLUSIVE"
    chunks = sx.month_chunks(datetime(2026, 8, 17, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
    assert [(a.month, a.day, b.month, b.day) for a, b in chunks] == [(8, 17, 9, 1), (9, 1, 10, 1), (10, 1, 10, 31)]
