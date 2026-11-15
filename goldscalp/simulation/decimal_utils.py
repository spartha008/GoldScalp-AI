"""Exact decimal helpers. Money and quantities are Decimal, never float."""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")


def to_decimal(value: Any, name: str) -> Decimal:
    """Parse a Decimal from str/int/Decimal. Floats are refused so binary
    rounding never enters a contract specification or a money amount."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be given as a string or integer, not {type(value).__name__} ({value!r})")
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, (int, str)):
        try:
            out = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"{name} {value!r} is not a number") from exc
    else:
        raise ValueError(f"{name} must be a number given as a string, got {type(value).__name__}")
    if not out.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def price_from_float(value: float, name: str = "price") -> Decimal:
    """A provider price (often a JSON float) as Decimal via its shortest repr."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    if isinstance(value, (int, Decimal)):
        out = Decimal(value)
    elif isinstance(value, float):
        out = Decimal(repr(value))
    elif isinstance(value, str):
        out = to_decimal(value, name)
    else:
        raise ValueError(f"{name} must be numeric, got {type(value).__name__}")
    if not out.is_finite() or out <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return out


def money(value: Decimal) -> Decimal:
    """Round to cents for DISPLAY only (banker's rounding). Stored values keep full precision."""
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def dstr(value: Decimal | None) -> str | None:
    """Canonical string for storage/JSON (no exponent notation)."""
    if value is None:
        return None
    return format(value, "f")
