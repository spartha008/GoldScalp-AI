"""Optional MT5 DEMO-account execution (FAKE broker module; no terminal, no real order):

* the gateway refuses every account that is not the linked DEMO account, before every order;
* linking needs the setting, the typed confirmation, a DEMO hedging account and an active paper monitor;
* simulated validation trades of linked versions are mirrored once (same side, volume, SL, TP), never late;
* simulated exits close the demo position; broker-side stops are recorded; the kill switch closes everything;
* a demo failure never stops the live monitor."""
from __future__ import annotations

import itertools
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from goldscalp.data.providers.mt5_provider import MT5Provider, ReadOnlyMT5
from goldscalp.execution import mirror as mirror_mod
from goldscalp.execution import mt5_demo
from tests.fake_mt5 import FakeMT5
from tests.test_phase4_live import CUT, T0, lab, mark_validated, monitor, ny_strategy, quote, run_cycle  # noqa: F401

SERVER = "VantageInternational-Demo"


class FakeBroker:
    """Documented MetaTrader5 trading functions over an in-memory DEMO account."""

    def __init__(self) -> None:
        self.account = SimpleNamespace(login=51234567, trade_mode=0, margin_mode=2, server=SERVER, company="Vantage",
                                       currency="USD", balance=100000.0, equity=100000.0)
        self.algo = True
        self.bid, self.ask = 3878.30, 3878.51
        self.positions: dict[int, SimpleNamespace] = {}
        self.deals: list[SimpleNamespace] = []
        self.sent: list[dict[str, Any]] = []
        self.checked: list[dict[str, Any]] = []
        self._ids = itertools.count(9001)

    def account_info(self) -> Any:
        return self.account

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=True, trade_allowed=self.algo)

    def symbol_info(self, name: str) -> Any:
        return SimpleNamespace(name=name, filling_mode=1, digits=2, point=0.01)

    def symbol_info_tick(self, name: str) -> Any:
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def last_error(self) -> tuple[int, str]:
        return (1, "Success")

    def order_check(self, request: dict[str, Any]) -> Any:
        self.checked.append(request)
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(request)
        if not self.algo:
            return SimpleNamespace(retcode=10027, comment="AutoTrading disabled by client", order=0, deal=0, price=0.0,
                                   volume=0.0, bid=self.bid, ask=self.ask)
        deal = next(self._ids)
        if "position" in request:
            p = self.positions.pop(request["position"])
            profit = (request["price"] - p.price_open) * 100 * p.volume * (1 if p.type == 0 else -1)
            self.deals.append(SimpleNamespace(ticket=deal, position_id=p.ticket, entry=1, price=request["price"],
                                              profit=round(profit, 2), commission=-0.07, swap=0.0, fee=0.0, reason=3,
                                              time=1794600000))
            return SimpleNamespace(retcode=10009, comment="Request executed", order=deal, deal=deal,
                                   price=request["price"], volume=request["volume"], bid=self.bid, ask=self.ask)
        ticket = next(self._ids)
        self.positions[ticket] = SimpleNamespace(ticket=ticket, symbol=request["symbol"], type=request["type"],
                                                 volume=request["volume"], price_open=request["price"], sl=request["sl"],
                                                 tp=request["tp"], profit=0.0, magic=request["magic"],
                                                 comment=request["comment"])
        self.deals.append(SimpleNamespace(ticket=deal, position_id=ticket, entry=0, price=request["price"], profit=0.0,
                                          commission=-0.07, swap=0.0, fee=0.0, reason=3, time=1794599000))
        return SimpleNamespace(retcode=10009, comment="Request executed", order=ticket, deal=deal,
                               price=request["price"], volume=request["volume"], bid=self.bid, ask=self.ask)

    def positions_get(self, ticket: int | None = None, **_: Any) -> tuple[Any, ...]:
        if ticket is not None:
            return tuple(p for p in self.positions.values() if p.ticket == ticket)
        return tuple(self.positions.values())

    def history_deals_get(self, position: int | None = None, **_: Any) -> tuple[Any, ...]:
        return tuple(d for d in self.deals if d.position_id == position)

    def hit_stop(self, ticket: int) -> None:
        p = self.positions.pop(ticket)
        profit = (p.sl - p.price_open) * 100 * p.volume * (1 if p.type == 0 else -1)
        self.deals.append(SimpleNamespace(ticket=next(self._ids), position_id=ticket, entry=1, price=p.sl,
                                          profit=round(profit, 2), commission=-0.07, swap=0.0, fee=0.0, reason=4,
                                          time=1794600100))


class FakeProvider:
    resolved_symbol = "XAUUSD"

    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.fail: Exception | None = None

    def call_on_mt5_thread(self, fn, timeout: float = 15.0):
        if self.fail is not None:
            raise self.fail
        return fn(self.broker)


@pytest.fixture
def demo(lab):  # noqa: F811
    broker = FakeBroker()
    provider = FakeProvider(broker)
    gs = lab.gs
    gs.settings = gs.settings.model_copy(update={"demo_execution_enabled": True})
    gs.demo._provider_getter = lambda: provider
    rec = ny_strategy(lab, hold=30)
    mark_validated(gs, rec)
    m = monitor(lab, rec)
    lab.broker, lab.provider, lab.m = broker, provider, m
    return lab


def link(c, mid, confirm="DEMO", enabled=True):
    return c.post(f"/api/live/demo/links/{mid}", json={"enabled": enabled, "confirm": confirm})


def open_signal(c, at=None):
    quote(c, "3878.30", "3878.51", at=CUT + timedelta(seconds=29))
    run_cycle(c, at or CUT + timedelta(seconds=30))


def demo_rows(c):
    return c.get("/api/live/demo").json()["trades"]


# ------------------------------------------------------------------------------------------------ gateway
def test_gateway_refuses_every_account_that_is_not_the_linked_demo_account():
    broker = FakeBroker()
    request = {"symbol": "XAUUSD"}
    ok = mt5_demo.DemoOnlyMT5(broker)
    assert ok.order_check(request).retcode == 0                         # DEMO account, demo server
    for change, words in (({"trade_mode": 2}, "REAL, not DEMO"), ({"trade_mode": 1}, "CONTEST, not DEMO"),
                          ({"server": "VantageInternational-Live"}, "not named as a demo server")):
        b = FakeBroker()
        for k, v in change.items():
            setattr(b.account, k, v)
        with pytest.raises(mt5_demo.NotDemoAccount, match=words):
            mt5_demo.DemoOnlyMT5(b).order_send(request)
        assert b.sent == []
    linked = {"server": SERVER, "account_hash": mt5_demo.account_hash(51234567)}
    other = FakeBroker()
    other.account.login = 50000001                                      # another demo login on the same server
    with pytest.raises(mt5_demo.NotDemoAccount, match="different account"):
        mt5_demo.DemoOnlyMT5(other, linked).order_send(request)
    assert other.sent == []
    gw = mt5_demo.DemoOnlyMT5(broker)
    for name in ("login", "initialize", "order_calc_margin", "orders_get", "history_orders_get", "copy_rates_from_pos"):
        with pytest.raises(PermissionError):
            getattr(gw, name)
    with pytest.raises(PermissionError):
        gw.order_send = None
    assert mt5_demo.DEMO_ALLOWED == {"account_info", "terminal_info", "symbol_info", "symbol_info_tick", "order_check",
                                     "order_send", "positions_get", "history_deals_get", "last_error"}


def test_account_facts_never_return_the_login_and_flag_netting_accounts():
    broker = FakeBroker()
    facts = mt5_demo.account_facts(mt5_demo.DemoOnlyMT5(broker))
    assert facts["demo"] and facts["hedging"] and facts["problems"] == [] and facts["server"] == SERVER
    assert "51234567" not in str(facts) and len(facts["account_hash"]) == 16
    broker.account.margin_mode = 0
    assert "HEDGING" in " ".join(mt5_demo.account_facts(mt5_demo.DemoOnlyMT5(broker))["problems"])


def test_the_provider_runs_gateway_calls_on_its_mt5_thread_and_stays_read_only_itself():
    fake = FakeMT5()
    provider = MT5Provider(clock=None, module_loader=lambda: ReadOnlyMT5(fake), process_check=lambda image: True,
                           time_basis="utc")
    assert provider.call_on_mt5_thread(lambda module: module) is fake
    with pytest.raises(PermissionError):
        ReadOnlyMT5(fake).order_send({})                               # the market-data proxy is unchanged


# ------------------------------------------------------------------------------------------------ links
def test_linking_needs_the_setting_the_confirmation_and_a_demo_hedging_account(demo):
    mid = demo.m["id"]
    demo.gs.settings = demo.gs.settings.model_copy(update={"demo_execution_enabled": False})
    r = link(demo, mid)
    assert r.status_code == 409 and "GOLDSCALP_DEMO_EXECUTION_ENABLED" in r.json()["detail"]
    demo.gs.settings = demo.gs.settings.model_copy(update={"demo_execution_enabled": True})
    assert link(demo, mid, confirm="yes").status_code == 409
    demo.broker.account.trade_mode = 2
    r = link(demo, mid)
    assert r.status_code == 409 and "not DEMO" in r.json()["detail"]
    demo.broker.account.trade_mode, demo.broker.account.margin_mode = 0, 0
    demo.gs.demo._account = None
    r = link(demo, mid)
    assert r.status_code == 409 and "HEDGING" in r.json()["detail"]
    demo.broker.account.margin_mode = 2
    demo.gs.demo._account = None
    demo.broker.algo = False
    r = link(demo, mid)
    assert r.status_code == 200 and r.json()["server"] == SERVER and "Algo Trading" in r.json()["note"]
    st = demo.get("/api/live/demo").json()
    assert st["enabled_setting"] and st["links"][0]["enabled"] and st["account"]["trade_mode"] == "DEMO"
    assert link(demo, 999).status_code == 404
    assert demo.post(f"/api/live/demo/links/{mid}", json={"enabled": True, "confirm": "DEMO"},
                     headers={"X-GoldScalp-Request": ""}).status_code == 403          # CSRF header required
    demo.patch(f"/api/live/monitor/{mid}", json={"mode": "observe"})
    link(demo, mid, enabled=False)
    r = link(demo, mid)
    assert r.status_code == 409 and "PAPER mode" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ mirroring
def test_simulated_trade_is_mirrored_once_and_closed_with_the_simulated_time_exit(demo):
    assert link(demo, demo.m["id"]).status_code == 200
    open_signal(demo)
    (t,) = demo.get("/api/live/trades?status=open").json()["trades"]
    (req,) = demo.broker.sent
    assert req["type"] == mt5_demo.ORDER_TYPE_BUY and req["volume"] == 0.02 and req["symbol"] == "XAUUSD"
    assert req["sl"] == float(t["stop_loss"]) and req["tp"] == float(t["take_profit"]) and req["price"] == 3878.51
    assert req["magic"] == demo.gs.settings.demo_execution_magic and req["comment"] == f"GoldScalp T{t['id']}"
    assert demo.broker.checked == [req]                                # checked first, then sent
    (d,) = demo_rows(demo)
    assert d["status"] == "open" and d["live_trade_id"] == t["id"] and d["position_ticket"] in demo.broker.positions
    assert [e["kind"] for e in d["events"]] == ["open"] and "DEMO" in d["label"]
    for s in (40, 50, 70):                                              # never a second order for the same trade
        run_cycle(demo, CUT + timedelta(seconds=s))
    assert len(demo.broker.sent) == 1
    # the simulated 30-minute time exit closes the demo position at market
    demo.broker.bid, demo.broker.ask = 3880.00, 3880.21
    quote(demo, "3880.00", "3880.21", at=CUT + timedelta(minutes=31) - timedelta(seconds=1))
    run_cycle(demo, CUT + timedelta(minutes=31))
    assert demo.get(f"/api/live/trades/{t['id']}").json()["trade"]["status"] == "closed"
    close = demo.broker.sent[1]
    assert close["position"] == d["position_ticket"] and close["type"] == mt5_demo.ORDER_TYPE_SELL and close["price"] == 3880.00
    (d,) = demo_rows(demo)
    assert d["status"] == "closed" and d["close_price"] == "3880.0" and d["profit"] == "2.98"
    assert "simulated trade closed" in d["close_reason"] and demo.broker.positions == {}
    st = demo.get("/api/live/demo").json()
    assert st["open"] == 0 and st["closed_count"] == 1 and st["closed_net"] == "2.84"     # 2.98 - 2 x 0.07


def test_a_broker_side_stop_is_recorded_and_entries_are_never_late(demo):
    link(demo, demo.m["id"])
    open_signal(demo)
    (d,) = demo_rows(demo)
    demo.broker.hit_stop(d["position_ticket"])
    run_cycle(demo, CUT + timedelta(seconds=40))
    (d,) = demo_rows(demo)
    assert d["status"] == "closed" and d["close_reason"] == "stop-loss" and float(d["profit"]) < 0
    assert len(demo.broker.sent) == 1                                   # nothing re-opened afterwards


def test_a_trade_seen_after_the_entry_delay_is_refused_not_sent(demo):
    link(demo, demo.m["id"])
    demo.gs.settings = demo.gs.settings.model_copy(update={"demo_execution_enabled": False})
    open_signal(demo)                                                   # simulated entry while demo execution is off
    demo.gs.settings = demo.gs.settings.model_copy(update={"demo_execution_enabled": True})
    run_cycle(demo, CUT + timedelta(minutes=3))
    (d,) = demo_rows(demo)
    assert d["status"] == "refused" and "never enters late" in d["message"] and demo.broker.sent == []


def test_an_account_switch_to_real_money_stops_demo_execution(demo):
    link(demo, demo.m["id"])
    demo.broker.account.trade_mode = 2                                  # the terminal now shows a REAL account
    open_signal(demo)
    (d,) = demo_rows(demo)
    assert d["status"] == "refused" and "REAL, not DEMO" in d["message"]
    assert demo.broker.sent == [] and demo.broker.checked == []
    lk = demo.get("/api/live/demo").json()["links"][0]
    assert not lk["enabled"] and "safety stop" in lk["disabled_reason"]


def test_the_kill_switch_disables_links_and_closes_open_demo_positions(demo):
    link(demo, demo.m["id"])
    open_signal(demo)
    out = demo.post("/api/live/demo/stop", json={"close_positions": True}).json()
    assert out["links_disabled"] == 1 and len(out["closed"]) == 1 and out["failed"] == []
    assert demo.broker.positions == {} and demo_rows(demo)[0]["status"] == "closed"
    assert "kill switch" in demo_rows(demo)[0]["close_reason"]


def test_a_lost_reply_is_adopted_and_a_demo_failure_never_stops_the_monitor(demo, monkeypatch):
    link(demo, demo.m["id"])
    real_open = mt5_demo.open_market

    def send_then_lose_reply(*args, **kwargs):
        real_open(*args, **kwargs)
        raise TimeoutError("no reply from the terminal")
    monkeypatch.setattr(mt5_demo, "open_market", send_then_lose_reply)
    open_signal(demo)                                                   # the same cycle's reconciliation finds it
    (d,) = demo_rows(demo)
    assert [e["kind"] for e in d["events"]] == ["open", "adopt"] and "no reply" in d["events"][0]["error"]
    assert d["status"] == "open" and d["position_ticket"] in demo.broker.positions and "adopted" in d["message"]
    run_cycle(demo, CUT + timedelta(seconds=40))
    assert len(demo.broker.sent) == 1                                   # never sent twice
    demo.provider.fail = ConnectionError("terminal gone")
    run_cycle(demo, CUT + timedelta(seconds=50))
    status = demo.gs.monitor.status()
    assert status["last_cycle"] == "2026-11-13T19:30:50Z" and demo.gs.demo.last_error.startswith("ConnectionError")


def test_demo_execution_is_off_by_default():
    from goldscalp.core.config import Settings
    assert Settings().demo_execution_enabled is False
    assert mirror_mod.CONFIRM_WORD == "DEMO"
