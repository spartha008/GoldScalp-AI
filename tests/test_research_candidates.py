"""Research candidates (research-candidate@1): the registered rule sets are exactly the screened ones, a changed rule
is detected, they are not Strategy Lab strategies, the API is read-only, and the library lists the pre-Phase 7 screens
as development-data screening results. Evidence fixtures here are SYNTHETIC."""
from __future__ import annotations

import json
import shutil
import warnings

import pytest

from goldscalp.core.config import PROJECT_ROOT, Settings
from goldscalp.monthly import library as lb
from goldscalp.monthly.standalone import create_research_app
from goldscalp.research import candidates as cd
from goldscalp.research import external_rules
from goldscalp.research import screening_ext as sx

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


def test_registered_candidates_are_exactly_the_screened_rule_sets():
    cands = cd.load_all()
    declared = {c["key"]: c for c in sx.load2()["candidates"]}
    assert [c["id"] for c in cands] == ["P7-FIX", "P7-TOD"]
    for c in cands:
        d = declared[c["id"]]
        assert c["definition"] == cd.DEFINITION and c["status"] == {"P7-FIX": cd.STATUS, "P7-TOD": cd.ARCHIVED}[c["id"]]
        assert c["rule"] == d["rule"] and c["params"] == d["params"]                  # no silent rule change
        assert c["source"] == d["source"] and c["original_rules"] == d["original_rules"] and c["deviations"] == d["deviations"]
        assert c["fingerprint"] == cd.fingerprint(c["rule"], c["params"]) and c["rule"] in external_rules.RULES
        assert c["origin"]["screen_classification"] == "WORTH FURTHER RESEARCH"
        assert set(c["evidence_at_registration"]) >= {"screen", "context"} and c["caveats"] and c["next_questions"]
        text = " ".join(c["restrictions"]).lower()
        assert "not validated" in text and "not a validation" in text and "frozen" in text
        assert "schema" not in c and "strategy@1" not in json.dumps(c)                 # not a Strategy Lab strategy
    chk = cd.check()
    assert chk["unchanged"] and chk["candidates"] == 2
    assert chk["active"] == ["P7-FIX"] and chk["archived"] == ["P7-TOD"]          # 16 Nov 2026, at the owner's request
    tod = next(c for c in cands if c["id"] == "P7-TOD")
    assert tod["archived"]["on"] == "2026-11-16" and tod["archived"]["status_before"] == cd.STATUS
    assert "P7-FIX stays the only active research candidate" in tod["archived"]["reason"]


def test_an_unknown_status_is_not_accepted(tmp_path):
    d = tmp_path / "candidates"
    shutil.copytree(cd.DIR, d)
    doc = json.loads((d / "P7-TOD.json").read_text(encoding="utf-8"))
    doc["status"] = "promoted"
    (d / "P7-TOD.json").write_text(json.dumps(doc), encoding="utf-8")
    cd._cache.clear()
    assert not cd.check(d)["unchanged"]
    cd._cache.clear()


def test_a_changed_rule_is_detected(tmp_path):
    d = tmp_path / "candidates"
    shutil.copytree(cd.DIR, d)
    assert cd.check(d)["unchanged"]
    doc = json.loads((d / "P7-FIX.json").read_text(encoding="utf-8"))
    doc["params"]["trades"][0]["enter"] = "05:10"                                      # a "small" tweak
    (d / "P7-FIX.json").write_text(json.dumps(doc), encoding="utf-8")
    cd._cache.clear()
    row = next(r for r in cd.check(d)["rows"] if r["id"] == "P7-FIX")
    assert not row["fingerprint_ok"] and not row["matches_declaration"] and not cd.check(d)["unchanged"]
    cd._cache.clear()


def _trade(day, r, net, tag=None):
    return {"entry": f"{day}T09:05:00Z", "exit": f"{day}T09:35:00Z", "r": r, "net": net, "tag": tag}


def _window(trades, nominal=True):
    return {"frame": "M5", "nominal_r": nominal, "usd_per_trade": 1.0, "exits": {"time": len(trades)}, "by_tag": {},
            "stats": {"trades": len(trades), "win_rate": 0.5, "expectancy_r": 0.1, "profit_factor": 1.5, "sum_r": 0.3,
                      "max_dd_r": 1.0, "net_usd": 3.0, "expectancy_r_costs_x2": 0.05, "expectancy_r_costs_x3": 0.01},
            "concentration": {"days_traded": 2, "positive_days": 1, "sum_r_without_best_day": -0.2, "best_day": "2026-09-01"},
            "delayed": {"trades": len(trades), "expectancy_r": 0.08}, "trades": trades}


@pytest.fixture
def pre7(tmp_path):
    d = tmp_path / "pre7"
    d.mkdir()
    trades = [_trade("2026-08-31", 0.5, 5.0), _trade("2026-09-01", -0.2, -2.0), _trade("2026-09-01", 0.4, 4.0)]
    windows = {"screen": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-15T00:00:00Z"},
               "context": {"start": "2026-08-17T00:00:00Z", "end": "2026-10-31T00:00:00Z", "note": "context"}}
    results = {"P7-FIX": {"screen": _window(trades[1:]), "context": _window(trades)},
               "P7-XYZ": {"screen": _window(trades[1:], nominal=False), "context": _window(trades, nominal=False)}}
    (d / "stage2-results.json").write_text(json.dumps({
        "ran_at": "2026-11-16T00:00:00Z", "windows": windows, "results": results,
        "classifications": {"P7-FIX": {"classification": "WORTH FURTHER RESEARCH", "why": ["passes"]},
                            "P7-XYZ": {"classification": "REJECTED", "why": ["negative"]}}}), encoding="utf-8")
    (d / "stage2-fix-placebo.json").write_text(json.dumps({"note": "placebo", "positive_windows": 1, "windows": [
        {"enter_new_york": "05:05", "trades": 3, "net_usd_per_trade": 2.0, "t_stat": 1.0, "win_rate": 0.6, "rank": 1},
        {"enter_new_york": "09:35", "trades": 3, "net_usd_per_trade": 1.0, "t_stat": 0.5, "win_rate": 0.6, "rank": 2},
        {"enter_new_york": "12:05", "trades": 3, "net_usd_per_trade": -1.0, "t_stat": -0.5, "win_rate": 0.3, "rank": 3},
        {"enter_new_york": "17:05", "trades": 0, "net_usd_per_trade": None, "t_stat": None, "win_rate": None}]}), encoding="utf-8")
    cd._cache.clear()
    yield d
    cd._cache.clear()


def test_evidence_series_come_from_the_stored_results(pre7):
    fix = next(c for c in cd.load_all() if c["id"] == "P7-FIX")
    ev = cd.evidence(fix, pre7)
    ctx = ev["windows"]["context"]
    assert [d["day"] for d in ctx["by_day"]] == ["2026-08-31", "2026-09-01"]
    assert [d["cum_r"] for d in ctx["by_day"]] == [0.5, 0.7] and ctx["by_day"][1]["trades"] == 2
    assert [(m["month"], m["trades"], m["net_usd"]) for m in ctx["by_month"]] == [("2026-08", 1, 5.0), ("2026-09", 2, 2.0)]
    assert ev["screen_classification"]["classification"] == "WORTH FURTHER RESEARCH"
    assert [r["enter_new_york"] for r in ev["placebo"]["own_windows"]] == ["05:05", "09:35"] and ev["placebo"]["windows_tested"] == 3
    tod = next(c for c in cd.load_all() if c["id"] == "P7-TOD")
    assert cd.evidence(tod, pre7) == {"available": False}                              # nothing invented when results are absent


def test_longer_history_is_shown_only_when_it_was_stored(pre7):
    fix = next(c for c in cd.load_all() if c["id"] == "P7-FIX")
    assert "history" not in cd.evidence(fix, pre7)                                     # no file: nothing is invented
    stats = {"trades": 4, "win_rate": 0.5, "expectancy_r": 0.05, "profit_factor": 1.1, "sum_r": 0.2, "max_dd_r": 2.0, "net_usd": -3.0,
             "expectancy_r_costs_x2": 0.01, "expectancy_r_costs_x3": -0.02, "extra": 1}
    (pre7 / cd.HISTORY_FILE).write_text(json.dumps({"P7-FIX": {
        "spans": [{"store": "research", "series": "x", "start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z", "bars": 9, "trades": 4}],
        "stats": stats, "concentration": {"days_traded": 2, "positive_days": 1}, "legs": {"AM fix": stats},
        "by_month": {"2026-02": {"trades": 1, "sum_r": 0.4, "net_usd": 2.0, "win_rate": 1.0},
                     "2026-01": {"trades": 3, "sum_r": -0.2, "net_usd": -5.0, "win_rate": 0.333}},
        "positive_months": 1, "months": 2}}), encoding="utf-8")
    h = cd.evidence(fix, pre7)["history"]
    assert h["spans"] == [{"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z", "trades": 4}]
    assert h["stats"]["net_usd"] == -3.0 and "extra" not in h["stats"]
    assert [m["month"] for m in h["by_month"]] == ["2026-01", "2026-02"] and (h["positive_months"], h["months"]) == (1, 2)
    assert (h["days_traded"], h["positive_days"]) == (2, 1)


def test_api_is_read_only_and_says_what_a_candidate_is_not(tmp_path):
    app = create_research_app(Settings(db_path=tmp_path / "app.sqlite3"), store_path=tmp_path / "monthly.sqlite3")
    with TestClient(app, headers={"X-GoldScalp-Request": "1"}) as c:
        r = c.get("/api/monthly/research-candidates")
        assert r.status_code == 200
        body = r.json()
        assert [x["id"] for x in body["candidates"]] == ["P7-FIX", "P7-TOD"] and body["active"] == ["P7-FIX"]
        assert [x["status"] for x in body["candidates"]] == ["research candidate", "archived"]
        assert "not validated" in body["banner"] and "not a validation" in body["banner"]
        assert all(x["check"]["fingerprint_ok"] and x["check"]["matches_declaration"] for x in body["candidates"])
        for method in (c.post, c.put, c.delete):
            assert method("/api/monthly/research-candidates").status_code == 405


def test_library_lists_the_screens_as_development_data(pre7):
    studies = {s["id"]: s for s in lb.prephase7(pre7)}
    assert set(studies) == {"p7s2-screen", "p7s2-context"}
    screen = {r["strategy"]: r for r in studies["p7s2-screen"]["results"]}
    context = {r["strategy"]: r for r in studies["p7s2-context"]["results"]}
    assert studies["p7s2-screen"]["phase"] == "P7" and "screening only" in studies["p7s2-screen"]["flags"]
    assert screen["P7-FIX"]["class"] == "WORTH FURTHER RESEARCH" and context["P7-FIX"]["class"] is None    # only the screen classifies
    assert "nominal R (no stop)" in context["P7-FIX"]["flags"] and "nominal R (no stop)" not in context["P7-XYZ"]["flags"]
    months = context["P7-FIX"]["months"]
    assert months["2026-08"]["partial"] and not months["2026-09"]["partial"] and months["2026-09"]["sum_r"] == 0.2
    assert lb.prephase7(pre7.parent / "missing") == []


def test_monthly_page_has_the_candidates_view():
    web = PROJECT_ROOT / "web"
    html = (web / "monthly.html").read_text(encoding="utf-8")
    assert 'data-view="candidates"' in html and 'id="view-candidates"' in html and 'id="cand-root"' in html
    js = (web / "js" / "candidates.js").read_text(encoding="utf-8")
    assert "/api/monthly/research-candidates" in js and "innerHTML" not in js and "method" not in js   # GET only, safe DOM
    assert '"candidates"' in (web / "js" / "monthly.js").read_text(encoding="utf-8")
