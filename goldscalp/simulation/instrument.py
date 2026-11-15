"""Instrument (contract) specification.

Contract size differs between venues, so it is CONFIGURATION, never a
constant. A specification must state every field explicitly: a missing field
is an error, not a default ("never silently assume missing broker
specifications").

Formulas (tested in tests/test_simulation_formulas.py):
    ounces(lots)  = lots x contract_size            (contract_size in troy oz per 1.00 lot)
    lots is valid  iff  min_lot <= lots <= max_lot  and  lots / lot_step is a whole number
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from goldscalp.core.fingerprint import digest
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.simulation.decimal_utils import dstr, to_decimal

REQUIRED_FIELDS = (
    "name", "symbol", "spec_origin", "is_hypothetical", "quote_currency", "unit", "contract_size",
    "price_decimals", "tick_size", "min_lot", "lot_step", "max_lot", "lot_presets", "schedule",
)
STANDARD_PRESET_NAMES = ("standard", "mini", "micro")


class SpecError(ValueError):
    """An instrument specification that is incomplete or inconsistent."""


@dataclass(frozen=True)
class InstrumentSpec:
    name: str
    symbol: str
    spec_origin: str
    is_hypothetical: bool
    quote_currency: str
    unit: str
    contract_size: Decimal
    price_decimals: int
    tick_size: Decimal
    min_lot: Decimal
    lot_step: Decimal
    max_lot: Decimal
    lot_presets: dict[str, Decimal] = field(default_factory=dict)
    schedule: TradingSchedule | None = None

    # ------------------------------------------------------------ construction

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> InstrumentSpec:
        if not isinstance(data, Mapping):
            raise SpecError("instrument specification must be a JSON object")
        missing = [k for k in REQUIRED_FIELDS if k not in data]
        extra = sorted(set(data) - set(REQUIRED_FIELDS))
        if missing:
            raise SpecError(f"instrument specification is missing required fields: {', '.join(missing)}. "
                            "GoldScalp does not assume broker specifications; state every field.")
        if extra:
            raise SpecError(f"instrument specification has unknown fields: {', '.join(extra)}")
        try:
            if not isinstance(data["is_hypothetical"], bool):
                raise SpecError("is_hypothetical must be true or false")
            price_decimals = data["price_decimals"]
            if isinstance(price_decimals, bool) or not isinstance(price_decimals, int) or not 0 <= price_decimals <= 8:
                raise SpecError("price_decimals must be an integer between 0 and 8")
            presets_raw = data["lot_presets"]
            if not isinstance(presets_raw, Mapping) or not presets_raw:
                raise SpecError("lot_presets must be a non-empty object of name -> lots")
            spec = cls(
                name=_text(data, "name"), symbol=_text(data, "symbol").upper(),
                spec_origin=_text(data, "spec_origin"), is_hypothetical=data["is_hypothetical"],
                quote_currency=_text(data, "quote_currency").upper(), unit=_text(data, "unit"),
                contract_size=to_decimal(data["contract_size"], "contract_size"),
                price_decimals=price_decimals,
                tick_size=to_decimal(data["tick_size"], "tick_size"),
                min_lot=to_decimal(data["min_lot"], "min_lot"),
                lot_step=to_decimal(data["lot_step"], "lot_step"),
                max_lot=to_decimal(data["max_lot"], "max_lot"),
                lot_presets={str(k): to_decimal(v, f"lot_presets.{k}") for k, v in presets_raw.items()},
                schedule=TradingSchedule.from_mapping(data["schedule"]),
            )
        except SpecError:
            raise
        except ValueError as exc:
            raise SpecError(str(exc)) from exc
        spec._check()
        return spec

    @classmethod
    def load(cls, path: str | Path) -> InstrumentSpec:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpecError(f"cannot read instrument specification {path}: {exc}") from exc
        return cls.from_mapping(raw)

    def _check(self) -> None:
        if self.contract_size <= 0:
            raise SpecError("contract_size must be positive")
        if self.tick_size <= 0:
            raise SpecError("tick_size must be positive")
        if self.lot_step <= 0 or self.min_lot <= 0 or self.max_lot < self.min_lot:
            raise SpecError("lot sizes must satisfy 0 < min_lot <= max_lot and lot_step > 0")
        if self.unit != "troy_oz":
            raise SpecError(f"unit must be 'troy_oz' for a gold contract, got {self.unit!r}")
        if self.quote_currency != "USD":
            raise SpecError("only USD-quoted instruments are supported in Phase 1")
        if not _is_multiple(self.min_lot, self.lot_step):
            raise SpecError("min_lot must be a whole multiple of lot_step")
        for name, lots in self.lot_presets.items():
            try:
                self.validate_lots(lots)
            except ValueError as exc:
                raise SpecError(f"lot preset {name!r} is not a valid lot size: {exc}") from exc

    # ---------------------------------------------------------------- formulas

    def ounces(self, lots: Decimal) -> Decimal:
        """Troy ounces represented by ``lots``: lots x contract_size."""
        return lots * self.contract_size

    def validate_lots(self, lots: Decimal) -> Decimal:
        if not isinstance(lots, Decimal) or not lots.is_finite():
            raise ValueError("lots must be a finite Decimal")
        if lots <= 0:
            raise ValueError("lots must be positive")
        if lots < self.min_lot:
            raise ValueError(f"{lots} lots is below the minimum of {self.min_lot}")
        if lots > self.max_lot:
            raise ValueError(f"{lots} lots is above the maximum of {self.max_lot}")
        if not _is_multiple(lots, self.lot_step):
            raise ValueError(f"{lots} lots is not a multiple of the lot step {self.lot_step}")
        return lots

    # ------------------------------------------------------------------- views

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name, "symbol": self.symbol, "spec_origin": self.spec_origin,
            "is_hypothetical": self.is_hypothetical, "quote_currency": self.quote_currency,
            "unit": self.unit, "contract_size": dstr(self.contract_size),
            "price_decimals": self.price_decimals, "tick_size": dstr(self.tick_size),
            "min_lot": dstr(self.min_lot), "lot_step": dstr(self.lot_step), "max_lot": dstr(self.max_lot),
            "lot_presets": {k: dstr(v) for k, v in self.lot_presets.items()},
            "schedule": self.schedule.to_mapping() if self.schedule else None,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.to_mapping())


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{key} must be a non-empty string")
    return value.strip()


def _is_multiple(value: Decimal, step: Decimal) -> bool:
    quotient = value / step
    return quotient == quotient.to_integral_value()
