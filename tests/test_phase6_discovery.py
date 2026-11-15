"""Phase 6 discovery@1: sealed and forward data are never read in development, a hypothesis gets at most four trials,
refinements are built by the tool from allowed kinds only, predictions are checked as pre-registered, and trials
refuse to run unless the protocol is frozen. The end-to-end run uses SYNTHETIC data."""
# ruff: noqa: F811 - pytest fixtures (world) are shared from the Phase 5 test module
from __future__ import annotations

import time
import warnings
from datetime import UTC, datetime

import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import discovery_report as dr
from goldscalp.monthly.standalone import create_research_app
from tests.test_phase5_monthly import NOW, _table_counts, world  # noqa: F401

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


def d(*a):
    return datetime(*a, tzinfo=UTC)


def test_development_windows_never_touch_sealed_or_forward_data():
    ws = dv.dev_windows()
    assert len(ws) == 11 and ws[0]["start"] == "2026-01-01T00:00:00Z" and ws[-1]["end"] == "2026-11-14T00:00:00Z"
    for w in ws:
        dv.guard(datetime.fromisoformat(w["start"].replace("Z", "+00:00")), datetime.fromisoformat(w["end"].replace("Z", "+00:00")), 5)
    with pytest.raises(dv.DiscoveryError, match="sealed"):
        dv.guard(d(2024, 5, 1), d(2024, 6, 1), 5)
    with pytest.raises(dv.DiscoveryError, match="sealed"):          # look-back and coarser history would reach into it
        dv.guard(d(2025, 11, 20), d(2025, 12, 1), 5)
    with pytest.raises(dv.DiscoveryError, match="forward"):
        dv.guard(d(2026, 11, 1), d(2026, 12, 1), 5)
    with pytest.raises(dv.DiscoveryError, match="purpose"):
        dv.guard(d(2026, 3, 1), d(2026, 4, 1), 5, purpose="validation")


def test_the_specification_pre_registers_three_complete_hypotheses():
    spec = dv.load_spec()
    assert [h["id"] for h in spec["hypotheses"]] == ["H6.1", "H6.2", "H6.3"]
    for h in spec["hypotheses"]:
        for field in ("question", "hypothesis", "mechanism", "entry", "exit", "invalidation", "prediction", "baselines"):
            assert h[field], (h["id"], field)
    assert set(spec["configs"]) == {"VOL-C", "VOL-F", "STR-X", "STR-0", "SES-B"}
    assert len(spec["sealed_validation"]["criteria"]) == 8


def test_refinements_are_built_by_the_tool_and_change_only_what_they_may():
    spec = dv.load_spec()
    h1, h2, h3 = (dv.hypothesis(spec, k) for k in ("H6.1", "H6.2", "H6.3"))
    base = spec["configs"]["VOL-C"]
    vol = dv.refine(base, "filter", {"type": "volatility_regime", "timeframe": "M15", "allowed": ["expanding"]}, h1)
    assert vol["conditions"][:-1] == base["conditions"] and vol["conditions"][-1]["type"] == "volatility_regime"
    assert vol["exit"] == base["exit"]
    with pytest.raises(dv.DiscoveryError):                           # H6.1 does not name a session filter
        dv.refine(base, "filter", {"type": "session", "sessions": ["London"], "overlap": "any"}, h1)
    with pytest.raises(dv.DiscoveryError):                           # H6.2 allows no filter at all
        dv.refine(spec["configs"]["STR-X"], "filter", {"type": "volatility_regime", "timeframe": "M15", "allowed": ["expanding"]}, h2)
    stop = dv.refine(base, "stop_timeframe", "H1", h1)
    assert stop["exit"]["stop"]["timeframe"] == "H1" and stop["conditions"] == base["conditions"]
    longer = dv.refine(spec["configs"]["SES-B"], "exit_horizon", 2, h3)
    assert longer["exit"]["max_holding_minutes"] == 720 and longer["conditions"] == spec["configs"]["SES-B"]["conditions"]
    for bad in (("exit_horizon", 1.5), ("stop_timeframe", "M15"), ("grid", 3)):
        with pytest.raises(dv.DiscoveryError):
            dv.refine(base, *bad, h1)


def _s(n, e):
    return {"trades": n, "expectancy_r": e, "sum_r": n * e}


def test_predictions_are_checked_exactly_as_pre_registered():
    spec = dv.load_spec()
    p1, p2, p3 = (dv.hypothesis(spec, k)["prediction"] for k in ("H6.1", "H6.2", "H6.3"))
    good = {"VOL-C": {"by_vol_m15": {"expanding": _s(30, 0.2), "contracting": _s(25, -0.1)}},
            "VOL-F": {"by_vol_m15": {"contracting": _s(22, 0.15), "expanding": _s(40, -0.2)}}}
    assert dv.check_prediction(p1, good)["holds"]
    thin = {**good, "VOL-F": {"by_vol_m15": {"contracting": _s(10, 0.5), "expanding": _s(40, -0.2)}}}
    assert not dv.check_prediction(p1, thin)["holds"]                 # favoured cell has too few trades
    flipped = {**good, "VOL-C": {"by_vol_m15": {"expanding": _s(30, -0.05), "contracting": _s(25, 0.1)}}}
    assert not dv.check_prediction(p1, flipped)["holds"]
    assert dv.check_prediction(p2, {"STR-X": {"pooled": _s(80, 0.1)}, "STR-0": {"pooled": _s(200, 0.0)}})["holds"]
    assert not dv.check_prediction(p2, {"STR-X": {"pooled": _s(40, 0.3)}, "STR-0": {"pooled": _s(200, 0.0)}})["holds"]
    months = {f"2026-{m:02d}": _s(8, 0.1 if m % 5 else -0.1) for m in range(1, 11)}
    ses = {"SES-B": {"by_session": {"London open": _s(80, 0.1), "Asian": _s(90, -0.05)}, "london_open_months": months}}
    assert dv.check_prediction(p3, ses)["holds"]
    ses["SES-B"]["london_open_months"] = {f"2026-{m:02d}": _s(8, -0.1 if m % 2 else 0.1) for m in range(1, 11)}
    assert not dv.check_prediction(p3, ses)["holds"]                  # 5 of 10 months is below 60 %


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    windows = [{"key": f"D{i + 1}", "start": a, "end": b, "month": a[:10], "store": "app", "series": "live:mt5:XAUUSD",
                "partial": True}
               for i, (a, b) in enumerate([("2026-09-30T07:00:00Z", "2026-09-30T13:00:00Z"),
                                           ("2026-10-01T07:00:00Z", "2026-10-01T13:00:00Z"),
                                           ("2026-10-05T07:00:00Z", "2026-10-05T13:00:00Z")])]   # the last has no data
    monkeypatch.setattr(dv, "dev_windows", lambda: windows)
    monkeypatch.setattr(dr, "check", lambda: {"frozen": True, "unchanged": True})
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def _wait(c, tid):
    for _ in range(3000):
        s = c.get(f"/api/monthly/discovery/trials/{tid}").json()
        if not s["running_job"] and s["status"] in ("COMPLETED", "FAILED"):
            return s
        time.sleep(0.1)
    raise AssertionError("trial did not finish")


def test_baseline_runs_on_development_windows_and_refinements_are_capped(client, world):
    before = _table_counts(world["app"])
    r = client.post("/api/monthly/discovery/trials", json={"hypothesis": "H6.3"})
    assert r.status_code == 201, r.text
    s = _wait(client, r.json()["id"])
    assert s["status"] == "COMPLETED", s["error"]
    a = s["analysis"]
    assert set(a["configs"]) == {"SES-B"} and "prediction" in a and a["prediction"]["holds"] in (True, False)
    assert a["configs"]["SES-B"]["months"]["2026-10-05"]["status"] == "INCOMPLETE"      # missing data is never invented
    assert client.post("/api/monthly/discovery/trials", json={"hypothesis": "H6.3"}).status_code == 409   # one baseline
    short = {"hypothesis": "H6.3", "kind": "refinement", "parent": "SES-B", "rationale": "looks good",
             "refinement": {"kind": "exit_horizon", "value": 2}}
    assert "rationale" in client.post("/api/monthly/discovery/trials", json=short).json()["detail"]
    ms = client.app.state.monthly
    why = "Development evidence: London-open breaks ran for the full holding time; test a longer horizon once."
    for ref in ({"kind": "exit_horizon", "value": 2}, {"kind": "stop_timeframe", "value": "M5"},
                {"kind": "filter", "value": {"type": "session", "sessions": ["Asian", "London"], "overlap": "any"}}):
        dv.create_trial(ms, "H6.3", "refinement", rationale=why, parent="SES-B", refinement=ref)
    with pytest.raises(dv.DiscoveryError, match="all 4 trials"):
        dv.create_trial(ms, "H6.3", "refinement", rationale=why, parent="SES-B", refinement={"kind": "exit_horizon", "value": 0.5})
    assert _table_counts(world["app"]) == before                                          # the app database is never written


def test_all_baselines_run_as_one_job_sharing_each_timeline(client):
    r = client.post("/api/monthly/discovery/baselines")
    assert r.status_code == 201, r.text
    tids = [t["id"] for t in r.json()["trials"]]
    assert [t["hypothesis"] for t in r.json()["trials"]] == ["H6.1", "H6.2", "H6.3"]
    done = [_wait(client, t) for t in tids]
    assert all(s["status"] == "COMPLETED" for s in done), [s["error"] for s in done]
    assert [set(s["analysis"]["configs"]) for s in done] == [{"VOL-C", "VOL-F"}, {"STR-X", "STR-0"}, {"SES-B"}]
    assert all("prediction" in s["analysis"] for s in done) and all(s["params"]["trial_number"] == 1 for s in done)
    datasets = {}
    con = client.app.state.monthly.store
    with con.read() as c:
        for row in c.execute("SELECT window, dataset FROM discovery_results WHERE dataset IS NOT NULL"):
            datasets.setdefault(row["window"], set()).add(row["dataset"])
    assert datasets and all(len(v) == 1 for v in datasets.values())                         # one shared dataset per window
    assert client.post("/api/monthly/discovery/baselines").status_code == 409


def test_monthly_evaluations_cannot_open_the_sealed_block(client):
    for month in ("2023-10", "2024-06", "2025-10"):
        r = client.post("/api/monthly/evaluations", json={"month": month, "store": "app", "series": "live:mt5:XAUUSD",
                                                          "research_days": 60, "configured_spread": "0.30",
                                                          "commission": "3.50", "slippage": "0.05"})
        assert r.status_code == 409 and "SEALED" in r.json()["detail"], (month, r.text)


def test_trials_are_refused_unless_the_protocol_is_frozen(client, monkeypatch):
    monkeypatch.setattr(dr, "check", lambda: {"frozen": False, "unchanged": False})
    assert client.post("/api/monthly/discovery/trials", json={"hypothesis": "H6.1"}).status_code == 409
    monkeypatch.setattr(dr, "check", lambda: {"frozen": True, "unchanged": True})
    assert client.post("/api/monthly/discovery/trials", json={"hypothesis": "H9.9"}).status_code == 422


def test_protocol_freeze_is_versioned_and_detects_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "REPORTS", tmp_path)
    monkeypatch.setattr(dr, "MANIFEST", tmp_path / "discovery-protocol-frozen.json")
    monkeypatch.setattr(dr, "trial_count", lambda store=None: 0)
    assert dr.check()["frozen"] is False
    first = dr.freeze("before any trial")
    assert dr.check()["unchanged"] and dr.check()["trials_at_freeze"] == 0
    assert dr.freeze("amendment").name.endswith("-v2.json") and first.exists()
    monkeypatch.setattr(dr, "hashes", lambda: {f: "x" for f in dr.FILES})
    assert dr.check()["unchanged"] is False
