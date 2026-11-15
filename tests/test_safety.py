"""Safety and separation guarantees, checked mechanically against the source tree."""
from __future__ import annotations

import ast
import base64
import hashlib
import re
from pathlib import Path

import pytest

from goldscalp.api.app import LWC_STYLE_HASH
from goldscalp.core.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "goldscalp"
WEB = ROOT / "web"
PY_FILES = sorted(PACKAGE.rglob("*.py"))
JS_FILES = sorted((WEB / "js").glob("*.js"))

# Order-placement and broker-execution identifiers that must never be CALLED or IMPORTED.
FORBIDDEN_CODE = [
    r"\border_send\s*\(", r"\bOrderSend\w*\s*\(", r"\bCTrade\b", r"import\s+MetaTrader5", r"from\s+MetaTrader5",
    r"oandapyV20", r"/v3/accounts/[^\"']*/orders", r"\bplace_order\s*\(", r"\bsubmit_order\s*\(",
    r"alpaca\.trading", r"\bTradingClient\b",
    # MetaTrader 5 trading / account functions: none may appear anywhere in GoldScalp's code.
    r"\border_check\b", r"\border_calc_\w+", r"\bpositions_(get|total)\b", r"\borders_(get|total)\b",
    r"\bhistory_(deals|orders)_(get|total)\b", r"\baccount_info\b", r"\blogin\s*\(",
]
# The ONE exception: the opt-in MT5 DEMO-account gateway (off by default) may call these MT5 trading functions, and
# only through its demo-only proxy (tests/test_demo_execution.py). Every other pattern above stays forbidden there too.
DEMO_GATEWAY = PACKAGE / "execution" / "mt5_demo.py"
DEMO_GATEWAY_MAY_USE = {r"\border_send\s*\(", r"\border_check\b", r"\bpositions_(get|total)\b",
                        r"\bhistory_(deals|orders)_(get|total)\b", r"\baccount_info\b"}


def _code_only(path: Path) -> str:
    """Source without comments/docstrings for Python, so documentation may name what is forbidden."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return re.sub(r"//[^\n]*", "", text)
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                first.value.value = ""
    return re.sub(r"#[^\n]*", "", ast.unparse(tree))


@pytest.mark.parametrize("path", PY_FILES + JS_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_broker_execution_code(path):
    code = _code_only(path)
    for pattern in FORBIDDEN_CODE:
        if path == DEMO_GATEWAY and pattern in DEMO_GATEWAY_MAY_USE:
            continue
        assert not re.search(pattern, code), f"{path.name} contains forbidden broker-execution code: {pattern}"


def test_only_the_demo_execution_package_reaches_the_raw_mt5_module():
    for path in PY_FILES:
        if "call_on_mt5_thread" in _code_only(path):
            rel = path.relative_to(PACKAGE).as_posix()
            assert rel.startswith("execution/") or rel == "data/providers/mt5_provider.py", rel
    assert DEMO_GATEWAY.is_file()


@pytest.mark.parametrize("package", ["analysis", "backtesting", "simulation", "strategies", "live", "research",
                                     "assistant", "monthly", "data", "storage"])
def test_no_other_package_imports_the_demo_execution_package(package):
    for path in (PACKAGE / package).rglob("*.py"):
        assert not {m for m in _imports(path) if m.startswith("goldscalp.execution")}, path.relative_to(ROOT)


def test_demo_execution_is_off_by_default_including_the_example_configuration():
    assert Settings().demo_execution_enabled is False
    assert Settings(_env_file=str(ROOT / ".env.example")).demo_execution_enabled is False


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package", ["analysis", "simulation"])
def test_engines_never_import_market_data_providers(package):
    for path in (PACKAGE / package).rglob("*.py"):
        bad = {m for m in _imports(path) if m.startswith("goldscalp.data.providers") or m.startswith("goldscalp.data.live")
               or m in ("httpx", "websockets", "requests")}
        assert not bad, f"{path.relative_to(ROOT)} imports {bad}"


def test_simulation_never_imports_a_broker_sdk():
    for path in (PACKAGE / "simulation").rglob("*.py"):
        assert not {m for m in _imports(path) if m.split(".")[0] in ("MetaTrader5", "oandapyV20", "alpaca", "ib_insync")}


def test_core_runs_without_metatrader5(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)   # make it unimportable
    import importlib
    import goldscalp.api.app as app_module
    importlib.reload(app_module)


def test_tradingview_data_never_reaches_python_or_analysis():
    for path in PY_FILES:
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in ("api/app.py", "core/config.py"):
            continue  # only the optional display route, its CSP and its on/off setting
        assert "tradingview" not in path.read_text(encoding="utf-8").lower(), f"{rel} mentions TradingView"


def test_only_the_tradingview_module_touches_the_widget_frame():
    for path in JS_FILES:
        text = path.read_text(encoding="utf-8")
        if path.name in ("tradingview.js", "tv-widget-page.js"):
            continue
        assert "tv-widget" not in text and "contentWindow" not in text, f"{path.name} touches the TradingView frame"
    page = (WEB / "js" / "tv-widget-page.js").read_text(encoding="utf-8")
    assert "fetch(" not in page and "/api/" not in page   # the widget page never calls the GoldScalp API


def test_no_inner_html_in_frontend():
    for path in JS_FILES:
        assert "innerHTML" not in _code_only(path), path.name


def test_csp_style_hash_matches_the_pinned_chart_library():
    css = ("a#tv-attr-logo{--fill:#131722;--stroke:#fff;position:absolute;left:10px;bottom:10px;height:19px;"
           "width:35px;margin:0;padding:0;border:0;z-index:3;}a#tv-attr-logo[data-dark]{--fill:#D1D4DC;--stroke:#131722;}")
    expected = "'sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode() + "'"
    assert LWC_STYLE_HASH == expected
    vendored = (WEB / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
    assert "Lightweight Charts™ v5.2.1" in vendored
    assert "tv-attr-logo" in vendored


def test_secrets_are_not_exposed_in_settings_repr():
    s = Settings(tiingo_api_key="super-secret-key")
    assert "super-secret-key" not in repr(s)


def test_unknown_settings_are_refused(monkeypatch):
    monkeypatch.setenv("GOLDSCALP_TIINGO_APIKEY", "typo")
    with pytest.raises(ValueError, match="unknown GoldScalp setting"):
        load_settings()


def test_provider_auto_selection():
    assert Settings(tiingo_api_key=None).resolved_provider() == "mt5"          # MT5 preferred when enabled
    assert Settings(mt5_enabled=False, tiingo_api_key=None).resolved_provider() == "gold-api"
    assert Settings(mt5_enabled=False, tiingo_api_key="k").resolved_provider() == "tiingo"
    assert Settings(live_provider="none").resolved_provider() == "none"


def test_env_example_is_a_valid_configuration():
    settings = Settings(_env_file=str(ROOT / ".env.example"))
    assert settings.host == "127.0.0.1"
    assert settings.tiingo_api_key is None and settings.mt5_symbol == ""
    assert settings.paper_starting_balance == "10000" and settings.paper_leverage == 1


def test_only_the_mt5_provider_loads_the_metatrader5_package():
    for path in PY_FILES:
        code = _code_only(path)
        if 'import_module("MetaTrader5")' in code or "import_module('MetaTrader5')" in code:
            assert path.name == "mt5_provider.py", f"{path.name} loads MetaTrader5"


def test_mt5_proxy_allowlist_is_market_data_only():
    from goldscalp.data.providers.mt5_provider import ALLOWED
    assert ALLOWED == {"initialize", "shutdown", "last_error", "version", "terminal_info", "symbols_total",
                       "symbols_get", "symbol_info", "symbol_select", "symbol_info_tick", "copy_rates_from_pos",
                       "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_H1"}


def test_settings_have_no_broker_credential_fields():
    for name in Settings.model_fields:
        assert not any(word in name for word in ("password", "passwd", "login", "mt5_server", "mt5_account", "broker")), name


def test_app_starts_with_mt5_unavailable_when_the_package_is_missing(monkeypatch, tmp_path):
    import sys
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    from goldscalp.api.app import create_app, provider_options
    settings = Settings(db_path=tmp_path / "x.sqlite3")
    mt5 = next(o for o in provider_options(settings) if o.id == "mt5")
    assert mt5.available is False and "not installed" in (mt5.reason or "") or sys.platform != "win32"
    app = create_app(settings, start_market_data=False)
    assert app.state.gs.market.active != "mt5"


# ------------------------------------------------------------------------------------------------ Phase 4 boundaries
@pytest.mark.parametrize("package", ["live", "research", "assistant"])
def test_phase4_packages_never_touch_providers_brokers_or_the_network(package):
    for path in (PACKAGE / package).rglob("*.py"):
        bad = {m for m in _imports(path) if m.startswith(("goldscalp.data.providers", "goldscalp.data.live",
                                                           "goldscalp.data.hub"))
               or m.split(".")[0] in ("httpx", "websockets", "requests", "MetaTrader5", "anthropic", "socket")}
        assert not bad, f"{path.relative_to(ROOT)} imports {bad}"


def test_only_the_assistant_llm_module_loads_the_anthropic_sdk_and_only_lazily():
    for path in PY_FILES:
        code = _code_only(path)
        if "anthropic" in _imports(path) or re.search(r"import_module\(\s*[\"']anthropic", code):
            assert path.name == "llm.py" and "anthropic" not in _imports(path), path.name


def test_assistant_tools_are_read_only():
    code = _code_only(PACKAGE / "assistant" / "tools.py")
    assert "transaction(" not in code and not re.search(r"(INSERT|UPDATE|DELETE)", code)
    assert ".open_position(" not in code and ".close_position(" not in code and "strategy_repo" not in code


def test_mt5_terminal_facts_never_include_account_fields():
    src = (PACKAGE / "data" / "providers" / "mt5_provider.py").read_text(encoding="utf-8")
    block = src[src.index("self.terminal = {"):src.index("if not terminal.connected")]
    for word in ("login", "balance", "equity", "margin", "account", "password", "server"):
        assert word not in block.lower(), word
