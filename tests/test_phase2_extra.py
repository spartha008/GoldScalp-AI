"""Phase 2, Parts 13-16: swing confirmation delays, DST and session boundaries, ATR, multi-timeframe
synchronization, incomplete candles, mirror symmetry and conflicting signals, recorded-data
no-look-ahead, granular API routes, display sensitivity, safety boundaries and Phase 1 regressions."""
from __future__ import annotations

import copy
import re
import warnings
from datetime import date, datetime, UTC

import numpy as np
import pandas as pd
import pytest

from goldscalp.analysis import (
    candles as candles_mod,
    fvg,
    intelligence,
    liquidity,
    market_structure,
    multi_timeframe,
    order_blocks,
    replay,
    sessions,
    signals,
)
from goldscalp.analysis.config import AnalysisConfig, _reference, default_config
from goldscalp.api.app import create_app
from goldscalp.core.clock import FixedClock, iso
from goldscalp.core.config import PROJECT_ROOT, Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample
from goldscalp.data.validation import validate
from tests.conftest import recorded

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

START = pd.Timestamp("2026-11-11T14:00Z")
M1, M5, M15, H1 = Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1


def frame_of(highs, lows=None, closes=None, start=START, minutes=1):
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float) if lows is not None else highs - 1.0
    closes = np.asarray(closes, dtype=float) if closes is not None else (highs + lows) / 2
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range(start, periods=len(highs), freq=f"{minutes}min", tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(highs, np.maximum(opens, closes)),
                         "low": np.minimum(lows, np.minimum(opens, closes)), "close": closes, "tick_volume": 1,
                         "spread": 0.2, "closed": True}, index=idx)


def strength(k: int) -> AnalysisConfig:
    data = copy.deepcopy(_reference())
    data["structure"]["swing_strength"]["M1"] = k
    return AnalysisConfig(data)


# ------------------------------------------------------------------------------- swing confirmation delay
@pytest.mark.parametrize("k", [2, 3, 5])
def test_a_swing_is_known_exactly_k_candles_after_its_pivot(k):
    highs = [100 + i for i in range(11)] + [110 - j for j in range(1, 11)]          # peak at bar 10
    frame = frame_of(highs)
    pivot = 10
    confirm_close = (START + pd.Timedelta(minutes=pivot + k + 1)).to_pydatetime()
    full = market_structure.analyse(frame, M1, confirm_close, strength(k), limit=0)
    swing = next(s for s in full["swings"] if s["type"] == "swing_high")
    assert swing["time"] == iso((START + pd.Timedelta(minutes=pivot)).to_pydatetime())
    assert swing["known_at"] == iso(confirm_close)
    early = market_structure.analyse(frame, M1, confirm_close - pd.Timedelta(minutes=1), strength(k), limit=0)
    assert not [s for s in early["swings"] if s["type"] == "swing_high"]
    assert any(d["type"] == "candidate_swing_high" and d["status"] == "developing" for d in early["developing"])


def test_an_equal_value_to_the_right_still_confirms_the_swing():
    """Ties: a later equal high (low) does not cancel a swing high (low); both sides use the same rule."""
    highs = [100, 101, 102, 105, 105, 103, 102, 101]
    up = market_structure.analyse(frame_of(highs), M1, (START + pd.Timedelta(minutes=8)).to_pydatetime(), strength(3), limit=0)
    assert [s["time"] for s in up["swings"] if s["type"] == "swing_high"] == [iso((START + pd.Timedelta(minutes=3)).to_pydatetime())]
    lows = [8000 - h for h in highs]
    down = market_structure.analyse(frame_of(np.array(lows) + 1.0, lows), M1, (START + pd.Timedelta(minutes=8)).to_pydatetime(),
                                    strength(3), limit=0)
    assert [s["time"] for s in down["swings"] if s["type"] == "swing_low"] == [iso((START + pd.Timedelta(minutes=3)).to_pydatetime())]


def test_hh_hl_lh_ll_labels():
    # highs: 105 (H) then 107 (HH) then 104 (LH); lows: 98 (L) then 100 (HL) then 96 (LL)
    h = [100, 101, 102, 105, 102, 101, 103, 104, 107, 103, 102, 103, 104, 102, 101, 100, 99, 98]
    lo = [99, 99, 99, 98.5, 98, 99, 101, 102, 102, 101, 100, 101, 102, 101, 99, 97, 96, 97]
    frame = frame_of(h, lo, closes=(np.array(h) + np.array(lo)) / 2)
    out = market_structure.analyse(frame, M1, (START + pd.Timedelta(minutes=len(h))).to_pydatetime(), strength(2), limit=0)
    highs = [s["label"] for s in out["swings"] if s["type"] == "swing_high"]
    lows = [s["label"] for s in out["swings"] if s["type"] == "swing_low"]
    assert highs[:3] == ["H", "HH", "LH"]
    assert lows[0] == "L" and "HL" in lows


# ------------------------------------------------------------------------------- DST and session boundaries
def test_trading_day_close_follows_us_dst():
    assert liquidity.day_close(date(2026, 7, 15)) == datetime(2026, 7, 15, 21, 0, tzinfo=UTC)     # EDT
    assert liquidity.day_close(date(2026, 10, 30)) == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)   # last EDT Friday
    assert liquidity.day_close(date(2026, 11, 2)) == datetime(2026, 11, 2, 22, 0, tzinfo=UTC)     # EST
    idx = pd.DatetimeIndex(["2026-10-30T20:59Z", "2026-10-30T21:00Z", "2026-11-02T21:59Z", "2026-11-02T22:00Z"])
    keys = [d.isoformat() for d in liquidity.day_key(idx)]
    assert keys == ["2026-10-30", "2026-10-31", "2026-11-02", "2026-11-03"]


@pytest.mark.parametrize("as_of, start, end", [
    (datetime(2026, 10, 26, 14, tzinfo=UTC), "2026-10-26T12:00:00Z", "2026-10-26T17:00:00Z"),   # UK on GMT, US on EDT
    (datetime(2026, 11, 3, 14, tzinfo=UTC), "2026-11-03T13:00:00Z", "2026-11-03T17:00:00Z"),    # both standard time
    (datetime(2026, 7, 15, 14, tzinfo=UTC), "2026-07-15T12:00:00Z", "2026-07-15T16:00:00Z"),    # both summer time
])
def test_london_new_york_overlap_across_dst_changes(as_of, start, end):
    frame = frame_of([100.0] * 10, start=pd.Timestamp(as_of) - pd.Timedelta(minutes=50), minutes=5)
    out = sessions.analyse(frame, M5, as_of, default_config())
    overlap = next(o for o in out["overlaps"] if o["sessions"] == ["London", "New York"])
    assert (overlap["start"], overlap["end"], overlap["status"]) == (start, end, "active")


def test_session_window_includes_its_start_and_excludes_its_end():
    idx = pd.DatetimeIndex(["2026-11-11T07:55Z", "2026-11-11T08:00Z", "2026-11-11T16:55Z", "2026-11-11T17:00Z"])
    frame = pd.DataFrame({"high": [1, 2, 3, 4], "low": [0, 1, 2, 3]}, index=idx)
    london = next(d for d in sessions.definitions(default_config()) if d.key == "london")
    s, e = london.window(date(2026, 11, 11))
    assert list(sessions.bars_in(frame, s, e).index) == [idx[1], idx[2]]
    assert sessions.active_names(sessions.definitions(default_config()), datetime(2026, 11, 11, 17, 0, tzinfo=UTC)) == ["New York"]
    assert sessions.active_names(sessions.definitions(default_config()), datetime(2026, 11, 11, 16, 59, tzinfo=UTC)) == ["London", "New York"]


# ------------------------------------------------------------------------------- ATR
def test_wilder_atr_golden():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 102, 98, 100), (100, 103, 97, 100), (100, 104, 96, 100)]
    idx = pd.date_range(START, periods=5, freq="min", tz="UTC")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    atr = candles_mod.atr_series(frame, 3)                    # TRs: 2, 4, 6, 8 (after the first bar)
    assert np.isnan(atr[:3]).all() and atr[3] == 4.0 and round(atr[4], 6) == round((4 * 2 + 8) / 3, 6)


# ------------------------------------------------------------------------------- multi-timeframe synchronization
@pytest.fixture(scope="module")
def rec_ds():
    return replay.ReplayDataset("rec", "mt5", M1, recorded("mt5_xauusd_m1_recorded.csv"),
                                history={"H1": recorded("mt5_xauusd_h1_recorded.csv")})


def test_all_timeframes_share_one_cutoff_and_one_source(rec_ds):
    cut = datetime(2026, 11, 13, 14, 37, tzinfo=UTC)                     # mid-hour, mid-M15, mid-M5
    frames = replay.frames_at(rec_ds, cut)
    results = multi_timeframe.build(frames, cut, default_config())
    for key, r in results.items():
        tf = Timeframe.parse(key)
        assert ((r["bars"].index + pd.Timedelta(seconds=tf.seconds)) <= pd.Timestamp(cut)).all()
    assert results["H1"]["freshness"]["forming_excluded"] and results["M15"]["freshness"]["forming_excluded"]
    last_h1 = results["H1"]["bars"].index[-1]
    m1 = frames["M1"][(frames["M1"].index >= last_h1) & (frames["M1"].index < last_h1 + pd.Timedelta(hours=1))]
    assert results["H1"]["bars"]["high"].iloc[-1] == m1["high"].max()       # H1 is built from the same M1 candles
    assert results["H1"]["bars"]["low"].iloc[-1] == m1["low"].min()


def test_incomplete_candles_are_never_analysed():
    frame = recorded("mt5_xauusd_m1_recorded.csv").iloc[:400].copy()
    frame.loc[frame.index[-5:], "closed"] = False
    cut = (frame.index[-1] + pd.Timedelta(minutes=1)).to_pydatetime()
    forming = {f"M1@{iso(t.to_pydatetime())}" for t in frame.index[-5:]}
    st = market_structure.analyse(frame, M1, cut, limit=0)
    assert st["bars_used"] == 395
    used = set()
    for e in st["events"] + st["swings"] + fvg.analyse(frame, M1, cut, limit=0)["gaps"] + \
            order_blocks.analyse(frame, M1, cut, limit=0)["candidates"] + liquidity.analyse(frame, M1, cut)["events"]:
        used.update(e["candles"])
    assert not used & forming


# ------------------------------------------------------------------------------- symmetric rules, conflicts
def mirror(frame: pd.DataFrame, pivot: float = 8000.0) -> pd.DataFrame:
    out = frame.copy()
    out["open"], out["close"] = pivot - frame["open"], pivot - frame["close"]
    out["high"], out["low"] = pivot - frame["low"], pivot - frame["high"]
    return out


FLIP = {"BULLISH": "BEARISH", "BEARISH": "BULLISH", "NEUTRAL": "NEUTRAL", "NO SIGNAL": "NO SIGNAL"}


def test_mirrored_prices_give_the_mirrored_signal(rec_ds):
    mirrored = replay.ReplayDataset("rec", "mt5", M1, mirror(rec_ds.base), history={"H1": mirror(rec_ds.history["H1"])})
    seen = set()
    for minute in range(0, 31 * 60, 50):
        cut = (pd.Timestamp("2026-11-12T11:00Z") + pd.Timedelta(minutes=minute)).to_pydatetime()
        a = signals.evaluate(multi_timeframe.build(replay.frames_at(rec_ds, cut), cut, default_config()), cut,
                             default_config(), mode="replay", series="rec", market_state=None, spread=0.2,
                             spread_source="t", source="s", generated_at=cut)
        b = signals.evaluate(multi_timeframe.build(replay.frames_at(mirrored, cut), cut, default_config()), cut,
                             default_config(), mode="replay", series="rec", market_state=None, spread=0.2,
                             spread_source="t", source="s", generated_at=cut)
        assert b["score"] == -a["score"] and b["direction"] == FLIP[a["direction"]], cut
        swap = lambda t: t.replace("bullish", "@").replace("bearish", "bullish").replace("@", "bearish")  # noqa: E731
        assert sorted(swap(e["rule"]) for e in a["supporting"]) == sorted(e["rule"] for e in b["supporting"])
        assert sorted(swap(e["rule"]) for e in a["conflicting"]) == sorted(e["rule"] for e in b["conflicting"])
        seen.add(a["direction"])
    assert seen & {"BULLISH", "BEARISH"} and len(seen) >= 2, seen


def test_conflicting_higher_timeframes_never_give_a_directional_signal(rec_ds):
    conflicts = 0
    for minute in range(0, 660, 15):
        cut = (pd.Timestamp("2026-11-13T08:00Z") + pd.Timedelta(minutes=minute)).to_pydatetime()
        sig = signals.evaluate(multi_timeframe.build(replay.frames_at(rec_ds, cut), cut, default_config()), cut,
                               default_config(), mode="replay", series="rec", market_state=None, spread=0.2,
                               spread_source="t", source="s", generated_at=cut)
        h1, m15 = sig["structure"]["H1"]["state"], sig["structure"]["M15"]["state"]
        if {h1, m15} == {"bullish", "bearish"}:
            conflicts += 1
            assert sig["direction"] in ("NEUTRAL", "NO SIGNAL")
            rules = {e["rule"]: e for e in sig["supporting"] + sig["conflicting"]}
            assert rules["H1 structure"]["score"] * rules["M15 structure"]["score"] < 0
            assert sig["conflicting"], "a conflict must be listed, never hidden"
    assert conflicts > 0, "the recorded fixture should contain an H1/M15 disagreement"


# ------------------------------------------------------------------------------- recorded data: no look-ahead
def test_engines_have_no_lookahead_on_recorded_data():
    m5 = resample(recorded("mt5_xauusd_m1_recorded.csv"), M1, M5)
    full_st = market_structure.analyse(m5, M5, None, limit=0)
    full_fvg = fvg.analyse(m5, M5, None, limit=0)["gaps"]
    full_ob = order_blocks.analyse(m5, M5, None, limit=0)["candidates"]
    strip = lambda es: [{k: v for k, v in e.items() if k != "folded_breaks"} for e in es]   # noqa: E731
    for k in range(80, len(m5), 41):
        cut = (m5.index[k - 1] + pd.Timedelta(minutes=5)).to_pydatetime()
        c = iso(cut)
        assert strip(market_structure.analyse(m5.iloc[:k], M5, cut, limit=0)["events"]) == \
            strip([e for e in full_st["events"] if e["known_at"] <= c])
        assert [g["id"] for g in fvg.analyse(m5.iloc[:k], M5, cut, limit=0)["gaps"]] == \
            [g["id"] for g in full_fvg if g["known_at"] <= c]
        assert [o["id"] for o in order_blocks.analyse(m5.iloc[:k], M5, cut, limit=0)["candidates"]] == \
            [o["id"] for o in full_ob if o["known_at"] <= c]


def test_replay_on_recorded_data_equals_the_truncated_dataset(rec_ds):
    session = replay.build_session(rec_ds, M5, datetime(2026, 11, 13, 10, tzinfo=UTC),
                                   datetime(2026, 11, 13, 14, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC))
    run = dict(chart_tf=M5, mode="replay", series="rec", market_state=None, spread=0.2, spread_source="t",
               source="s", generated_at=datetime(2026, 11, 13, tzinfo=UTC))
    for n in (5, 13):
        session.step(n)
        cut = session.cursor
        truncated = replay.ReplayDataset("rec", "mt5", M1, rec_ds.base[rec_ds.base.index < pd.Timestamp(cut)],
                                         history=rec_ds.history)
        a = intelligence.run(session.frames(), cut, default_config(), **run)
        b = intelligence.run(replay.frames_at(truncated, cut), cut, default_config(), **run)
        assert a == b


def test_recorded_fixture_passes_phase1_validation():
    clean, report = validate(recorded("mt5_xauusd_m1_recorded.csv").reset_index(), M1)
    assert report.rows_rejected == 0 and len(clean) == 2068


# ------------------------------------------------------------------------------- API: granular routes
def csv_of(frame: pd.DataFrame) -> bytes:
    lines = ["timestamp,open,high,low,close,tick_volume,spread"]
    for ts, r in frame.iterrows():
        lines.append(f"{ts.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['tick_volume']},{r['spread']}")
    return "\n".join(lines).encode()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = tmp_path_factory.mktemp("p2x") / "x.sqlite3"
    clock = FixedClock(datetime(2026, 11, 13, 17, 37, 30, tzinfo=UTC))
    app = create_app(Settings(db_path=path, live_provider="none", max_import_mb=5), clock=clock, provider=None,
                     start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        for name, tf in (("mt5_xauusd_h1_recorded.csv", "H1"), ("mt5_xauusd_m1_recorded.csv", "M1")):
            r = c.post("/api/data/import", files={"file": (name, csv_of(recorded(name)), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": tf, "time_basis": "utc", "series_label": "rec",
                             "candle_side": "bid", "price_decimals": "2"})
            assert r.status_code == 200, r.text
        yield c


@pytest.mark.parametrize("path, key", [
    ("/api/analysis/structure?tf=M5", "structure"), ("/api/analysis/liquidity?tf=M5", "liquidity"),
    ("/api/analysis/fvg?tf=M15", "fvg"), ("/api/analysis/order-blocks?tf=M15", "order_blocks"),
    ("/api/analysis/multi-timeframe", "multi_timeframe"), ("/api/analysis/sessions", "sessions"),
    ("/api/analysis/volatility", "volatility"),
])
def test_granular_analysis_routes(client, path, key):
    body = client.get(f"{path}{'&' if '?' in path else '?'}series=csv:rec").json()
    assert body["kind"] == "historical" and key in body["data"]
    assert body["data"]["config_fingerprint"] == default_config().fingerprint


def test_granular_routes_match_the_intelligence_pass(client):
    intel = client.get("/api/analysis/intelligence?tf=M5&series=csv:rec").json()["data"]
    structure = client.get("/api/analysis/structure?tf=M5&series=csv:rec&limit=40").json()["data"]["structure"]
    assert structure["state"] == intel["overlays"]["structure"]["state"]
    assert structure["events"][-5:] == intel["overlays"]["structure"]["events"][-5:]
    mtf = client.get("/api/analysis/multi-timeframe?series=csv:rec").json()["data"]["multi_timeframe"]
    assert mtf == intel["multi_timeframe"]
    current = client.get("/api/signals/current?series=csv:rec").json()["data"]["signal"]
    strip = lambda s: {k: v for k, v in s.items() if k not in ("generated_at", "stored_id", "recorded")}  # noqa: E731
    assert strip(current) == strip(intel["signal"])


def test_display_sensitivity_changes_the_chart_only(client):
    base = client.get("/api/analysis/intelligence?tf=M5&series=csv:rec").json()["data"]
    coarse = client.get("/api/analysis/intelligence?tf=M5&series=csv:rec&swing_strength=8").json()["data"]
    assert coarse["overlays"]["display"]["swing_strength"] == 8
    assert len(coarse["overlays"]["structure"]["swings"]) <= len(base["overlays"]["structure"]["swings"])
    assert [s["time"] for s in coarse["overlays"]["structure"]["swings"]] != [s["time"] for s in base["overlays"]["structure"]["swings"]]
    strip = lambda s: {k: v for k, v in s.items() if k not in ("generated_at", "stored_id", "recorded")}  # noqa: E731
    assert strip(coarse["signal"]) == strip(base["signal"]) and coarse["multi_timeframe"] == base["multi_timeframe"]
    assert client.get("/api/analysis/structure?tf=M5&series=csv:rec&swing_strength=11").status_code == 422


# ------------------------------------------------------------------------------- Part 16: safety boundaries
WEB = PROJECT_ROOT / "web"


def test_no_password_or_credential_inputs_anywhere_in_the_ui():
    for page in WEB.glob("*.html"):
        text = page.read_text(encoding="utf-8").lower()
        assert 'type="password"' not in text and "mt5 password" not in text.replace("never asks for your mt5 password", "")


def test_analysis_never_executes_or_stores_paper_trades():
    import ast
    for path in (PROJECT_ROOT / "goldscalp" / "analysis").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in modules if m.startswith(("goldscalp.storage", "goldscalp.simulation.portfolio",
                                                         "goldscalp.simulation.account"))}, path.name
        assert "paper_positions" not in path.read_text(encoding="utf-8"), path.name


def test_server_binds_to_localhost_by_default():
    assert Settings().host == "127.0.0.1"
    assert "from goldscalp.__main__ import main" in (PROJECT_ROOT / "run.py").read_text(encoding="utf-8")
    assert re.search(r"host\s*=\s*settings\.host", (PROJECT_ROOT / "goldscalp" / "__main__.py").read_text(encoding="utf-8"))


def test_every_new_state_changing_route_needs_the_csrf_header(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "c.sqlite3", live_provider="none"), provider=None,
                     start_market_data=False)
    with TestClient(app) as c:
        for path in ("/api/performance/runs", "/api/performance/runs/x/cancel", "/api/replay"):
            assert c.post(path, json={}).status_code == 403
        assert c.delete("/api/performance/runs/x").status_code == 403


# ------------------------------------------------------------------------------- Phase 1 regressions
def test_phase1_overview_route_is_unchanged(client):
    body = client.get("/api/analysis/overview?tf=M5&series=csv:rec").json()["data"]
    assert body["structure"]["definition"] == "structure@1" and body["sessions"]["definition"] == "sessions@1"
    assert body["volatility"]["definition"] == "vol@1"


def test_phase1_candles_route_marks_a_partial_derived_hour_as_forming(client):
    candles = client.get("/api/market/candles?tf=H1&series=csv:rec&limit=50").json()["data"]["candles"]
    assert candles[-1]["closed"] is False and all(c["closed"] for c in candles[:-1])    # 17:37 is mid-hour


# ------------------------------------------------------------------------------- symmetry and light-pass equivalence
LABEL_FLIP = {"H": "L", "L": "H", "HH": "LL", "LL": "HH", "HL": "LH", "LH": "HL"}


def test_mirrored_candles_give_mirrored_swings_and_breaks():
    m5 = resample(recorded("mt5_xauusd_m1_recorded.csv"), M1, M5)
    a = market_structure.analyse(m5, M5, None, limit=0)
    b = market_structure.analyse(mirror(m5), M5, None, limit=0)
    assert len(a["swings"]) == len(b["swings"]) > 20
    flipped = {(x["time"], x["known_at"], LABEL_FLIP[x["label"]], round(8000 - x["price"], 5)) for x in a["swings"]}
    assert flipped == {(y["time"], y["known_at"], y["label"], y["price"]) for y in b["swings"]}
    assert [(e["type"], e["time"]) for e in a["events"]] == [(e["type"], e["time"]) for e in b["events"]]


@pytest.mark.parametrize("cut", ["2026-11-12T16:40:00Z", "2026-11-12T17:20:00Z", "2026-11-12T18:20:00Z",
                                 "2026-11-13T09:00:00Z", "2026-11-13T15:30:00Z"])
def test_light_build_gives_the_same_signal_as_the_full_build(rec_ds, cut):
    """Bulk evaluation skips liquidity/FVG/OB on H1 and M1 (the rules never read them)."""
    when = datetime.fromisoformat(cut.replace("Z", "+00:00"))
    frames = replay.frames_at(rec_ds, when)
    kw = dict(mode="replay", series="rec", market_state=None, spread=0.2, spread_source="t", source="s", generated_at=when)
    full = signals.evaluate(multi_timeframe.build(frames, when, default_config()), when, default_config(), **kw)
    light = signals.evaluate(multi_timeframe.build(frames, when, default_config(), detail=signals.DETAIL_TIMEFRAMES),
                             when, default_config(), **kw)
    assert full == light
    if cut.startswith("2026-11-12T1"):                     # these cut-offs use an M15 zone as evidence
        assert any("zone" in e["rule"] and e["timeframe"] == "M15" for e in full["supporting"] + full["conflicting"])
