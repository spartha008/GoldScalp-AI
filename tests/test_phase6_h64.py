"""Phase 6 H6.4 (sealed@1): the candidate is VOL-C plus exactly one condition, the control is the frozen VOL-C, coverage
follows the pre-registered market calendar, the sealed run is one-shot and needs a frozen protocol, development runs
never read sealed dates, and the evaluation applies the frozen criteria. End-to-end runs use SYNTHETIC data."""
# ruff: noqa: F811 - pytest fixtures (world) are shared from the Phase 5 test module
from __future__ import annotations

import time
import warnings

import pandas as pd
import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import discovery_report as dr
from goldscalp.monthly import sealed as sl
from goldscalp.monthly import sealed_report as sr
from goldscalp.monthly.standalone import create_research_app
from goldscalp.strategies import builder
from tests.test_phase5_monthly import NOW, _table_counts, world  # noqa: F401

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


def test_registration_is_one_variable_with_a_frozen_control_and_disclosed_origin():
    spec = sl.load_spec()
    cand, ctrl = spec["candidate"]["config"], spec["control"]["config"]
    assert builder.fingerprint(ctrl) == builder.fingerprint(dv.load_spec()["configs"]["VOL-C"])
    extra = [c for c in cand["conditions"] if c not in ctrl["conditions"]]
    assert len(extra) == 1 and extra[0]["type"] == "volatility_regime" and extra[0]["allowed"] == ["normal", "expanding"]
    assert not any(c["type"] == "session" for c in cand["conditions"])           # no London filter
    assert cand["exit"] == ctrl["exit"] and cand["entry"] == ctrl["entry"] and cand["risk"] == ctrl["risk"]
    assert "Development-derived" in spec["hypothesis"]["origin"] and "comparison" in spec["control"]["role"]
    assert spec["criteria"] == dv.load_spec()["sealed_validation"]["criteria"]


def test_sealed_windows_are_the_registered_24_months():
    ws = sl.sealed_windows()
    assert len(ws) == 24 and ws[0]["month"] == "2023-11" and ws[-1]["month"] == "2025-10"
    assert [w["half"] for w in ws] == [1] * 12 + [2] * 12


def test_coverage_follows_the_market_calendar_without_lowering_the_threshold():
    spec = sl.load_spec()
    hol = sl.holiday_windows(spec)
    exp = pd.date_range("2024-12-24T17:00Z", "2024-12-26T02:00Z", freq="15min", inclusive="left")
    present = exp[(exp < pd.Timestamp("2024-12-24T18:45Z")) | (exp >= pd.Timestamp("2024-12-26T00:00Z"))]
    out = sl.adjusted_share(exp, present, hol)
    assert out["holiday_missing"] == out["missing"] and out["share"] == 1.0 and out["plain_share"] < 0.95
    ordinary = pd.date_range("2024-12-10T00:00Z", "2024-12-11T00:00Z", freq="15min", inclusive="left")
    gappy = ordinary[10:]                                                     # 10 missing bars on an ordinary day
    out = sl.adjusted_share(ordinary, gappy, hol)
    assert out["holiday_missing"] == 0 and out["share"] == out["plain_share"] < 1.0
    assert spec["coverage_rule"]["threshold"] == 0.95


def _s(trades, sum_r, x2=0.01, pf=1.3):
    return {"trades": trades, "sum_r": sum_r, "expectancy_r": sum_r / trades if trades else None, "profit_factor": pf,
            "expectancy_r_costs_x2": x2, "expectancy_r_costs_x3": x2 / 2, "max_dd_r": 5.0, "win_rate": 0.4, "net_usd": 1.0}


def _series(month_sums, per=6, pf=1.3, x2=0.02, m15=0.05):
    months = {f"m{i:02d}": {"status": "COMPLETED", **_s(per, v)} for i, v in enumerate(month_sums)}
    valid = list(months)
    total = sum(month_sums)
    half = len(valid) // 2
    return {"valid": valid, "months": months, "pooled": _s(per * len(valid), total, x2=x2, pf=pf),
            "halves": {1: _s(per * half, sum(month_sums[:half])), 2: _s(per * (len(valid) - half), sum(month_sums[half:]))},
            "by_vol_m15": {"contracting": _s(20, -2.0), "normal": _s(80, 6.0), "expanding": _s(40, 3.0)},
            "m15": m15}


def test_the_frozen_criteria_decide_the_class():
    good = _series([1.0] * 16 + [-0.4] * 8)                                  # halves +12.0 and +0.8 R
    m15 = {"pooled": _s(100, 3.0)}
    a = sl.assess(good, m15, good, 24)
    assert a["class"] == "promising" and not a["failed"]
    half_bad = _series([2.0] * 12 + [-0.5] * 12)                               # second 12-month half negative
    assert sl.assess(half_bad, m15, good, 24)["class"] == "inconclusive"
    assert "consistency" in sl.assess(half_bad, m15, good, 24)["failed"]
    thin = _series([1.0] * 16)                                                 # only 16 valid months
    assert "data" in sl.assess(thin, m15, good, 24)["failed"]
    neg = _series([-1.0] * 24)
    assert sl.assess(neg, m15, good, 24)["class"] == "rejected"
    no_pred = {**good, "by_vol_m15": {"contracting": _s(20, 4.0), "normal": _s(80, 2.0)}}
    assert "prediction" in sl.assess(good, m15, no_pred, 24)["failed"]        # control: contracting must be worse
    assert "timeframes" in sl.assess(good, {"pooled": _s(100, -1.0)}, good, 24)["failed"]


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    dev = [{"key": "D1", "start": "2026-09-30T07:00:00Z", "end": "2026-09-30T13:00:00Z", "month": "2026-09-30",
            "store": "app", "series": "live:mt5:XAUUSD", "partial": True}]
    sealed_like = [{"key": f"S{i + 1}", "start": a, "end": b, "month": a[:10], "store": "app", "series": "live:mt5:XAUUSD",
                    "half": 1 if i == 0 else 2, "partial": False}
                   for i, (a, b) in enumerate([("2026-10-01T07:00:00Z", "2026-10-01T13:00:00Z"),
                                               ("2026-10-05T07:00:00Z", "2026-10-05T13:00:00Z")])]   # second: no data
    monkeypatch.setattr(dv, "dev_windows", lambda: dev)
    monkeypatch.setattr(sl, "sealed_windows", lambda: sealed_like)
    monkeypatch.setattr(dv, "SEALED_EVAL", (pd.Timestamp("2026-10-01T00:00Z").to_pydatetime(),
                                            pd.Timestamp("2026-11-01T00:00Z").to_pydatetime()))
    monkeypatch.setattr(dr, "check", lambda: {"frozen": True, "unchanged": True})
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def _wait(c, rid):
    for _ in range(3000):
        s = c.get(f"/api/monthly/h64/runs/{rid}").json()
        if not s["running_job"] and s["status"] in ("COMPLETED", "FAILED"):
            return s
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_development_then_one_sealed_run_only_after_freezing(client, world, monkeypatch):
    before = _table_counts(world["app"])
    r = client.post("/api/monthly/h64/runs", json={"purpose": "development"})
    assert r.status_code == 201, r.text
    s = _wait(client, r.json()["id"])
    assert s["status"] == "COMPLETED", s["error"] and set(s["analysis"]["configs"]) == {"H6.4", "VOL-C"}
    assert "assessment" not in s["analysis"]                                   # development decides nothing
    monkeypatch.setattr(sr, "check", lambda: {"frozen": False, "unchanged": False})
    assert client.post("/api/monthly/h64/runs", json={"purpose": "sealed"}).status_code == 409
    monkeypatch.setattr(sr, "check", lambda: {"frozen": True, "unchanged": True})
    r = client.post("/api/monthly/h64/runs", json={"purpose": "sealed"})
    assert r.status_code == 201, r.text
    s = _wait(client, r.json()["id"])
    assert s["status"] == "COMPLETED", s["error"]
    a = s["analysis"]
    assert a["assessment"]["class"] in ("promising", "inconclusive", "rejected") and set(a["configs"]["H6.4"]) == {"M5", "M15"}
    assert a["configs"]["H6.4"]["M5"]["months"]["2026-10-05"]["status"] == "INCOMPLETE"      # never invented
    again = client.post("/api/monthly/h64/runs", json={"purpose": "sealed"})
    assert again.status_code == 409 and "once" in again.json()["detail"]
    assert _table_counts(world["app"]) == before


def test_sealed_run_refuses_windows_outside_the_registration(client, monkeypatch):
    monkeypatch.setattr(sr, "check", lambda: {"frozen": True, "unchanged": True})
    monkeypatch.setattr(sl, "sealed_windows", lambda: [{"key": "S1", "start": "2026-12-01T00:00:00Z",
                                                        "end": "2027-01-01T00:00:00Z", "month": "2026-12", "store": "app",
                                                        "series": "live:mt5:XAUUSD", "half": 1}])
    r = client.post("/api/monthly/h64/runs", json={"purpose": "sealed"})
    assert r.status_code == 409 and "outside" in r.json()["detail"]


def test_compare_detects_any_disagreement_between_timeframes():
    idx = pd.date_range("2024-03-04T00:00Z", periods=6, freq="5min")
    fine = pd.DataFrame({"o": [1, 2, 3, 4, 5, 6], "h": [5, 6, 7, 8, 9, 10], "l": [0, 1, 2, 3, 4, 5], "c": [2, 3, 4, 5, 6, 7]}, index=idx)
    coarse = pd.DataFrame({"o": [1, 4], "h": [7, 10], "l": [0, 3], "c": [4, 7]}, index=idx[::3])
    assert sr.compare(fine, coarse, sl.Timeframe.M5, sl.Timeframe.M15) == {"compared": 2, "mismatched": 0}
    coarse.iloc[1, 1] = 11
    assert sr.compare(fine, coarse, sl.Timeframe.M5, sl.Timeframe.M15) == {"compared": 2, "mismatched": 1}
