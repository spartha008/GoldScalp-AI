"""Manual market orders on the MT5 DEMO account from the Dashboard order ticket (/api/live/demo/manual).

The user chooses the side, the volume, the stop-loss and the take-profit on the chart and confirms. This module sends
them through the demo-only gateway (goldscalp/execution/mt5_demo.py), which re-reads the account before every order
and refuses any account that is not the DEMO account the user saw when confirming.

* **Separate from the strategy mirror.** Manual positions carry their own magic number (``MANUAL_MAGIC``) and the
  comment ``GoldScalp manual M<id>``, so the mirror (mirror.py) never adopts, counts or closes them, and this ticket
  only ever changes or closes positions that carry ``MANUAL_MAGIC``.
* **Always a stop-loss**, on the losing side of the current price; the take-profit is optional.
* **Limits.** At most ``demo_execution_max_lots`` per order; demo execution must be switched on.
* **Recorded.** Every request is written BEFORE it is sent (manual_orders, migration 0008), then its result or the
  reason it was refused. Nothing is ever retried.
"""
from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation
from typing import Any

from goldscalp.execution import mt5_demo
from goldscalp.execution.mirror import DemoUnavailable
from goldscalp.storage import manual_repo

MANUAL_MAGIC = 26111401
CONFIRM_WORD = "DEMO"
LABEL = "Manual orders on your MT5 DEMO account (virtual funds). Real-money accounts are always refused."


class TicketRefused(ValueError):
    """The request itself is not acceptable (side, volume, levels or confirmation): nothing is sent."""


def comment_for(order_id: int) -> str:
    return f"GoldScalp manual M{order_id}"


def _price(value: Any, name: str, *, required: bool) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise TicketRefused(f"a {name} is required")
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise TicketRefused(f"{name} must be a number") from exc
    if not out.is_finite() or out <= 0:
        raise TicketRefused(f"{name} must be a positive price")
    return out


def estimate(side: str, entry: Decimal, sl: Decimal | None, tp: Decimal | None, volume: Decimal,
             contract: Decimal) -> dict[str, Any]:
    """USD result at the stop-loss and the take-profit for a fill at ``entry`` (BUY at the ask, SELL at the bid), so
    the spread is included; the broker's commission and swap are not."""
    sign = Decimal(1) if side == "buy" else Decimal(-1)
    at = lambda level: None if level is None else (level - entry) * sign * contract * volume  # noqa: E731
    risk, reward = at(sl), at(tp)
    r = abs(entry - sl) if sl is not None else None
    return {"at_sl_usd": None if risk is None else str(risk.quantize(Decimal("0.01"))),
            "at_tp_usd": None if reward is None else str(reward.quantize(Decimal("0.01"))),
            "reward_r": None if not r or tp is None else str((abs(tp - entry) / r).quantize(Decimal("0.01")))}


class ManualTicket:
    def __init__(self, mirror: Any) -> None:
        self.mirror = mirror
        self.gs = mirror.gs
        self._lock = threading.Lock()

    @property
    def settings(self) -> Any:
        return self.gs.settings

    def _limits(self) -> dict[str, Any]:
        s = self.settings
        return {"max_lots": str(s.demo_execution_max_lots), "deviation_points": s.demo_execution_deviation_points,
                "magic": MANUAL_MAGIC}

    # -------------------------------------------------------------- read
    def status(self) -> dict[str, Any]:
        with self.gs.db.read() as con:
            orders = manual_repo.recent(con, 20)
        out: dict[str, Any] = {"enabled": self.mirror.enabled, "label": LABEL, "limits": self._limits(),
                               "orders": orders, "account": None, "symbol": None, "positions": [], "problems": []}
        if not self.mirror.enabled:
            out["problems"] = ["demo execution is switched off: set GOLDSCALP_DEMO_EXECUTION_ENABLED=true in .env and "
                               "restart GoldScalp"]
            return out
        account = self.mirror.account()
        out["account"] = {k: account.get(k) for k in ("available", "demo", "server", "company", "currency", "balance",
                                                       "equity", "algo_trading", "hedging", "account_hash")}
        out["problems"] = list(account.get("problems") or [])
        if account.get("available") and not account.get("algo_trading"):
            out["problems"].append("MT5's Algo Trading button is OFF: the terminal refuses orders until you switch it on")
        try:
            symbol = self.mirror._symbol()
            out["symbol"] = self.mirror._call(mt5_demo.symbol_facts, symbol)
            out["positions"] = [p | {"label": "MT5 DEMO position (virtual funds)"}
                                for p in self.mirror._call(mt5_demo.positions, MANUAL_MAGIC)]
        except Exception as exc:  # noqa: BLE001 - shown on the ticket
            out["problems"].append(f"{type(exc).__name__}: {exc}")
        return out

    # -------------------------------------------------------------- checks shared by every action
    def _ready(self, account_hash: str | None) -> tuple[dict[str, Any], dict[str, Any], str]:
        if not self.mirror.enabled:
            raise DemoUnavailable("demo execution is switched off: set GOLDSCALP_DEMO_EXECUTION_ENABLED=true in .env and "
                                  "restart GoldScalp")
        if self.settings.demo_execution_magic == MANUAL_MAGIC:
            raise DemoUnavailable(f"GOLDSCALP_DEMO_EXECUTION_MAGIC must not be {MANUAL_MAGIC}: that number is reserved "
                                  "for manual orders")
        facts = self.mirror.account(fresh=True)
        problems = facts.get("problems") or []
        if not facts.get("available") or problems:
            raise DemoUnavailable("not sent: " + "; ".join(problems or ["the MT5 account is not available"]))
        if account_hash and facts.get("account_hash") != account_hash:
            raise DemoUnavailable("not sent: the terminal is now logged in to a different account than the one shown "
                                  "on the ticket; reload the page")
        if not facts.get("algo_trading"):
            raise DemoUnavailable("not sent: MT5's Algo Trading button is OFF")
        symbol = self.mirror._symbol()
        quote = self.mirror._call(mt5_demo.symbol_facts, symbol)
        if not quote.get("available"):
            raise DemoUnavailable(quote.get("message") or "no price for the gold symbol")
        return facts, quote, symbol

    @staticmethod
    def _check_levels(side: str, quote: dict[str, Any], sl: Decimal | None, tp: Decimal | None) -> None:
        """MT5's rule: a BUY's stop-loss below the bid and take-profit above it; a SELL's against the ask; at least the
        broker's minimum stop distance away."""
        mark = Decimal(str(quote["bid"] if side == "buy" else quote["ask"]))
        gap = Decimal(str(quote.get("stops_level_points") or 0)) * Decimal(str(quote.get("point") or 0.01))
        if side == "buy":
            if sl is not None and sl >= mark - gap:
                raise TicketRefused(f"a BUY stop-loss must be below the bid {mark}" + (f" by at least {gap}" if gap else ""))
            if tp is not None and tp <= mark + gap:
                raise TicketRefused(f"a BUY take-profit must be above the bid {mark}" + (f" by at least {gap}" if gap else ""))
        else:
            if sl is not None and sl <= mark + gap:
                raise TicketRefused(f"a SELL stop-loss must be above the ask {mark}" + (f" by at least {gap}" if gap else ""))
            if tp is not None and tp >= mark - gap:
                raise TicketRefused(f"a SELL take-profit must be below the ask {mark}" + (f" by at least {gap}" if gap else ""))

    def _refused(self, action: str, message: str, **fields: Any) -> None:
        with self.gs.db.transaction() as con:
            manual_repo.insert(con, self.gs.clock.now(), action=action, status="refused", message=message, **fields)

    def _send(self, row_id: int, fn: Any, expect: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        now = self.gs.clock.now()
        try:
            res = self.mirror._call(fn, expect=expect, **kwargs)
        except mt5_demo.NotDemoAccount as exc:
            with self.gs.db.transaction() as con:
                manual_repo.update(con, row_id, now, status="refused", message=str(exc))
            raise DemoUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - recorded, never retried
            with self.gs.db.transaction() as con:
                manual_repo.update(con, row_id, now, status="error", message=f"{type(exc).__name__}: {exc}")
            raise DemoUnavailable(f"the terminal call failed: {type(exc).__name__}: {exc}") from exc
        reply = res.get("reply") or {}
        with self.gs.db.transaction() as con:
            manual_repo.update(con, row_id, now, status="done" if res.get("ok") else "rejected", message=res.get("message"),
                               request=res.get("request"), reply=reply or None,
                               price=None if reply.get("price") in (None, 0, 0.0) else str(reply.get("price")))
            row = manual_repo.get(con, row_id)
        return {"ok": bool(res.get("ok")), "message": res.get("message"), "order": row}

    # -------------------------------------------------------------- actions (user requests)
    def place(self, *, side: str, volume: Any, sl: Any, tp: Any = None, confirm: str | None = None,
              account_hash: str | None = None) -> dict[str, Any]:
        if side not in ("buy", "sell"):
            raise TicketRefused("side must be buy or sell")
        try:
            vol = Decimal(str(volume))
        except InvalidOperation as exc:
            raise TicketRefused("volume must be a number of lots") from exc
        stop, target = _price(sl, "stop-loss", required=True), _price(tp, "take-profit", required=False)
        if not vol.is_finite() or vol <= 0 or vol != vol.quantize(Decimal("0.01")):
            raise TicketRefused("volume must be a positive number of lots in steps of 0.01")
        fields = {"side": side, "volume": str(vol), "sl": str(stop), "tp": None if target is None else str(target)}
        if (confirm or "").strip().upper() != CONFIRM_WORD:
            raise TicketRefused(f"confirm with {CONFIRM_WORD}: the order goes to your MT5 demo account")
        max_lots = Decimal(str(self.settings.demo_execution_max_lots))
        if vol > max_lots:
            self._refused("open", f"not sent: {vol} lots is above the demo limit of {max_lots} lots", **fields)
            raise TicketRefused(f"{vol} lots is above the demo limit of {max_lots} lots")
        with self._lock:
            try:
                facts, quote, symbol = self._ready(account_hash)
                if vol < Decimal(str(quote["volume_min"])) or vol > Decimal(str(quote["volume_max"])):
                    raise TicketRefused(f"the broker accepts {quote['volume_min']} to {quote['volume_max']} lots")
                self._check_levels(side, quote, stop, target)
            except (DemoUnavailable, TicketRefused) as exc:
                self._refused("open", str(exc), **fields)
                raise
            entry = Decimal(str(quote["ask"] if side == "buy" else quote["bid"]))
            est = estimate(side, entry, stop, target, vol, Decimal(str(quote["contract_size"])))
            with self.gs.db.transaction() as con:
                row_id = manual_repo.insert(con, self.gs.clock.now(), action="open", status="pending", symbol=symbol,
                                            server=facts["server"], risk_usd=est["at_sl_usd"], reward_usd=est["at_tp_usd"],
                                            **fields)
            out = self._send(row_id, mt5_demo.open_market, {"server": facts["server"], "account_hash": facts["account_hash"]},
                             symbol=symbol, side=side, volume=vol, sl=stop, tp=target,
                             deviation=self.settings.demo_execution_deviation_points, magic=MANUAL_MAGIC,
                             comment=comment_for(row_id))
            ticket = (out["order"].get("reply") or {}).get("order")
            if out["ok"] and ticket:
                with self.gs.db.transaction() as con:
                    manual_repo.update(con, row_id, self.gs.clock.now(), position_ticket=int(ticket))
                    out["order"] = manual_repo.get(con, row_id)
            return out | {"estimate": est}

    def modify(self, ticket: int, *, sl: Any, tp: Any = None, account_hash: str | None = None) -> dict[str, Any]:
        stop, target = _price(sl, "stop-loss", required=True), _price(tp, "take-profit", required=False)
        fields = {"position_ticket": int(ticket), "sl": str(stop), "tp": None if target is None else str(target)}
        with self._lock:
            try:
                facts, quote, _symbol = self._ready(account_hash)
                mine = {p["ticket"]: p for p in self.mirror._call(mt5_demo.positions, MANUAL_MAGIC)}
                if int(ticket) not in mine:
                    raise TicketRefused(f"position {ticket} is not an open manual-ticket position")
                self._check_levels(mine[int(ticket)]["side"], quote, stop, target)
            except (DemoUnavailable, TicketRefused) as exc:
                self._refused("modify", str(exc), **fields)
                raise
            side = mine[int(ticket)]["side"]
            with self.gs.db.transaction() as con:
                row_id = manual_repo.insert(con, self.gs.clock.now(), action="modify", status="pending", side=side,
                                            server=facts["server"], **fields)
            return self._send(row_id, mt5_demo.modify_sltp, {"server": facts["server"], "account_hash": facts["account_hash"]},
                              ticket=int(ticket), sl=stop, tp=target, magic=MANUAL_MAGIC)

    def close(self, ticket: int, *, account_hash: str | None = None) -> dict[str, Any]:
        fields = {"position_ticket": int(ticket)}
        with self._lock:
            try:
                facts, _quote, _symbol = self._ready(account_hash)
                mine = {p["ticket"]: p for p in self.mirror._call(mt5_demo.positions, MANUAL_MAGIC)}
                if int(ticket) not in mine:
                    raise TicketRefused(f"position {ticket} is not an open manual-ticket position")
            except (DemoUnavailable, TicketRefused) as exc:
                self._refused("close", str(exc), **fields)
                raise
            p = mine[int(ticket)]
            with self.gs.db.transaction() as con:
                row_id = manual_repo.insert(con, self.gs.clock.now(), action="close", status="pending", side=p["side"],
                                            symbol=p["symbol"], volume=str(p["volume"]), server=facts["server"], **fields)
            return self._send(row_id, mt5_demo.close_market, {"server": facts["server"], "account_hash": facts["account_hash"]},
                              ticket=int(ticket), deviation=self.settings.demo_execution_deviation_points,
                              magic=MANUAL_MAGIC, comment=f"GoldScalp manual close M{row_id}")
