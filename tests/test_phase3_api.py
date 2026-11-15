"""Phase 3 API and integration: Strategy Builder routes and versioning, research runs (backtest, comparison,
walk-forward) with exports, the coverage-aware dataset loader, the live strategy monitor and USER-INITIATED
simulated paper entries with the strategy journal, and the safety boundaries."""
from __future__ import annotations

import ast
import re
import time
import warnings
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path

import pytest

from goldscalp.analysis.config import default_config
from goldscalp.api.app import create_app
from goldscalp.api.routers import strategies as strategies_router
from goldscalp.api.services import choose_base, load_dataset
from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.strategies import builder
from tests.conftest import make_quote, recorded

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 13, 19, 30, 30, tzinfo=UTC)          # just after the recorded data's last close


def csv_of(frame) -> bytes:
    lines = ["timestamp,open,high,low,close,tick_volume,spread"]
    for ts, r in frame.iterrows():
        lines.append(f"{ts.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['tick_volume']},{r['spread']}")
    return "\n".join(lines).encode()


@pytest.fixture
def client(tmp_path, account_config):
    app = create_app(Settings(db_path=tmp_path / "p3.sqlite3", live_provider="none", max_import_mb=5),
                     clock=FixedClock(NOW), provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        for name, tf in (("mt5_xauusd_h1_recorded.csv", "H1"), ("mt5_xauusd_m1_recorded.csv", "M1")):
            r = c.post("/api/data/import", files={"file": (name, csv_of(recorded(name)), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": tf, "time_basis": "utc", "series_label": "rec",
                             "candle_side": "bid", "price_decimals": "2"})
            assert r.status_code == 200, r.text
        c.account_config = account_config
        yield c


def wait(c, run_id, seconds=240):
    for _ in range(seconds * 10):
        run = c.get(f"/api/research/runs/{run_id}").json()["run"]
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def make_strategies(c):
    return [c.post("/api/strategies", json=t).json()["id"] for t in builder.templates()]


# ------------------------------------------------------------------------------- strategy builder routes
def test_strategy_crud_versions_export_and_import(client):
    cat = client.get("/api/strategies/catalogue").json()
    assert set(cat["conditions"]) >= {"structure_state", "structure_event", "swing_sequence", "liquidity_sweep", "fvg",
                                      "order_block", "htf_alignment", "session", "volatility_regime", "atr_range",
                                      "spread", "signal_agrees"}
    assert len(cat["templates"]) == 3 and "not claims of profitable" in cat["note"]
    created = client.post("/api/strategies", json={**cat["templates"][0], "note": "first"})
    assert created.status_code == 201
    sid = created.json()["id"]
    same = client.put(f"/api/strategies/{sid}", json=cat["templates"][0]).json()
    assert same["version"] == 1 and same["created_version"] is False
    changed = dict(cat["templates"][0], exit={"max_holding_minutes": 90})
    v2 = client.put(f"/api/strategies/{sid}", json=changed).json()
    assert v2["version"] == 2 and v2["fingerprint"] != created.json()["fingerprint"]
    assert [v["version"] for v in client.get(f"/api/strategies/{sid}/versions").json()["versions"]] == [2, 1]
    assert client.get(f"/api/strategies/{sid}?version=1").json()["config"]["exit"]["max_holding_minutes"] == 60
    exported = client.get(f"/api/strategies/{sid}/export.json")
    assert "attachment" in exported.headers["content-disposition"] and exported.json()["version"] == 2
    imported = client.post("/api/strategies/import", json=exported.json()).json()
    assert imported["id"] != sid and imported["fingerprint"] == v2["fingerprint"]
    assert client.post("/api/strategies", json={"name": "x", "conditions": [{"type": "magic"}]}).status_code == 422
    assert client.post("/api/strategies/validate", json=cat["templates"][1]).json()["valid"] is True
    assert client.delete(f"/api/strategies/{sid}").status_code == 200
    assert sid not in [s["id"] for s in client.get("/api/strategies").json()["strategies"]]
    assert client.get(f"/api/strategies/{sid}").json()["archived"] is True          # versions are kept


# ------------------------------------------------------------------------------- research runs
def test_backtest_comparison_walk_forward_and_exports(client):
    ids = make_strategies(client)
    base = {"series": "csv:rec", "start": "2026-11-13T06:00:00Z", "end": "2026-11-13T17:00:00Z",
            "assumptions": {"min_trades_for_ratios": 3}}
    bt = client.post("/api/research/backtests", json={**base, "strategies": [{"id": ids[2]}]})
    assert bt.status_code == 202, bt.text
    run = wait(client, bt.json()["run"]["id"])
    assert run["status"] == "complete", run["error"]
    s = run["result"]["strategies"][0]
    assert s["strategy"]["id"] == ids[2] and s["strategy"]["version"] == 1 and len(s["result_hash"]) == 64
    assert run["result"]["coverage"]["base_tf"] == "M1" and run["result"]["limitations"]
    assert s["metrics"]["summary"]["trades"] == len(s["trades"])
    comp = client.post("/api/research/comparisons", json={**base, "strategies": [{"id": i} for i in ids], "name": "cmp"})
    run_c = wait(client, comp.json()["run"]["id"])
    assert run_c["status"] == "complete" and len(run_c["result"]["comparison"]) == 3
    assert {c["steps"] for c in run_c["result"]["comparison"]} == {s["steps"]}              # identical timeline
    again = [x for x in run_c["result"]["strategies"] if x["strategy"]["id"] == ids[2]][0]
    assert again["result_hash"] == s["result_hash"]                                           # reproducible
    for path, kind in (("trades.csv", "text/csv"), ("summary.csv", "text/csv"), ("report.pdf", "application/pdf")):
        r = client.get(f"/api/research/runs/{run_c['id']}/{path}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and "attachment" in r.headers["content-disposition"]
    assert client.get(f"/api/research/runs/{run_c['id']}/trades.csv").text.startswith("# report: GoldScalp AI comparison")
    assert client.patch(f"/api/research/runs/{run_c['id']}", json={"saved": True, "name": "kept"}).json()["run"]["saved"]
    assert [r["id"] for r in client.get("/api/research/runs?saved=true").json()["runs"]] == [run_c["id"]]
    wf = client.post("/api/research/walk-forward", json={**base, "strategies": [{"id": i} for i in ids], "mode": "select",
                                                          "objective": "net", "min_dev_trades": 1, "dev_days": 0.2,
                                                          "eval_days": 0.1})
    assert wf.status_code == 202, wf.text
    run_w = wait(client, wf.json()["run"]["id"])
    assert run_w["status"] == "complete", run_w["error"]
    res = run_w["result"]
    assert res["folds"] and res["variation"]["folds"] == len(res["folds"]) == run_w["params"]["folds"]
    assert "development windows only" in res["note"]
    assert client.get(f"/api/research/runs/{run_w['id']}/summary.csv").text.count("\nfold,") == len(res["folds"])
    assert client.delete(f"/api/research/runs/{run_w['id']}").status_code == 200


def test_research_refusals(client):
    ids = make_strategies(client)
    base = {"series": "csv:rec", "start": "2026-11-13T06:00:00Z", "end": "2026-11-13T17:00:00Z"}
    assert client.post("/api/research/backtests", json={**base, "strategies": [{"id": 999}]}).status_code == 404
    assert client.post("/api/research/backtests", json={**base, "strategies": [{"id": ids[0]}, {"id": ids[1]}]}).status_code == 422
    assert client.post("/api/research/comparisons", json={**base, "strategies": [{"id": ids[0]}]}).status_code == 422
    assert client.post("/api/research/backtests", json={**base, "end": base["start"], "strategies": [{"id": ids[0]}]}).status_code == 422
    assert client.post("/api/research/walk-forward", json={**base, "strategies": [{"id": ids[0]}, {"id": ids[1]}],
                                                           "mode": "fixed"}).status_code == 422
    assert client.post("/api/research/walk-forward", json={**base, "strategies": [{"id": ids[0]}], "dev_days": 30}).status_code == 422
    assert client.post("/api/research/backtests", json={**base, "strategies": [{"id": ids[0]}],
                                                        "assumptions": {"intrabar": "random"}}).status_code == 422
    assert client.get("/api/research/runs/rr_nope").status_code == 404
    assert client.post("/api/research/runs/rr_nope/cancel").status_code == 409


def test_dataset_loader_uses_the_finest_timeframe_that_covers_the_start(client):
    gs = client.app.state.gs
    with gs.db.read() as con:
        row = con.execute("SELECT * FROM series WHERE key = 'csv:rec'").fetchone()
    tf, note = choose_base(gs.db, row, Timeframe.M5, datetime(2026, 11, 13, 6, tzinfo=UTC), NOW)
    assert tf is Timeframe.M1 and "cover" in note
    early = datetime(2026, 11, 5, tzinfo=UTC)                                      # before M1 coverage, H1 only
    tf, note = choose_base(gs.db, row, Timeframe.H1, early, NOW)
    assert tf is Timeframe.H1
    ds = load_dataset(gs.db, row, Timeframe.M5, early, NOW, 5)
    assert ds.base_tf is Timeframe.M1 and "no timeframe covers the window start" in ds.note   # reported, not silent


# ------------------------------------------------------------------------------- live monitor and paper journal
def live_ready(client, monkeypatch, bid="4000.00", ask="4000.20"):
    gs = client.app.state.gs
    quote = make_quote(bid, ask, at=NOW)
    monkeypatch.setattr(strategies_router, "live_context", lambda gs_, row: ("LIVE", 0.2, "provider bid/ask"))
    monkeypatch.setattr(gs.market, "market_quote", lambda: quote)
    gs.engine = PaperTradingEngine(gs.db, gs.clock, lambda: quote, gs.spec, gs.paper_costs, client.account_config)
    gs.engine.ensure_account()
    return gs


def session_strategy(client):
    cfg = {"name": "NY session long", "direction": "long", "evaluation": {"step_timeframe": "M5"},
           "conditions": [{"id": "ny", "type": "session", "sessions": ["New York"]}],
           "exit": {"stop": {"type": "fixed", "distance": 5}, "target": {"type": "fixed", "distance": 10},
                    "max_holding_minutes": 30}, "risk": {"lots": "0.02"}}
    return client.post("/api/strategies", json=cfg).json()["id"]


def test_live_monitor_requires_live_data(client):
    sid = session_strategy(client)
    out = client.get(f"/api/strategies/{sid}/live?series=csv:rec").json()
    assert out["setup"] is None and any("not LIVE" in g for g in out["gates"])
    refused = client.post(f"/api/strategies/{sid}/paper")
    assert refused.status_code == 409 and "no live strategy setup" in refused.json()["detail"]


def test_user_initiated_simulated_entry_is_journaled_with_its_outcome(client, monkeypatch):
    gs = live_ready(client, monkeypatch)
    monkeypatch.setattr(strategies_router, "active_series_key", lambda gs_: "csv:rec")
    sid = session_strategy(client)
    live = client.get(f"/api/strategies/{sid}/live").json()
    assert live["setup"] == "long" and live["gates"] == [] and live["plan"]["stop"] == "3995.00"
    assert live["plan"]["target"] == "4010.20" and live["plan"]["lots"] == "0.02"         # target from the ask
    opened = client.post(f"/api/strategies/{sid}/paper")
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert "SIMULATED" in body["label"]
    (entry,) = client.get("/api/journal").json()["entries"]
    assert (entry["strategy_id"], entry["position_id"], entry["status"]) == (sid, body["position_id"], "open")
    assert entry["planned"]["stop"] == "3995.00" and entry["planned"]["max_holding_minutes"] == 30
    assert Decimal(entry["entry_costs"]["commission"]) == Decimal("0.07") and entry["entry_costs"]["price_state"] == "LIVE"
    assert entry["conditions"][0]["passed"] is True and entry["signal_time"] == "2026-11-13T19:30:30Z"
    pos = [p for p in client.get("/api/sim/positions?status=open").json()["data"] if p["id"] == body["position_id"]][0]
    assert pos["stop_loss"] == "3995.00" and pos["take_profit"] == "4010.20"
    gs.engine = PaperTradingEngine(gs.db, gs.clock, lambda: make_quote("3994.00", "3994.20", at=NOW), gs.spec,
                                   gs.paper_costs, client.account_config)
    gs.engine.on_quote()                                                               # the paper SL triggers
    (closed,) = client.get("/api/journal").json()["entries"]
    assert closed["status"] == "closed" and closed["close_reason"] == "stop_loss"
    assert closed["invalidation_reason"].startswith("invalidated: stop loss at 3995.00") and float(closed["net_pnl"]) < 0
    csv_text = client.get("/api/journal/export.csv").text
    assert "SIMULATED paper positions" in csv_text and "stop_loss" in csv_text


def test_holding_time_exceeded_is_flagged(client, monkeypatch):
    live_ready(client, monkeypatch)
    monkeypatch.setattr(strategies_router, "active_series_key", lambda gs_: "csv:rec")
    sid = session_strategy(client)
    assert client.post(f"/api/strategies/{sid}/paper").status_code == 201
    client.app.state.gs.clock = FixedClock(NOW + timedelta(minutes=45))
    (entry,) = client.get("/api/journal").json()["entries"]
    assert entry["holding_exceeded"] and "planned 30 minutes" in entry["note"]


# ------------------------------------------------------------------------------- safety boundaries
def test_research_never_executes_orders_or_touches_paper_tables():
    for folder in ("backtesting", "strategies", "reports"):
        for path in (ROOT / "goldscalp" / folder).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            assert not {m for m in mods if m.startswith(("goldscalp.storage", "goldscalp.simulation.portfolio",
                                                         "goldscalp.data.providers", "goldscalp.data.live",
                                                         "goldscalp.data.hub", "goldscalp.api", "httpx", "MetaTrader5"))}, path.name
            assert not re.search(r"\.open_position\(", text) and "paper_positions" not in text, path.name


def test_only_the_user_initiated_route_opens_simulated_positions():
    callers = [p.name for p in (ROOT / "goldscalp").rglob("*.py") if "open_position(" in p.read_text(encoding="utf-8")
               and p.name not in ("portfolio.py",)]
    # the paper ticket, the strategy paper route and (Phase 4) the live monitor for OPTED-IN validation accounts
    assert sorted(callers) == ["monitor.py", "simulation.py", "strategies.py"]
    monitor_src = (ROOT / "goldscalp" / "live" / "monitor.py").read_text(encoding="utf-8")
    calls = re.findall(r"open_position\([^)]*\)", monitor_src, re.S)
    assert calls and all("account_id=account_id" in c for c in calls)  # never the user's manual account


def test_new_state_changing_routes_need_csrf(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "c.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with TestClient(app) as c:
        for path in ("/api/strategies", "/api/strategies/import", "/api/strategies/1/paper", "/api/research/backtests",
                     "/api/research/comparisons", "/api/research/walk-forward", "/api/research/runs/x/cancel"):
            assert c.post(path, json={}).status_code == 403, path
        assert c.put("/api/strategies/1", json={}).status_code == 403
        assert c.delete("/api/research/runs/x").status_code == 403
        assert c.patch("/api/research/runs/x", json={}).status_code == 403


def test_interrupted_research_runs_are_marked_failed(tmp_path):
    from goldscalp.storage import performance_repo, research_repo
    from goldscalp.storage.db import Database
    db = Database(tmp_path / "i.sqlite3")
    db.migrate()
    with db.transaction() as con:
        performance_repo.save_config(con, default_config().fingerprint, default_config().data, NOW)
        research_repo.create(con, "rr_old", kind="backtest", name=None, created_at=NOW, series_key="s", params={},
                             total=3, fingerprint=default_config().fingerprint)
    create_app(Settings(db_path=tmp_path / "i.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with db.read() as con:
        assert research_repo.get(con, "rr_old")["status"] == "failed"


def test_lab_page_and_navigation(client):
    assert client.get("/lab").status_code == 200
    for page in ("/", "/paper", "/replay", "/performance", "/lab"):
        assert 'href="/lab"' in client.get(page).text
