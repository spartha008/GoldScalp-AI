"""Runs the pure-JavaScript unit tests (tests/js/*.test.mjs) with Node's built-in test runner.
Skipped when Node.js is not installed; the Python suite never needs it."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = sorted((ROOT / "tests" / "js").glob("*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_unit_tests_pass():
    assert JS_TESTS
    result = subprocess.run(["node", "--test", *map(str, JS_TESTS)], cwd=ROOT, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
