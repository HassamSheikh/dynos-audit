"""Tests for the quote-aware Bash write-destination extractor (D5).

The old regex scan matched `>` characters inside quoted prompt text and
heredoc bodies, producing false-positive write destinations that the policy
then denied (P0-b in docs/permissions-on-design.md). These tests pin the
token-based behavior: quoted strings and heredoc bodies are data, real
redirects and write verbs are still caught.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import pre_tool_use  # noqa: E402
from pre_tool_use import _extract_bash_destinations  # noqa: E402


# ---------------------------------------------------------------------------
# False positives the old regex produced: must extract NOTHING.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        # `>` inside a quoted prompt string (inject-prompt pipe pattern).
        'echo "Data flow: request -> handler > response" | python3 router.py inject-prompt',
        # Markdown blockquote inside a quoted argument.
        'git commit -m "fix: handle a > b comparisons"',
        # python -c source containing a redirect-looking string.
        "python3 -c \"print('exit > 0 means failure')\"",
        # Heredoc body containing redirects and rm-looking text.
        "python3 hooks/ctl.py write-classification .dynos/task-x --from - <<'JSON'\n"
        '{"type": "feature", "notes": "rm old > new comparisons"}\n'
        "JSON",
        # Quoted token that merely *names* a write verb.
        'echo "run rm -rf later"',
        # fd duplication is not a path write.
        "command 2>&1",
        # mv with a single operand has no distinct destination.
        "mv onlyarg",
    ],
)
def test_no_false_positive_destinations(command: str) -> None:
    assert _extract_bash_destinations(command) == []


# ---------------------------------------------------------------------------
# Sinks: extracted-then-filtered (never reach the policy).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "python3 registry.py register . 2>/dev/null || true",
        "noisy-tool > /dev/null",
        "tool >> /dev/null 2>&1",
    ],
)
def test_device_sinks_are_not_destinations(command: str) -> None:
    assert _extract_bash_destinations(command) == []


# ---------------------------------------------------------------------------
# Real writes: still detected.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hi > out.txt", ["out.txt"]),
        ("echo hi >> log.md", ["log.md"]),
        ("echo x>compact.txt", ["compact.txt"]),
        ("cat a | tee -a notes.md", ["notes.md"]),
        ("mv src.py dst.py", ["dst.py"]),
        ("cp -r srcdir destdir", ["destdir"]),
        ("rm -rf .dynos/task-1/active-segment-role", [".dynos/task-1/active-segment-role"]),
        ("unlink some/file.json", ["some/file.json"]),
        ("truncate -s 0 data.log", ["data.log"]),
        # Multiple statements: each side of the separator is scanned.
        ("echo a > first.txt; echo b > second.txt", ["first.txt", "second.txt"]),
        ("ls && rm stale.lock", ["stale.lock"]),
        # Redirect after a quoted argument still counts.
        ('echo "a > b" > real-target.txt', ["real-target.txt"]),
    ],
)
def test_real_write_destinations_detected(command: str, expected: list[str]) -> None:
    assert _extract_bash_destinations(command) == expected


def test_unparseable_command_falls_back_to_legacy_regex() -> None:
    # Unbalanced quote: shlex fails; the legacy regex still sees the redirect.
    command = "echo 'unterminated > target.txt"
    assert "target.txt" in _extract_bash_destinations(command)


def test_heredoc_terminator_resumes_scanning() -> None:
    command = (
        "cat <<'EOF'\n"
        "body > not-a-write.txt\n"
        "EOF\n"
        "echo done > after.txt"
    )
    assert _extract_bash_destinations(command) == ["after.txt"]


def test_herestring_is_not_treated_as_heredoc() -> None:
    # `<<<` must not swallow the rest of the command as a heredoc body.
    command = "grep pattern <<<'a > b'\necho hi > real.txt"
    assert _extract_bash_destinations(command) == ["real.txt"]


# ---------------------------------------------------------------------------
# Denial message contract (D5-2): self-identifies as a plugin guardrail.
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch: pytest.MonkeyPatch, payload: dict) -> tuple[int, str]:
    import contextlib

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue()


def test_denial_message_names_guardrail_role_path_and_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    # Non-terminal manifest so pre_tool_use keeps the DYNOS_TASK_DIR binding
    # (a manifest-less/terminal task dir is ignored as stale).
    (task_dir / "manifest.json").write_text(json.dumps({"stage": "EXECUTION"}))
    monkeypatch.setenv("DYNOS_ROLE", "planning")
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "outside.txt"), "content": "x"},
        "cwd": str(tmp_path),
    }
    code, err = _run_hook(monkeypatch, payload)
    assert code == 2
    assert "write-policy:" in err
    assert "dynos-work guardrail" in err
    assert "not a Claude Code permission" in err
    assert "role=planning" in err
    assert "outside.txt" in err
    assert "docs/write-boundary-spec.md" in err


def test_wrapper_required_denial_names_sanctioned_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / ".dynos" / "task-20260611-002"
    task_dir.mkdir(parents=True)
    # Non-terminal manifest so the task_dir binding is honoured; otherwise the
    # write resolves as a cross-task control-plane write, not a wrapper-required
    # one, and the sanctioned-command hint is never surfaced.
    (task_dir / "manifest.json").write_text(json.dumps({"stage": "EXECUTION"}))
    monkeypatch.setenv("DYNOS_ROLE", "planning")
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(task_dir / "classification.json"),
            "content": "{}",
        },
        "cwd": str(tmp_path),
    }
    code, err = _run_hook(monkeypatch, payload)
    assert code == 2
    assert "Sanctioned path:" in err
    assert "write-classification" in err


def test_prompt_string_redirect_no_longer_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P0-b regression: quoted `>` in a piped prompt must pass."""
    task_dir = tmp_path / ".dynos" / "task-20260611-003"
    task_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_ROLE", "planning")
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                'echo "Implement a > b ordering and pipe -> sink" '
                "| python3 router.py inject-prompt --segment seg-1"
            )
        },
        "cwd": str(tmp_path),
    }
    code, err = _run_hook(monkeypatch, payload)
    assert code == 0, f"expected allow, got stderr={err!r}"


def test_dev_null_redirect_allowed_for_planning_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start-skill Step 0 pattern: `2>/dev/null || true` must pass."""
    task_dir = tmp_path / ".dynos" / "task-20260611-004"
    task_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_ROLE", "planning")
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python3 registry.py register . 2>/dev/null || true"},
        "cwd": str(tmp_path),
    }
    code, err = _run_hook(monkeypatch, payload)
    assert code == 0, f"expected allow, got stderr={err!r}"
