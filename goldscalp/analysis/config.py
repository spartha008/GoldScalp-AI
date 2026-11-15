"""Analysis configuration (config/analysis.json): strict, versioned, fingerprinted.

The shipped file is the reference: a user file must contain exactly the same keys with values of
the same JSON types. Unknown or missing keys are errors (no silent defaults).
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from goldscalp.core.config import PROJECT_ROOT
from goldscalp.core.fingerprint import digest

REFERENCE_PATH = PROJECT_ROOT / "config" / "analysis.json"


class AnalysisConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def _reference() -> dict[str, Any]:
    return json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))


def _check(value: Any, ref: Any, path: str) -> None:
    if isinstance(ref, dict):
        if not isinstance(value, dict):
            raise AnalysisConfigError(f"{path} must be an object")
        if path == "sessions.definitions":   # handled below (list)
            return
        missing, extra = sorted(set(ref) - set(value)), sorted(set(value) - set(ref))
        if missing:
            raise AnalysisConfigError(f"{path or 'config'} is missing {missing}")
        if extra:
            raise AnalysisConfigError(f"{path or 'config'} has unknown keys {extra}")
        for key in ref:
            _check(value[key], ref[key], f"{path}.{key}" if path else key)
    elif isinstance(ref, list):
        if not isinstance(value, list) or not value:
            raise AnalysisConfigError(f"{path} must be a non-empty list")
        for i, item in enumerate(value):
            _check(item, ref[0], f"{path}[{i}]")
    elif isinstance(ref, bool):
        if not isinstance(value, bool):
            raise AnalysisConfigError(f"{path} must be true or false")
    elif isinstance(ref, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise AnalysisConfigError(f"{path} must be a non-negative number")
        if isinstance(ref, int) and not isinstance(ref, bool) and not isinstance(value, int):
            raise AnalysisConfigError(f"{path} must be a whole number")
    elif isinstance(ref, str):
        if not isinstance(value, str) or not value:
            raise AnalysisConfigError(f"{path} must be a non-empty string")


def validate(data: dict[str, Any]) -> dict[str, Any]:
    _check(data, _reference(), "")
    for i, s in enumerate(data["sessions"]["definitions"]):
        _check(s, _reference()["sessions"]["definitions"][0], f"sessions.definitions[{i}]")
        ZoneInfo(s["timezone"])
        for key in ("start", "end"):
            hh, _, mm = s[key].partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise AnalysisConfigError(f"sessions.definitions[{i}].{key} must be HH:MM")
    if data["order_blocks"]["zone"] not in ("range", "body"):
        raise AnalysisConfigError("order_blocks.zone must be 'range' or 'body'")
    if data["volatility"]["fast_atr"] >= data["volatility"]["slow_atr"]:
        raise AnalysisConfigError("volatility.fast_atr must be smaller than slow_atr")
    return data


class AnalysisConfig:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = validate(copy.deepcopy(data))
        self.fingerprint = digest(self.data)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @classmethod
    def load(cls, path: str | Path | None = None) -> AnalysisConfig:
        source = Path(path) if path else REFERENCE_PATH
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AnalysisConfigError(f"cannot read analysis config {source}: {exc}") from exc
        return cls(raw)

    def view(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "config": self.data}

    def with_swing_strength(self, timeframe: str, strength: int) -> AnalysisConfig:
        """A copy with one timeframe's swing strength changed (display sensitivity; new fingerprint)."""
        if not 1 <= int(strength) <= 10:
            raise AnalysisConfigError("swing strength must be between 1 and 10")
        data = copy.deepcopy(self.data)
        data["structure"]["swing_strength"][timeframe] = int(strength)
        return AnalysisConfig(data)


def default_config() -> AnalysisConfig:
    return AnalysisConfig(_reference())
