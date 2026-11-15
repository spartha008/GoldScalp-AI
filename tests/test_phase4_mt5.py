"""Phase 4 Vantage MT5 integration, MOCKED (tests.fake_mt5 - no terminal, no broker, no network).

These tests prove the LOGIC: broker identification, demo detection without account data, symbol discovery,
ticks, all four timeframes, candle integrity, disconnect/reconnect, feed health and the verification verdicts.
They can never produce an actual-terminal verification: the record names the fake adapter, which caps the
verdict at PARTIAL. Actual verification is the stored POST /api/live/verifications run against the user's
terminal (see PHASE_4_IMPLEMENTATION_REPORT.md)."""
from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.core.kinds import MarketState
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import integrity, mt5_verify
from goldscalp.data.providers.mt5_identity import broker_identity, classify_server, environment, read_server_name
from goldscalp.data.providers.mt5_provider import MT5Provider, ReadOnlyMT5
from goldscalp.data.resample import resample
from goldscalp.live.feed import FeedMonitor
from tests.conftest import make_quote, synthetic_m1
from tests.fake_mt5 import FakeMT5, symbol

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

NOW = datetime(2026, 11, 13, 19, 30, 30, tzinfo=UTC)      # Friday, market open
LOGIN = "87654321"
VANTAGE = "Vantage Global Prime Pty Ltd"


def write_ini(folder, server: str, *, utf16: bool = True) -> None:
    cfg = folder / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    text = f"[Common]\r\nLogin={LOGIN}\r\nServer={server}\r\nProxyEnable=0\r\n[Charts]\r\nMaxBars=100000\r\n"
    (cfg / "common.ini").write_bytes(text.encode("utf-16") if utf16 else text.encode("utf-8"))


def make_server_folder(data, server, *, age_seconds=0.0):
    """bases/<server>/history/... as the terminal keeps it (plus the non-server folders it always has)."""
    for name in ("Default", "Custom", "signals"):
        (data / "bases" / name).mkdir(parents=True, exist_ok=True)
    cache = data / "bases" / server / "history" / "XAUUSD" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / "M1.hc"
    f.write_bytes(b"x")
    t = time.time() - age_seconds
    os.utime(f, (t, t))


def terminal(tmp_path, schedule, *, company=VANTAGE, server="VantageInternational-Demo", symbols=None, **kw) -> FakeMT5:
    data = tmp_path / "MetaQuotes" / "Terminal" / "ABC123"
    write_ini(data, server)
    make_server_folder(data, server)
    fake = FakeMT5(company=company, data_path=str(data),
                   symbols=symbols or [symbol("XAUUSD+"), symbol("XAUUSD.r"), symbol("XAUEUR", profit="EUR")], **kw)
    m1 = synthetic_m1("2026-11-09T01:00:00Z", "2026-11-13T19:30:00Z", schedule, seed=11, price=3900.0)
    bars = {Timeframe.M1: m1}
    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
        bars[tf] = resample(m1, Timeframe.M1, tf)
    for name in ("XAUUSD+", "XAUUSD.r"):
        for tf, frame in bars.items():
            fake.set_rates(name, getattr(fake, f"TIMEFRAME_{tf.value}"),
                           [(ts.to_pydatetime(), r.open, r.high, r.low, r.close) for ts, r in frame.iterrows()],
                           spread_points=21)
        fake.set_tick(name, NOW - timedelta(seconds=1), 3901.10, 3901.31)
    return fake


def provider_for(fake: FakeMT5, clock=None, running=True) -> MT5Provider:
    return MT5Provider(clock=clock or FixedClock(NOW), module_loader=lambda: ReadOnlyMT5(fake),
                       process_check=lambda image: running)


def verify(provider, schedule, clock=None):
    return asyncio.run(mt5_verify.run(provider, clock=clock or FixedClock(NOW), schedule=schedule))


# ------------------------------------------------------------------------------------------------ identity
def test_vantage_is_recognised_from_the_terminal_company_only():
    assert broker_identity("Vantage Global Prime Pty Ltd")["is_vantage"]
    assert broker_identity("Vantage International Group Limited")["is_vantage"]
    assert not broker_identity("Advantage Markets Ltd")["is_vantage"]          # whole word, not a substring
    assert not broker_identity("Demo Broker Ltd")["is_vantage"]
    assert broker_identity(None) == {"company": None, "is_vantage": False,
                                     "evidence": "the terminal reported no company name (not connected to a broker yet?)"}


@pytest.mark.parametrize("utf16", [True, False])
def test_demo_environment_reads_only_the_server_line(tmp_path, utf16):
    write_ini(tmp_path, "VantageInternational-Demo", utf16=utf16)
    out = environment(str(tmp_path))
    assert out["environment"] == "DEMO" and out["server"] == "VantageInternational-Demo"
    assert LOGIN not in json.dumps(out) and "most recent login" in out["evidence"]
    assert read_server_name(tmp_path / "config" / "common.ini") == "VantageInternational-Demo"


def test_environment_classification_and_failures(tmp_path):
    assert classify_server("VantageInternational-Live 3")[0] == "LIVE"
    assert classify_server("VantageInternational-Real")[0] == "LIVE"
    assert classify_server("Vantage-Server7")[0] == "UNKNOWN"
    assert environment(str(tmp_path / "missing"))["environment"] == "UNKNOWN"
    assert environment(None)["environment"] == "UNKNOWN"
    write_ini(tmp_path, "VantageInternational-Demo")
    assert environment(str(tmp_path), enabled=False)["environment"] == "UNKNOWN"
    write_ini(tmp_path, "x" * 200 + "<script>")
    assert environment(str(tmp_path))["server"] is None                            # implausible values are dropped


# ------------------------------------------------------------------------------------------------ verification
def test_fake_vantage_terminal_passes_every_check_but_is_never_an_actual_verification(tmp_path, spec):
    fake = terminal(tmp_path, spec.schedule)
    rec = verify(provider_for(fake), spec.schedule)
    status = {c["key"]: c["status"] for c in rec["checks"]}
    assert status == {"terminal": "PASS", "broker_connection": "PASS", "broker_is_vantage": "PASS", "environment": "PASS",
                      "symbol": "PASS", "live_tick": "PASS", "candles_M1": "PASS", "candles_M5": "PASS",
                      "candles_M15": "PASS", "candles_H1": "PASS", "consistency_M5": "PASS", "consistency_M15": "PASS",
                      "consistency_H1": "PASS", "reconnect": "PASS", "adapter": "WARN"}
    assert rec["overall"] == "PARTIAL" and rec["adapter"] == "tests.fake_mt5.FakeMT5"
    assert rec["broker"] == VANTAGE and rec["is_vantage"] and rec["environment"] == "DEMO"
    assert rec["symbol"] == "XAUUSD+"                         # discovered, not hardcoded (XAUEUR rejected)
    assert LOGIN not in json.dumps(rec, default=str)
    inits = [kw for name, kw in fake.calls if name == "initialize"]
    assert len(inits) == 2 and all(set(kw) <= {"timeout", "path"} for kw in inits)   # reconnect, never credentials
    assert ("shutdown", None) in fake.calls
    tick = next(c for c in rec["checks"] if c["key"] == "live_tick")
    assert tick["data"]["bid"] == "3901.1" and tick["data"]["age_seconds"] == 1.0 and "rule:ny_close" in tick["detail"]


def test_verified_needs_the_real_adapter(tmp_path, spec):
    fake = terminal(tmp_path, spec.schedule)
    provider = provider_for(fake)
    facts, frames = provider.verification_facts()
    env = environment(facts["terminal"]["data_path"])
    assert mt5_verify.evaluate(facts, frames, now=NOW, schedule=spec.schedule, env=env)["overall"] == "PARTIAL"
    real = {**facts, "adapter": mt5_verify.REAL_ADAPTER}
    assert mt5_verify.evaluate(real, frames, now=NOW, schedule=spec.schedule, env=env)["overall"] == "VERIFIED"
    unknown = {**env, "environment": "UNKNOWN"}
    assert mt5_verify.evaluate(real, frames, now=NOW, schedule=spec.schedule, env=unknown)["overall"] == "PARTIAL"


def test_other_broker_terminal_not_running_and_closed_market(tmp_path, spec):
    rec = verify(provider_for(terminal(tmp_path, spec.schedule, company="Other Broker Ltd", server="OtherBroker-Demo")),
                 spec.schedule)
    assert rec["overall"] == "FAILED" and {c["key"]: c["status"] for c in rec["checks"]}["broker_is_vantage"] == "FAIL"
    down = verify(provider_for(terminal(tmp_path, spec.schedule), running=False), spec.schedule)
    assert down["overall"] == "FAILED" and down["checks"][0]["status"] == "FAIL" and "not running" in down["checks"][0]["detail"]
    assert all(c["status"] == "SKIPPED" for c in down["checks"][1:] if c["key"] != "adapter")
    saturday = FixedClock(datetime(2026, 11, 14, 12, 0, tzinfo=UTC))           # weekly close: the last tick is old
    closed = verify(provider_for(terminal(tmp_path, spec.schedule), clock=saturday), spec.schedule, clock=saturday)
    tick = next(c for c in closed["checks"] if c["key"] == "live_tick")
    assert tick["status"] == "WARN" and "market is closed" in tick["detail"] and closed["overall"] == "PARTIAL"


def test_failed_reconnect_is_reported(tmp_path, spec):
    fake = terminal(tmp_path, spec.schedule)
    real_init = fake.initialize
    count = {"n": 0}

    def flaky(**kw):
        count["n"] += 1
        if count["n"] > 1:
            fake._error = (-10003, "IPC initialize failed")
            return False
        return real_init(**kw)
    fake.initialize = flaky
    rec = verify(provider_for(fake), spec.schedule)
    rc = next(c for c in rec["checks"] if c["key"] == "reconnect")
    assert rc["status"] == "FAIL" and "reconnect failed" in rc["detail"] and rec["overall"] == "FAILED"


# ------------------------------------------------------------------------------------------------ integrity
def test_integrity_catches_planted_defects(spec):
    m1 = synthetic_m1("2026-11-12T10:00:00Z", "2026-11-12T12:00:00Z", spec.schedule)
    now = datetime(2026, 11, 12, 12, 0, 30, tzinfo=UTC)
    assert integrity.check_candles(m1, Timeframe.M1, now=now, schedule=spec.schedule)["status"] == "PASS"
    bad = m1.copy()
    bad.iloc[5, bad.columns.get_loc("low")] = bad.iloc[5]["high"] + 1                        # invalid OHLC
    bad = pd.concat([bad, bad.iloc[[10]]])                                                     # duplicate
    shifted = bad.iloc[[20]].copy()
    shifted.index = shifted.index + pd.Timedelta(seconds=30)                                   # off-grid
    future = bad.iloc[[-1]].copy()
    future.index = pd.DatetimeIndex([pd.Timestamp("2026-11-12T13:00:00Z")])                   # closed, in the future
    bad = pd.concat([bad, shifted, future])
    rep = integrity.check_candles(bad, Timeframe.M1, now=now, schedule=spec.schedule)
    found = {e["check"] for e in rep["errors"]}
    assert rep["status"] == "FAIL"
    assert {"invalid_ohlc", "duplicate_timestamp", "misaligned_timestamp", "closed_bar_in_future"} <= found
    gappy = m1.drop(m1.index[30:40])
    rep = integrity.check_candles(gappy, Timeframe.M1, now=now, schedule=spec.schedule)
    assert rep["status"] == "WARN" and rep["warnings"][0]["check"] == "missing_bars" and rep["warnings"][0]["count"] == 10
    native = resample(m1, Timeframe.M1, Timeframe.M5)
    assert integrity.cross_timeframe(m1, native, Timeframe.M5, tolerance=0.01)["status"] == "PASS"
    native.iloc[3, native.columns.get_loc("high")] += 2
    cmp = integrity.cross_timeframe(m1, native, Timeframe.M5, tolerance=0.01)
    assert cmp["mismatches"] == 1 and cmp["status"] == "FAIL"     # 1 of 24 bars (> 2 %)


# ------------------------------------------------------------------------------------------------ feed health
def test_feed_monitor_records_spreads_and_state_transitions(db):
    clock = FixedClock(NOW)
    box = {"q": make_quote("3900.00", "3900.20", at=NOW), "state": (MarketState.LIVE, "fresh")}
    market = SimpleNamespace(market_quote=lambda: box["q"], state=lambda: box["state"], active="mt5")
    feed = FeedMonitor(db, clock, market)
    feed.tick(NOW)
    for i, spread in enumerate(("0.30", "0.25", "0.60"), 1):
        t = NOW + timedelta(seconds=i)
        box["q"] = make_quote("3900.00", str(Decimal("3900.00") + Decimal(spread)), at=t)
        feed.tick(t)
    st = feed.stats()
    assert st["quotes"] == 4 and st["median"] == pytest.approx(0.275) and st["max"] == pytest.approx(0.6)
    assert feed.spread_context(Decimal("0.80"))["above_p95"] is True
    box["state"] = (MarketState.DELAYED, "no new tick")
    feed.tick(NOW + timedelta(seconds=30))
    box["state"] = (MarketState.DISCONNECTED, "terminal closed")
    feed.tick(NOW + timedelta(seconds=90))
    box["state"] = (MarketState.LIVE, "reconnected")
    feed.tick(NOW + timedelta(seconds=150))
    with db.read() as con:
        events = [(r["from_state"], r["to_state"]) for r in con.execute("SELECT * FROM feed_events ORDER BY id")]
        samples = con.execute("SELECT COUNT(*) FROM feed_samples").fetchone()[0]
    assert events == [(None, "LIVE"), ("LIVE", "DELAYED"), ("DELAYED", "DISCONNECTED"), ("DISCONNECTED", "LIVE")]
    assert samples == 3                                                       # one per minute


# ------------------------------------------------------------------------------------------------ API
def test_verification_route_stores_a_labelled_record(tmp_path, spec):
    fake = terminal(tmp_path, spec.schedule)
    app = create_app_with(tmp_path, provider_for(fake))
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        assert c.get("/api/live/status").json()["verified"] is False
        rec = c.post("/api/live/verifications")
        assert rec.status_code == 201, rec.text
        body = rec.json()
        assert body["overall"] == "PARTIAL" and body["actual_terminal"] is False and body["adapter"] == "tests.fake_mt5.FakeMT5"
        listed = c.get("/api/live/verifications").json()
        assert listed["verifications"][0]["id"] == body["id"] and "actual terminal" in listed["note"]
        status = c.get("/api/live/status").json()
        assert status["verified"] is False and status["verification"]["overall"] == "PARTIAL"
        assert c.post("/api/live/verifications", headers={"X-GoldScalp-Request": "0"}).status_code == 403   # CSRF


def create_app_with(tmp_path, provider):
    from goldscalp.api.app import create_app
    return create_app(Settings(db_path=tmp_path / "v.sqlite3", live_provider="none"), clock=FixedClock(NOW),
                      provider=provider, start_market_data=False)


# ------------------------------------------------------------------------------------------------ generic MetaQuotes build
def test_generic_metaquotes_terminal_is_identified_by_its_vantage_trade_server(tmp_path, spec):
    """A terminal downloaded from MetaQuotes reports company 'MetaQuotes Ltd.' even when logged in to Vantage: the
    trade server (last login + the server data folder the terminal writes) identifies the broker instead."""
    fake = terminal(tmp_path, spec.schedule, company="MetaQuotes Ltd.", server="VantageMarkets-Demo")
    rec = verify(provider_for(fake), spec.schedule)
    check = next(c for c in rec["checks"] if c["key"] == "broker_is_vantage")
    assert check["status"] == "PASS" and check["data"]["basis"] == "trade server"
    assert "'MetaQuotes Ltd.' (the terminal build's distributor)" in check["detail"]
    assert [f["server"] for f in check["data"]["server_folders"]] == ["VantageMarkets-Demo"]   # Default/Custom/signals skipped
    assert rec["broker"] == "VantageMarkets-Demo (trade server)" and rec["is_vantage"] and LOGIN not in json.dumps(rec)


def test_trade_server_identification_needs_consistent_evidence(tmp_path):
    from goldscalp.data.providers.mt5_identity import identify_broker, server_activity
    data = tmp_path / "T"
    make_server_folder(data, "VantageMarkets-Demo", age_seconds=3600)
    make_server_folder(data, "OtherBroker-Live", age_seconds=10)               # another server is the active one
    env = {"server": "VantageMarkets-Demo"}
    act = server_activity(str(data))
    assert [s["server"] for s in act["servers"]] == ["OtherBroker-Live", "VantageMarkets-Demo"]
    ident = identify_broker("MetaQuotes Ltd.", connected=True, env=env, activity=act)
    assert ident["status"] == "WARN" and not ident["is_vantage"] and "could not be confirmed" in ident["evidence"]
    solo = tmp_path / "S"
    make_server_folder(solo, "VantageMarkets-Demo")
    one = server_activity(str(solo))
    assert identify_broker("MetaQuotes Ltd.", connected=False, env=env, activity=one)["status"] == "WARN"
    assert identify_broker("MetaQuotes Ltd.", connected=True, env=env, activity=one)["status"] == "PASS"
    assert identify_broker("MetaQuotes Ltd.", connected=True, env={"server": "AdvantageFX-Demo"},
                           activity=one)["status"] == "FAIL"
