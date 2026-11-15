"""Simulated transaction costs: spread, commission, slippage.

Formulas (tested):
    commission(lots)           = lots x commission_per_lot_per_side          (USD, charged at open AND at close)
    slippage is ADVERSE on every fill, in price units (USD per oz):
        buying  (buy-open, sell-close):  fill = ask + slippage
        selling (sell-open, buy-close):  fill = bid - slippage
    round-trip cost estimate   = spread x ounces + 2 x commission(lots) + 2 x slippage x ounces
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from collections.abc import Mapping

from goldscalp.simulation.decimal_utils import dstr, to_decimal


class SpreadMode(StrEnum):
    MARKET = "market"                                  # use provider bid/ask; refuse if absent
    CONFIGURED = "configured"                          # always configured spread around the mid/last price
    MARKET_ELSE_CONFIGURED = "market_else_configured"  # provider bid/ask when given, else configured (labelled)


COST_FIELDS = ("spread_mode", "configured_spread", "commission_per_lot_per_side", "slippage")


@dataclass(frozen=True)
class CostModel:
    spread_mode: SpreadMode
    configured_spread: Decimal | None      # USD per oz; required unless spread_mode is MARKET
    commission_per_lot_per_side: Decimal   # USD per 1.00 lot per side
    slippage: Decimal                      # USD per oz, adverse, per fill

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CostModel:
        if not isinstance(data, Mapping):
            raise ValueError("cost model must be an object")
        missing = [k for k in COST_FIELDS if k not in data]
        extra = sorted(set(data) - set(COST_FIELDS))
        if missing:
            raise ValueError(f"cost model is missing: {', '.join(missing)}")
        if extra:
            raise ValueError(f"cost model has unknown fields: {', '.join(extra)}")
        try:
            mode = SpreadMode(str(data["spread_mode"]))
        except ValueError as exc:
            raise ValueError(f"spread_mode must be one of {[m.value for m in SpreadMode]}") from exc
        spread = None if data["configured_spread"] is None else to_decimal(data["configured_spread"], "configured_spread")
        model = cls(spread_mode=mode, configured_spread=spread,
                    commission_per_lot_per_side=to_decimal(data["commission_per_lot_per_side"],
                                                           "commission_per_lot_per_side"),
                    slippage=to_decimal(data["slippage"], "slippage"))
        if mode is not SpreadMode.MARKET and (spread is None or spread <= 0):
            raise ValueError("configured_spread must be a positive amount unless spread_mode is 'market'")
        if model.commission_per_lot_per_side < 0 or model.slippage < 0:
            raise ValueError("commission and slippage must not be negative")
        return model

    def to_mapping(self) -> dict[str, Any]:
        return {"spread_mode": self.spread_mode.value, "configured_spread": dstr(self.configured_spread),
                "commission_per_lot_per_side": dstr(self.commission_per_lot_per_side),
                "slippage": dstr(self.slippage)}


def commission(lots: Decimal, model: CostModel) -> Decimal:
    """Commission for ONE side of a trade: lots x commission_per_lot_per_side."""
    return lots * model.commission_per_lot_per_side


def round_trip_estimate(*, lots: Decimal, ounces: Decimal, spread: Decimal, model: CostModel) -> dict[str, Decimal]:
    """Estimated cost of opening and closing at the current spread (no price move)."""
    spread_cost = spread * ounces
    commissions = commission(lots, model) * 2
    slippage_cost = model.slippage * ounces * 2
    return {"spread_cost": spread_cost, "commission": commissions, "slippage_cost": slippage_cost,
            "total": spread_cost + commissions + slippage_cost}
