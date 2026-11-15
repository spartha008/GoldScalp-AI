"""Phase 2: signal rules and gates, signal storage and the paper link, replay without look-ahead,
API routes and wording."""
from __future__ import annotations

import re
import warnings
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pandas as pd
import pytest

from goldscalp.analysis import intelligence, multi_timeframe, replay, signals
from goldscalp.analysis.config import default_config
from goldscalp.api.app import create_app
from goldscalp.core.clock import iso
from goldscalp.core.config import PROJECT_ROOT, Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.storage import paper_repo, signal_repo
from tests.conftest import csv_bytes, make_quote, synthetic_m1

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

AS_OF = datetime(2026, 11, 11, 15, 0, 30, tzinfo=UTC)
REQUIRED = ("direction", "generated_at", "as_of", "cutoff", "timeframe", "structure", "supporting", "conflicting",
            "invalidation", "context", "source", "freshness", "explanation", "definitions", "config_fingerprint",
            "input_range", "score_label")
FORBIDDEN = re.compile(r"guarantee|probability of profit|profit probability|win probability|risk[- ]free|"
                       r"sure profit|certain profit", re.IGNORECASE)


@pytest.fixture(scope="module")
def m1():
    from goldscalp.simulation.instrument import InstrumentSpec
    from tests.conftest import SPEC_PATH
    schedule = InstrumentSpec.load(SPEC_PATH).schedule
    return synthetic_m1("2026-11-05T00:00Z", "2026-11-11T15:00Z", schedule, seed=21)


def frames_from(m1: pd.DataFrame, as_of: datetime) -> dict[str, pd.DataFrame]:
    visible = m1[(m1.index + pd.Timedelta(minutes=1)) <= pd.Timestamp(as_of)]
    out = {"M1": visible.tail(intelligence.FRAME_LIMITS["M1"])}
    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
        out[tf.value] = resample(visible, Timeframe.M1, tf, as_of=as_of).tail(intelligence.FRAME_LIMITS[tf.value])
    return out


def evaluate(m1, *, as_of=AS_OF, mode="live", state="LIVE", spread=0.3, generated_at=AS_OF):
    frames = frames_from(m1, as_of)
    results = multi_timeframe.build(frames, as_of, default_config())
    return signals.evaluate(results, as_of, default_config(), mode=mode, series="test", market_state=state,
                            spread=spread, spread_source="test", source="synthetic", generated_at=generated_at)


# ------------------------------------------------------------------------------- signal rules
@pytest.mark.parametrize("score, h1, m15, gates, expected", [
    (4, "bullish", "bullish", [], "BULLISH"),
    (6, "ranging", "bullish", [], "BULLISH"),
    (3, "bullish", "bullish", [], "NEUTRAL"),
    (5, "bearish", "bullish", [], "NEUTRAL"),        # H1 against it
    (5, "bullish", "bearish", [], "NEUTRAL"),        # M15 against it
    (-4, "bearish", "ranging", [], "BEARISH"),
    (-6, "bearish", "bullish", [], "NEUTRAL"),
    (8, "bullish", "bullish", ["stale"], "NO SIGNAL"),
])
def test_classification_rule(score, h1, m15, gates, expected):
    assert signals.classify(score, h1, m15, 4, gates) == expected


def test_signal_has_every_required_field_and_is_deterministic(m1):
    first = evaluate(m1, generated_at=AS_OF)
    second = evaluate(m1, generated_at=AS_OF + timedelta(minutes=7))
    for key in REQUIRED:
        assert key in first, key
    assert first["direction"] in signals.DIRECTIONS
    assert first["score_label"] == "evidence agreement score (not a probability)"
    assert first["cutoff"] == "2026-11-11T15:00:00Z" and first["as_of"] == iso(AS_OF)
    assert first["id"] == second["id"]
    assert {k: v for k, v in first.items() if k != "generated_at"} == {k: v for k, v in second.items() if k != "generated_at"}
    assert first["generated_at"] != second["generated_at"]
    assert set(first["structure"]) == {"H1", "M15", "M5", "M1"}
    for item in first["supporting"] + first["conflicting"]:
        assert item["score"] != 0 and item["observation"]


@pytest.mark.parametrize("kwargs, reason", [
    ({"state": "DELAYED"}, "DELAYED, not LIVE"),
    ({"state": "DISCONNECTED"}, "DISCONNECTED, not LIVE"),
    ({"spread": 2.5}, "above the maximum"),
    ({"as_of": AS_OF + timedelta(hours=3)}, "stale data"),
])
def test_signal_gates_give_no_signal(m1, kwargs, reason):
    out = evaluate(m1, **kwargs)
    assert out["direction"] == "NO SIGNAL"
    assert any(reason in g for g in out["gates_failed"]), out["gates_failed"]
    assert out["explanation"].startswith("No signal:")


def test_too_few_candles_and_invalid_candles_give_no_signal(m1):
    short = m1[m1.index >= pd.Timestamp("2026-11-11T13:00Z")]
    out = evaluate(short)
    assert out["direction"] == "NO SIGNAL" and any("required" in g for g in out["gates_failed"])
    broken = m1.copy()
    broken.loc[broken.index[-3], "high"] = broken["close"].iloc[-3] - 5.0      # high below close
    out = evaluate(broken)
    assert any("inconsistent OHLC" in g for g in out["gates_failed"])


def test_replay_mode_ignores_market_state_but_not_data_quality(m1):
    out = evaluate(m1, mode="replay", state=None)
    assert not any("not LIVE" in g for g in out["gates_failed"])


def test_forward_outcome_is_a_historical_observation():
    payload = {"direction": "BULLISH", "reference_price": 100.0, "atr_m5": 2.0}
    after = pd.DataFrame({"open": [100, 101, 102], "high": [101, 103, 102.5], "low": [99, 100.5, 101],
                          "close": [100.5, 102, 101.5]},
                         index=pd.date_range("2026-11-11T15:00Z", periods=3, freq="5min", tz="UTC"))
    out = signals.forward_outcome(payload, after, Timeframe.M5, [2, 6])
    assert out["label"] == "historical observation"
    done, pending = out["horizons"]
    assert (done["move"], done["mfe"], done["mae"], done["move_atr"]) == (2.0, 3.0, 1.0, 1.0)
    assert pending["status"] == "pending"
    bearish = signals.forward_outcome({**payload, "direction": "BEARISH"}, after, Timeframe.M5, [2])
    assert bearish["horizons"][0]["move"] == -2.0
    assert signals.forward_outcome({**payload, "direction": "NEUTRAL"}, after, Timeframe.M5, [2])["applicable"] is False


# ------------------------------------------------------------------------------- storage + paper link
def payload(direction, cutoff, score=0):
    return {"id": f"sig_{abs(hash((direction, cutoff))) % 10**20:020x}"[:24], "mode": "live", "replay_id": None,
            "series": "s", "as_of": cutoff, "cutoff": cutoff, "generated_at": cutoff, "direction": direction,
            "score": score, "definition": "signal@1", "config_fingerprint": "f"}


def test_signals_are_stored_only_when_the_classification_changes(db):
    db.migrate()
    with db.transaction() as con:
        assert signal_repo.record_if_changed(con, payload("NEUTRAL", "2026-11-11T15:00:00Z"))
        assert not signal_repo.record_if_changed(con, payload("NEUTRAL", "2026-11-11T15:01:00Z"))
        assert signal_repo.record_if_changed(con, payload("BULLISH", "2026-11-11T15:02:00Z", 5))
        assert not signal_repo.record_if_changed(con, payload("BEARISH", "2026-11-11T14:00:00Z"))   # older
        current = payload("BULLISH", "2026-11-11T15:03:00Z", 5)
        assert signal_repo.stored_id_for(con, current) == payload("BULLISH", "2026-11-11T15:02:00Z")["id"]
        assert signal_repo.stored_id_for(con, payload("NEUTRAL", "2026-11-11T15:03:00Z")) is None
    with db.read() as con:
        assert [s["direction"] for s in signal_repo.history(con, mode="live")] == ["BULLISH", "NEUTRAL"]


def test_paper_position_records_the_signal_that_prefilled_it(db, clock, spec, costs, account_config):
    db.migrate()
    stored = payload("BULLISH", "2026-11-11T15:00:00Z", 5)
    with db.transaction() as con:
        signal_repo.record_if_changed(con, stored)
    engine = PaperTradingEngine(db, clock, lambda: make_quote("4000.00", "4000.30"), spec, costs, account_config)
    position = engine.open_position("buy", "0.01", stop_loss="3990", signal_id=stored["id"])
    with db.read() as con:
        assert paper_repo.position(con, position.id).signal_id == stored["id"]
    assert position.stop_loss == Decimal("3990")


# ------------------------------------------------------------------------------- replay (no look-ahead)
def dataset(m1, end=None) -> replay.ReplayDataset:
    base = m1 if end is None else m1[m1.index < pd.Timestamp(end)]
    return replay.ReplayDataset(series_key="test", source_id="synthetic", base_tf=Timeframe.M1, base=base)


def test_replay_frames_never_contain_candles_after_the_cursor(m1):
    ds = dataset(m1)
    session = replay.build_session(ds, Timeframe.M5, datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
                                   datetime(2026, 11, 11, 15, 0, tzinfo=UTC), AS_OF)
    for n in (1, 7, 13):
        session.step(n)
        cursor = pd.Timestamp(session.cursor)
        frames = session.frames()
        for key, frame in frames.items():
            tf = Timeframe.parse(key)
            assert (frame.index <= cursor).all()
            closed = frame[frame["closed"]]
            assert ((closed.index + pd.Timedelta(seconds=tf.seconds)) <= cursor).all(), key
        if cursor.minute:                                       # mid-hour: the hour is shown as forming
            assert not bool(frames["H1"]["closed"].iloc[-1])


def strip(result: dict) -> dict:
    out = dict(result)
    out["signal"] = {k: v for k, v in result["signal"].items() if k != "generated_at"}
    return out


def test_replay_step_equals_analysis_of_the_truncated_dataset(m1):
    cfg = default_config()
    start, end = datetime(2026, 11, 11, 12, 0, tzinfo=UTC), datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    session = replay.build_session(dataset(m1), Timeframe.M5, start, end, AS_OF)
    future = m1.copy()                                          # the same data plus wild future candles
    extra = future.iloc[-30:].copy()
    extra.index = extra.index + pd.Timedelta(hours=1)
    extra[["open", "high", "low", "close"]] *= 1.05
    with_future = replay.build_session(dataset(pd.concat([future, extra])), Timeframe.M5, start, end, AS_OF)
    for n in (3, 9, 17):
        session.step(n)
        with_future.step(n)
        cursor = session.cursor
        run = dict(chart_tf=Timeframe.M5, mode="replay", series="test", market_state=None, spread=0.3,
                   spread_source="t", source="s", generated_at=AS_OF)
        from_replay = intelligence.run(session.frames(), cursor, cfg, **run)
        truncated = dataset(m1, end=cursor)                    # candles that OPENED before the cursor
        direct = intelligence.run(replay.frames_at(truncated, cursor), cursor, cfg, **run)
        future_run = intelligence.run(with_future.frames(), with_future.cursor, cfg, **run)
        assert strip(from_replay) == strip(direct)
        assert strip(from_replay) == strip(future_run)


def test_replay_session_validation(m1):
    ds = dataset(m1)
    with pytest.raises(replay.ReplayError):
        replay.build_session(ds, Timeframe.M5, datetime(2026, 11, 11, 15, tzinfo=UTC),
                             datetime(2026, 11, 11, 14, tzinfo=UTC), AS_OF)
    with pytest.raises(replay.ReplayError):
        replay.build_session(ds, Timeframe.M5, datetime(2026, 12, 1, tzinfo=UTC), datetime(2026, 12, 2, tzinfo=UTC), AS_OF)
    session = replay.build_session(ds, Timeframe.H1, datetime(2026, 11, 11, 10, tzinfo=UTC),
                                   datetime(2026, 11, 11, 15, tzinfo=UTC), AS_OF)
    with pytest.raises(replay.ReplayError):
        session.step(0)
    session.step(500)
    assert session.finished and session.cursor == datetime(2026, 11, 11, 15, tzinfo=UTC)


# ------------------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path, clock, m1):
    settings = Settings(db_path=tmp_path / "p2.sqlite3", live_provider="none", max_import_mb=5)
    app = create_app(settings, clock=clock, provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        frame = m1[m1.index >= pd.Timestamp("2026-11-09T00:00Z")]
        r = c.post("/api/data/import", files={"file": ("x.csv", csv_bytes(frame), "text/csv")},
                   data={"dialect": "generic_utc", "timeframe": "M1", "time_basis": "utc", "series_label": "p2",
                         "candle_side": "bid", "price_decimals": "2"})
        assert r.status_code == 200, r.text
        yield c


def test_intelligence_route(client):
    body = client.get("/api/analysis/intelligence?tf=M5&series=csv:p2").json()
    assert body["kind"] == "historical"
    data = body["data"]
    assert set(data["overlays"]) >= {"structure", "liquidity", "fvg", "order_blocks"}
    assert data["signal"]["direction"] == "NO SIGNAL"          # imported history is never a live signal
    assert any("HISTORICAL, not LIVE" in g for g in data["signal"]["gates_failed"])
    assert data["signal"]["stored_id"] is None and "recorded" not in data["signal"]
    assert [r["timeframe"] for r in data["multi_timeframe"]["timeframes"]] == ["H1", "M15", "M5", "M1"]
    assert not FORBIDDEN.search(str(body))


def test_analysis_config_and_signal_routes(client):
    cfg = client.get("/api/analysis/config").json()
    assert cfg["fingerprint"] == default_config().fingerprint and cfg["definitions"]["signal"] == "signal@1"
    assert client.get("/api/signals").json()["signals"] == []
    assert client.get("/api/signals/sig_00000000000000000000").status_code == 404
    order = client.post("/api/sim/orders", json={"side": "buy", "lots": "0.01", "signal_id": "sig_00000000000000000000"})
    assert order.status_code == 422 and "unknown signal" in order.text
    assert client.post("/api/sim/orders", json={"side": "buy", "lots": "0.01", "signal_id": "x"}).status_code == 422
    replay_signal = {**payload("BEARISH", "2026-11-11T15:00:00Z", -5), "mode": "replay", "replay_id": "rp_1"}
    with client.app.state.gs.db.transaction() as con:
        signal_repo.record_if_changed(con, replay_signal)
    refused = client.post("/api/sim/orders", json={"side": "sell", "lots": "0.01", "signal_id": replay_signal["id"]})
    assert refused.status_code == 422 and "only a live signal" in refused.text


def test_replay_api_flow(client):
    created = client.post("/api/replay", json={"series": "csv:p2", "timeframe": "M5", "start": "2026-11-11T12:00:00Z",
                                               "end": "2026-11-11T15:00:00Z", "lookback_days": 3})
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    rid = data["replay"]["id"]
    assert data["cursor"] == "2026-11-11T12:00:00Z" and data["replay"]["base_tf"] == "M1"
    assert all(c["time"] + 300 <= int(pd.Timestamp(data["cursor"]).timestamp()) for c in data["candles"])
    stepped = client.post(f"/api/replay/{rid}/step", json={"n": 3}).json()["data"]
    assert stepped["cursor"] == "2026-11-11T12:15:00Z" and stepped["replay"]["position"] == 3
    assert stepped["analysis"]["signal"]["mode"] == "replay"
    assert stepped["signal_history"] and stepped["signal_history"][0]["replay_id"] == rid
    assert client.post(f"/api/replay/{rid}/seek", json={"position": 0}).json()["data"]["cursor"] == "2026-11-11T12:00:00Z"
    assert client.get(f"/api/replay/{rid}").status_code == 200
    assert client.delete(f"/api/replay/{rid}").status_code == 200
    assert client.get(f"/api/replay/{rid}").status_code == 404
    bad = client.post("/api/replay", json={"series": "csv:p2", "timeframe": "M5", "start": "2026-11-11T15:00:00Z",
                                           "end": "2026-11-11T12:00:00Z"})
    assert bad.status_code == 422
    assert client.post("/api/replay", json={"series": "nope", "timeframe": "M5", "start": "2026-11-11T12:00:00Z",
                                            "end": "2026-11-11T15:00:00Z"}).status_code == 404


def test_replay_needs_the_csrf_header(tmp_path, clock):
    app = create_app(Settings(db_path=tmp_path / "c.sqlite3", live_provider="none"), clock=clock, provider=None,
                     start_market_data=False)
    with TestClient(app) as c:
        assert c.post("/api/replay", json={}).status_code == 403


def test_pages_exist(client):
    for path in ("/", "/replay", "/paper"):
        assert client.get(path).status_code == 200


def test_user_facing_text_never_promises_outcomes():
    files = [*PROJECT_ROOT.joinpath("web").glob("*.html"), *PROJECT_ROOT.joinpath("web", "js").glob("*.js"),
             *PROJECT_ROOT.joinpath("goldscalp", "analysis").glob("*.py")]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not FORBIDDEN.search(text), f"{path.name}: {FORBIDDEN.search(text).group(0)}"
    assert "not a probability" in signals.SCORE_LABEL
