"""One as-of analysis pass shared by the live dashboard and the replay engine.

``run(frames, as_of, ...)`` -> chart overlays for one timeframe, the multi-timeframe summary, sessions,
volatility and the current rule-based signal. Every engine sees only candles closed by ``as_of``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.analysis import liquidity, market_structure, multi_timeframe, order_blocks, sessions, signals, volatility
from goldscalp.analysis.config import AnalysisConfig
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import Timeframe

LABEL = ("Algorithmic observations from closed candles (historical observations, not predictions). "
         "Zones are candidates, sweeps are potential sweeps, and the signal is a rule-based evidence summary.")
FRAME_LIMITS = {"M1": 1500, "M5": 1000, "M15": 600, "H1": 400}
STRUCTURE_EVENTS_SHOWN = 40
SWINGS_SHOWN = 60


def run(frames: dict[str, pd.DataFrame], as_of: datetime, cfg: AnalysisConfig, *, chart_tf: Timeframe, mode: str,
        series: str, market_state: str | None, spread: float | None, spread_source: str, source: str | None,
        generated_at: datetime, replay_id: str | None = None,
        display_swing_strength: int | None = None) -> dict[str, Any]:
    """``display_swing_strength`` changes the swing strength of the CHART overlays only; the
    multi-timeframe summary and the signal always use ``cfg`` (config/analysis.json)."""
    as_of = ensure_aware(as_of)
    results = multi_timeframe.build(frames, as_of, cfg)
    summary = multi_timeframe.summarise(results, as_of)
    session_key = "M5" if "M5" in frames and len(frames["M5"]) else "M1"
    m5_atr = results.get("M5", {}).get("volatility", {}).get("atr") if "M5" in results else None
    sess = None
    if session_key in frames:
        sess = sessions.analyse(frames[session_key], Timeframe.parse(session_key), as_of, cfg,
                                history=frames.get("H1"), history_tf=Timeframe.H1 if "H1" in frames else None,
                                atr_value=m5_atr)
    vol = volatility.analyse(frames, as_of, cfg, daily_source=frames.get("H1"),
                             daily_tf=Timeframe.H1 if "H1" in frames else None, sessions_result=sess)
    signal = signals.evaluate(results, as_of, cfg, mode=mode, series=series, market_state=market_state,
                              spread=spread, spread_source=spread_source, source=source,
                              generated_at=generated_at, sessions_result=sess, replay_id=replay_id)
    chart = results.get(chart_tf.value)
    display = None
    configured = int(cfg["structure"]["swing_strength"][chart_tf.value])
    if chart is not None and display_swing_strength and int(display_swing_strength) != configured:
        chart = chart_overlays_with(frames, chart_tf, as_of, cfg.with_swing_strength(chart_tf.value,
                                                                                    display_swing_strength), chart)
        display = {"swing_strength": int(display_swing_strength), "configured_swing_strength": configured,
                   "note": "display sensitivity: changes the chart overlays only; the signal uses the configuration"}
    ov = overlays(chart) if chart else None
    if ov is not None:
        ov["display"] = display or {"swing_strength": configured, "configured_swing_strength": configured}
    return {
        "as_of": iso(as_of), "chart_tf": chart_tf.value, "label": LABEL,
        "overlays": ov,
        "multi_timeframe": summary, "sessions": sess, "volatility": vol, "signal": signal,
        "definitions": signals.DEFINITIONS, "config_fingerprint": cfg.fingerprint,
    }


def chart_overlays_with(frames: dict[str, pd.DataFrame], tf: Timeframe, as_of: datetime, dcfg: AnalysisConfig,
                        base: dict[str, Any]) -> dict[str, Any]:
    """Structure-dependent overlays of one timeframe recomputed with a display configuration."""
    frame = frames[tf.value]
    h1 = frames.get("H1")
    st = market_structure.analyse(frame, tf, as_of, dcfg, limit=0)
    return {**base, "structure": st,
            "liquidity": liquidity.analyse(frame, tf, as_of, dcfg, structure=st, reference=h1,
                                           reference_tf=Timeframe.H1 if h1 is not None else None),
            "order_blocks": order_blocks.analyse(frame, tf, as_of, dcfg, structure=st, limit=0)}


def overlays(r: dict[str, Any]) -> dict[str, Any]:
    st, liq, gaps, obs = r["structure"], r["liquidity"], r["fvg"], r["order_blocks"]
    return {
        "timeframe": r["timeframe"],
        "structure": {"state": st["state"], "state_explanation": st["state_explanation"],
                      "events": st["events"][-STRUCTURE_EVENTS_SHOWN:], "swings": st["swings"][-SWINGS_SHOWN:],
                      "developing": st["developing"], "reference_high": st["reference_high"],
                      "reference_low": st["reference_low"], "definition": st["definition"], "params": st["params"]},
        "liquidity": {"levels": liq["levels"], "events": liq["events"][-30:], "developing": liq["developing"],
                      "nearest_above": liq["nearest_above"], "nearest_below": liq["nearest_below"],
                      "definition": liq["definition"], "params": liq["params"]},
        "fvg": {"open": gaps["open"], "recent_filled": [g for g in gaps["gaps"]
                                                        if g["mitigation"] == "fully_mitigated"][-10:],
                "developing": gaps["developing"], "filtered_small": gaps["filtered_small"],
                "merged": gaps["merged"], "definition": gaps["definition"], "params": gaps["params"]},
        "order_blocks": {"open": obs["open"], "skipped": obs["skipped"], "label": obs["label"],
                         "recent_invalidated": [o for o in obs["candidates"] if o["mitigation"] == "invalidated"][-10:],
                         "definition": obs["definition"], "params": obs["params"]},
        "freshness": r["freshness"], "last_close": r["volatility"].get("last_close"),
    }
