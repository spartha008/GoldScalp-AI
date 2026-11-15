"""Phase 5 monthly multi-timeframe research and backtesting (historical, simulation-only).

Data: SYNTHETIC candles in a TEMPORARY application database (never the real one). The month and the research
window are shortened in the end-to-end test (monkeypatched calendar) so the full workflow runs in seconds; the
logic is the same as for a calendar month."""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import warnings
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.backtesting import engine
from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample
from goldscalp.monthly import candidates as cand
from goldscalp.monthly import data as mdata
from goldscalp.monthly import report, runner, service
from goldscalp.monthly import store as st
from goldscalp.monthly.standalone import create_research_app
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.storage import candle_repo, strategy_repo
from goldscalp.storage.db import Database
from goldscalp.strategies import builder
from tests.conftest import SPEC_PATH, synthetic_m1

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SESSION_LONG = {"name": "session long", "direction": "long", "evaluation": {"step_timeframe": "M5"},
                "conditions": [{"id": "s", "type": "session", "sessions": ["Asian", "London", "New York"]}],
                "entry": {"trigger": "every_step"},
                "exit": {"stop": {"type": "fixed", "distance": 3}, "target": {"type": "r_multiple", "r": 2.0},
                         "max_holding_minutes": 30}, "risk": {"lots": "0.02"}}
VERIFIED = {"contract_size": 100.0, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "digits": 2,
            "point": 0.01, "name": "XAUUSD", "description": "Gold US Dollar"}


def _build_app_db(path: Path, spec: InstrumentSpec) -> None:
    db = Database(path)
    db.migrate()
    m1 = synthetic_m1("2026-09-24T00:00:00Z", "2026-10-03T00:00:00Z", spec.schedule, seed=5, price=3800.0)
    info = candle_repo.SeriesInfo(key="live:mt5:XAUUSD", symbol="XAUUSD", kind="live", source_id="mt5",
                                  source_symbol="XAUUSD", candle_side="bid", price_decimals=2, description="synthetic test")
    with db.transaction() as con:
        sid = candle_repo.ensure_series(con, info, NOW)
        candle_repo.upsert_candles(con, sid, 2, Timeframe.M1, m1, "provider", None, NOW)
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
            candle_repo.upsert_candles(con, sid, 2, tf, resample(m1, Timeframe.M1, tf), "provider", None, NOW)
        for t in builder.templates():
            strategy_repo.create(con, builder.validate(t), NOW)
        con.execute("INSERT INTO mt5_verifications (id, started_at, finished_at, overall, adapter, checks, summary) "
                    "VALUES ('mv_test', '2026-11-14T11:52:37Z', '2026-11-14T11:53:00Z', 'PARTIAL', 'MetaTrader5', ?, '{}')",
                    (json.dumps([{"key": "symbol", "data": {"spec": VERIFIED}}]),))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    base = tmp_path_factory.mktemp("p5")
    spec = InstrumentSpec.load(SPEC_PATH)
    _build_app_db(base / "app.sqlite3", spec)
    return {"app": base / "app.sqlite3", "base": base, "spec": spec}


@pytest.fixture
def sources(world, tmp_path):
    return mdata.DataSources(st.ReadOnlySource(world["app"]), st.open_store(tmp_path / "monthly.sqlite3"))


def _row(src):
    return src.series_row("app", "live:mt5:XAUUSD")


# ------------------------------------------------------------------ calendar and availability
def test_month_ranges_and_default_month_never_substitute(world, sources):
    assert mdata.month_range("2026-10") == (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert mdata.month_range("2026-02")[1] == datetime(2026, 3, 1, tzinfo=UTC)
    assert mdata.latest_completed_month(NOW) == "2026-10" and mdata.previous_month("2026-01") == "2025-12"
    with pytest.raises(ValueError):
        mdata.month_range("October")
    d = mdata.default_month(sources, "app", "live:mt5:XAUUSD", world["spec"].schedule, NOW, research_days=5,
                            lookback_days=5, search=3)
    assert d["month"] is None                                   # synthetic data covers only 2 October days
    assert [s["month"] for s in d["skipped"]] == ["2026-10", "2026-09", "2026-08"]
    assert "cover" in " ".join(d["skipped"][0]["missing"])        # every skipped month says why


def test_availability_reports_per_timeframe_coverage_and_missing_data(world, sources, monkeypatch):
    monkeypatch.setattr(mdata, "month_range", lambda m: (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)))
    av = mdata.availability(sources, "app", "live:mt5:XAUUSD", "2026-10", world["spec"].schedule, research_days=3,
                            lookback_days=5)
    assert set(av["timeframes"]) == {"M15", "M5", "M1"} and av["ready"] == ["M15", "M5", "M1"]
    assert av["timeframes"]["M1"]["analysis_base"] == "M1" and av["timeframes"]["M15"]["analysis_base"] == "M5"
    assert av["timeframes"]["M5"]["execution"]["share"] >= 0.95
    # a month without data: explained, not substituted
    monkeypatch.setattr(mdata, "month_range", lambda m: (datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC)))
    av = mdata.availability(sources, "app", "live:mt5:XAUUSD", "2026-10", world["spec"].schedule, research_days=3,
                            lookback_days=5)
    assert av["ready"] == [] and "cover 0%" in av["timeframes"]["M5"]["problems"][0]


# ------------------------------------------------------------------ candidates, readiness, RR
def test_research_candidates_are_valid_documented_and_in_the_rr_range():
    assert len(cand.CONCEPTS) == 9 and len({c['key'] for c in cand.CONCEPTS}) == 9
    for c in cand.CONCEPTS:
        vs = cand.variants(c)
        assert [v["rr"] for v in vs] == [1.5, 2.0]
        for v in vs:
            assert builder.validate(v["config"]) == v["config"] and cand.rr_check(v["config"])["in_range"] is True
        assert set(c["documentation"]) >= {"entry", "exit", "stop_loss", "take_profit", "investigates", "limitations", "data"}
    assert cand.rr_check({"exit": {"target": {"type": "r_multiple", "r": 3.0}}})["in_range"] is False
    assert cand.rr_check({"exit": {"target": {"type": "atr", "r": 2.0}}})["in_range"] is None


def test_readiness_uses_configured_risk_and_never_invents_one():
    cfg = builder.validate(SESSION_LONG)
    r = runner.readiness(cfg, {})
    assert r["ready"] and r["sizing"]["mode"].startswith("fixed_lots") and r["sizing"]["lots"] == "0.02"
    assert r["rr"]["planned_rr"] == 2.0 and r["rr"]["in_range"]
    o = runner.readiness(cfg, {"risk_percent_override": 1.0})
    assert "USER OVERRIDE" in o["sizing"]["mode"]
    bad = {**cfg, "risk": {**cfg["risk"], "sizing": "martingale"}}
    assert runner.readiness(bad, {})["ready"] is False


def test_new_indicator_conditions_are_evaluated_from_closed_candles():
    snap = {"cutoff": "2026-10-01T10:00:00Z", "price": 3810.0, "spread": 0.2, "sessions": [], "signal": None,
            "tf": {"M15": {"ind": {"ema": {"20": 3805.0, "50": 3800.0}, "rsi14": 62.0, "close": 3810.0}},
                   "M5": {"ind": {"ema": {}, "rsi14": 62.0, "close": 3810.0}}}}
    from goldscalp.strategies import conditions
    ema = {"id": "e", "type": "ema_trend", "timeframe": "M15", "fast": "20", "slow": "50", "mode": "both"}
    mom = {"id": "m", "type": "momentum", "timeframe": "M5", "level": 60, "mode": "confirm"}
    assert conditions._check(ema, snap, "long")[0] and not conditions._check(ema, snap, "short")[0]
    assert conditions._check(mom, snap, "long")[0] and not conditions._check(mom, snap, "short")[0]
    with pytest.raises(builder.StrategyError, match="shorter than slow"):
        builder.validate({**SESSION_LONG, "conditions": [{**ema, "fast": "50", "slow": "50"}]})


# ------------------------------------------------------------------ runs: sizing, SL/TP, margin, costs, gaps
def _prepared(sources, tf, start, end):
    return runner.prepare(sources, "app", _row(sources), tf, start, end, lookback_days=5, detail=set(),
                          cfg=AnalysisConfig.load())


def _spec(world):
    return runner.contract_spec(world["spec"], {"record": "mv_test", "verified_at": "x", **VERIFIED})


def test_independent_timeframe_runs_with_sizing_sl_tp_costs_and_margin(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 11, tzinfo=UTC)
    spec, contract = _spec(world)
    a = runner.make_assumptions({"configured_spread": "0.30", "commission": "3.50", "slippage": "0.05"})
    cfg = builder.validate(SESSION_LONG)
    results = {}
    for tf in (Timeframe.M15, Timeframe.M5, Timeframe.M1):
        prep = _prepared(sources, tf, start, end)
        assert prep["dataset"]["run_timeframe"] == tf.value and prep["dataset"]["execution_origin"] == "native"
        r = runner.run_one(cfg, prep, tf, start, end, a, spec)
        results[tf.value] = report.build_run(r, starting_balance=1000.0, leverage=500, contract=contract)
        own = sources.load("app", _row(sources), tf, start, end)
        assert prep["dataset"]["execution_candles"] == len(own)                       # this timeframe's candles only
        for t in r["trades"]:                                                         # fills on its own bar grid
            assert pd.Timestamp(t["entry_time"]).minute % tf.minutes == 0
            assert pd.Timestamp(t["exit_time"]).minute % tf.minutes == 0, (tf, t["exit_time"])
    trades = results["M5"]["trades"]
    assert trades and len({len(v["trades"]) for v in results.values()}) >= 1
    t = trades[0]
    ounces = Decimal(t["ounces"])
    assert t["lots"] == "0.02" and ounces == Decimal("2")                            # configured fixed lots
    assert t["planned_rr"] == pytest.approx(2.0, abs=1e-3)                           # TP at 2 R
    assert t["risk_usd"] == pytest.approx(abs(t["entry_price"] - t["stop"]) * 2, abs=0.01)
    assert t["commission"] == pytest.approx(0.14) and t["slippage_cost"] == pytest.approx(0.05 * 2 * 2)
    assert t["margin"] == pytest.approx(2 * t["entry_price"] / 500, abs=0.01)         # 1:500, margin only
    p = results["M5"]["performance"]
    assert p["starting_balance"] == 1000.0 and p["ending_balance"] == pytest.approx(1000 + p["net"], abs=0.01)
    assert results["M5"]["risk"]["planned_rr_average"] == pytest.approx(2.0, abs=1e-3)
    assert results["M5"]["costs"]["total"] == pytest.approx(sum(x["commission"] + x["spread_cost"] + x["slippage_cost"]
                                                                for x in trades), abs=0.05)


def test_insufficient_margin_is_rejected_and_recorded(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 9, tzinfo=UTC)
    spec, contract = _spec(world)
    a = engine.Assumptions.from_mapping({**runner.make_assumptions({"configured_spread": "0.3", "commission": "3.5",
                                                                     "slippage": "0.05"}).to_mapping(), "leverage": 1})
    prep = _prepared(sources, Timeframe.M5, start, end)
    r = runner.run_one(builder.validate(SESSION_LONG), prep, Timeframe.M5, start, end, a, spec)
    built = report.build_run(r, starting_balance=1000.0, leverage=1, contract=contract)
    assert r["trades"] == [] and built["risk"]["margin_rejections"] > 0       # 2 oz x ~3800 = 7,600 USD > 1,000
    rej = built["rejections"][0]
    assert rej["reason"] == "insufficient_margin" and rej["margin_required"] > rej["free_margin"]


def test_risk_percent_override_sizes_from_the_stop_distance(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 9, tzinfo=UTC)
    spec, _ = _spec(world)
    a = runner.make_assumptions({"configured_spread": "0.3", "commission": "3.5", "slippage": "0.05",
                                 "risk_percent_override": 1.0})
    prep = _prepared(sources, Timeframe.M5, start, end)
    r = runner.run_one(builder.validate(SESSION_LONG), prep, Timeframe.M5, start, end, a, spec)
    t = r["trades"][0]
    risk_oz = abs(Decimal(str(t["entry_price"])) - Decimal(str(t["stop"])))
    expected = (Decimal("10") / (risk_oz * 100) / Decimal("0.01")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.01")
    assert Decimal(t["lots"]) == expected                                       # 1% of $1,000 at the stop distance


def test_gaps_reduce_coverage_and_stop_target_collisions_are_conservative(world, sources, monkeypatch):
    frame = sources.load("app", _row(sources), Timeframe.M5, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    gappy = frame.drop(frame.index[20:60])
    cov = mdata.coverage(gappy, Timeframe.M5, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC),
                         world["spec"].schedule)
    assert cov["share"] < 0.95 and cov["missing_ranges"]
    assert engine.Assumptions.from_mapping({"configured_spread": "0.3", "commission_per_lot_per_side": "3.5",
                                            "slippage": "0.05"}).intrabar == "stop_first"   # collisions: stop first


def test_drawdown_equity_and_interpretation_from_a_known_result():
    trades = [{"id": i, "side": "buy", "setup_cutoff": f"2026-10-01T0{i}:00:00Z", "entry_time": f"2026-10-01T0{i}:05:00Z",
               "entry_price": 3800.0, "lots": "0.02", "ounces": "2", "stop": 3795.0, "stop_source": "fixed",
               "target": 3810.0, "exit_time": f"2026-10-01T0{i}:30:00Z", "exit_price": 0.0, "exit_reason": ex,
               "ambiguous_bar": False, "gross_pnl": g, "commission": 0.14, "spread_cost": 0.6, "slippage_cost": 0.2,
               "net_pnl": round(g - 0.14, 2), "r_multiple": round((g - 0.14) / 10, 3), "mfe": 1.0, "mae": 1.0,
               "holding_minutes": 25.0, "session": "London", "volatility": "normal"}
              for i, (g, ex) in enumerate([(20.0, "target"), (-10.0, "stop"), (-10.0, "stop"), (20.0, "target")], 1)]
    marked = [{"time": f"2026-10-01T0{i}:30:00Z", "equity": e, "balance": e, "used_margin": 0.0}
              for i, e in enumerate([1019.86, 1009.72, 999.58, 1019.44], 1)]
    res = report.build_run({"trades": trades, "closed_equity": marked, "marked_equity": marked,
                            "diagnostics": {"rejections": [], "skipped": {}}, "result_hash": "h"},
                           starting_balance=1000.0, leverage=500, contract={})
    p = res["performance"]
    assert p["net"] == pytest.approx(19.44) and p["return_pct"] == pytest.approx(1.944)
    assert p["max_drawdown"] == pytest.approx(20.28) and p["largest_loss"] == pytest.approx(-10.14)
    assert p["small_sample"] and res["risk"]["stop_loss_exits"] == 2 and res["risk"]["take_profit_exits"] == 2
    assert min(x["value"] for x in res["charts"]["drawdown"]) == pytest.approx(-20.28)
    assert any("one historical month" in line for line in res["interpretation"])
    assert res["trades"][1]["balance_before"] == pytest.approx(1019.86)


# ------------------------------------------------------------------ research: before the month only
def test_research_uses_only_data_before_the_month(world, sources, tmp_path):
    month_start = datetime(2026, 10, 2, tzinfo=UTC)
    spec, _ = _spec(world)
    a = runner.make_assumptions({"configured_spread": "0.3", "commission": "3.5", "slippage": "0.05"})
    kw = {"month_start": month_start, "window_start": datetime(2026, 10, 1, tzinfo=UTC), "lookback_days": 5,
          "cfg": AnalysisConfig.load(), "assumptions": a, "spec": spec}
    first = runner.research(sources, "app", _row(sources), **kw)
    assert first["window"]["end"] == "2026-10-02T00:00:00Z" and len(first["candidates"]) == 9
    assert pd.Timestamp(first["dataset"]["analysis_last"]) < pd.Timestamp(month_start)
    for c in first["candidates"]:
        assert c["research"]["chosen_rr"] in (1.5, 2.0) and c["config"]["exit"]["target"]["r"] == c["research"]["chosen_rr"]
    # rewrite every candle INSIDE the month: research results must not change
    copy = tmp_path / "app_copy.sqlite3"
    sqlite3.connect(world["app"]).backup(sqlite3.connect(copy))
    con = sqlite3.connect(copy)
    con.execute("UPDATE candles SET o = o * 2, h = h * 2, l = l * 2, c = c * 2 WHERE ts >= ?", (int(month_start.timestamp()),))
    con.commit()
    con.close()
    other = mdata.DataSources(st.ReadOnlySource(copy), sources.research)
    second = runner.research(other, "app", other.series_row("app", "live:mt5:XAUUSD"), **kw)
    assert [c["research"] for c in second["candidates"]] == [c["research"] for c in first["candidates"]]
    with pytest.raises(runner.MonthlyError):
        runner.research(sources, "app", _row(sources), **{**kw, "window_start": month_start})


# ------------------------------------------------------------------ end to end: nine runs, freezing, reports
@pytest.fixture
def client(world, tmp_path, monkeypatch):
    short = (datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC))
    monkeypatch.setattr(mdata, "month_range", lambda m: short)
    monkeypatch.setattr(service, "month_range", lambda m: short)
    monkeypatch.setattr(mdata, "MIN_RESEARCH_DAYS", 1)
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def _wait(c, eid, done=("NOT STARTED", "READY", "COMPLETED", "INCOMPLETE", "FAILED"), seconds=600):
    import time
    for _ in range(seconds * 10):
        ev = c.get(f"/api/monthly/evaluations/{eid}").json()
        if not ev["running_job"] and ev["status"] in done:
            return ev
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def _table_counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    out = {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    out["_schema"] = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    con.close()
    return out


def test_full_workflow_nine_independent_runs_freezing_reports_and_reproducibility(client, world):
    before = _table_counts(world["app"])
    ids = [s["id"] for s in client.get("/api/monthly/strategies").json()["strategies"]]
    refs = [{"id": i, "version": 1} for i in sorted(ids)]
    created = client.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1, "strategies": refs})
    assert created.status_code == 201, created.text
    ev = created.json()
    assert ev["status"] == "NOT STARTED" and ev["availability"]["ready"] == ["M15", "M5", "M1"]
    assert client.post(f"/api/monthly/evaluations/{ev['id']}/freeze").status_code == 409        # research first
    assert client.post(f"/api/monthly/evaluations/{ev['id']}/research").status_code == 202
    ev = _wait(client, ev["id"])
    assert ev["status"] == "NOT STARTED" and ev["research_done"] and len(ev["candidates"]) == 9
    assert ev["research"]["window"]["end"] <= ev["range_start"]                                # before the month
    frozen = client.post(f"/api/monthly/evaluations/{ev['id']}/freeze").json()
    assert frozen["status"] == "READY" and [s["key"] for s in frozen["frozen"]["strategies"]] == ["A", "B", "C"]
    assert all(s["config"] == strategy_repo_get(world["app"], s["strategy_id"], s["version"])["config"]
               for s in frozen["frozen"]["strategies"])                                       # exact versions
    assert client.post(f"/api/monthly/evaluations/{ev['id']}/run").status_code == 202
    ev = _wait(client, ev["id"])
    existing = [r for r in ev["runs"] if r["kind"] == "existing"]
    assert ev["status"] == "COMPLETED" and len(existing) == 9 and len({r["id"] for r in existing}) == 9
    assert {(r["strategy_key"], r["timeframe"]) for r in existing} == {(k, tf) for k in "ABC" for tf in ("M15", "M5", "M1")}
    assert len([r for r in ev["runs"] if r["kind"] == "candidate"]) == 27
    full = client.get(f"/api/monthly/runs/{existing[0]['id']}").json()["run"]
    assert full["result"]["performance"]["starting_balance"] == 1000.0 and full["assumptions"]["engine"]["leverage"] == 500
    assert full["dataset"]["execution_origin"] == "native" and full["dataset"]["series"] == "live:mt5:XAUUSD"
    # reports and exports
    rid = existing[0]["id"]
    for path, kind in ((f"/api/monthly/runs/{rid}/report.pdf", b"%PDF"),
                       (f"/api/monthly/evaluations/{ev['id']}/strategies/A/report.pdf", b"%PDF"),
                       (f"/api/monthly/evaluations/{ev['id']}/report.pdf", b"%PDF")):
        r = client.get(path)
        assert r.status_code == 200 and r.content.startswith(kind), path
    ledger = client.get(f"/api/monthly/runs/{rid}/ledger.csv").text
    assert "SIMULATED" in ledger and "risk_usd" in ledger and "planned_rr" in ledger
    summary = client.get(f"/api/monthly/evaluations/{ev['id']}/summary.csv").text
    assert summary.count("\n") == 2 + 9 and "separate hypothetical accounts" in summary
    js = client.get(f"/api/monthly/runs/{rid}/run.json").json()
    assert js["strategy_config"] == full["config"] and js["app_version"].startswith("goldscalp")
    comb = client.get(f"/api/monthly/evaluations/{ev['id']}/combined").json()
    assert comb["completed_runs"] == 9 and "not one account" in comb["separate_accounts_note"].lower() or \
        "separate" in comb["separate_accounts_note"]
    assert "One month of results cannot establish" in comb["conclusion"]
    comp = client.get(f"/api/monthly/evaluations/{ev['id']}/strategies/B").json()["comparison"]
    assert [r["timeframe"] for r in comp["rows"]] == ["M15", "M5", "M1"]
    rep = client.post(f"/api/monthly/runs/{rid}/reproduce").json()
    assert rep["identical"] and rep["same_input_data"]
    # nothing was written to the application database
    assert _table_counts(world["app"]) == before


def strategy_repo_get(path, sid, version):
    with st.ReadOnlySource(path).read() as con:
        return strategy_repo.get(con, sid, version)


def test_freeze_refuses_a_tampered_version_and_missing_month_data_is_incomplete(client, world, monkeypatch):
    ev = client.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1,
                                                        "strategies": [{"id": 1, "version": 1}]}).json()
    client.post(f"/api/monthly/evaluations/{ev['id']}/research")
    _wait(client, ev["id"])
    real = mdata.DataSources.strategy

    def tampered(self, sid, version=None):
        rec = real(self, sid, version)
        return {**rec, "config": {**rec["config"], "direction": "short"}}
    monkeypatch.setattr(mdata.DataSources, "strategy", tampered)
    refused = client.post(f"/api/monthly/evaluations/{ev['id']}/freeze")
    assert refused.status_code == 409 and "fingerprint" in refused.json()["detail"]
    monkeypatch.setattr(mdata.DataSources, "strategy", real)
    assert client.post(f"/api/monthly/evaluations/{ev['id']}/freeze").json()["status"] == "READY"
    # M1 data disappears before the run: the M1 runs are INCOMPLETE with the reason, never substituted
    real_av = mdata.availability

    def no_m1(*a, **k):
        av = real_av(*a, **k)
        av["timeframes"]["M1"].update(sufficient=False, problems=["M1 execution candles cover 0% of the month"])
        return av
    monkeypatch.setattr(service, "availability", no_m1)
    client.post(f"/api/monthly/evaluations/{ev['id']}/run")
    done = _wait(client, ev["id"])
    m1 = [r for r in done["runs"] if r["timeframe"] == "M1"]
    assert done["status"] == "INCOMPLETE" and m1 and all(r["status"] == "INCOMPLETE" and "M1" in r["reason"] for r in m1)


def test_completed_months_only_and_security_headers(world, tmp_path):
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "m.sqlite3")
    client = TestClient(app, headers={"X-GoldScalp-Request": "1"})
    r = client.post("/api/monthly/evaluations", json={"month": "2026-11"})                 # 14 Nov: not completed
    assert r.status_code == 422 and "not a completed calendar month" in r.json()["detail"]
    assert client.post("/api/monthly/evaluations", json={"month": "2026-10"},
                       headers={"X-GoldScalp-Request": "0"}).status_code == 403          # CSRF
    page = client.get("/monthly-backtest")
    assert page.status_code == 200 and "Monthly Backtest" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]


# ------------------------------------------------------------------ safety
def test_monthly_package_cannot_trade_or_write_to_the_application_database():
    from goldscalp.api import app as main_app
    from goldscalp.monthly import standalone
    assert standalone.CSP == main_app.CSP and standalone.CSRF_HEADER == main_app.CSRF_HEADER
    assert not hasattr(st.ReadOnlySource, "transaction") and not hasattr(st.ReadOnlySource, "migrate")
    forbidden = ("goldscalp.simulation.portfolio", "goldscalp.storage.paper_repo", "goldscalp.storage.live_repo",
                 "goldscalp.live", "goldscalp.data.providers.mt5", "goldscalp.data.hub", "goldscalp.data.live",
                 "goldscalp.api.app", "MetaTrader5", "httpx")
    for path in (ROOT / "goldscalp" / "monthly").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in mods if m.startswith(forbidden)}, path.name
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.(open_position|close_position)\(", text) and "strategy_repo.save" not in text, path.name
        assert "strategy_repo.create" not in text and "order_send" not in text, path.name


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows process priority")
def test_research_service_lowers_its_own_priority():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", "from goldscalp.monthly.standalone import lower_priority; print(lower_priority())"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True", out.stderr



def test_level_break_condition_uses_only_confirmed_acceptance_events():
    from goldscalp.strategies import conditions
    cut = "2026-10-01T13:30:00Z"
    snap = {"cutoff": cut, "price": 3810.0, "spread": 0.2, "sessions": ["London", "New York"], "signal": None,
            "tf": {"M5": {"detail": True, "sweeps": [
                {"type": "sweep", "direction": "bullish", "level_type": "PDL", "price": 3790.0, "known_at": "2026-10-01T13:20:00Z"},
                {"type": "break", "direction": "bullish", "level_type": "PDH", "price": 3805.0, "known_at": "2026-10-01T13:25:00Z"},
                {"type": "break", "direction": "bearish", "level_type": "session_low", "price": 3780.0,
                 "known_at": "2026-10-01T10:00:00Z"}]}}}
    c = {"id": "b", "type": "level_break", "timeframe": "M5", "within_candles": 3, "level_types": ["PD", "session"]}
    ok, why = conditions._check(c, snap, "long")
    assert ok and "confirmed bullish break of PDH 3805.0" in why
    assert not conditions._check(c, snap, "short")[0]                         # the bearish break is too old
    assert not conditions._check({**c, "level_types": ["EQ"]}, snap, "long")[0]
    assert "level_break" in builder.DETAIL_TYPES


def test_mt5_export_with_spread_points_imports_into_the_research_store_only(world, tmp_path):
    store = tmp_path / "m.sqlite3"
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=store)
    client = TestClient(app, headers={"X-GoldScalp-Request": "1"})
    lines = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    for i in range(30):                                     # Thursday 1 Oct 2026, 10:00 server time = 07:00 UTC
        px = 3850 + i * 0.1
        lines.append(f"2026.10.01\t10:{i:02d}:00\t{px:.2f}\t{px + 0.5:.2f}\t{px - 0.5:.2f}\t{px + 0.1:.2f}\t300\t0\t26")
    body = ("\r\n".join(lines) + "\r\n").encode()
    form = {"dialect": "mt5_export", "timeframe": "M1", "time_basis": "rule:ny_close", "series_label": "vantage-test"}
    refused = client.post("/api/monthly/import", data=form, files={"file": ("XAUUSD_M1.csv", body, "text/csv")})
    assert refused.status_code == 422 and "point size" in refused.json()["detail"]      # never assumed
    before = {t: c for t, c in _app_counts(world["app"]).items()}
    ok = client.post("/api/monthly/import", data={**form, "point": "0.01"},
                     files={"file": ("XAUUSD_M1.csv", body, "text/csv")})
    assert ok.status_code == 200, ok.text
    res = ok.json()
    assert res["store"] == "research" and res["series_key"] == "csv:vantage-test" and res["rows_stored"] == 30
    assert res["first"] == "2026-10-01T07:00:00Z"                                        # server time -> UTC
    con = sqlite3.connect(store)
    spreads = {r[0] for r in con.execute("SELECT spread FROM candles WHERE tf = 'M1'")}
    con.close()
    assert spreads == {26}                                  # 26 points x 0.01 = 0.26 USD, stored at 2 decimals
    assert _app_counts(world["app"]) == before              # the application database is untouched


def _app_counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


class _ScheduledM1Source:
    """A stub source with on-schedule M1 bars from ``m1_from`` to the end of October and nothing else."""

    def __init__(self, schedule, m1_from):
        self.schedule, self.m1_from = schedule, m1_from

    def series_row(self, store, key):
        return {"id": 1, "key": key, "price_decimals": 2}

    def load(self, store, row, tf, start, end, limit=None):
        if tf != Timeframe.M1:
            return pd.DataFrame({"close": []}, index=pd.DatetimeIndex([], tz=UTC))
        idx = self.schedule.expected_bars(max(start, self.m1_from), end, 60)
        return pd.DataFrame({"close": 1.0}, index=idx)


def test_lookback_history_counts_from_the_first_trading_bar_when_it_starts_on_a_weekend(world):
    """October 2026 starts on a Thursday; 5 days back is Saturday 26 Sep, and the market reopens on Sunday 27 Sep.
    A history that starts at that reopen is everything a run with a 5-day lookback can load, so it is enough; one
    that starts later is not."""
    sched = world["spec"].schedule
    reopen = sched.expected_bars(datetime(2026, 9, 26, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC), 60)[0]
    assert reopen.weekday() == 6                                              # Sunday evening, after the weekend
    def m1(av):
        return av["timeframes"]["M1"]
    at_reopen = mdata.availability(_ScheduledM1Source(sched, reopen.to_pydatetime()), "research", "csv:x", "2026-10",
                                   sched, research_days=60, lookback_days=5)
    assert m1(at_reopen)["sufficient"] and m1(at_reopen)["execution"]["share"] == 1.0
    late = mdata.availability(_ScheduledM1Source(sched, (reopen + pd.Timedelta(hours=1)).to_pydatetime()), "research",
                              "csv:x", "2026-10", sched, research_days=60, lookback_days=5)
    assert not m1(late)["sufficient"] and "5 days are needed" in m1(late)["problems"][0]
    friday = mdata.availability(_ScheduledM1Source(sched, datetime(2026, 9, 25, tzinfo=UTC)), "research", "csv:x",
                                "2026-10", sched, research_days=60, lookback_days=5)
    assert m1(friday)["sufficient"]
    # a lookback that starts on a trading day still needs data from that day
    tue = mdata.availability(_ScheduledM1Source(sched, datetime(2026, 9, 29, tzinfo=UTC)), "research", "csv:x",
                             "2026-10", sched, research_days=60, lookback_days=2)                # 29 Sep is a Tuesday
    assert m1(tue)["sufficient"]
    tue_late = mdata.availability(_ScheduledM1Source(sched, datetime(2026, 9, 29, 6, tzinfo=UTC)), "research", "csv:x",
                                  "2026-10", sched, research_days=60, lookback_days=2)
    assert not m1(tue_late)["sufficient"]
