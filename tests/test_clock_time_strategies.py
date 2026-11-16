"""Clock-time entries and no-stop exits in strategy@1 (added for the pre-Phase 7 research candidates).

Checks: the additions validate strictly, every existing configuration keeps its fingerprint, the clock is
daylight-saving aware, a bt@1 run of the Strategy Lab form places exactly the trades of the screened rule set, and demo
execution still refuses a trade without a stop-loss. Market data here is SYNTHETIC."""
from __future__ import annotations

import inspect
import json
from datetime import timedelta

import pandas as pd
import pytest

from goldscalp.backtesting import engine
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.execution import mirror
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import sealed as sl
from goldscalp.research import candidates as cd
from goldscalp.research import external_rules, rulesim
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder, conditions
from tests.conftest import SPEC_PATH
from tests.test_prephase7_tools import COSTS, T0, synthetic

SPEC = InstrumentSpec.load(SPEC_PATH)
CLOCK = {"id": "clock", "type": "clock_time", "timezone": "America/New_York", "times": ["05:05", "09:35"], "window_minutes": 5}


def strategy(**exit_over):
    return {"name": "t", "direction": "short", "conditions": [dict(CLOCK)],
            "exit": {"stop": {"type": "none"}, "target": {"type": "none"}, "max_holding_minutes": 30, **exit_over}}


def test_clock_time_and_no_stop_validate_strictly():
    cfg = builder.validate(strategy())
    assert cfg["conditions"][0]["times"] == ["05:05", "09:35"] and cfg["exit"]["stop"]["type"] == "none"
    assert builder.timeframes_used(cfg) == {"M5"}                              # a stop of type none reads no timeframe
    for bad in (["25:00"], ["9:30"], [], ["09:30", "09:30"], "09:30"):
        with pytest.raises(builder.StrategyError):
            builder.validate({**strategy(), "conditions": [{**CLOCK, "times": bad}]})
    with pytest.raises(builder.StrategyError):
        builder.validate({**strategy(), "conditions": [{**CLOCK, "timezone": "Mars/Phobos"}]})
    with pytest.raises(builder.StrategyError, match="needs a stop"):
        builder.validate(strategy(target={"type": "r_multiple", "r": 2.0}))
    for extra in ({"breakeven": {"enabled": True}}, {"trail": {"enabled": True}}):
        with pytest.raises(builder.StrategyError, match="needs a stop"):
            builder.validate(strategy(**extra))
    assert "clock_time" in builder.catalogue()["conditions"] and "none" in builder.catalogue()["stop_types"]


def test_existing_configurations_keep_their_fingerprints():
    assert builder.fingerprint(dv.load_spec()["configs"]["VOL-C"]).startswith("64d2a3b37538efbd")       # frozen Phase 6
    assert builder.fingerprint(sl.load_spec()["candidate"]["config"]).startswith("0019c2a629e8a3e2")    # frozen H6.4
    a = builder.templates()[0]
    again = builder.validate(json.loads(json.dumps(builder.validate(a))))
    assert builder.fingerprint(builder.validate(a)) == builder.fingerprint(again)
    assert builder.timeframes_used(builder.validate(a)) >= {"M5", "H1", "M15"}                           # stop timeframe still read


def snap(cutoff: str) -> dict:
    return {"cutoff": cutoff, "tf": {"M5": {"enough": True, "fresh": True, "problems": [], "bars": 400, "state": "ranging",
                                            "events": [], "vol": {"atr": 2.0, "condition": "normal"}}},
            "sessions": [], "price": 3000.0, "spread": 0.2}


@pytest.mark.parametrize(("cutoff", "passes"), [
    ("2026-10-05T09:05:00Z", True),      # 05:05 New York in summer (UTC-4)
    ("2026-10-05T13:35:00Z", True),      # 09:35
    ("2026-10-05T09:10:00Z", False),     # the 5-minute window has passed
    ("2026-11-16T10:05:00Z", True),      # 05:05 New York in winter (UTC-5)
    ("2026-11-16T09:05:00Z", False),     # that was 04:05 in winter
])
def test_clock_time_follows_the_local_clock_through_daylight_saving(cutoff, passes):
    cfg = builder.validate(strategy())
    d = conditions.decide(cfg, snap(cutoff))
    assert (d["setup"] == "short") is passes and "long" not in d["evaluations"]
    wrap = builder.validate({**strategy(), "conditions": [{**CLOCK, "timezone": "UTC", "times": ["23:55"], "window_minutes": 15}]})
    assert conditions.decide(wrap, snap("2026-10-06T00:05:00Z"))["setup"] == "short"                     # across midnight


def test_lab_form_places_exactly_the_screened_trades():
    """bt@1 with the Strategy Lab form of P7-FIX against the rule simulator with the registered rule, on the same bars."""
    bars = synthetic(days=30)
    start, end = (T0 + pd.Timedelta(days=10)).to_pydatetime(), (T0 + pd.Timedelta(days=24)).to_pydatetime()
    fix = next(c for c in cd.load_all() if c["id"] == "P7-FIX")
    sim = rulesim.simulate(bars, external_rules.make(fix["rule"], fix["params"]), bar_minutes=5, start=start, end=end, costs=COSTS,
                           lots=0.02, ounces_per_lot=100.0)["trades"]
    lab = next(s for s in cd.lab_strategies() if s["key"] == "P7-FIX")["versions"][0]
    assert lab["as_screened"] and lab["fingerprint_ok"]
    window = bars[(bars.index >= pd.Timestamp(start) - pd.Timedelta(minutes=5)) & (bars.index < pd.Timestamp(end))]
    snaps = [snap(iso((t + pd.Timedelta(minutes=5)).to_pydatetime())) for t in window.index]
    candles = bars.assign(closed=True)
    res = engine.run(lab["config"], snaps, candles, Timeframe.M5, start=start, end=end, spec=SPEC, force_close_at_end=True,
                     assumptions=engine.Assumptions.from_mapping({"configured_spread": "0.30", "commission_per_lot_per_side": "3.50",
                                                                  "slippage": "0.05", "starting_balance": "1000", "leverage": 500}))
    bt = res["trades"]
    assert len(bt) == len(sim) >= 15
    for x, y in zip(sorted(bt, key=lambda t: t["entry_time"]), sim, strict=True):
        assert x["entry_time"] == y["entry"] and x["exit_time"] == y["exit"] and x["exit_reason"] == "time"
        assert x["net_pnl"] == pytest.approx(y["net"], abs=0.011) and x["stop"] is None and x["r_multiple"] is None


def test_lab_forms_match_the_registered_candidates():
    lab = {s["key"]: s for s in cd.lab_strategies()}
    cands = {c["id"]: c for c in cd.load_all()}
    fix = lab["P7-FIX"]["versions"]
    v1, v2 = fix[0]["config"], fix[1]["config"]
    legs = cands["P7-FIX"]["params"]["trades"]
    assert v1["direction"] == "short" and v1["conditions"][0]["times"] == [t["enter"] for t in legs]
    assert v1["exit"]["max_holding_minutes"] == 30 and v1["exit"]["stop"]["type"] == "none" and v1["risk"]["lots"] == "0.02"
    assert {k: v for k, v in v2.items() if k not in ("exit", "description")} == {k: v for k, v in v1.items() if k not in ("exit", "description")}
    assert v2["exit"]["stop"]["type"] == "atr" and v2["exit"]["max_holding_minutes"] == 30 and not fix[1]["as_screened"]
    for key, side, enter, minutes in (("P7-TOD-LONG", "long", "11:00", 900), ("P7-TOD-SHORT", "short", "02:00", 540)):
        cfg = lab[key]["versions"][0]["config"]
        assert cfg["direction"] == side and cfg["conditions"][0]["times"] == [enter] and cfg["exit"]["max_holding_minutes"] == minutes
        assert cfg["exit"]["stop"]["type"] == "none"
    tod = {t["side"]: t for t in cands["P7-TOD"]["params"]["trades"]}
    assert tod["long"]["enter"] == "11:00" and tod["long"]["exit"] == "02:00" and tod["short"]["enter"] == "02:00"
    assert all(v["fingerprint_ok"] for s in lab.values() for v in s["versions"])


def test_demo_execution_still_refuses_a_trade_without_a_stop_loss():
    src = inspect.getsource(mirror.DemoMirror._open)
    assert 'not t["stop_loss"]' in src and "no stop-loss" in src
    assert engine.plan_stop_level({"type": "none", "timeframe": "M5"}, engine.Side.SELL, snap("2026-10-05T09:05:00Z"),
                                  engine.Decimal("3000"), engine.Decimal("3000.2"))[0] is None
    assert timedelta(minutes=30) == timedelta(minutes=cd.lab_strategies()[0]["versions"][0]["config"]["exit"]["max_holding_minutes"])
