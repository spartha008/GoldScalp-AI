"""UI pages after the interface refresh: every element a page script looks up by a literal id still exists, every page
links to the project overview, and the overview is served and covers every phase."""
from __future__ import annotations

import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from goldscalp.api.app import create_app  # noqa: E402
from goldscalp.core.config import Settings  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = ("index", "paper", "replay", "performance", "lab", "live", "monthly", "project")
SCRIPT_OF = {"monthly": "monthly", "lab": "lab", "index": "dashboard"}


def _ids(text: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', text)) | set(re.findall(r'\bid:\s*"([^"]+)"', text))


def test_every_literal_element_lookup_in_a_page_script_exists():
    for page, script in SCRIPT_OF.items():
        html = (WEB / f"{page}.html").read_text(encoding="utf-8")
        js = (WEB / "js" / f"{script}.js").read_text(encoding="utf-8")
        wanted = set(re.findall(r'getElementById\("([^"$`]+)"\)', js)) | set(re.findall(r'setText\("([^"$`]+)"', js))
        missing = sorted(wanted - _ids(html) - _ids(js))
        assert not missing, f"{page}.html lacks ids used by {script}.js: {missing}"


def test_every_page_links_to_the_project_overview_and_live_validation():
    for page in PAGES:
        text = (WEB / f"{page}.html").read_text(encoding="utf-8")
        assert "/static/project.html" in text, page
        assert '/live"' in text, page


def test_monthly_page_is_organised_in_four_steps_without_stray_section_numbers():
    html = (WEB / "monthly.html").read_text(encoding="utf-8")
    for view in ("data", "evaluate", "risk", "research"):
        assert f'id="view-{view}"' in html and f'data-view="{view}"' in html
    assert not re.search(r"<h2[^>]*>\s*\d+\.", html), "panel titles are no longer numbered"
    assert "Monthly Backtest" in html and "simulation-only" in html


def test_project_overview_is_served_and_covers_every_phase(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "ui.sqlite3", live_provider="none"), provider=None, start_market_data=False)
    with TestClient(app) as c:
        r = c.get("/static/project.html")
        assert r.status_code == 200 and "Project Overview" in r.text
        for phase in ("Phase 0", "Phase 1", "Phase 1.1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "Phase 5.1",
                      "Phase 5.2", "Phase 5.3", "Closure"):
            assert f">{phase}<" in r.text, phase
        assert "SIMULATED" in r.text and "demo" in r.text.lower()
