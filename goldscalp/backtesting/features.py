"""Feature snapshots (definition features@1). PHASE3_ARCHITECTURE.md section 4.

A snapshot is a compact, JSON-serializable summary of everything strategy conditions may read at one
step cut-off. It is built from the Phase 2 as-of pass, so it uses ONLY candles closed by the cut-off:

    replay.frames_at(dataset, t) -> multi_timeframe.build(detail=...) -> signals.evaluate -> snapshot(t)

One timeline (a snapshot per step) is shared by every strategy of a backtest, comparison or walk-forward
run, so strategies are compared on identical inputs. Timelines are cached in memory by a key that
includes a hash of the candles used.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldscalp.analysis import multi_timeframe, signals
from goldscalp.analysis import sessions as sessions_mod
from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.replay import ReplayDataset, frames_at
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.resample import resample

VERSION = "features@1"
EVENTS_KEPT = 10
ZONES_KEPT = 12
ZONE_RECENT_CANDLES = 100
_CACHE: dict[tuple, list[dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_SIZE = 4


class Cancelled(Exception):
    pass


def candle_hash(frame: pd.DataFrame) -> str:
    """SHA-256 over the times and OHLC of the candles (reproducibility key)."""
    if frame is None or frame.empty:
        return "empty"
    h = hashlib.sha256()
    h.update(frame.index.as_unit("s").asi8.tobytes())
    h.update(np.ascontiguousarray(frame[["open", "high", "low", "close"]].to_numpy(dtype="float64")).tobytes())
    return h.hexdigest()


def dataset_hash(ds: ReplayDataset) -> str:
    parts = [candle_hash(ds.base)] + [f"{k}:{candle_hash(v)}" for k, v in sorted(ds.history.items())]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def step_cutoffs(ds: ReplayDataset, step_tf: Timeframe, start: datetime, end: datetime) -> list[pd.Timestamp]:
    """Close times of every step candle in [start, end] that exists in the base data."""
    window = ds.base[(ds.base.index >= pd.Timestamp(start)) & (ds.base.index < pd.Timestamp(end))]
    if window.empty or step_tf.seconds < ds.base_tf.seconds:
        return []
    bars = window if step_tf is ds.base_tf else resample(window, ds.base_tf, step_tf)
    step = pd.Timedelta(seconds=step_tf.seconds)
    return [ts + step for ts in bars.index if ts + step <= pd.Timestamp(end)]


EMA_PERIODS = (9, 20, 50, 100, 200)
RSI_PERIOD = 14


def indicators(bars: pd.DataFrame | None) -> dict[str, Any]:
    """Phase 5 (additive): EMAs of the close and Wilder RSI(14) on the timeframe's CLOSED candles only (the frame
    reaching the snapshot is already as-of the cut-off). An EMA needs 2 x its period of bars, RSI needs 50 bars."""
    out: dict[str, Any] = {"ema": {str(n): None for n in EMA_PERIODS}, "rsi14": None}
    if bars is None or bars.empty:
        return out
    closed = bars[bars["closed"]] if "closed" in bars else bars
    close = closed["close"].astype("float64")
    for n in EMA_PERIODS:
        if len(close) >= 2 * n:
            out["ema"][str(n)] = round(float(close.ewm(span=n, adjust=False).mean().iloc[-1]), 4)
    if len(close) >= 50:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean().iloc[-1]
        out["rsi14"] = 100.0 if loss == 0 else round(float(100 - 100 / (1 + gain / loss)), 3)
    out["close"] = round(float(close.iloc[-1]), 4) if len(close) else None
    return out


def _zones(items: list[dict[str, Any]], kind: str, price: float | None, cutoff: pd.Timestamp,
           tf: Timeframe) -> list[dict[str, Any]]:
    horizon = cutoff - pd.Timedelta(seconds=tf.seconds * ZONE_RECENT_CANDLES)
    keep = []
    for z in items:
        if (kind == "fvg" and z["mitigation"] == "fully_mitigated") or (kind == "ob" and z["mitigation"] == "invalidated"):
            continue
        inside = price is not None and z["zone"]["bottom"] <= price <= z["zone"]["top"]
        if inside or pd.Timestamp(z["known_at"]) > horizon:
            keep.append({"direction": z["direction"], "top": z["zone"]["top"], "bottom": z["zone"]["bottom"],
                         "known_at": z["known_at"], "mitigation": z["mitigation"], "id": z["id"]})
    keep.sort(key=lambda z: z["known_at"])
    return keep[-ZONES_KEPT:]


def snapshot(results: dict[str, Any], cutoff: datetime, cfg: AnalysisConfig, *, base: pd.DataFrame | None,
             signal: dict[str, Any] | None) -> dict[str, Any]:
    cut = pd.Timestamp(cutoff)
    price = float(base["close"].iloc[-1]) if base is not None and len(base) else None
    spread = None
    if base is not None and len(base) and "spread" in base:
        value = base["spread"].iloc[-1]
        spread = float(value) if value is not None and np.isfinite(value) and value > 0 else None
    tfs: dict[str, Any] = {}
    for key, r in results.items():
        tf = Timeframe.parse(key)
        st, vol, fr = r["structure"], r["volatility"], r["freshness"]
        bars = 0 if r["bars"] is None else len(r["bars"])
        item: dict[str, Any] = {
            "bars": bars, "fresh": bool(fr["fresh"]), "enough": bars >= int(cfg["signals"]["min_bars"][key]),
            "problems": signals.candle_problems(r["bars"], key),
            "state": st["state"] if st["bars_used"] else "no data",
            "high_label": st["last_high_label"], "low_label": st["last_low_label"],
            "ref_high": (st["reference_high"] or {}).get("price"), "ref_low": (st["reference_low"] or {}).get("price"),
            "events": [{"type": e["type"], "direction": e["direction"], "price": e["price"], "known_at": e["known_at"]}
                       for e in st["events"][-EVENTS_KEPT:]],
            "vol": {"condition": vol.get("condition"), "atr": vol.get("atr"), "atr_pct": vol.get("atr_pct"),
                    "ratio": vol.get("fast_slow_ratio")},
            "last_close": vol.get("last_close"), "detail": bool(r.get("detail", True)),
            "ind": indicators(r["bars"]),
        }
        if item["detail"]:
            item["sweeps"] = [{"type": e["type"], "direction": e["direction"], "level_type": e["level_type"],
                               "price": e["price"], "known_at": e["known_at"]}
                              for e in r["liquidity"]["events"] if e["status"] == "confirmed"][-EVENTS_KEPT:]
            item["fvg"] = _zones(r["fvg"]["gaps"], "fvg", price, cut, tf)
            item["ob"] = _zones(r["order_blocks"]["candidates"], "ob", price, cut, tf)
        tfs[key] = item
    return {"definition": VERSION, "cutoff": iso(cut.to_pydatetime()), "price": price, "spread": spread,
            "sessions": sessions_mod.active_names(sessions_mod.definitions(cfg), cut.to_pydatetime()),
            "signal": None if signal is None else {"direction": signal["direction"], "score": signal["score"],
                                                   "id": signal["id"]},
            "tf": tfs}


def compute_timeline(ds: ReplayDataset, step_tf: Timeframe, cutoffs: list[pd.Timestamp], cfg: AnalysisConfig, *,
                     detail: set[str], progress: Callable[[int], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """One snapshot per cut-off. Cached by dataset content, step timeframe, cut-offs, detail and config."""
    detail_all = tuple(sorted(set(detail) | set(signals.DETAIL_TIMEFRAMES)))
    # The series and source are part of the key: snapshots embed series-specific signal ids, so two series with
    # identical candles (e.g. an MT5 export of the same bars) must never share a cached timeline.
    key = (ds.series_key, ds.source_id, dataset_hash(ds), ds.base_tf.value, step_tf.value,
           iso(cutoffs[0].to_pydatetime()) if cutoffs else "",
           iso(cutoffs[-1].to_pydatetime()) if cutoffs else "", len(cutoffs), detail_all, cfg.fingerprint)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        if progress is not None:
            progress(len(cutoffs))
        return hit
    out: list[dict[str, Any]] = []
    for n, cut in enumerate(cutoffs, 1):
        if cancelled is not None and cancelled():
            raise Cancelled("cancelled")
        cutoff = cut.to_pydatetime()
        frames = frames_at(ds, cutoff)
        results = multi_timeframe.build(frames, cutoff, cfg, detail=detail_all)
        base = frames.get(ds.base_tf.value)
        spread = None
        if base is not None and len(base):
            value = base["spread"].iloc[-1]
            spread = float(value) if value is not None and np.isfinite(value) and value > 0 else None
        sig = signals.evaluate(results, cutoff, cfg, mode="replay", series=ds.series_key, market_state=None,
                               spread=spread, spread_source="historical candle spread", source=ds.source_id,
                               generated_at=cutoff, replay_id=f"bt:{step_tf.value}")
        out.append(snapshot(results, cutoff, cfg, base=base, signal=sig))
        if progress is not None:
            progress(n)
    with _CACHE_LOCK:
        while len(_CACHE) >= CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = out
    return out


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
