"""Dashboard order ticket: manual orders on the MT5 DEMO account (FAKE broker module; no terminal, no real order).

* an order is sent once, with its stop-loss and take-profit, its own magic number and comment, and is recorded;
* nothing is sent without the confirmation, above the lot limit, without a stop-loss, with levels on the wrong side,
  with demo execution off, on a real-money account, on another account than the one shown, or with Algo Trading off;
* the ticket changes and closes only its own positions, and the strategy mirror never touches them;
* every state-changing route needs the CSRF header."""
from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from goldscalp.api.app import create_app
from goldscalp.core.config import Settings
from goldscalp.execution import manual, mt5_demo
from tests.test_demo_execution import SERVER, FakeBroker, FakeProvider
from tests.test_phase4_live import lab  # noqa: F401

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

URL = "/api/live/demo/manual"


class TicketBroker(FakeBroker):
    """The fake demo broker plus contract facts and stop-loss / take-profit changes (TRADE_ACTION_SLTP)."""

    def symbol_info(self, name: str) -> Any:
        return SimpleNamespace(name=name, filling_mode=1, digits=2, point=0.01, trade_contract_size=100.0,
                               volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=0)

    def symbol_info_tick(self, name: str) -> Any:
        return SimpleNamespace(bid=self.bid, ask=self.ask, time=1794600000)

    def order_send(self, request: dict[str, Any]) -> Any:
        if request.get("action") == mt5_demo.TRADE_ACTION_SLTP:
            self.sent.append(request)
            p = self.positions[request["position"]]
            p.sl, p.tp = request["sl"], request["tp"]
            return SimpleNamespace(retcode=10009, comment="Request executed", order=0, deal=0, price=0.0, volume=0.0,
                                   bid=self.bid, ask=self.ask)
        return super().order_send(request)


@pytest.fixture
def tk(lab):  # noqa: F811
    broker = TicketBroker()
    provider = FakeProvider(broker)
    gs = lab.gs
    gs.settings = gs.settings.model_copy(update={"demo_execution_enabled": True, "demo_execution_max_lots": "2.00"})
    gs.demo._provider_getter = lambda: provider
    lab.broker = broker
    lab.hash = mt5_demo.account_hash(broker.account.login)
    return lab


def order(c, **over):
    body = {"side": "buy", "volume": "0.10", "sl": "3873.00", "tp": "3888.00", "confirm": "DEMO", "account_hash": c.hash}
    return c.post(f"{URL}/orders", json={**body, **over})


def rows(c):
    return c.get(URL).json()["orders"]


def test_status_shows_the_demo_account_and_the_contract_but_never_the_login(tk):
    s = tk.get(URL).json()
    assert s["enabled"] and s["account"]["demo"] and s["account"]["server"] == SERVER and s["problems"] == []
    assert s["symbol"]["contract_size"] == 100.0 and (s["symbol"]["bid"], s["symbol"]["ask"]) == (3878.30, 3878.51)
    assert s["limits"] == {"max_lots": "2.00", "deviation_points": 30, "magic": manual.MANUAL_MAGIC}
    assert manual.MANUAL_MAGIC != tk.gs.settings.demo_execution_magic
    assert s["positions"] == [] and "51234567" not in str(s) and "DEMO" in s["label"]


def test_a_buy_is_sent_once_with_its_levels_its_own_magic_and_is_recorded(tk):
    r = order(tk)
    assert r.status_code == 201, r.text
    out = r.json()
    (sent,) = tk.broker.sent
    assert (sent["type"], sent["volume"], sent["sl"], sent["tp"]) == (mt5_demo.ORDER_TYPE_BUY, 0.1, 3873.0, 3888.0)
    assert sent["magic"] == manual.MANUAL_MAGIC and sent["comment"] == "GoldScalp manual M1"
    assert not sent["comment"].startswith("GoldScalp T")                      # the mirror's adoption key
    # spread included: a BUY fills at the ask 3878.51 and its levels are hit by the bid
    assert out["estimate"] == {"at_sl_usd": "-55.10", "at_tp_usd": "94.90", "reward_r": "1.72"}
    row = out["order"]
    assert (row["action"], row["status"], row["position_ticket"], row["risk_usd"]) == ("open", "done", 9002, "-55.10")
    assert row["request"]["sl"] == 3873.0 and row["server"] == SERVER
    (pos,) = tk.get(URL).json()["positions"]                 # ticket 9002: the fake broker's deal took 9001
    assert (pos["ticket"], pos["side"], pos["volume"], pos["sl"], pos["tp"]) == (9002, "buy", 0.1, 3873.0, 3888.0)
    sell = order(tk, side="sell", sl="3884.00", tp="3870.00")
    assert sell.status_code == 201 and sell.json()["estimate"]["at_sl_usd"] == "-57.00"   # fills at the bid 3878.30


@pytest.mark.parametrize("over, code, words", [
    ({"confirm": None}, 422, "confirm with DEMO"),
    ({"volume": "2.50"}, 422, "above the demo limit of 2.00 lots"),
    ({"volume": "0.105"}, 422, "steps of 0.01"),
    ({"sl": None}, 422, "stop-loss is required"),
    ({"sl": "3880.00"}, 422, "BUY stop-loss must be below the bid"),
    ({"tp": "3877.00"}, 422, "BUY take-profit must be above the bid"),
    ({"side": "sell", "sl": "3870.00", "tp": "3860.00"}, 422, "SELL stop-loss must be above the ask"),
    ({"account_hash": "0" * 16}, 409, "different account"),
])
def test_unacceptable_orders_send_nothing(tk, over, code, words):
    r = order(tk, **over)
    assert r.status_code == code and words in r.json()["detail"], r.text
    assert tk.broker.sent == [] and tk.broker.checked == []


def test_nothing_is_sent_to_a_real_account_with_algo_trading_off_or_with_demo_execution_off(tk):
    tk.broker.account.trade_mode = 2
    r = order(tk)
    assert r.status_code == 409 and "REAL, not DEMO" in r.json()["detail"]
    tk.broker.account.trade_mode = 0
    tk.broker.algo = False
    tk.gs.demo._account = None                                               # no cached account facts
    r = order(tk)
    assert r.status_code == 409 and "Algo Trading" in r.json()["detail"]
    tk.broker.algo = True
    tk.gs.settings = tk.gs.settings.model_copy(update={"demo_execution_enabled": False})
    r = order(tk)
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert tk.get(URL).json()["enabled"] is False
    assert tk.broker.sent == []
    assert [x["status"] for x in rows(tk)] == ["refused", "refused", "refused"]


def test_the_gateway_still_refuses_a_real_account_at_the_moment_of_sending(tk, monkeypatch):
    real = tk.gs.demo.account                                                 # facts say DEMO, then the account changes
    monkeypatch.setattr(tk.gs.demo, "account", lambda fresh=False: real(fresh=fresh) | {})
    original = mt5_demo.open_market

    def switch_then_open(mt5, **kw):
        tk.broker.account.trade_mode = 2
        return original(mt5, **kw)
    monkeypatch.setattr(mt5_demo, "open_market", switch_then_open)
    r = order(tk)
    assert r.status_code == 409 and "order refused" in r.json()["detail"]
    assert tk.broker.sent == [] and rows(tk)[0]["status"] == "refused"


def test_the_ticket_changes_and_closes_only_its_own_positions(tk):
    order(tk)
    tk.broker.positions[7777] = SimpleNamespace(ticket=7777, symbol="XAUUSD", type=1, volume=0.02, price_open=3878.3,
                                                sl=3890.0, tp=0.0, profit=0.0, magic=tk.gs.settings.demo_execution_magic,
                                                comment="GoldScalp T1")                    # a strategy position
    r = tk.post(f"{URL}/positions/9002/protection", json={"sl": "3878.00", "tp": "3890.00", "account_hash": tk.hash})
    assert r.status_code == 200 and r.json()["ok"], r.text
    sltp = tk.broker.sent[-1]
    assert sltp == {"action": mt5_demo.TRADE_ACTION_SLTP, "symbol": "XAUUSD", "position": 9002, "magic": manual.MANUAL_MAGIC,
                    "sl": 3878.0, "tp": 3890.0}
    assert (tk.broker.positions[9002].sl, tk.broker.positions[9002].tp) == (3878.0, 3890.0)
    sent = len(tk.broker.sent)
    for path, body in (("/positions/7777/protection", {"sl": "3895.00"}), ("/positions/7777/close", {})):
        r = tk.post(URL + path, json=body)
        assert r.status_code == 422 and "not an open manual-ticket position" in r.json()["detail"]
    assert len(tk.broker.sent) == sent and 7777 in tk.broker.positions
    r = tk.post(f"{URL}/positions/9002/close", json={"account_hash": tk.hash})
    assert r.status_code == 200 and r.json()["ok"] and 9002 not in tk.broker.positions
    assert tk.broker.sent[-1]["magic"] == manual.MANUAL_MAGIC and tk.broker.sent[-1]["position"] == 9002
    actions = [(x["action"], x["status"]) for x in reversed(rows(tk))]
    assert actions == [("open", "done"), ("modify", "done"), ("modify", "refused"), ("close", "refused"), ("close", "done")]


def test_the_strategy_mirror_never_counts_adopts_or_closes_manual_positions(tk):
    order(tk)
    gw = mt5_demo.DemoOnlyMT5(tk.broker)
    assert mt5_demo.positions(gw, tk.gs.settings.demo_execution_magic) == []
    out = tk.post("/api/live/demo/stop", json={"close_positions": True}).json()       # the kill switch
    assert out["closed"] == [] and 9002 in tk.broker.positions
    tk.gs.demo._sync(tk.gs.clock.now())
    assert 9002 in tk.broker.positions and tk.get("/api/live/demo").json()["trades"] == []


def test_ticket_routes_need_the_csrf_header(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "t.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with TestClient(app) as c:
        for path in (f"{URL}/orders", f"{URL}/positions/1/protection", f"{URL}/positions/1/close"):
            assert c.post(path, json={}).status_code == 403, path
        assert c.get(URL).json()["enabled"] is False
