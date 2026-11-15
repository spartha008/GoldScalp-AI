"""Phase 5.2: strategy redesign and multi-month robustness research. Protocol guards (development / walk-forward /
hold-out separation), preservation of the original strategies, the pre-declared configurations, the new
level_retest condition, verdict rules, and the study end to end on SYNTHETIC data. Simulation only."""
# ruff: noqa: F811 - pytest fixtures (world) are shared from the Phase 5 test module
from __future__ import annotations

import copy
import json
import sqlite3
import time
import warnings
from datetime import UTC, datetime

import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.monthly import data as mdata
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import service
from goldscalp.monthly.standalone import create_research_app
from goldscalp.strategies import builder, conditions
from tests.test_phase5_monthly import NOW, _table_counts, sources, world  # noqa: F401

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


# ------------------------------------------------------------------ the new condition
def test_level_retest_needs_an_aged_confirmed_break_and_price_back_near_the_level():
    snap = {"cutoff": "2026-10-01T13:30:00Z", "price": 3806.0, "spread": 0.2, "sessions": ["London"], "signal": None,
            "tf": {"M5": {"detail": True, "vol": {"atr": 4.0}, "sweeps": [
                {"type": "break", "direction": "bullish", "level_type": "PDH", "price": 3805.0,
                 "known_at": "2026-10-01T12:30:00Z"}]}}}
    c = builder.validate({"name": "t", "direction": "both", "evaluation": {"step_timeframe": "M5"},
                          "conditions": [{"id": "rt", "type": "level_retest", "timeframe": "M5", "within_candles": 24,
                                          "min_age_candles": 2, "max_distance_atr": 0.5, "level_types": ["PD"]}],
                          "risk": {"lots": "0.02"}})["conditions"][0]
    ok, why = conditions._check(c, snap, "long")
    assert ok and "retest of the confirmed bullish break of PDH 3805.0" in why            # 1.0 above = 0.25 ATR
    assert not conditions._check(c, {**snap, "price": 3808.5}, "long")[0]                 # 0.875 ATR: too far
    assert not conditions._check(c, {**snap, "price": 3804.0}, "long")[0]                 # back below the level
    fresh = copy.deepcopy(snap)
    fresh["tf"]["M5"]["sweeps"][0]["known_at"] = "2026-10-01T13:25:00Z"                   # 1 candle old: not a retest
    assert not conditions._check(c, fresh, "long")[0]
    assert not conditions._check(c, snap, "short")[0]
    with pytest.raises(builder.StrategyError):
        builder.validate({"name": "t", "direction": "both", "evaluation": {"step_timeframe": "M5"}, "risk": {"lots": "0.02"},
                          "conditions": [{"id": "rt", "type": "level_retest", "within_candles": 3, "min_age_candles": 3}]})


# ------------------------------------------------------------------ protocol
def test_windows_split_development_and_hold_out_and_folds_only_look_back():
    w = rb.windows(*rb.default_range(None))
    assert [(x["key"], x["month"], x["role"]) for x in w] == [
        ("W1", "2026-06", "development"), ("W2", "2026-07", "development"), ("W3", "2026-08", "development"),
        ("W4", "2026-09", "development"), ("W5", "2026-10", "hold-out"), ("W6", "2026-11", "hold-out")]
    assert w[0]["partial"] and w[-1]["partial"] and not w[2]["partial"]
    keys = [x["key"] for x in w]
    dev = [x["key"] for x in w if x["role"] == "development"]
    for f in rb.folds(w):
        assert f["select_on"] == [k for k in keys[: keys.index(f["test"])] if k in dev]   # earlier AND development only
    assert [f["select_on"] for f in rb.folds(w)][-1] == ["W1", "W2", "W3", "W4"]           # November: never October
    with pytest.raises(rb.RobustnessError):
        rb.windows(datetime(2026, 6, 22, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC))


def _trades(rs, month):
    return [{"entry": f"{month}-0{i + 1}T10:00:00Z", "exit": f"{month}-0{i + 1}T11:00:00Z", "side": "buy", "lots": "0.02",
             "reason": "target" if r > 0 else "stop", "net": r * 10, "r": r, "mfe_r": max(r, 0.5), "mae_r": 0.5, "hold": 60,
             "session": "London", "vol_m5": "normal", "vol_m15": "normal", "h1": "with", "m15": "with", "stop_atr": 1.2,
             "risk_usd": 10.0, "cost_usd": 0.7} for i, r in enumerate(rs)]


def _res(table):
    """table: {(key, window): [r, ...]} on M5 -> results dict as stored."""
    return {(k, "M5", w): {"status": "COMPLETED", "reason": None, "metrics": None,
                           "trades": _trades(rs, {"W1": "2026-07", "W2": "2026-08", "W3": "2026-09", "W4": "2026-10"}[w])}
            for (k, w), rs in table.items()}


def test_selection_refuses_hold_out_data_and_walk_forward_ignores_later_months():
    # X is best on W1-W2; Y is far better once W3 is included. The fold testing W3 must still choose X.
    good, bad = [1.0] * 8, [-1.0] * 8
    res = _res({("S|v-x", "W1"): good, ("S|v-x", "W2"): good, ("S|v-x", "W3"): bad, ("S|v-x", "W4"): bad,
                ("S|v-y", "W1"): [0.2] * 8, ("S|v-y", "W2"): [0.2] * 8, ("S|v-y", "W3"): [5.0] * 8, ("S|v-y", "W4"): [5.0] * 8})
    with pytest.raises(rb.RobustnessError):
        rb.select(res, ["S|v-x", "S|v-y"], ["W1", "W3"], allowed=["W1", "W2"], min_trades=1)
    assert rb.select(res, ["S|v-x", "S|v-y"], ["W1", "W2"], allowed=["W1", "W2"], min_trades=10)["chosen"] == "S|v-x"
    assert rb.select(res, ["S|v-x", "S|v-y"], ["W1", "W2", "W3"], allowed=["W1", "W2", "W3"], min_trades=10)["chosen"] == "S|v-y"
    few = rb.select(res, ["S|v-x"], ["W1", "W2"], allowed=["W1", "W2"], min_trades=30)    # 16 trades: below 30
    assert few["flag"] == "insufficient sample" and few["chosen"] == "S|v-x"
    assert rb.select(res, ["S|v-x"], ["W1"], allowed=["W1"], min_trades=30)["chosen"] is None    # 8 trades


def test_verdict_rules_are_strict_and_sizing_independent():
    base = {"trades": 40, "expectancy_r": 0.15, "profit_factor": 1.3, "positive_windows": 3}
    assert rb.verdict(base, {"expectancy_r": 0.1}, 4)["verdict"] == "consistent"
    assert rb.verdict({**base, "trades": 20}, {"expectancy_r": 0.1}, 4)["verdict"] == "insufficient sample"
    assert rb.verdict({**base, "expectancy_r": -0.01}, {"expectancy_r": 0.1}, 4)["verdict"] == "fails"
    assert rb.verdict({**base, "positive_windows": 2}, {"expectancy_r": 0.1}, 4)["verdict"] == "positive but inconsistent"
    assert rb.verdict(base, {"expectancy_r": -0.2}, 4)["verdict"] == "positive but inconsistent"


def _originals():
    out = {}
    for i, t in enumerate(builder.templates()):
        out["ABC"[i]] = {"name": t["name"], "config": builder.validate(t)}
    return out


def test_configs_preserve_the_originals_and_change_one_exit_dimension_at_a_time():
    originals = _originals()
    cfgs = rb.build_configs(originals)
    for key, o in originals.items():
        bench = [c for c in cfgs if c["strategy"] == key and c["role"] == "benchmark"]
        assert len(bench) == 1 and bench[0]["config"] == o["config"]                      # unchanged benchmark
        for c in [c for c in cfgs if c["strategy"] == key and c["role"] == "exit_experiment"]:
            assert c["config"]["conditions"] == o["config"]["conditions"]                    # entry untouched
            assert c["config"]["entry"] == o["config"]["entry"] and c["config"]["risk"] == o["config"]["risk"]
            ex_new, ex_old = c["config"]["exit"], o["config"]["exit"]
            changed = {k for k in set(ex_new) | set(ex_old) if ex_new.get(k) != ex_old.get(k)}
            assert changed and changed <= {"max_holding_minutes", "exit_on_structure_flip", "target", "stop",
                                           "breakeven", "trail"}
            assert len(changed - {"exit_on_structure_flip"}) == 1 or c["experiment"] in ("E4", "E11")
        grid = [c for c in cfgs if c["strategy"] == key and c["role"] == "redesign"]
        plain = [c for c in grid if c["overlay"] == "none"]
        n = {"A": 16, "B": 20, "C": 16}[key]                                            # entry variants x 4 exits
        assert len(plain) == n and len({c["fingerprint"] for c in grid}) == len(grid)      # complete, distinct
        for c in grid:                                                                      # paired overlays
            if c["overlay"] == "be":
                assert c["config"]["exit"]["breakeven"]["trigger_r"] == 1.0
            if c["overlay"] == "tr":
                assert c["config"]["exit"]["trail"]["mode"] == "r" and c["config"]["exit"]["target"]["type"] == "r_multiple"
        assert {c["overlay"] for c in grid} == {"none", "be", "tr"}
    orig_fps = {builder.fingerprint(o["config"]) for o in originals.values()}
    assert not any(c["fingerprint"] in orig_fps for c in cfgs if c["role"] != "benchmark")
    a3 = [c for c in cfgs if c["key"] == "A|v-a-Xa"][0]["config"]
    assert {c["type"] for c in a3["conditions"]} >= {"htf_alignment", "ema_trend", "swing_sequence", "momentum"}
    b = {c["key"]: c["config"] for c in cfgs if c["strategy"] == "B" and c["role"] == "redesign"}
    sessions = {k: next(x for x in v["conditions"] if x["type"] == "session")["sessions"] for k, v in b.items()}
    assert sessions["B|v-a-Xa"] == ["London"] and sessions["B|v-b-Xa"] == ["New York"]    # compared separately
    assert any(x["type"] == "fvg" for x in b["B|v-c-Xa"]["conditions"])                 # retest into the FVG
    assert any(x["type"] == "level_retest" for x in b["B|v-e-Xa"]["conditions"])        # level acceptance retest
    assert all(any(x["type"] == "structure_event" and x["timeframe"] == "M15" for x in b[k]["conditions"])
               for k in ("B|v-a-Xa", "B|v-b-Xa", "B|v-c-Xa", "B|v-d-Xa"))                  # confirmed structure break
    c5 = [c for c in cfgs if c["key"] == "C|v-a-Xb"][0]["config"]
    assert any(x["type"] == "structure_state" and x["state"] == "ranging" for x in c5["conditions"])


# ------------------------------------------------------------------ end to end on synthetic data
WINDOWS = [("W1", "2026-09-30T07:00:00Z", "2026-09-30T13:00:00Z", "development"),
           ("W2", "2026-09-30T13:00:00Z", "2026-09-30T19:00:00Z", "development"),
           ("W3", "2026-10-01T07:00:00Z", "2026-10-01T13:00:00Z", "hold-out"),
           ("W4", "2026-10-01T13:00:00Z", "2026-10-01T19:00:00Z", "hold-out")]


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    short = (datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC))
    monkeypatch.setattr(mdata, "month_range", lambda m: short)
    monkeypatch.setattr(service, "month_range", lambda m: short)
    monkeypatch.setattr(mdata, "MIN_RESEARCH_DAYS", 1)
    monkeypatch.setattr(rb, "windows", lambda s, e, d: [{"key": k, "start": a, "end": b, "month": a[:7], "partial": True,
                                                         "role": r} for k, a, b, r in WINDOWS])
    real = rb.build_configs
    monkeypatch.setattr(rb, "build_configs", lambda originals: [c for c in real(originals) if c["strategy"] in ("A", "R6")
                                                                and (c["role"] != "redesign" or c["key"].endswith("Xa"))])
    monkeypatch.setattr(rb, "MIN_DEV_TRADES", 1)
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def _wait(c, path, done):
    for _ in range(6000):
        s = c.get(path).json()
        if not s.get("running_job") and s["status"] in done:
            return s
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_study_end_to_end_keeps_originals_and_the_application_database(client, world):
    before = _table_counts(world["app"])
    ids = sorted(s["id"] for s in client.get("/api/monthly/strategies").json()["strategies"])
    ev = client.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1,
                                                       "strategies": [{"id": i, "version": 1} for i in ids]}).json()
    client.post(f"/api/monthly/evaluations/{ev['id']}/research")
    _wait(client, f"/api/monthly/evaluations/{ev['id']}", ("NOT STARTED", "FAILED"))
    client.post(f"/api/monthly/evaluations/{ev['id']}/freeze")
    created = client.post("/api/monthly/robustness-studies", json={"baseline_evaluation": ev["id"]})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    s = _wait(client, f"/api/monthly/robustness-studies/{rid}", ("COMPLETED", "FAILED"))
    assert s["status"] == "COMPLETED", s["error"]
    a = s["analysis"]
    assert set(a["strategies"]) == {"A", "R6"} and a["development"] == ["W1", "W2"] and a["hold_out"] == ["W3", "W4"]
    assert [f["test"] for f in a["folds"]] == ["W3", "W4"] and a["folds"][0]["select_on"] == ["W1", "W2"]
    for st in a["strategies"].values():
        assert st["weakness"]["findings"]
        assert all(f["selected_on"] == ["W1", "W2"] for f in st["walk_forward"])          # never a hold-out window
        assert st["verdict"]["verdict"] in ("consistent", "positive but inconsistent", "fails", "insufficient sample",
                                            "no version")
    assert a["exit_experiments"]["A"]["rows"] == []                      # exit experiments live in the diagnostic stage
    roles = {c["role"] for c in client.get(f"/api/monthly/robustness-studies/{rid}/configs").json()["configs"]}
    assert roles == {"benchmark", "redesign"}
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(client.get(f"/api/monthly/robustness-studies/{rid}/results.csv").text)))[2:]
    m15 = {r[0] for r in rows if r[4] == "M15"}
    chosen = {st["selection"]["chosen"] for st in a["strategies"].values()} |         {f["chosen"] for st in a["strategies"].values() for f in st["walk_forward"]}
    assert m15 <= {f"{k}|original" for k in a["strategies"]} | chosen                 # M15 / M1: originals + choices
    assert {f"{k}|original" for k in a["strategies"]} <= m15
    # M1 has no data in the research store here: INCOMPLETE with the reason, never invented
    csv_text = client.get(f"/api/monthly/robustness-studies/{rid}/results.csv").text
    assert "INCOMPLETE" in csv_text and "SIMULATED" in csv_text
    pdf = client.get(f"/api/monthly/robustness-studies/{rid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    versions = client.get("/api/monthly/research-versions").json()["versions"]
    names = {v["strategy"]: v["name"] for v in versions}
    assert names.get("A") == "Example A v3 — trend continuation" or "A" not in names
    cfgs = client.get(f"/api/monthly/robustness-studies/{rid}/configs").json()["configs"]
    bench = [c for c in cfgs if c["role"] == "benchmark" and c["strategy"] == "A"][0]
    app = sqlite3.connect(f"file:{world['app']}?mode=ro", uri=True)
    stored = json.loads(app.execute("SELECT config FROM strategy_versions WHERE strategy_id = 1 AND version = 1").fetchone()[0])
    app.close()
    assert bench["config"] == stored                                         # the original is the benchmark, unchanged
    assert _table_counts(world["app"]) == before                              # nothing written to the application database


# ------------------------------------------------------------------ break-even and trailing stops (engine)
def _managed(world, sources, exit_extra):
    import pandas as pd

    from goldscalp.core.timeframes import Timeframe
    from goldscalp.monthly import runner
    from tests.test_phase5_monthly import SESSION_LONG, _prepared, _spec
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC)
    spec, _ = _spec(world)
    plain = builder.validate(SESSION_LONG)
    managed = builder.validate({**SESSION_LONG, "exit": {**SESSION_LONG["exit"], **exit_extra}})
    a = runner.make_assumptions({"configured_spread": "0.30", "commission": "3.50", "slippage": "0.05"})
    prep = _prepared(sources, Timeframe.M5, start, end)
    bar = pd.Timedelta(minutes=5)
    trades = runner.run_one(plain, prep, Timeframe.M5, start, end, a, spec)["trades"]
    n, t0 = next((i, t) for i, t in enumerate(trades) if pd.Timestamp(t["exit_time"]) > pd.Timestamp(t["entry_time"]) + 2 * bar)
    e, risk = t0["entry_price"], t0["entry_price"] - t0["stop"]
    ex = prep["execution"].copy()
    k = pd.Timestamp(t0["entry_time"]) + bar
    o = float(ex.loc[pd.Timestamp(t0["entry_time"]), "open"])                   # the entry bar stays below +0.25R
    ex.loc[pd.Timestamp(t0["entry_time"]), ["high", "low", "close"]] = [o + 0.2 * risk, o - 0.2 * risk, o]
    return dict(plain=plain, managed=managed, a=a, prep=prep, spec=spec, start=start, end=end, n=n, t0=t0, e=e, risk=risk,
                ex=ex, k=k, bar=bar)


def _run(d, cfg, ex):
    from goldscalp.core.timeframes import Timeframe
    from goldscalp.monthly import runner
    return runner.run_one(cfg, {**d["prep"], "execution": ex}, Timeframe.M5, d["start"], d["end"], d["a"], d["spec"])["trades"]


def test_breakeven_moves_the_stop_to_entry_only_after_the_bar_that_reached_1r(world, sources):
    d = _managed(world, sources, {"breakeven": {"enabled": True, "trigger_r": 1.0, "lock_r": 0.0}})
    e, risk, ex, k, bar = d["e"], d["risk"], d["ex"], d["k"], d["bar"]
    # bar k: +1.2R high but ALSO a low below entry (above the stop): the same bar must not stop out at break-even
    ex.loc[k, ["open", "high", "low", "close"]] = [e + 0.1, e + 1.2 * risk, e - 0.3 * risk, e + 0.5 * risk]
    # bar k+1: trades back through the entry -> break-even stop at the entry price
    ex.loc[k + bar, ["open", "high", "low", "close"]] = [e + 0.4 * risk, e + 0.45 * risk, e - 0.6 * risk, e - 0.5 * risk]
    t = _run(d, d["managed"], ex)[d["n"]]
    assert t["entry_time"] == d["t0"]["entry_time"]
    assert t["exit_reason"] == "breakeven" and t["breakeven_moved"] and t["exit_time"] == (k + 2 * bar).isoformat().replace("+00:00", "Z")
    assert t["stop"] == pytest.approx(d["t0"]["stop"], abs=1e-3)                  # the planned (initial) stop is kept
    assert t["stop_final"] == pytest.approx(e, abs=1e-3)                            # moved to the entry price
    assert -1.0 < t["net_pnl"] < 0 and t["r_multiple"] > -0.1                       # only costs lost, not 1R
    plain = _run(d, d["plain"], ex)[d["n"]]
    assert plain["exit_reason"] != "breakeven" and "breakeven_moved" not in plain   # off by default, trades unchanged


def test_trailing_stop_locks_profit_and_only_tightens(world, sources):
    d = _managed(world, sources, {"trail": {"enabled": True, "activate_r": 1.0, "mode": "r", "distance": 1.0},
                                  "target": {"type": "none"}})
    e, risk, ex, k, bar = d["e"], d["risk"], d["ex"], d["k"], d["bar"]
    ex.loc[k, ["open", "high", "low", "close"]] = [e + 0.1, e + 1.6 * risk, e + 0.05, e + 1.4 * risk]   # best +1.6R
    ex.loc[k + bar, ["open", "high", "low", "close"]] = [e + 1.3 * risk, e + 1.35 * risk, e + 0.4 * risk, e + 0.5 * risk]
    t = _run(d, d["managed"], ex)[d["n"]]
    assert t["exit_reason"] == "trailing_stop" and t["trail_moved"]
    assert t["stop_final"] == pytest.approx(e + 0.6 * risk, abs=0.02)               # best (+1.6R) - 1R distance
    assert t["r_multiple"] == pytest.approx(0.6, abs=0.1)                           # profit locked, minus costs


def test_stop_management_is_omitted_when_off_so_fingerprints_do_not_change():
    for tpl in builder.templates():
        v = builder.validate(tpl)
        assert "breakeven" not in v["exit"] and "trail" not in v["exit"]
        assert builder.fingerprint(builder.validate(v)) == builder.fingerprint(v)
        off = builder.validate({**v, "exit": {**v["exit"], "breakeven": {"enabled": False}, "trail": {"enabled": False}}})
        assert builder.fingerprint(off) == builder.fingerprint(v)
    with pytest.raises(builder.StrategyError):
        builder.validate({**builder.templates()[0], "exit": {"breakeven": {"enabled": True, "trigger_r": 1.0, "lock_r": 1.0}}})
    with pytest.raises(builder.StrategyError):
        builder.validate({**builder.templates()[0], "exit": {"trail": {"enabled": True, "mode": "pips"}}})


def test_diagnostic_stage_uses_development_months_only_and_no_redesign(client, world):
    ids = sorted(s["id"] for s in client.get("/api/monthly/strategies").json()["strategies"])
    ev = client.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1,
                                                       "strategies": [{"id": i, "version": 1} for i in ids]}).json()
    client.post(f"/api/monthly/evaluations/{ev['id']}/research")
    _wait(client, f"/api/monthly/evaluations/{ev['id']}", ("NOT STARTED", "FAILED"))
    client.post(f"/api/monthly/evaluations/{ev['id']}/freeze")
    rid = client.post("/api/monthly/robustness-studies", json={"baseline_evaluation": ev["id"], "stage": "diagnostic"}).json()["id"]
    s = _wait(client, f"/api/monthly/robustness-studies/{rid}", ("COMPLETED", "FAILED"))
    assert s["status"] == "COMPLETED", s["error"]
    a = s["analysis"]
    assert a["stage"] == "diagnostic" and a["development"] == ["W1", "W2"] and a["hold_out"] == [] and a["folds"] == []
    assert {w["role"] for w in s["params"]["windows"]} == {"development"}                 # no hold-out month touched
    assert {r["experiment"] for r in a["exit_experiments"]["A"]["rows"]} >= {"E9", "E10", "E11"}
    assert "mfe_reached" in a["strategies"]["A"]["weakness"] or a["strategies"]["A"]["weakness"]["trades"] == 0
    roles = {c["role"] for c in client.get(f"/api/monthly/robustness-studies/{rid}/configs").json()["configs"]}
    assert roles == {"benchmark", "exit_experiment"}                                     # no redesign before the study
    assert client.get(f"/api/monthly/robustness-studies/{rid}/report.pdf").content.startswith(b"%PDF")
    assert client.get("/api/monthly/research-versions").json()["versions"] == []
