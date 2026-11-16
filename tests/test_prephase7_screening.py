"""Pre-Phase 7 screening (pre-phase7-screen@1): the candidate file is valid and never reaches sealed or forward data,
and the declared screening rules classify mechanically."""
from __future__ import annotations

from datetime import datetime

from goldscalp.core import forward
from goldscalp.monthly import discovery
from goldscalp.research import screening as sc


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_candidate_file_is_valid_and_uses_only_spent_development_data():
    spec = sc.load()
    start, end = _t(spec["window"]["start"]), _t(spec["window"]["end"])
    assert (end - start).days in range(10, 16)                                   # a 10-15 calendar-day screen
    assert not forward.includes_forward(start, end)                              # never forward data
    assert end <= discovery.SEALED[0] or start >= discovery.SEALED[1]            # never the sealed block
    assert spec["series"]["key"] != "csv:vantage-mt5-xauusd-sealed-2023-2025"
    keys = [c["key"] for c in spec["candidates"]]
    assert len(keys) == len(set(keys)) == 14
    for c in spec["candidates"]:
        assert c["sources"] and (c["config"] is not None) == c["screened"]
        if not c["screened"]:
            assert c["not_screened_reason"] and c["pre_classification"] in ("INCONCLUSIVE", "REJECTED")
        else:
            assert c["deviations"] and c["original_rules"]


def _m(n, e, pf=1.3, x2=0.05):
    return {"trades": n, "expectancy_r": e, "profit_factor": pf, "expectancy_r_costs_x2": x2}


def test_declared_rules_classify_mechanically():
    rules = {"min_trades": 15, "min_profit_factor": 1.1}
    good_conc = {"sum_r_without_best_day": 1.0}
    assert sc.classify(_m(8, 0.4), _m(8, 0.4), good_conc, rules)[0] == "INCONCLUSIVE"            # too few trades
    assert sc.classify(_m(30, -0.01), _m(30, 0.1), good_conc, rules)[0] == "REJECTED"
    assert sc.classify(_m(30, 0.1), _m(20, 0.05), good_conc, rules)[0] == "WORTH FURTHER RESEARCH"
    one_day = {"sum_r_without_best_day": -0.5}
    assert sc.classify(_m(30, 0.1), _m(20, 0.05), one_day, rules)[0] == "INCONCLUSIVE"            # one good day
    assert sc.classify(_m(30, 0.1, x2=-0.01), _m(20, 0.05), good_conc, rules)[0] == "INCONCLUSIVE"
    assert sc.classify(_m(30, 0.1), _m(20, -0.05), good_conc, rules)[0] == "INCONCLUSIVE"         # M15 negative


def test_concentration_finds_the_best_day():
    trades = [{"entry": "2026-10-05T01:00:00Z", "r": 3.0, "net": 30}, {"entry": "2026-10-06T01:00:00Z", "r": -1.0, "net": -10},
              {"entry": "2026-10-06T05:00:00Z", "r": -1.0, "net": -10}]
    c = sc.concentration(trades)
    assert c["best_day"] == "2026-10-05" and c["sum_r_without_best_day"] == -2.0 and c["days_traded"] == 2
