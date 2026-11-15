"""HTTP API: envelopes with data kinds, security headers, refusals, TradingView route."""
from __future__ import annotations

import warnings

import pytest

from goldscalp.api.app import TV_CSP, create_app
from goldscalp.core.config import Settings
from tests.conftest import csv_bytes, synthetic_m1

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

ENVELOPE_ROUTES = ["/api/status", "/api/market/quote", "/api/market/series", "/api/market/candles?tf=M5",
                   "/api/analysis/overview", "/api/sim/account", "/api/sim/positions", "/api/sim/ledger",
                   "/api/sim/equity", "/api/data/imports"]


@pytest.fixture
def client(tmp_path, clock):
    settings = Settings(db_path=tmp_path / "api.sqlite3", live_provider="none", max_import_mb=1)
    app = create_app(settings, clock=clock, provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        yield c


def test_every_data_route_returns_a_labelled_envelope(client):
    for path in ENVELOPE_ROUTES:
        body = client.get(path).json()
        assert body["kind"] in ("current", "historical", "simulated"), path
        assert "generated_at" in body and "data" in body, path


def test_simulated_routes_are_labelled_simulated(client):
    body = client.get("/api/sim/account").json()
    assert body["kind"] == "simulated"
    assert "SIMULATED" in body["notes"][0]


def test_market_state_without_provider_is_disconnected(client):
    assert client.get("/api/market/quote").json()["market_state"] == "DISCONNECTED"


def test_security_headers(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "script-src 'self';" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_tradingview_page_has_its_own_narrow_csp(client):
    response = client.get("/tv-widget")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert csp == TV_CSP
    assert "connect-src https://*.tradingview.com" in csp and "'self'" not in csp.split("connect-src")[1].split(";")[0]


def test_tradingview_can_be_disabled(tmp_path, clock):
    settings = Settings(db_path=tmp_path / "x.sqlite3", live_provider="none", tradingview_widget=False)
    app = create_app(settings, clock=clock, provider=None, start_market_data=False)
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        assert c.get("/tv-widget").status_code == 404
        assert c.get("/api/ui-config").json()["tradingview_widget"] is False


def test_order_without_price_is_refused(client):
    response = client.post("/api/sim/orders", json={"side": "buy", "lots": "0.01"})
    assert response.status_code == 422
    assert "no market price" in response.json()["detail"]


def test_import_and_candles_round_trip(client, spec):
    frame = synthetic_m1("2026-11-10T13:00Z", "2026-11-10T16:00Z", spec.schedule)
    response = client.post("/api/data/import", files={"file": ("x.csv", csv_bytes(frame), "text/csv")},
                           data={"dialect": "generic_utc", "timeframe": "M1", "time_basis": "utc",
                                 "series_label": "api", "candle_side": "bid", "price_decimals": "2"})
    assert response.status_code == 200, response.text
    body = client.get("/api/market/candles?tf=M15&series=csv:api").json()
    assert body["kind"] == "historical" and body["market_state"] == "HISTORICAL"
    candles = body["data"]["candles"]
    assert len(candles) == 12 and all(c["origin"] == "derived_from_M1" and c["sub_bars"] == 15 for c in candles)


def test_candles_page_back_through_history_with_before(client, spec):
    frame = synthetic_m1("2026-11-10T13:00Z", "2026-11-10T16:00Z", spec.schedule)
    assert client.post("/api/data/import", files={"file": ("x.csv", csv_bytes(frame), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": "M1", "time_basis": "utc", "series_label": "page",
                             "candle_side": "bid", "price_decimals": "2"}).status_code == 200
    latest = client.get("/api/market/candles?tf=M1&series=csv:page&limit=60").json()["data"]["candles"]
    assert len(latest) == 60 and latest[-1]["time"] == latest[0]["time"] + 59 * 60
    # the page before the oldest candle shown: strictly older, contiguous, newest last
    page = client.get(f"/api/market/candles?tf=M1&series=csv:page&limit=60&before={latest[0]['time']}").json()
    assert page["kind"] == "historical"
    older = page["data"]["candles"]
    assert len(older) == 60 and older[-1]["time"] == latest[0]["time"] - 60 and all(c["time"] < latest[0]["time"] for c in older)
    # ISO-8601 works too; derived timeframes page the same way; the start of history returns a short page
    iso = client.get("/api/market/candles?tf=M15&series=csv:page&before=2026-11-10T15:00:00Z").json()["data"]["candles"]
    assert [c["time"] for c in iso][-1] == 1794322800 - 900 and len(iso) == 8          # 14:45, the last bar before 15:00
    first = client.get(f"/api/market/candles?tf=M1&series=csv:page&limit=500&before={older[0]['time']}").json()["data"]
    assert len(first["candles"]) == 60 and first["candles"][0]["time"] == latest[0]["time"] - 120 * 60
    for bad in ("yesterday", "2026-11-10T15:00:00"):                 # no offset: refused rather than guessed
        assert client.get(f"/api/market/candles?tf=M5&before={bad}").status_code == 422


def test_monthly_and_yearly_chart_candles_come_from_stored_hourly_bars(client, spec):
    from datetime import UTC, datetime
    frame = synthetic_m1("2026-11-10T13:00Z", "2026-11-10T16:00Z", spec.schedule)
    assert client.post("/api/data/import", files={"file": ("x.csv", csv_bytes(frame), "text/csv")},
                       data={"dialect": "generic_utc", "timeframe": "M1", "time_basis": "utc", "series_label": "cal",
                             "candle_side": "bid", "price_decimals": "2"}).status_code == 200
    hourly = client.get("/api/market/candles?tf=H1&series=csv:cal").json()["data"]["candles"]
    closed_h1 = [c for c in hourly if c["closed"]]
    for tf, start in (("MN1", datetime(2026, 11, 1, tzinfo=UTC)), ("Y1", datetime(2026, 1, 1, tzinfo=UTC))):
        body = client.get(f"/api/market/candles?tf={tf}&series=csv:cal").json()
        d = body["data"]
        assert d["tf"] == tf and d["source_tf"] == "H1" and "display only" in d["note"]
        (bar,) = d["candles"]
        assert bar["time"] == int(start.timestamp()) and bar["origin"] == "derived_from_H1"
        assert bar["partial"] is True                      # stored history starts on 10 Nov, not at the period start
        assert bar["sub_bars"] == len(closed_h1)
        assert bar["open"] == closed_h1[0]["open"] and bar["close"] == closed_h1[-1]["close"]
        assert bar["high"] == max(c["high"] for c in closed_h1) and bar["low"] == min(c["low"] for c in closed_h1)
    assert client.get("/api/market/candles?tf=mn1&series=csv:cal").json()["data"]["tf"] == "MN1"


def test_oversized_import_is_refused(client):
    big = b"x" * (1024 * 1024 + 10)
    response = client.post("/api/data/import", files={"file": ("big.csv", big, "text/csv")},
                           data={"dialect": "generic_utc", "timeframe": "M1", "time_basis": "utc",
                                 "series_label": "big", "candle_side": "bid", "price_decimals": "2"})
    assert response.status_code == 413


def test_unknown_timeframe_is_refused(client):
    assert client.get("/api/market/candles?tf=M7").status_code == 422


def test_state_changing_requests_need_the_csrf_header(tmp_path, clock):
    settings = Settings(db_path=tmp_path / "c.sqlite3", live_provider="none")
    app = create_app(settings, clock=clock, provider=None, start_market_data=False)
    with TestClient(app) as bare:
        assert bare.post("/api/sim/orders", json={"side": "buy", "lots": "0.01"}).status_code == 403
        assert bare.post("/api/market/reconnect").status_code == 403
        assert bare.get("/api/sim/account").status_code == 200   # reads are unaffected
