#!/usr/bin/env python3
"""PreToolUse hook for dynos-work write-policy enforcement.

Reads a JSON payload from stdin, extracts tool_name, tool_input, and cwd,
resolves the DYNOS_ROLE and DYNOS_TASK_DIR from the environment, then
calls decide_write() to enforce the write boundary.

Exit codes:
    0 -- write allowed
    1 -- internal error (never a silent allow)
    2 -- write denied (deny-and-continue semantics)

Stderr on denial is prefixed with "write-policy: ".
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

# Shared discriminator that rejects the framework home (~/.dynos) and stray
# orphan .dynos dirs when resolving a project root from an ancestor walk. The
# `.dynos` name is overloaded (framework home vs. per-project control plane);
# a bare `.dynos`.is_dir() match would treat $HOME — or any orphan .dynos high
# in the tree — as a project root and govern unrelated repos. If the shared
# module cannot be imported, fall back to excluding just the framework home,
# which is the critical half of the guard.
try:
    from lib_dynos_root import is_project_dynos_dir as _is_project_dynos_dir
except Exception:  # pragma: no cover - defensive import fallback
    def _is_project_dynos_dir(dynos_dir: Path) -> bool:
        try:
            if not dynos_dir.is_dir():
                return False
            home = Path(
                os.environ.get("DYNOS_HOME") or (Path.home() / ".dynos")
            ).expanduser().resolve()
            return dynos_dir.resolve() != home
        except Exception:
            return False

# Allowlist of valid executor role names that may appear in active-segment-role.
# Privileged internal roles (ctl, receipt-writer, eventbus, scheduler, system)
# are intentionally excluded to prevent role-file injection attacks.
_EXECUTOR_ROLE_ALLOWLIST: frozenset[str] = frozenset({
    "backend-executor", "ui-executor", "testing-executor", "integration-executor",
    "ml-executor", "db-executor", "refactor-executor", "docs-executor",
    "infra-executor", "security-executor", "data-executor",
    "observability-executor", "release-executor",
    "planning", "execute-inline", "repair-coordinator", "investigator",
    "audit-spec-completion", "audit-security", "audit-code-quality",
    "audit-performance", "audit-dead-code", "audit-db-schema", "audit-ui", "audit-claude-md",
    "audit-architecture", "audit-threat-model", "audit-api-contract",
    "audit-test-strategy", "audit-accessibility", "audit-privacy",
    "audit-supply-chain", "audit-infrastructure", "audit-observability",
    "audit-release", "audit-data-integrity", "audit-docs-accuracy",
})

# Mirrors lib_core.TERMINAL_STAGES without importing lib_core from this hook.
_TERMINAL_STAGES: frozenset[str] = frozenset({
    "DONE", "FAILED", "CANCELLED", "CALIBRATED",
})

def _subagent_isolation(task_dir: "Path | None") -> bool:
    """True only when the host harness gives each subagent a DISTINCT
    session_id, making per-subagent write-role enforcement meaningful.

    Default False: Claude Code shares the orchestrator's session_id across
    all subagents with no distinguishing field (GitHub issue #7881), so a
    planner/executor/auditor subagent resolves as the orchestrator. With
    isolation False the orchestrator is permitted to ADOPT the stamped
    active-segment-role so it can act as that segment's role; otherwise no
    subagent could ever author its role-scoped artifacts.

    Set ``"subagent_isolation": true`` in
    ``<project>/.dynos/config/policy.json`` ONLY on a harness that truly
    isolates subagent sessions — that restores the strict D3 behavior where
    the orchestrator never adopts a stamped role.
    """
    if task_dir is None:
        return False
    try:
        cfg = task_dir.parent.parent / ".dynos" / "config" / "policy.json"
        if not cfg.is_file():
            return False
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("subagent_isolation", False))
    except Exception:
        return False


def _adoptable_stamped_role(task_dir: "Path | None") -> "str | None":
    """Return the stamped active-segment-role when it is a valid,
    non-orchestrator executor/planning/audit role; else None. Lets the
    orchestrator session adopt the current segment role under a
    non-isolating harness (see ``_subagent_isolation``)."""
    if task_dir is None:
        return None
    try:
        rf = task_dir / "active-segment-role"
        if not rf.is_file():
            return None
        val = rf.read_text(encoding="utf-8").strip()
        if val and val != "orchestrator" and val in _EXECUTOR_ROLE_ALLOWLIST:
            return val
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Module-level imports for write_policy, read_policy, and lib_log.
# These are resolved lazily at first-use inside main() so the hook can still
# run when the hooks directory is not yet on sys.path (the path is added at
# the start of main()). The module-level names are set to None initially and
# replaced with real objects after the import succeeds. Tests can patch these
# names to intercept calls.
# ---------------------------------------------------------------------------
decide_write = None  # type: ignore[assignment]
_emit_policy_event = None  # type: ignore[assignment]
WriteAttempt = None  # type: ignore[assignment]
decide_read = None  # type: ignore[assignment]
ReadAttempt_RP = None  # type: ignore[assignment]
_emit_read_policy_event = None  # type: ignore[assignment]
log_event = None  # type: ignore[assignment]


def _task_is_non_terminal(task_dir: Path) -> bool:
    try:
        manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
        return isinstance(manifest, dict) and manifest.get("stage") not in _TERMINAL_STAGES
    except Exception:
        return False


def _active_task_from_pointer(dynos: Path) -> Path | None:
    try:
        pointer = json.loads((dynos / "active-task.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    task_id = pointer.get("task_id") if isinstance(pointer, dict) else None
    if not task_id:
        return None
    raw_task_dir = pointer.get("task_dir")
    task_dir = Path(raw_task_dir) if isinstance(raw_task_dir, str) and raw_task_dir else dynos / str(task_id)
    try:
        if not task_dir.exists():
            return None
        if _task_is_non_terminal(task_dir):
            return task_dir
    except Exception:
        return None
    return None


def _active_tasks_from_manifests(dynos: Path) -> list[Path]:
    tasks: list[Path] = []
    try:
        candidates = sorted(dynos.glob("task-*"), reverse=True)
        for candidate in candidates:
            if _task_is_non_terminal(candidate):
                tasks.append(candidate)
    except Exception:
        pass
    return tasks


def _task_dir_from_ancestor_path(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        if ancestor.parent.name == ".dynos" and ancestor.name.startswith("task-"):
            if _task_is_non_terminal(ancestor):
                return ancestor
            return None
    return None


def _project_root_from_ancestors(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        if _is_project_dynos_dir(ancestor / ".dynos"):
            return ancestor
    return None


def _task_dir_from_path(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except Exception:
        return None
    for ancestor in [current, *current.parents]:
        if ancestor.parent.name == ".dynos" and ancestor.name.startswith("task-"):
            if _task_is_non_terminal(ancestor):
                return ancestor
            return None
    return None


def _find_task_dir_from_tool_target(tool_name: str, tool_input: object, cwd: Path) -> Path | None:
    """Infer task context from the actual write target when cwd is unreliable."""
    raw_paths: list[str] = []
    if isinstance(tool_input, dict):
        if tool_name in ("Write", "Edit"):
            raw = tool_input.get("file_path") or tool_input.get("path")
            if isinstance(raw, str):
                raw_paths.append(raw)
        elif tool_name == "MultiEdit":
            edits = tool_input.get("edits", [])
            if isinstance(edits, list):
                for edit in edits:
                    if isinstance(edit, dict) and isinstance(edit.get("file_path"), str):
                        raw_paths.append(edit["file_path"])
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            if isinstance(command, str):
                raw_paths.extend(_extract_bash_destinations(command))

    for raw in raw_paths:
        try:
            target = _resolve_path(raw, cwd)
        except Exception:
            continue
        task_dir = _task_dir_from_path(target)
        if task_dir is not None:
            return task_dir
    return None


def _find_task_dir_from_ancestors(cwd: Path) -> Path | None:
    """Walk upward from cwd, returning only an unambiguous active task."""
    direct_task = _task_dir_from_ancestor_path(cwd)
    if direct_task is not None:
        return direct_task
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        dynos = ancestor / ".dynos"
        if _is_project_dynos_dir(dynos):
            active_tasks = _active_tasks_from_manifests(dynos)
            pointer_path = dynos / "active-task.json"
            if pointer_path.exists():
                try:
                    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
                else:
                    if isinstance(pointer, dict) and not pointer.get("task_id"):
                        return None
                    task_dir = _active_task_from_pointer(dynos)
                    if task_dir is not None and len(active_tasks) <= 1:
                        return task_dir
                    return None
            if len(active_tasks) == 1:
                return active_tasks[0]
            return None
    return None


# ---------------------------------------------------------------------------
# Bash pre-filter: quote-aware write-destination extraction
# ---------------------------------------------------------------------------
# The original implementation ran regexes over the raw command string, which
# matched `>` characters inside quoted prompt text and heredoc bodies and
# produced false-positive "write destinations" (P0-b in
# docs/permissions-on-design.md). Extraction is now token-based:
#   1. heredoc bodies are stripped (their content is data, not shell),
#   2. the command is tokenized with shlex (quoted strings stay single
#      tokens and can never be operators),
#   3. redirection operators and write-verb argv positions are resolved on
#      the token stream.
# This filter is defense-in-depth, not the trust anchor: interpreter-internal
# writes (e.g. `python3 -c "open(p,'w')"`) are invisible to any Bash-level
# parser. The unforgeable guarantees live in receipts + spawn-log + the
# ctl-internal require_write_allowed path (see docs/write-boundary-spec.md).

# Legacy regex patterns, retained ONLY as the fallback when shlex cannot
# tokenize the command (unbalanced quotes). Fallback keeps the filter
# fail-closed rather than silently allowing unparseable commands.
_LEGACY_BASH_WRITE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r">>?\s*([^\s;|&>]+)"), 1),
    (re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)"), 1),
    (re.compile(r"\bmv\s+\S+\s+([^\s;|&]+)"), 1),
    (re.compile(r"\bcp\s+(?:-[a-zA-Z]+\s+)?(?:\S+\s+)+([^\s;|&]+)"), 1),
    (re.compile(r"\brsync\s+(?:\S+\s+)+([^\s;|&]+)"), 1),
    (re.compile(r"\binstall\s+(?:-[a-zA-Z]+\s+(?:\S+\s+)?)?(?:\S+\s+)+([^\s;|&]+)"), 1),
    (re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)*([^\s;|&]+)"), 1),
    (re.compile(r"\bunlink\s+([^\s;|&]+)"), 1),
]

# Heredoc opener: `<< WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`. The negative
# lookbehind keeps herestrings (`<<<`) from being misparsed as heredocs.
_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<-?\s*(['\"]?)(\w+)\1")

# Punctuation-run tokens that redirect output to a file. `>&` (fd dup) is
# handled separately — its operand is a file descriptor, not a path.
_REDIRECT_TOKENS = frozenset({">", ">>", "&>", "&>>", ">|"})

# Device sinks: writing to these persists nothing. The plugin's own skills
# prescribe `2>/dev/null`; denying a sink only produces noise.
_SINK_DESTINATIONS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty",
})

_COMMAND_SEPARATOR_TOKENS = frozenset({";", "|", "||", "&&", "&", "(", ")"})

# argv-based write verbs -> which following non-flag args are destinations.
#   "last" -> final non-flag argument (mv/cp/rsync/install)
#   "all"  -> every non-flag argument (tee/rm/unlink/shred/truncate)
_WRITE_VERB_DEST: dict[str, str] = {
    "tee": "all",
    "mv": "last",
    "cp": "last",
    "rsync": "last",
    "install": "last",
    "rm": "all",
    "unlink": "all",
    "shred": "all",
    "truncate": "all",
}


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies so their content is not tokenized as shell."""
    out: list[str] = []
    terminator: str | None = None
    for line in command.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC_OPEN_RE.search(line)
        out.append(line)
        if match:
            terminator = match.group(2)
    return "\n".join(out)


def _tokenize_bash(command: str) -> list[str] | None:
    """shlex-tokenize a command; None when the command cannot be parsed."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _extract_bash_destinations_legacy(command: str) -> list[str]:
    """Regex fallback used only when shlex tokenization fails."""
    destinations: list[str] = []
    seen: set[str] = set()
    for pattern, group in _LEGACY_BASH_WRITE_PATTERNS:
        for match in pattern.finditer(command):
            dest = match.group(group).strip().rstrip(";|&>")
            if dest and dest not in seen:
                seen.add(dest)
                destinations.append(dest)
    return destinations


def _is_candidate_dest(token: str) -> bool:
    """Filter tokens that cannot be filesystem write destinations."""
    if not token:
        return False
    if token in _SINK_DESTINATIONS:
        return False
    if token.isdigit():  # fd numbers from `>& 2` style constructs
        return False
    if token.startswith("-"):
        return False
    if token in _COMMAND_SEPARATOR_TOKENS or token in _REDIRECT_TOKENS:
        return False
    if token in {"<", "<<", "<<<", ">&", "<&"}:
        return False
    return True


def _extract_bash_destinations(command: str) -> list[str]:
    """Return write destination paths detected in a Bash command string.

    Token-based: quoted strings are single tokens (never operators), heredoc
    bodies are stripped before tokenization. Falls back to the legacy regex
    scan when the command cannot be tokenized.
    """
    stripped = _strip_heredocs(command)
    tokens = _tokenize_bash(stripped)
    if tokens is None:
        return _extract_bash_destinations_legacy(command)

    destinations: list[str] = []
    seen: set[str] = set()

    def _add(dest: str) -> None:
        if _is_candidate_dest(dest) and dest not in seen:
            seen.add(dest)
            destinations.append(dest)

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # Redirection operator: next token is the destination. Punctuation
        # runs may glue a trailing newline onto the operator; normalize.
        # Strip backticks so tokens like `` `cp `` resolve correctly as verbs.
        op = tok.strip().strip("`")
        if op in _REDIRECT_TOKENS:
            if i + 1 < n:
                _add(tokens[i + 1].strip("`"))
                i += 2
                continue
        # Write verb: collect its argv tail up to the next separator. The
        # verb is recognized at any statement position (parity with the old
        # \b-anchored regexes) — quoted text can no longer false-positive
        # because it arrives as a single token, not as words.
        verb = os.path.basename(op)
        if verb in _WRITE_VERB_DEST:
            mode = _WRITE_VERB_DEST[verb]
            args: list[str] = []
            j = i + 1
            while j < n and tokens[j] not in _COMMAND_SEPARATOR_TOKENS:
                t = tokens[j].strip("`")
                if t in _REDIRECT_TOKENS:
                    break
                if t != "--" and not t.startswith("-"):
                    args.append(t)
                j += 1
            if mode == "all":
                for a in args:
                    _add(a)
            elif args:
                # "last" verbs need >= 2 operands to have a distinct
                # destination (`mv src dst`); a single operand is a source.
                if verb in {"mv", "cp", "rsync", "install"}:
                    if len(args) >= 2:
                        _add(args[-1])
                else:
                    _add(args[-1])
            i = j
            continue
        i += 1
    return destinations


def _resolve_path(raw: str, cwd: Path) -> Path:
    """Resolve a path string relative to cwd."""
    p = Path(raw)
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def _format_denial(role: str, path: Path | str, decision) -> str:
    """Self-explaining denial text (P0-f in docs/permissions-on-design.md).

    Denials from this hook are routinely mistaken for Claude Code permission
    failures, sending users into settings changes that cannot help. Every
    denial therefore names (a) what it is, (b) the role and path it judged,
    and (c) the sanctioned alternative when one exists.
    """
    sanctioned = ""
    wrapper = getattr(decision, "wrapper_command", None)
    if wrapper:
        sanctioned = f" Sanctioned path: {wrapper}."
    return (
        f"write-policy: DENIED — {decision.reason} "
        f"[dynos-work guardrail, not a Claude Code permission; changing "
        f"permission settings will not affect this] "
        f"role={role} path={path}.{sanctioned} "
        f"Temp files belong in .dynos/task-*/_scratch/. "
        f"See docs/write-boundary-spec.md."
    )


def main() -> int:
    """Main entry point. Returns exit code."""
    global decide_write, _emit_policy_event, WriteAttempt, decide_read, ReadAttempt_RP, _emit_read_policy_event, log_event

    # Add hooks directory to sys.path so we can import write_policy, read_policy, and lib_log
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    # Parse stdin JSON -- any failure here is an internal error (exit 1)
    try:
        raw_stdin = sys.stdin.read()
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError as exc:
        print(f"pre-tool-use: malformed stdin (not valid JSON): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"pre-tool-use: failed to read stdin: {exc}", file=sys.stderr)
        return 1

    # Extract required fields -- missing fields are an internal error (exit 1)
    try:
        tool_name = payload["tool_name"]
        tool_input = payload["tool_input"]
        cwd_str = payload.get("cwd", os.getcwd())
        cwd = Path(cwd_str).resolve()
    except KeyError as exc:
        print(f"pre-tool-use: missing required field in payload: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"pre-tool-use: failed to parse payload fields: {exc}", file=sys.stderr)
        return 1

    # Import write_policy -- failure is an internal error (exit 1)
    # decide_write may be pre-populated by tests via module-level patching;
    # WriteAttempt and _emit_policy_event are always resolved from write_policy
    # because they are not independently patchable by the test suite.
    try:
        import write_policy as _wp
        if decide_write is None:
            decide_write = _wp.decide_write
        if _emit_policy_event is None:
            _emit_policy_event = _wp._emit_policy_event
        if WriteAttempt is None:
            WriteAttempt = _wp.WriteAttempt
    except ImportError as exc:
        # Only fatal if decide_write itself is not yet available
        if decide_write is None:
            print(f"pre-tool-use: cannot import write_policy: {exc}", file=sys.stderr)
            return 1
        # If decide_write was pre-populated (test context), proceed without
        # write_policy — WriteAttempt and _emit_policy_event may be None

    # Import read_policy -- failure is non-fatal if decide_read was pre-populated
    try:
        import read_policy as _rp
        if decide_read is None:
            decide_read = _rp.decide_read
        if ReadAttempt_RP is None:
            ReadAttempt_RP = _rp.ReadAttempt
        if _emit_read_policy_event is None:
            _emit_read_policy_event = _rp._emit_read_policy_event
    except ImportError as exc:
        # Only fatal if decide_read itself is not yet available
        if decide_read is None:
            print(f"pre-tool-use: cannot import read_policy: {exc}", file=sys.stderr)
            return 1

    # Import lib_log -- failure is non-fatal (event logging is optional)
    if log_event is None:
        try:
            import lib_log as _ll
            log_event = _ll.log_event
        except ImportError:
            pass

    # Actor identity inputs (D3): the harness-provided session_id and the
    # SessionStart-pinned orchestrator identity. Neither is agent-writable.
    session_id = str(payload.get("session_id") or "").strip()

    # Resolve role from environment
    role_from_env = os.environ.get("DYNOS_ROLE", "").strip()

    # Resolve task_dir from explicit task context first, then from the
    # hook-owned session binding, and only then from unambiguous ancestors.
    # The old project-global active-task pointer is deliberately not trusted
    # when multiple active tasks exist.
    task_dir_from_env = os.environ.get("DYNOS_TASK_DIR", "").strip()
    project_root = _project_root_from_ancestors(cwd)
    if task_dir_from_env:
        env_task_dir = Path(task_dir_from_env).resolve()
        if _task_is_non_terminal(env_task_dir):
            task_dir: Path | None = env_task_dir
            project_root = task_dir.parent.parent
        else:
            task_dir = None
    else:
        task_dir = None
        target_task_dir = _find_task_dir_from_tool_target(tool_name, tool_input, cwd)
        if target_task_dir is not None:
            task_dir = target_task_dir
            project_root = task_dir.parent.parent
        if task_dir is None and project_root is not None and session_id:
            try:
                import actor_identity as _ai_task
                bound_task = _ai_task.lookup_session_task(project_root, session_id)
                if bound_task is not None:
                    task_dir = bound_task
            except Exception:
                pass
        if task_dir is None:
            task_dir = _find_task_dir_from_ancestors(cwd)
            if task_dir is not None:
                project_root = task_dir.parent.parent

    if task_dir is not None and session_id:
        try:
            import actor_identity as _ai_bind
            _ai_bind.bind_session_task(task_dir.parent.parent, session_id, task_dir)
        except Exception:
            pass

    actor_identity = None
    pin = None
    latest_pin = None
    if task_dir is not None:
        try:
            import actor_identity as _ai
            actor_identity = _ai
            pin = _ai.read_pin(task_dir.parent.parent, session_id=session_id)
            latest_pin = _ai.read_pin(task_dir.parent.parent)
        except Exception:
            actor_identity = None
            pin = None
            latest_pin = None

    # Role resolution chain:
    # (a) DYNOS_ROLE env var — highest priority (daemon/scheduler/tests)
    # (b) orchestrator pin — session_id matches the SessionStart pin: the
    #     MAIN session always resolves to 'orchestrator' and never reads
    #     role files (stamping a subagent role can no longer mutate the
    #     orchestrator's own rights mid-flow)
    # (c) session binding / grant consumption — any OTHER session is a
    #     subagent: reuse its existing binding, else consume the oldest
    #     pending grant from the ctl-written ledger
    # (d) legacy role file — sessions without a pin (pre-D3 sessions) and
    #     subagents on tasks that still stamp active-segment-role
    # (e) default "execute-inline"
    role_file_searched: bool = False
    role_file_path: Path | None = None
    role_file_reason: str | None = None  # "absent" | "empty" | "invalid"

    if role_from_env:
        # Step (a): env var wins
        role = role_from_env
        role_missing = False
    else:
        role: str | None = None
        role_missing = True

        # Steps (b)/(c): only meaningful when both a task dir and a pin
        # exist — without a pin we cannot distinguish actors and fall back
        # to the legacy chain wholesale (today's behavior, no regression).
        if task_dir is not None and actor_identity is not None and latest_pin and session_id:
            if pin is not None and session_id == pin.get("session_id"):
                # Orchestrator session. Strict D3 (subagent_isolation=true):
                # never adopt a stamped role — prevents self-escalation.
                # Under a non-isolating harness like Claude Code (default),
                # subagents share this session_id with no distinguisher
                # (issue #7881), so the orchestrator MUST adopt the stamped
                # active-segment-role to act as the planner/executor/auditor
                # for the segment, else no subagent can author its
                # role-scoped artifacts. Forgery defenses are unaffected:
                # control-plane.json and the pin are denied to ALL roles,
                # and audit-report writes remain cross-checked against
                # spawn-log.jsonl at receipt time.
                adopted = (
                    None if _subagent_isolation(task_dir)
                    else _adoptable_stamped_role(task_dir)
                )
                if adopted is not None:
                    role = adopted
                    role_missing = False
                    try:
                        if log_event is not None:
                            log_event(
                                task_dir.parent.parent,
                                "orchestrator_role_adopted",
                                task=task_dir.name,
                                role=adopted,
                            )
                    except Exception:
                        pass
                else:
                    role = "orchestrator"
                    role_missing = False
            else:
                bound = actor_identity.lookup_binding(task_dir, session_id)
                if bound is None:
                    bound = actor_identity.consume_grant(task_dir, session_id)
                    if bound is not None:
                        try:
                            if log_event is not None:
                                log_event(
                                    task_dir.parent.parent,
                                    "role_grant_consumed",
                                    task=task_dir.name,
                                    role=bound,
                                    session=session_id[:16],
                                )
                        except Exception:
                            pass
                if bound is not None and bound in _EXECUTOR_ROLE_ALLOWLIST:
                    role = bound
                    role_missing = False

        # Step (d): legacy role file fallback.
        if role is None:
            role = "execute-inline"
            if task_dir is not None:
                role_file_path = task_dir / "active-segment-role"
                role_file_searched = True
                try:
                    if role_file_path.is_file():
                        file_contents = role_file_path.read_text(encoding="utf-8").strip()
                        if file_contents and file_contents in _EXECUTOR_ROLE_ALLOWLIST:
                            role = file_contents
                            role_missing = False
                        elif file_contents and file_contents not in _EXECUTOR_ROLE_ALLOWLIST:
                            # Not an allowed executor role — reject and fall to
                            # (e) to prevent privilege escalation via role file.
                            role_file_reason = "invalid"
                        else:
                            role_file_reason = "empty"
                    else:
                        role_file_reason = "absent"
                except Exception:
                    role_file_reason = "absent"

    # Degraded-mode diagnostic (D3): if the ORCHESTRATOR role is denied while
    # grants are pending, the denial may actually be a subagent whose tool
    # calls arrived under the pinned session_id (harness did not distinguish
    # actors). Fail closed, but say so — this is the one failure mode of
    # session-keyed actor resolution and it must be diagnosable from the
    # error text alone.
    degraded_hint = ""
    if role == "orchestrator" and actor_identity is not None and task_dir is not None:
        try:
            if actor_identity.pending_grants(task_dir):
                degraded_hint = (
                    " NOTE: unconsumed role grants are pending for this task. "
                    "If this denial occurred inside a SUBAGENT, the harness "
                    "did not present a distinct session_id for it (degraded "
                    "actor resolution) — see docs/permissions-on-design.md §D3."
                )
        except Exception:
            pass

    # Emit pre_tool_use_role_missing event if role was not set in the environment
    # AND role was not resolved from the role file (step b)
    if role_missing:
        if tool_name == "Bash":
            raw_cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            missing_event_path = str(raw_cmd)[:120]
        else:
            if isinstance(tool_input, dict):
                missing_event_path = tool_input.get("file_path", tool_input.get("path", ""))
            else:
                missing_event_path = ""
        try:
            if log_event is not None and task_dir is not None:
                root = task_dir.parent.parent
                log_event(
                    root,
                    "pre_tool_use_role_missing",
                    task=task_dir.name,
                    tool_name=tool_name,
                    resolved_path=str(missing_event_path),
                    fallback_role=role,
                )
        except Exception:
            # Event logging failure is NOT a fatal internal error -- proceed
            pass

    # Emit pre_tool_use_role_file_missing when resolution landed on (c) and
    # a role file was searched at (b) but was absent or empty
    if role_missing and role_file_searched and role_file_reason is not None and task_dir is not None:
        try:
            if log_event is not None:
                log_event(
                    task_dir.parent.parent,
                    "pre_tool_use_role_file_missing",
                    task_dir=str(task_dir),
                    path=str(role_file_path),
                    reason=role_file_reason,
                )
        except Exception:
            # Event logging failure is NOT a fatal internal error -- proceed
            pass

    # -----------------------------------------------------------------
    # Handle Bash tool: apply regex pre-filter on command
    # -----------------------------------------------------------------
    if tool_name == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not isinstance(command, str):
            print("pre-tool-use: Bash tool_input.command is not a string", file=sys.stderr)
            return 1

        # Role authorization probe: verify the resolved role is recognized before
        # any side effects occur. Check directly against _EXECUTOR_ROLE_ALLOWLIST
        # rather than calling decide_write(path=cwd) — passing the repo root
        # through decide_write always produces "path escapes task boundary" for
        # non-executor roles (planning, audit-*, repair-coordinator), blocking
        # every Bash command including required ctl wrapper calls. The allowlist
        # is the authoritative set of valid agent roles; anything else is a
        # mis-configured or injected role and should be denied unconditionally.
        if task_dir is not None:
            if (
                role != "orchestrator"
                and role not in _EXECUTOR_ROLE_ALLOWLIST
                and not role.endswith("-executor")
            ):
                print(
                    f"write-policy: role '{role}' is not in the authorized role "
                    f"allowlist [dynos-work guardrail, not a Claude Code "
                    f"permission]. Stamp a valid role via "
                    f"`ctl.py stamp-role <task_dir> --role <role>`; valid roles: "
                    f"{', '.join(sorted(_EXECUTOR_ROLE_ALLOWLIST))}",
                    file=sys.stderr,
                )
                return 2

        destinations = _extract_bash_destinations(command)

        # Substring-scan defense for control-plane filenames against deletion
        # primitives that bypass _extract_bash_destinations. Only scans when
        # the command contains a deletion verb (rm/rmdir/unlink/shred/find-
        # -delete/truncate) — avoids false positives on `git commit -m
        # "...audit-grep-quota.json..."` and similar non-destructive uses.
        # Parser-independent within the deletion class; closes V1-V5 from
        # sec-1-residual.
        _DELETION_VERB = re.compile(
            r"\b(rm|rmdir|unlink|shred|truncate)\b|\bfind\b[^|;&]*-delete\b"
        )
        if task_dir is not None and _DELETION_VERB.search(command):
            from write_policy import _CONTROL_PLANE_EXACT
            for cp_name in _CONTROL_PLANE_EXACT:
                if cp_name in command:
                    cp_path = (task_dir / cp_name).resolve()
                    if str(cp_path) not in destinations and cp_name not in destinations:
                        destinations.append(str(cp_path))

        if not destinations:
            # No write pattern matched -- pass without policy check
            return 0

        # Check each destination through decide_write
        for raw_dest in destinations:
            dest_path = _resolve_path(raw_dest, cwd)
            attempt = WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=dest_path,
                operation="modify",
                source="agent",
            )
            decision = decide_write(attempt)
            _emit_policy_event(attempt, decision)

            # Emit structured bash_check event
            try:
                if log_event is not None and task_dir is not None:
                    root = task_dir.parent.parent
                    log_event(
                        root,
                        "pre_tool_use_bash_check",
                        task=task_dir.name,
                        command=command[:120],
                        destination=str(dest_path),
                        role=role,
                        allowed=decision.allowed,
                        reason=decision.reason,
                    )
            except Exception:
                pass

            if not decision.allowed:
                print(_format_denial(role, dest_path, decision) + degraded_hint, file=sys.stderr)
                return 2

        return 0

    # -----------------------------------------------------------------
    # Handle Write, Edit, MultiEdit tools: extract the target file path
    # -----------------------------------------------------------------
    if tool_name in ("Write", "Edit", "MultiEdit"):
        if tool_name == "MultiEdit":
            edits = tool_input.get("edits", []) if isinstance(tool_input, dict) else []
            if not isinstance(edits, list):
                print("pre-tool-use: MultiEdit edits is not a list", file=sys.stderr)
                return 1
            paths_to_check: list[Path] = []
            for edit in edits:
                fp = edit.get("file_path", "") if isinstance(edit, dict) else ""
                if fp:
                    paths_to_check.append(_resolve_path(fp, cwd))
            if not paths_to_check:
                print("pre-tool-use: MultiEdit has no edits with file_path", file=sys.stderr)
                return 1
        else:
            if not isinstance(tool_input, dict):
                print("pre-tool-use: tool_input is not a dict", file=sys.stderr)
                return 1
            raw_path = tool_input.get("file_path", tool_input.get("path", ""))
            if not raw_path:
                print("pre-tool-use: no file_path found in tool_input", file=sys.stderr)
                return 1
            paths_to_check = [_resolve_path(raw_path, cwd)]

        for target_path in paths_to_check:
            attempt = WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=target_path,
                operation="modify",
                source="agent",
            )
            decision = decide_write(attempt)
            _emit_policy_event(attempt, decision)

            if not decision.allowed:
                print(_format_denial(role, target_path, decision) + degraded_hint, file=sys.stderr)
                return 2

        return 0

    # -----------------------------------------------------------------
    # Handle Read, Grep, Glob tools: enforce read policy for audit roles
    # and the investigator's dossier-only guarantee
    # -----------------------------------------------------------------
    if tool_name in ("Read", "Grep", "Glob"):
        # Investigator (D7-3): "the LLM never gathers evidence on its own"
        # is enforced here, not just promised in prose. Reads are allowed
        # only inside .dynos/investigations/ (the dossier and its siblings)
        # or for files the dossier itself references; Grep/Glob are denied
        # outright — searching IS evidence-gathering.
        if role == "investigator":
            if tool_name in ("Grep", "Glob"):
                print(
                    "read-policy: investigator is dossier-only — Grep/Glob "
                    "are evidence-gathering and are denied [dynos-work "
                    "guardrail]. Cite pre-minted evidence IDs from the "
                    "dossier instead.",
                    file=sys.stderr,
                )
                return 2
            raw_path = ""
            if isinstance(tool_input, dict):
                raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
            target = _resolve_path(str(raw_path), cwd) if raw_path else cwd
            # Locate the project .dynos root: task dir wins, else cwd walk.
            dynos_root: Path | None = None
            if task_dir is not None:
                dynos_root = task_dir.parent
            else:
                for ancestor in [cwd, *cwd.parents]:
                    if _is_project_dynos_dir(ancestor / ".dynos"):
                        dynos_root = ancestor / ".dynos"
                        break
            if dynos_root is not None:
                investigations = (dynos_root / "investigations").resolve()
                try:
                    target.relative_to(investigations)
                    return 0  # dossier and investigation artifacts: allowed
                except ValueError:
                    pass
                # Files referenced by the most recent dossier are citable —
                # allow confirming a citation snippet.
                try:
                    dossiers = sorted(investigations.glob("*/dossier.json"))
                    if dossiers:
                        dossier_text = dossiers[-1].read_text(encoding="utf-8", errors="ignore")
                        project_root = dynos_root.parent.resolve()
                        try:
                            rel = target.resolve().relative_to(project_root).as_posix()
                        except ValueError:
                            rel = None
                        if rel and rel in dossier_text:
                            return 0
                except OSError:
                    pass
            print(
                f"read-policy: investigator is dossier-only — {target} is "
                "neither under .dynos/investigations/ nor referenced by the "
                "dossier [dynos-work guardrail].",
                file=sys.stderr,
            )
            return 2

        # AC 16: non-audit roles bypass read-policy enforcement entirely.
        if not role.startswith("audit-"):
            return 0

        # No task_dir: cannot enforce stage/diff scope; passthrough.
        if task_dir is None:
            return 0

        # AC 15: stage guard. Read manifest.json; on any read/parse exception
        # (including FileNotFoundError when the file is missing), fail-closed.
        # Permitted stages source: spec.md AC 15 (PLAN_AUDIT, CHECKPOINT_AUDIT,
        # FINAL_AUDIT, REPAIR_PLANNING, REPAIR_EXECUTION).
        permitted_stages = {
            "PLAN_AUDIT", "CHECKPOINT_AUDIT", "FINAL_AUDIT",
            "REPAIR_PLANNING", "REPAIR_EXECUTION",
        }
        try:
            manifest_data = json.loads((task_dir / "manifest.json").read_text())
            stage = manifest_data.get("stage", "")
        except Exception:
            print(
                "read-policy: manifest unreadable; denying audit-* tool call",
                file=sys.stderr,
            )
            return 2
        if stage not in permitted_stages:
            print(
                f"read-policy: audit role not permitted at stage {stage}",
                file=sys.stderr,
            )
            return 2

        # AC 13/14: load audit-plan.json with distinct missing/invalid events.
        audit_plan = None
        audit_plan_path = task_dir / "audit-plan.json"
        if not audit_plan_path.exists():
            # Missing: emit pre_tool_use_audit_plan_missing
            try:
                if log_event is not None:
                    log_event(
                        task_dir.parent.parent,
                        "pre_tool_use_audit_plan_missing",
                        task=task_dir.name,
                        tool_name=tool_name,
                    )
            except Exception:
                pass
        else:
            # Exists: parse and validate. Any failure → audit_plan=None +
            # pre_tool_use_audit_plan_invalid.
            invalid = False
            try:
                parsed = json.loads(audit_plan_path.read_text())
                if isinstance(parsed, dict) and isinstance(parsed.get("diff_files"), list):
                    audit_plan = parsed
                else:
                    invalid = True
            except Exception:
                invalid = True
            if invalid:
                audit_plan = None
                try:
                    if log_event is not None:
                        log_event(
                            task_dir.parent.parent,
                            "pre_tool_use_audit_plan_invalid",
                            task=task_dir.name,
                            tool_name=tool_name,
                        )
                except Exception:
                    pass

        # AC 3: extract target per tool. tool_input must be a dict.
        if not isinstance(tool_input, dict):
            print(f"pre-tool-use: {tool_name} tool_input is not a dict", file=sys.stderr)
            return 1

        raw_pattern: str | None = None
        if tool_name == "Read":
            raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
            if not raw_path:
                print("pre-tool-use: Read has no file_path in tool_input", file=sys.stderr)
                return 1
            target = _resolve_path(raw_path, cwd)
        elif tool_name == "Grep":
            # CRITICAL: Grep target is `path` (search root), NOT `pattern` (regex).
            raw_path = tool_input.get("path", "")
            if not raw_path:
                # No explicit search root: fall back to cwd. This still allows
                # decide_read to apply the diff/task_dir allowlist on cwd.
                raw_path = str(cwd)
            target = _resolve_path(raw_path, cwd)
        else:  # Glob
            pattern = tool_input.get("pattern", "")
            if not pattern:
                print("pre-tool-use: Glob has no pattern in tool_input", file=sys.stderr)
                return 1
            raw_pattern = pattern
            # Best-effort: resolve pattern as a path so prefix checks see
            # an absolute string. The original pattern is preserved in raw_pattern.
            target = _resolve_path(pattern, cwd)

        attempt = ReadAttempt_RP(
            role=role,
            task_dir=task_dir,
            target=target,
            tool_name=tool_name,
            raw_pattern=raw_pattern,
        )
        decision = decide_read(attempt, audit_plan=audit_plan)
        try:
            _emit_read_policy_event(attempt, decision)
        except Exception:
            pass

        if not decision.allowed:
            print(f"read-policy: {decision.reason}", file=sys.stderr)
            return 2
        return 0

    # Unknown tool name -- pass without policy check
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"pre-tool-use: internal error: {exc}", file=sys.stderr)
        sys.exit(1)
