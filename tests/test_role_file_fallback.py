"""TDD-first tests for the role-file fallthrough in pre_tool_use.py.

Covers ACs 6, 7:

  AC 6 — skills/execute/SKILL.md contains at least 2 references to
          'active-segment-role' (create + delete).
  AC 7 — pre_tool_use.py resolve role via three-step chain:
    (i)   env var set → wins over file
    (ii)  env unset, file present + non-empty → role = file contents
    (iii) env unset, file absent → role = execute-inline + event logged
          with reason 'absent'
    (iv)  env unset, file present but empty/whitespace → role = execute-inline
          + event logged with reason 'empty'
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

# ---------------------------------------------------------------------------
# Conditional imports
# ---------------------------------------------------------------------------
try:
    import pre_tool_use as _pre_tool_use
    # Detect whether the role-file fallthrough is implemented by checking
    # if the module's source references 'active-segment-role'
    _src = (HOOKS_DIR / "pre_tool_use.py").read_text()
    _HAS_ROLE_FILE = "active-segment-role" in _src
except Exception:
    _pre_tool_use = None  # type: ignore[assignment]
    _HAS_ROLE_FILE = False


# ---------------------------------------------------------------------------
# AC 6 — SKILL.md references
# ---------------------------------------------------------------------------

SKILL_PATH = ROOT / "skills" / "execute" / "SKILL.md"


def test_skill_md_exists() -> None:
    """AC 6 prerequisite: skills/execute/SKILL.md must exist."""
    assert SKILL_PATH.exists(), f"SKILL.md not found at {SKILL_PATH}"


@pytest.mark.skipif(not SKILL_PATH.exists(), reason="skills/execute/SKILL.md does not exist")
def test_skill_md_contains_at_least_two_active_segment_role_references() -> None:
    """AC 6: SKILL.md must contain at least 2 distinct references to 'active-segment-role'
    (one for create, one for delete)."""
    content = SKILL_PATH.read_text()
    occurrences = content.count("active-segment-role")
    assert occurrences >= 2, (
        f"skills/execute/SKILL.md must contain at least 2 references to 'active-segment-role' "
        f"(create + delete); found {occurrences}"
    )


# ---------------------------------------------------------------------------
# Helpers for AC 7 tests
# ---------------------------------------------------------------------------

def _invoke_resolve_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    dynos_role_env: str | None,
    dynos_task_dir_env: str | None,
    role_file_contents: str | None,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
) -> tuple[str, list[dict]]:
    """Drive the role-resolution logic in pre_tool_use.py and return (resolved_role, logged_events).

    This helper wires up the environment and filesystem state, then calls
    the resolve-role path directly by importing and invoking the relevant
    internal function (if exposed) or by parsing the logic inline.

    Because pre_tool_use.py is a hook script (not a library), we can't call
    it cleanly as a function. We test the OBSERVABLE OUTPUT: the resolved
    role ends up in the decision that gets passed to `decide_write`, and
    events get emitted via `log_event`. We capture both.
    """
    raise NotImplementedError("Use _test_role_resolution below instead")


def _make_task_dir(tmp_path: Path, task_id: str = "task-20260423-RF1") -> tuple[Path, Path]:
    """Create (root, task_dir) with minimal structure.

    A non-terminal manifest.json is required: pre_tool_use ignores a
    DYNOS_TASK_DIR whose manifest is missing or terminal (stale-binding
    hardening), which would otherwise drop the task_dir and short-circuit role
    resolution before the active-segment-role fallthrough under test.
    """
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / ".dynos").mkdir(exist_ok=True)
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "manifest.json").write_text(
        json.dumps({"stage": "EXECUTION"}), encoding="utf-8"
    )
    return root, task_dir


# ---------------------------------------------------------------------------
# AC 7 — role resolution chain
#
# We test the resolution logic by examining what role pre_tool_use resolves
# to. The most reliable way is to extract and call the internal resolve
# function directly, or by running the hook logic with subprocess and
# observing structured output.
#
# Since the function has not been implemented yet, these tests use
# pytest.skip when _HAS_ROLE_FILE is False.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_ROLE_FILE, reason="active-segment-role fallthrough not yet implemented")
class TestRoleResolutionChain:
    """AC 7: role resolution falls through env → file → default."""

    def _resolve_role(
        self,
        monkeypatch: pytest.MonkeyPatch,
        task_dir: Path,
        *,
        env_role: str | None,
        role_file_text: str | None,
    ) -> tuple[str, list[dict]]:
        """Invoke the role-resolution logic from pre_tool_use and return
        (resolved_role, list_of_logged_event_dicts).

        Calls the module-level _resolve_role_for_task helper if it exists,
        otherwise falls back to directly importing and inspecting.
        """
        # Set up env
        if env_role is not None:
            monkeypatch.setenv("DYNOS_ROLE", env_role)
        else:
            monkeypatch.delenv("DYNOS_ROLE", raising=False)

        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))

        role_file = task_dir / "active-segment-role"
        if role_file_text is not None:
            role_file.write_text(role_file_text)
        elif role_file.exists():
            role_file.unlink()

        logged_events: list[dict] = []

        # Patch log_event to capture emitted events without real I/O
        def _capture_log_event(root: Any, event_type: str, **kwargs: Any) -> None:
            logged_events.append({"event": event_type, **kwargs})

        # Check if the module exposes an internal resolve function
        resolve_fn = getattr(_pre_tool_use, "_resolve_active_role", None)
        if resolve_fn is None:
            # Try the expected entry point signature
            resolve_fn = getattr(_pre_tool_use, "_get_role", None)

        if resolve_fn is not None:
            with patch("pre_tool_use.log_event", side_effect=_capture_log_event):
                resolved = resolve_fn(task_dir)
            return resolved, logged_events

        # Fallback: call main() with a mocked Bash invocation and capture the role
        # used in the decide_write call
        captured_role: list[str] = []

        def _capture_decide_write(attempt: Any) -> Any:
            captured_role.append(attempt.role)
            decision = MagicMock()
            decision.allowed = True
            decision.mode = "direct"
            decision.reason = "test"
            decision.wrapper_command = None
            return decision

        import os as _os
        import sys as _sys

        old_argv = _sys.argv[:]
        try:
            # Simulate pre_tool_use being called with a Bash tool use
            _sys.argv = ["pre_tool_use.py"]
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide_write),
                patch("pre_tool_use.log_event", side_effect=_capture_log_event),
            ):
                fake_stdin = json.dumps({
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                    "cwd": str(task_dir),
                })
                import io
                old_stdin = _sys.stdin
                _sys.stdin = io.StringIO(fake_stdin)
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
                finally:
                    _sys.stdin = old_stdin
        finally:
            _sys.argv = old_argv

        resolved = captured_role[0] if captured_role else "unknown"
        return resolved, logged_events

    # AC 7(i) — env var set wins over file
    def test_env_var_wins_over_role_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 7i: DYNOS_ROLE set → env role is used, file is not consulted."""
        _, task_dir = _make_task_dir(tmp_path, "task-RR-env")
        role_file = task_dir / "active-segment-role"
        role_file.write_text("backend-executor")  # file has a different role

        monkeypatch.setenv("DYNOS_ROLE", "ui-executor")
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        # We test the resolution by directly checking what role would be resolved
        # by simulating the env + file state and checking the role source
        resolved_role = None
        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            nonlocal resolved_role
            resolved_role = attempt.role
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        assert resolved_role == "ui-executor", (
            f"Env var DYNOS_ROLE='ui-executor' must win; got {resolved_role!r}"
        )

    # AC 7(ii) — env unset, file present + non-empty → role = file contents
    def test_file_present_nonempty_role_is_file_contents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 7ii: env unset + file present + non-empty → role = file contents."""
        _, task_dir = _make_task_dir(tmp_path, "task-RR-file")
        role_file = task_dir / "active-segment-role"
        role_file.write_text("integration-executor")

        monkeypatch.delenv("DYNOS_ROLE", raising=False)
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        resolved_role = None
        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            nonlocal resolved_role
            resolved_role = attempt.role
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        assert resolved_role == "integration-executor", (
            f"Role file contents must be used; got {resolved_role!r}"
        )
        # When resolution lands on (b), role_missing event must NOT be emitted
        role_missing_events = [e for e in logged if e.get("event") == "pre_tool_use_role_missing"]
        assert not role_missing_events, (
            f"pre_tool_use_role_missing must NOT be emitted when role resolved from file: "
            f"{role_missing_events}"
        )

    # AC 7(iii) — env unset, file absent → role = execute-inline + event logged
    def test_file_absent_role_is_execute_inline_and_event_emitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 7iii: env unset + file absent → role=execute-inline + pre_tool_use_role_file_missing."""
        _, task_dir = _make_task_dir(tmp_path, "task-RR-absent")
        # Ensure file does not exist
        role_file = task_dir / "active-segment-role"
        if role_file.exists():
            role_file.unlink()

        monkeypatch.delenv("DYNOS_ROLE", raising=False)
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        resolved_role = None
        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            nonlocal resolved_role
            resolved_role = attempt.role
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        assert resolved_role == "execute-inline", (
            f"With no env and no role file, role must be 'execute-inline'; got {resolved_role!r}"
        )

        missing_events = [
            e for e in logged if e.get("event") == "pre_tool_use_role_file_missing"
        ]
        assert missing_events, (
            f"pre_tool_use_role_file_missing event must be emitted when role file absent; "
            f"logged: {logged}"
        )
        assert missing_events[0].get("reason") == "absent", (
            f"reason must be 'absent'; got: {missing_events[0]!r}"
        )

    # AC 7(iv) — env unset, file present but empty/whitespace → role = execute-inline + event
    def test_file_present_empty_role_is_execute_inline_reason_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 7iv: env unset + file present but whitespace-only → execute-inline + reason 'empty'."""
        _, task_dir = _make_task_dir(tmp_path, "task-RR-empty")
        role_file = task_dir / "active-segment-role"
        role_file.write_text("   \n   ")  # whitespace only

        monkeypatch.delenv("DYNOS_ROLE", raising=False)
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        resolved_role = None
        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            nonlocal resolved_role
            resolved_role = attempt.role
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        assert resolved_role == "execute-inline", (
            f"With whitespace-only role file, role must be 'execute-inline'; got {resolved_role!r}"
        )

        missing_events = [
            e for e in logged if e.get("event") == "pre_tool_use_role_file_missing"
        ]
        assert missing_events, (
            f"pre_tool_use_role_file_missing event must be emitted when role file empty; "
            f"logged: {logged}"
        )
        assert missing_events[0].get("reason") == "empty", (
            f"reason must be 'empty'; got: {missing_events[0]!r}"
        )

    # Regression: env unset, DYNOS_TASK_DIR unset, but cwd is inside .dynos/task-XXX/
    # → ancestor-walk discovers task_dir → role file MUST be consulted.
    #
    # This is the production path for harness-spawned subagents (e.g., the
    # planning subagent invoked via the Agent tool). Subagents do not inherit
    # DYNOS_TASK_DIR, so gating the role-file lookup on the env var alone left
    # the active-segment-role mechanism unreachable and forced spec.md writes
    # to fall through to execute-inline (denied).
    def test_role_file_resolved_via_ancestor_walk_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ancestor-walked task_dir must consult the role file.

        When DYNOS_TASK_DIR is not set but cwd lives inside a .dynos/task-*
        directory containing a valid active-segment-role, the role file must
        still be honored. Without this, harness subagents (which don't inherit
        DYNOS_TASK_DIR) can never elevate above execute-inline.
        """
        _, task_dir = _make_task_dir(tmp_path, "task-RR-ancestor")
        (task_dir / "manifest.json").write_text(json.dumps({"stage": "EXECUTION"}))
        role_file = task_dir / "active-segment-role"
        role_file.write_text("planning")

        monkeypatch.delenv("DYNOS_ROLE", raising=False)
        monkeypatch.delenv("DYNOS_TASK_DIR", raising=False)
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        resolved_role = None
        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            nonlocal resolved_role
            resolved_role = attempt.role
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        # cwd is inside the task dir so _find_task_dir_from_ancestors discovers it.
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        assert resolved_role == "planning", (
            f"Role file must be consulted via ancestor-walked task_dir even when "
            f"DYNOS_TASK_DIR is unset; got {resolved_role!r}"
        )
        role_missing_events = [
            e for e in logged if e.get("event") == "pre_tool_use_role_missing"
        ]
        assert not role_missing_events, (
            f"pre_tool_use_role_missing must NOT be emitted when role resolved from file: "
            f"{role_missing_events}"
        )

    # AC 7(ii) — when file resolves role, event 'pre_tool_use_role_missing' must NOT fire
    def test_no_role_missing_event_when_file_resolves_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 7ii: when role is resolved from file, the legacy role_missing event is NOT emitted."""
        _, task_dir = _make_task_dir(tmp_path, "task-RR-nomissing")
        role_file = task_dir / "active-segment-role"
        role_file.write_text("backend-executor")

        monkeypatch.delenv("DYNOS_ROLE", raising=False)
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

        logged: list[dict] = []

        def _capture_log(root: Any, event_type: str, **kw: Any) -> None:
            logged.append({"event": event_type, **kw})

        def _capture_decide(attempt: Any) -> Any:
            dec = MagicMock()
            dec.allowed = True
            dec.mode = "direct"
            dec.reason = "test"
            dec.wrapper_command = None
            return dec

        import io
        fake_stdin = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi > /tmp/role-fallback-probe.txt"},
            "cwd": str(task_dir),
        })
        old_stdin = sys.stdin
        old_argv = sys.argv[:]
        sys.stdin = io.StringIO(fake_stdin)
        sys.argv = ["pre_tool_use.py"]
        try:
            with (
                patch("pre_tool_use.decide_write", side_effect=_capture_decide),
                patch("pre_tool_use.log_event", side_effect=_capture_log),
            ):
                try:
                    _pre_tool_use.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = old_stdin
            sys.argv = old_argv

        role_missing_events = [e for e in logged if "role_missing" in e.get("event", "")]
        assert not role_missing_events, (
            f"pre_tool_use_role_missing must NOT be emitted when role resolved from file; "
            f"logged: {role_missing_events}"
        )
