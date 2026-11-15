"""Phase 5.3: frozen pre-June validation (validation@1) - locked candidates, no selection, explicit five-month rules,
freeze-before-run, and the run end to end on SYNTHETIC data."""
# ruff: noqa: F811 - pytest fixtures (world) are shared from the Phase 5 test module
from __future__ import annotations

import json
import sqlite3
import time
import warnings
from datetime import UTC, datetime

import pytest

from goldscalp.core.clock import FixedClock
from goldscalp.core.config import Settings
from goldscalp.monthly import closure as cl
from goldscalp.monthly import validation as vl
from goldscalp.monthly import validation_report as vr
from goldscalp.monthly.standalone import create_research_app
from goldscalp.strategies import builder
from tests.test_phase5_monthly import NOW, _table_counts, world  # noqa: F401

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

MONTHS = ["V1", "V2", "V3", "V4", "V5"]


def _trades(rs, i=0):
    return [{"entry": f"2026-0{i + 1}-{(n % 27) + 1:02d}T10:00:00Z", "exit": f"2026-0{i + 1}-{(n % 27) + 1:02d}T11:{n % 60:02d}:00Z",
             "side": "buy", "lots": "0.02", "reason": "target" if r > 0 else "stop", "net": r * 10, "r": r, "mfe_r": 1,
             "mae_r": 0.5, "hold": 60, "session": "London", "vol_m5": "normal", "vol_m15": "normal", "h1": "with",
             "m15": "with", "stop_atr": 1.5, "risk_usd": 10.0, "cost_usd": 0.7} for n, r in enumerate(rs)]


def _results(per_month, key="X", m15=True, missing=()):
    out = {}
    for i, m in enumerate(MONTHS):
        if m in missing:
            out[(key, "M5", m)] = {"status": "INCOMPLETE", "reason": "no data", "trades": []}
            continue
        out[(key, "M5", m)] = {"status": "COMPLETED", "reason": None, "trades": _trades(per_month(m), i)}
        if m15:
            out[(key, "M15", m)] = {"status": "COMPLETED", "reason": None, "trades": _trades(per_month(m), i)}
    return out


def test_protocol_windows_are_january_to_may_and_nothing_else():
    w = vl.windows()
    assert [(x["key"], x["month"]) for x in w] == [("V1", "2026-01"), ("V2", "2026-02"), ("V3", "2026-03"),
                                                   ("V4", "2026-04"), ("V5", "2026-05")]
    assert w[0]["start"] == "2026-01-01T00:00:00Z" and w[-1]["end"] == "2026-06-01T00:00:00Z"
    assert set(vl.CANDIDATES.values()) == {"R2|v-d-Xb", "R1|v-b-Xd", "R5|v-d-Xd-tr", "R7|v-c-Xe-tr"}   # the shortlist only


def test_five_month_rules_are_explicit():
    good = lambda m: [1.5, -1.0] * 8                                         # noqa: E731 - +0.25 R/trade every month
    a = vl.assess(_results(good), "X", MONTHS)
    assert a["class"] == "promising" and all(c["pass"] for c in a["criteria"].values())
    # 3 of 5 months positive: consistency and PF months fail (4 of 5 needed)
    a = vl.assess(_results(lambda m: [3.0, -1.0] * 8 if m in ("V1", "V2", "V3") else [-1.0, 0.5] * 8), "X", MONTHS)
    assert "need 4" in a["criteria"]["consistency"]["detail"] and not a["criteria"]["consistency"]["pass"]
    assert a["class"] == "inconclusive"
    # a month with data but no trade is NOT positive
    a = vl.assess(_results(lambda m: [] if m == "V5" else [1.5, -1.0] * 8), "X", MONTHS)
    assert a["criteria"]["consistency"]["detail"].startswith("4 of 5") and a["criteria"]["consistency"]["pass"]
    a = vl.assess(_results(lambda m: [] if m in ("V4", "V5") else [1.5, -1.0] * 8), "X", MONTHS)
    assert not a["criteria"]["consistency"]["pass"]
    # months without sufficient data are excluded from every count; fewer than 4 valid months is not enough
    a = vl.assess(_results(good, missing=("V5",)), "X", MONTHS)
    assert a["criteria"]["data"]["pass"] and a["criteria"]["consistency"]["detail"].startswith("4 of 4") and "need 3" in a["criteria"]["consistency"]["detail"]
    a = vl.assess(_results(good, missing=("V4", "V5")), "X", MONTHS)
    assert not a["criteria"]["data"]["pass"] and a["class"] == "inconclusive"
    # negative -> rejected; M15 negative -> inconclusive
    assert vl.assess(_results(lambda m: [-1.0, 0.5] * 8), "X", MONTHS)["class"] == "rejected"
    r = _results(good)
    for m in MONTHS:
        r[("X", "M15", m)]["trades"] = _trades([-1.0, 0.5] * 8)
    a = vl.assess(r, "X", MONTHS)
    assert not a["criteria"]["timeframes"]["pass"] and a["class"] == "inconclusive"
    assert a["pooled"]["expectancy_r_costs_x3"] is not None


def _store_source_study(store_path, keys):
    """A stand-in for the Phase 5.2 study in a temporary research store, holding the 8 locked configurations."""
    tpl = [builder.validate(t) for t in builder.templates()]
    con = sqlite3.connect(store_path)
    con.execute("INSERT INTO robustness_studies (id, status, params, app_version, created_at, updated_at, progress, total) "
                "VALUES (?, 'COMPLETED', '{}', 'x', '2026-11-14T00:00:00Z', '2026-11-14T00:00:00Z', 0, 0)", (vl.SOURCE_STUDY,))
    fps = {}
    for i, k in enumerate(keys):
        cfg = tpl[i % len(tpl)]
        cfg = builder.validate({**cfg, "name": f"{k} test"})
        fps[k] = builder.fingerprint(cfg)
        con.execute("INSERT INTO robustness_configs (study_id, key, config, fingerprint) VALUES (?, ?, ?, ?)",
                    (vl.SOURCE_STUDY, k, json.dumps(cfg, sort_keys=True), fps[k]))
    con.commit()
    con.close()
    return fps


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    windows = [{"key": f"V{i + 1}", "start": a, "end": b, "month": a[:10], "role": "validation", "partial": True}
               for i, (a, b) in enumerate([("2026-09-30T07:00:00Z", "2026-09-30T13:00:00Z"),
                                           ("2026-09-30T13:00:00Z", "2026-09-30T19:00:00Z"),
                                           ("2026-10-01T07:00:00Z", "2026-10-01T13:00:00Z"),
                                           ("2026-10-01T13:00:00Z", "2026-10-01T19:00:00Z"),
                                           ("2026-12-01T07:00:00Z", "2026-12-01T13:00:00Z")])]   # no data: INCOMPLETE
    monkeypatch.setattr(vl, "windows", lambda: windows)
    monkeypatch.setattr(vl, "SERIES", ("app", "live:mt5:XAUUSD"))
    monkeypatch.setattr(vr, "check", lambda: {"frozen": True, "unchanged": True})
    monkeypatch.setattr(cl, "closed", lambda: False)      # isolated from the real closure manifest in var/monthly/reports
    app = create_research_app(Settings(db_path=world["app"]), clock=FixedClock(NOW), app_db_path=world["app"],
                              store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        keys = list(vl.CANDIDATES.values()) + [f"{k}|original" for k in vl.CANDIDATES]
        fps = _store_source_study(tmp_path / "monthly.sqlite3", keys)
        monkeypatch.setattr(vl, "FINGERPRINTS", {k: v[:16] for k, v in fps.items()})
        yield c, tmp_path / "monthly.sqlite3", fps


def test_validation_runs_only_the_locked_configurations_and_never_writes_the_app_database(client, world, monkeypatch):
    c, store, fps = client
    before = _table_counts(world["app"])
    r = c.post("/api/monthly/validation-studies")
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    for _ in range(3000):
        s = c.get(f"/api/monthly/validation-studies/{vid}").json()
        if not s["running_job"] and s["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    assert s["status"] == "COMPLETED", s["error"]
    assert s["params"]["selection"] == "none" and set(s["params"]["configs"]) == set(fps)
    con = sqlite3.connect(store)
    rows = con.execute("SELECT config_key, timeframe, window, status FROM validation_results WHERE study_id = ?", (vid,)).fetchall()
    con.close()
    assert {k for k, *_ in rows} == set(fps) and len(rows) == len(fps) * 2 * 5          # 8 configs x M5/M15 x 5 months
    assert {(w, st) for _k, _tf, w, st in rows if w == "V5"} == {("V5", "INCOMPLETE")}       # no data: never invented
    a = s["analysis"]
    assert set(a["strategies"]) == set(vl.CANDIDATES)
    for strat, v in a["strategies"].items():
        assert v["candidate"]["key"] == vl.CANDIDATES[strat] and v["original"]["key"] == f"{strat}|original"
        assert v["candidate"]["months"]["V5"]["status"] == "INCOMPLETE"
    assert _table_counts(world["app"]) == before
    # the report renders every section from the stored study, read-only
    monkeypatch.setattr(vr, "check", lambda: {"frozen": True, "unchanged": True, "manifest": "m.json", "frozen_at": "t",
                                              "candles_at_freeze": 0, "phase52_at_freeze": None})
    paths = vr.write(vid, out_dir=store.parent / "out", store=store)
    md = paths["markdown"].read_text(encoding="utf-8")
    for h in ("## 2. Results", "## 3. Month by month", "## 4. Timeframes", "## 5. Cost stress", "## 6. Criteria",
              "## 7. The questions", "## 8. Integrity"):
        assert h in md
    assert "INCOMPLETE" in md and "not trading readiness" in md and "1 dataset" in md


def test_runs_are_refused_unless_frozen_and_rules_must_match(client, monkeypatch):
    c, store, fps = client
    monkeypatch.setattr(vr, "check", lambda: {"frozen": False, "unchanged": False})
    assert c.post("/api/monthly/validation-studies").status_code == 409
    monkeypatch.setattr(vr, "check", lambda: {"frozen": True, "unchanged": True})
    monkeypatch.setattr(vl, "FINGERPRINTS", {**vl.FINGERPRINTS, "R2|v-d-Xb": "0000000000000000"})
    r = c.post("/api/monthly/validation-studies")
    assert r.status_code == 409 and "does not match the locked" in r.json()["detail"]


def test_protocol_freeze_is_versioned_and_detects_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "REPORTS", tmp_path)
    monkeypatch.setattr(vr, "MANIFEST", tmp_path / "validation-protocol-frozen.json")
    monkeypatch.setattr(vr, "series_candles", lambda store=None: 0)
    monkeypatch.setattr(vr, "phase52_fingerprint", lambda store=None: {"files": {}, "results": {}, "configs_sha256": "",
                                                                        "study": {}})
    assert vr.check()["frozen"] is False
    first = vr.freeze("before data")
    chk = vr.check()
    assert chk["unchanged"] and chk["candles_at_freeze"] == 0
    second = vr.freeze("amendment")
    assert first.exists() and second.name.endswith("-v2.json")
    monkeypatch.setattr(vr, "hashes", lambda: {f: "x" for f in vr.FILES})
    assert vr.check()["unchanged"] is False


def test_phase52_manifests_are_not_part_of_this_protocol():
    assert not set(vr.FILES) & {"goldscalp/monthly/verdicts.py", "goldscalp/monthly/robustness.py",
                                "goldscalp/monthly/verdicts_report.py"}
    assert vr.MANIFEST.name != "verdicts-framework-frozen.json"
    assert datetime(2026, 1, 1, tzinfo=UTC) == vl.PERIOD[0]


def test_phase52_fingerprint_detects_any_change(monkeypatch):
    base = {"files": {"a.md": "1"}, "results": {"rows": 3, "sha256": "x"}, "configs_sha256": "c",
            "study": {"status": "COMPLETED", "analysis_sha256": "s"}}
    monkeypatch.setattr(vr, "phase52_fingerprint", lambda store=None: base)
    assert vr.phase52_check(base) == {"checked": True, "unchanged": True, "changed": [], "result_rows": 3}
    for mutated, what in (({**base, "files": {"a.md": "2"}}, "a.md"), ({**base, "results": {"rows": 4, "sha256": "y"}}, "results"),
                          ({**base, "study": {"status": "FAILED", "analysis_sha256": "s"}}, "study")):
        monkeypatch.setattr(vr, "phase52_fingerprint", lambda store=None, m=mutated: m)
        chk = vr.phase52_check(base)
        assert not chk["unchanged"] and what in chk["changed"]
    assert vr.phase52_check(None) == {"checked": False}
