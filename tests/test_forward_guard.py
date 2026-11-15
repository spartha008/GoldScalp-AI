"""Forward-data guard (forward-guard@1): research may not read candles from 2026-11-14T00:00:00Z onward.

Covers historical requests, the exact boundary, ranges crossing it, November 2026 requests, dynamic presets (an
improvement study computed from the stored coverage), direct API calls, the background-job layer (the loaders and the
monthly runner), raw monitoring access, and that only a registered, versioned protocol could unlock. SYNTHETIC data:
the recorded test candles, plus a copy shifted four days forward (Mon 16 - Tue 17 Nov) into the reserved period."""
# ruff: noqa: F811 - pytest fixtures (world) are shared from the Phase 5 test module
from __future__ import annotations

import ast
import dataclasses
import json
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from goldscalp.api import research_data
from goldscalp.api.app import create_app
from goldscalp.api.services import pick_series
from goldscalp.core import forward
from goldscalp.core.clock import FixedClock
from goldscalp.core.config import PROJECT_ROOT, Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import discovery, runner
from goldscalp.monthly.standalone import create_research_app
from goldscalp.strategies import builder
from tests.conftest import recorded
from tests.test_phase3_api import csv_of, wait
from tests.test_phase5_monthly import world  # noqa: F401 - shared fixture

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

B = forward.FORWARD_FROM
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ the boundary itself
def test_boundary_and_canonical_convention():
    assert B == datetime(2026, 11, 14, tzinfo=UTC) and forward.FORWARD_FROM_TS == 1794614400
    assert discovery.FORWARD_FROM == B                               # the frozen Phase 6 boundary agrees
    assert not forward.includes_forward(t("2026-10-01T00:00Z"), t("2026-11-01T00:00Z"))       # historical
    assert not forward.includes_forward(t("2026-11-13T00:00Z"), B)                            # ends exactly at it
    assert forward.includes_forward(t("2026-11-13T00:00Z"), B + timedelta(minutes=1))         # crosses it
    assert forward.includes_forward(t("2026-11-01T00:00Z"), t("2026-12-01T00:00Z"))           # November 2026
    assert forward.includes_forward(B, B + timedelta(hours=1))                                # inside it


def test_check_range_refuses_and_never_truncates():
    forward.check_range(t("2026-06-01T00:00Z"), B, "historical")                              # no error
    with pytest.raises(forward.ForwardDataLocked, match="never shortened"):
        forward.check_range(t("2026-11-10T00:00Z"), t("2026-11-20T00:00Z"), "crossing request")
    with pytest.raises(forward.ForwardDataLocked):
        forward.check_range(None, t("2026-11-14T00:05Z"), "dynamic preset")


def test_check_index_catches_any_forward_candle():
    ok = pd.DatetimeIndex([t("2026-11-13T23:59Z")])
    forward.check_index(ok, "historical frame")
    with pytest.raises(forward.ForwardDataLocked, match="forward-period candles"):
        forward.check_index(pd.DatetimeIndex([t("2026-11-13T23:59Z"), B]), "frame with one forward candle")
    forward.check_index(pd.DatetimeIndex([]), "empty frame")


def test_guard_cannot_be_switched_off_by_settings_or_environment(monkeypatch):
    names = [f.name for f in dataclasses.fields(Settings)] if dataclasses.is_dataclass(Settings) else list(
        getattr(Settings, "model_fields", {}))
    assert not any("forward" in n or "guard" in n for n in names)
    for var in ("GOLDSCALP_FORWARD_GUARD", "GOLDSCALP_FORWARD_FROM", "FORWARD_GUARD"):
        monkeypatch.setenv(var, "off")
    with pytest.raises(forward.ForwardDataLocked):
        forward.check_range(t("2026-11-10T00:00Z"), t("2026-11-20T00:00Z"), "request")


def _register(d: Path, doc: dict, *, tamper: bool = False) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['id']}.json").write_text(json.dumps(doc), encoding="utf-8")
    reg = {"definition": forward.UNLOCK_DEFINITION,
           "protocols": {doc["id"]: {"version": doc["version"], "sha256": forward._body_hash(doc)}}}
    (d / "REGISTRY.json").write_text(json.dumps(reg), encoding="utf-8")
    if tamper:
        (d / f"{doc['id']}.json").write_text(json.dumps({**doc, "purposes": ["anything"]}), encoding="utf-8")


def test_only_a_registered_versioned_protocol_unlocks(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "UNLOCK_DIR", tmp_path / "unlock")
    req = (t("2026-11-10T00:00Z"), t("2026-12-01T00:00Z"))
    with pytest.raises(forward.ForwardDataLocked, match="not registered"):
        forward.check_range(*req, "request", protocol_id="p7-confirm")
    doc = {"definition": forward.UNLOCK_DEFINITION, "id": "p7-confirm", "version": 1, "registered_at": "2027-01-01",
           "scope": {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}, "purposes": ["confirmation"]}
    _register(tmp_path / "unlock", doc)
    forward.check_range(*req, "request", purpose="confirmation", protocol_id="p7-confirm")       # inside its scope
    with pytest.raises(forward.ForwardDataLocked, match="does not cover"):
        forward.check_range(*req, "request", purpose="development", protocol_id="p7-confirm")
    with pytest.raises(forward.ForwardDataLocked, match="outside"):
        forward.check_range(req[0], t("2027-01-01T00:00Z"), "request", purpose="confirmation", protocol_id="p7-confirm")
    _register(tmp_path / "unlock", doc, tamper=True)
    with pytest.raises(forward.ForwardDataLocked, match="changed after it was registered"):
        forward.check_range(*req, "request", purpose="confirmation", protocol_id="p7-confirm")
    assert forward.status()["locked"] is True


def test_no_unlock_protocol_is_registered_in_the_project():
    assert forward.status(forward.UNLOCK_DIR)["registered_unlock_protocols"] == []


def test_research_routers_only_read_candles_through_the_guarded_loaders():
    for name in ("research", "performance", "improvement"):
        src = (PROJECT_ROOT / "goldscalp" / "api" / "routers" / f"{name}.py").read_text(encoding="utf-8")
        calls = {n.func.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"load_dataset", "load_outcome_candles"}, name
        assert calls & {"research_dataset", "research_outcome_candles"}, name
    runner_src = (PROJECT_ROOT / "goldscalp" / "monthly" / "runner.py").read_text(encoding="utf-8")
    assert "forward.check_range(start, end" in runner_src and "forward.check_dataset(ds" in runner_src


# ------------------------------------------------------------------ main application (Strategy Lab, Performance, studies)
@pytest.fixture
def app_client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "fg.sqlite3", live_provider="none", max_import_mb=5),
                     clock=FixedClock(NOW), provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        m1 = recorded("mt5_xauusd_m1_recorded.csv")
        forward_m1 = m1.copy()
        forward_m1.index = forward_m1.index + pd.Timedelta(days=4)          # 16 Nov 08:00 - 17 Nov 19:29: reserved
        for name, frame, tf in (("h1.csv", recorded("mt5_xauusd_h1_recorded.csv"), "H1"), ("m1.csv", m1, "M1"),
                                ("m1_forward.csv", forward_m1, "M1")):
            r = c.post("/api/data/import", files={"file": (name, csv_of(frame), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": tf, "time_basis": "utc", "series_label": "fg",
                             "candle_side": "bid", "price_decimals": "2"})
            assert r.status_code == 200, r.text
        c.ids = [c.post("/api/strategies", json=x).json()["id"] for x in builder.templates()]
        yield c


def _lab(c, start: str, end: str, path: str = "/api/research/backtests"):
    ids = c.ids if path.endswith("comparisons") else c.ids[:1]
    return c.post(path, json={"series": "csv:fg", "start": start, "end": end, "strategies": [{"id": i} for i in ids]})


def test_strategy_lab_accepts_history_and_the_exact_boundary(app_client):
    for path, start, end in (("/api/research/backtests", "2026-11-13T06:00:00Z", "2026-11-13T17:00:00Z"),   # history
                             ("/api/research/comparisons", "2026-11-12T09:00:00Z", "2026-11-14T00:00:00Z")):  # boundary
        r = _lab(app_client, start, end, path)
        assert r.status_code == 202, r.text
        assert wait(app_client, r.json()["run"]["id"])["status"] == "complete"


@pytest.mark.parametrize("path", ["/api/research/backtests", "/api/research/comparisons", "/api/research/walk-forward"])
def test_strategy_lab_refuses_crossing_and_forward_ranges(app_client, path):
    for start, end in (("2026-11-13T06:00:00Z", "2026-11-14T12:00:00Z"),       # crosses the boundary
                       ("2026-11-16T08:00:00Z", "2026-11-17T12:00:00Z"),       # entirely forward
                       ("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z")):      # November 2026
        r = _lab(app_client, start, end, path)
        assert r.status_code == 409 and "forward-period" in r.json()["detail"], (path, start, r.text)
        assert r.json()["forward_from"] == "2026-11-14T00:00:00Z"


def test_performance_refuses_forward_and_accepts_history(app_client):
    body = {"series": "csv:fg", "timeframes": ["M15"], "start": "2026-11-12T11:00:00Z", "end": "2026-11-13T17:00:00Z"}
    r = app_client.post("/api/performance/runs", json={**body, "end": "2026-11-16T12:00:00Z"})
    assert r.status_code == 409 and "Performance run" in r.json()["detail"]
    ok = app_client.post("/api/performance/runs", json=body)
    assert ok.status_code == 202, ok.text
    rid = ok.json()["run"]["id"]
    for _ in range(2400):
        if app_client.get(f"/api/performance/runs/{rid}").json()["run"]["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)


def test_improvement_study_refuses_a_preset_that_reaches_forward_data(app_client):
    r = app_client.post("/api/research/studies", json={"strategy_id": app_client.ids[0], "series": "csv:fg",
                                                       "use_live_evidence": False})
    assert r.status_code == 409 and "Improvement study" in r.json()["detail"], r.text


def test_raw_monitoring_still_reads_forward_candles(app_client):
    r = app_client.get("/api/market/candles", params={"tf": "M1", "series": "csv:fg", "limit": 5000})
    assert r.status_code == 200
    times = [c["time"] for c in r.json()["data"]["candles"]]
    assert max(times) >= forward.FORWARD_FROM_TS                             # charts see the reserved period
    s = app_client.get("/api/research/forward-guard").json()
    assert s["locked"] and s["forward_from"] == "2026-11-14T00:00:00Z"


def test_loaders_refuse_forward_reads_even_when_called_directly(app_client):
    gs = app_client.app.state.gs
    row = pick_series(gs.db, "csv:fg")
    with pytest.raises(forward.ForwardDataLocked):
        research_data.research_dataset(gs.db, row, Timeframe.M5, t("2026-11-13T00:00Z"), t("2026-11-15T00:00Z"), 5,
                                       what="direct call")
    with pytest.raises(forward.ForwardDataLocked, match="manage or score"):
        research_data.research_outcome_candles(gs.db, row, Timeframe.M1, t("2026-11-13T00:00Z"), t("2026-11-16T10:00Z"),
                                               what="direct call")       # an extension reaching forward candles


# ------------------------------------------------------------------ research service (Monthly Backtest) and its jobs
@pytest.fixture
def research_client(world, tmp_path):
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(datetime(2026, 12, 15, tzinfo=UTC)),
                              app_db_path=world["app"], store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def test_monthly_backtest_refuses_november_2026_and_later(research_client):
    for month in ("2026-11", "2026-12"):
        r = research_client.post("/api/monthly/evaluations", json={"month": month})
        assert r.status_code in (409, 422)
        if month == "2026-11":
            assert r.status_code == 409 and "forward-period" in r.json()["detail"], r.text
    r = research_client.post("/api/monthly/robustness-studies", json={"end": "2026-12-01T00:00:00Z"})
    assert r.status_code == 409 and "Robustness study" in r.json()["detail"], r.text
    assert research_client.get("/api/monthly/forward-guard").json()["locked"] is True


def test_background_research_jobs_cannot_read_forward_data(research_client):
    ms = research_client.app.state.monthly
    row = ms.sources.series_row("app", "live:mt5:XAUUSD")
    with pytest.raises(forward.ForwardDataLocked):
        runner.prepare(ms.sources, "app", row, Timeframe.M5, t("2026-11-10T00:00Z"), t("2026-11-20T00:00Z"),
                       lookback_days=5, detail=set(), cfg=ms.analysis_cfg)
