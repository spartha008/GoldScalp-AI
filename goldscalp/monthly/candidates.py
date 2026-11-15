"""Phase 5 research concepts (PHASE5_ARCHITECTURE.md section 5).

Every candidate is expressed ONLY with GoldScalp's existing strategy@1 conditions, plus the two Phase 5 indicator
conditions (``ema_trend``, ``momentum``) computed from the same closed candles. Nothing here is an "internet
strategy" taken on faith: each concept states what it investigates, its limitations and its data needs, and is
tested on data before the evaluation month only.

Risk-to-reward (user requirement): every candidate's take-profit is an R multiple with two PRE-DECLARED variants,
1.5 R and 2.0 R (1:1.5 and 1:2). The research stage chooses one on development data before the month."""
from __future__ import annotations

import copy
from typing import Any

from goldscalp.strategies import builder

RR_VARIANTS = (1.5, 2.0)
CANDIDATE_VERSION = 1


def _base(name: str, description: str, conditions: list[dict[str, Any]], *, stop: dict[str, Any],
          max_hold: int) -> dict[str, Any]:
    return {"name": name, "description": description, "direction": "both", "evaluation": {"step_timeframe": "M5"},
            "conditions": conditions, "entry": {"trigger": "on_change", "max_entry_delay_minutes": 10},
            "exit": {"stop": stop, "target": {"type": "r_multiple", "r": 2.0}, "max_holding_minutes": max_hold},
            "risk": {"sizing": "fixed_lots", "lots": "0.02", "max_open_positions": 1}}


STRUCTURE_STOP = {"type": "structure", "timeframe": "M5", "buffer_atr": 0.1, "atr_multiple": 1.5}
ATR_STOP = {"type": "atr", "timeframe": "M5", "atr_multiple": 1.5}

CONCEPTS: list[dict[str, Any]] = [
    {"key": "R1", "concept": "Trend-following with moving-average structure",
     "config": _base("R1: EMA trend pullback", "M15 and H1 EMA structure with the trade, an M5 pullback and "
                     "momentum back in the trend direction.",
                     [{"id": "ema15", "type": "ema_trend", "timeframe": "M15", "fast": "20", "slow": "50", "mode": "both"},
                      {"id": "emah1", "type": "ema_trend", "timeframe": "H1", "fast": "20", "slow": "50",
                       "mode": "price_vs_slow"},
                      {"id": "pull", "type": "swing_sequence", "timeframe": "M5", "pattern": "pullback"},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 50, "mode": "confirm"}],
                     stop=STRUCTURE_STOP, max_hold=120),
     "documentation": {
         "entry": "Long when M15 close is above EMA50 and EMA20 is above EMA50, H1 close is above its EMA50, the last M5 "
                  "swing low is a higher low (pullback) and M5 RSI(14) is at least 50; mirrored for shorts.",
         "exit": "Take-profit at the variant's R multiple; stop at the last unbroken M5 swing (+0.1 ATR buffer); time "
                 "exit after 120 minutes.",
         "stop_loss": "M5 structure stop (ATR 1.5 fallback when no usable swing).",
         "take_profit": "1.5 R or 2.0 R (pre-declared variants).",
         "investigates": "Whether gold continues in the direction of an established intraday trend after a pullback.",
         "limitations": "EMAs lag; in ranges the stack flips often. Needs 100+ M15 and H1 candles for EMA50.",
         "data": "M5, M15 and H1 closed candles (EMA50 needs 100 bars per timeframe)."}},
    {"key": "R2", "concept": "Momentum breakout",
     "config": _base("R2: momentum breakout", "A fresh M5 break of structure with RSI momentum during London or New York "
                     "in a normal or expanding volatility regime.",
                     [{"id": "bos", "type": "structure_event", "timeframe": "M5", "event": "BOS", "direction": "with",
                       "within_candles": 2},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 60, "mode": "confirm"},
                      {"id": "vol", "type": "volatility_regime", "timeframe": "M5", "allowed": ["normal", "expanding"]},
                      {"id": "sess", "type": "session", "sessions": ["London", "New York"]}],
                     stop=ATR_STOP, max_hold=60),
     "documentation": {
         "entry": "Long when an M5 bullish BOS became known within 2 candles, M5 RSI(14) >= 60, M5 volatility is normal "
                  "or expanding and London or New York is open; mirrored for shorts (RSI <= 40).",
         "exit": "R-multiple take-profit, 1.5 x ATR(M5) stop, 60-minute time exit.",
         "stop_loss": "1.5 x ATR(M5) from the entry side.", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether breaks of structure with momentum follow through in liquid sessions.",
         "limitations": "Breakouts often retrace (false breaks); late entries after a 2-candle window.",
         "data": "M5 closed candles; session definitions."}},
    {"key": "R3", "concept": "Range-market reversion after a liquidity sweep",
     "config": _base("R3: range sweep reversion", "In an H1 range, trade back after a confirmed M5 liquidity sweep while "
                     "momentum is not stretched in the trade direction.",
                     [{"id": "range", "type": "structure_state", "timeframe": "H1", "state": "ranging"},
                      {"id": "sweep", "type": "liquidity_sweep", "timeframe": "M5", "within_candles": 6,
                       "level_types": ["EQ", "PD", "session"]},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 65, "mode": "not_extreme"}],
                     stop=STRUCTURE_STOP, max_hold=60),
     "documentation": {
         "entry": "Long when H1 structure is ranging, a bullish sweep of sell-side liquidity (equal lows, previous-day "
                  "or session lows) became known within 6 M5 candles and M5 RSI(14) <= 65; mirrored for shorts.",
         "exit": "R-multiple take-profit, M5 structure stop, 60-minute time exit.",
         "stop_loss": "M5 structure stop.", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Mean reversion after stop runs when the higher timeframe has no trend.",
         "limitations": "'Ranging' is a structure label, not a statistical range test; sweeps in trends fail.",
         "data": "H1 and M5 closed candles with liquidity analysis on M5."}},
    {"key": "R4", "concept": "Session breakout and retest",
     "config": _base("R4: session breakout retest", "An M15 break of structure in London or New York, entered on an M5 "
                     "retest of a same-direction fair value gap, with no higher timeframe against it.",
                     [{"id": "sess", "type": "session", "sessions": ["London", "New York"]},
                      {"id": "bos", "type": "structure_event", "timeframe": "M15", "event": "BOS", "direction": "with",
                       "within_candles": 4},
                      {"id": "fvg", "type": "fvg", "timeframe": "M5", "relation": "price_inside"},
                      {"id": "htf", "type": "htf_alignment", "timeframes": ["H1", "M15"], "min_agree": 1,
                       "none_against": True}],
                     stop=STRUCTURE_STOP, max_hold=90),
     "documentation": {
         "entry": "Long when London or New York is open, an M15 bullish BOS became known within 4 candles, price is "
                  "inside an open bullish M5 FVG (the retest) and neither H1 nor M15 is bearish; mirrored for shorts.",
         "exit": "R-multiple take-profit, M5 structure stop, 90-minute time exit.",
         "stop_loss": "M5 structure stop.", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether session breakouts that retest an imbalance continue.",
         "limitations": "FVG retests are frequent; the BOS may already be exhausted when the retest comes.",
         "data": "M15, H1 and M5 closed candles with FVG analysis on M5."}},
    {"key": "R5", "concept": "Volatility-filtered trend",
     "config": _base("R5: volatility-filtered trend", "H1 and M15 structure aligned, price beyond the M15 EMA50, and M5 "
                     "ATR inside a moderate band.",
                     [{"id": "htf", "type": "htf_alignment", "timeframes": ["H1", "M15"], "min_agree": 2,
                       "none_against": True},
                      {"id": "ema", "type": "ema_trend", "timeframe": "M15", "fast": "20", "slow": "50",
                       "mode": "price_vs_slow"},
                      {"id": "atr", "type": "atr_range", "timeframe": "M5", "min": 0.03, "max": 0.2, "unit": "percent"}],
                     stop=ATR_STOP, max_hold=120),
     "documentation": {
         "entry": "Long when H1 and M15 structure are both bullish, M15 close is above EMA50 and ATR(M5) is between "
                  "0.03 % and 0.20 % of price; mirrored for shorts.",
         "exit": "R-multiple take-profit, 1.5 x ATR(M5) stop, 120-minute time exit.",
         "stop_loss": "1.5 x ATR(M5).", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether trend entries avoid both dead and disorderly markets.",
         "limitations": "The ATR band is fixed in advance (not tuned); alignment can persist long after the move.",
         "data": "H1, M15 and M5 closed candles."}},
    {"key": "R6", "concept": "Structure-shift pullback into an order block",
     "config": _base("R6: CHoCH order-block pullback", "A recent M15 change of character, entered when price pulls back "
                     "into a same-direction M5 order-block candidate, with an acceptable spread.",
                     [{"id": "choch", "type": "structure_event", "timeframe": "M15", "event": "CHoCH",
                       "direction": "with", "within_candles": 8},
                      {"id": "ob", "type": "order_block", "timeframe": "M5", "relation": "price_inside"},
                      {"id": "spread", "type": "spread", "max_spread": 0.6}],
                     stop=STRUCTURE_STOP, max_hold=90),
     "documentation": {
         "entry": "Long when an M15 bullish CHoCH became known within 8 candles, price is inside an open bullish M5 "
                  "order-block candidate and the spread is at most 0.60; mirrored for shorts.",
         "exit": "R-multiple take-profit, M5 structure stop, 90-minute time exit.",
         "stop_loss": "M5 structure stop.", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether early reversals continue after a pullback into the last opposing candle.",
         "limitations": "Order-block candidates are heuristic; CHoCH is often a failed reversal.",
         "data": "M15 and M5 closed candles with order-block analysis on M5; candle spreads."}},
    # ---- added 14 Nov 2026 at the user's request, grounded in GoldScalp's own pre-October M5 history (15 Jun - 30 Sep):
    # mean M5 range peaks 12:00-15:00 UTC (6.9-8.0 USD vs 3.6-4.5 in the London morning); the Asian range was broken
    # on 94% of days but extended 1x first on only 24 of 72 vs 21 snap-backs to mid (no plain-breakout edge).
    {"key": "R7", "concept": "New York overlap acceptance breakout",
     "config": _base("R7: NY-overlap acceptance breakout", "During the London/New York overlap (the highest-volatility "
                     "window), a CONFIRMED break of a previous-day or session level with momentum and H1 not against it.",
                     [{"id": "ovl", "type": "session", "sessions": ["New York"], "overlap": "required"},
                      {"id": "brk", "type": "level_break", "timeframe": "M5", "within_candles": 3,
                       "level_types": ["PD", "session"]},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 55, "mode": "confirm"},
                      {"id": "h1", "type": "structure_state", "timeframe": "H1", "state": "not_against"}],
                     stop=ATR_STOP, max_hold=60),
     "documentation": {
         "entry": "Long when London and New York are both open, a confirmed bullish break (acceptance) of a previous-day "
                  "or session high became known within 3 M5 candles, M5 RSI(14) >= 55 and H1 structure is not bearish; "
                  "mirrored for shorts.",
         "exit": "R-multiple take-profit, 1.5 x ATR(M5) stop, 60-minute time exit.",
         "stop_loss": "1.5 x ATR(M5).", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether accepted breaks follow through when volatility is highest (stored data: M5 range "
                         "peaks 12:00-15:00 UTC), unlike plain Asian-range breaks, which showed no edge.",
         "limitations": "Acceptance needs extra candles, so entries are late; US data releases can reverse sharply.",
         "data": "M5 closed candles with liquidity analysis on M5; H1 structure; session definitions."}},
    {"key": "R8", "concept": "London sweep reversal (stop run before New York)",
     "config": _base("R8: London sweep reversal", "In London hours before the New York overlap, a sweep of previous-day "
                     "or session liquidity confirmed by an M5 change of character, with momentum not stretched.",
                     [{"id": "ldn", "type": "session", "sessions": ["London"], "overlap": "excluded"},
                      {"id": "sweep", "type": "liquidity_sweep", "timeframe": "M5", "within_candles": 4,
                       "level_types": ["PD", "session", "EQ"]},
                      {"id": "choch", "type": "structure_event", "timeframe": "M5", "event": "CHoCH", "direction": "with",
                       "within_candles": 4},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 65, "mode": "not_extreme"}],
                     stop=STRUCTURE_STOP, max_hold=60),
     "documentation": {
         "entry": "Long during London-only hours when a bullish sweep of sell-side liquidity (previous-day, session or "
                  "equal lows) and a bullish M5 CHoCH both became known within 4 candles, with M5 RSI(14) <= 65; "
                  "mirrored for shorts.",
         "exit": "R-multiple take-profit, M5 structure stop, 60-minute time exit.",
         "stop_loss": "M5 structure stop (beyond the swept extreme).", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether London stop runs reverse (stored data: after the first close beyond the Asian range, "
                         "price returned to the range middle first on 21 of 72 days).",
         "limitations": "Many sweeps are the start of a trend; CHoCH confirmation can come after most of the move.",
         "data": "M5 closed candles with liquidity analysis on M5; session definitions."}},
    {"key": "R9", "concept": "Volatility squeeze breakout",
     "config": _base("R9: volatility squeeze breakout", "An M5 break of structure with momentum while M15 volatility is "
                     "still contracting, during London or New York.",
                     [{"id": "sq", "type": "volatility_regime", "timeframe": "M15", "allowed": ["contracting"]},
                      {"id": "bos", "type": "structure_event", "timeframe": "M5", "event": "BOS", "direction": "with",
                       "within_candles": 2},
                      {"id": "mom", "type": "momentum", "timeframe": "M5", "level": 58, "mode": "confirm"},
                      {"id": "sess", "type": "session", "sessions": ["London", "New York"]}],
                     stop=ATR_STOP, max_hold=60),
     "documentation": {
         "entry": "Long when M15 volatility is contracting (fast ATR below slow ATR), an M5 bullish BOS became known "
                  "within 2 candles, M5 RSI(14) >= 58 and London or New York is open; mirrored for shorts.",
         "exit": "R-multiple take-profit, 1.5 x ATR(M5) stop (small in a squeeze), 60-minute time exit.",
         "stop_loss": "1.5 x ATR(M5).", "take_profit": "1.5 R or 2.0 R.",
         "investigates": "Whether breaks out of compressed volatility expand (volatility clustering).",
         "limitations": "A small ATR stop is easily hit by noise; compression can persist after a false break.",
         "data": "M15 and M5 closed candles; session definitions."}},
]


def variants(concept: dict[str, Any]) -> list[dict[str, Any]]:
    """The pre-declared 1.5 R and 2.0 R variants of a concept, validated strategy@1 configurations."""
    out = []
    for rr in RR_VARIANTS:
        cfg = copy.deepcopy(concept["config"])
        cfg["exit"]["target"] = {"type": "r_multiple", "r": rr}
        cfg["name"] = f"{cfg['name']} ({rr:g}R)"
        out.append({"rr": rr, "config": builder.validate(cfg)})
    return out


def planned_rr(config: dict[str, Any]) -> float | None:
    t = config["exit"]["target"]
    return float(t["r"]) if t["type"] == "r_multiple" else None


def rr_check(config: dict[str, Any], low: float = 1.5, high: float = 2.0) -> dict[str, Any]:
    """Is the planned risk-to-reward inside the user's 1:1.5-1:2 range? (Flag only: frozen strategies are not changed.)"""
    rr = planned_rr(config)
    t = config["exit"]["target"]
    if rr is None:
        return {"planned_rr": None, "in_range": None,
                "note": f"target type {t['type']}: the reward is not a fixed R multiple, so the RR varies per trade"}
    return {"planned_rr": rr, "in_range": low <= rr <= high,
            "note": f"take-profit at {rr:g} R (1:{rr:g})" + ("" if low <= rr <= high else f": outside 1:{low:g}-1:{high:g}")}
