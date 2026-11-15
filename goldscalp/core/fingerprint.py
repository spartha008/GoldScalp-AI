"""Lossless canonical digests.

Adapted from SpicyStock src/provenance.py _typed() and digest() @ 96ef4732:
type tags keep None/False/0/0.0 distinct and floats are hashed through
float.hex(), so equal digests mean equal values. Decimal support added.
"""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any


def _typed(value: Any) -> list:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite number cannot be fingerprinted")
        return ["float", value.hex()]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite decimal cannot be fingerprinted")
        return ["decimal", str(value.normalize())]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (list, tuple)):
        return ["list", [_typed(v) for v in value]]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return ["dict", [[k, _typed(value[k])] for k in sorted(value)]]
    raise ValueError(f"unsupported type for fingerprint: {type(value).__name__}")


def digest(value: Any) -> str:
    """SHA-256 of the canonical typed JSON form of ``value``."""
    payload = json.dumps(_typed(value), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
