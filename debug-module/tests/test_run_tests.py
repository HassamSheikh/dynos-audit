"""
Regression tests for debug-module/debuglib/run_tests.py _parse_dart — AC 14
(task-20260616-002, finding #69).

The loose `_DART_PASS` / `_DART_FAIL` regexes (plus-digits / minus-digits) match
any +N / -N token anywhere in the output, so timestamps and flags like
+20240103 inflate the pass count. These tests encode the FIXED, anchored
behaviour: a +N/-N token only counts when preceded by start-of-string or
whitespace AND followed by whitespace or a colon (the Dart summary format).
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_run_tests():
    try:
        from debuglib import run_tests
        return run_tests
    except ModuleNotFoundError as exc:  # pragma: no cover
        pytest.fail(f"run_tests module not importable: {exc}")


# ---------------------------------------------------------------------------
# Negative cases: embedded numeric tokens must NOT be counted.
# ---------------------------------------------------------------------------

def test_dart_parse_ignores_timestamp_tokens():
    """A '+20240103'-style timestamp token must not inflate the pass count, and
    a '--retry 3'-style flag must not inflate the fail count. FAILS while the
    regexes are unanchored (the timestamp currently yields passed=20240103)."""
    rt = _import_run_tests()
    passed, _ = rt._parse_dart("Progress: +20240103 ms")
    assert passed == 0, (
        f"timestamp token inflated the Dart pass count: passed={passed}"
    )
    _, failed = rt._parse_dart("Building with --retry 3 enabled")
    assert failed == 0, (
        f"flag token inflated the Dart fail count: failed={failed}"
    )


def test_dart_parse_ignores_embedded_minus_token():
    """A '-15' embedded inside a non-summary token (no trailing space/colon)
    must not be counted as a failure."""
    rt = _import_run_tests()
    _, failed = rt._parse_dart("offset-15px applied to widget")
    assert failed == 0, f"embedded '-15px' token counted as failure: {failed}"


# ---------------------------------------------------------------------------
# Positive cases: real Dart summary tokens must still parse correctly.
# ---------------------------------------------------------------------------

def test_dart_parse_counts_summary_token():
    """'+3: All tests passed!' yields passed=3."""
    rt = _import_run_tests()
    passed, failed = rt._parse_dart("+3: All tests passed!")
    assert (passed, failed) == (3, 0), f"got {(passed, failed)!r}"


def test_dart_parse_fail_token():
    """'+2 -1: Some tests failed.' yields passed=2, failed=1."""
    rt = _import_run_tests()
    passed, failed = rt._parse_dart("+2 -1: Some tests failed.")
    assert (passed, failed) == (2, 1), f"got {(passed, failed)!r}"


def test_dart_parse_timestamped_summary():
    """'00:01 +3: All tests passed!' yields passed=3 (whitespace-anchored)."""
    rt = _import_run_tests()
    passed, failed = rt._parse_dart("00:01 +3: All tests passed!")
    assert (passed, failed) == (3, 0), f"got {(passed, failed)!r}"
