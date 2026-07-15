"""Tests for `ctl next-continuation` — the segment-completion continuation loop.

When an executor exits without finishing its segment (the "ran out of turns
mid-edit, never wrote evidence" failure), the segment must not be abandoned.
next-continuation reports the incomplete segments with a resume seed and loops
until the work is actually done; budget is never the stop condition. The only
automatic halt is genuine stall (two consecutive continuations that move
nothing), reported as status "stalled" with exit code 3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from test_ctl import _run_ctl, _setup_task_dir  # noqa: E402


def _seed_execution(task_dir: Path) -> None:
    """Bring a fresh task dir to EXECUTION with routing in place."""
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {"segment_id": "seg-1", "executor": "backend-executor", "model": "sonnet",
         "route_mode": "generic", "agent_path": None},
        {"segment_id": "seg-2", "executor": "testing-executor", "model": "sonnet",
         "route_mode": "generic", "agent_path": None},
    ])


def _complete_segment(task_dir: Path, seg_id: str, executor: str, rel_file: str) -> None:
    """Fully finish a segment: files_expected on disk, evidence, done-receipt."""
    from lib_receipts import receipt_executor_done

    root = task_dir.parent.parent
    target = root / rel_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# produced by execution\n")

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{seg_id}.md"
    evidence_path.write_text(f"{seg_id} evidence\n")

    sidecar_dir = task_dir / "receipts" / "_injected-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = ("f" if seg_id == "seg-1" else "a") * 64
    (sidecar_dir / f"{seg_id}.sha256").write_text(digest)
    receipt_executor_done(
        task_dir,
        segment_id=seg_id,
        executor_type=executor,
        model_used="sonnet",
        injected_prompt_sha256=digest,
        agent_name=None,
        evidence_path=str(evidence_path),
        tokens_used=5,
        diff_verified_files=[],
        no_op_justified=False,
    )


def test_incomplete_segment_reported_with_resume_seed(tmp_path: Path) -> None:
    """seg-1 finished, seg-2 never executed → seg-2 is reported as a
    continuation with role/model/files/criteria and its incomplete reason."""
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")

    result = _run_ctl("next-continuation", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)

    assert payload["status"] == "continuation_needed"
    assert payload["complete_segments"] == ["seg-1"]
    segs = payload["incomplete_segments"]
    assert [s["segment_id"] for s in segs] == ["seg-2"]
    seg2 = segs[0]
    assert seg2["role"] == "testing-executor"
    assert seg2["model"] == "sonnet"
    assert seg2["files_expected"] == ["tests/test_a.py"]
    assert seg2["criteria_ids"] == [2]
    assert seg2["depends_on"] == ["seg-1"]
    assert seg2["evidence_present"] is False
    assert seg2["incomplete_reasons"]  # non-empty
    assert seg2["stall_count"] == 0
    assert seg2["stalled"] is False


def test_completing_the_segment_reports_complete(tmp_path: Path) -> None:
    """Once the continuation finishes seg-2, the loop terminates (complete)."""
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")

    first = json.loads(_run_ctl("next-continuation", str(task_dir)).stdout)
    assert first["status"] == "continuation_needed"

    _complete_segment(task_dir, "seg-2", "testing-executor", "tests/test_a.py")

    result = _run_ctl("next-continuation", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    done = json.loads(result.stdout)
    assert done["status"] == "complete"
    assert done["incomplete_segments"] == []
    assert set(done["complete_segments"]) == {"seg-1", "seg-2"}


def test_no_progress_across_calls_stalls_with_exit_3(tmp_path: Path) -> None:
    """Repeated continuations that move nothing eventually report 'stalled'
    (exit 3) instead of spinning forever — the only automatic halt."""
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")

    # seg-2 stays untouched across calls: nothing changes between them.
    r1 = _run_ctl("next-continuation", str(task_dir))
    assert r1.returncode == 0
    assert json.loads(r1.stdout)["incomplete_segments"][0]["stall_count"] == 0

    r2 = _run_ctl("next-continuation", str(task_dir))
    assert r2.returncode == 0
    assert json.loads(r2.stdout)["incomplete_segments"][0]["stall_count"] == 1

    r3 = _run_ctl("next-continuation", str(task_dir))
    assert r3.returncode == 3, r3.stdout + r3.stderr
    payload = json.loads(r3.stdout)
    assert payload["status"] == "stalled"
    assert payload["stalled_segments"] == ["seg-2"]


def test_progress_resets_the_stall_counter(tmp_path: Path) -> None:
    """A continuation that grows evidence (partial progress) resets the stall
    counter, so a segment making headway is never wrongly halted."""
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")

    _run_ctl("next-continuation", str(task_dir))              # stall 0
    r2 = _run_ctl("next-continuation", str(task_dir))         # stall 1
    assert json.loads(r2.stdout)["incomplete_segments"][0]["stall_count"] == 1

    # Partial progress: the continuation created a files_expected file but died
    # before writing evidence, so the segment is still incomplete (pending) —
    # yet real work happened and the stall counter must reset.
    root = task_dir.parent.parent
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_a.py").write_text("# partial work by continuation\n")

    r3 = _run_ctl("next-continuation", str(task_dir))
    assert r3.returncode == 0
    seg2 = json.loads(r3.stdout)["incomplete_segments"][0]
    assert seg2["segment_id"] == "seg-2"
    assert seg2["stall_count"] == 0
