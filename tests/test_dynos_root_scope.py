"""Regression tests for project-root discrimination in `.dynos` ancestor walks.

The `.dynos` directory name is overloaded: it is both the framework home
(`~/.dynos` / `$DYNOS_HOME`) and each project's control plane
(`<repo>/.dynos`). A bare `.dynos`.is_dir() match in the ancestor walk treated
the framework home — and any stray orphan `.dynos` high in the tree — as a
project root, pulling every unrelated repo under `$HOME` into governance and
producing spurious write denials.

These tests pin the fix: `is_project_dynos_dir` rejects the framework home and
unregistered/taskless orphans, and `decide_write` no longer governs writes to a
folder that is not a real dynos project.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_dynos_root  # noqa: E402
import write_policy  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402


@pytest.fixture()
def dynos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DYNOS_HOME at an isolated tmp home with an empty registry."""
    home = tmp_path / "dynos-home"
    home.mkdir()
    (home / "registry.json").write_text(
        json.dumps({"projects": []}), encoding="utf-8"
    )
    monkeypatch.setenv("DYNOS_HOME", str(home))
    # lru_cache on the home/registry helpers must not leak across tests.
    lib_dynos_root.dynos_home.cache_clear()
    lib_dynos_root._registered_project_roots.cache_clear()
    return home


def _register(home: Path, project_root: Path) -> None:
    reg = {
        "projects": [
            {
                "id": "test-id",
                "paths": [{"path": str(project_root.resolve())}],
                "status": "active",
            }
        ]
    }
    (home / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    lib_dynos_root._registered_project_roots.cache_clear()


def _mk_dynos(root: Path, *, tasks: int = 0) -> Path:
    dynos = root / ".dynos"
    dynos.mkdir(parents=True)
    for i in range(tasks):
        (dynos / f"task-{i:03d}").mkdir()
    return dynos


# --- is_project_dynos_dir ---------------------------------------------------

def test_framework_home_is_not_a_project_root(dynos_home: Path) -> None:
    """`$DYNOS_HOME` itself must never be treated as a project control plane,
    even if it somehow contains task-shaped directories."""
    (dynos_home / "task-000").mkdir()  # would trip the task fallback if reached
    assert lib_dynos_root.is_project_dynos_dir(dynos_home) is False


def test_orphan_dynos_is_rejected(dynos_home: Path, tmp_path: Path) -> None:
    """An unregistered, taskless `.dynos` (e.g. one left at ~/code by a one-off
    session) must not govern its parent."""
    orphan = _mk_dynos(tmp_path / "code")  # unregistered, no tasks
    assert lib_dynos_root.is_project_dynos_dir(orphan) is False


def test_registered_project_is_accepted(dynos_home: Path, tmp_path: Path) -> None:
    project = tmp_path / "code" / "real-project"
    dynos = _mk_dynos(project)  # no tasks yet
    _register(dynos_home, project)
    assert lib_dynos_root.is_project_dynos_dir(dynos) is True


def test_taskful_unregistered_project_is_accepted(
    dynos_home: Path, tmp_path: Path
) -> None:
    """Fallback: a legit project predating registration (or during a registry
    read failure) is recognised by its on-disk task state."""
    dynos = _mk_dynos(tmp_path / "code" / "legacy-project", tasks=1)
    assert lib_dynos_root.is_project_dynos_dir(dynos) is True


# --- _nearest_project_root_from_cwd -----------------------------------------

def test_nearest_root_ignores_home_and_orphan(
    dynos_home: Path, tmp_path: Path
) -> None:
    """A repo whose only `.dynos` ancestors are the framework home and an
    orphan must resolve to no project root."""
    _mk_dynos(tmp_path / "code")  # orphan at the parent level
    repo = tmp_path / "code" / "unrelated-repo"
    repo.mkdir()
    assert write_policy._nearest_project_root_from_cwd(repo) is None


def test_nearest_root_finds_registered_project(
    dynos_home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "code" / "real-project"
    _mk_dynos(project)
    _register(dynos_home, project)
    sub = project / "src"
    sub.mkdir()
    assert write_policy._nearest_project_root_from_cwd(sub) == project.resolve()


# --- decide_write end to end ------------------------------------------------

def test_write_to_non_dynos_folder_is_not_governed(
    dynos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug: an orchestrator/reviewer writing a report into a
    non-dynos repo was denied because the folder was wrongly governed. It must
    now be allowed as out-of-scope."""
    _mk_dynos(tmp_path / "code")  # orphan parent, as in the field report
    repo = tmp_path / "code" / "env-reviewer"
    repo.mkdir()
    monkeypatch.chdir(repo)
    attempt = WriteAttempt(
        role="orchestrator",
        task_dir=None,
        path=repo / "REPORT.md",
        operation="create",
        source="agent",
    )
    decision = decide_write(attempt)
    assert decision.allowed is True
    assert "not governed" in decision.reason


def test_write_inside_registered_project_still_governed(
    dynos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Governance must still bite inside a real project: the orchestrator may
    not write repo files directly (existing write-boundary behaviour)."""
    project = tmp_path / "code" / "real-project"
    _mk_dynos(project)
    _register(dynos_home, project)
    monkeypatch.chdir(project)
    attempt = WriteAttempt(
        role="orchestrator",
        task_dir=None,
        path=project / "src" / "foo.py",
        operation="create",
        source="agent",
    )
    decision = decide_write(attempt)
    assert decision.allowed is False
    assert "not governed" not in decision.reason
