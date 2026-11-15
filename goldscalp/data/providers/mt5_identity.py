"""Who is the MT5 terminal connected to? Broker identity and demo/live environment, WITHOUT account data.

PHASE4_ARCHITECTURE.md section 3.1-3.2.

- Broker: ``terminal_info().company`` as the terminal reports it. Vantage is recognised by the word
  "Vantage" in the company name (Vantage Global Prime Pty Ltd, Vantage International Group Limited, ...).
- Environment: the market-data path may not call ``account_info`` (read-only market-data allowlist), so the
  demo environment is read from the terminal's own configuration file ``<data_path>/config/common.ini``.
  (Only the opt-in demo-execution gateway, goldscalp/execution/mt5_demo.py, reads ``account_info``: to refuse
  any account that is not a DEMO account before each order.)
  ONLY the ``Server=`` value of the ``[Common]`` section is extracted; every other key of that file
  (including the account number) is discarded by the parser and never returned, stored or logged.
  The file names the server of the terminal's most recent login, so the evidence says exactly that.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

VANTAGE = re.compile(r"\bvantage\b", re.IGNORECASE)
# Vantage trade servers are named VantageMarkets-Demo, VantageInternational-Live 3, ... ("Advantage..." is not).
VANTAGE_SERVER = re.compile(r"^vantage", re.IGNORECASE)
_SERVER_OK = re.compile(r"^[A-Za-z0-9 ._\-()]{1,64}$")
MAX_INI_BYTES = 256 * 1024


def broker_identity(company: str | None) -> dict[str, Any]:
    name = (company or "").strip()
    if not name:
        return {"company": None, "is_vantage": False,
                "evidence": "the terminal reported no company name (not connected to a broker yet?)"}
    vantage = bool(VANTAGE.search(name))
    return {"company": name, "is_vantage": vantage,
            "evidence": f"terminal_info().company = {name!r}" + ("" if vantage else " (not Vantage)")}


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if len(raw) > 1 and raw[1:2] == b"\x00":          # UTF-16 LE without a byte-order mark
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def read_server_name(path: Path) -> str | None:
    """The ``Server=`` value of the [Common] section, or None. Nothing else leaves this function."""
    raw = path.read_bytes()[:MAX_INI_BYTES]
    section = None
    for line in _decode(raw).splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section == "common" and s.lower().startswith("server="):
            value = s.split("=", 1)[1].strip()
            return value if _SERVER_OK.match(value) else None
    return None


def classify_server(server: str | None) -> tuple[str, str]:
    if not server:
        return "UNKNOWN", "no server name available"
    low = server.lower()
    if "demo" in low:
        return "DEMO", f"server name {server!r} contains 'demo'"
    if "live" in low or "real" in low:
        return "LIVE", f"server name {server!r} indicates a LIVE (real-money) server; GoldScalp still only reads market data"
    return "UNKNOWN", f"server name {server!r} does not say demo or live"


NON_SERVER_FOLDERS = {"custom", "default", "signals"}
SCAN_LIMIT = 50_000


def server_activity(data_path: str | None) -> dict[str, Any]:
    """Trade-server data folders under ``<data_path>/bases`` with the time of their newest file write.

    METADATA ONLY (folder names and modification times; no file is opened). The terminal keeps one folder per
    trade server it has used and writes the connected server's quotes/history there."""
    if not data_path:
        return {"servers": [], "note": "no data folder"}
    base = Path(data_path) / "bases"
    try:
        folders = [d for d in base.iterdir() if d.is_dir() and d.name.lower() not in NON_SERVER_FOLDERS]
    except OSError as exc:
        return {"servers": [], "note": f"could not list {base.name} ({type(exc).__name__})"}
    out = []
    for folder in folders:
        newest, seen = None, 0
        for path in folder.rglob("*"):
            seen += 1
            if seen > SCAN_LIMIT:
                break
            try:
                if path.is_file():
                    m = path.stat().st_mtime
                    newest = m if newest is None or m > newest else newest
            except OSError:
                continue
        out.append({"server": folder.name, "newest_write_epoch": newest})
    out.sort(key=lambda s: -(s["newest_write_epoch"] or 0))
    return {"servers": out, "note": None}


def identify_broker(company: str | None, *, connected: bool, env: dict[str, Any],
                    activity: dict[str, Any] | None) -> dict[str, Any]:
    """Vantage identification without account data.

    1. A Vantage-branded terminal reports the broker in ``terminal_info().company``.
    2. A generic MetaQuotes build reports 'MetaQuotes Ltd.' there (the terminal's distributor, not the account's
       broker). Then the trade server decides: the server of the most recent login (config/common.ini) must name
       Vantage AND be the trade-server data folder the terminal writes to (the only one, or the most recently
       written), while the terminal is connected."""
    ident = broker_identity(company)
    if ident["is_vantage"]:
        return {**ident, "basis": "terminal company", "status": "PASS"}
    server = env.get("server")
    servers = (activity or {}).get("servers") or []
    names = [s["server"] for s in servers]
    active = names[0] if names else None
    consistent = bool(server) and (server.lower() == (active or "").lower()
                                   or (len(names) == 1 and names[0].lower() == server.lower()))
    server_vantage = bool(server and VANTAGE_SERVER.match(server))
    facts = (f"terminal_info().company = {company!r} (the terminal build's distributor); trade server of the most "
             f"recent login: {server!r}; trade-server data folders: {', '.join(names) or 'none'}"
             + (f" (most recently written: {active})" if active else ""))
    if server_vantage and consistent and connected:
        return {"company": company, "is_vantage": True, "basis": "trade server", "status": "PASS",
                "server": server, "evidence": facts + f"; the connected terminal is using {server}, a Vantage server"}
    if server_vantage:
        why = "the terminal is not connected" if not connected else \
            "the active trade server could not be confirmed from the data folders"
        return {"company": company, "is_vantage": False, "basis": "trade server", "status": "WARN",
                "server": server, "evidence": facts + f"; the last login was a Vantage server, but {why}"}
    return {"company": company, "is_vantage": False, "basis": "none", "status": "FAIL", "server": server,
            "evidence": facts + "; neither the terminal company nor the trade server names Vantage"}


def environment(data_path: str | None, *, enabled: bool = True,
                reader: Callable[[Path], str | None] = read_server_name) -> dict[str, Any]:
    """DEMO / LIVE / UNKNOWN with the evidence. Never raises."""
    if not enabled:
        return {"environment": "UNKNOWN", "server": None, "source": None,
                "evidence": "environment detection is disabled (GOLDSCALP_MT5_DETECT_ENVIRONMENT=false)"}
    if not data_path:
        return {"environment": "UNKNOWN", "server": None, "source": None,
                "evidence": "the terminal reported no data folder"}
    path = Path(data_path) / "config" / "common.ini"
    try:
        server = reader(path)
    except OSError as exc:
        return {"environment": "UNKNOWN", "server": None, "source": str(path),
                "evidence": f"could not read the terminal configuration file ({type(exc).__name__})"}
    env, why = classify_server(server)
    return {"environment": env, "server": server, "source": str(path),
            "evidence": why + " (server of the terminal's most recent login, from its configuration file; "
                              "no account data was read)"}
