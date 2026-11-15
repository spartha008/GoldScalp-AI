"""Virtual (paper) account configuration and summary formulas.

Formulas (tested):
    balance        = starting_balance + sum(ledger amounts after the deposit)
                   = starting_balance + sum(realized_net of closed positions) - sum(open_commission of open positions)
    used_margin    = sum(margin of open positions)
    unrealized     = sum(unrealized P/L of open positions at the current bid/ask)
    equity         = balance + unrealized
    free_margin    = equity - used_margin
    margin_level % = equity / used_margin x 100           (undefined when used_margin = 0)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from collections.abc import Mapping

from goldscalp.simulation.decimal_utils import dstr, to_decimal

ACCOUNT_FIELDS = ("name", "currency", "starting_balance", "leverage", "max_quote_age_seconds",
                  "allow_delayed_fills")


@dataclass(frozen=True)
class AccountConfig:
    name: str
    currency: str
    starting_balance: Decimal
    leverage: int
    max_quote_age_seconds: int
    allow_delayed_fills: bool

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AccountConfig:
        if not isinstance(data, Mapping):
            raise ValueError("account configuration must be an object")
        missing = [k for k in ACCOUNT_FIELDS if k not in data]
        extra = sorted(set(data) - set(ACCOUNT_FIELDS))
        if missing:
            raise ValueError(f"account configuration is missing: {', '.join(missing)}")
        if extra:
            raise ValueError(f"account configuration has unknown fields: {', '.join(extra)}")
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("name must be 1-80 characters")
        currency = str(data["currency"]).upper()
        if currency != "USD":
            raise ValueError("only USD accounts are supported in Phase 1 (XAUUSD is quoted in USD)")
        balance = to_decimal(data["starting_balance"], "starting_balance")
        if balance <= 0 or balance > Decimal("1000000000"):
            raise ValueError("starting_balance must be positive and at most 1,000,000,000")
        leverage = data["leverage"]
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 1000:
            raise ValueError("leverage must be a whole number from 1 to 1000 (1 means 1:1)")
        max_age = data["max_quote_age_seconds"]
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= 3600:
            raise ValueError("max_quote_age_seconds must be a whole number from 1 to 3600")
        if not isinstance(data["allow_delayed_fills"], bool):
            raise ValueError("allow_delayed_fills must be true or false")
        return cls(name=name.strip(), currency=currency, starting_balance=balance, leverage=leverage,
                   max_quote_age_seconds=max_age, allow_delayed_fills=data["allow_delayed_fills"])

    def to_mapping(self) -> dict[str, Any]:
        return {"name": self.name, "currency": self.currency, "starting_balance": dstr(self.starting_balance),
                "leverage": self.leverage, "max_quote_age_seconds": self.max_quote_age_seconds,
                "allow_delayed_fills": self.allow_delayed_fills}


def margin_level(equity: Decimal, used_margin: Decimal) -> Decimal | None:
    if used_margin <= 0:
        return None
    return equity / used_margin * Decimal(100)
