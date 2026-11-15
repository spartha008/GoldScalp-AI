"""Backtest metrics (definition metrics@1). Formulas (PHASE3_ARCHITECTURE.md section 7):

  net_i            = gross_i - commission_i   (spread and slippage are already inside the fill prices)
  win / loss / be  = net_i > 0 / net_i < 0 / net_i == 0
  win rate         = wins / trades, with a Wilson 95% score interval
  gross profit     = sum(net_i | net_i > 0);   gross loss = |sum(net_i | net_i < 0)|
  profit factor    = gross profit / gross loss
  average outcome  = sum(net_i) / trades (USD)
  expectancy (R)   = mean(R_i), R_i = net_i / (initial risk per oz x ounces)
                   = win rate x average win R - loss rate x |average loss R| (identical when every trade has R)
  max drawdown     = max over t of (peak_t - equity_t), peak_t = max equity up to t; also as % of peak_t,
                     on closed-trade equity and on marked-to-market equity (step closes)
  MFE / MAE        = favourable / adverse excursion from the fill price while open (price, R, ATR)
  holding          = exit time - entry time (minutes)

SAMPLE-SIZE GATE: with fewer than ``min_trades`` trades, ratio and average metrics (win rate, profit factor,
expectancy, averages) are WITHHELD (None + reason); counts and net are always shown. Profit factor is
also withheld when there is no losing trade (division by zero), expectancy R when no trade has an R.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

VERSION = "metrics@1"
Z95 = 1.959963984540054


def wilson(wins: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def max_drawdown(points: Iterable[float], start: float) -> tuple[float, float]:
    peak, worst, worst_pct = start, 0.0, 0.0
    for equity in points:
        peak = max(peak, equity)
        dd = peak - equity
        if dd > worst:
            worst = dd
        if peak > 0 and dd / peak > worst_pct:
            worst_pct = dd / peak
    return round(worst, 2), round(worst_pct * 100, 2)


def drawdown_series(points: list[dict[str, Any]], start: float) -> list[dict[str, Any]]:
    peak, out = start, []
    for p in points:
        peak = max(peak, p["equity"])
        out.append({"time": p["time"], "drawdown": round(p["equity"] - peak, 2)})
    return out


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _max(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(max(vals), 4) if vals else None


def compute(trades: list[dict[str, Any]], *, starting_balance: float, min_trades: int,
            closed_equity: list[dict[str, Any]] | None = None,
            marked_equity: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    n = len(trades)
    nets = [float(t["net_pnl"]) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_profit, gross_loss = round(sum(wins), 2), round(abs(sum(losses)), 2)
    out: dict[str, Any] = {
        "definition": VERSION, "trades": n, "wins": len(wins), "losses": len(losses),
        "breakeven": n - len(wins) - len(losses), "gross_profit": gross_profit, "gross_loss": gross_loss,
        "net": round(sum(nets), 2), "min_trades": min_trades, "sufficient": n >= min_trades, "withheld": [],
        "total_commission": round(sum(float(t["commission"]) for t in trades), 2),
        "total_spread_cost": round(sum(float(t["spread_cost"]) for t in trades), 2),
        "total_slippage_cost": round(sum(float(t["slippage_cost"]) for t in trades), 2),
        "ambiguous_bar_trades": sum(1 for t in trades if t.get("ambiguous_bar")),
        "exit_reasons": {},
    }
    for t in trades:
        out["exit_reasons"][t["exit_reason"]] = out["exit_reasons"].get(t["exit_reason"], 0) + 1
    closed_points = [p["equity"] for p in (closed_equity or [])]
    marked_points = [p["equity"] for p in (marked_equity or [])]
    out["max_drawdown"], out["max_drawdown_pct"] = max_drawdown(closed_points, starting_balance)
    out["max_drawdown_marked"], out["max_drawdown_marked_pct"] = max_drawdown(marked_points, starting_balance)
    ratio_keys = ("win_rate", "win_rate_ci95", "profit_factor", "average_outcome", "average_win", "average_loss",
                  "expectancy_r", "average_r", "mfe_avg", "mae_avg", "mfe_r_avg", "mae_r_avg", "mfe_atr_avg",
                  "mae_atr_avg", "average_holding_minutes")
    if n < min_trades:
        for k in ratio_keys:
            out[k] = None
        out["withheld"] = list(ratio_keys)
        out["reason"] = (f"insufficient sample: {n} trade(s), at least {min_trades} required before ratios and averages "
                         "are shown")
    else:
        rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
        out.update(
            win_rate=round(len(wins) / n, 4), win_rate_ci95=wilson(len(wins), n),
            profit_factor=round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
            average_outcome=round(sum(nets) / n, 2), average_win=_mean(wins), average_loss=_mean(losses),
            expectancy_r=_mean(rs), average_r=_mean(rs),
            mfe_avg=_mean([t["mfe"] for t in trades]), mae_avg=_mean([t["mae"] for t in trades]),
            mfe_r_avg=_mean([t.get("mfe_r") for t in trades]), mae_r_avg=_mean([t.get("mae_r") for t in trades]),
            mfe_atr_avg=_mean([t.get("mfe_atr") for t in trades]), mae_atr_avg=_mean([t.get("mae_atr") for t in trades]),
            average_holding_minutes=_mean([t["holding_minutes"] for t in trades]),
            reason=None)
        if gross_loss == 0:
            out["withheld"].append("profit_factor")
            out["profit_factor_reason"] = "no losing trade: profit factor is undefined"
        if not rs:
            out["withheld"].append("expectancy_r")
    out["mfe_max"] = _max([t["mfe"] for t in trades])
    out["mae_max"] = _max([t["mae"] for t in trades])
    return out


def breakdown(trades: list[dict[str, Any]], key: str, *, starting_balance: float, min_trades: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or "unknown"), []).append(t)
    rows = []
    for name, members in sorted(groups.items()):
        m = compute(members, starting_balance=starting_balance, min_trades=min_trades)
        rows.append({"group": name, **{k: m[k] for k in ("trades", "wins", "losses", "net", "win_rate", "profit_factor",
                                                         "expectancy_r", "average_outcome", "sufficient", "reason")}})
    return rows


def full(result: dict[str, Any], *, starting_balance: float, min_trades: int) -> dict[str, Any]:
    trades = result["trades"]
    return {"summary": compute(trades, starting_balance=starting_balance, min_trades=min_trades,
                               closed_equity=result["closed_equity"], marked_equity=result["marked_equity"]),
            "by_timeframe": breakdown(trades, "timeframe", starting_balance=starting_balance, min_trades=min_trades),
            "by_session": breakdown(trades, "session", starting_balance=starting_balance, min_trades=min_trades),
            "by_volatility": breakdown(trades, "volatility", starting_balance=starting_balance, min_trades=min_trades),
            "by_side": breakdown(trades, "side", starting_balance=starting_balance, min_trades=min_trades),
            "drawdown": drawdown_series(result["marked_equity"], starting_balance)}
