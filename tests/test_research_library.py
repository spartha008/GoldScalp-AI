"""Research library (library@1): read-only normalisation of every study into comparable results. A run that has not
COMPLETED never contributes results (a running sealed test stays hidden), incomplete and failed studies stay listed,
classes are copied rather than recomputed, documents are served from a whitelist only, and nothing is written.
All data here is SYNTHETIC."""
from __future__ import annotations

import json
import warnings

import pytest

from goldscalp.core.config import Settings
from goldscalp.monthly import library as lb
from goldscalp.monthly import store as st
from goldscalp.monthly.standalone import create_research_app

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

T = "2026-11-15T00:00:00Z"
STATS = {"trades": 40, "win_rate": 0.5, "sum_r": 4.0, "expectancy_r": 0.1, "profit_factor": 1.4, "max_dd_r": 3.0,
         "net_usd": 120.0, "expectancy_r_costs_x2": 0.08, "expectancy_r_costs_x3": 0.06}


def _seed(con) -> None:
    ev = ("INSERT INTO evaluations (id, month, range_start, range_end, status, created_at, updated_at, params, availability, "
          "app_version, finished_at) VALUES (?, '2026-10', '2026-10-01', '2026-11-01', ?, ?, ?, ?, '{}', 'test', ?)")
    con.execute(ev, ("me_base", "COMPLETED", T, T, json.dumps({"month": "2026-10"}), T))
    con.execute(ev, ("me_risk", "COMPLETED", T, T, json.dumps({"risk_study": "rs_1", "label": "HYPOTHETICAL S1 5%"}), T))
    con.execute(ev, ("me_bad", "INCOMPLETE", T, T, json.dumps({}), None))
    run = ("INSERT INTO runs (id, evaluation_id, strategy_key, kind, timeframe, status, config, fingerprint, assumptions, "
           "result, reason, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'fp', '{}', ?, ?, ?)")
    summary = {"summary": {"trades": 20, "win_rate": 0.4, "average_r": -0.05, "profit_factor": 0.9, "net": -30.0,
                           "max_drawdown_pct": 12.0, "return_pct": -3.0}}
    con.execute(run, ("r1", "me_base", "A", "existing", "M5", "COMPLETED", json.dumps({"name": "Example A"}),
                      json.dumps(summary), None, T))
    con.execute(run, ("r2", "me_base", "A", "existing", "M1", "INCOMPLETE", json.dumps({"name": "Example A"}), None,
                      "not enough M1 candles", T))
    con.execute(run, ("r3", "me_risk", "A", "existing", "M5", "COMPLETED", json.dumps({"name": "Example A"}),
                      json.dumps(summary), None, T))
    rb = ("INSERT INTO robustness_studies (id, status, params, app_version, error, created_at, updated_at) "
          "VALUES (?, ?, ?, 'test', ?, ?, ?)")
    windows = [{"key": "W1", "month": "2026-06", "start": "2026-06-22T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
               {"key": "W2", "month": "2026-07", "start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"}]
    con.execute(rb, ("rb_final", "COMPLETED", json.dumps({"windows": windows}), None, T, T))
    con.execute(rb, ("rb_failed", "FAILED", json.dumps({"windows": windows}), "boom", T, T))
    dt = ("INSERT INTO discovery_trials (id, hypothesis, kind, status, params, configs, analysis, app_version, created_at, "
          "updated_at, finished_at) VALUES (?, ?, 'baseline', 'COMPLETED', '{}', '{}', ?, 'test', ?, ?, ?)")
    con.execute(dt, ("dt_1", "H6.1", json.dumps({"prediction": {"detail": "did not hold"}, "gate": "not supported",
                                                  "configs": {"VOL-C": {"pooled": STATS, "valid_months": 2,
                                                              "months": {"2026-01": STATS, "2026-02": STATS},
                                                              "by_vol_m15": {"contracting": STATS, "normal": STATS}}}}),
                     T, T, T))
    sr = ("INSERT INTO sealed_runs (id, hypothesis, purpose, status, params, configs, analysis, progress, total, "
          "app_version, created_at, updated_at, finished_at) VALUES (?, 'H6.4', ?, ?, ?, '{}', ?, 0, 4, 'test', ?, ?, ?)")
    series = {"valid": ["2023-11"], "pooled": STATS, "months": {"2023-11": {"status": "COMPLETED", **STATS}},
              "halves": {"1": STATS, "2": STATS}, "by_vol_m15": {"normal": STATS}, "by_session": {"Asian": STATS}}
    analysis = {"definition": "sealed@1", "purpose": "sealed",
                "configs": {"H6.4": {"M5": series, "M15": series}, "VOL-C": {"M5": series, "M15": series}},
                "assessment": {"class": "inconclusive", "why": "a criterion failed", "criteria": {"data": {"pass": True}}}}
    params = json.dumps({"windows": [{"key": "S1", "month": "2023-11"}, {"key": "S2", "month": "2023-12"}]})
    con.execute(sr, ("sr_done", "sealed", "COMPLETED", params, json.dumps(analysis), T, T, T))
    con.execute(sr, ("sr_live", "sealed", "RUNNING", params, json.dumps(analysis), T, T, None))   # partial: never shown
    con.execute("INSERT INTO sealed_results (run_id, config_key, timeframe, window, status, metrics) "
                "VALUES ('sr_live', 'H6.4', 'M5', 'S1', 'COMPLETED', ?)", (json.dumps(STATS),))


def _reports(tmp_path):
    rep = tmp_path / "reports"
    rep.mkdir()
    variant = {"class": "promising", "why": "passes", "criteria": {}, "development_choice": "R2|v-d-Xb",
               "oos": STATS, "all_months_m5": {"W1": {"status": "COMPLETED", "trades": 2, "sum_r": 0.2, "expectancy_r": 0.1},
                                               "W2": {"status": "COMPLETED", "trades": 9, "sum_r": -0.9, "expectancy_r": -0.1}}}
    (rep / "rb_final-final-verdicts.json").write_text(json.dumps({"study": "rb_final", "final": {
        "definition": "verdicts@2", "oos_months": ["W2"], "strategies": {"R2": {"family": "breakout", "version_name": "R2 v2",
                                                                                 "original": {**variant, "class": "inconclusive"},
                                                                                 "redesign": variant}}}}), encoding="utf-8")
    cand = {"key": "R2|v-d-Xb", "class": "rejected", "why": "negative", "criteria": {}, "pooled": STATS, "m15": STATS,
            "months": {"V1": {"status": "COMPLETED", "trades": 10, "sum_r": 1.0, "expectancy_r": 0.1, "sum_r_costs_x2": 0.5}},
            "m15_months": {"V1": {"trades": 5, "sum_r": -0.5, "expectancy_r": -0.1}}}
    (rep / "vl_x-validation.json").write_text(json.dumps({"study": "vl_x", "analysis": {
        "months": {"V1": "2026-01"}, "strategies": {"R2": {"candidate": cand, "original": {**cand, "key": "R2|original"}}}}}),
        encoding="utf-8")
    (rep / "h64-report.md").write_text("# H6.4 report\n\nbody", encoding="utf-8")
    (rep / "notes.txt").write_text("not a report", encoding="utf-8")
    return rep


@pytest.fixture
def lib(tmp_path, monkeypatch):
    store = st.open_store(tmp_path / "monthly.sqlite3")
    with store.transaction() as con:
        _seed(con)
    rep = _reports(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "PHASE_6_PROTOCOL.md").write_text("# Phase 6 protocol\n", encoding="utf-8")
    (root / "SECRET.md").write_text("# not listed\n", encoding="utf-8")
    monkeypatch.setattr(lb, "REPORTS", rep)
    monkeypatch.setattr(lb, "DOCS_ROOT", root)
    monkeypatch.setattr(lb, "PRE7", tmp_path / "pre7")          # isolated from the real pre-Phase 7 results
    return {"store": store, "reports": rep, "root": root, "path": tmp_path / "monthly.sqlite3"}


def _by_id(out):
    return {s["id"]: s for s in out["studies"]}


def test_every_study_is_listed_and_only_completed_runs_have_results(lib):
    with lib["store"].read() as con:
        out = lb.build(con, lib["reports"])
    s = _by_id(out)
    assert {"me_base", "me_risk", "me_bad", "rb_final", "rb_failed", "vl_x", "dt_1", "sr_done", "sr_live"} <= set(s)
    assert s["me_bad"]["status"] == "INCOMPLETE" and s["me_bad"]["results"] == []          # listed, not hidden
    assert s["rb_failed"]["status"] == "FAILED" and s["rb_failed"]["error"] == "boom" and not s["rb_failed"]["results"]
    assert s["sr_live"]["status"] == "RUNNING" and s["sr_live"]["results"] == []           # a running sealed test: nothing early
    assert "only after the run completes" in s["sr_live"]["summary"]
    assert out["results"] == sum(len(x["results"]) for x in out["studies"])


def test_results_are_normalised_with_copied_classes_and_labels(lib):
    with lib["store"].read() as con:
        s = _by_id(lb.build(con, lib["reports"]))
    base = {r["timeframe"]: r for r in s["me_base"]["results"]}
    assert base["M5"]["pooled"]["expectancy_r"] == -0.05 and base["M5"]["pooled"]["sum_r"] == -1.0
    assert base["M5"]["months"]["2026-10"]["trades"] == 20
    assert base["M1"]["status"] == "INCOMPLETE" and base["M1"]["pooled"]["trades"] is None and not base["M1"]["months"]
    assert s["me_risk"]["phase"] == "5.1" and "HYPOTHETICAL risk scenario" in s["me_risk"]["results"][0]["flags"]
    rb = {r["role"].split(" ")[0]: r for r in s["rb_final"]["results"]}
    assert rb["redesigned"]["class"] == "promising" and rb["original"]["class"] == "inconclusive"
    assert rb["redesigned"]["months"]["2026-06"]["partial"] and not rb["redesigned"]["months"]["2026-07"]["partial"]
    vl = {r["id"]: r for r in s["vl_x"]["results"]}
    m5 = vl["vl_x:R2|v-d-Xb|M5"]
    assert m5["class"] == "rejected" and m5["months"]["2026-01"]["sum_r"] == 1.0 and vl["vl_x:R2|v-d-Xb|M15"]["timeframe"] == "M15"
    dev = s["dt_1"]["results"][0]
    assert "development data" in dev["flags"] and set(dev["breakdowns"]["M15 volatility"]) == {"contracting", "normal"}
    sealed = {r["id"]: r for r in s["sr_done"]["results"]}
    assert sealed["sr_done:H6.4|M5"]["class"] == "inconclusive"
    assert sealed["sr_done:VOL-C|M5"]["class"] is None and "comparison only" in sealed["sr_done:VOL-C|M5"]["role"]


def test_metrics_never_invent_missing_values():
    m = lb.metrics({"trades": 4, "sum_r_costs_x2": -0.4})
    assert m["expectancy_r_costs_x2"] == -0.1 and m["expectancy_r"] is None and m["profit_factor"] is None
    assert lb.metrics(None)["trades"] is None


def test_api_is_get_only_and_documents_come_from_a_whitelist(lib):
    app = create_research_app(Settings(db_path=lib["root"] / "app.sqlite3"), store_path=lib["path"])
    before = _counts(lib["path"])
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        r = c.get("/api/monthly/library")
        assert r.status_code == 200 and r.json()["definition"] == "library@1"
        docs = {d["id"] for d in r.json()["documents"]}
        assert docs == {"PHASE_6_PROTOCOL.md", "h64-report.md"}                       # .txt and unlisted files excluded
        d = c.get("/api/monthly/library/documents/h64-report.md")
        assert d.status_code == 200 and d.json()["text"].startswith("# H6.4 report")
        for bad in ("SECRET.md", "..%2Froot%2FSECRET.md", "notes.txt", "nope.md"):
            assert c.get(f"/api/monthly/library/documents/{bad}").status_code == 404
        assert c.post("/api/monthly/library").status_code == 405
    assert _counts(lib["path"]) == before                                                  # nothing written


def _counts(path):
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: con.execute(f"SELECT COUNT(*) FROM [{n}]").fetchone()[0] for n in names}
    finally:
        con.close()


def test_monthly_page_has_the_library_step():
    from goldscalp.monthly.standalone import WEB_DIR
    html = (WEB_DIR / "monthly.html").read_text(encoding="utf-8")
    assert 'data-view="library"' in html and 'id="view-library"' in html and 'id="lib-root"' in html
    js = (WEB_DIR / "js" / "library.js").read_text(encoding="utf-8")
    assert "/api/monthly/library" in js and "innerHTML" not in js                 # DOM built with textContent only
    assert '"library"' in (WEB_DIR / "js" / "monthly.js").read_text(encoding="utf-8")
