"""
Tests for debug-module/debuglib/dossier.py — AC18.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_dossier():
    try:
        from debuglib import dossier
        return dossier
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"dossier module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/dossier.py to make this test pass."
        )


def _make_pipeline_output(semgrep_count=0, coverage_gap_count=0):
    """Build a minimal pipeline output dict for testing."""
    semgrep_findings = [
        {
            "rule_id": f"rule-{i}",
            "file": f"src/file{i}.ts",
            "line": i * 10,
            "message": f"Finding {i}",
            "severity": "WARNING",
        }
        for i in range(1, semgrep_count + 1)
    ]
    coverage_gaps = [
        {
            "file": f"src/module{i}.ts",
            "uncovered_lines": [i * 5],
            "coverage_pct": 75.0,
            "format": "istanbul",
        }
        for i in range(1, coverage_gap_count + 1)
    ]
    return {
        "bug_text": "test bug",
        "repo_path": "/tmp/repo",
        "bug_type": "logic-bug",
        "languages": ["TypeScript"],
        "stack_frames": [],
        "linter_findings": [],
        "semgrep_findings": semgrep_findings,
        "git_forensics": {"recent_commits": [], "blame_ranges": {}, "co_change_pairs": []},
        "log_entries": [],
        "schema_drift": [],
        "coverage_gaps": coverage_gaps,
        "pipeline_errors": [],
    }


# ---------------------------------------------------------------------------
# ID minting: 2 semgrep + 3 coverage gaps → specific IDs
# ---------------------------------------------------------------------------

def test_ac18_semgrep_ids_minted():
    """assemble() with 2 semgrep findings mints S-001 and S-002."""
    m = _import_dossier()
    pipeline = _make_pipeline_output(semgrep_count=2, coverage_gap_count=3)
    result = m.assemble(pipeline)
    evidence_index = result.get("evidence_index", {})
    assert "S-001" in evidence_index, f"S-001 not in evidence_index: {list(evidence_index.keys())}"
    assert "S-002" in evidence_index, f"S-002 not in evidence_index: {list(evidence_index.keys())}"


def test_ac18_coverage_gap_ids_minted():
    """assemble() with 3 coverage gaps mints CG-001, CG-002, CG-003."""
    m = _import_dossier()
    pipeline = _make_pipeline_output(semgrep_count=2, coverage_gap_count=3)
    result = m.assemble(pipeline)
    evidence_index = result.get("evidence_index", {})
    assert "CG-001" in evidence_index, f"CG-001 missing: {list(evidence_index.keys())}"
    assert "CG-002" in evidence_index, f"CG-002 missing: {list(evidence_index.keys())}"
    assert "CG-003" in evidence_index, f"CG-003 missing: {list(evidence_index.keys())}"


def test_ac18_all_five_ids_present_together():
    """With 2 semgrep + 3 gaps, all 5 IDs are in evidence_index simultaneously."""
    m = _import_dossier()
    pipeline = _make_pipeline_output(semgrep_count=2, coverage_gap_count=3)
    result = m.assemble(pipeline)
    index = result.get("evidence_index", {})
    for expected_id in ("S-001", "S-002", "CG-001", "CG-002", "CG-003"):
        assert expected_id in index, (
            f"Expected ID {expected_id!r} in evidence_index, got: {list(index.keys())}"
        )


# ---------------------------------------------------------------------------
# Counter resets between assemble() calls
# ---------------------------------------------------------------------------

def test_ac18_counter_resets_between_calls():
    """Second assemble() call starts S-001 again, not S-003."""
    m = _import_dossier()
    pipeline_a = _make_pipeline_output(semgrep_count=2, coverage_gap_count=0)
    pipeline_b = _make_pipeline_output(semgrep_count=1, coverage_gap_count=0)

    m.assemble(pipeline_a)  # First call — mints S-001, S-002
    result_b = m.assemble(pipeline_b)  # Second call — must start fresh at S-001

    index_b = result_b.get("evidence_index", {})
    assert "S-001" in index_b, (
        f"Second assemble() call must mint S-001 (counter reset), got: {list(index_b.keys())}"
    )
    assert "S-003" not in index_b, (
        f"S-003 must not appear — counter must reset, got: {list(index_b.keys())}"
    )


def test_ac18_counter_reset_for_coverage_gaps():
    """Second assemble() call starts CG-001 again."""
    m = _import_dossier()
    pipeline_a = _make_pipeline_output(semgrep_count=0, coverage_gap_count=3)
    pipeline_b = _make_pipeline_output(semgrep_count=0, coverage_gap_count=2)

    m.assemble(pipeline_a)
    result_b = m.assemble(pipeline_b)

    index_b = result_b.get("evidence_index", {})
    assert "CG-001" in index_b
    assert "CG-004" not in index_b, (
        "CG-004 must not appear — counter must reset between calls"
    )


# ---------------------------------------------------------------------------
# Evidence index completeness
# ---------------------------------------------------------------------------

def test_ac18_evidence_index_payload_is_non_null():
    """Every ID in evidence_index maps to a non-null payload."""
    m = _import_dossier()
    pipeline = _make_pipeline_output(semgrep_count=2, coverage_gap_count=2)
    result = m.assemble(pipeline)
    for id_key, payload in result["evidence_index"].items():
        assert payload is not None, f"Payload for {id_key!r} is None"


def test_ac18_dossier_contains_investigation_id():
    """Assembled dossier contains a non-empty 'investigation_id' string."""
    m = _import_dossier()
    pipeline = _make_pipeline_output()
    result = m.assemble(pipeline)
    assert "investigation_id" in result, "'investigation_id' key missing from dossier"
    assert isinstance(result["investigation_id"], str) and result["investigation_id"], (
        "investigation_id must be a non-empty string"
    )


def test_ac18_dossier_contains_pipeline_errors():
    """Assembled dossier contains a 'pipeline_errors' list."""
    m = _import_dossier()
    pipeline = _make_pipeline_output()
    result = m.assemble(pipeline)
    assert "pipeline_errors" in result
    assert isinstance(result["pipeline_errors"], list)


def test_ac18_dossier_contains_evidence_index():
    """Assembled dossier contains an 'evidence_index' dict."""
    m = _import_dossier()
    pipeline = _make_pipeline_output()
    result = m.assemble(pipeline)
    assert "evidence_index" in result
    assert isinstance(result["evidence_index"], dict)


# ---------------------------------------------------------------------------
# ID format: zero-padded to 3 digits
# ---------------------------------------------------------------------------

def test_ac18_ids_are_zero_padded():
    """Minted IDs are zero-padded to 3 digits (e.g. S-001, not S-1)."""
    m = _import_dossier()
    pipeline = _make_pipeline_output(semgrep_count=1)
    result = m.assemble(pipeline)
    index = result["evidence_index"]
    semgrep_ids = [k for k in index if k.startswith("S-")]
    for s_id in semgrep_ids:
        suffix = s_id.split("-")[1]
        assert len(suffix) == 3, f"ID suffix not 3 digits: {s_id!r}"
        assert suffix.isdigit(), f"ID suffix not numeric: {s_id!r}"
