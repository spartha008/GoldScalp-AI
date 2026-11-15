"""Phase 2 analysis engines: golden hand-worked cases, no look-ahead (truncation invariance),
sweep-vs-break classification, FVG mitigation, order-block links, sessions, volatility, MTF wording."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, UTC

import numpy as np
import pandas as pd
import pytest

from goldscalp.analysis import fvg, liquidity, market_structure, multi_timeframe, order_blocks, sessions, volatility
from goldscalp.analysis.candles import closed_view
from goldscalp.analysis.config import AnalysisConfig, AnalysisConfigError, _reference, default_config
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample
from tests.conftest import synthetic_m1

START = pd.Timestamp("2026-11-11T14:00Z")
M1 = Timeframe.M1
M5 = Timeframe.M5


def bars(rows, start=START, minutes=1) -> pd.DataFrame:
    """rows: (open, high, low, close) tuples."""
    idx = pd.date_range(start, periods=len(rows), freq=f"{minutes}min", tz="UTC")
    o, h, low, c = (np.array([r[k] for r in rows], dtype=float) for k in range(4))
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "tick_volume": 1, "spread": 0.3,
                         "closed": True}, index=idx)


def from_closes(closes, highs=None, lows=None, start=START) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(opens, closes) + 0.5
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(opens, closes) - 0.5
    return bars(list(zip(opens, highs, lows, closes, strict=True)), start)


def at(minutes: int) -> datetime:
    return (START + pd.Timedelta(minutes=minutes)).to_pydatetime()


def z(minutes: int) -> str:
    return iso(at(minutes))


def cfg_with(**changes) -> AnalysisConfig:
    data = copy.deepcopy(_reference())
    for path, value in changes.items():
        section, key = path.split("__")
        data[section][key] = value
    return AnalysisConfig(data)


@pytest.fixture(scope="module")
def m5_frame():
    from goldscalp.simulation.instrument import InstrumentSpec
    from tests.conftest import SPEC_PATH
    schedule = InstrumentSpec.load(SPEC_PATH).schedule
    m1 = synthetic_m1("2026-11-09T00:00Z", "2026-11-11T15:00Z", schedule, seed=5)
    as_of = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    return resample(m1, M1, M5, as_of=as_of), as_of


def cut_points(frame, step=37, first=40):
    for k in range(first, len(frame), step):
        yield k, (frame.index[k - 1] + pd.Timedelta(minutes=5)).to_pydatetime()


# ------------------------------------------------------------------------------- config
def test_config_is_strict():
    data = copy.deepcopy(_reference())
    data["structure"]["unknown"] = 1
    with pytest.raises(AnalysisConfigError, match="unknown"):
        AnalysisConfig(data)
    data = copy.deepcopy(_reference())
    del data["fvg"]["min_size_abs"]
    with pytest.raises(AnalysisConfigError, match="missing"):
        AnalysisConfig(data)
    with pytest.raises(AnalysisConfigError, match="fast_atr"):
        cfg_with(volatility__fast_atr=60)
    with pytest.raises(AnalysisConfigError, match="zone"):
        cfg_with(order_blocks__zone="wick")
    with pytest.raises(AnalysisConfigError, match="whole number"):
        cfg_with(structure__dedupe_bars=2.5)


def test_config_fingerprint_changes_with_values():
    assert default_config().fingerprint == default_config().fingerprint
    assert cfg_with(fvg__min_size_abs=0.2).fingerprint != default_config().fingerprint


# ------------------------------------------------------------------------------- as-of rule
def test_closed_view_drops_forming_and_future_bars():
    frame = from_closes([100, 101, 102, 103, 104])
    frame.loc[frame.index[-1], "closed"] = False
    assert len(closed_view(frame, M1, None)) == 4
    assert len(closed_view(frame, M1, at(3))) == 3            # bar 3 (14:03) ends at 14:04 > 14:03
    assert list(closed_view(frame, M1, at(3)).index) == list(frame.index[:3])


def test_resample_as_of_keeps_a_partial_hour_forming():
    m1 = from_closes(np.linspace(100, 101, 30))               # 14:00-14:29 closed M1 bars
    h1 = resample(m1, M1, Timeframe.H1, as_of=at(30))
    assert len(h1) == 1 and not bool(h1["closed"].iloc[0])     # the 14:00 hour has not ended
    full = resample(from_closes(np.linspace(100, 101, 60)), M1, Timeframe.H1, as_of=at(60))
    assert bool(full["closed"].iloc[0])


# ------------------------------------------------------------------------------- structure
GOLDEN = [100, 101, 102, 104, 103, 102, 101, 103, 105, 103, 99, 100, 101, 102, 100, 97]


def test_structure_golden_case():
    frame = from_closes(GOLDEN, highs=np.array(GOLDEN) + 0.5, lows=np.array(GOLDEN) - 0.5)
    out = market_structure.analyse(frame, M1, at(16), limit=0)
    assert [(e["type"], e["direction"], e["price"]) for e in out["events"]] == [
        ("BOS", "bullish", 104.5), ("CHoCH", "bearish", 100.5), ("BOS", "bearish", 98.5)]
    assert out["events"][1]["known_at"] == z(11)               # close of the 14:10 candle
    assert out["events"][0]["explanation"].endswith("the first structure break in the available candles")
    assert [(s["label"], s["price"], s["known_at"]) for s in out["swings"]] == [
        ("H", 104.5, z(7)), ("L", 100.5, z(10)), ("HH", 105.5, z(12)), ("LL", 98.5, z(14))]
    assert out["state"] == "bearish"
    for e in out["events"]:
        assert e["status"] == "confirmed" and e["candles"][-1] == f"M1@{e['time']}" and e["timeframe"] == "M1"


def test_structure_wick_through_a_swing_is_not_a_break():
    closes = GOLDEN[:8] + [104.4]                              # close stays below the 104.5 swing high
    highs = np.array(closes) + 0.5
    highs[-1] = 106.0                                          # the wick pierces it
    out = market_structure.analyse(from_closes(closes, highs=highs, lows=np.array(closes) - 0.5), M1, at(9), limit=0)
    assert out["events"] == []


def test_structure_developing_break_comes_only_from_the_forming_candle():
    frame = from_closes(GOLDEN[:8] + [106.0], highs=np.array(GOLDEN[:8] + [106.0]) + 0.5,
                        lows=np.array(GOLDEN[:8] + [106.0]) - 0.5)
    frame.loc[frame.index[-1], "closed"] = False
    out = market_structure.analyse(frame, M1, at(8) + timedelta(seconds=30), limit=0)
    assert out["events"] == []
    dev = [d for d in out["developing"] if d["type"] == "developing_break"]
    assert len(dev) == 1 and dev[0]["status"] == "developing" and dev[0]["known_at"] is None


def test_structure_bos_dedupe_folds_near_duplicates():
    # Two bullish breaks of nearly the same level within a few candles: the second is folded.
    cfg = cfg_with(structure__dedupe_atr=100.0)
    closes = [100, 101, 102, 104, 103, 102, 101, 103, 105, 104.4, 104.3, 104.2, 104.1, 104.0, 106]
    out = market_structure.analyse(from_closes(closes, highs=np.array(closes) + 0.5, lows=np.array(closes) - 0.5),
                                   M1, at(len(closes)), cfg, limit=0)
    bos = [e for e in out["events"] if e["direction"] == "bullish"]
    assert len(bos) == 1 and bos[0]["folded_breaks"] == 1 and out["dedupe_folded"] == 1


def test_structure_has_no_lookahead(m5_frame):
    frame, as_of = m5_frame
    full = market_structure.analyse(frame, M5, as_of, limit=0)
    strip = lambda es: [{k: v for k, v in e.items() if k != "folded_breaks"} for e in es]   # noqa: E731
    for k, cut in cut_points(frame):
        part = market_structure.analyse(frame.iloc[:k], M5, cut, limit=0)
        cutoff = iso(cut)
        assert strip(part["events"]) == strip([e for e in full["events"] if e["known_at"] <= cutoff])
        assert [s["id"] for s in part["swings"]] == [s["id"] for s in full["swings"] if s["known_at"] <= cutoff]
        # passing FUTURE candles with an earlier as_of gives the same result as truncating
        same = market_structure.analyse(frame, M5, cut, limit=0)
        assert strip(same["events"]) == strip(part["events"])


# ------------------------------------------------------------------------------- liquidity
EQ_HIGHS = [100, 101, 102, 105, 102, 101, 100, 101, 102, 103, 105.05, 103, 102, 101, 100, 99]


def eq_frame(extra=()):
    rows = [(h - 0.6, h, h - 1.0, h - 0.5) for h in EQ_HIGHS] + list(extra)
    return bars(rows)


def test_equal_highs_golden():
    out = liquidity.analyse(eq_frame(), M1, at(16))
    eqh = [lv for lv in out["levels"] if lv["type"] == "EQH"]
    assert len(eqh) == 1
    level = eqh[0]
    assert level["price"] == 105.05 and level["known_at"] == z(14)     # second swing confirmed at the 14:13 close
    assert level["members"] == [z(3), z(10)] and level["status"] == "untouched"
    assert level["side"] == "buy-side"


def test_equal_high_sweep_needs_a_close_back_below():
    out = liquidity.analyse(eq_frame([(99.5, 105.3, 99.4, 104.8)]), M1, at(17))
    lv = next(lv for lv in out["levels"] if lv["type"] == "EQH")
    sweep = lv["interaction"]
    assert lv["status"] == "swept" and sweep["sweep_type"] == "confirmed_rejection"
    assert sweep["direction"] == "bearish" and sweep["known_at"] == z(17) and sweep["candles"] == [f"M1@{z(16)}"]
    assert "Potential bearish liquidity sweep" in sweep["label"]


LEVEL = {"id": "t:EQH", "type": "EQH", "label": "Equal highs", "price": 100.0}


@pytest.mark.parametrize("rows, expected", [
    ([(99, 100.5, 98.9, 99.8)], ("sweep", "confirmed_rejection", "confirmed", 1)),
    ([(99, 100.6, 98.9, 100.3)], ("break", None, "developing", None)),
    ([(99, 100.6, 98.9, 100.3), (100.3, 100.4, 99.5, 99.9)], ("sweep", "delayed_rejection", "confirmed", 2)),
    ([(99, 100.6, 98.9, 100.3), (100.3, 100.4, 99.5, 100.0)], ("sweep", "delayed_rejection", "confirmed", 2)),
    ([(99, 100.6, 98.9, 100.3)] + [(100.3, 101, 100.1, 100.5)] * 3, ("break", None, "confirmed", 4)),
    ([(99, 100.6, 98.9, 100.3)] + [(100.3, 101, 100.1, 100.5)] * 2, ("break", None, "developing", None)),
    ([(99, 99.9, 98.9, 99.5)], None),
])
def test_sweep_versus_break_classification(rows, expected):
    result = liquidity.classify(LEVEL, bars(rows), M1, return_bars=3)
    if expected is None:
        assert result is None
        return
    typ, sweep_type, status, known_bars = expected
    assert (result["type"], result["sweep_type"], result["status"]) == (typ, sweep_type, status)
    assert result["known_at"] == (z(known_bars) if known_bars else None)


def test_liquidity_has_no_lookahead(m5_frame):
    frame, as_of = m5_frame
    full = liquidity.analyse(frame, M5, as_of)
    full_eq = {lv["id"]: lv for lv in liquidity._equal_levels(
        closed_view(frame, M5, as_of), M5, market_structure.analyse(frame, M5, as_of, limit=0)["swings"],
        __import__("goldscalp.analysis.candles", fromlist=["atr_series"]).atr_series(closed_view(frame, M5, as_of), 14),
        default_config()["liquidity"])}
    assert full["levels"]
    for k, cut in cut_points(frame, step=53, first=60):
        part = liquidity.analyse(frame.iloc[:k], M5, cut)
        cutoff = iso(cut)
        for lv in part["levels"]:
            assert lv["known_at"] <= cutoff
            if lv["type"] in ("EQH", "EQL") and lv["id"] in full_eq:
                assert lv["price"] == full_eq[lv["id"]]["price"]
            got = lv["interaction"]
            if got is None:
                continue
            assert got["status"] == "developing" or got["known_at"] <= cutoff
            later = liquidity.classify(lv, frame[frame.index >= pd.Timestamp(lv["known_at"])], M5, 3)
            if got["status"] == "confirmed":                     # a confirmed interaction never changes later
                assert later["id"] == got["id"] and later["known_at"] == got["known_at"]


def test_previous_day_and_week_levels_use_the_17h_new_york_rollover():
    from goldscalp.simulation.instrument import InstrumentSpec
    from tests.conftest import SPEC_PATH
    schedule = InstrumentSpec.load(SPEC_PATH).schedule
    m1 = synthetic_m1("2026-11-01T22:00Z", "2026-11-11T15:00Z", schedule, seed=8)
    as_of = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    h1 = resample(m1, M1, Timeframe.H1, as_of=as_of)
    out = liquidity.analyse(h1, Timeframe.H1, as_of)
    by = {lv["type"]: lv for lv in out["levels"] if lv["type"] in ("PDH", "PDL", "PWH", "PWL")}
    day = h1[(h1.index >= pd.Timestamp("2026-11-09T22:00Z")) & (h1.index < pd.Timestamp("2026-11-10T22:00Z"))]
    assert by["PDH"]["price"] == round(day["high"].max(), 5) and by["PDH"]["known_at"] == "2026-11-10T22:00:00Z"
    assert by["PDL"]["price"] == round(day["low"].min(), 5)
    week = h1[(h1.index >= pd.Timestamp("2026-11-01T22:00Z")) & (h1.index < pd.Timestamp("2026-11-06T22:00Z"))]
    assert by["PWH"]["price"] == round(week["high"].max(), 5) and by["PWH"]["known_at"] == "2026-11-06T22:00:00Z"


# ------------------------------------------------------------------------------- FVG
FVG_ROWS = [(100, 101, 99.5, 100.8), (100.8, 103.5, 100.7, 103.3), (103.3, 104, 101.6, 103.8),
            (103.8, 104.2, 101.4, 102.0), (102.0, 102.5, 100.9, 101.2)]


def test_fvg_golden_and_mitigation_progression():
    frame = bars(FVG_ROWS)
    states = []
    for minutes in (3, 4, 5):
        out = fvg.analyse(frame, M1, at(minutes), limit=0)
        assert len(out["gaps"]) == 1
        g = out["gaps"][0]
        assert g["zone"] == {"top": 101.6, "bottom": 101.0} and g["direction"] == "bullish"
        assert g["time"] == z(1) and g["known_at"] == z(3)
        assert g["candles"] == [f"M1@{z(0)}", f"M1@{z(1)}", f"M1@{z(2)}"]
        states.append((g["mitigation"], g["mitigated_pct"], g["fully_mitigated_at"]))
    assert states == [("unmitigated", 0.0, None), ("partially_mitigated", 33.3, None), ("fully_mitigated", 100.0, z(5))]


def test_fvg_minimum_size_and_dedupe():
    tiny = [list(r) for r in FVG_ROWS[:3]]
    tiny[2][2] = 101.05                                        # gap of 0.05 < min_size_abs 0.10
    out = fvg.analyse(bars([tuple(r) for r in tiny]), M1, at(3))
    assert out["gaps"] == [] and out["filtered_small"] == 1
    overlap = [(100, 101, 99.5, 100.8), (100.8, 101.5, 100.9, 101.4), (101.4, 102.5, 101.6, 102.4),
               (102.4, 103, 101.7, 102.9)]
    out = fvg.analyse(bars(overlap), M1, at(4), limit=0)
    assert len(out["gaps"]) == 1 and out["merged"] == 1 and out["gaps"][0]["merged_gaps"] == 1


def test_fvg_has_no_lookahead(m5_frame):
    frame, as_of = m5_frame
    full = fvg.analyse(frame, M5, as_of, limit=0)["gaps"]
    for k, cut in cut_points(frame):
        cutoff = iso(cut)
        part = fvg.analyse(frame.iloc[:k], M5, cut, limit=0)["gaps"]
        expected = [g for g in full if g["known_at"] <= cutoff]
        assert [g["id"] for g in part] == [g["id"] for g in expected]
        for got, ref in zip(part, expected, strict=True):
            for key in ("first_touch_at", "fully_mitigated_at"):
                assert got[key] == (ref[key] if ref[key] and ref[key] <= cutoff else None)


# ------------------------------------------------------------------------------- order blocks
def ob_rows():
    rows, prev = [], 100.0
    for i in range(20):                                        # flat warm-up: no swings, ATR defined
        close = 100.0 if i % 2 else 100.2
        rows.append((prev, max(prev, close) + 0.1, min(prev, close) - 0.1, close))
        prev = close
    rows += [(100.2, 101.1, 100.1, 101.0), (101.0, 102.1, 100.9, 102.0), (102.0, 102.5, 101.4, 101.5),
             (101.5, 101.6, 100.9, 101.0), (101.0, 101.1, 100.5, 100.6), (100.6, 101.3, 100.55, 101.2),
             (101.2, 104.1, 101.1, 104.0), (104.0, 104.6, 103.9, 104.5)]
    return rows


def test_order_block_candidate_is_linked_to_its_break():
    frame = bars(ob_rows())
    out = order_blocks.analyse(frame, M1, at(28), limit=0)
    st = market_structure.analyse(frame, M1, at(28), limit=0)
    assert [(e["type"], e["direction"], e["time"]) for e in st["events"]] == [("BOS", "bullish", z(26))]
    (ob,) = [o for o in out["candidates"] if o["direction"] == "bullish"]
    assert ob["time"] == z(24) and ob["zone"] == {"top": 101.1, "bottom": 100.5}
    assert ob["structure_event"] == st["events"][0]["id"] and ob["known_at"] == z(27)
    assert ob["mitigation"] == "untouched" and "candidate" in ob["label"] and ob["candidate_label"] == "algorithmic candidate"
    assert out["label"] == "algorithmic candidate"


def test_order_block_touch_then_invalidation():
    frame = bars(ob_rows() + [(104.5, 104.6, 100.9, 101.0), (101.0, 101.1, 100.2, 100.3)])
    (ob,) = [o for o in order_blocks.analyse(frame, M1, at(30), limit=0)["candidates"] if o["direction"] == "bullish"]
    assert ob["touched_at"] == z(29) and ob["invalidated_at"] == z(30) and ob["mitigation"] == "invalidated"
    before = order_blocks.analyse(frame, M1, at(29), limit=0)
    (ob_then,) = [o for o in before["candidates"] if o["direction"] == "bullish"]
    assert ob_then["mitigation"] == "touched" and ob_then["invalidated_at"] is None


def test_order_block_needs_displacement():
    out = order_blocks.analyse(bars(ob_rows()), M1, at(28), cfg_with(order_blocks__displacement_atr=50.0), limit=0)
    assert out["candidates"] == [] and out["skipped"]["weak_displacement"] >= 1


def test_order_blocks_have_no_lookahead(m5_frame):
    frame, as_of = m5_frame
    full = order_blocks.analyse(frame, M5, as_of, limit=0)["candidates"]
    assert full
    for k, cut in cut_points(frame):
        cutoff = iso(cut)
        part = order_blocks.analyse(frame.iloc[:k], M5, cut, limit=0)["candidates"]
        assert [(o["id"], o["zone"]) for o in part] == [(o["id"], o["zone"]) for o in full if o["known_at"] <= cutoff]


# ------------------------------------------------------------------------------- sessions
def test_configured_sessions_follow_each_zone_dst():
    london, new_york = (d for d in sessions.definitions(default_config()) if d.key in ("london", "new_york"))
    assert london.window(datetime(2026, 3, 16).date())[0] == datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
    assert new_york.window(datetime(2026, 3, 16).date())[0] == datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    assert london.window(datetime(2026, 7, 15).date())[0] == datetime(2026, 7, 15, 7, 0, tzinfo=UTC)


def test_sessions_current_overlap_and_next():
    frame = bars([(100, 100.5, 99.5, 100.2)] * 12, start=pd.Timestamp("2026-11-11T14:00Z"), minutes=5)
    out = sessions.analyse(frame, M5, datetime(2026, 11, 11, 15, 0, tzinfo=UTC), default_config())
    assert out["current"] == "London / New York" and out["overlap"] is True
    ldn_ny = next(o for o in out["overlaps"] if o["sessions"] == ["London", "New York"])
    assert (ldn_ny["start"], ldn_ny["end"], ldn_ny["status"]) == ("2026-11-11T13:00:00Z", "2026-11-11T17:00:00Z", "active")
    later = sessions.analyse(frame, M5, datetime(2026, 11, 11, 23, 30, tzinfo=UTC), default_config())
    assert later["active"] == [] and later["next"] == {"name": "Asian", "start": "2026-11-12T00:00:00Z"}
    assert "never assumed to be UTC" in out["timezone_note"]


def test_london_close_above_the_asian_high_is_a_breakout_observation():
    rows = [(100.5, 101.0, 100.0, 100.5)] * 114                 # 00:00-09:25 UTC, Asian high 101.0
    rows += [(100.5, 101.6, 100.4, 101.5)]                       # 09:30 close 101.5 > 101.0
    rows += [(101.5, 101.6, 101.4, 101.5)] * 5
    frame = bars(rows, start=pd.Timestamp("2026-11-11T00:00Z"), minutes=5)
    out = sessions.analyse(frame, M5, datetime(2026, 11, 11, 10, 0, tzinfo=UTC), default_config())
    hits = [b for b in out["breakouts"] if b["session"] == "London" and b["reference_session"] == "Asian"]
    assert len(hits) == 1
    b = hits[0]
    assert (b["direction"], b["price"], b["time"], b["known_at"]) == ("bullish", 101.0, "2026-11-11T09:30:00Z",
                                                                       "2026-11-11T09:35:00Z")
    assert "not a sweep" in b["explanation"]


# ------------------------------------------------------------------------------- volatility
def ranged(ranges):
    rows, price = [], 100.0
    for r in ranges:
        rows.append((price, price + r / 2, price - r / 2, price))
    return bars(rows)


def test_volatility_expansion_and_contraction():
    cfg = default_config()
    expanding = volatility.timeframe_profile(ranged([1.0] * 60 + [4.0] * 6), cfg)
    assert expanding["condition"] == "expanding" and expanding["fast_slow_ratio"] >= 1.3
    contracting = volatility.timeframe_profile(ranged([2.0] * 60 + [0.4] * 10), cfg)
    assert contracting["condition"] == "contracting" and contracting["fast_slow_ratio"] <= 0.75
    normal = volatility.timeframe_profile(ranged([1.0] * 70), cfg)
    assert normal["condition"] == "normal" and normal["atr"] == 1.0
    assert normal["atr_pct"] == 1.0 and normal["last_range_pct"] == 1.0          # 1.0 on a 100 price


def test_adr_uses_complete_trading_days_only():
    idx = pd.date_range("2026-11-09T23:00Z", "2026-11-11T14:00Z", freq="h", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "tick_volume": 1,
                          "spread": 0.3, "closed": True}, index=idx)
    frame.loc[frame.index >= pd.Timestamp("2026-11-10T23:00Z"), ["high", "low"]] = [110.0, 90.0]   # today: wide
    out = volatility.analyse({"H1": frame}, datetime(2026, 11, 11, 15, 0, tzinfo=UTC), default_config(),
                             daily_source=frame, daily_tf=Timeframe.H1)
    assert out["adr"]["days_used"] == 1 and out["adr"]["adr"] == 2.0 and out["adr"]["today_range"] == 20.0


# ------------------------------------------------------------------------------- multi-timeframe wording
def test_mtf_disagreement_explanations():
    text = multi_timeframe.explain_disagreements({"H1": "bullish", "M15": "bullish", "M5": "bearish", "M1": "bearish"})
    assert text == ["M5 bearish against bullish M15: a pullback within the M15 up-trend, or an early sign of a "
                    "reversal. Only an M15 bearish CHoCH would confirm a reversal on M15."]
    aligned = multi_timeframe.explain_disagreements({"H1": "bearish", "M15": "bearish", "M5": "bearish"})
    assert aligned[0].startswith("All available timeframes are bearish") and "not a forecast" in aligned[0]
    ranging = multi_timeframe.explain_disagreements({"H1": "ranging", "M15": "bullish"})
    assert ranging == ["H1 ranging while M15 is bullish: the M15 move has no H1 directional backing and may stay "
                       "inside the larger range."]


def test_mtf_build_excludes_the_forming_higher_timeframe_candle(m5_frame):
    frame, as_of = m5_frame
    cut = as_of - timedelta(minutes=20)                          # 14:40: the 14:00 hour is still forming
    h1 = resample(frame[frame.index < pd.Timestamp(cut)], M5, Timeframe.H1, as_of=cut)
    assert not bool(h1["closed"].iloc[-1])
    results = multi_timeframe.build({"H1": h1, "M5": frame[frame.index < pd.Timestamp(cut)]}, cut)
    assert results["H1"]["bars"].index[-1] < h1.index[-1]
    assert results["H1"]["freshness"]["forming_excluded"] is True
