"""Mirror SIMULATED validation trades of LINKED monitored strategy versions onto the MT5 DEMO account.

The live monitor (live@1) keeps deciding and simulating exactly as before; this component only follows its
validation trades, at the end of each monitor cycle:

* **Entry.** A new simulated trade of a linked version (opened after the link was enabled) is sent as a market
  order with the same side, volume, stop-loss and take-profit, if it is at most
  ``demo_execution_max_entry_delay_seconds`` old and the limits allow it. Never retried: a failed, rejected or
  refused entry is recorded with its reason. A row is written BEFORE the order is sent, so a crash can never cause
  a second order for the same trade (an unrecorded fill is adopted later by its comment).
* **Exit.** When the simulated trade closes (time exit, structure flip, simulated stop or target) the demo
  position is closed at market. When the broker closes it first (its stop-loss or take-profit), the closing deal is
  recorded.
* **Kill switch.** ``stop_all`` disables every link and can close every open GoldScalp demo position.

``GOLDSCALP_DEMO_EXECUTION_ENABLED=false`` (the default) means no MT5 trading call at all: positions already open
keep their broker-side stop-loss and take-profit.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.execution import mt5_demo
from goldscalp.storage import demo_repo, live_repo

log = logging.getLogger("goldscalp.demo")

SYNC_SECONDS = 5.0
ACCOUNT_CACHE_SECONDS = 10.0
ADOPT_WINDOW = timedelta(days=2)
CONFIRM_WORD = "DEMO"


class DemoUnavailable(RuntimeError):
    """Demo execution cannot run now (disabled, MT5 not the active provider, terminal unavailable)."""


def comment_for(live_trade_id: int) -> str:
    return f"GoldScalp T{live_trade_id}"


class DemoMirror:
    def __init__(self, gs: Any, provider_getter: Callable[[], Any]) -> None:
        self.gs = gs
        self._provider_getter = provider_getter
        self._lock = threading.RLock()
        self._last_sync = 0.0
        self._account: tuple[float, dict[str, Any]] | None = None
        self.last_error: str | None = None
        self.last_reconcile: str | None = None

    # -------------------------------------------------------------- plumbing
    @property
    def settings(self) -> Any:
        return self.gs.settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.demo_execution_enabled)

    def _provider(self) -> Any:
        provider = self._provider_getter()
        if provider is None or not hasattr(provider, "call_on_mt5_thread"):
            raise DemoUnavailable("MetaTrader 5 is not the active market-data provider")
        return provider

    def _call(self, fn: Callable[..., Any], *args: Any, expect: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        provider = self._provider()
        return provider.call_on_mt5_thread(lambda module: fn(mt5_demo.DemoOnlyMT5(module, expect), *args, **kwargs))

    def _symbol(self) -> str:
        symbol = getattr(self._provider(), "resolved_symbol", None)
        if not symbol:
            raise DemoUnavailable("the MT5 gold symbol is not resolved yet")
        return symbol

    def account(self, *, fresh: bool = False) -> dict[str, Any]:
        """Demo-account facts (cached briefly). Never raises: problems are reported in the result."""
        now = self.gs.clock.now().timestamp()
        if not fresh and self._account and now - self._account[0] < ACCOUNT_CACHE_SECONDS:
            return self._account[1]
        try:
            facts = self._call(mt5_demo.account_facts)
        except Exception as exc:  # noqa: BLE001 - shown to the user, never raised into the monitor
            facts = {"available": False, "demo": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        self._account = (now, facts)
        return facts

    # -------------------------------------------------------------- links (user actions)
    def link(self, monitored_id: int, *, enable: bool, confirm: str | None = None) -> dict[str, Any]:
        now = self.gs.clock.now()
        with self.gs.db.read() as con:
            m = live_repo.monitored(con, monitored_id)
        if m is None:
            raise LookupError(f"unknown monitored strategy {monitored_id}")
        if not enable:
            with self.gs.db.transaction() as con:
                demo_repo.disable_links(con, now, "unlinked by the user", monitored_id)
            return {"enabled": False}
        if not self.enabled:
            raise DemoUnavailable("demo execution is switched off: set GOLDSCALP_DEMO_EXECUTION_ENABLED=true in .env "
                                  "and restart GoldScalp")
        if m["status"] != "active" or m["mode"] != "paper":
            raise DemoUnavailable("only an ACTIVE monitored version in PAPER mode can be linked (demo orders follow "
                                  "its simulated validation trades)")
        if (confirm or "").strip().upper() != CONFIRM_WORD:
            raise DemoUnavailable(f"type {CONFIRM_WORD} to confirm that orders go to your MT5 demo account")
        facts = self.account(fresh=True)
        problems = facts.get("problems") or []
        if not facts.get("available") or problems:
            raise DemoUnavailable("cannot link: " + "; ".join(problems or ["the MT5 account is not available"]))
        with self.gs.db.transaction() as con:
            demo_repo.enable_link(con, monitored_id, server=facts["server"], account_hash=facts["account_hash"], now=now)
        return {"enabled": True, "server": facts["server"], "algo_trading": facts.get("algo_trading"),
                "note": None if facts.get("algo_trading") else
                "MT5's 'Algo Trading' button is OFF: orders will be refused by the terminal until you switch it on."}

    def stop_all(self, *, close_positions: bool) -> dict[str, Any]:
        """Kill switch: disable every link; optionally close every open GoldScalp demo position now."""
        now = self.gs.clock.now()
        with self.gs.db.transaction() as con:
            disabled = demo_repo.disable_links(con, now, "kill switch")
        closed, failed = [], []
        if close_positions:
            with self._lock:
                with self.gs.db.read() as con:
                    open_rows = demo_repo.trades(con, status="open", limit=1000)
                for d in open_rows:
                    ok, message = self._close_demo(d, now, "kill switch")
                    (closed if ok else failed).append({"id": d["id"], "ticket": d["position_ticket"], "message": message})
        return {"links_disabled": disabled, "closed": closed, "failed": failed}

    # -------------------------------------------------------------- the monitor hook
    def reconcile(self, now: datetime) -> None:
        if not self.enabled:
            return
        with self._lock:
            with self.gs.db.read() as con:
                linked = {lk["monitored_id"]: lk for lk in demo_repo.links(con, enabled=True)}
                open_rows = demo_repo.trades(con, status="open", limit=1000)
                pending = [t for t in live_repo.trades(con, status="open") if t["monitored_id"] in linked
                           and t["position_status"] == "open"
                           and parse_iso_aware(t["opened_at"]) >= parse_iso_aware(linked[t["monitored_id"]]["enabled_at"])]
                done = demo_repo.mirrored_live_ids(con, [t["id"] for t in pending])
            for t in sorted((t for t in pending if t["id"] not in done), key=lambda t: t["opened_at"]):
                self._open(t, linked[t["monitored_id"]], now, open_count=len(open_rows))
                with self.gs.db.read() as con:
                    open_rows = demo_repo.trades(con, status="open", limit=1000)
            mono = now.timestamp()
            if open_rows or self._recent_errors(now):
                if mono - self._last_sync >= SYNC_SECONDS:
                    self._last_sync = mono
                    self._sync(now)
            self.last_reconcile = iso(now)

    # -------------------------------------------------------------- entries
    def _open(self, t: dict[str, Any], lk: dict[str, Any], now: datetime, *, open_count: int) -> None:
        s = self.settings
        side, volume = t["side"], Decimal(t["lots"])
        try:
            max_lots = Decimal(str(s.demo_execution_max_lots))
        except InvalidOperation:
            max_lots = Decimal("0.10")
        age = (now - parse_iso_aware(t["opened_at"])).total_seconds()
        base = {"live_trade_id": t["id"], "monitored_id": t["monitored_id"], "side": side, "volume": str(volume),
                "now": now, "sl": t["stop_loss"], "tp": t["take_profit"], "server": lk.get("server")}
        refusal = None
        if age > s.demo_execution_max_entry_delay_seconds:
            refusal = (f"not sent: the simulated entry is {int(age)} s old (limit "
                       f"{s.demo_execution_max_entry_delay_seconds} s; GoldScalp never enters late)")
        elif volume > max_lots:
            refusal = f"not sent: {volume} lots is above the demo limit of {max_lots} lots"
        elif open_count >= s.demo_execution_max_positions:
            refusal = f"not sent: {open_count} GoldScalp demo positions are open (limit {s.demo_execution_max_positions})"
        elif not t["stop_loss"]:
            refusal = "not sent: the simulated trade has no stop-loss"
        if refusal:
            with self.gs.db.transaction() as con:
                demo_repo.insert_trade(con, status="refused", symbol=None, message=refusal, **base)
            return
        try:
            symbol = self._symbol()
        except DemoUnavailable as exc:
            with self.gs.db.transaction() as con:
                demo_repo.insert_trade(con, status="error", symbol=None, message=f"not sent: {exc}", **base)
            return
        # Recorded BEFORE sending: a crash after the send can never produce a second order for this trade.
        with self.gs.db.transaction() as con:
            row_id = demo_repo.insert_trade(con, status="error", symbol=symbol,
                                            message="order being sent (no terminal reply recorded yet)", **base)
        expect = {"server": lk.get("server"), "account_hash": lk.get("account_hash")}
        try:
            res = self._call(mt5_demo.open_market, symbol=symbol, side=side, volume=volume,
                             sl=Decimal(t["stop_loss"]), tp=Decimal(t["take_profit"]) if t["take_profit"] else None,
                             deviation=s.demo_execution_deviation_points, magic=s.demo_execution_magic,
                             comment=comment_for(t["id"]), expect=expect)
        except mt5_demo.NotDemoAccount as exc:
            with self.gs.db.transaction() as con:
                demo_repo.update_trade(con, row_id, now, status="refused", message=str(exc),
                                       event={"at": iso(now), "kind": "open", "refused": str(exc)})
                demo_repo.disable_links(con, now, f"safety stop: {exc}")
            self.last_error = str(exc)
            log.warning("demo execution stopped: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001 - recorded; the monitor keeps running
            message = f"{type(exc).__name__}: {exc}"
            with self.gs.db.transaction() as con:
                demo_repo.update_trade(con, row_id, now, message=f"no confirmed reply: {message}",
                                       event={"at": iso(now), "kind": "open", "error": message})
            self.last_error = message
            return
        reply = res.get("reply") or {}
        event = {"at": iso(now), "kind": "open", "request": res.get("request"), "reply": reply,
                 "stage": res.get("stage"), "message": res.get("message")}
        with self.gs.db.transaction() as con:
            if res.get("ok"):
                demo_repo.update_trade(con, row_id, now, status="open", position_ticket=reply.get("order"),
                                       open_price=str(reply.get("price")), opened_at=iso(now),
                                       requested_price=str((res.get("request") or {}).get("price")),
                                       message=res.get("message"), event=event)
            else:
                demo_repo.update_trade(con, row_id, now, status="rejected", message=res.get("message"), event=event)

    # -------------------------------------------------------------- exits and reconciliation
    def _recent_errors(self, now: datetime) -> list[dict[str, Any]]:
        with self.gs.db.read() as con:
            rows = demo_repo.trades(con, status="error", limit=50)
        return [r for r in rows if r["symbol"] and parse_iso_aware(r["created_at"]) >= now - ADOPT_WINDOW]

    def _sync(self, now: datetime) -> None:
        magic = self.settings.demo_execution_magic
        positions = {p["ticket"]: p for p in self._call(mt5_demo.positions, magic)}
        by_comment = {p["comment"]: p for p in positions.values()}
        for d in self._recent_errors(now):                 # adopt a fill whose reply was lost
            p = by_comment.get(comment_for(d["live_trade_id"]))
            if p is not None:
                with self.gs.db.transaction() as con:
                    demo_repo.update_trade(con, d["id"], now, status="open", position_ticket=p["ticket"],
                                           open_price=str(p["price_open"]), opened_at=iso(now),
                                           message="adopted: the position exists on the demo account",
                                           event={"at": iso(now), "kind": "adopt", "position": p})
        with self.gs.db.read() as con:
            open_rows = demo_repo.trades(con, status="open", limit=1000)
        for d in open_rows:
            with self.gs.db.read() as con:
                live = live_repo.trade(con, d["live_trade_id"])
            if d["position_ticket"] in positions:
                if live is not None and live["position_status"] != "open":
                    self._close_demo(d, now, f"simulated trade closed ({live['close_reason']})")
            else:
                self._record_broker_close(d, now)

    def _close_demo(self, d: dict[str, Any], now: datetime, why: str) -> tuple[bool, str]:
        s = self.settings
        try:
            res = self._call(mt5_demo.close_market, ticket=int(d["position_ticket"]),
                             deviation=s.demo_execution_deviation_points, magic=s.demo_execution_magic,
                             comment=comment_for(d["live_trade_id"]))
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            with self.gs.db.transaction() as con:
                demo_repo.update_trade(con, d["id"], now, event={"at": iso(now), "kind": "close", "error": message})
            self.last_error = message
            return False, message
        with self.gs.db.transaction() as con:
            demo_repo.update_trade(con, d["id"], now, event={"at": iso(now), "kind": "close", "why": why,
                                                             "request": res.get("request"), "reply": res.get("reply"),
                                                             "message": res.get("message")})
        if res.get("ok"):
            self._record_broker_close(d, now, why=why)
        return bool(res.get("ok")), str(res.get("message"))

    def _record_broker_close(self, d: dict[str, Any], now: datetime, why: str | None = None) -> None:
        try:
            deal = self._call(mt5_demo.closing_deal, int(d["position_ticket"]))
        except Exception as exc:  # noqa: BLE001
            deal, err = None, f"{type(exc).__name__}: {exc}"
        else:
            err = None
        if deal is None:
            with self.gs.db.transaction() as con:
                demo_repo.update_trade(con, d["id"], now, status="closed", closed_at=iso(now),
                                       close_reason=why or "no longer open on the demo account",
                                       message=err or "closing deal not found in the terminal history",
                                       event={"at": iso(now), "kind": "closed", "deal": None, "error": err})
            return
        reason = deal["reason"] if why is None else f"{why}; {deal['reason']}"
        with self.gs.db.transaction() as con:
            demo_repo.update_trade(con, d["id"], now, status="closed", closed_at=iso(now), close_reason=reason,
                                   close_price=str(deal["price"]), profit=deal["profit"], commission=deal["commission"],
                                   swap=deal["swap"], event={"at": iso(now), "kind": "closed", "deal": deal})

    # -------------------------------------------------------------- view
    def status(self) -> dict[str, Any]:
        s = self.settings
        with self.gs.db.read() as con:
            monitored = {m["id"]: m for m in live_repo.monitored_list(con, include_stopped=False)}
            links = {lk["monitored_id"]: lk for lk in demo_repo.links(con)}
            rows = demo_repo.trades(con, limit=200)
        closed = [r for r in rows if r["status"] == "closed" and r["profit"] is not None]
        net = sum((Decimal(r["profit"]) + Decimal(r["commission"] or 0) + Decimal(r["swap"] or 0) for r in closed),
                  Decimal(0))
        account = self.account() if self.enabled else None
        return {
            "enabled_setting": self.enabled,
            "limits": {"max_lots": str(s.demo_execution_max_lots), "max_positions": s.demo_execution_max_positions,
                       "max_entry_delay_seconds": s.demo_execution_max_entry_delay_seconds,
                       "deviation_points": s.demo_execution_deviation_points, "magic": s.demo_execution_magic},
            "account": account,
            "links": [{"monitored_id": mid, "strategy_name": m["strategy_name"], "version": m["version"],
                       "mode": m["mode"], "status": m["status"],
                       **({k: links[mid][k] for k in ("enabled", "enabled_at", "disabled_at", "disabled_reason", "server")}
                          if mid in links else {"enabled": False})} for mid, m in monitored.items()],
            "trades": rows, "open": sum(1 for r in rows if r["status"] == "open"),
            "closed_net": str(net), "closed_count": len(closed),
            "last_error": self.last_error, "last_reconcile": self.last_reconcile,
            "label": "Orders on your MT5 DEMO account (virtual funds). Real-money accounts are always refused.",
        }
