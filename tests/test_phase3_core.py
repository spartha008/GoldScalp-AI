"""Phase 3 core: strategy configuration, condition evaluation, the backtest engine (hand-worked cases),
metrics, walk-forward folds and selection, feature snapshots without look-ahead, reproducibility and exports."""
from __future__ import annotations

import copy
import csv
import io
import re
from datetime import datetime, timedelta, UTC

import numpy as np
import pandas as pd
import pytest

from goldscalp.analysis import replay
from goldscalp.analysis.config import default_config
from goldscalp.backtesting import engine, features, lab, metrics, walk_forward
from goldscalp.core.timeframes import Timeframe
from goldscalp.reports import exports
from goldscalp.reports.pdf import PDF
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder, conditions
from tests.conftest import SPEC_PATH, recorded

SPEC = InstrumentSpec.load(SPEC_PATH)          # 100 oz per lot, min lot 0.01, step 0.01
T0 = pd.Timestamp("2026-11-11T14:00Z")
M1 = Timeframe.M1
A = engine.Assumptions.from_mapping({"configured_spread": "0.30", "commission_per_lot_per_side": "3.50",
                                     "slippage": "0.05", "min_trades_for_ratios": 1})


def at(minutes: int) -> str:
    return (T0 + pd.Timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def strategy(**over):
    base = {"name": "test", "direction": "long", "evaluation": {"step_timeframe": "M1"},
            "conditions": [{"id": "s", "type": "structure_state", "timeframe": "M1", "state": "with"}],
            "entry": {"trigger": "on_change", "max_entry_delay_minutes": 5},
            "exit": {"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0.0}, "target": {"type": "r_multiple", "r": 2.0},
                     "max_holding_minutes": 0},
            "risk": {"sizing": "fixed_lots", "lots": "0.10"}}
    for k, v in over.items():
        base[k] = v
    return builder.validate(base)


def snap(minute: int, state="bullish", ref_low=99.0, ref_high=101.0, atr=1.0, enough=True, fresh=True, **tf_extra):
    item = {"bars": 100, "fresh": fresh, "enough": enough, "problems": [], "state": state, "high_label": "HH",
            "low_label": "HL", "ref_high": ref_high, "ref_low": ref_low, "events": [], "detail": True,
            "vol": {"condition": "normal", "atr": atr, "atr_pct": atr / 100, "ratio": 1.0}, "last_close": 100.0,
            "sweeps": [], "fvg": [], "ob": []}
    item.update(tf_extra)
    return {"definition": "features@1", "cutoff": at(minute), "price": 100.0, "spread": 0.2, "sessions": ["London"],
            "signal": {"direction": "BULLISH", "score": 5, "id": "sig_x"}, "tf": {"M1": item}}


def candles(rows, spread=0.20, start=T0):
    idx = pd.date_range(start, periods=len(rows), freq="min", tz="UTC")
    o, h, low, c = (np.array([r[k] for r in rows], dtype=float) for k in range(4))
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "tick_volume": 1, "spread": spread,
                         "closed": True}, index=idx)


FLAT = (100.0, 100.3, 99.9, 100.1)


def bt(cfg, snaps, rows, *, end_minute=None, assumptions=A, force=False, spread=0.20):
    frame = candles(rows, spread)
    end = (T0 + pd.Timedelta(minutes=end_minute if end_minute is not None else len(rows))).to_pydatetime()
    return engine.run(cfg, snaps, frame, M1, start=T0.to_pydatetime(), end=end, assumptions=assumptions, spec=SPEC,
                      force_close_at_end=force)


# ------------------------------------------------------------------------------- configuration
def test_templates_are_valid_and_defaults_are_filled():
    for t in builder.templates():
        assert t["schema"] == "strategy@1" and t["conditions"] and t["risk"]["lots"] == "0.02"
    cfg = builder.validate({"name": "x", "conditions": [{"type": "structure_event"}]})
    c = cfg["conditions"][0]
    assert (c["id"], c["timeframe"], c["event"], c["direction"], c["within_candles"]) == ("c1", "M5", "BOS", "with", 3)
    assert cfg["exit"]["stop"]["type"] == "structure" and cfg["entry"]["trigger"] == "on_change"


@pytest.mark.parametrize("bad, message", [
    ({"name": "x", "conditions": [{"type": "nope"}]}, "unknown"),
    ({"name": "x", "conditions": [{"type": "fvg", "colour": "red"}]}, "unknown keys"),
    ({"name": "x", "conditions": [{"type": "structure_state", "timeframe": "M2"}]}, "timeframe"),
    ({"name": "", "conditions": [{"type": "session"}]}, "name"),
    ({"name": "x", "conditions": [{"type": "session", "enabled": False}]}, "at least one condition"),
    ({"name": "x", "conditions": [{"type": "atr_range"}]}, "min or a max"),
    ({"name": "x", "conditions": [{"type": "atr_range", "min": 5, "max": 1}]}, "min must not exceed"),
    ({"name": "x", "conditions": [{"type": "session"}], "exit": {"stop": {"type": "fixed"}}}, "distance is required"),
    ({"name": "x", "conditions": [{"type": "htf_alignment", "timeframes": ["H1"], "min_agree": 2}]}, "min_agree"),
    ({"name": "x", "conditions": [{"type": "session", "id": "a"}, {"type": "session", "id": "a"}]}, "used twice"),
    ({"name": "x", "conditions": [{"type": "session"}] * 21}, "1-20"),
    ({"name": "x", "conditions": [{"type": "session"}], "risk": {"lots": "abc"}}, "decimal"),
    ({"name": "x", "conditions": [{"type": "session"}], "colour": "red"}, "unknown keys"),
])
def test_invalid_configurations_are_refused(bad, message):
    with pytest.raises(builder.StrategyError, match=message):
        builder.validate(bad)


def test_fingerprint_covers_logic_not_names():
    a = builder.validate({"name": "one", "conditions": [{"type": "session"}]})
    b = builder.validate({"name": "two", "description": "renamed", "conditions": [{"type": "session"}]})
    c = builder.validate({"name": "one", "conditions": [{"type": "session", "overlap": "required"}]})
    assert builder.fingerprint(a) == builder.fingerprint(b) != builder.fingerprint(c)


def test_timeframes_and_detail_are_derived_from_enabled_conditions():
    cfg = builder.templates()[1]                    # sweep + CHoCH + FVG + session on M5
    assert builder.detail_timeframes(cfg) == {"M5"} and "M5" in builder.timeframes_used(cfg)
    cfg2 = copy.deepcopy(cfg)
    cfg2["conditions"][0]["enabled"] = False
    cfg2["conditions"][2]["enabled"] = False
    assert builder.detail_timeframes(cfg2) == set()


# ------------------------------------------------------------------------------- conditions
def full_snap(**tf):
    s = snap(0)
    s["tf"] = {"H1": dict(snap(0)["tf"]["M1"]), "M15": dict(snap(0)["tf"]["M1"]), "M5": dict(snap(0)["tf"]["M1"]),
               "M1": dict(snap(0)["tf"]["M1"])}
    for k, v in tf.items():
        s["tf"][k].update(v)
    return s


def one(ctype, side="long", s=None, **params):
    cfg = builder.validate({"name": "x", "direction": side, "conditions": [{"type": ctype, **params}],
                            "evaluation": {"step_timeframe": "M5"}})
    return conditions.evaluate(cfg, s or full_snap(), side)


def test_direction_relative_conditions():
    s = full_snap(M5={"state": "bullish"})
    assert one("structure_state", "long", s, timeframe="M5")["passed"]
    assert not one("structure_state", "short", s, timeframe="M5")["passed"]
    assert one("structure_state", "short", s, timeframe="M5", state="against")["passed"]
    s = full_snap(M5={"events": [{"type": "BOS", "direction": "bullish", "price": 101, "known_at": at(-2)}]})
    assert one("structure_event", "long", s, timeframe="M5", within_candles=1)["passed"]      # 2 min < one M5 candle
    assert not one("structure_event", "long", s, timeframe="M5", event="CHoCH")["passed"]
    s2 = full_snap(M5={"events": [{"type": "BOS", "direction": "bullish", "price": 101, "known_at": at(-30)}]})
    assert not one("structure_event", "long", s2, timeframe="M5", within_candles=3)["passed"]  # 30 min > 3 x M5
    s = full_snap(M15={"high_label": "LH", "low_label": "LL"})
    assert one("swing_sequence", "short", s, timeframe="M15")["passed"] and not one("swing_sequence", "long", s, timeframe="M15")["passed"]


def test_zone_sweep_session_volatility_spread_and_signal_conditions():
    zone = {"direction": "bullish", "top": 100.5, "bottom": 99.5, "known_at": at(-3), "mitigation": "partially_mitigated", "id": "z"}
    s = full_snap(M5={"fvg": [zone], "sweeps": [{"type": "sweep", "direction": "bullish", "level_type": "PDL", "price": 99, "known_at": at(-4)}]})
    assert one("fvg", "long", s, timeframe="M5")["passed"]
    assert not one("fvg", "long", s, timeframe="M5", allow_partial=False)["passed"]
    assert not one("fvg", "short", s, timeframe="M5")["passed"]
    assert one("liquidity_sweep", "long", s, timeframe="M5", level_types=["PD"])["passed"]
    assert not one("liquidity_sweep", "long", s, timeframe="M5", level_types=["EQ"])["passed"]
    assert one("session", s=s, sessions=["London"])["passed"]
    assert not one("session", s=s, sessions=["London"], overlap="required")["passed"]
    assert one("volatility_regime", s=s, timeframe="M5", allowed=["normal"])["passed"]
    assert one("atr_range", s=s, timeframe="M5", min=0.5, max=2)["passed"] and not one("atr_range", s=s, timeframe="M5", min=2)["passed"]
    assert one("spread", s=s, max_spread=0.3)["passed"] and not one("spread", s=s, max_spread=0.1)["passed"]
    assert not one("spread", s=s, max_spread=None, max_spread_atr=0.1)["passed"]               # 0.2 > 0.1 x ATR 1.0
    unknown = dict(s, spread=None)
    assert not one("spread", s=unknown)["passed"] and one("spread", s=unknown, if_unknown="pass")["passed"]
    assert one("signal_agrees", s=s, min_abs_score=4)["passed"] and not one("signal_agrees", "short", s)["passed"]
    s = full_snap(H1={"state": "bullish"}, M15={"state": "ranging"})
    assert not one("htf_alignment", s=s, timeframes=["H1", "M15"], min_agree=2)["passed"]
    assert one("htf_alignment", s=s, timeframes=["H1", "M15"], min_agree=1)["passed"]


def test_data_quality_gate_blocks_setups():
    s = full_snap(M5={"fresh": False})
    r = one("structure_state", "long", s, timeframe="M5")
    assert not r["passed"] and any("stale" in g for g in r["gate"])
    s = full_snap()
    del s["tf"]["M1"]
    assert any("no data" in g for g in one("structure_state", s=s, timeframe="M1")["gate"])


def test_both_directions_passing_is_ambiguous_not_a_trade():
    cfg = builder.validate({"name": "x", "direction": "both", "conditions": [{"type": "session", "sessions": ["London"]}],
                            "evaluation": {"step_timeframe": "M5"}})
    d = conditions.decide(cfg, full_snap())
    assert d["ambiguous"] and d["setup"] is None


# ------------------------------------------------------------------------------- engine: hand-worked cases
def test_entry_next_open_and_stop_with_exact_costs():
    # signal at the close of minute 0 -> entry at the open of minute 1: ask = 100 + 0.20, + 0.05 slippage = 100.25
    # stop = swing low 99.0; candle 3 trades to 98.9 -> exit 99.0 - 0.05 = 98.95; gross (98.95 - 100.25) x 10 oz
    r = bt(strategy(), [snap(1)], [FLAT, FLAT, FLAT, (99.5, 99.6, 98.9, 99.0), FLAT])
    (t,) = r["trades"]
    assert (t["entry_time"], t["entry_price"], t["stop"], t["exit_reason"], t["exit_price"]) == (at(1), 100.25, 99.0, "stop", 98.95)
    assert (t["gross_pnl"], t["commission"], t["net_pnl"], t["spread_cost"], t["slippage_cost"]) == (-13.0, 0.7, -13.7, 2.0, 1.0)
    assert t["exit_time"] == at(4) and t["r_multiple"] == round(-13.7 / (1.25 * 10), 3) and t["target"] == 102.75


def test_target_and_intrabar_ambiguity_under_both_rules():
    rows = [FLAT, FLAT, (100.5, 103.0, 98.5, 100.0), FLAT]
    stop_first = bt(strategy(), [snap(1)], rows)
    (t,) = stop_first["trades"]
    assert t["exit_reason"] == "stop" and t["ambiguous_bar"] and stop_first["diagnostics"]["ambiguous_intrabar"] == 1
    target_first = bt(strategy(), [snap(1)], rows, assumptions=engine.Assumptions.from_mapping(
        {**A.to_mapping(), "intrabar": "target_first"}))
    (t2,) = target_first["trades"]
    assert t2["exit_reason"] == "target" and t2["exit_price"] == round(102.75 - 0.05, 3) and t2["ambiguous_bar"]
    clean = bt(strategy(), [snap(1)], [FLAT, FLAT, (100.5, 103.0, 100.4, 102.9), FLAT])
    assert clean["trades"][0]["exit_reason"] == "target" and not clean["trades"][0]["ambiguous_bar"]


def test_gap_through_the_stop_fills_at_the_open():
    r = bt(strategy(), [snap(1)], [FLAT, FLAT, (98.0, 98.4, 97.8, 98.2), FLAT])
    (t,) = r["trades"]
    assert (t["exit_reason"], t["exit_time"], t["exit_price"]) == ("stop", at(2), 97.95)


def test_time_exit_and_structure_flip_exit():
    cfg = strategy(exit={"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0}, "target": {"type": "none"},
                         "max_holding_minutes": 3})
    (t,) = bt(cfg, [snap(1)], [FLAT] * 8)["trades"]
    assert (t["exit_reason"], t["entry_time"], t["exit_time"]) == ("time", at(1), at(4))
    flip = strategy(exit={"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0}, "target": {"type": "none"},
                          "max_holding_minutes": 0, "exit_on_structure_flip": {"enabled": True, "timeframe": "M1"}})
    (t,) = bt(flip, [snap(1), snap(3, state="bearish")], [FLAT] * 8)["trades"]
    assert (t["exit_reason"], t["exit_time"]) == ("structure_flip", at(3))


def test_short_trade_uses_the_ask_side():
    cfg = strategy(direction="short")
    # entry = bid 100 - 0.05 = 99.95; stop = swing high 101.0 checked on ask = bid high + 0.20
    r = bt(cfg, [snap(1, state="bearish")], [FLAT, FLAT, (100.4, 100.85, 100.3, 100.6), FLAT])
    (t,) = r["trades"]
    assert (t["side"], t["entry_price"], t["exit_reason"], t["exit_price"]) == ("sell", 99.95, "stop", 101.05)
    assert t["net_pnl"] == round((99.95 - 101.05) * 10 - 0.7, 2)


def test_a_data_gap_cancels_a_late_entry():
    rows = [FLAT] * 2
    frame = pd.concat([candles(rows), candles([FLAT] * 3, start=T0 + pd.Timedelta(minutes=12))])
    r = engine.run(strategy(), [snap(1)], frame, M1, start=T0.to_pydatetime(), end=(T0 + pd.Timedelta(minutes=15)).to_pydatetime(),
                   assumptions=A, spec=SPEC)
    assert r["trades"] == [] or r["trades"][0]["entry_time"] == at(1)
    gap_only = pd.concat([candles([FLAT]), candles([FLAT] * 3, start=T0 + pd.Timedelta(minutes=12))])
    r = engine.run(strategy(), [snap(1)], gap_only, M1, start=T0.to_pydatetime(),
                   end=(T0 + pd.Timedelta(minutes=15)).to_pydatetime(), assumptions=A, spec=SPEC)
    assert r["trades"] == [] and r["diagnostics"]["skipped"]["entry_gap"] == 1


def test_decision_is_kept_when_the_cutoff_candle_is_missing():
    # candles at 14:00 and 14:02 (14:01 missing): the decision at 14:01 executes at the 14:02 open
    frame = pd.concat([candles([FLAT]), candles([FLAT, FLAT], start=T0 + pd.Timedelta(minutes=2))])
    r = engine.run(strategy(exit={"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0}, "target": {"type": "none"},
                                  "max_holding_minutes": 0}), [snap(1)], frame, M1, start=T0.to_pydatetime(),
                   end=(T0 + pd.Timedelta(minutes=4)).to_pydatetime(), assumptions=A, spec=SPEC)
    assert r["trades"][0]["entry_time"] == at(2) and r["trades"][0]["exit_reason"] == "end_of_data"


def test_on_change_trigger_and_exposure_limits():
    persistent = [snap(m) for m in (1, 2, 3, 4)]
    hold = {"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0}, "target": {"type": "none"}, "max_holding_minutes": 0}
    r = bt(strategy(exit=hold), persistent, [FLAT] * 6)
    assert r["diagnostics"]["setups"] == 1 and len(r["trades"]) == 1
    every = strategy(exit=hold, entry={"trigger": "every_step"}, risk={"max_open_positions": 2, "lots": "0.10"})
    r = bt(every, persistent, [FLAT] * 6)
    assert r["diagnostics"]["setups"] == 4 and r["diagnostics"]["entries"] == 2 and r["diagnostics"]["skipped"]["exposure"] == 2
    lots_cap = strategy(exit=hold, entry={"trigger": "every_step"}, risk={"max_open_positions": 5, "lots": "0.10", "max_exposure_lots": "0.25"})
    assert bt(lots_cap, persistent, [FLAT] * 6)["diagnostics"]["entries"] == 2


def test_risk_percent_sizing_and_minimum_lot():
    # risk 0.5% of 10,000 = 50 USD; risk per oz = 100.25 - 99.0 = 1.25 -> 50 / (1.25 x 100) = 0.40 lots
    cfg = strategy(risk={"sizing": "risk_percent", "risk_percent": 0.5})
    (t,) = bt(cfg, [snap(1)], [FLAT, FLAT, (99.5, 99.6, 98.9, 99.0)])["trades"]
    assert t["lots"] == "0.40" and t["net_pnl"] == round(-1.3 * 40 - 0.4 * 3.5 * 2, 2)
    big = strategy(risk={"sizing": "fixed_lots", "lots": "1.50", "max_exposure_lots": "5.00"})            # 150 oz x 100.25 = 15,038 > 10,000 at 1:1
    r = bt(big, [snap(1)], [FLAT] * 4)
    assert r["trades"] == [] and r["diagnostics"]["skipped"]["insufficient_margin"] == 1
    levered = engine.Assumptions.from_mapping({**A.to_mapping(), "leverage": 10})
    assert len(bt(big, [snap(1)], [FLAT] * 4, assumptions=levered)["trades"]) == 1
    tiny = strategy(risk={"sizing": "risk_percent", "risk_percent": 0.01})       # 1 USD / 125 = 0.008 < 0.01 lot
    r = bt(tiny, [snap(1)], [FLAT] * 4)
    assert r["trades"] == [] and r["diagnostics"]["skipped"]["below_min_lot"] == 1


def test_window_force_close_and_incomplete_candles():
    hold = {"stop": {"type": "structure", "timeframe": "M1", "buffer_atr": 0}, "target": {"type": "none"}, "max_holding_minutes": 0}
    r = bt(strategy(exit=hold), [snap(1)], [FLAT] * 6, end_minute=3, force=True)
    (t,) = r["trades"]
    assert (t["exit_reason"], t["exit_time"]) == ("window_end", at(3))
    frame = candles([FLAT] * 3)
    frame.loc[frame.index[-1], "closed"] = False
    with pytest.raises(engine.BacktestError, match="incomplete"):
        engine.run(strategy(), [snap(1)], frame, M1, start=T0.to_pydatetime(), end=(T0 + pd.Timedelta(minutes=3)).to_pydatetime(),
                   assumptions=A, spec=SPEC)


def test_mfe_mae_and_holding():
    hold = {"stop": {"type": "fixed", "distance": 5}, "target": {"type": "none"}, "max_holding_minutes": 3}
    (t,) = bt(strategy(exit=hold), [snap(1)], [FLAT, (100.0, 101.25, 99.75, 100.0), (100.0, 100.1, 99.25, 99.9), FLAT, FLAT])["trades"]
    assert (t["mfe"], t["mae"], t["holding_minutes"]) == (1.0, 1.0, 3.0)        # vs fill 100.25
    assert t["mfe_r"] == round(1.0 / (100.25 - 95.0), 3)


# ------------------------------------------------------------------------------- metrics
def trade(net, r=None, **kw):
    return {"net_pnl": net, "r_multiple": r, "commission": 0.7, "spread_cost": 2.0, "slippage_cost": 1.0, "exit_reason": "stop",
            "mfe": 1.0, "mae": 0.5, "mfe_r": 0.8, "mae_r": 0.4, "holding_minutes": 10.0, "session": "London", **kw}


def test_metrics_against_hand_calculations():
    trades = [trade(30, 1.5), trade(-10, -1.0), trade(20, 1.0), trade(-10, -1.0), trade(0, 0.0)]
    m = metrics.compute(trades, starting_balance=1000, min_trades=5,
                        closed_equity=[{"time": "t", "equity": e} for e in (1030, 1020, 1040, 1030, 1030)])
    assert (m["trades"], m["wins"], m["losses"], m["breakeven"]) == (5, 2, 2, 1)
    assert (m["gross_profit"], m["gross_loss"], m["net"], m["profit_factor"]) == (50.0, 20.0, 30.0, 2.5)
    assert (m["win_rate"], m["average_outcome"], m["expectancy_r"]) == (0.4, 6.0, 0.1)
    assert m["average_win"] == 25.0 and m["average_loss"] == -10.0
    assert (m["max_drawdown"], m["max_drawdown_pct"]) == (10.0, 0.97)
    lo, hi = m["win_rate_ci95"]
    assert 0.11 < lo < 0.12 and 0.76 < hi < 0.78                                      # Wilson, n = 5, p = 0.4
    assert m["average_holding_minutes"] == 10.0 and m["total_commission"] == 3.5


def test_metrics_are_withheld_for_small_samples_and_undefined_ratios():
    m = metrics.compute([trade(30, 1.5), trade(-10, -1.0)], starting_balance=1000, min_trades=30)
    assert m["win_rate"] is None and m["profit_factor"] is None and m["expectancy_r"] is None
    assert m["net"] == 20.0 and m["trades"] == 2 and "insufficient sample" in m["reason"]
    m = metrics.compute([trade(5, 0.5), trade(5, 0.5)], starting_balance=1000, min_trades=2)
    assert m["profit_factor"] is None and "profit_factor" in m["withheld"] and "no losing trade" in m["profit_factor_reason"]
    rows = metrics.breakdown([trade(5, session="London"), trade(-5, session="New York")], "session", starting_balance=1000, min_trades=1)
    assert [(r["group"], r["net"]) for r in rows] == [("London", 5.0), ("New York", -5.0)]


def test_drawdown_series():
    series = metrics.drawdown_series([{"time": "a", "equity": 1010}, {"time": "b", "equity": 990}, {"time": "c", "equity": 1020}], 1000)
    assert [p["drawdown"] for p in series] == [0.0, -20.0, 0.0]


# ------------------------------------------------------------------------------- walk-forward
def test_fold_boundaries_rolling_anchored_and_embargo():
    s = datetime(2026, 11, 1, tzinfo=UTC)
    folds = walk_forward.make_folds(s, s + timedelta(days=11), dev=timedelta(days=4), evaluation=timedelta(days=2),
                                    step=timedelta(days=2), embargo=timedelta(hours=1))
    assert len(folds) == 3                       # the 4th would end at day 12 h 1, past the range end
    for f in folds:
        assert f["dev_end"] + timedelta(hours=1) == f["eval_start"] and f["eval_end"] - f["eval_start"] == timedelta(days=2)
        assert f["dev_end"] <= f["eval_start"]                                           # chronological separation
    assert folds[1]["dev_start"] == s + timedelta(days=2)
    anchored = walk_forward.make_folds(s, s + timedelta(days=10), dev=timedelta(days=4), evaluation=timedelta(days=2),
                                       step=timedelta(days=2), embargo=timedelta(0), anchored=True)
    assert all(f["dev_start"] == s for f in anchored)
    with pytest.raises(walk_forward.WalkForwardError):
        walk_forward.make_folds(s, s + timedelta(days=1), dev=timedelta(days=4), evaluation=timedelta(days=2),
                                step=timedelta(days=2), embargo=timedelta(0))


def wf_setup(eval_spike: bool):
    """Two candidates on synthetic data: 'long' gains in development, 'short' loses there. The evaluation window
    can be changed drastically; the SELECTION must not change (it only reads development trades)."""
    # 7-minute holds do not line up with the 60-minute windows, so the last trade of a window must be force-closed
    hold = {"stop": {"type": "fixed", "distance": 50}, "target": {"type": "none"}, "max_holding_minutes": 7}
    long_c = strategy(name="long", exit=hold, entry={"trigger": "every_step"})
    short_c = strategy(name="short", direction="short", exit=hold, entry={"trigger": "every_step"},
                       conditions=[{"id": "s", "type": "structure_state", "timeframe": "M1", "state": "against"}])
    n = 240
    prices = 100 + np.arange(n) * 0.05                                                 # rising: longs win
    if eval_spike:
        prices[150:] = prices[150:] - np.arange(n - 150) * 0.2                         # evaluation window falls hard
    rows = [(p, p + 0.1, p - 0.1, p) for p in prices]
    snaps = [snap(m) for m in range(1, n)]
    tl = {"M1": {"snapshots": snaps, "cutoffs": [pd.Timestamp(s["cutoff"]) for s in snaps]}}
    folds = walk_forward.make_folds(T0.to_pydatetime(), (T0 + pd.Timedelta(minutes=n - 10)).to_pydatetime(),
                                    dev=timedelta(minutes=120), evaluation=timedelta(minutes=60),
                                    step=timedelta(minutes=60), embargo=timedelta(minutes=10))
    return [long_c, short_c], tl, candles(rows), folds


def test_selection_uses_development_data_only():
    results = []
    for spike in (False, True):
        cands, tl, frame, folds = wf_setup(spike)
        results.append(walk_forward.run(cands, tl, frame, M1, folds, mode="select", objective="net", min_dev_trades=1,
                                        assumptions=A, spec=SPEC))
    calm, spiked = results
    assert [f["selected"] for f in calm["folds"]] == [f["selected"] for f in spiked["folds"]]
    first = calm["folds"][0]
    assert first["selected"] == "long"
    assert spiked["out_of_sample"]["net"] != calm["out_of_sample"]["net"]               # but evaluation results differ


def test_no_trade_spans_development_and_evaluation():
    cands, tl, frame, folds = wf_setup(False)
    r = walk_forward.run(cands[:1], tl, frame, M1, folds, mode="fixed", objective="net", min_dev_trades=1, assumptions=A, spec=SPEC)
    for t in r["out_of_sample_trades"]:
        f = folds[t["fold"] - 1]
        assert pd.Timestamp(f["eval_start"]) <= pd.Timestamp(t["entry_time"]) and pd.Timestamp(t["exit_time"]) <= pd.Timestamp(f["eval_end"])
    assert any(t["exit_reason"] == "window_end" for t in r["out_of_sample_trades"])
    assert r["out_of_sample"]["trades"] == sum(f["evaluation"]["trades"] for f in r["folds"])
    assert round(sum(f["evaluation"]["net"] for f in r["folds"]), 2) == r["out_of_sample"]["net"]
    assert r["variation"]["folds"] == len(folds) and all("candidates" in f for f in r["folds"])


# ------------------------------------------------------------------------------- features: no look-ahead, reproducibility
@pytest.fixture(scope="module")
def rec():
    m1, h1 = recorded("mt5_xauusd_m1_recorded.csv"), recorded("mt5_xauusd_h1_recorded.csv")
    return m1, h1


def test_snapshots_do_not_depend_on_later_candles(rec):
    m1, h1 = rec
    cut_end = pd.Timestamp("2026-11-13T12:00Z")
    full = replay.ReplayDataset("r", "mt5", M1, m1, history={"H1": h1})
    cut = replay.ReplayDataset("r", "mt5", M1, m1[m1.index < cut_end], history={"H1": h1})
    cuts = features.step_cutoffs(cut, Timeframe.M15, datetime(2026, 11, 13, 9, tzinfo=UTC), cut_end.to_pydatetime())
    a = features.compute_timeline(full, Timeframe.M15, cuts, default_config(), detail={"M5"})
    b = features.compute_timeline(cut, Timeframe.M15, cuts, default_config(), detail={"M5"})
    assert a == b and len(a) == 12
    assert features.dataset_hash(full) != features.dataset_hash(cut)


def test_backtests_are_reproducible_and_ignore_future_candles(rec):
    m1, h1 = rec
    start, end = datetime(2026, 11, 13, 6, tzinfo=UTC), datetime(2026, 11, 13, 14, tzinfo=UTC)
    configs = [builder.templates()[2], builder.templates()[0]]
    ds = replay.ReplayDataset("r", "mt5", M1, m1[m1.index < pd.Timestamp(end)], history={"H1": h1})
    one = lab.run_strategies(ds, m1, configs, start, end, default_config(), A, SPEC)
    two = lab.run_strategies(ds, m1, configs, start, end, default_config(), A, SPEC)
    assert [s["result_hash"] for s in one["strategies"]] == [s["result_hash"] for s in two["strategies"]]
    future = m1.copy()
    future.loc[future.index >= pd.Timestamp(end) + pd.Timedelta(hours=3), ["open", "high", "low", "close"]] *= 1.10
    three = lab.run_strategies(ds, future, configs, start, end, default_config(), A, SPEC)
    for a, b in zip(one["strategies"], three["strategies"], strict=True):
        before = [t for t in a["trades"] if pd.Timestamp(t["exit_time"]) <= pd.Timestamp(end) + pd.Timedelta(hours=3)]
        assert before == [t for t in b["trades"] if pd.Timestamp(t["exit_time"]) <= pd.Timestamp(end) + pd.Timedelta(hours=3)]
        assert [t["setup_cutoff"] for t in a["trades"]] == [t["setup_cutoff"] for t in b["trades"]]
    assert one["comparison"][0]["steps"] == one["comparison"][1]["steps"]                 # identical inputs
    assert one["assumptions"] == A.to_mapping() and one["dataset_hash"] == features.dataset_hash(ds)


# ------------------------------------------------------------------------------- exports
def sample_run():
    r = bt(strategy(), [snap(1)], [FLAT, FLAT, FLAT, (99.5, 99.6, 98.9, 99.0), FLAT])
    r["metrics"] = metrics.full(r, starting_balance=10000, min_trades=1)
    r["config"] = strategy()
    r["conditions_text"] = builder.describe(r["config"])
    r["steps"] = 1
    r["strategy"].update(id=7, version=3)
    r.pop("_trades")
    result = {"range": {"start": at(0), "end": at(5)}, "series": "csv:test", "base_tf": "M1", "dataset_hash": "ab" * 32,
              "analysis_config": "cfgfp", "assumptions": A.to_mapping(), "strategies": [r], "comparison": [],
              "disclaimer": lab.DISCLAIMER, "limitations": lab.LIMITATIONS}
    return {"id": "rr_test", "kind": "backtest", "name": "unit", "series_key": "csv:test", "params": {},
            "config_fingerprint": "cfgfp", "result": result}


def test_csv_exports_carry_metadata_and_exact_rows():
    run = sample_run()
    text = exports.trades_csv(run)
    meta = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert any("dataset: csv:test" in ln for ln in meta) and any("date range (UTC)" in ln for ln in meta)
    assert any("assumptions:" in ln and "slippage=0.05" in ln for ln in meta) and any("limitation 1" in ln for ln in meta)
    rows = list(csv.DictReader(io.StringIO("\n".join(ln for ln in text.splitlines() if not ln.startswith("#")))))
    assert len(rows) == 1 and rows[0]["net_pnl"] == "-13.7" and rows[0]["exit_reason"] == "stop" and rows[0]["strategy"] == "test"
    summary = exports.summary_csv(run)
    body = [ln for ln in summary.splitlines() if not ln.startswith("#")]
    assert body[0] == "metric,test" and "net,-13.7" in body and any(ln.startswith("by_session") for ln in body)


def test_strategy_json_round_trips_through_validation():
    record = {"id": 7, "version": 3, "fingerprint": "f", "note": None, "config": builder.templates()[0]}
    out = exports.strategy_json(record)
    assert out["schema"] == "strategy@1" and builder.validate(out["config"]) == record["config"]


def test_pdf_report_is_well_formed_and_states_its_limits():
    data = exports.report_pdf(sample_run())
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
    offsets = re.findall(rb"(\d{10}) 00000 n ", data[xref:])
    assert all(data[int(o):].startswith(b"%d 0 obj" % (i + 1)) for i, o in enumerate(offsets))
    for needle in (b"Limitations", b"csv:test", b"Dataset, range and assumptions", b"do not predict future"):
        assert needle in data
    assert len(PDF().output()) > 100                                                    # an empty document still works
