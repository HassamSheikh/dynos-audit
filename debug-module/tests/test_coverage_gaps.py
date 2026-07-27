"""
Tests for debug-module/debuglib/coverage_gaps.py — AC12.
"""
import json
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_coverage_gaps():
    try:
        from debuglib import coverage_gaps
        return coverage_gaps
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"coverage_gaps module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/coverage_gaps.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# Istanbul (coverage-final.json)
# ---------------------------------------------------------------------------

def test_ac12_istanbul_coverage_returns_one_gap(istanbul_coverage_dir):
    """Synthetic Istanbul coverage-final.json with one uncovered line returns exactly one gap dict."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) == 1, f"Expected exactly 1 gap, got {len(result)}: {result}"


def test_ac12_istanbul_gap_format_field(istanbul_coverage_dir):
    """The gap dict returned for Istanbul coverage has format == 'istanbul'."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    assert result[0]["format"] == "istanbul", (
        f"Expected format 'istanbul', got {result[0].get('format')!r}"
    )


def test_ac12_istanbul_gap_has_non_empty_uncovered_lines(istanbul_coverage_dir):
    """The Istanbul gap dict has a non-empty uncovered_lines list."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    uncovered = result[0]["uncovered_lines"]
    assert isinstance(uncovered, list)
    assert len(uncovered) > 0, f"uncovered_lines should be non-empty, got {uncovered}"


def test_ac12_istanbul_gap_has_required_keys(istanbul_coverage_dir):
    """Every gap dict contains the required keys: file, uncovered_lines, coverage_pct, format."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    required = {"file", "uncovered_lines", "coverage_pct", "format"}
    for gap in result:
        missing = required - set(gap.keys())
        assert not missing, f"Gap missing required keys {missing}: {gap}"


# ---------------------------------------------------------------------------
# No coverage file present
# ---------------------------------------------------------------------------

def test_ac12_no_coverage_file_returns_empty_list(empty_coverage_dir):
    """find_gaps() on a dir with no coverage files returns empty list without raising."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(empty_coverage_dir), ["TypeScript"])
    assert result == [], f"Expected [], got {result!r}"


def test_ac12_no_coverage_file_empty_languages(empty_coverage_dir):
    """find_gaps() with empty languages list returns empty list without raising."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(empty_coverage_dir), [])
    assert isinstance(result, list)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Data integrity
# ---------------------------------------------------------------------------

def test_ac12_coverage_pct_is_float(istanbul_coverage_dir):
    """coverage_pct in every gap dict is a float (0.0 to 100.0)."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    for gap in result:
        assert isinstance(gap["coverage_pct"], (int, float)), (
            f"coverage_pct must be numeric, got {type(gap['coverage_pct'])}"
        )
        assert 0.0 <= gap["coverage_pct"] <= 100.0, (
            f"coverage_pct out of range: {gap['coverage_pct']}"
        )


def test_ac12_uncovered_lines_are_ints(istanbul_coverage_dir):
    """uncovered_lines contains integers only."""
    m = _import_coverage_gaps()
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    for gap in result:
        for line_no in gap["uncovered_lines"]:
            assert isinstance(line_no, int), (
                f"uncovered_lines entry must be int, got {type(line_no)}: {line_no}"
            )


def test_ac12_format_is_from_allowed_set(istanbul_coverage_dir):
    """The format field value belongs to the allowed set of format strings."""
    m = _import_coverage_gaps()
    allowed = {"istanbul", "pytest-cov-json", "go-cover", "tarpaulin", "lcov", "simplecov"}
    result = m.find_gaps(str(istanbul_coverage_dir), ["TypeScript"])
    for gap in result:
        assert gap["format"] in allowed, (
            f"format {gap['format']!r} not in allowed set {allowed}"
        )


# ---------------------------------------------------------------------------
# AC 6 / AC 7 (task-20260616-002, finding #32): _parse_pytest_cov must NOT
# fall back to excluded_lines. Pragma-excluded lines are not uncovered lines.
# ---------------------------------------------------------------------------

def _write_pytest_cov(tmp_path, files: dict) -> Path:
    """Write a coverage.json (pytest-cov shape) and return its path."""
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"files": files}), encoding="utf-8")
    return path


def test_coverage_gaps_excluded_lines_not_reported(tmp_path):
    """An entry with empty missing_lines but non-empty excluded_lines yields
    NO gap. excluded_lines (pragma: no cover) must not be reported as
    uncovered. FAILS while the `or entry.get("excluded_lines")` fallback
    remains in _parse_pytest_cov."""
    m = _import_coverage_gaps()
    cov = _write_pytest_cov(
        tmp_path,
        {
            "src/app.py": {
                "missing_lines": [],
                "excluded_lines": [10, 11],
                "summary": {"percent_covered": 100.0},
            }
        },
    )
    gaps = m._parse_pytest_cov(cov)
    assert gaps == [], (
        "excluded_lines were reported as a coverage gap; "
        f"expected no gaps, got {gaps!r}"
    )


def test_coverage_gaps_missing_lines_reported(tmp_path):
    """An entry with non-empty missing_lines still produces a gap for those
    lines — non-regression guard ensuring the fallback removal does not
    silence real uncovered lines."""
    m = _import_coverage_gaps()
    cov = _write_pytest_cov(
        tmp_path,
        {
            "src/app.py": {
                "missing_lines": [5, 6],
                "excluded_lines": [],
                "summary": {"percent_covered": 80.0},
            }
        },
    )
    gaps = m._parse_pytest_cov(cov)
    assert len(gaps) == 1, f"expected exactly one gap, got {gaps!r}"
    assert gaps[0]["uncovered_lines"] == [5, 6], (
        f"missing_lines were not reported correctly: {gaps[0]!r}"
    )
