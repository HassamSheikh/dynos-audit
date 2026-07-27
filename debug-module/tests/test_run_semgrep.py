"""
Tests for debug-module/debuglib/run_semgrep.py — AC14.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)

RULES_PATH = str(Path(__file__).parent.parent / "rules" / "silent-accomplices.yml")

VALID_SEMGREP_JSON = {
    "results": [
        {
            "check_id": "silent-accomplices.swallowed-error",
            "path": "src/app.ts",
            "start": {"line": 10},
            "extra": {
                "message": "Swallowed error in empty catch block",
                "severity": "WARNING",
            },
        },
        {
            "check_id": "silent-accomplices.missing-await",
            "path": "src/service.ts",
            "start": {"line": 55},
            "extra": {
                "message": "Missing await on async function call",
                "severity": "ERROR",
            },
        },
    ],
    "errors": [],
}


def _import_run_semgrep():
    try:
        from debuglib import run_semgrep
        return run_semgrep
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"run_semgrep module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/run_semgrep.py to make this test pass."
        )


def _make_fake_process(json_data):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps(json_data)
    fake.stderr = ""
    return fake


# ---------------------------------------------------------------------------
# semgrep not in PATH → structured skip record
# ---------------------------------------------------------------------------

def test_ac14_semgrep_not_installed_returns_skip_record(empty_path, tmp_path):
    """When semgrep is not in PATH, run() returns a list with exactly one skip record."""
    m = _import_run_semgrep()
    result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) == 1, f"Expected exactly 1 skip record, got {len(result)}: {result}"


def test_ac14_semgrep_skip_record_tool_key(empty_path, tmp_path):
    """Skip record has tool == 'semgrep'."""
    m = _import_run_semgrep()
    result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    assert result[0].get("tool") == "semgrep", f"Expected tool='semgrep', got {result[0]}"


def test_ac14_semgrep_skip_record_skipped_true(empty_path, tmp_path):
    """Skip record has skipped == True."""
    m = _import_run_semgrep()
    result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    assert result[0].get("skipped") is True, f"Expected skipped=True, got {result[0]}"


def test_ac14_semgrep_skip_record_reason_key(empty_path, tmp_path):
    """Skip record has a 'reason' key with value 'not installed'."""
    m = _import_run_semgrep()
    result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    assert result[0].get("reason") == "not installed", (
        f"Expected reason='not installed', got {result[0].get('reason')!r}"
    )


# ---------------------------------------------------------------------------
# Mock semgrep subprocess returning valid JSON → expected finding dicts
# ---------------------------------------------------------------------------

def test_ac14_mock_semgrep_returns_finding_dicts(tmp_path):
    """Mock subprocess returning valid semgrep JSON produces finding dicts with required keys."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    assert len(findings) == 2, f"Expected 2 findings, got {len(findings)}: {result}"


def test_ac14_finding_has_rule_id(tmp_path):
    """Each finding dict has a 'rule_id' key."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    for f in findings:
        assert "rule_id" in f, f"Finding missing 'rule_id': {f}"
        assert f["rule_id"], "rule_id must be non-empty"


def test_ac14_finding_has_file(tmp_path):
    """Each finding dict has a 'file' key."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    for f in findings:
        assert "file" in f, f"Finding missing 'file': {f}"


def test_ac14_finding_has_line(tmp_path):
    """Each finding dict has a 'line' key with an integer value."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    for f in findings:
        assert "line" in f, f"Finding missing 'line': {f}"
        assert isinstance(f["line"], int), f"line must be int, got {type(f['line'])}"


def test_ac14_finding_has_message(tmp_path):
    """Each finding dict has a 'message' key."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    for f in findings:
        assert "message" in f, f"Finding missing 'message': {f}"


def test_ac14_finding_has_severity(tmp_path):
    """Each finding dict has a 'severity' key."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    for f in findings:
        assert "severity" in f, f"Finding missing 'severity': {f}"


def test_ac14_finding_values_match_semgrep_output(tmp_path):
    """Finding dict values match the mock semgrep JSON output."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    first = findings[0]
    assert first["rule_id"] == "silent-accomplices.swallowed-error"
    assert first["file"] == "src/app.ts"
    assert first["line"] == 10


def test_ac14_rule_id_filter_respected(tmp_path):
    """When rule_ids=['silent-accomplices.swallowed-error'], only matching findings returned."""
    m = _import_run_semgrep()
    with patch("subprocess.run", return_value=_make_fake_process(VALID_SEMGREP_JSON)):
        result = m.run(
            str(tmp_path), RULES_PATH, ["TypeScript"],
            ["silent-accomplices.swallowed-error"]
        )
    findings = [r for r in result if not r.get("skipped")]
    assert all(
        f["rule_id"] == "silent-accomplices.swallowed-error" for f in findings
    ), f"Unexpected rule IDs in filtered result: {[f['rule_id'] for f in findings]}"


def test_ac14_empty_results_returns_empty_list(tmp_path):
    """Mock semgrep returning 0 results produces an empty findings list."""
    m = _import_run_semgrep()
    empty_output = {"results": [], "errors": []}
    with patch("subprocess.run", return_value=_make_fake_process(empty_output)):
        result = m.run(str(tmp_path), RULES_PATH, ["TypeScript"], None)
    findings = [r for r in result if not r.get("skipped")]
    assert findings == [], f"Expected empty findings, got {findings}"
