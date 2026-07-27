"""
Tests for debug-module/debuglib/render_report.py — AC19, AC20.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)

RENDER_REPORT_SCRIPT = str(Path(__file__).parent.parent / "debuglib" / "render_report.py")


def _import_render_report():
    try:
        from debuglib import render_report
        return render_report
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"render_report module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/render_report.py to make this test pass."
        )


def _make_dossier(evidence_ids=None):
    """Build a minimal dossier with the given evidence IDs pre-populated."""
    index = {}
    if evidence_ids:
        for eid in evidence_ids:
            index[eid] = {"payload": f"data for {eid}"}
    return {
        "investigation_id": "INV-test-001",
        "bug_text": "test bug",
        "bug_type": "logic-bug",
        "repo_path": "/tmp/repo",
        "languages_detected": ["TypeScript"],
        "pipeline_errors": [],
        "evidence_index": index,
    }


def _make_bug_report(evidence_ids_per_step=None, root_cause_ids=None, fix_ids=None):
    """Build a minimal bug_report JSON citing the given IDs."""
    if evidence_ids_per_step is None:
        evidence_ids_per_step = [["S-001"]]
    if root_cause_ids is None:
        root_cause_ids = evidence_ids_per_step[0]
    if fix_ids is None:
        fix_ids = evidence_ids_per_step[0]

    causal_chain = [
        {"step": i + 1, "description": f"Step {i + 1}", "evidence_ids": ids}
        for i, ids in enumerate(evidence_ids_per_step)
    ]
    return {
        "investigation_id": "INV-test-001",
        "causal_chain": causal_chain,
        "root_cause": {
            "description": "Root cause description",
            "evidence_ids": root_cause_ids,
        },
        "recommended_fix": {
            "description": "Fix description",
            "locations": [{"file": "src/app.ts", "line": 10}],
            "evidence_ids": fix_ids,
        },
    }


# ---------------------------------------------------------------------------
# AC19: Missing citation → WARNING block
# ---------------------------------------------------------------------------

def test_ac19_missing_citation_produces_warning():
    """render() with a bug_report citing S-999 (absent from dossier) includes WARNING."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-999"]],
        root_cause_ids=["S-999"],
        fix_ids=["S-999"],
    )
    output = m.render(report, doss)
    assert "WARNING" in output, f"Expected 'WARNING' in rendered output, got:\n{output}"
    assert "S-999" in output, f"Expected 'S-999' in warning output:\n{output}"
    assert "not found" in output.lower(), f"Expected 'not found' phrase in output:\n{output}"


def test_ac19_missing_citation_contains_exact_warning_string():
    """render() includes the exact string 'WARNING: citation S-999 not found'."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-999"]],
        root_cause_ids=["S-999"],
        fix_ids=["S-999"],
    )
    output = m.render(report, doss)
    assert "WARNING: citation S-999 not found" in output, (
        f"Exact warning string not in output:\n{output}"
    )


def test_ac19_missing_citation_does_not_raise():
    """render() with an invalid citation ID does not raise an exception."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-999"]],
        root_cause_ids=["S-999"],
        fix_ids=["S-999"],
    )
    result = m.render(report, doss)
    assert result is not None


def test_ac19_missing_citation_in_causal_chain():
    """Missing citation in causal_chain step triggers WARNING."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"], ["S-999"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-001"],
    )
    output = m.render(report, doss)
    assert "WARNING: citation S-999 not found" in output


def test_ac19_missing_citation_in_root_cause():
    """Missing citation in root_cause.evidence_ids triggers WARNING."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-999"],
        fix_ids=["S-001"],
    )
    output = m.render(report, doss)
    assert "WARNING: citation S-999 not found" in output


def test_ac19_missing_citation_in_recommended_fix():
    """Missing citation in recommended_fix.evidence_ids triggers WARNING."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-999"],
    )
    output = m.render(report, doss)
    assert "WARNING: citation S-999 not found" in output


# ---------------------------------------------------------------------------
# AC19: Valid citations → NO warning
# ---------------------------------------------------------------------------

def test_ac19_valid_citations_no_warning():
    """render() with all valid citations produces output with no WARNING block."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001", "CG-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["CG-001"],
    )
    output = m.render(report, doss)
    assert "WARNING" not in output, (
        f"Unexpected WARNING in output for valid citations:\n{output}"
    )


def test_ac19_render_returns_string():
    """render() always returns a string."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-001"],
    )
    output = m.render(report, doss)
    assert isinstance(output, str)


def test_ac19_render_output_is_non_empty():
    """render() output is non-empty."""
    m = _import_render_report()
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-001"],
    )
    output = m.render(report, doss)
    assert len(output) > 0


# ---------------------------------------------------------------------------
# AC20: CLI invocability
# ---------------------------------------------------------------------------

def test_ac20_cli_exits_without_import_error(tmp_path):
    """render_report.py CLI with valid JSON inputs exits without ImportError."""
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-001"],
    )
    dossier_path = tmp_path / "dossier.json"
    report_path = tmp_path / "report.json"
    dossier_path.write_text(json.dumps(doss))
    report_path.write_text(json.dumps(report))

    proc = subprocess.run(
        [
            sys.executable,
            RENDER_REPORT_SCRIPT,
            "--report", str(report_path),
            "--dossier", str(dossier_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "ImportError" not in proc.stderr, (
        f"ImportError in CLI stderr:\n{proc.stderr}"
    )
    assert proc.returncode == 0, (
        f"CLI exited with code {proc.returncode}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def test_ac20_cli_writes_markdown_to_stdout(tmp_path):
    """render_report.py CLI writes non-empty content to stdout."""
    doss = _make_dossier(evidence_ids=["S-001"])
    report = _make_bug_report(
        evidence_ids_per_step=[["S-001"]],
        root_cause_ids=["S-001"],
        fix_ids=["S-001"],
    )
    dossier_path = tmp_path / "dossier.json"
    report_path = tmp_path / "report.json"
    dossier_path.write_text(json.dumps(doss))
    report_path.write_text(json.dumps(report))

    proc = subprocess.run(
        [
            sys.executable,
            RENDER_REPORT_SCRIPT,
            "--report", str(report_path),
            "--dossier", str(dossier_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout.strip(), (
        f"Expected non-empty stdout from CLI, got empty. stderr: {proc.stderr}"
    )
