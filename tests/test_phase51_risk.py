"""Phase 5.1: percentage-risk sizing (5 / 7.5 / 10 % of current simulated equity), risk-scenario studies (27 A/B/C
runs from a fixed-lot baseline that is never modified), the historical CSV import preview / labels / coverage, and
isolation. Simulation only."""
# ruff: noqa: F811 - pytest fixtures (world, sources) are shared from the Phase 5 test module
from __future__ import annotations

import random
import sqlite3
import time
import warnings
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from goldscalp.backtesting import engine
from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import data as mdata
from goldscalp.monthly import importing, report, risk, runner, service
from goldscalp.monthly.standalone import create_research_app
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder
from tests.conftest import SPEC_PATH
from tests.test_phase5_monthly import (  # noqa: F401 - module fixtures shared with Phase 5
    NOW,
    SESSION_LONG,
    VERIFIED,
    _prepared,
    _row,
    _spec,
    _table_counts,
    sources,
    world,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


def _verified_spec() -> InstrumentSpec:
    spec, _ = runner.contract_spec(InstrumentSpec.load(SPEC_PATH), {"record": "mv_test", "verified_at": "x", **VERIFIED})
    return spec


def _size(equity="1000", pct="5", entry="4000.00", stop="3995.00", slip="0.05", comm="3.50", max_lots=None):
    return engine.size_risk_of_equity(equity=Decimal(equity), risk_percent=Decimal(pct), entry_price=Decimal(entry),
                                      stop=None if stop is None else Decimal(stop), slippage=Decimal(slip),
                                      commission_per_lot_per_side=Decimal(comm), spec=_verified_spec(),
                                      max_lots=None if max_lots is None else Decimal(max_lots))


# ------------------------------------------------------------------ the sizing formula
@pytest.mark.parametrize(("pct", "lots", "planned"), [("5", "0.09", "46.08"), ("7.5", "0.14", "71.68"),
                                                      ("10", "0.19", "97.28")])
def test_percentage_risk_sizes_5_75_10_percent_with_costs_and_rounds_down(pct, lots, planned):
    # $5.00 stop: planned loss per lot = (5.00 + 0.05 exit slippage) x 100 oz + 2 x 3.50 commission = 512 USD
    s = _size(pct=pct)
    assert s["per_lot_loss"] == Decimal("512.00") and s["budget"] == Decimal(1000) * Decimal(pct) / 100
    assert s["lots"] == Decimal(lots) and s["planned_risk"] == Decimal(planned)
    assert s["planned_risk"] <= s["budget"] < s["planned_risk"] + Decimal("0.01") * s["per_lot_loss"]   # floor
    assert s["reason"] is None and s["capped_by"] is None


def test_budget_follows_current_equity_and_costs_are_inside_the_planned_risk():
    assert _size(equity="1200")["lots"] == Decimal("0.11")          # 60 / 512 -> 0.117 -> 0.11 (compounding up)
    assert _size(equity="700")["lots"] == Decimal("0.06")           # 35 / 512 -> 0.068 -> 0.06 (and down)
    no_costs = _size(slip="0", comm="0")
    assert no_costs["per_lot_loss"] == Decimal("500.00") and no_costs["lots"] == Decimal("0.10")
    assert _size()["lots"] < no_costs["lots"]                        # costs reduce the size, never the reverse


def test_rounding_never_exceeds_the_budget_on_random_inputs():
    rnd = random.Random(7)
    for _ in range(500):
        eq = Decimal(str(round(rnd.uniform(50, 5000), 2)))
        pct = Decimal(rnd.choice(["5", "7.5", "10"]))
        dist = Decimal(str(round(rnd.uniform(0.2, 40), 2)))
        s = _size(equity=str(eq), pct=str(pct), stop=str(Decimal("4000") - dist))
        if s["lots"] is None:
            assert s["reason"] == "risk_budget_below_min_volume" and s["min_lot_planned_risk"] > s["budget"]
            continue
        assert s["planned_risk"] <= s["budget"]
        if s["capped_by"] is None:
            assert (s["lots"] + Decimal("0.01")) * s["per_lot_loss"] > s["budget"]


def test_minimum_volume_above_budget_rejects_and_missing_stop_rejects():
    s = _size(stop="3950.00")                         # per lot (50.05 x 100 + 7) = 5,012 > 0.01 lot budget 50 / 0.01
    assert s["lots"] is None and s["reason"] == "risk_budget_below_min_volume"
    assert s["min_lot_planned_risk"] == Decimal("50.1200") and s["min_lot_planned_risk"] > s["budget"]
    s = _size(stop=None)
    assert s["lots"] is None and s["reason"] == "no_stop"


def test_maximum_volume_caps_at_the_strategy_limit_and_the_broker_maximum():
    s = _size(pct="10", stop="3999.99", max_lots="1.00")       # 100 / 13 = 7.69 lots wanted
    assert s["lots"] == Decimal("1.00") and "strategy exposure limit" in s["capped_by"]
    assert s["planned_risk"] < s["budget"]
    s = _size(equity="1000000", pct="10", stop="3999.99")      # 100,000 / 13 lots wanted > broker maximum 100
    assert s["lots"] == Decimal("100.00") and "broker maximum" in s["capped_by"]


def test_assumptions_accept_only_0_to_10_percent_for_the_scenario_mode():
    base = runner.make_assumptions({"configured_spread": "0.3", "commission": "3.5", "slippage": "0.05"}).to_mapping()
    for bad in (0, -1, 12.5):
        with pytest.raises(engine.BacktestError):
            engine.Assumptions.from_mapping({**base, "sizing_override": {"sizing": engine.RISK_EQUITY, "risk_percent": bad}})
    for sc in runner.RISK_SCENARIOS:
        a = runner.make_assumptions({"configured_spread": "0.3", "commission": "3.5", "slippage": "0.05",
                                     "risk_scenario": sc})
        assert a.sizing_override == {"sizing": engine.RISK_EQUITY, "risk_percent": sc["risk_percent"]}
        assert a.leverage == 500 and a.starting_balance == Decimal(1000)


# ------------------------------------------------------------------ the engine with percentage sizing
def _assumptions(pct=None, leverage=500):
    p = {"configured_spread": "0.30", "commission": "3.50", "slippage": "0.05"}
    if pct is not None:
        p["risk_scenario"] = {"key": "S", "risk_percent": pct}
    return engine.Assumptions.from_mapping({**runner.make_assumptions(p).to_mapping(), "leverage": leverage})


def test_percentage_runs_keep_the_signals_and_record_budget_planned_and_actual(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC)
    spec, contract = _spec(world)
    cfg = builder.validate(SESSION_LONG)
    prep = _prepared(sources, Timeframe.M5, start, end)
    fixed = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(), spec)
    assert fixed["trades"] and not any("risk_budget_usd" in t for t in fixed["trades"])     # baseline shape unchanged
    for pct in (5.0, 7.5, 10.0):
        r = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(pct), spec)
        assert [t["setup_cutoff"] for t in r["trades"]] == [t["setup_cutoff"] for t in fixed["trades"]]  # same signals
        balance = 1000.0
        for t in r["trades"]:
            assert t["sizing"] == engine.RISK_EQUITY and t["risk_percent"] == pct
            assert t["equity_at_entry"] == pytest.approx(balance, abs=0.011)              # current equity (compounding)
            assert t["risk_budget_usd"] == pytest.approx(balance * pct / 100, abs=0.011)
            assert t["planned_risk_usd"] <= t["risk_budget_usd"] + 1e-9 and t["within_budget"]
            per_lot = (abs(t["entry_price"] - t["stop"]) + 0.05) * 100 + 7.0
            assert t["planned_risk_usd"] == pytest.approx(float(t["lots"]) * per_lot, abs=0.02)
            assert t["loss_beyond_plan_usd"] == pytest.approx(max(0.0, -t["net_pnl"] - t["planned_risk_usd"]), abs=0.011)
            balance += t["net_pnl"]
        built = report.build_run(r, starting_balance=1000.0, leverage=500, contract=contract)
        pr = built["risk"]["percentage_risk"]
        assert pr["risk_percent"] == pct and pr["budget_violations"] == 0 and "within its budget" in pr["compliance"]
        assert built["charts"]["position_size"] and built["charts"]["risk_budget"] and "longest_losing_streak" in built["performance"]
        assert built["summary"]["risk_percent"] == pct


def test_leverage_does_not_change_the_size_and_margin_rejections_are_separate(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC)
    spec, contract = _spec(world)
    cfg = builder.validate(SESSION_LONG)
    prep = _prepared(sources, Timeframe.M5, start, end)
    a500 = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(10.0, 500), spec)
    a1000 = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(10.0, 1000), spec)
    assert [t["lots"] for t in a500["trades"]] == [t["lots"] for t in a1000["trades"]]      # leverage is not risk
    tight = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(10.0, 1), spec)
    built = report.build_run(tight, starting_balance=1000.0, leverage=1, contract=contract)
    assert tight["trades"] == [] and built["risk"]["margin_rejections"] > 0
    assert built["diagnostics"]["setups"] > 0                                   # signals existed: rejected, not absent
    assert {x["reason"] for x in built["rejections"]} == {"insufficient_margin"}


def test_a_gap_through_the_stop_is_reported_as_loss_beyond_plan(world, sources):
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC)
    spec, contract = _spec(world)
    cfg = builder.validate(SESSION_LONG)
    prep = _prepared(sources, Timeframe.M5, start, end)
    trades = runner.run_one(cfg, prep, Timeframe.M5, start, end, _assumptions(5.0), spec)["trades"]
    bar = pd.Timedelta(minutes=5)
    n, first = next((i, t) for i, t in enumerate(trades)                       # a trade still open after its entry bar
                    if pd.Timestamp(t["exit_time"]) > pd.Timestamp(t["entry_time"]) + bar)
    ex = prep["execution"].copy()
    after = pd.Timestamp(first["entry_time"]) + bar
    gap = first["stop"] - 15.0                                                  # the next bar opens $15 below the stop
    ex.loc[after, ["open", "high", "low", "close"]] = [gap, gap + 0.5, gap - 0.5, gap]
    r = runner.run_one(cfg, {**prep, "execution": ex}, Timeframe.M5, start, end, _assumptions(5.0), spec)
    t = r["trades"][n]
    assert t["entry_time"] == first["entry_time"]
    assert t["exit_reason"] == "stop" and t["loss_beyond_plan_usd"] > 0 and -t["net_pnl"] > t["planned_risk_usd"]
    built = report.build_run(r, starting_balance=1000.0, leverage=500, contract=contract)
    assert built["risk"]["percentage_risk"]["losses_beyond_plan"] >= 1


def test_readiness_needs_a_verified_contract_for_percentage_sizing():
    cfg = builder.validate(SESSION_LONG)
    p = {"risk_scenario": {"key": "S1", "risk_percent": 5.0}}
    assert runner.readiness(cfg, p, {"verified": False})["ready"] is False
    ok = runner.readiness(cfg, p, {"verified": True})
    assert ok["ready"] and "HYPOTHETICAL STRESS SCENARIO S1" in ok["sizing"]["mode"]


# ------------------------------------------------------------------ the study, end to end
@pytest.fixture
def client(world, tmp_path, monkeypatch):
    short = (datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 13, tzinfo=UTC))
    for mod in (mdata, service, risk, importing):
        monkeypatch.setattr(mod, "month_range", lambda m: short)
    monkeypatch.setattr(mdata, "MIN_RESEARCH_DAYS", 1)
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def _wait_eval(c, eid, done=("NOT STARTED", "READY", "COMPLETED", "INCOMPLETE", "FAILED")):
    for _ in range(6000):
        ev = c.get(f"/api/monthly/evaluations/{eid}").json()
        if not ev["running_job"] and ev["status"] in done:
            return ev
        time.sleep(0.1)
    raise AssertionError("evaluation job did not finish")


def _wait_study(c, sid):
    for _ in range(6000):
        s = c.get(f"/api/monthly/risk-studies/{sid}").json()
        if not s["running_job"] and s["study"]["status"] in ("COMPLETED", "INCOMPLETE", "FAILED"):
            return s
        time.sleep(0.1)
    raise AssertionError("study did not finish")


def _baseline(c):
    ids = sorted(s["id"] for s in c.get("/api/monthly/strategies").json()["strategies"])
    ev = c.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1,
                                                  "strategies": [{"id": i, "version": 1} for i in ids]}).json()
    c.post(f"/api/monthly/evaluations/{ev['id']}/research")
    _wait_eval(c, ev["id"])
    assert c.post(f"/api/monthly/evaluations/{ev['id']}/freeze").status_code == 200
    assert c.post(f"/api/monthly/evaluations/{ev['id']}/run").status_code == 202
    return _wait_eval(c, ev["id"])


def _runs_snapshot(c, eid):
    ev = c.get(f"/api/monthly/evaluations/{eid}").json()
    return {r["id"]: (r["status"], r["result_hash"], r["fingerprint"],
                      c.get(f"/api/monthly/runs/{r['id']}").json()["run"]["result"]) for r in ev["runs"]}, ev


def test_risk_study_27_independent_runs_reports_and_an_untouched_baseline(client, world):
    app_before = _table_counts(world["app"])
    base = _baseline(client)
    assert base["status"] == "COMPLETED"
    # refusals: not from an unfinished evaluation, not above 10 %, unknown candidates
    pending = client.post("/api/monthly/evaluations", json={"month": "2026-10", "research_days": 1}).json()
    assert client.post("/api/monthly/risk-studies", json={"baseline_evaluation": pending["id"]}).status_code == 409
    assert client.post("/api/monthly/risk-studies", json={"baseline_evaluation": base["id"],
                                                          "scenarios": [5, 12.5]}).status_code == 409
    assert client.post("/api/monthly/risk-studies", json={"baseline_evaluation": base["id"],
                                                          "candidates": ["R99"]}).status_code == 409
    before, _ = _runs_snapshot(client, base["id"])
    created = client.post("/api/monthly/risk-studies", json={"baseline_evaluation": base["id"], "candidates": ["R1"]})
    assert created.status_code == 201, created.text
    sid = created.json()["study"]["id"]
    s = _wait_study(client, sid)
    assert s["study"]["status"] == "COMPLETED" and len(s["study"]["evaluation_ids"]) == 3
    assert base["id"] not in s["study"]["evaluation_ids"]
    pcts, seen_runs = [], set()
    for eid in s["study"]["evaluation_ids"]:
        ev = client.get(f"/api/monthly/evaluations/{eid}").json()
        sc = ev["params"]["risk_scenario"]
        pcts.append(sc["risk_percent"])
        assert ev["params"]["baseline_evaluation"] == base["id"] and ev["status"] == "COMPLETED"
        assert [f["fingerprint"] for f in ev["frozen"]["strategies"]] == [f["fingerprint"] for f in base["frozen"]["strategies"]]
        existing = [r for r in ev["runs"] if r["kind"] == "existing"]
        assert {(r["strategy_key"], r["timeframe"]) for r in existing} == {(k, tf) for k in "ABC" for tf in ("M15", "M5", "M1")}
        cand = [r for r in ev["runs"] if r["kind"] == "candidate"]
        assert {r["strategy_key"] for r in cand} == {"R1"}
        assert ev["candidates"][0]["config"] == base["candidates"][0]["config"]          # chosen RR variant unchanged
        assert client.post(f"/api/monthly/evaluations/{eid}/run").status_code == 409    # only via the study
        for r in existing:
            seen_runs.add(r["id"])
            full = client.get(f"/api/monthly/runs/{r['id']}").json()["run"]
            assert full["assumptions"]["engine"]["sizing_override"] == {"sizing": engine.RISK_EQUITY,
                                                                        "risk_percent": sc["risk_percent"]}
            perf = full["result"]["performance"]
            assert perf["starting_balance"] == 1000.0 and full["assumptions"]["engine"]["leverage"] == 500
            assert full["dataset"]["run_timeframe"] == r["timeframe"] and full["dataset"]["execution_origin"] == "native"
            curve = full["result"]["charts"]["balance"]
            assert not curve or curve[0]["value"] == pytest.approx(1000.0, abs=50)     # its own $1,000 account
            for t in full["result"]["trades"]:
                assert t["risk_percent"] == sc["risk_percent"] and t["within_budget"]
    assert sorted(pcts) == [5.0, 7.5, 10.0] and len(seen_runs) == 27
    # reproducible: same inputs -> same hash
    rid = sorted(seen_runs)[0]
    rep = client.post(f"/api/monthly/runs/{rid}/reproduce").json()
    assert rep["identical"] and rep["same_input_data"]
    # the fixed-lot baseline is unchanged
    after, base_after = _runs_snapshot(client, base["id"])
    assert after == before and base_after["status"] == "COMPLETED" and base_after["params"].get("risk_scenario") is None
    # reports
    m = client.get(f"/api/monthly/risk-studies/{sid}").json()
    assert [c["key"] for c in m["columns"]] == ["baseline", "S1", "S2", "S3"] and len(m["rows"]) == 9
    assert "High-risk hypothetical simulation scenarios" in m["banner"] and "never added" in m["separate_accounts_note"]
    assert {r["strategy"] for r in m["candidates"]} == {"R1"}
    for key in ("A", "B", "C"):
        sc_view = client.get(f"/api/monthly/risk-studies/{sid}/strategies/{key}/scenarios").json()
        assert len(sc_view["rows"]) == 3 and sc_view["findings"][-1].startswith("No scenario is a recommended setting")
        tf_view = client.get(f"/api/monthly/risk-studies/{sid}/strategies/{key}/timeframes").json()
        assert [r["timeframe"] for r in tf_view["rows"]] == ["M15", "M5", "M1"]
    for path in (f"/api/monthly/risk-studies/{sid}/master.pdf", f"/api/monthly/risk-studies/{sid}/strategies/A/scenarios.pdf",
                 f"/api/monthly/risk-studies/{sid}/strategies/B/timeframes.pdf", f"/api/monthly/runs/{rid}/report.pdf"):
        r = client.get(path)
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), path
    with_trades = next(r for r in sorted(seen_runs)
                       if client.get(f"/api/monthly/runs/{r}").json()["run"]["result"]["trades"])
    ledger = client.get(f"/api/monthly/runs/{with_trades}/ledger.csv").text
    assert "HYPOTHETICAL percentage-risk" in ledger and "risk_budget_usd" in ledger and "planned_risk_usd" in ledger
    base_ledger = client.get(f"/api/monthly/runs/{base['runs'][0]['id']}/ledger.csv").text
    assert "risk_budget_usd" not in base_ledger and "HYPOTHETICAL" not in base_ledger          # baseline unchanged
    csv_text = client.get(f"/api/monthly/risk-studies/{sid}/master.csv").text
    assert "HYPOTHETICAL" in csv_text and csv_text.count("\n") == 2 + (9 + 3) * 4
    # a recorded re-run of one timeframe replaces only that timeframe's runs, with identical results
    m15_before = {r: client.get(f"/api/monthly/runs/{r}").json()["run"]["result_hash"] for r in seen_runs if r.endswith("_M15")}
    m5_before = {r: client.get(f"/api/monthly/runs/{r}").json()["run"]["finished_at"] for r in seen_runs if r.endswith("_M5")}
    assert client.post(f"/api/monthly/risk-studies/{sid}/run", json={"timeframes": ["M15"]}).status_code == 422  # reason
    assert client.post(f"/api/monthly/risk-studies/{sid}/run", json={"timeframes": ["M15"], "reason": "test"}).status_code == 202
    s2 = _wait_study(client, sid)
    assert s2["study"]["status"] == "COMPLETED"
    assert {r: client.get(f"/api/monthly/runs/{r}").json()["run"]["result_hash"] for r in m15_before} == m15_before
    assert {r: client.get(f"/api/monthly/runs/{r}").json()["run"]["finished_at"] for r in m5_before} == m5_before
    assert _table_counts(world["app"]) == app_before                   # the application database is untouched


# ------------------------------------------------------------------ CSV import: preview, labels, idempotency, coverage
def _mt5_rows(days, start_h=1, end_h=23, spread=26, bad=()):
    """MT5 export rows (server time). ``days``: 'YYYY.MM.DD' strings."""
    lines = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    px = 3850.0
    for d in days:
        for h in range(start_h, end_h):
            for mnt in range(60):
                px += 0.01
                o, c = px, px + 0.05
                hi, lo = max(o, c) + 0.2, min(o, c) - 0.2
                if (d, h, mnt) in bad:
                    hi = lo - 1                                     # high below low: inconsistent OHLC
                lines.append(f"{d}\t{h:02d}:{mnt:02d}:00\t{o:.2f}\t{hi:.2f}\t{lo:.2f}\t{c:.2f}\t100\t0\t{spread}")
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture
def research_client(world, tmp_path):
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "m.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c, tmp_path / "m.sqlite3"


FORM = {"dialect": "mt5_export", "timeframe": "M1", "time_basis": "rule:ny_close", "instrument": "XAUUSD",
        "point": "0.01", "month": "2026-10", "series_label": "vantage-test"}


def test_preview_converts_server_time_across_the_us_dst_change_without_storing(research_client):
    c, store = research_client
    body = _mt5_rows(["2026.10.30", "2026.11.02"], start_h=10, end_h=11)      # US DST ends Sunday 1 Nov 2026
    r = c.post("/api/monthly/import/preview", data=FORM, files={"file": ("XAUUSD_M1_test.csv", body, "text/csv")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["first"] == "2026-10-30T07:00:00Z"                             # UTC+3 before the change
    assert out["last"] == "2026-11-02T08:59:00Z"                              # UTC+2 after it
    assert out["dst_transitions"] == [{"date": "2026-11-01", "offset_before_hours": 3.0, "offset_after_hours": 2.0}]
    checks = {x["check"]: x["status"] for x in out["checks"]}
    assert checks["CSV structure"] == "PASS" and checks["Time zone (time basis)"] == "PASS"
    assert checks["2026-10 coverage"] == "FAIL" and out["coverage"]["ready"] is False
    assert out["samples"][0]["spread_usd"] == 0.26 and out["samples"][0]["utc"] == "2026-10-30T07:00:00Z"
    con = sqlite3.connect(store)
    assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0      # preview stores nothing
    con.close()


def test_preview_reports_bad_rows_wrong_timeframe_and_instrument(research_client):
    c, _ = research_client
    body = _mt5_rows(["2026.10.01"], start_h=10, end_h=12, bad={("2026.10.01", 10, 5)})
    body = body.replace(b"2026.10.01\t10:07:00", b"2026.13.01\t10:07:00")    # an impossible date
    out = c.post("/api/monthly/import/preview", data=FORM, files={"file": ("EURUSD_M5_x.csv", body, "text/csv")}).json()
    checks = {x["check"]: x for x in out["checks"]}
    assert checks["Date and time parsing"]["status"] == "FAIL" and "1 row" in checks["Date and time parsing"]["detail"]
    assert checks["OHLC values and prices"]["status"] == "FAIL"
    assert checks["Timeframe in file name"]["status"] == "FAIL" and checks["Instrument"]["status"] == "WARN"
    assert out["importable"] is False


def test_import_is_idempotent_labelled_and_coverage_lists_missing_dates(research_client, world):
    c, store = research_client
    app_before = _table_counts(world["app"])
    days = [f"2026.10.{d:02d}" for d in range(1, 31) if datetime(2026, 10, d).weekday() < 5 and d not in (14, 15)]
    body = _mt5_rows(days, start_h=1, end_h=23)
    form = {**FORM, "label": "Vantage XAUUSD M1 October 2026"}
    files = {"file": ("XAUUSD_M1_oct.csv", body, "text/csv")}
    first = c.post("/api/monthly/import", data=form, files=files).json()
    second = c.post("/api/monthly/import", data=form, files=files).json()
    assert first["rows_stored"] > 0 and not first["already_imported"]
    assert second["already_imported"] and second["rows_stored"] == 0
    con = sqlite3.connect(store)
    n = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    con.close()
    assert n == first["rows_stored"]                                           # no duplicate candles
    imports = c.get("/api/monthly/imports").json()["imports"]
    assert len(imports) == 1 and imports[0]["label"] == "Vantage XAUUSD M1 October 2026"
    cov = c.get("/api/monthly/coverage", params={"month": "2026-10", "timeframe": "M1", "store": "research",
                                                  "series": "csv:vantage-test"}).json()
    missing_days = {d["date"] for d in cov["missing_dates"]}
    assert {"2026-10-14", "2026-10-15"} <= missing_days and cov["missing"] > 0
    assert cov["status"] == "INCOMPLETE" and cov["warmup"]["satisfied"] is False   # no September history
    assert cov["total_candles"] == n and cov["earliest"] and cov["missing_intervals"]
    assert _table_counts(world["app"]) == app_before


def test_month_coverage_never_fills_missing_bars():
    sched = InstrumentSpec.load(SPEC_PATH).schedule
    idx = sched.expected_bars(datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), 60)
    holed = idx[(idx < pd.Timestamp("2026-10-07T10:00Z")) | (idx >= pd.Timestamp("2026-10-07T11:00Z"))]
    cov = importing.month_coverage(holed, Timeframe.M1, sched, "2026-10", 5)
    assert cov["missing"] == 60 and cov["missing_intervals"] == [
        {"from": "2026-10-07T10:00:00Z", "to": "2026-10-07T11:00:00Z", "bars": 60}]
    assert cov["warmup"]["satisfied"] and cov["ready"] and cov["share"] < 1.0


def test_timeline_cache_is_never_shared_between_series_with_identical_candles(world, sources):
    """Snapshots carry series-specific signal ids: an MT5 export identical to the stored candles must not reuse the
    application series' cached timeline (found on 14 Nov 2026 during the Phase 5.1 October study)."""
    from goldscalp.analysis.config import AnalysisConfig
    from goldscalp.analysis.replay import ReplayDataset
    from goldscalp.backtesting import features
    start, end = datetime(2026, 10, 1, 7, tzinfo=UTC), datetime(2026, 10, 1, 9, tzinfo=UTC)
    ds = runner.load_dataset(sources, "app", _row(sources), Timeframe.M5, start, end, 5)
    twin = ReplayDataset(series_key="csv:twin", source_id="csv:mt5_export", base_tf=ds.base_tf, base=ds.base,
                         history=ds.history, note="identical candles, another series")
    assert features.dataset_hash(twin) == features.dataset_hash(ds)
    cuts = features.step_cutoffs(ds, Timeframe.M5, start, end)
    a = features.compute_timeline(ds, Timeframe.M5, cuts, AnalysisConfig.load(), detail=set())
    b = features.compute_timeline(twin, Timeframe.M5, cuts, AnalysisConfig.load(), detail=set())
    assert a is not b
    ids_a = [s["signal"]["id"] for s in a if s["signal"]]
    ids_b = [s["signal"]["id"] for s in b if s["signal"]]
    assert ids_a and len(ids_a) == len(ids_b) and not set(ids_a) & set(ids_b)
