"""Tests for the investigate-skill conformance fixes (D7-3).

The read-only investigator RETURNS its bug-report JSON; `triage.py finalize`
owns validation (structure + citations) and persistence; the dossier-only
guarantee is enforced by the pre-tool-use hook, not just promised in prose.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "debug-module"))

import pre_tool_use  # noqa: E402

TRIAGE = str(ROOT / "debug-module" / "triage.py")


def _dossier(tmp_path: Path) -> Path:
    inv_dir = tmp_path / ".dynos" / "investigations" / "20260611-120000"
    inv_dir.mkdir(parents=True)
    dossier_path = inv_dir / "dossier.json"
    dossier_path.write_text(json.dumps({
        "investigation_id": "INV-test-001",
        "bug_type": "ci-failure",
        "bug_text": "workflow fails on main",
        "repo_path": str(tmp_path),
        "evidence_index": {
            "F-001": {"file": "src/app.py", "line": 10},
            "S-001": {"symbol": "main", "file": "src/app.py"},
        },
    }))
    return dossier_path


_VALID_REPORT = {
    "investigation_id": "INV-test-001",
    "causal_chain": [
        {"step": 1, "description": "config drift", "evidence_ids": ["F-001"]},
    ],
    "root_cause": {"description": "missing env var", "evidence_ids": ["F-001", "S-001"]},
    "recommended_fix": {
        "description": "set the var",
        "locations": [{"file": "src/app.py", "line": 10}],
        "evidence_ids": ["S-001"],
    },
}


def _finalize(dossier: Path, report_json: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", TRIAGE, "finalize", "--report", "-", "--dossier", str(dossier)],
        input=report_json,
        capture_output=True,
        text=True,
        check=False,
    )


def test_finalize_persists_valid_report_and_renders(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    result = _finalize(dossier, json.dumps(_VALID_REPORT))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "report_finalized"

    report_path = Path(payload["report_path"])
    assert json.loads(report_path.read_text())["investigation_id"] == "INV-test-001"
    markdown = Path(payload["markdown_path"]).read_text()
    # Renderer drift fix: bug_type comes through, causal chain renders.
    assert "**Bug type:** ci-failure" in markdown
    assert "No causal chain provided" not in markdown
    assert "config drift" in markdown


def test_finalize_tolerates_markdown_fences(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    fenced = "```json\n" + json.dumps(_VALID_REPORT) + "\n```"
    result = _finalize(dossier, fenced)
    assert result.returncode == 0, result.stderr


def test_finalize_rejects_unknown_citation(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    bad = json.loads(json.dumps(_VALID_REPORT))
    bad["root_cause"]["evidence_ids"] = ["F-999"]
    result = _finalize(dossier, json.dumps(bad))
    assert result.returncode == 1
    assert "F-999" in result.stderr
    # Nothing persisted on rejection.
    assert not (dossier.parent / "bug_report.json").exists()


def test_finalize_rejects_empty_evidence_ids(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    bad = json.loads(json.dumps(_VALID_REPORT))
    bad["causal_chain"][0]["evidence_ids"] = []
    result = _finalize(dossier, json.dumps(bad))
    assert result.returncode == 1
    assert "evidence_ids" in result.stderr


def test_finalize_rejects_non_json(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    result = _finalize(dossier, "I found the bug, trust me")
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Dossier-only enforcement (hook-level)
# ---------------------------------------------------------------------------

def _run_hook(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue()


@pytest.fixture()
def investigator_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _dossier(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')\n")
    (tmp_path / "src" / "secret.py").write_text("KEY = 'x'\n")
    monkeypatch.setenv("DYNOS_ROLE", "investigator")
    monkeypatch.delenv("DYNOS_TASK_DIR", raising=False)
    return tmp_path


def test_investigator_grep_denied(investigator_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, err = _run_hook(monkeypatch, {
        "tool_name": "Grep",
        "tool_input": {"pattern": "password", "path": str(investigator_env)},
        "cwd": str(investigator_env),
    })
    assert code == 2
    assert "dossier-only" in err


def test_investigator_reads_dossier(investigator_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dossier = investigator_env / ".dynos" / "investigations" / "20260611-120000" / "dossier.json"
    code, err = _run_hook(monkeypatch, {
        "tool_name": "Read",
        "tool_input": {"file_path": str(dossier)},
        "cwd": str(investigator_env),
    })
    assert code == 0, err


def test_investigator_reads_dossier_referenced_file(
    investigator_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err = _run_hook(monkeypatch, {
        "tool_name": "Read",
        "tool_input": {"file_path": str(investigator_env / "src" / "app.py")},
        "cwd": str(investigator_env),
    })
    assert code == 0, err


def test_investigator_cannot_read_unreferenced_file(
    investigator_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err = _run_hook(monkeypatch, {
        "tool_name": "Read",
        "tool_input": {"file_path": str(investigator_env / "src" / "secret.py")},
        "cwd": str(investigator_env),
    })
    assert code == 2
    assert "dossier-only" in err


# ---------------------------------------------------------------------------
# Classifier taxonomy (process-shaped bugs)
# ---------------------------------------------------------------------------

def test_classifier_recognizes_ci_and_config_failures() -> None:
    sys.path.insert(0, str(ROOT / "debug-module" / "debuglib"))
    import bug_classifier

    assert bug_classifier.classify("the GitHub Actions workflow fails on main")["bug_type"] == "ci-failure"
    assert bug_classifier.classify("CI job failing after merge")["bug_type"] == "ci-failure"
    assert bug_classifier.classify("missing environment variable DATABASE_URL in prod")["bug_type"] == "config-error"
    # Existing classifications keep working.
    assert bug_classifier.classify("TypeError: Cannot read properties of undefined")["bug_type"] == "runtime-error"
