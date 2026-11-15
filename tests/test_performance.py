"""Part 11: historical signal performance evaluation (perf@1). Hand-worked trade outcomes and costs,
aggregation, signal-change logic, no look-ahead, and the API flow on recorded MT5 candles."""
from __future__ import annotations

import threading
import time
import warnings
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from goldscalp.analysis import intelligence, multi_timeframe, performance, replay, signals
from goldscalp.analysis.config import default_config
from goldscalp.api.app import create_app
from goldscalp.api.routers.performance import PerformanceJobs
from goldscalp.core.clock import FixedClock, iso
from goldscalp.core.config import Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.storage import performance_repo
from goldscalp.storage.db import Database
from tests.conftest import SPEC_PATH, recorded

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

START = pd.Timestamp("2026-11-11T14:00Z")
COSTS = CostModel.from_mapping({"spread_mode": "market_else_configured", "configured_spread": "0.30",
                                "commission_per_lot_per_side": "3.50", "slippage": "0.05"})
SPEC = InstrumentSpec.load(SPEC_PATH)        # 100 oz per lot -> 0.10 lots = 10 oz


def candles(rows, spread=0.20) -> pd.DataFrame:
    idx = pd.date_range(START, periods=len(rows), freq="min", tz="UTC")
    o, h, low, c = (np.array([r[k] for r in rows], dtype=float) for k in range(4))
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "tick_volume": 1, "spread": spread,
                         "closed": True}, index=idx)


def trade(rows, direction="BULLISH", invalidation=99.0, spread=0.20, target_r=2.0, horizon_m5=1):
    event = {"direction": direction, "cutoff": "2026-11-11T14:00:00Z", "atr_m5": 1.0, "reference_price": 100.0,
             "invalidation": [{"price": invalidation, "condition": "test level"}] if invalidation else []}
    return performance.simulate_trade(event, candles(rows, spread), Timeframe.M1, horizon_m5=horizon_m5,
                                      stop_atr_fallback=1.5, target_r=target_r, lots=Decimal("0.10"), spec=SPEC,
                                      costs=COSTS)


FLAT = (100.2, 100.4, 100.1, 100.3)


# ------------------------------------------------------------------------------- hypothetical trades
def test_long_stop_with_exact_costs():
    # entry = ask + slippage = 100 + 0.20 + 0.05 = 100.25; stop 99.00 is hit on the 2nd candle (open above it)
    # exit = stop bid - slippage = 98.95; gross = (98.95 - 100.25) x 10 oz = -13.00; commission 2 x 0.35
    t = trade([(100, 100.5, 99.8, 100.2), (100.2, 100.3, 98.9, 99.0), FLAT, FLAT, FLAT])
    assert (t["outcome"], t["entry_price"], t["exit_price"], t["gross_pnl"], t["commission"], t["net_pnl"]) == \
        ("stop", 100.25, 98.95, -13.0, 0.7, -13.7)
    assert (t["spread_cost"], t["slippage_cost"], t["risk_per_oz"], t["target"]) == (2.0, 1.0, 1.25, 102.75)
    assert t["exit_time"] == "2026-11-11T14:02:00Z" and t["stop_source"] == "invalidation: test level"
    assert t["r_multiple"] == round(-13.7 / (1.25 * 10), 2)


def test_long_target_then_same_candle_stop_first_then_gap_through_stop():
    target = trade([(100, 101, 99.5, 100.8), (100.8, 103, 100.5, 102.9), FLAT, FLAT, FLAT])
    assert (target["outcome"], target["exit_price"], target["net_pnl"]) == ("target", 102.7, 23.8)
    both = trade([(100, 101, 99.5, 100.8), (100.8, 103, 98.5, 100), FLAT, FLAT, FLAT])
    assert (both["outcome"], both["net_pnl"]) == ("stop", -13.7)                 # conservative: stop first
    gap = trade([(100, 100.5, 99.8, 100.2), (98.0, 98.5, 97.5, 98.2), FLAT, FLAT, FLAT])
    assert (gap["outcome"], gap["exit_price"], gap["net_pnl"]) == ("stop", 97.95, -23.7)   # filled at the open


def test_horizon_exit_and_insufficient_data():
    t = trade([(100, 100.5, 99.8, 100.2)] * 4 + [(100.2, 100.7, 100.1, 100.6)])
    assert (t["outcome"], t["exit_reason"], t["exit_price"], t["net_pnl"]) == ("horizon_win", "horizon", 100.55, 2.3)
    assert t["exit_time"] == "2026-11-11T14:05:00Z"
    assert trade([(100, 100.5, 99.8, 100.2)] * 3)["outcome"] == "insufficient_data"


def test_short_pays_the_spread_on_the_ask_side():
    # entry = bid - slippage = 99.95; stop 101.00 is checked on ask = bid + 0.20: high 100.85 -> ask 101.05
    t = trade([(100, 100.3, 99.9, 100.0), (100.2, 100.85, 100.1, 100.6), FLAT, FLAT, FLAT], direction="BEARISH",
              invalidation=101.0)
    assert (t["outcome"], t["entry_price"], t["exit_price"], t["net_pnl"], t["spread_cost"]) == \
        ("stop", 99.95, 101.05, -11.7, 2.0)


def test_fallback_stop_and_configured_spread():
    t = trade([FLAT] * 5, invalidation=101.0)                     # above the bid: wrong side for a long
    assert t["stop_source"].startswith("fallback") and t["stop"] == round(100.2 - 1.5, 3)
    t = trade([(100, 100.5, 99.8, 100.2)] * 5, spread=np.nan)
    assert (t["entry_spread"], t["entry_spread_source"], t["entry_price"]) == (0.3, "configured", 100.35)


# ------------------------------------------------------------------------------- aggregation
def test_profit_factor_only_when_meaningful():
    nets = [Decimal("10"), Decimal("-5"), Decimal("5")]
    assert performance.profit_factor(nets, 3)["value"] == 3.0
    assert performance.profit_factor(nets, 4)["value"] is None and "at least 4" in performance.profit_factor(nets, 4)["reason"]
    assert performance.profit_factor([Decimal("1")] * 5, 2)["reason"] == "not meaningful: no losing trades"


def test_equity_curve_and_max_drawdown():
    trades = [{"_net": Decimal(n), "exit_time": f"t{i}"} for i, n in enumerate(["100", "-300", "50", "-100"])]
    curve = performance.equity_curve(trades, Decimal("1000"))
    assert [p["equity"] for p in curve["points"]] == [1100.0, 800.0, 850.0, 750.0]
    assert (curve["max_drawdown"], curve["max_drawdown_pct"], curve["final_equity"]) == (350.0, 31.82, 750.0)


def test_analytical_classification_uses_the_flat_band():
    after = candles([(100, 100.5, 99.5, 100.1)] * 4 + [(100.1, 101, 99, 100.6)])
    ev = {"direction": "BULLISH", "cutoff": "2026-11-11T14:00:00Z", "reference_price": 100.0, "atr_m5": 1.0}
    assert performance.analytical_outcome(ev, after, Timeframe.M1, 1, 0.25)["classification"] == "favourable"
    assert performance.analytical_outcome(ev, after, Timeframe.M1, 1, 1.0)["classification"] == "flat"
    assert performance.analytical_outcome({**ev, "direction": "BEARISH"}, after, Timeframe.M1, 1, 0.25)["classification"] == "adverse"
    assert performance.analytical_outcome(ev, after.iloc[:3], Timeframe.M1, 1, 0.25)["classification"] == "insufficient_data"


def test_only_changes_of_classification_are_signals(monkeypatch):
    sequence = ["NEUTRAL", "BULLISH", "BULLISH", "NO SIGNAL", "BULLISH", "BEARISH", "BEARISH", "NEUTRAL"]
    calls = iter(sequence)
    monkeypatch.setattr(performance, "frames_at", lambda ds, cut: {})
    monkeypatch.setattr(multi_timeframe, "build", lambda *a, **k: {})

    def fake_evaluate(results, cutoff, cfg, **kwargs):
        d = next(calls)
        return {"direction": d, "id": f"{d}@{cutoff}", "gates_failed": ["M5: stale data (x)"] if d == "NO SIGNAL" else []}
    monkeypatch.setattr(signals, "evaluate", fake_evaluate)
    ds = replay.ReplayDataset("s", "src", Timeframe.M1, candles([FLAT]))
    cuts = [START + pd.Timedelta(minutes=i + 1) for i in range(len(sequence))]
    out = performance.generate_signals(ds, Timeframe.M1, cuts, default_config(), stream_id="r:M1", generated_at=START)
    assert [e["direction"] for e in out["events"]] == ["BULLISH", "BULLISH", "BEARISH"]
    assert len(out["neutral"]) == 2 and out["gated_steps"] == {"stale data": 1}
    assert out["steps_by_direction"] == {"NEUTRAL": 2, "BULLISH": 3, "NO SIGNAL": 1, "BEARISH": 2}


# ------------------------------------------------------------------------------- end to end (recorded MT5 data)
@pytest.fixture(scope="module")
def rec():
    m1, h1 = recorded("mt5_xauusd_m1_recorded.csv"), recorded("mt5_xauusd_h1_recorded.csv")
    start = (m1.index[0] + pd.Timedelta(hours=3)).to_pydatetime()
    end = (m1.index[-1] - pd.Timedelta(hours=2)).floor("15min").to_pydatetime()
    signal_ds = replay.ReplayDataset("rec", "mt5", Timeframe.M1, m1[m1.index < pd.Timestamp(end)], history={"H1": h1})
    future_ds = replay.ReplayDataset("rec", "mt5", Timeframe.M1, m1, history={"H1": h1})
    params = dict(default_config()["performance"])
    result = performance.evaluate(signal_ds, m1, [Timeframe.M15], start, end, default_config(), params, SPEC, COSTS,
                                  run_id="pr_test", generated_at=START.to_pydatetime())
    return {"m1": m1, "h1": h1, "start": start, "end": end, "signal_ds": signal_ds, "future_ds": future_ds,
            "params": params, "result": result}


def test_evaluation_totals_are_consistent(rec):
    r = rec["result"]
    (s,) = r["streams"]
    c, st = s["counts"], s["strategy"]
    assert c["total_signals"] == c["bullish"] + c["bearish"] + c["neutral_observations"]
    assert c["bullish"] + c["bearish"] > 0, "the recorded fixture should contain directional signals"
    assert st["trades"] + st["skipped_overlap"] + st["insufficient_data"] == c["bullish"] + c["bearish"]
    assert st["equity"]["final_equity"] == round(10000 + st["net_pnl"], 2)
    assert sum(c["steps_by_direction"].values()) == s["steps"] == r["steps"]
    for item in r["items"]:
        assert r["range"]["start"] <= item["cutoff"] <= r["range"]["end"]
        if item["trade"]["outcome"] not in ("skipped_overlap", "insufficient_data"):
            assert item["trade"]["commission"] == 0.7 and item["trade"]["slippage_cost"] == 1.0
            assert item["trade"]["entry_time"] >= item["cutoff"]          # entry only after the signal is known
    assert r["disclaimer"] == performance.DISCLAIMER and "do not predict future" in r["disclaimer"]
    assert sum(g["signals"] for g in r["by_session"]) == len(r["items"])
    assert sum(g["signals"] for g in r["by_volatility"]) == len(r["items"])


def test_signals_do_not_depend_on_candles_after_their_cutoff(rec):
    cuts = performance.step_cutoffs(rec["signal_ds"], Timeframe.M15, rec["start"], rec["end"])
    a = performance.generate_signals(rec["signal_ds"], Timeframe.M15, cuts, default_config(), stream_id="x",
                                     generated_at=START.to_pydatetime())
    b = performance.generate_signals(rec["future_ds"], Timeframe.M15, cuts, default_config(), stream_id="x",
                                     generated_at=START.to_pydatetime())
    assert [e["id"] for e in a["events"]] == [e["id"] for e in b["events"]]
    assert a["steps_by_direction"] == b["steps_by_direction"]
    steps = {iso(c.to_pydatetime()) for c in cuts}
    assert all(e["cutoff"] in steps for e in a["events"] + a["neutral"])     # the data cut-off IS the step close
    for ev in a["events"][:4]:                                              # same as a dataset cut at that time
        cut = pd.Timestamp(ev["cutoff"])
        truncated = replay.ReplayDataset("rec", "mt5", Timeframe.M1, rec["m1"][rec["m1"].index < cut],
                                         history={"H1": rec["h1"]})
        again = performance.generate_signals(truncated, Timeframe.M15, [cut], default_config(), stream_id="x",
                                             generated_at=START.to_pydatetime())
        assert again["events"][0]["id"] == ev["id"] and again["events"][0]["score"] == ev["score"]


def test_bulk_signals_equal_the_dashboard_and_replay_signal(rec):
    """The evaluation's light pass (detail only on M5/M15) produces the exact signal of the full pass."""
    events = [i["signal"] for i in rec["result"]["_items"]][:3]
    assert events
    ds = rec["signal_ds"]
    for ev in events:
        cut = datetime.fromisoformat(ev["cutoff"].replace("Z", "+00:00"))
        frames = replay.frames_at(ds, cut)
        spread = float(frames["M1"]["spread"].iloc[-1])
        full = intelligence.run(frames, cut, default_config(), chart_tf=Timeframe.M15, mode="replay", series="rec",
                                market_state=None, spread=spread, spread_source="historical candle spread",
                                source="mt5", generated_at=START.to_pydatetime(), replay_id="pr_test:M15")["signal"]
        assert {k: v for k, v in full.items() if k != "generated_at"} == {k: v for k, v in ev.items() if k != "generated_at"}


def test_step_limit_is_never_silently_applied(rec):
    with pytest.raises(performance.EvaluationError, match="never shortened"):
        performance.evaluate(rec["signal_ds"], rec["m1"], [Timeframe.M1], rec["start"], rec["end"], default_config(),
                             {**rec["params"], "max_steps": 10}, SPEC, COSTS, run_id="x", generated_at=START.to_pydatetime())


# ------------------------------------------------------------------------------- jobs, storage, API
def test_only_one_job_runs_at_a_time():
    jobs = PerformanceJobs()
    release, started = threading.Event(), threading.Event()
    jobs.start("a", lambda cancel: (started.set(), release.wait(5)))
    started.wait(5)
    with pytest.raises(RuntimeError):
        jobs.start("b", lambda cancel: None)
    assert jobs.busy() == "a" and jobs.cancel("a") is True
    release.set()
    for _ in range(50):
        if jobs.busy() is None:
            break
        time.sleep(0.05)
    assert jobs.busy() is None and jobs.cancel("a") is False


def csv_of(frame: pd.DataFrame) -> bytes:
    lines = ["timestamp,open,high,low,close,tick_volume,spread"]
    for ts, r in frame.iterrows():
        lines.append(f"{ts.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['tick_volume']},{r['spread']}")
    return "\n".join(lines).encode()


@pytest.fixture
def api(tmp_path):
    clock = FixedClock(datetime(2026, 11, 13, 19, 30, 30, tzinfo=UTC))
    app = create_app(Settings(db_path=tmp_path / "perf.sqlite3", live_provider="none", max_import_mb=5), clock=clock,
                     provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        for name, tf in (("mt5_xauusd_h1_recorded.csv", "H1"), ("mt5_xauusd_m1_recorded.csv", "M1")):
            r = c.post("/api/data/import", files={"file": (name, csv_of(recorded(name)), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": tf, "time_basis": "utc", "series_label": "rec",
                             "candle_side": "bid", "price_decimals": "2"})
            assert r.status_code == 200, r.text
        yield c


def wait_for(c, run_id, seconds=120):
    for _ in range(seconds * 10):
        run = c.get(f"/api/performance/runs/{run_id}").json()["run"]
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_performance_api_flow(api):
    defaults = api.get("/api/performance/defaults").json()
    assert defaults["config"] == default_config()["performance"] and defaults["costs"]["commission_per_lot_per_side"]
    body = {"series": "csv:rec", "timeframes": ["M15"], "start": "2026-11-12T11:00:00Z", "end": "2026-11-13T17:00:00Z"}
    created = api.post("/api/performance/runs", json=body)
    assert created.status_code == 202, created.text
    run = wait_for(api, created.json()["run"]["id"])
    assert run["status"] == "complete", run["error"]
    result = run["result"]
    (stream,) = result["streams"]
    directional = stream["counts"]["bullish"] + stream["counts"]["bearish"]
    assert directional > 0 and len(result["items"]) == directional
    assert result["coverage"]["history_before_start"]["H1"] > 0 and result["coverage"]["outcome_candles_after_end"] > 0
    stored = api.get(f"/api/signals?replay_id={run['id']}:M15&limit=500").json()["signals"]
    assert len(stored) == directional and all(s["evaluation"]["performance_run"] == run["id"] for s in stored)
    assert all(s["mode"] == "replay" for s in stored)                   # never linkable to a paper position
    config = api.get(f"/api/performance/configs/{run['config_fingerprint']}").json()
    assert config["config"] == default_config().data
    assert api.get("/api/performance/runs").json()["runs"][0]["id"] == run["id"]
    deleted = api.delete(f"/api/performance/runs/{run['id']}").json()
    assert deleted["signals_deleted"] == directional
    assert api.get(f"/api/performance/runs/{run['id']}").status_code == 404


def test_performance_api_refusals(api):
    base = {"series": "csv:rec", "start": "2026-11-12T11:00:00Z", "end": "2026-11-13T17:00:00Z"}
    assert api.post("/api/performance/runs", json={**base, "timeframes": ["H1"]}).status_code == 422
    assert api.post("/api/performance/runs", json={**base, "end": base["start"], "timeframes": ["M5"]}).status_code == 422
    assert api.post("/api/performance/runs", json={**base, "lots": "0.003", "timeframes": ["M5"]}).status_code == 422
    assert api.post("/api/performance/runs", json={**base, "series": "nope", "timeframes": ["M5"]}).status_code == 404
    assert api.post("/api/performance/runs/pr_x/cancel").status_code == 409
    assert api.delete("/api/performance/runs/pr_x").status_code == 404


def test_runs_left_running_by_a_stopped_server_are_marked_failed(tmp_path):
    db = Database(tmp_path / "i.sqlite3")
    db.migrate()
    now = datetime(2026, 11, 13, tzinfo=UTC)
    with db.transaction() as con:
        performance_repo.save_config(con, default_config().fingerprint, default_config().data, now)
        performance_repo.create_run(con, "pr_old", created_at=now, series_key="s", params={}, total=5,
                                    fingerprint=default_config().fingerprint)
    create_app(Settings(db_path=tmp_path / "i.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with db.read() as con:
        run = performance_repo.get_run(con, "pr_old")
    assert run["status"] == "failed" and "server stopped" in run["error"]


def test_evaluation_never_touches_paper_accounts(api):
    before = api.get("/api/sim/positions").json()["data"]
    body = {"series": "csv:rec", "timeframes": ["M15"], "start": "2026-11-13T08:00:00Z", "end": "2026-11-13T12:00:00Z"}
    wait_for(api, api.post("/api/performance/runs", json=body).json()["run"]["id"])
    assert api.get("/api/sim/positions").json()["data"] == before
    assert api.get("/api/sim/ledger").status_code == 200


def test_outcome_window_extends_past_end_only_for_outcomes():
    assert performance.STEP_TIMEFRAMES == ("M1", "M5", "M15")
    from goldscalp.api.routers.performance import OUTCOME_EXTENSION
    assert OUTCOME_EXTENSION == timedelta(days=2)


def test_bulk_signal_uses_the_spread_known_at_the_cutoff(rec):
    """The next candle's spread must not leak into the signal (a wide spread there would gate it)."""
    m1 = rec["m1"].copy()
    cut = pd.Timestamp("2026-11-13T12:00Z")
    m1.loc[m1.index >= cut, "spread"] = 9.99
    ds = replay.ReplayDataset("rec", "mt5", Timeframe.M1, m1, history={"H1": rec["h1"]})
    out = performance.generate_signals(ds, Timeframe.M15, [cut], default_config(), stream_id="x",
                                       generated_at=START.to_pydatetime())
    assert out["steps_by_direction"].get("NO SIGNAL", 0) == 0
    (sig,) = out["events"] + out["neutral"]
    assert sig["context"]["spread"] == float(m1.loc[m1.index < cut, "spread"].iloc[-1])
