"""Forward-data protection: candles from 14 November 2026 onward are reserved, and research may not read them.

The boundary uses the project's canonical convention: UTC, candles keyed by their OPEN time in epoch seconds (``ts``),
and research windows half-open, [start, end). A request *includes* forward-period candles when its end is after the
boundary or its start is at or after it. A window ending exactly at the boundary (2026-11-14T00:00:00Z) is
allowed: its last candle opens before midnight and closes at the boundary.

Two layers, both server-side, both raising ``ForwardDataLocked`` (HTTP 409 in both applications):
  1. ``check_range``: every research request and every computed preset is checked BEFORE any data is read.
  2. ``check_frame`` / ``check_index``: every candle frame a research path loads is checked AFTER reading, so a
     background job, a look-back, an outcome extension or any other read can never carry a forward candle into
     research, whatever route started it.
Nothing is truncated: a request that touches the forward period is refused with the reason.

Raw monitoring is unaffected. Live MT5 collection, raw charts, the dashboard, replay and the live monitor do not use these checks.

Unlocking is impossible without a separate, versioned and REGISTERED research protocol (``forward-unlock@1``):
  - a JSON file in config/research/forward-unlock/<id>.json;
  - listed in that folder's REGISTRY.json with its SHA-256;
  - only for the scope (start/end) and purposes it declares.
There is no setting, environment variable or flag that disables the guard. No protocol is registered.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.core.config import PROJECT_ROOT

DEFINITION = "forward-guard@1"
FORWARD_FROM = datetime(2026, 11, 14, tzinfo=UTC)
FORWARD_FROM_TS = int(FORWARD_FROM.timestamp())          # 1794614400
UNLOCK_DIR = PROJECT_ROOT / "config" / "research" / "forward-unlock"
UNLOCK_DEFINITION = "forward-unlock@1"


class ForwardDataLocked(RuntimeError):
    """A research request or read touched the reserved forward period."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.forward_from = FORWARD_FROM.isoformat().replace("+00:00", "Z")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _iso(t: datetime) -> str:
    return _aware(t).astimezone(UTC).isoformat().replace("+00:00", "Z")


def includes_forward(start: datetime | None, end: datetime) -> bool:
    """Does the half-open window [start, end) include any forward-period candle?"""
    return _aware(end) > FORWARD_FROM or (start is not None and _aware(start) >= FORWARD_FROM)


# ------------------------------------------------------------------ unlock protocols (none registered)
def _body_hash(doc: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def registered_protocol(protocol_id: str, unlock_dir: Path | None = None) -> dict[str, Any]:
    """The registered unlock protocol, or ForwardDataLocked if it is missing, unregistered, changed or malformed."""
    d = unlock_dir or UNLOCK_DIR
    path, reg_path = d / f"{protocol_id}.json", d / "REGISTRY.json"
    if not path.is_file() or not reg_path.is_file():
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} is not registered")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} is unreadable: {exc}") from exc
    entry = (registry.get("protocols") or {}).get(protocol_id)
    if registry.get("definition") != UNLOCK_DEFINITION or not entry:
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} is not in the registry")
    if doc.get("definition") != UNLOCK_DEFINITION or doc.get("id") != protocol_id or not isinstance(doc.get("version"), int):
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} is not a versioned {UNLOCK_DEFINITION} document")
    if entry.get("sha256") != _body_hash(doc) or entry.get("version") != doc["version"]:
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} changed after it was registered")
    scope = doc.get("scope") or {}
    if not scope.get("start") or not scope.get("end") or not doc.get("purposes"):
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} declares no scope or purposes")
    return doc


def _unlocked(protocol_id: str | None, purpose: str, start: datetime | None, end: datetime) -> bool:
    if protocol_id is None:
        return False
    doc = registered_protocol(protocol_id)
    lo = datetime.fromisoformat(doc["scope"]["start"].replace("Z", "+00:00"))
    hi = datetime.fromisoformat(doc["scope"]["end"].replace("Z", "+00:00"))
    if purpose not in doc["purposes"]:
        raise ForwardDataLocked(f"forward-unlock protocol {protocol_id!r} does not cover {purpose!r}")
    if _aware(end) > hi or (start is not None and _aware(start) < lo):
        raise ForwardDataLocked(f"the request lies outside forward-unlock protocol {protocol_id!r}'s scope")
    return True


# ------------------------------------------------------------------ the checks
def check_range(start: datetime | None, end: datetime, what: str, *, purpose: str = "research",
                protocol_id: str | None = None) -> None:
    """Layer 1: refuse a research window that includes forward-period candles (never truncate it)."""
    if not includes_forward(start, end):
        return
    if _unlocked(protocol_id, purpose, start, end):
        return
    span = f"{_iso(start)} – {_iso(end)}" if start is not None else f"up to {_iso(end)}"
    raise ForwardDataLocked(
        f"{what} ({span}) includes forward-period candles: data from {_iso(FORWARD_FROM)} onward is reserved and may "
        f"not be used for research. Choose a range that ends at or before {_iso(FORWARD_FROM)}; it is never "
        "shortened automatically.")


def check_index(index: Any, what: str, *, purpose: str = "research", protocol_id: str | None = None) -> None:
    """Layer 2: refuse loaded candles that include a forward-period candle (index of UTC open times)."""
    if index is None or len(index) == 0:
        return
    last = index.max()
    last_dt = last.to_pydatetime() if hasattr(last, "to_pydatetime") else last
    if isinstance(last_dt, (int, float)):
        last_dt = datetime.fromtimestamp(last_dt, UTC)
    if _aware(last_dt) < FORWARD_FROM:
        return
    if _unlocked(protocol_id, purpose, None, _aware(last_dt)):
        return
    raise ForwardDataLocked(
        f"{what} would read forward-period candles (latest {_iso(last_dt)}): data from {_iso(FORWARD_FROM)} onward is "
        "reserved and may not be used for research. Choose an earlier range; it is never shortened automatically.")


def check_frame(frame: Any, what: str, **kw: Any) -> None:
    check_index(getattr(frame, "index", None), what, **kw)


def check_dataset(ds: Any, what: str, **kw: Any) -> None:
    """A features.Dataset-like object: its base frame and every history frame."""
    check_frame(getattr(ds, "base", None), what, **kw)
    for tf, frame in (getattr(ds, "history", None) or {}).items():
        check_frame(frame, f"{what} ({tf} history)", **kw)


def status(unlock_dir: Path | None = None) -> dict[str, Any]:
    d = unlock_dir or UNLOCK_DIR
    reg = d / "REGISTRY.json"
    protocols = []
    if reg.is_file():
        try:
            protocols = sorted(json.loads(reg.read_text(encoding="utf-8")).get("protocols") or {})
        except ValueError:
            protocols = ["<unreadable registry>"]
    return {"definition": DEFINITION, "forward_from": _iso(FORWARD_FROM), "forward_from_ts": FORWARD_FROM_TS,
            "locked": True, "convention": "UTC; candles keyed by open time; research windows [start, end)",
            "registered_unlock_protocols": protocols,
            "unlocking": "only through a versioned forward-unlock@1 protocol registered in "
                         "config/research/forward-unlock/REGISTRY.json; no setting or flag disables the guard",
            "unaffected": ["live MT5 collection", "raw charts and market endpoints", "dashboard", "replay",
                           "live monitor"]}
