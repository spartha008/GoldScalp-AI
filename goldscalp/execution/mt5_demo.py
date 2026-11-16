"""MT5 DEMO-account order gateway: the only GoldScalp module that may send orders to MetaTrader 5.

Demo-only by construction
-------------------------
Every function here receives the MetaTrader5 package wrapped in ``DemoOnlyMT5``, an allowlist proxy:

* only ``DEMO_ALLOWED`` functions are reachable (no login, no pending orders, no account changes);
* ``order_check`` and ``order_send`` re-read ``account_info()`` immediately before EVERY call and raise
  ``NotDemoAccount`` unless ``trade_mode`` is ACCOUNT_TRADE_MODE_DEMO **and** the trade server's name says demo
  **and** (when a link recorded them) the server and the hashed login are the ones the user linked.

A real-money account can therefore never receive an order, even if the terminal is logged in to one later.
GoldScalp still never handles MT5 credentials: it attaches to the terminal you are already logged in to.
Functions run on the MT5 provider's single thread (``MT5Provider.call_on_mt5_thread``).
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from goldscalp.data.providers.mt5_identity import classify_server

DEMO_ALLOWED = frozenset({"account_info", "terminal_info", "symbol_info", "symbol_info_tick", "order_check",
                          "order_send", "positions_get", "history_deals_get", "last_error"})
GUARDED = frozenset({"order_check", "order_send"})

# Documented MetaTrader 5 enumeration values (MQL5 reference), so the gateway needs no other attribute.
ACCOUNT_TRADE_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_MARGIN_MODE = {0: "RETAIL_NETTING", 1: "EXCHANGE", 2: "RETAIL_HEDGING"}
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6                                  # change an open position's stop-loss / take-profit
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2          # bit flags of symbol_info().filling_mode
POSITION_TYPE_BUY = 0
TRADE_RETCODE_OK = {10008: "placed", 10009: "done", 10010: "done (partial)"}
DEAL_ENTRY_OUT = {1, 3}                                # DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY
DEAL_REASON = {0: "closed in the terminal", 1: "closed from the mobile app", 2: "closed from the web terminal",
               3: "closed by an expert / script", 4: "stop-loss", 5: "take-profit", 6: "stop-out"}


class NotDemoAccount(PermissionError):
    """The terminal's account is not the DEMO account the user linked: no order is sent."""


def account_hash(login: Any) -> str:
    return hashlib.sha256(str(login).encode()).hexdigest()[:16]


def demo_problems(acct: Any, expect: dict[str, Any] | None = None) -> list[str]:
    """Why this account must NOT receive an order (empty = a DEMO account, and the linked one if ``expect``)."""
    if acct is None:
        return ["the terminal returned no account information (not logged in?)"]
    problems = []
    mode = getattr(acct, "trade_mode", None)
    if mode != ACCOUNT_TRADE_MODE_DEMO:
        problems.append(f"the account is {ACCOUNT_TRADE_MODE.get(mode, mode)}, not DEMO")
    server = getattr(acct, "server", None)
    if classify_server(server)[0] != "DEMO":
        problems.append(f"the trade server {server!r} is not named as a demo server")
    if expect:
        if expect.get("server") and server != expect["server"]:
            problems.append(f"the terminal is now on server {server!r}, not the linked {expect['server']!r}")
        if expect.get("account_hash") and account_hash(getattr(acct, "login", None)) != expect["account_hash"]:
            problems.append("the terminal is logged in to a different account than the one that was linked")
    return problems


class DemoOnlyMT5:
    """Allowlist proxy over the MetaTrader5 package; order functions re-check the DEMO account on every call."""

    __slots__ = ("_module", "_expect")

    def __init__(self, module: Any, expect: dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_expect", expect)

    def __getattr__(self, name: str) -> Any:
        if name not in DEMO_ALLOWED:
            raise PermissionError(f"MetaTrader5.{name} is not available to GoldScalp's demo-execution gateway")
        module = object.__getattribute__(self, "_module")
        fn = getattr(module, name)
        if name not in GUARDED:
            return fn
        expect = object.__getattribute__(self, "_expect")

        def guarded(request: dict[str, Any]) -> Any:
            problems = demo_problems(module.account_info(), expect)
            if problems:
                raise NotDemoAccount("order refused: " + "; ".join(problems))
            return fn(request)
        return guarded

    def __setattr__(self, name: str, value: Any) -> None:
        raise PermissionError("the demo-execution proxy cannot be modified")


def _err(mt5: Any) -> str:
    try:
        code, desc = mt5.last_error()
        return f"{code} {desc}"
    except Exception:  # noqa: BLE001
        return "unknown error"


def _filling(info: Any) -> int:
    mode = int(getattr(info, "filling_mode", 0) or 0)
    if mode & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if mode & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN


def _reply(res: Any) -> dict[str, Any]:
    return {k: getattr(res, k, None) for k in ("retcode", "comment", "order", "deal", "price", "volume", "bid", "ask")}


# ------------------------------------------------------------------------------------------------ read
def account_facts(mt5: DemoOnlyMT5) -> dict[str, Any]:
    """Facts shown before linking and on the Demo Execution tab. The login itself is never returned."""
    acct, term = mt5.account_info(), mt5.terminal_info()
    out: dict[str, Any] = {"terminal_connected": bool(term and term.connected),
                           "algo_trading": bool(term and getattr(term, "trade_allowed", False))}
    if acct is None:
        return out | {"available": False, "demo": False, "problems": demo_problems(None), "error": _err(mt5)}
    mode, margin = getattr(acct, "trade_mode", None), getattr(acct, "margin_mode", None)
    problems = demo_problems(acct)
    if margin != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        problems.append(f"the account uses {ACCOUNT_MARGIN_MODE.get(margin, margin)} positions; demo execution needs "
                        "a HEDGING account so each strategy keeps its own position")
    return out | {"available": True, "demo": mode == ACCOUNT_TRADE_MODE_DEMO and classify_server(acct.server)[0] == "DEMO",
                  "trade_mode": ACCOUNT_TRADE_MODE.get(mode, str(mode)), "margin_mode": ACCOUNT_MARGIN_MODE.get(margin, str(margin)),
                  "hedging": margin == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING, "server": acct.server,
                  "company": getattr(acct, "company", None), "currency": getattr(acct, "currency", None),
                  "balance": getattr(acct, "balance", None), "equity": getattr(acct, "equity", None),
                  "account_hash": account_hash(getattr(acct, "login", None)), "problems": problems}


def positions(mt5: DemoOnlyMT5, magic: int) -> list[dict[str, Any]]:
    """GoldScalp's open demo positions (identified by the magic number)."""
    out = []
    for p in mt5.positions_get() or ():
        if getattr(p, "magic", None) == magic:
            out.append({"ticket": p.ticket, "symbol": p.symbol, "side": "buy" if p.type == POSITION_TYPE_BUY else "sell",
                        "volume": p.volume, "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                        "profit": getattr(p, "profit", None), "comment": getattr(p, "comment", "")})
    return out


def closing_deal(mt5: DemoOnlyMT5, ticket: int) -> dict[str, Any] | None:
    """How a position that is no longer open was closed: the closing deal plus the position's total result."""
    deals = list(mt5.history_deals_get(position=ticket) or ())
    outs = [d for d in deals if getattr(d, "entry", None) in DEAL_ENTRY_OUT]
    if not outs:
        return None
    last = outs[-1]
    total = lambda attr: sum(Decimal(str(getattr(d, attr, 0) or 0)) for d in deals)  # noqa: E731
    return {"price": last.price, "server_time": getattr(last, "time", None), "deal": last.ticket,
            "reason": DEAL_REASON.get(getattr(last, "reason", None), f"reason {getattr(last, 'reason', None)}"),
            "profit": str(total("profit")), "commission": str(total("commission") + total("fee")), "swap": str(total("swap"))}


def symbol_facts(mt5: DemoOnlyMT5, symbol: str) -> dict[str, Any]:
    """Contract facts and the current tick of ``symbol``, for the order ticket's P&L estimates and its checks."""
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return {"available": False, "symbol": symbol, "message": f"no symbol information or tick for {symbol}: {_err(mt5)}"}
    num = lambda attr, default: float(getattr(info, attr, None) or default)  # noqa: E731
    return {"available": True, "symbol": symbol, "bid": float(tick.bid), "ask": float(tick.ask),
            "tick_time": getattr(tick, "time", None), "digits": int(getattr(info, "digits", None) or 2),
            "point": num("point", 0.01), "contract_size": num("trade_contract_size", 100.0),
            "volume_min": num("volume_min", 0.01), "volume_max": num("volume_max", 100.0),
            "volume_step": num("volume_step", 0.01), "stops_level_points": int(getattr(info, "trade_stops_level", 0) or 0)}


# ------------------------------------------------------------------------------------------------ orders
def open_market(mt5: DemoOnlyMT5, *, symbol: str, side: str, volume: Decimal, sl: Decimal, tp: Decimal | None,
                deviation: int, magic: int, comment: str) -> dict[str, Any]:
    """Market order with the plan's stop-loss and take-profit attached on the broker side."""
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return {"ok": False, "stage": "prepare", "message": f"no symbol information or tick for {symbol}: {_err(mt5)}"}
    price = tick.ask if side == "buy" else tick.bid
    request = {"action": TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
               "type": ORDER_TYPE_BUY if side == "buy" else ORDER_TYPE_SELL, "price": price, "sl": float(sl),
               "tp": float(tp) if tp is not None else 0.0, "deviation": int(deviation), "magic": int(magic),
               "comment": comment[:31], "type_time": ORDER_TIME_GTC, "type_filling": _filling(info)}
    check = mt5.order_check(request)
    if check is None or getattr(check, "retcode", None) != 0:
        return {"ok": False, "stage": "check", "request": request,
                "reply": _reply(check) if check is not None else None,
                "message": getattr(check, "comment", None) or _err(mt5)}
    res = mt5.order_send(request)
    if res is None:
        return {"ok": False, "stage": "send", "request": request, "message": _err(mt5)}
    reply = _reply(res)
    ok = res.retcode in TRADE_RETCODE_OK
    return {"ok": ok, "stage": "send", "request": request, "reply": reply,
            "message": f"{res.retcode} {TRADE_RETCODE_OK.get(res.retcode, res.comment)}"}


def modify_sltp(mt5: DemoOnlyMT5, *, ticket: int, sl: Decimal | None, tp: Decimal | None, magic: int) -> dict[str, Any]:
    """Change the broker-side stop-loss and take-profit of one open position carrying ``magic`` (0 removes a level)."""
    found = mt5.positions_get(ticket=ticket) or ()
    if not found:
        return {"ok": False, "stage": "prepare", "message": "the position is no longer open"}
    p = found[0]
    if getattr(p, "magic", None) != magic:
        return {"ok": False, "stage": "prepare", "message": f"position {ticket} does not carry magic number {magic}"}
    request = {"action": TRADE_ACTION_SLTP, "symbol": p.symbol, "position": ticket, "magic": int(magic),
               "sl": float(sl) if sl is not None else 0.0, "tp": float(tp) if tp is not None else 0.0}
    res = mt5.order_send(request)
    if res is None:
        return {"ok": False, "stage": "send", "request": request, "message": _err(mt5)}
    return {"ok": res.retcode in TRADE_RETCODE_OK, "stage": "send", "request": request, "reply": _reply(res),
            "message": f"{res.retcode} {TRADE_RETCODE_OK.get(res.retcode, res.comment)}"}


def close_market(mt5: DemoOnlyMT5, *, ticket: int, deviation: int, magic: int, comment: str) -> dict[str, Any]:
    """Close one GoldScalp demo position at market (opposite deal referencing the position)."""
    found = mt5.positions_get(ticket=ticket) or ()
    if not found:
        return {"ok": True, "already_closed": True, "message": "the position is no longer open"}
    p = found[0]
    if getattr(p, "magic", None) != magic:
        return {"ok": False, "stage": "prepare", "message": f"position {ticket} was not opened by GoldScalp"}
    info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
    if info is None or tick is None:
        return {"ok": False, "stage": "prepare", "message": f"no symbol information or tick for {p.symbol}: {_err(mt5)}"}
    buy = p.type == POSITION_TYPE_BUY
    request = {"action": TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": ticket,
               "type": ORDER_TYPE_SELL if buy else ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
               "deviation": int(deviation), "magic": int(magic), "comment": comment[:31],
               "type_time": ORDER_TIME_GTC, "type_filling": _filling(info)}
    res = mt5.order_send(request)
    if res is None:
        return {"ok": False, "stage": "send", "request": request, "message": _err(mt5)}
    return {"ok": res.retcode in TRADE_RETCODE_OK, "stage": "send", "request": request, "reply": _reply(res),
            "message": f"{res.retcode} {TRADE_RETCODE_OK.get(res.retcode, res.comment)}"}
