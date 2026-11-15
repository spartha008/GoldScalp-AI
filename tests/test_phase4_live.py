"""Phase 4 live strategy evaluation (MOCKED market feed over RECORDED MT5 candles; no terminal, no broker):
signal generation at cut-offs, backtest parity, no look-ahead, gates, SIMULATED validation trades in isolated
paper accounts, SL / time exits, MFE/MAE, signal outcomes with the backtest replica, the journal and the per-trade
evaluation, and the 0006 migration."""
from __future__ import annotations

import json
import sqlite3
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from goldscalp.api.app import create_app
from goldscalp.api.services import load_dataset, pick_series
from goldscalp.backtesting import features
from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.live import monitor as monitor_mod
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.storage import live_repo, performance_repo, research_repo, strategy_repo
from goldscalp.storage.db import Database
from goldscalp.strategies import builder, conditions
from tests.conftest import make_quote, recorded

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

T0 = datetime(2026, 11, 13, 19, 20, tzinfo=UTC)          # monitoring starts
CUT = datetime(2026, 11, 13, 19, 30, tzinfo=UTC)         # an M5 close covered by the recorded data
M1 = recorded("mt5_xauusd_m1_recorded.csv")


def csv_of(frame) -> bytes:
    lines = ["timestamp,open,high,low,close,tick_volume,spread"]
    for ts, r in frame.iterrows():
        lines.append(f"{ts.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['tick_volume']},{r['spread']}")
    return "\n".join(lines).encode()


@pytest.fixture
def lab(tmp_path, account_config, monkeypatch):
    clock = FixedClock(datetime(2026, 11, 14, 0, 0, tzinfo=UTC))      # import with every recorded candle in the past
    app = create_app(Settings(db_path=tmp_path / "p4.sqlite3", live_provider="none", max_import_mb=5),
                     clock=clock, provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        for name, tf in (("mt5_xauusd_h1_recorded.csv", "H1"), ("mt5_xauusd_m1_recorded.csv", "M1")):
            r = c.post("/api/data/import", files={"file": (name, csv_of(recorded(name)), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": tf, "time_basis": "utc", "series_label": "rec",
                             "candle_side": "bid", "price_decimals": "2"})
            assert r.status_code == 200, r.text
        gs = app.state.gs
        box = {"q": None, "state": (MarketState.LIVE, "mocked feed")}
        monkeypatch.setattr(gs.market, "market_quote", lambda: box["q"])
        monkeypatch.setattr(gs.market, "state", lambda: box["state"])
        monkeypatch.setattr(monitor_mod, "active_series_key", lambda gs_: "csv:rec")
        gs.engine = PaperTradingEngine(gs.db, gs.clock, lambda: box["q"], gs.spec, gs.paper_costs, account_config)
        gs.engine.ensure_account()
        c.gs, c.clock, c.box = gs, clock, box
        clock.set(T0)
        yield c


def quote(c, bid: str, ask: str, at: datetime | None = None) -> None:
    c.box["q"] = make_quote(bid, ask, at=at or c.clock.now() - timedelta(seconds=1))


def ny_strategy(c, *, hold=30, extra=None, direction="long") -> dict:
    cfg = {"name": "NY session long", "direction": direction, "evaluation": {"step_timeframe": "M5"},
           "conditions": [{"id": "ny", "type": "session", "sessions": ["New York"]}] + (extra or []),
           "exit": {"stop": {"type": "fixed", "distance": 5}, "target": {"type": "fixed", "distance": 10},
                    "max_holding_minutes": hold}, "risk": {"lots": "0.02"}}
    sid = c.post("/api/strategies", json=cfg).json()["id"]
    with c.gs.db.read() as con:
        return strategy_repo.get(con, sid)


def mark_validated(gs, record, run_id="rr_fixture") -> None:
    """A completed Phase 3 run containing this version (test fixture standing in for a Strategy Lab backtest)."""
    with gs.db.transaction() as con:
        performance_repo.save_config(con, gs.analysis_cfg.fingerprint, gs.analysis_cfg.data, gs.clock.now())
        research_repo.create(con, run_id, kind="backtest", name="fixture", created_at=gs.clock.now(), series_key="csv:rec",
                             params={"strategies": [{"id": record["id"], "version": record["version"],
                                                     "fingerprint": record["fingerprint"], "name": record["name"]}],
                                     "start": "2026-11-12T08:00:00Z", "end": "2026-11-13T19:30:00Z"},
                             total=1, fingerprint=gs.analysis_cfg.fingerprint)
        research_repo.set_status(con, run_id, "complete", at=gs.clock.now(), result={"comparison": [
            {"strategy": {"fingerprint": record["fingerprint"]}, "trades": 40, "net": 12.5, "win_rate": 0.5,
             "profit_factor": 1.1, "expectancy_r": 0.05, "sufficient": True, "max_drawdown": 30.0}]})


def monitor(c, record, mode="paper") -> dict:
    r = c.post("/api/live/monitor", json={"strategy_id": record["id"], "mode": mode})
    assert r.status_code == 201, r.text
    return r.json()["monitored"]


def run_cycle(c, at: datetime) -> None:
    c.clock.set(at)
    c.gs.monitor.cycle()


# ------------------------------------------------------------------------------------------------ eligibility
def test_only_phase3_validated_versions_can_be_monitored(lab):
    rec = ny_strategy(lab)
    refused = lab.post("/api/live/monitor", json={"strategy_id": rec["id"], "mode": "paper"})
    assert refused.status_code == 409 and "Strategy Lab" in refused.json()["detail"]
    mark_validated(lab.gs, rec)
    elig = lab.get(f"/api/live/eligibility/{rec['id']}").json()
    assert elig["eligible"] and elig["runs"][0]["id"] == "rr_fixture" and elig["runs"][0]["trades"] == 40
    m = monitor(lab, rec)
    assert m["mode"] == "paper" and m["account_id"] and m["fingerprint"] == rec["fingerprint"]
    again = lab.post("/api/live/monitor", json={"strategy_id": rec["id"]})
    assert again.status_code == 409 and "already monitored" in again.json()["detail"]
    acct = lab.gs.engine.summary(m["account_id"])
    assert acct["purpose"] == "validation" and acct["balance"] == "10000" and "SIMULATED" in acct["label"]
    assert lab.gs.engine.summary()["purpose"] == "manual"          # the user's own account stays the active one


# ------------------------------------------------------------------------------------------------ signal + trade + exits
def test_signal_simulated_trade_stop_exit_evaluation_and_journal(lab):
    rec = ny_strategy(lab)
    mark_validated(lab.gs, rec)
    m = monitor(lab, rec)
    quote(lab, "3878.30", "3878.51", at=CUT + timedelta(seconds=29))
    run_cycle(lab, CUT + timedelta(seconds=30))
    (sig,) = lab.get("/api/live/signals").json()["signals"]
    assert sig["strategy"] == {"id": rec["id"], "name": "NY session long", "version": 1, "fingerprint": rec["fingerprint"]}
    assert (sig["direction"], sig["timeframe"], sig["cutoff"], sig["status"]) == ("long", "M5", "2026-11-13T19:30:00Z", "opened")
    assert sig["supporting"][0]["evidence"].startswith("active:") and "New York" in sig["supporting"][0]["evidence"]
    assert isinstance(sig["conflicting"], list)
    kinds = [i["kind"] for i in sig["invalidation"]]
    assert kinds == ["stop", "time", "entry_delay"] and "3873.30" in sig["invalidation"][0]["evidence"]
    assert sig["price"]["bid"] == "3878.30" and sig["price"]["ask"] == "3878.51" and sig["price"]["spread"] == "0.21"
    assert sig["freshness"]["quote_age_seconds"] == 1.0 and sig["freshness"]["market_state"] == "LIVE"
    assert sig["source"]["series"] == "csv:rec" and "SIMULATED" in sig["label"]
    assert sig["plan"]["stop"] == "3873.30" and sig["plan"]["target"] == "3888.56" and sig["plan"]["lots"] == "0.02"
    # the SIMULATED trade lives in the version's own validation account
    (t,) = lab.get("/api/live/trades?status=open").json()["trades"]
    assert t["account_id"] == m["account_id"] and t["open_price"] == "3878.56" and t["stop_loss"] == "3873.30"
    assert t["entry"]["latency_from_cutoff_seconds"] == 30.0 and "SIMULATED" in t["label"]
    assert lab.gs.engine.summary()["open_positions"] == 0 and lab.gs.engine.summary(m["account_id"])["open_positions"] == 1
    # the same cut-off is never evaluated twice
    run_cycle(lab, CUT + timedelta(seconds=40))
    assert len(lab.get("/api/live/signals").json()["signals"]) == 1
    evals = lab.get(f"/api/live/monitor/{m['id']}/evaluations").json()
    assert evals["counts"] == {"setup": 1}
    # MFE / MAE from quotes: BUY uses the bid against the fill 3878.56
    quote(lab, "3880.30", "3880.51")
    lab.gs.monitor.on_quote_excursions(lab.clock.now())
    quote(lab, "3877.00", "3877.21")
    lab.gs.monitor.on_quote_excursions(lab.clock.now())
    # the paper simulator's stop loss closes the position; the monitor finalizes and evaluates it
    lab.clock.set(CUT + timedelta(minutes=1, seconds=1))
    quote(lab, "3873.00", "3873.21")
    closed = lab.gs.engine.on_quote()
    assert [p.close_reason for p in closed] == ["stop_loss"]
    run_cycle(lab, CUT + timedelta(minutes=1, seconds=2))
    detail = lab.get(f"/api/live/trades/{t['id']}").json()
    tr, ev = detail["trade"], detail["evaluation"]
    assert tr["status"] == "closed" and tr["mfe"] == "1.74"
    assert tr["mae"] == "5.56"          # the closing quote (bid 3873.00 = fill 3872.95 + slippage) counts too
    assert tr["exit"]["reason"] == "stop_loss" and tr["exit"]["holding_minutes"] == pytest.approx(0.52, abs=0.01)
    assert [a["n"] for a in ev["answers"]] == [1, 2, 3, 4, 5, 6, 7] and ev["summary"].startswith("LOSS")
    assert ev["answers"][5]["verdict"] == "yes" and "stop_loss" in ev["answers"][5]["answer"]
    assert "stopped out after" in " ".join(ev["went_wrong"]) and ev["definition"] == "eval@1"
    assert "one trade is not evidence" in ev["answers"][6]["answer"]
    # permanent: a second evaluation request does not replace it, and the repository offers no update
    lab.gs.monitor.evaluate(t["id"], lab.clock.now())
    with lab.gs.db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM trade_evaluations").fetchone()[0] == 1
    assert not [n for n in dir(live_repo) if "evaluation" in n and n.startswith(("update", "delete", "set_"))]
    # journal: filters by outcome / session / timeframe / date, CSV export
    (entry,) = lab.get("/api/live/journal").json()["entries"]
    assert entry["outcome"] == "loss" and entry["invalidated"] and entry["invalidated_how"] == "stop loss hit"
    assert entry["mfe"] == "1.74" and entry["mae"] == "5.56" and entry["costs"]["spread_at_fill"] == "0.21" and entry["label"] == "SIMULATED"
    assert entry["went_wrong"] and entry["market_conditions"]["sessions"] == entry["session"]
    for query, n in (("outcome=invalidated", 1), ("outcome=win", 0), ("session=New%20York", 1), ("session=Asian", 0),
                     ("timeframe=M5", 1), ("timeframe=M15", 0), ("start=2026-11-14T00:00:00Z", 0),
                     (f"strategy_id={rec['id']}", 1), ("direction=short", 0)):
        assert len(lab.get(f"/api/live/journal?{query}").json()["entries"]) == n, query
    assert "SIMULATED" in lab.get("/api/live/journal/export.csv").text
    perf = lab.get("/api/live/performance").json()["strategies"][0]
    assert perf["live"]["trades"] == 1 and perf["live"]["sufficient"] is False and perf["backtest"]["id"] == "rr_fixture"
    eq = lab.get("/api/live/equity").json()["lines"][0]
    assert eq["account_id"] == m["account_id"] and float(eq["points"][-1]["equity"]) < 10000


def test_time_exit_and_paused_monitor(lab):
    rec = ny_strategy(lab, hold=30)
    mark_validated(lab.gs, rec)
    m = monitor(lab, rec)
    quote(lab, "3878.30", "3878.51", at=CUT + timedelta(seconds=29))
    run_cycle(lab, CUT + timedelta(seconds=30))
    later = CUT + timedelta(minutes=31)
    quote(lab, "3879.00", "3879.21", at=later - timedelta(seconds=1))
    run_cycle(lab, later)
    (t,) = lab.get("/api/live/trades").json()["trades"]
    assert t["status"] == "closed" and t["close_reason"] == "time_exit" and t["exit"]["holding_minutes"] >= 30
    ev = lab.get(f"/api/live/trades/{t['id']}").json()["evaluation"]
    assert ev["answers"][5]["verdict"] == "yes"
    assert lab.patch(f"/api/live/monitor/{m['id']}", json={"status": "paused"}).json()["monitored"]["status"] == "paused"
    before = lab.get(f"/api/live/monitor/{m['id']}/evaluations").json()["counts"]
    run_cycle(lab, later + timedelta(minutes=10))
    assert lab.get(f"/api/live/monitor/{m['id']}/evaluations").json()["counts"] == before


def test_gates_skip_miss_and_block(lab):
    lab.clock.set(datetime(2026, 11, 13, 14, 20, tzinfo=UTC))
    rec = ny_strategy(lab)
    mark_validated(lab.gs, rec)
    m = monitor(lab, rec, mode="paper")
    evals = lambda: lab.get(f"/api/live/monitor/{m['id']}/evaluations").json()["evaluations"]  # noqa: E731
    cut = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
    # not LIVE -> skipped, no signal
    lab.box["state"] = (MarketState.DELAYED, "delayed provider")
    quote(lab, "3900.00", "3900.21", at=cut)
    run_cycle(lab, cut + timedelta(seconds=30))
    assert evals()[0]["outcome"] == "skipped" and "DELAYED" in evals()[0]["reason"]
    # LIVE but the quote is older than the limit -> skipped
    lab.box["state"] = (MarketState.LIVE, "ok")
    quote(lab, "3900.00", "3900.21", at=cut + timedelta(minutes=5) - timedelta(seconds=40))
    run_cycle(lab, cut + timedelta(minutes=5, seconds=10))
    assert evals()[0]["outcome"] == "skipped" and "old (limit 15 s)" in evals()[0]["reason"]
    assert lab.get("/api/live/signals").json()["signals"] == []
    # the candle for a cut-off is not stored: wait during the grace period, then 'missed'
    late = datetime(2026, 11, 13, 19, 40, tzinfo=UTC)
    run_cycle(lab, late + timedelta(seconds=30))
    assert len(evals()) == 2
    run_cycle(lab, late + timedelta(minutes=2, seconds=1))
    assert evals()[0]["outcome"] == "missed" and "not stored within 120 s" in evals()[0]["reason"]


def test_live_spread_above_the_strategy_limit_blocks_the_simulated_entry(lab):
    rec = ny_strategy(lab, extra=[{"id": "sp", "type": "spread", "max_spread": 0.5}])
    mark_validated(lab.gs, rec)
    monitor(lab, rec, mode="paper")
    quote(lab, "3878.30", "3879.10", at=CUT + timedelta(seconds=29))        # live spread 0.80, candle spread 0.21
    run_cycle(lab, CUT + timedelta(seconds=30))
    (sig,) = lab.get("/api/live/signals").json()["signals"]
    assert sig["status"] == "blocked" and "live spread 0.80 exceeds" in sig["status_reason"]
    assert lab.get("/api/live/trades").json()["trades"] == []
    (entry,) = lab.get("/api/live/journal?outcome=not_traded").json()["entries"]
    assert entry["signal_status"] == "blocked"


# ------------------------------------------------------------------------------------------------ outcomes and replica
def test_observed_signal_outcome_and_backtest_replica_use_only_later_candles(lab):
    lab.clock.set(datetime(2026, 11, 13, 14, 20, tzinfo=UTC))
    rec = ny_strategy(lab, hold=30)
    mark_validated(lab.gs, rec)
    monitor(lab, rec, mode="observe")
    cut = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
    quote(lab, "3900.00", "3900.21", at=cut + timedelta(seconds=29))
    run_cycle(lab, cut + timedelta(seconds=30))
    (sig,) = lab.get("/api/live/signals").json()["signals"]
    assert sig["status"] == "observed" and sig["outcome"] is None and lab.get("/api/live/trades").json()["trades"] == []
    quote(lab, "3901.00", "3901.21", at=cut + timedelta(minutes=33) - timedelta(seconds=1))
    run_cycle(lab, cut + timedelta(minutes=33))
    assert len(lab.get("/api/live/signals").json()["signals"]) == 1               # on_change: no repeat signal
    assert lab.get("/api/live/monitor").json()["monitored"][0]["evaluations"] == {"setup": 1, "repeat": 1}
    sig = lab.get(f"/api/live/signals/{sig['id']}").json()["signal"]
    oc = sig["outcome"]
    after = M1[(M1.index >= cut) & (M1.index < cut + timedelta(minutes=30))]
    ref = float(sig["plan"]["entry_estimate"])
    assert oc["status"] == "complete" and oc["bars"] == len(after) and oc["horizon_minutes"] == 30
    assert oc["mfe"] == pytest.approx(max(after["high"].max() - ref, 0), abs=1e-3)
    assert oc["mae"] == pytest.approx(max(ref - after["low"].min(), 0), abs=1e-3)
    rep = oc["replica"]["trade"]
    first = M1.loc[pd.Timestamp(cut)]
    assert rep["entry_time"] == "2026-11-13T14:30:00Z"
    assert rep["entry_price"] == pytest.approx(first["open"] + first["spread"] + 0.05, abs=1e-6)   # next open + spread + slip
    assert rep["exit_reason"] in ("stop", "target", "time")


# ------------------------------------------------------------------------------------------------ parity and no look-ahead
def test_live_snapshot_equals_the_backtest_snapshot_at_every_cutoff(lab):
    cfg = builder.validate(builder.templates()[0])
    row = pick_series(lab.gs.db, "csv:rec")
    step = Timeframe.M5
    start, end = datetime(2026, 11, 13, 15, 0, tzinfo=UTC), datetime(2026, 11, 13, 19, 30, tzinfo=UTC)
    ds = load_dataset(lab.gs.db, row, step, start, end, 5)
    cuts = features.step_cutoffs(ds, step, start, end)[::6]
    detail = builder.detail_timeframes(cfg)
    timeline = features.compute_timeline(ds, step, cuts, lab.gs.analysis_cfg, detail=detail)
    for cut, bt_snap in zip(cuts, timeline, strict=True):
        live_snap = lab.gs.monitor.snapshot_at(row, step, cut.to_pydatetime(), detail)
        assert live_snap == bt_snap, cut
        assert conditions.decide(cfg, live_snap) == conditions.decide(cfg, bt_snap)


def test_future_candles_never_change_a_live_snapshot(lab):
    row = pick_series(lab.gs.db, "csv:rec")
    detail = {"M5", "M15"}
    before = lab.gs.monitor.snapshot_at(row, Timeframe.M5, CUT - timedelta(minutes=30), detail)
    future = M1[M1.index >= CUT - timedelta(minutes=30)].copy()
    future[["open", "high", "low", "close"]] = future[["open", "high", "low", "close"]] * 1.05   # wildly different
    future.index = future.index + timedelta(hours=1)                                                # strictly after
    with lab.gs.db.transaction() as con:
        sid = con.execute("SELECT id FROM series WHERE key = 'csv:rec'").fetchone()[0]
        for ts, r in future.iterrows():
            con.execute("INSERT OR REPLACE INTO candles (series_id, tf, ts, o, h, l, c, tick_volume, spread, closed, "
                        "origin, updated_at) VALUES (?, 'M1', ?, ?, ?, ?, ?, 1, 21, 1, 'test', 'x')",
                        (sid, int(ts.timestamp()), *(int(round(r[k] * 100)) for k in ("open", "high", "low", "close"))))
    after = lab.gs.monitor.snapshot_at(row, Timeframe.M5, CUT - timedelta(minutes=30), detail)
    assert after == before


# ------------------------------------------------------------------------------------------------ migration
def test_migration_0006_preserves_paper_positions_and_extends_close_reasons(tmp_path, account_config, spec, costs, clock):
    from goldscalp.storage import db as db_mod
    staged = tmp_path / "mig"
    staged.mkdir()
    for p in sorted(db_mod.MIGRATIONS_DIR.glob("000[1-5]_*.sql")):
        (staged / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    assert Database(tmp_path / "m.sqlite3", migrations_dir=staged).migrate() == [1, 2, 3, 4, 5]   # schema as of Phase 3
    db = Database(tmp_path / "m.sqlite3")                                                          # the real migrations
    engine = PaperTradingEngine(db, clock, lambda: make_quote("4000.00", "4000.30"), spec, costs, account_config)
    # pre-0006 engine paths still work on the old schema (no purpose column yet)
    with db.transaction() as con:
        con.execute("INSERT INTO instrument_specs (fingerprint, spec_json, created_at) VALUES ('f', ?, '2026-11-11T15:00:00Z')",
                    (json.dumps(spec.to_mapping()),))
        con.execute("INSERT INTO paper_accounts (name, currency, starting_balance, leverage, instrument_spec_id, "
                    "cost_model_json, max_quote_age_seconds, allow_delayed_fills, active, created_at) "
                    "VALUES ('old', 'USD', '10000', 1, 1, ?, 30, 0, 1, '2026-11-11T15:00:00Z')", (json.dumps(costs.to_mapping()),))
        con.execute("INSERT INTO paper_positions (account_id, side, lots, ounces, open_price, open_time, open_quote_time, "
                    "open_price_state, open_spread, open_spread_source, open_slippage, open_commission, margin, status) "
                    "VALUES (1, 'buy', '0.01', '1', '4000', '2026-11-11T15:00:00Z', '2026-11-11T15:00:00Z', 'LIVE', '0.3', 'market', '0.05', '0.035', '4000', 'open')")
        con.execute("INSERT INTO account_ledger (account_id, ts, kind, amount, balance_after, position_id, note) "
                    "VALUES (1, '2026-11-11T15:00:00Z', 'commission', '-0.035', '9999.965', 1, 'n')")
    with db.read() as con:
        before = [tuple(r) for r in con.execute("SELECT * FROM paper_positions")]
    assert db.migrate() == [6, 7]          # 0006 (Phase 4) and 0007 (demo execution: new tables only)
    with db.read() as con:
        after = [tuple(r) for r in con.execute("SELECT id, account_id, side, lots, ounces, open_price, open_time, "
                                                "open_quote_time, open_price_state, open_spread, open_spread_source, "
                                                "open_slippage, open_commission, margin, stop_loss, take_profit, status, "
                                                "close_price, close_time, close_quote_time, close_price_state, "
                                                "close_commission, close_reason, realized_gross, realized_net, "
                                                "open_source, close_source, signal_id FROM paper_positions")]
        assert after == before and con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("SELECT purpose FROM paper_accounts").fetchone()[0] == "manual"
    with db.transaction() as con:
        con.execute("UPDATE paper_positions SET close_reason = 'time_exit' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE paper_positions SET close_reason = 'broker_close' WHERE id = 1")
    assert engine.summary()["account_id"] == 1


def test_validation_accounts_never_become_active(db, clock, spec, costs, account_config):
    engine = PaperTradingEngine(db, clock, lambda: make_quote("4000.00", "4000.30"), spec, costs, account_config)
    manual = engine.ensure_account()
    val = engine.create_account(account_config, spec, costs, purpose="validation")
    assert engine.ensure_account().id == manual.id and val.purpose == "validation"
    p = engine.open_position("buy", "0.01", account_id=val.id)
    assert engine.summary()["open_positions"] == 0 and engine.summary(val.id)["open_positions"] == 1
    with pytest.raises(Exception, match="does not exist on the active account"):
        engine.close_position(p.id)                                   # not the manual account's position
    closed = engine.close_position(p.id, account_id=val.id)
    assert closed.status == "closed" and Decimal(engine.summary(val.id)["balance"]) < Decimal("10000")



def test_selection_walk_forward_credits_only_the_folds_that_selected_this_version(lab):
    rec = ny_strategy(lab)
    gs = lab.gs
    with gs.db.transaction() as con:
        research_repo.create(con, "rr_wf", kind="walk_forward", name="wf", created_at=gs.clock.now(), series_key="csv:rec",
                             params={"strategies": [{"id": rec["id"], "version": 1, "fingerprint": rec["fingerprint"],
                                                     "name": rec["name"]}, {"id": 99, "version": 1, "fingerprint": "other",
                                                                            "name": "Other"}],
                                     "assumptions": {"min_trades_for_ratios": 30}},
                             total=1, fingerprint=gs.analysis_cfg.fingerprint)
        research_repo.set_status(con, "rr_wf", "complete", at=gs.clock.now(), result={
            "mode": "select", "out_of_sample": {"trades": 50, "net": 80.0, "sufficient": True},
            "folds": [{"selected": "Other", "evaluation": {"trades": 40, "net": 90.0}},
                      {"selected": rec["name"], "evaluation": {"trades": 10, "net": -10.0}}, {"selected": None}]})
    (run,) = lab.get(f"/api/live/eligibility/{rec['id']}").json()["runs"]
    assert (run["trades"], run["net"], run["sufficient"]) == (10, -10.0, False) and "1 of 3" in run["scope"]


def test_phase4_state_changing_routes_need_csrf_and_pages_are_served(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "c4.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with TestClient(app) as c:
        for path in ("/api/live/verifications", "/api/live/monitor", "/api/research/studies", "/api/proposals/x/approve",
                     "/api/proposals/x/reject", "/api/assistant/ask"):
            assert c.post(path, json={}).status_code == 403, path
        assert c.patch("/api/live/monitor/1", json={}).status_code == 403
        assert c.patch("/api/live/signals/x/notes", json={}).status_code == 403
        page = c.get("/live")
        assert page.status_code == 200 and "Live Validation" in page.text and "SIMULATED" in page.text
        assert page.headers["cache-control"] == "no-cache" and c.get("/static/js/live.js").headers["cache-control"] == "no-cache"
        for name in ("index", "paper", "replay", "performance", "lab"):
            assert 'href="/live"' in (ROOT_WEB / f"{name}.html").read_text(encoding="utf-8"), name


ROOT_WEB = __import__("pathlib").Path(__file__).resolve().parents[1] / "web"


def test_weekly_close_is_not_logged_as_missed_candles(lab):
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    lab.clock.set(saturday)
    rec = ny_strategy(lab)
    mark_validated(lab.gs, rec)
    m = monitor(lab, rec)
    lab.box["state"] = (MarketState.HISTORICAL, "market closed (weekly close)")
    for minutes in (5.5, 8, 10.5, 20, 45):
        run_cycle(lab, saturday + timedelta(minutes=minutes))
    assert lab.get(f"/api/live/monitor/{m['id']}/evaluations").json()["evaluations"] == []
