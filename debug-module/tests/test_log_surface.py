"""
Tests for debug-module/debuglib/log_surface.py — AC16.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_log_surface():
    try:
        from debuglib import log_surface
        return log_surface
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"log_surface module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/log_surface.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# Synthetic error.log with exactly 3 ERROR lines
# ---------------------------------------------------------------------------

def test_ac16_three_error_lines_returns_three_dicts(log_dir_with_errors):
    """surface() with a log file containing 3 ERROR lines returns exactly 3 dicts."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "database connection failed")
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) == 3, f"Expected exactly 3 dicts, got {len(result)}: {result}"


def test_ac16_each_dict_has_level_containing_error(log_dir_with_errors):
    """Each returned dict has a 'level' field containing 'error' (case-insensitive)."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "database connection failed")
    for entry in result:
        assert "level" in entry, f"Entry missing 'level' key: {entry}"
        assert "error" in entry["level"].lower(), (
            f"'level' does not contain 'error' (case-insensitive): {entry['level']!r}"
        )


def test_ac16_each_dict_has_required_keys(log_dir_with_errors):
    """Each dict has keys: source, line_no, timestamp, message, level."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "database connection failed")
    required = {"source", "line_no", "timestamp", "message", "level"}
    for entry in result:
        missing = required - set(entry.keys())
        assert not missing, f"Entry missing keys {missing}: {entry}"


def test_ac16_source_is_non_empty_string(log_dir_with_errors):
    """Each dict's 'source' field is a non-empty string."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "")
    for entry in result:
        assert isinstance(entry["source"], str) and entry["source"], (
            f"'source' must be non-empty string: {entry}"
        )


def test_ac16_message_is_string(log_dir_with_errors):
    """Each dict's 'message' field is a string."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "")
    for entry in result:
        assert isinstance(entry["message"], str), (
            f"'message' must be a string: {entry}"
        )


# ---------------------------------------------------------------------------
# No log files → empty list, no exception
# ---------------------------------------------------------------------------

def test_ac16_no_log_files_returns_empty_list(empty_log_dir):
    """surface() on a directory with no log files returns empty list without raising."""
    m = _import_log_surface()
    result = m.surface(str(empty_log_dir), "some bug text")
    assert result == [], f"Expected [], got {result!r}"


def test_ac16_nonexistent_path_does_not_raise():
    """surface() on a non-existent path does not raise an exception."""
    m = _import_log_surface()
    result = m.surface("/nonexistent/path/for/test", "bug text")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Cap at 50 entries
# ---------------------------------------------------------------------------

def test_ac16_returns_at_most_50_entries(tmp_path):
    """surface() returns at most 50 entries even when the log has > 50 ERROR lines."""
    lines = [f"2024-01-01T10:{i:02d}:00Z ERROR Error number {i}\n" for i in range(100)]
    (tmp_path / "big.log").write_text("".join(lines))
    m = _import_log_surface()
    result = m.surface(str(tmp_path), "")
    assert len(result) <= 50, f"Expected at most 50 entries, got {len(result)}"


# ---------------------------------------------------------------------------
# Regression: INFO lines must not appear in the error-surface output
# ---------------------------------------------------------------------------

def test_ac16_info_lines_excluded(log_dir_with_errors):
    """INFO lines from the log file are not included in the returned dicts."""
    m = _import_log_surface()
    result = m.surface(str(log_dir_with_errors), "")
    for entry in result:
        level = entry.get("level", "").lower()
        assert "info" not in level or "error" in level, (
            f"INFO entry leaked into error surface: {entry}"
        )


# ---------------------------------------------------------------------------
# AC 13 (task-20260616-002, finding #68): the module docstring must mention
# that .txt files under logs/ are also gathered; .txt-collection behaviour is
# preserved.
# ---------------------------------------------------------------------------

def test_log_surface_docstring_mentions_txt():
    """The module docstring must mention '.txt' so it matches the actual
    _gather_log_files behaviour. FAILS while the docstring only describes
    '*.log files'."""
    m = _import_log_surface()
    docstring = m.__doc__ or ""
    assert ".txt" in docstring, (
        "log_surface module docstring does not mention '.txt'; it must state "
        "that .txt files under logs/ are also gathered. Docstring:\n"
        f"{docstring}"
    )


def test_log_surface_txt_file_still_gathered(tmp_path):
    """A .txt file placed under a logs/ directory is returned by
    _gather_log_files — non-regression guard for the preserved behaviour."""
    m = _import_log_surface()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    txt_file = logs_dir / "service.txt"
    txt_file.write_text("2026-06-15 ERROR boom\n", encoding="utf-8")

    gathered = m._gather_log_files(tmp_path)
    assert txt_file in gathered, (
        f".txt file under logs/ was not gathered: {[str(p) for p in gathered]}"
    )
