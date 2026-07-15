"""Central write-boundary policy for task and control-plane artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


WriteOperation = Literal["create", "modify", "delete"]
WriteSource = Literal[
    "agent",
    "inline",
    "ctl",
    "scheduler",
    "receipt-writer",
    "eventbus",
    "system",
]
WriteMode = Literal["direct", "wrapper", "deny"]

# Maps each privileged role to the set of modules whose call chain may claim it.
# ctl is included in receipt-writer because cmd_amend_artifact writes amendment
# receipts directly without routing through lib_receipts.write_receipt.
# lib_core is included in ctl because write_ctl_json is the canonical ctl-write
# wrapper used by lib_validate and other helpers that lack a direct ctl import.
# lib_log and lib_tokens are included in system because role='system' is the
# global-fallback write role used by their top-level event/token write paths.
_PRIVILEGED_ROLE_MODULE_MAP: dict[str, frozenset[str]] = {
    "eventbus": frozenset({"lib_log"}),
    "receipt-writer": frozenset({"lib_receipts", "ctl", "router"}),
    "ctl": frozenset({"ctl", "lib_core"}),
    "scheduler": frozenset({"scheduler"}),
    "system": frozenset({"lib_log", "lib_tokens"}),
}

# PRO-001: per-role capability-key sentinels replace sys._getframe stack-walking.
# Each privileged role gets a distinct object() identity that callers must
# import and pass explicitly. Forge-resistance equals Python object identity:
# without importing this module's private symbol, no caller can construct a
# matching token. The capability key proves caller identity at runtime.
#
# _PRIVILEGED_ROLE_MODULE_MAP is NOT consulted at runtime by this module —
# it is a static allowlist enforced at lint/test time by
# tests/test_write_policy_module_allowlist.py, which scans imports of the
# capability-key helper across the codebase and fails CI if a module outside
# the allowlist imports a privileged role's key. Runtime enforcement is the
# capability-key identity check in require_write_allowed; the module map is
# the static counterpart that catches drift before code lands.
_CAPABILITY_KEYS: dict[str, object] = {
    role: object() for role in _PRIVILEGED_ROLE_MODULE_MAP
}


def _get_capability_key(role: str) -> object:
    """Return the capability-key sentinel for a privileged role.

    Raises KeyError if role is not in _CAPABILITY_KEYS. Privileged callers
    must import this and pass the returned sentinel as the capability_key
    kwarg of require_write_allowed.
    """
    return _CAPABILITY_KEYS[role]


@dataclass(frozen=True)
class WriteAttempt:
    role: str
    task_dir: Path | None
    path: Path
    operation: WriteOperation
    source: WriteSource


@dataclass(frozen=True)
class WriteDecision:
    allowed: bool
    reason: str
    mode: WriteMode
    wrapper_command: str | None = None


_CONTROL_PLANE_EXACT = frozenset({
    "manifest.json",
    "external-solution-gate.json",
    "token-usage.json",
    "events.jsonl",
    "spawn-log.jsonl",
    "audit-grep-quota.json",
    "role-grants.json",
    "role-bindings.json",
    "tool-call-counters.json",
})

_WRAPPER_REQUIRED = {
    "execution-graph.json": "python3 hooks/ctl.py write-execution-graph <task_dir> --from <json>",
    "repair-log.json": "python3 hooks/ctl.py write-repair-log <task_dir> --from <json>",
    "classification.json": "python3 hooks/ctl.py write-classification <task_dir> --from <json>",
    # active-segment-role gates which role write_policy resolves on the next
    # pre_tool_use call. If an agent could write it directly, it could elevate
    # itself to audit-* and unlock audit-reports/ writes, which is exactly the
    # primitive the 2026-04-30 audit-chain forgery incident exploited. The
    # stamp-role wrapper enforces a role allowlist; the real forgery defense
    # for audit-* claims is the spawn-log cross-check at receipt time.
    "active-segment-role": "python3 hooks/ctl.py stamp-role <task_dir> --role <role>",
    # role-grants.json is the per-actor grant ledger (D3). Same elevation
    # primitive as active-segment-role: only the ctl wrapper may append
    # grants, and the wrapper enforces the role allowlist.
    "role-grants.json": "python3 hooks/ctl.py grant-role <task_dir> --role <role>",
}


def _task_relative(path: Path, task_dir: Path | None) -> Path | None:
    if task_dir is None:
        return None
    try:
        return path.resolve().relative_to(task_dir.resolve())
    except Exception:
        return None


def _owning_task_dir(path: Path) -> Path | None:
    """Walk path.resolve().parents; return the first parent p where
    p.parent.name == ".dynos" and p.name.startswith("task-").
    Pure path operations — no I/O.
    """
    resolved = path.resolve()
    for p in resolved.parents:
        if p.parent.name == ".dynos" and p.name.startswith("task-"):
            return p
    return None


def _is_cross_task_control_plane(rel_posix: str) -> bool:
    """Return True iff rel_posix names a control-plane artifact within a task dir.

    Covers: _CONTROL_PLANE_EXACT names, _WRAPPER_REQUIRED keys,
    audit-summary.json / audit-plan.json, and the path prefixes
    receipts/, audit-reports/, evidence/verification/, and
    handoff-*.json patterns.

    Case-insensitive: on case-insensitive filesystems (macOS/Windows),
    Path.resolve().name preserves the on-disk casing the caller typed
    (e.g. MANIFEST.JSON), but the OS still writes the real control-plane
    file. We case-fold the input once and match against the
    (already-lowercase) sets and prefixes so a mixed-case spelling cannot
    bypass the cross-task guard. All _CONTROL_PLANE_EXACT and
    _WRAPPER_REQUIRED keys are lowercase, so the lowercased compare is exact.
    """
    r = rel_posix.lower()
    if r in _CONTROL_PLANE_EXACT:
        return True
    if r in _WRAPPER_REQUIRED:
        return True
    if r in {"audit-summary.json", "audit-plan.json"}:
        return True
    if r.startswith("receipts/"):
        return True
    if r.startswith("audit-reports/"):
        return True
    if r.startswith("evidence/verification/"):
        return True
    if r.startswith("handoff-") and r.endswith(".json"):
        return True
    return False


def _matches_role(role: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return role.startswith(pattern[:-1])
    return role == pattern


def is_control_plane_path(path: Path, task_dir: Path | None) -> bool:
    rel = _task_relative(path, task_dir)
    if rel is None:
        return False
    rel_posix = rel.as_posix()
    if rel_posix in _CONTROL_PLANE_EXACT:
        return True
    if rel_posix in _WRAPPER_REQUIRED:
        return True
    if rel_posix.startswith("receipts/"):
        return True
    if rel_posix.startswith("handoff-") and rel_posix.endswith(".json"):
        return True
    return False


def allowed_globs_for_role(role: str, task_dir: Path | None) -> list[str]:
    if role == "orchestrator":
        return [
            ".dynos/task-*/_scratch/**",
            ".dynos/task-*/execution-log.md",
            ".dynos/task-*/escalation.md",
            ".dynos/task-*/audit-context.md",
            ".dynos/task-*/raw-input.md",
            ".dynos/task-*/discovery-notes.md",
            ".dynos/task-*/design-decisions.md",
            "repo files only during inline fast-track EXECUTION",
        ]
    if _matches_role(role, "planning"):
        return [
            ".dynos/task-*/discovery-notes.md",
            ".dynos/task-*/design-decisions.md",
            ".dynos/task-*/design-doc.md",
            ".dynos/task-*/spec.md",
            ".dynos/task-*/plan.md",
            # NOTE: classification.json and execution-graph.json require the ctl
            # wrapper — planning cannot write them directly. Use:
            #   python3 hooks/ctl.py write-classification <task_dir> --from <json>
            #   python3 hooks/ctl.py write-execution-graph <task_dir> --from <json>
            ".dynos/task-*/classification.json (via ctl wrapper only)",
            ".dynos/task-*/execution-graph.json (via ctl wrapper only)",
        ]
    if _matches_role(role, "repair-coordinator"):
        return [".dynos/task-*/repair-log.json"]
    if _matches_role(role, "audit-*"):
        return [".dynos/task-*/audit-reports/*.json"]
    if role == "execute-inline" or role.endswith("-executor"):
        return [
            "repo-owned source/test/doc/config files",
            ".dynos/task-*/evidence/*.md",
        ]
    if role == "ctl":
        return [
            ".dynos/task-*/manifest.json",
            ".dynos/task-*/external-solution-gate.json",
            ".dynos/task-*/handoff-*.json",
            ".dynos/task-*/classification.json",
            ".dynos/task-*/execution-graph.json",
            ".dynos/task-*/repair-log.json",
        ]
    if role == "receipt-writer":
        return [
            ".dynos/task-*/receipts/*.json",
            ".dynos/task-*/token-usage.json",
        ]
    if role in {"scheduler", "eventbus", "system"}:
        return [
            ".dynos/task-*/events.jsonl",
            ".dynos/events.jsonl",
            ".dynos/task-*/token-usage.json",
        ]
    return []


# Device sinks: writing here persists nothing, so no boundary applies.
# Skill steps legitimately use `2>/dev/null`; denying a sink only produces
# noise and teaches users that denials are arbitrary.
_DEVICE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

# Roles that may use the task-scoped scratch namespace. _scratch/ is
# deliberately excluded from every receipt, gate, and validator read path
# (asserted by tests/test_scratch_namespace.py), so a scratch write can never
# certify or forge anything — it is temp space inside the task boundary.
_SCRATCH_ROLES = frozenset({
    "orchestrator", "planning", "execute-inline", "repair-coordinator",
})


def _scratch_allowed_for(role: str) -> bool:
    return (
        role in _SCRATCH_ROLES
        or role.endswith("-executor")
        or role.startswith("audit-")
    )


# --- H1 self-modification guard (docs/permissions-on-design.md §D5-3) -------
# Executor roles may write repo work artifacts anywhere, which historically
# included the *installed plugin directory itself* — hooks.json, this file,
# ctl.py. That is the "agent disables its own guardrails" primitive. Agent
# roles are therefore denied writes under the plugin root and under the
# host-level config locations that control hook execution.
#
# Developer mode: when the project being worked on IS the plugin source repo
# (its root equals the plugin root and carries .claude-plugin/), plugin files
# are ordinary repo files reviewed by the human like any other diff — the
# guard stands down for the plugin-root check only.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Roles that are deterministic framework code, not LLM tool calls.
_FRAMEWORK_ROLES = frozenset({
    "ctl", "scheduler", "eventbus", "system", "receipt-writer", "daemon",
})


def _is_plugin_source_checkout(root: Path) -> bool:
    """True when root is a checked-out plugin source repo.

    Key off the project root, not _PLUGIN_ROOT: the running hook may be the
    cached install while the developer is editing the source checkout.
    """
    try:
        return (root / ".claude-plugin").is_dir() and (root / ".git").exists()
    except Exception:
        return False


def _protected_host_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".claude" / "plugins",
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        home / ".dynos",
    )


def _is_under(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def _self_modification_denial(attempt: WriteAttempt, path: Path) -> WriteDecision | None:
    """Return a deny decision when an agent role targets guardrail-owning paths."""
    if attempt.role in _FRAMEWORK_ROLES:
        return None
    if _is_under(path, _PLUGIN_ROOT):
        if attempt.task_dir is not None:
            project_root = attempt.task_dir.resolve().parent.parent
            dev_mode = project_root == _PLUGIN_ROOT and _is_plugin_source_checkout(project_root)
        else:
            # No active task: developer mode iff the plugin root is a source
            # checkout (marketplace cache installs ship without .git).
            dev_mode = _is_plugin_source_checkout(_PLUGIN_ROOT)
        if not dev_mode:
            return WriteDecision(
                False,
                "plugin installation directory is not writable by task "
                "agents (guardrail self-modification defense)",
                "deny",
            )
        return None
    for protected in _protected_host_paths():
        if path == protected or _is_under(path, protected):
            return WriteDecision(
                False,
                f"{protected} controls harness/hook execution and is not "
                "writable by task agents (guardrail self-modification defense)",
                "deny",
            )
    return None


def _nearest_project_root_from_cwd(cwd: Path) -> Path | None:
    """Return nearest ancestor governed by a project .dynos, or None.

    The `.dynos` name is overloaded (framework home vs. per-project control
    plane), so a bare-name match would treat `$HOME` — or a stray orphan
    `.dynos` high in the tree — as a project root and pull unrelated repos into
    governance. `is_project_dynos_dir` is the shared discriminator that rejects
    those cases; on import failure fall back to excluding the framework home,
    which is the critical part of the guard.
    """
    resolved = cwd.resolve()
    try:
        from lib_dynos_root import is_project_dynos_dir  # noqa: PLC0415
    except Exception:
        _home = Path(
            os.environ.get("DYNOS_HOME") or (Path.home() / ".dynos")
        ).expanduser().resolve()

        def is_project_dynos_dir(dynos_dir: Path) -> bool:
            try:
                return dynos_dir.is_dir() and dynos_dir.resolve() != _home
            except Exception:
                return False

    for candidate in (resolved, *resolved.parents):
        if is_project_dynos_dir(candidate / ".dynos"):
            return candidate
    return None


def _persistent_project_root(project_root: Path) -> Path | None:
    """Resolve the persistent project dir without importing lib_core.

    write_policy is imported by lib_core, so importing _persistent_project_dir
    here would create a cycle. This mirrors lib_core's pure path derivation and
    intentionally swallows identity-resolution failures: inability to compute a
    persistence path must not widen the governed project scope.
    """
    try:
        from lib_project_id import resolve_project_id  # noqa: PLC0415

        dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
        return (dynos_home / "projects" / resolve_project_id(project_root)).resolve()
    except Exception:
        return None


def _project_scope_roots(attempt: WriteAttempt) -> tuple[Path, ...]:
    """Return roots governed by dynos-work for this attempt."""
    project_root: Path | None = None
    roots: list[Path] = []
    if attempt.task_dir is not None:
        task_dir = attempt.task_dir.resolve()
        project_root = task_dir.parent.parent.resolve()
        roots.extend([project_root, task_dir])
    else:
        project_root = _nearest_project_root_from_cwd(Path.cwd())
        if project_root is not None:
            roots.append(project_root.resolve())

    if project_root is not None:
        persistent = _persistent_project_root(project_root)
        if persistent is not None:
            roots.append(persistent)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return tuple(deduped)


def _is_inside_project_scope(attempt: WriteAttempt, path: Path) -> bool:
    roots = _project_scope_roots(attempt)
    if roots:
        return any(path == root or _is_under(path, root) for root in roots)
    # No project root resolved from the active task or the hook's cwd. Rather
    # than govern-when-unsure (which treated every non-dynos folder — and, via
    # the overloaded `.dynos` home, all of $HOME — as in-scope and produced
    # spurious write denials), decide from the TARGET path itself: govern only
    # when the write lands inside a real project control plane. A write to a
    # location with no governing project .dynos ancestor is simply not ours to
    # police. The self-modification guard and hook-owned control-plane checks
    # in decide_write run *before* this gate, so they still apply regardless.
    try:
        target_parent = path.parent
    except Exception:
        return False
    return _nearest_project_root_from_cwd(target_parent) is not None


# Task-root files the orchestrator authors directly: logs, escalation notes,
# the audit-context sidecar, the raw task input, and the discovery/design Q&A
# records it appends user answers to. spec.md / plan.md / audit-reports /
# evidence stay out — those belong to the planner, auditors, and executors.
_ORCHESTRATOR_TASK_FILES = frozenset({
    "execution-log.md",
    "escalation.md",
    "audit-context.md",
    "raw-input.md",
    "discovery-notes.md",
    "design-decisions.md",
})


def _orchestrator_inline_execution_active(task_dir: Path | None) -> bool:
    """True when the orchestrator may write repo files: inline fast-track
    execution (single segment, no subagent spawn). Capability follows the
    deterministic state machine — stage and fast_track are set exclusively
    by ctl gates, never by the model."""
    if task_dir is None:
        return False
    try:
        manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        manifest.get("stage") in {"EXECUTION", "TEST_EXECUTION"}
        and manifest.get("fast_track") is True
    )


def decide_write(attempt: WriteAttempt) -> WriteDecision:
    path = attempt.path.resolve()
    if str(path) in _DEVICE_SINKS:
        return WriteDecision(True, "device sink — not a persistence target", "direct")
    self_mod = _self_modification_denial(attempt, path)
    if self_mod is not None:
        return self_mod
    # The orchestrator-session pin is hook-owned identity state (D3): only
    # the SessionStart hook subprocess writes it. If any agent role could
    # rewrite or delete it, a session could re-pin itself or force the
    # orchestrator into the grant-consuming subagent branch.
    if path.name == "orchestrator-session.json" and path.parent.name == ".dynos":
        return WriteDecision(
            False,
            "orchestrator-session.json is hook-owned actor identity; only "
            "the SessionStart hook subprocess may write it",
            "deny",
        )
    if path.name == "session-tasks.json" and path.parent.name == ".dynos":
        return WriteDecision(
            False,
            "session-tasks.json is hook-owned task identity; only "
            "dynos hook subprocesses may write it",
            "deny",
        )
    # control-plane.json is hook-owned actor/host identity (same elevation
    # class as orchestrator-session.json above). It drives host resolution for
    # receipt anti-forgery validation (receipts/stage.py) and token capture
    # (lib_tokens_hook.py): if an agent could write {"host": "codex"}, receipt
    # writers would accept resolved_model=None and the model cross-check would
    # be blinded. ALL agent roles are denied — including executors and the
    # orchestrator. The framework write path is the SessionStart hook
    # subprocess's direct file I/O (lib_host.persist_host), which does not pass
    # through this policy because hook subprocesses do not invoke harness tools.
    if path.name == "control-plane.json" and path.parent.name == ".dynos":
        return WriteDecision(
            False,
            "control-plane.json is hook-owned actor/host identity; only "
            "the SessionStart hook subprocess may write it",
            "deny",
        )
    if path.name == ".rules_corrupt" and path.parent.name == ".dynos":
        if attempt.role == "daemon":
            return WriteDecision(True, ".dynos/.rules_corrupt allowed for daemon role", "direct")
        return WriteDecision(False, ".dynos/.rules_corrupt is daemon-owned kill-switch state; agent writes denied", "deny")
    # FINDING #1: cross-task control-plane write bypass guard.
    # Agent roles (execute-inline, *-executor) must not write control-plane
    # artifacts that belong to a DIFFERENT task directory, regardless of which
    # task directory the role is bound to.  _FRAMEWORK_ROLES are exempt because
    # they are deterministic code, not LLM tool calls.
    if attempt.role not in _FRAMEWORK_ROLES:
        target = _owning_task_dir(path)
        if target is not None and (
            attempt.task_dir is None
            or target.resolve() != attempt.task_dir.resolve()
        ):
            tgt_rel = path.resolve().relative_to(target.resolve()).as_posix()
            if _is_cross_task_control_plane(tgt_rel):
                return WriteDecision(
                    False,
                    f"cross-task control-plane write denied: {path.name}",
                    "deny",
                )
    if not _is_inside_project_scope(attempt, path):
        return WriteDecision(
            True,
            "outside dynos-work project scope - not governed",
            "direct",
        )
    rel = _task_relative(path, attempt.task_dir)
    rel_posix = rel.as_posix() if rel is not None else None

    # Task-scoped scratch namespace (D2 in docs/permissions-on-design.md):
    # sanctioned temp space for any recognized actor role. Nothing in the
    # control plane reads _scratch/, so these writes are proof-irrelevant.
    if rel_posix is not None and (
        rel_posix == "_scratch" or rel_posix.startswith("_scratch/")
    ):
        if _scratch_allowed_for(attempt.role):
            return WriteDecision(True, "task scratch namespace", "direct")
        return WriteDecision(
            False,
            f"_scratch/ is reserved for recognized task actor roles; "
            f"role={attempt.role!r} is not one",
            "deny",
        )

    if rel_posix is not None and rel_posix in _WRAPPER_REQUIRED:
        if attempt.role == "ctl":
            return WriteDecision(True, f"{rel_posix} persisted by ctl wrapper", "direct")
        return WriteDecision(
            False,
            f"{rel_posix} must be persisted through ctl wrapper",
            "wrapper",
            _WRAPPER_REQUIRED[rel_posix],
        )

    if rel_posix == "manifest.json":
        if attempt.role == "ctl":
            return WriteDecision(True, "manifest.json is code-owned ctl state", "direct")
        return WriteDecision(False, "manifest.json is code-owned control-plane state", "deny")

    # audit-grep-quota.json is read-policy state owned by the read_policy
    # module's atomic writer (which runs inside the pre_tool_use hook
    # subprocess and does NOT route through this policy — Python file
    # operations are not tool calls). Any write/delete from an agent role
    # via Bash/Write/Edit is denied here parser-independently, closing
    # sec-1's quota-deletion bypass.
    if rel_posix == "audit-grep-quota.json":
        return WriteDecision(
            False,
            "audit-grep-quota.json is read-policy state; agent writes are denied",
            "deny",
        )

    if rel_posix == "external-solution-gate.json":
        if attempt.role == "ctl":
            return WriteDecision(True, "external-solution-gate.json is ctl-owned control-plane state", "direct")
        return WriteDecision(False, "external-solution-gate.json is code-owned control-plane state", "deny")

    if rel_posix == "token-usage.json":
        if attempt.role in {"system", "receipt-writer"}:
            return WriteDecision(True, "token-usage.json is system-owned usage state", "direct")
        return WriteDecision(False, "token-usage.json is code-owned control-plane state", "deny")

    if rel_posix == "events.jsonl":
        if attempt.role in {"eventbus", "system"}:
            return WriteDecision(True, "events.jsonl is eventbus/system-owned log state", "direct")
        return WriteDecision(False, "events.jsonl is code-owned control-plane state", "deny")

    if rel_posix == "spawn-log.jsonl":
        # spawn-log.jsonl is hook-owned. The agent-spawn-log hook subprocess
        # appends to it directly via Python file I/O (bypassing this policy
        # entirely because hook subprocesses do not invoke harness tools).
        # No agent role can claim it via Write/Edit/MultiEdit/Bash — those
        # paths all flow through this policy and are denied here. This is the
        # mechanical defense against the audit-chain forgery pattern: the
        # orchestrator cannot fabricate spawn-log entries because it has no
        # write path to the file.
        return WriteDecision(
            False,
            "spawn-log.jsonl is hook-owned harness telemetry; only the "
            "agent-spawn-log hook subprocess may append to it",
            "deny",
        )

    if rel_posix == "role-bindings.json":
        # Session->role bindings are hook-owned (the pre_tool_use subprocess
        # writes them via direct file I/O when a subagent's first tool call
        # consumes a grant). If an agent could write this file it could bind
        # its own session to an arbitrary granted role out of order.
        return WriteDecision(
            False,
            "role-bindings.json is hook-owned actor identity; only the "
            "pre-tool-use hook subprocess may write it",
            "deny",
        )

    if rel_posix == "web-tool-log.jsonl":
        # web-tool-log.jsonl is hook-owned. The web-tool-log hook subprocess
        # appends to it directly via Python file I/O (bypassing this policy
        # entirely because hook subprocesses do not invoke harness tools).
        # No agent role can claim it via Write/Edit/MultiEdit/Bash — those
        # paths all flow through this policy and are denied here. This mirrors
        # the spawn-log.jsonl enforcement pattern.
        return WriteDecision(
            False,
            "web-tool-log.jsonl is hook-owned harness telemetry; only the "
            "web-tool-log hook subprocess may append to it",
            "deny",
        )

    if rel_posix == "tool-call-counters.json":
        # tool-call-counters.json is hook-owned telemetry (D3 artifact-durability).
        # The hook subprocess writes it via direct file I/O (atomic rename),
        # bypassing this policy entirely. All agent roles are denied — including
        # executors, orchestrators, and receipt-writers. This closes the
        # fabrication vector: an agent cannot increment or reset its own counters.
        return WriteDecision(
            False,
            "tool-call-counters.json is hook-owned telemetry; only the "
            "hook subprocess may write it",
            "deny",
        )

    if rel_posix is not None and rel_posix.startswith("receipts/"):
        if attempt.role == "receipt-writer":
            return WriteDecision(True, "receipts are receipt-writer-owned control-plane state", "direct")
        return WriteDecision(False, "receipts are code-owned control-plane state", "deny")

    if rel_posix is not None and rel_posix.startswith("handoff-") and rel_posix.endswith(".json"):
        if attempt.role == "ctl":
            return WriteDecision(True, "handoff json is ctl-owned control-plane state", "direct")
        return WriteDecision(False, "handoff json is code-owned control-plane state", "deny")

    if rel_posix is not None and rel_posix.startswith("audit-reports/"):
        if attempt.role == "ctl" and attempt.operation == "create":
            return WriteDecision(True, "ctl skeleton pre-creation", "direct")
        if attempt.role.startswith("audit-"):
            return WriteDecision(True, "audit report is auditor-owned evidence", "direct")
        return WriteDecision(False, "audit reports are reserved for auditor roles", "deny")

    if rel_posix is not None and rel_posix.startswith("evidence/verification/"):
        # Machine-captured verification evidence (D6): written ONLY by
        # `ctl run-verification-evidence`. If executors could write here
        # they could doctor the captured exit codes — the exact narrative-
        # claim laundering this artifact exists to prevent.
        if attempt.role == "ctl":
            return WriteDecision(True, "verification evidence is ctl-captured", "direct")
        return WriteDecision(
            False,
            "evidence/verification/ is machine-captured by "
            "`ctl.py run-verification-evidence`; agent roles may not write it",
            "deny",
        )

    if rel_posix is not None and rel_posix.startswith("evidence/"):
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(True, "evidence markdown is executor-owned", "direct")
        if attempt.role == "orchestrator" and _orchestrator_inline_execution_active(attempt.task_dir):
            # Inline fast-track execution: the orchestrator IS the executor
            # for the single segment, so it writes that segment's evidence.
            return WriteDecision(True, "inline fast-track evidence write", "direct")
        return WriteDecision(False, "evidence artifacts are reserved for executor roles", "deny")

    if rel_posix is None:
        # Path lives outside the task dir. Executor and planning roles
        # legitimately write repo artifacts (src/, tests/, docs/) as part of
        # their work — those are NOT "task boundary escapes." System roles
        # (scheduler/eventbus/system) likewise need the global log path.
        # Only reject out-of-task paths when the role has no authority to
        # write repo artifacts.
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(True, "repo work artifact allowed for executor role", "direct")
        if attempt.role == "orchestrator":
            # The orchestrator only writes repo files itself during inline
            # fast-track execution (no subagent spawn); otherwise repo work
            # belongs to executor subagents.
            if _orchestrator_inline_execution_active(attempt.task_dir):
                return WriteDecision(
                    True,
                    "repo work artifact allowed for orchestrator during "
                    "inline fast-track execution",
                    "direct",
                )
            _proot = (
                attempt.task_dir.resolve().parent.parent
                if attempt.task_dir is not None
                else _nearest_project_root_from_cwd(Path.cwd())
            )
            if _proot is not None and _is_plugin_source_checkout(_proot):
                return WriteDecision(
                    True,
                    "repo work artifact allowed: orchestrator developer mode",
                    "direct",
                )
            return WriteDecision(
                False,
                "orchestrator may not write repo files outside inline "
                "fast-track execution; spawn an executor (after "
                "`ctl.py grant-role`) or use the task _scratch/ dir",
                "deny",
            )
        if attempt.role in {"scheduler", "eventbus", "system"}:
            return WriteDecision(True, "non-task system path allowed", "direct")
        if attempt.task_dir is not None:
            return WriteDecision(False, "path escapes task boundary", "deny")
        return WriteDecision(False, "non-task path requires system-owned writer", "deny")
    # plan.md and spec.md are human-approved artifacts locked at PLAN_REVIEW.
    # Executor roles must not mutate them — renaming or removing sections
    # bypasses gap-analysis and plan-validation gates without triggering any
    # downstream hash mismatch (the plan-validated receipt can be refreshed).
    # The planning role retains write access so it can author these files
    # before approval; after approval only ctl/operator paths should touch them.
    if rel_posix in {"plan.md", "spec.md"}:
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(
                False,
                f"{rel_posix} is a human-approved artifact; executor roles may not write it after PLAN_REVIEW",
                "deny",
            )

    # ctl-owned aggregation artifacts at task root — explicit deny with a clear
    # reason so agents receive a useful message instead of the catch-all below.
    if rel_posix in {"audit-summary.json", "audit-plan.json"}:
        if attempt.role != "ctl":
            return WriteDecision(
                False,
                f"{rel_posix} is a ctl-owned aggregation artifact written by "
                "`python3 hooks/ctl.py run-audit-setup` / `run-audit-summary`; "
                "agents must not write it directly",
                "deny",
            )

    if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
        return WriteDecision(True, "repo work artifact allowed for executor role", "direct")
    if attempt.role == "orchestrator":
        if rel_posix in _ORCHESTRATOR_TASK_FILES:
            return WriteDecision(True, f"{rel_posix} is orchestrator-owned coordination output", "direct")
        return WriteDecision(
            False,
            f"orchestrator role may not write {rel_posix}; planner/auditor/"
            "executor artifacts are written by their subagents, control-plane "
            "files by ctl wrappers",
            "deny",
        )
    if attempt.role == "planning":
        if rel_posix in {"discovery-notes.md", "design-decisions.md", "design-doc.md", "spec.md", "plan.md"}:
            return WriteDecision(True, f"{rel_posix} is planner-owned judgment output", "direct")
        return WriteDecision(False, "planning role may only write planning artifacts", "deny")
    if attempt.role == "repair-coordinator":
        return WriteDecision(False, "repair-coordinator may only persist repair-log via wrapper", "wrapper", _WRAPPER_REQUIRED["repair-log.json"])
    if attempt.role == "ctl":
        return WriteDecision(True, "ctl direct write allowed", "direct")
    if attempt.role in {"scheduler", "eventbus", "system", "receipt-writer"}:
        return WriteDecision(True, f"{attempt.role} write allowed", "direct")
    return WriteDecision(False, f"no write policy matched for role={attempt.role}", "deny")


def _emit_policy_event(attempt: WriteAttempt, decision: WriteDecision) -> None:
    if attempt.task_dir is None:
        return
    try:
        from lib_log import log_event
    except Exception:
        return
    try:
        root = attempt.task_dir.parent.parent
        task_id = attempt.task_dir.name
        rel = attempt.path.resolve()
        try:
            rel = rel.relative_to(root.resolve())
        except Exception:
            pass
        payload = {
            "role": attempt.role,
            "path": str(rel),
            "operation": attempt.operation,
            "reason": decision.reason,
        }
        if decision.mode == "direct":
            log_event(root, "write_policy_allowed", task=task_id, mode=decision.mode, **payload)
        elif decision.mode == "wrapper":
            log_event(
                root,
                "write_policy_wrapper_required",
                task=task_id,
                wrapper_command=decision.wrapper_command,
                **payload,
            )
        else:
            log_event(root, "write_policy_denied", task=task_id, **payload)
    except Exception:
        pass


def require_write_allowed(
    attempt: WriteAttempt, *, capability_key: object, emit_event: bool = True
) -> None:
    # PRO-001: identity-check the capability key against the per-role sentinel.
    # For privileged roles (in _CAPABILITY_KEYS) the key must match the dict
    # entry. For non-privileged roles the key must be None — there is no
    # registered sentinel to match. Either path is enforced by the same
    # identity check: dict.get returns None for missing roles, so non-privileged
    # callers passing capability_key=None pass; non-privileged callers passing
    # any non-None key fail.
    if capability_key is not _CAPABILITY_KEYS.get(attempt.role):
        raise ValueError(
            f"capability_key mismatch for role {attempt.role!r}: "
            f"token does not match the registered sentinel"
        )
    decision = decide_write(attempt)
    if emit_event:
        _emit_policy_event(attempt, decision)
    if not decision.allowed:
        raise ValueError(decision.reason)


def find_write_violations(
    *,
    role: str,
    task_dir: Path | None,
    paths: list[Path],
    source: WriteSource,
    operation: WriteOperation = "modify",
) -> list[str]:
    """Return human-readable violations for a batch of touched paths."""
    violations: list[str] = []
    for path in paths:
        attempt = WriteAttempt(
            role=role,
            task_dir=task_dir,
            path=path,
            operation=operation,
            source=source,
        )
        decision = decide_write(attempt)
        _emit_policy_event(attempt, decision)
        if not decision.allowed:
            label = str(path)
            if task_dir is not None:
                rel = _task_relative(path.resolve(), task_dir)
                if rel is not None:
                    label = rel.as_posix()
            violations.append(f"{label}: {decision.reason}")
    return violations
