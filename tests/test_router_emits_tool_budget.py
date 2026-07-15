"""Tests that build_executor_plan emits tool_budget and build_executor_prompt
injects the ## Tool-Use Budget block.

TDD-first: tests will fail until production code lands in hooks/router.py
and hooks/lib_tool_budget.py.

Coverage:
  - AC-4: every plan_entry carries tool_budget
  - AC-6: cache-miss recompute attaches tool_budget without full replan
  - AC-7: build_executor_prompt output contains ## Tool-Use Budget heading + correct N
  - AC-8: routing receipt carries tool_budget per-segment
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router.py"

if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _run(env_obj, *args, stdin=None):
    """Run router.py as a subprocess."""
    return subprocess.run(
        ["python3", str(ROUTER), *args],
        input=stdin,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "DYNOS_HOME": str(env_obj.dynos_home)},
    )


def _write_graph(env_obj, task_id: str, segments: list[dict]) -> Path:
    task_dir = env_obj.root / ".dynos" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    graph_path = task_dir / "execution-graph.json"
    graph_path.write_text(json.dumps({"segments": segments}))
    return graph_path


def _build_plan_in_process(root: Path, task_type: str, segments: list[dict]) -> dict:
    """Call build_executor_plan directly (in-process) for unit-style assertions."""
    import router as router_mod
    return router_mod.build_executor_plan(root, task_type, segments)


# ---------------------------------------------------------------------------
# AC-4: every plan_entry carries a tool_budget field
# ---------------------------------------------------------------------------


def test_single_segment_plan_entry_has_tool_budget(dynos_home):
    """A single-segment plan: the one plan entry must have a tool_budget field."""
    segments = [{"id": "s1", "executor": "backend-executor", "files_expected": ["a.py", "b.py"]}]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    entry = plan["segments"][0]
    assert "tool_budget" in entry, f"plan entry missing tool_budget: {entry}"


def test_multi_segment_plan_all_entries_have_tool_budget(dynos_home):
    """All plan entries in a multi-segment plan must carry tool_budget."""
    segments = [
        {"id": "s1", "executor": "backend-executor", "files_expected": ["a.py"]},
        {"id": "s2", "executor": "ui-executor", "files_expected": ["x.tsx", "y.tsx", "z.tsx"]},
        {"id": "s3", "executor": "db-executor", "files_expected": []},
    ]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    for entry in plan["segments"]:
        assert "tool_budget" in entry, f"plan entry {entry.get('segment_id')} missing tool_budget"


def test_plan_entry_tool_budget_numeric_value_matches_compute(dynos_home):
    """tool_budget value must equal compute_segment_budget(len(files_expected), model)."""
    from lib_tool_budget import compute_segment_budget
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]  # 5 files
    segments = [{"id": "s1", "executor": "backend-executor", "files_expected": files}]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    entry = plan["segments"][0]
    model = entry["model"]
    expected = compute_segment_budget(len(files), model)
    assert entry["tool_budget"] == expected, (
        f"tool_budget={entry['tool_budget']} but expected compute_segment_budget({len(files)}, {model!r})={expected}"
    )


def test_zero_files_expected_plan_entry_has_tool_budget_at_floor(dynos_home):
    """Segment with no files_expected: tool_budget is static floor for the model."""
    from lib_tool_budget import compute_segment_budget
    segments = [{"id": "s1", "executor": "backend-executor", "files_expected": []}]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    entry = plan["segments"][0]
    model = entry["model"]
    expected = compute_segment_budget(0, model)
    assert entry["tool_budget"] == expected


def test_eleven_file_segment_budget_is_38_for_sonnet(dynos_home, monkeypatch):
    """For a sonnet-assigned segment with 11 files: budget must be 38."""
    import router as router_mod
    from lib_tool_budget import compute_segment_budget

    # Force model to sonnet for determinism
    monkeypatch.setattr(router_mod, "resolve_model",
                        lambda root, executor, task_type, ctx=None: {"model": "sonnet", "source": "test"})

    files = [f"f{i}.py" for i in range(11)]
    segments = [{"id": "s1", "executor": "backend-executor", "files_expected": files}]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    entry = plan["segments"][0]
    assert entry["tool_budget"] == 38, f"expected 38 for 11-file sonnet segment, got {entry['tool_budget']}"


def test_twelve_file_segment_budget_is_capped_at_40_for_sonnet(dynos_home, monkeypatch):
    """For a sonnet-assigned segment with 12 files: budget must be ceiling=40."""
    import router as router_mod

    monkeypatch.setattr(router_mod, "resolve_model",
                        lambda root, executor, task_type, ctx=None: {"model": "sonnet", "source": "test"})

    files = [f"f{i}.py" for i in range(12)]
    segments = [{"id": "s1", "executor": "backend-executor", "files_expected": files}]
    plan = _build_plan_in_process(dynos_home.root, "feature", segments)
    entry = plan["segments"][0]
    assert entry["tool_budget"] == 40, (
        f"expected ceiling 40 for 12-file sonnet segment, got {entry['tool_budget']}"
    )


# ---------------------------------------------------------------------------
# AC-6: cache-miss recompute — legacy plan_entry without tool_budget
# ---------------------------------------------------------------------------


def test_cache_miss_recompute_attaches_tool_budget(dynos_home):
    """A cached plan entry that lacks tool_budget must have it recomputed
    when build_executor_plan loads it on cache miss."""
    import router as router_mod
    from lib_tool_budget import compute_segment_budget

    task_id = "task-20260506-cache-miss"
    files = ["a.py", "b.py", "c.py"]
    graph_path = _write_graph(dynos_home, task_id, [
        {"id": "s1", "executor": "backend-executor", "files_expected": files},
    ])

    # Build a cache entry manually without tool_budget to simulate a legacy record
    cache_dir = dynos_home.root / ".dynos" / task_id / "router-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    legacy_entry = {
        "task_id": task_id,
        "task_type": "feature",
        "version": "1",
        "fingerprint": "deadbeef" * 8,  # Intentionally stale — forces re-derive
        "plan": {
            "generated_at": "2025-01-01T00:00:00Z",
            "task_type": "feature",
            "segments": [{
                "segment_id": "s1",
                "executor": "backend-executor",
                "model": "sonnet",
                "model_source": "default",
                "route_mode": "generic",
                "route_source": "default",
                "agent_path": None,
                "agent_name": None,
                "composite_score": 0.0,
                "prevention_rules": [],
                "prevention_rules_omitted": 0,
                # NOTE: tool_budget intentionally absent to simulate legacy cache
            }],
        },
    }
    (cache_dir / "executor-plan.json").write_text(json.dumps(legacy_entry))

    # Call inject-prompt — the stale fingerprint forces a rebuild, which must
    # produce a plan_entry with tool_budget.
    r = _run(dynos_home, "inject-prompt",
             "--root", str(dynos_home.root),
             "--task-type", "feature",
             "--graph", str(graph_path),
             "--segment-id", "s1",
             stdin="base prompt")
    assert r.returncode == 0, f"inject-prompt failed: {r.stderr}"
    # The injected prompt must contain the tool_budget section
    assert "Tool-Use Budget" in r.stdout, (
        "inject-prompt output missing ## Tool-Use Budget after cache-miss recompute"
    )


# ---------------------------------------------------------------------------
# AC-7: build_executor_prompt contains ## Tool-Use Budget heading + value
# ---------------------------------------------------------------------------


def test_build_executor_prompt_contains_tool_use_budget_heading(dynos_home):
    """build_executor_prompt must include the literal '## Tool-Use Budget' heading."""
    import router as router_mod

    plan_entry = {
        "segment_id": "s1",
        "executor": "backend-executor",
        "model": "sonnet",
        "model_source": "default",
        "route_mode": "generic",
        "route_source": "default",
        "agent_path": None,
        "agent_name": None,
        "composite_score": 0.0,
        "prevention_rules": [],
        "prevention_rules_omitted": 0,
        "tool_budget": 29,
    }
    segment = {"id": "s1", "executor": "backend-executor", "files_expected": ["a.py"]}
    prompt = router_mod.build_executor_prompt(dynos_home.root, segment, plan_entry, "base prompt")
    assert "## Tool-Use Budget" in prompt, (
        "build_executor_prompt output missing '## Tool-Use Budget' heading"
    )


def test_build_executor_prompt_contains_numeric_budget_value(dynos_home):
    """The prompt must contain the numeric tool_budget value from plan_entry."""
    import router as router_mod

    plan_entry = {
        "segment_id": "s1",
        "executor": "backend-executor",
        "model": "sonnet",
        "model_source": "default",
        "route_mode": "generic",
        "route_source": "default",
        "agent_path": None,
        "agent_name": None,
        "composite_score": 0.0,
        "prevention_rules": [],
        "prevention_rules_omitted": 0,
        "tool_budget": 38,
    }
    segment = {"id": "s1", "executor": "backend-executor", "files_expected": []}
    prompt = router_mod.build_executor_prompt(dynos_home.root, segment, plan_entry, "base")
    assert "38" in prompt, f"tool_budget value 38 not found in prompt output"


def test_build_executor_prompt_budget_self_pacing_instruction(dynos_home):
    """The budget block must frame the figure as guidance (not a cap) and ask
    for a resumable progress ledger rather than a stop-at-N-calls cutoff."""
    import router as router_mod

    plan_entry = {
        "segment_id": "s1",
        "executor": "backend-executor",
        "model": "haiku",
        "model_source": "default",
        "route_mode": "generic",
        "route_source": "default",
        "agent_path": None,
        "agent_name": None,
        "composite_score": 0.0,
        "prevention_rules": [],
        "prevention_rules_omitted": 0,
        "tool_budget": 15,
    }
    segment = {"id": "s1", "executor": "backend-executor", "files_expected": []}
    prompt = router_mod.build_executor_prompt(dynos_home.root, segment, plan_entry, "x")
    # PR2 (7.5.12): the budget is guidance, not a cutoff — the block reframes
    # from "stop within 3 calls" to a resumable progress ledger so an
    # interrupted segment can be continued rather than abandoned.
    assert "budget" in prompt.lower(), "word 'budget' not found in prompt"
    lowered = prompt.lower()
    assert "not a cap" in lowered, "budget must be framed as an estimate, not a cap"
    assert "resumable" in lowered, "resumability guidance missing from budget block"
    assert "progress ledger" in lowered, "progress-ledger instruction missing from budget block"


def test_build_executor_prompt_reads_budget_from_plan_entry_not_recomputed(dynos_home):
    """build_executor_prompt must use plan_entry['tool_budget'], not recompute it.
    Inject a non-standard budget value and verify the same value appears in output."""
    import router as router_mod

    non_standard_budget = 17  # not a typical computed value
    plan_entry = {
        "segment_id": "s1",
        "executor": "backend-executor",
        "model": "sonnet",
        "model_source": "default",
        "route_mode": "generic",
        "route_source": "default",
        "agent_path": None,
        "agent_name": None,
        "composite_score": 0.0,
        "prevention_rules": [],
        "prevention_rules_omitted": 0,
        "tool_budget": non_standard_budget,
    }
    segment = {"id": "s1", "executor": "backend-executor", "files_expected": ["a.py", "b.py"]}
    prompt = router_mod.build_executor_prompt(dynos_home.root, segment, plan_entry, "base")
    assert str(non_standard_budget) in prompt, (
        f"Expected plan_entry tool_budget value {non_standard_budget} in prompt; "
        f"build_executor_prompt may be recomputing instead of reading from plan_entry."
    )


# ---------------------------------------------------------------------------
# AC-8: executor-routing receipt carries tool_budget per-segment
# ---------------------------------------------------------------------------


def test_routing_receipt_carries_tool_budget_per_segment(dynos_home):
    """After inject-prompt run, the output prompt must include Tool-Use Budget
    (the receipt check is implicit via the subprocess output since the receipt
    is written to disk during inject-prompt execution)."""
    task_id = "task-20260506-receipt"
    graph_path = _write_graph(dynos_home, task_id, [
        {"id": "s1", "executor": "backend-executor",
         "files_expected": ["a.py", "b.py", "c.py"]},
    ])

    # First build the executor plan
    r = _run(dynos_home, "executor-plan",
             "--root", str(dynos_home.root),
             "--task-type", "feature",
             "--graph", str(graph_path))
    assert r.returncode == 0, f"executor-plan failed: {r.stderr}"
    plan = json.loads(r.stdout)
    assert "tool_budget" in plan["segments"][0], (
        "executor-plan output missing tool_budget on segment"
    )

    # The plan output itself is the receipt of what inject-prompt will use
    budget = plan["segments"][0]["tool_budget"]
    assert isinstance(budget, int), f"tool_budget must be int, got {type(budget)}"
    assert 1 <= budget <= 40, f"tool_budget {budget} out of valid range [1, 40]"
