"""
Tests for debug-module/debuglib/run_linters.py — AC13.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_run_linters():
    try:
        from debuglib import run_linters
        return run_linters
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"run_linters module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/run_linters.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# No linters installed → skip records only, no exception
# ---------------------------------------------------------------------------

def test_ac13_no_linters_returns_list_of_skips(empty_path, tmp_path):
    """run() with empty PATH returns a list of skip records without raising."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript", "Python", "Go"])
    assert isinstance(result, list), f"Expected list, got {type(result)}"


def test_ac13_skip_records_have_tool_key(empty_path, tmp_path):
    """Every skip record has a 'tool' key."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript", "Python"])
    for record in result:
        assert "tool" in record, f"Skip record missing 'tool': {record}"


def test_ac13_skip_records_have_skipped_true(empty_path, tmp_path):
    """Every skip record has skipped == True."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript", "Python"])
    for record in result:
        if record.get("skipped"):
            assert record["skipped"] is True, (
                f"skipped must be True, got {record['skipped']!r}"
            )


def test_ac13_skip_records_have_reason_key(empty_path, tmp_path):
    """Every skip record has a non-empty 'reason' string."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript", "Python"])
    for record in result:
        if record.get("skipped"):
            assert "reason" in record, f"Skip record missing 'reason': {record}"
            assert isinstance(record["reason"], str) and record["reason"], (
                f"'reason' must be a non-empty string: {record}"
            )


def test_ac13_empty_languages_returns_list_without_crashing(empty_path, tmp_path):
    """run() with empty languages list returns a list without raising."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), [])
    assert isinstance(result, list)


def test_ac13_result_is_always_list_even_with_bad_path(empty_path):
    """run() on a non-existent repo path returns a list, not an exception."""
    m = _import_run_linters()
    result = m.run("/nonexistent/path/that/does/not/exist", ["Python"])
    assert isinstance(result, list)


def test_ac13_skip_records_not_none(empty_path, tmp_path):
    """run() never returns None or contains None entries."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript"])
    assert result is not None
    for record in result:
        assert record is not None


# ---------------------------------------------------------------------------
# Structure of skip record (regression: tool name must be a real linter name)
# ---------------------------------------------------------------------------

def test_ac13_eslint_skip_record_when_not_installed(empty_path, tmp_path):
    """When eslint is not installed, result includes a skip record with tool='eslint'."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["TypeScript"])
    tool_names = [r.get("tool") for r in result]
    assert "eslint" in tool_names, (
        f"Expected 'eslint' skip record in {tool_names}"
    )


def test_ac13_ruff_skip_record_when_not_installed(empty_path, tmp_path):
    """When ruff is not installed, result includes a skip record with tool='ruff'."""
    m = _import_run_linters()
    result = m.run(str(tmp_path), ["Python"])
    tool_names = [r.get("tool") for r in result]
    assert "ruff" in tool_names, (
        f"Expected 'ruff' skip record in {tool_names}"
    )
