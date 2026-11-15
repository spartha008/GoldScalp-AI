"""Phase 5.2 final classification (verdicts@1): pre-declared criteria and classes on SYNTHETIC stored results."""
from __future__ import annotations

import pytest

from goldscalp.monthly import robustness as rb
from goldscalp.monthly import verdicts as vd

MONTHS = {"W1": "2026-06", "W2": "2026-07", "W3": "2026-08", "W4": "2026-09", "W5": "2026-10", "W6": "2026-11"}
WINDOWS = [{"key": k, "start": f"{m}-01T00:00:00Z", "end": f"{m}-28T00:00:00Z", "month": m, "partial": False,
            "role": "development" if k in ("W1", "W2", "W3", "W4") else "hold-out"} for k, m in MONTHS.items()]


def trades(rs, window, cost=0.7, risk=10.0):
    m = MONTHS[window]
    return [{"entry": f"{m}-{(i % 27) + 1:02d}T10:{i % 60:02d}:00Z", "exit": f"{m}-{(i % 27) + 1:02d}T11:{i % 60:02d}:00Z",
             "side": "buy", "lots": "0.02", "reason": "target" if r > 0 else "stop", "net": r * risk, "r": r,
             "mfe_r": max(r, 0.5), "mae_r": 0.5, "hold": 60, "session": "London", "vol_m5": "normal", "vol_m15": "normal",
             "h1": "with", "m15": "with", "stop_atr": 1.5, "risk_usd": risk, "cost_usd": cost} for i, r in enumerate(rs)]


def results(table):
    """table: {(key, tf, window): [r, ...]}"""
    return {(k, tf, w): {"status": "COMPLETED", "reason": None, "metrics": None, "trades": trades(rs, w)}
            for (k, tf, w), rs in table.items()}


def params(keys):
    return {"stage": "full", "windows": WINDOWS, "folds": rb.folds(WINDOWS),
            "configs": [{"key": "S|original", "strategy": "S", "role": "benchmark"}] +
                       [{"key": k, "strategy": "S", "role": "redesign"} for k in keys]}


def good(n=12):                      # 12 trades: 6 x +1.5R, 6 x -1R -> +0.25 R/trade
    return [1.5, -1.0] * (n // 2)


def build(variant_rs, original_rs=None, m15=True):
    t = {}
    for w in MONTHS:
        t[("S|v-a", "M5", w)] = variant_rs(w)
        t[("S|original", "M5", w)] = (original_rs or (lambda _w: [-1.0, 0.5] * 6))(w)
        if m15:
            t[("S|v-a", "M15", w)] = variant_rs(w)
            t[("S|original", "M15", w)] = (original_rs or (lambda _w: [-1.0, 0.5] * 6))(w)
    return results(t)


def test_stressed_r_scales_only_spread_and_slippage():
    t = trades([1.0], "W3", cost=0.7, risk=10.0)[0]              # commission 0.14, fills 0.56
    assert vd.stressed_r(t, 1.0) == pytest.approx(1.0)
    assert vd.stressed_r(t, 2.0) == pytest.approx(1.0 - 0.056)
    assert vd.stressed_r(t, 3.0) == pytest.approx(1.0 - 0.112)


def test_promising_requires_every_criterion():
    out = vd.final(build(lambda w: good()), params(["S|v-a"]))
    red = out["strategies"]["S"]["redesign"]
    assert red["class"] == "promising", red["why"]
    assert all(c["pass"] for c in red["criteria"].values())
    assert out["strategies"]["S"]["original"]["class"] == "rejected"          # the benchmark is classified too


def test_one_exceptional_month_is_inconclusive():
    out = vd.final(build(lambda w: [5.0] * 10 + [-1.0] * 2 if w == "W6" else [-1.0, 0.8] * 6), params(["S|v-a"]))
    red = out["strategies"]["S"]["redesign"]
    assert red["class"] == "inconclusive" and not red["criteria"]["consistency"]["pass"]


def test_too_few_trades_is_inconclusive_when_positive_and_rejected_when_not():
    out = vd.final(build(lambda w: [1.5, -1.0] * 3), params(["S|v-a"]))      # 6 per month -> 24 OOS trades
    assert out["strategies"]["S"]["redesign"]["class"] == "inconclusive"
    assert not out["strategies"]["S"]["redesign"]["criteria"]["trade_count"]["pass"]
    out = vd.final(build(lambda w: [-1.0, 0.5] * 3), params(["S|v-a"]))
    assert out["strategies"]["S"]["redesign"]["class"] == "rejected"


def test_overfit_strong_development_weak_unseen():
    # strong in June-July (where it is chosen), negative in every out-of-sample month
    out = vd.final(build(lambda w: [3.0, -1.0] * 8 if w in ("W1", "W2") else [-1.0, 0.2] * 8), params(["S|v-a"]))
    red = out["strategies"]["S"]["redesign"]
    assert red["development"]["expectancy_r"] >= vd.OVERFIT_DEV_R and red["oos"]["expectancy_r"] < 0
    assert red["class"] == "overfit"


def test_execution_sensitivity_and_timeframe_stability_fail_separately():
    def thin(w):
        return [0.12, 0.05] * 8                                    # +0.085 R/trade, wiped out by doubled fills
    t = build(thin)
    for k in list(t):
        for x in t[k]["trades"]:
            x["cost_usd"] = 3.0                                    # large fill costs relative to a $10 risk
    out = vd.final(t, params(["S|v-a"]))
    red = out["strategies"]["S"]["redesign"]
    assert not red["criteria"]["execution"]["pass"] and red["class"] == "inconclusive"
    out = vd.final(build(lambda w: good(), m15=False), params(["S|v-a"]))
    red = out["strategies"]["S"]["redesign"]
    assert not red["criteria"]["timeframes"]["pass"] and red["class"] == "inconclusive"


def test_november_choice_never_uses_october():
    # v-a is better on June-September; v-b is better only once October is included
    def a(w):
        return good() if w in ("W1", "W2", "W3", "W4") else [-1.0, 0.5] * 6

    def b(w):
        return [0.2, -0.1] * 6 if w != "W5" else [9.0] * 12
    t = {}
    for w in MONTHS:
        for key, f in (("S|v-a", a), ("S|v-b", b), ("S|original", lambda _w: [-1.0, 0.5] * 6)):
            t[(key, "M5", w)] = f(w)
            t[(key, "M15", w)] = f(w)
    out = vd.final(results(t), params(["S|v-a", "S|v-b"]))
    red = out["strategies"]["S"]["redesign"]
    assert red["chosen_per_month"]["W6"] == "S|v-a" == red["development_choice"]
    assert [f["select_on"] for f in out["folds"]][-1] == ["W1", "W2", "W3", "W4"]


def test_full_variant_distribution_and_paired_overlays_are_kept():
    t = {}
    for w in MONTHS:
        for key, rs in (("S|v-a-X1", [1.0, -1.0] * 6), ("S|v-a-X1-be", [0.5, -1.0] * 6), ("S|v-b-X1", [-1.0, 0.2] * 6),
                        ("S|original", [-1.0, 0.5] * 6)):
            t[(key, "M5", w)] = rs
    cfg = [{"key": "S|original", "strategy": "S", "role": "benchmark"},
           {"key": "S|v-a-X1", "strategy": "S", "role": "redesign", "entry": "a", "exit": "X1", "overlay": "none", "label": "a; x1"},
           {"key": "S|v-a-X1-be", "strategy": "S", "role": "redesign", "entry": "a", "exit": "X1", "overlay": "be", "label": "a; x1; be"},
           {"key": "S|v-b-X1", "strategy": "S", "role": "redesign", "entry": "b", "exit": "X1", "overlay": "none", "label": "b; x1"}]
    out = vd.final(results(t), {"stage": "full", "windows": WINDOWS, "folds": rb.folds(WINDOWS), "configs": cfg})
    s = out["strategies"]["S"]
    assert {v["key"] for v in s["variants"]} == {"S|v-a-X1", "S|v-a-X1-be", "S|v-b-X1"}      # failures included
    assert {g["entry"] for g in s["by_entry"]} == {"a", "b"} and {g["overlay"] for g in s["by_overlay"]} == {"none", "be"}
    pair = next(x for x in s["paired_overlays"] if x["entry"] == "a")
    assert pair["none"]["hold_out_r"] == pytest.approx(0.0) and pair["be"]["hold_out_r"] == pytest.approx(-0.25)
    assert pair["tr"]["hold_out_r"] is None
    assert all("expectancy_r_costs_x3" in v["hold_out"] for v in s["variants"])


def test_framework_freeze_detects_changes(tmp_path, monkeypatch):
    from goldscalp.monthly import verdicts_report as vr
    monkeypatch.setattr(vr, "FREEZE", tmp_path / "frozen.json")
    assert vr.framework_check()["frozen"] is False
    first = vr.freeze("test")
    assert vr.framework_check()["unchanged"] and vr.framework_check()["version"] == 1
    second = vr.freeze("amended before results")                   # an amendment is a NEW manifest
    assert first.exists() and second.exists() and second != first and second.name.endswith("-v2.json")
    chk = vr.framework_check()
    assert chk["version"] == 2 and len(chk["history"]) == 2 and chk["history"][0]["note"] == "test"
    monkeypatch.setattr(vr, "framework_hashes", lambda: {f: "x" for f in vr.FRAMEWORK_FILES})
    chk = vr.framework_check()
    assert chk["unchanged"] is False and set(chk["changed_files"]) == set(vr.FRAMEWORK_FILES)


def test_october_is_secondary_and_never_changes_a_class():
    def base(w):
        return good()
    for october in ([9.0] * 12, [-1.0] * 12, []):
        def f(w, october=october):
            return october if w == "W5" else base(w)
        out = vd.final(build(f), params(["S|v-a"]))
        red = out["strategies"]["S"]["redesign"]
        assert red["class"] == "promising" and "W5" not in red["months"]           # October is not an OOS month
        assert out["secondary"] == ["W5"] and out["hold_out"] == ["W6"] and out["oos_months"] == ["W3", "W4", "W6"]
        assert red["secondary_october"]["M5"]["trades"] == len(october)            # but it is reported in full
