#!/usr/bin/env python3
"""Core utilities, constants, and path helpers for dynos-work."""

from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib_project_id import _safe_git_env, resolve_project_id
from write_policy import WriteAttempt, _get_capability_key, require_write_allowed


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# PRO-007: pin python3/git binaries to absolute paths resolved at import time
# so PATH-shadowing cannot substitute a malicious binary.
_PYTHON3: str = shutil.which("python3") or sys.executable
_GIT: str | None = shutil.which("git")

def _discover_executors() -> set[str]:
    """Auto-discover executor types from agents/*-executor.md files."""
    agents_dir = Path(__file__).resolve().parent.parent / "agents"
    _FALLBACK = {
        "ui-executor", "backend-executor", "ml-executor", "db-executor",
        "refactor-executor", "testing-executor", "integration-executor", "docs-executor",
    }
    if not agents_dir.is_dir():
        return _FALLBACK
    discovered = {f.stem for f in agents_dir.glob("*-executor.md") if f.is_file()}
    return discovered if discovered else _FALLBACK


VALID_EXECUTORS: set[str] = _discover_executors()

VALID_CLASSIFICATION_TYPES: set[str] = {
    "feature",
    "bugfix",
    "refactor",
    "migration",
    "ml",
    "full-stack",
}

VALID_DOMAINS: set[str] = {
    "ui", "backend", "db", "ml", "security",
    "testing", "refactor", "migration", "docs", "infra",
}
VALID_RISK_LEVELS: set[str] = {"low", "medium", "high", "critical"}

COMPOSITE_WEIGHTS: tuple[float, float, float] = (0.6, 0.25, 0.15)

STAGE_ORDER: list[str] = [
    "FOUNDRY_INITIALIZED",
    "CLASSIFY_AND_SPEC",
    "SPEC_NORMALIZATION",
    "SPEC_REVIEW",
    "PLANNING",
    "PLAN_REVIEW",
    "PLAN_AUDIT",
    "TDD_REVIEW",
    "PRE_EXECUTION_SNAPSHOT",
    "EXECUTION_GRAPH_BUILD",
    "EXECUTION",
    "TEST_EXECUTION",
    "CHECKPOINT_AUDIT",
    "FINAL_AUDIT",
    "REPAIR_PLANNING",
    "REPAIR_EXECUTION",
    "DONE",
    "CALIBRATED",
    "CANCELLED",
    "FAILED",
]

ALLOWED_STAGE_TRANSITIONS: dict[str, set[str]] = {
    "CLASSIFY_AND_SPEC": {"SPEC_NORMALIZATION", "SPEC_REVIEW", "PLANNING", "FAILED", "CANCELLED"},
    "FOUNDRY_INITIALIZED": {"SPEC_NORMALIZATION", "FAILED"},
    "SPEC_NORMALIZATION": {"SPEC_REVIEW", "FAILED"},
    "SPEC_REVIEW": {"SPEC_NORMALIZATION", "PLANNING", "FAILED"},
    "PLANNING": {"PLAN_REVIEW", "FAILED"},
    "PLAN_REVIEW": {"PLANNING", "PLAN_AUDIT", "FAILED"},
    "PLAN_AUDIT": {"TDD_REVIEW", "PRE_EXECUTION_SNAPSHOT", "FAILED", "PLANNING"},
    "TDD_REVIEW": {"PRE_EXECUTION_SNAPSHOT", "FAILED"},
    "PRE_EXECUTION_SNAPSHOT": {"EXECUTION", "FAILED"},
    "EXECUTION": {"TEST_EXECUTION", "REPAIR_PLANNING", "FAILED"},
    "TEST_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "CHECKPOINT_AUDIT": {"REPAIR_PLANNING", "DONE", "FAILED", "FINAL_AUDIT"},
    "REPAIR_PLANNING": {"REPAIR_EXECUTION", "FAILED"},
    "REPAIR_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "FINAL_AUDIT": {"CHECKPOINT_AUDIT", "DONE", "FAILED"},
    "EXECUTION_GRAPH_BUILD": {"PRE_EXECUTION_SNAPSHOT", "EXECUTION", "FAILED"},
    "DONE": {"CALIBRATED", "FAILED"},
    "CALIBRATED": set(),
    "CANCELLED": set(),
    "FAILED": set(),
}

# CALIBRATED has no outgoing transitions and is reached after DONE; treat it
# as terminal everywhere active-task state is derived.
TERMINAL_STAGES: frozenset[str] = frozenset({
    "DONE", "FAILED", "CANCELLED", "CALIBRATED",
})

NEXT_COMMAND: dict[str, str] = {
    "CLASSIFY_AND_SPEC": "/dynos-work:start",
    "FOUNDRY_INITIALIZED": "/dynos-work:start",
    "SPEC_NORMALIZATION": "/dynos-work:start",
    "SPEC_REVIEW": "/dynos-work:start",
    "PLANNING": "/dynos-work:plan",
    "PLAN_REVIEW": "/dynos-work:plan",
    "PLAN_AUDIT": "/dynos-work:plan",
    "TDD_REVIEW": "/dynos-work:plan",
    "EXECUTION_GRAPH_BUILD": "/dynos-work:execute",
    "PRE_EXECUTION_SNAPSHOT": "/dynos-work:execute",
    "EXECUTION": "/dynos-work:execute",
    "TEST_EXECUTION": "/dynos-work:execute",
    "CHECKPOINT_AUDIT": "/dynos-work:audit",
    "REPAIR_PLANNING": "/dynos-work:audit",
    "REPAIR_EXECUTION": "/dynos-work:audit",
    "FINAL_AUDIT": "/dynos-work:audit",
    "DONE": "Task complete",
    "CALIBRATED": "Task calibrated",
    "CANCELLED": "Task cancelled",
    "FAILED": "Review failure state before continuing",
}

# Token-estimate lookup keyed by tier name (AC-1 guard requirement).
TOKEN_ESTIMATES_BY_TIER: dict[str, int] = {"fast": 12000, "balanced": 25000, "deep": 45000}

# Model/tier-keyed estimates. Tier keys are composed in from
# TOKEN_ESTIMATES_BY_TIER so consumers that receive either a vendor model
# name or a tier name resolve through this one dict; vendor keys are
# byte-identical to the pre-task values.
TOKEN_ESTIMATES: dict[str, int] = {
    "opus": 45000,  # noqa: model-literal
    "sonnet": 25000,  # noqa: model-literal
    "haiku": 12000,  # noqa: model-literal
    "default": 20000,
    **TOKEN_ESTIMATES_BY_TIER,
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: object, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default if not numeric."""
    return float(value) if isinstance(value, (int, float)) else default


def _safe_int(value: object, default: int = 0) -> int:
    """Safely convert a value to int, returning default if not numeric."""
    return int(value) if isinstance(value, (int, float)) else default


def load_json(path: Path) -> dict:
    """Read and parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    """Atomic JSON write: write to temp file then rename to avoid partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(data, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_ctl_json(task_dir: Path, path: Path, data: Any) -> None:
    """Persist ctl-owned task JSON through the write-boundary policy."""
    require_write_allowed(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=path,
            operation="modify" if path.exists() else "create",
            source="ctl",
        ),
        capability_key=_get_capability_key("ctl"),
    )
    write_json(path, data)


def require(path: Path) -> str:
    """Read and return the text content of a file."""
    return path.read_text()


# ---------------------------------------------------------------------------
# Persistent project directory
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _resolve_git_toplevel(root_str: str) -> Optional[str]:
    """Return the absolute path of the main working tree if `root_str` is
    inside a git repository — otherwise None.

    For git worktrees, `git rev-parse --show-toplevel` returns the WORKTREE's
    toplevel (not the main repo). That's the wrong target for us; we want all
    worktrees to fold back to ONE canonical slug. We get that by asking for
    `--git-common-dir` (the `.git/` directory shared across worktrees) and
    taking its parent: the path that directory lives in is the main working
    tree.

    Cached per-process on the input string so repeated _persistent_project_dir
    calls don't re-shell-out. The cache key is the raw input path (not the
    resolved toplevel) because different input paths can legitimately map to
    different toplevels if the process crosses repo boundaries.

    Returns None on any of: git not installed, path not inside a repo,
    git command returns non-zero, output is empty. Callers fall back to the
    legacy slug-from-absolute-path behavior.
    """
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", root_str, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_safe_git_env(),  # T-2: strip GIT_DIR/GIT_CONFIG_* injection
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common_dir = result.stdout.strip()
    if not common_dir:
        return None
    # --git-common-dir returns a path that may be relative to root_str.
    # Resolve it to absolute, then take its parent (the main working tree).
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (Path(root_str) / common_path).resolve()
    else:
        common_path = common_path.resolve()
    # Bare repos (no working tree) return `.git` or the repo root itself as
    # --git-common-dir. If we're in a bare repo there's no "main working
    # tree" to canonicalize to; fall back.
    if common_path.name != ".git":
        return None
    main_worktree = common_path.parent
    # T-5: parent-contains-root check. The input root must be the same as or
    # a subdirectory of the main worktree. If a planted .git dir was resolved
    # to an unrelated location (e.g. via GIT_CEILING bypass residual), the
    # root would NOT be under main_worktree — reject it.
    try:
        resolved_root = Path(root_str).resolve()
    except OSError:
        return None
    try:
        resolved_root.relative_to(main_worktree)
    except ValueError:
        return None
    return str(main_worktree)


# Path-safe task-id slug. Anchored, no path separators, no leading dot.
# Matches dynos-style ``task-YYYYMMDD-NNN`` plus test ids ``task-T``, ``task-A``.
# A crafted ``task_id`` like ``../etc`` cannot pass — used by callers that
# construct ``root / ".dynos" / task_id`` from untrusted input (retrospective
# JSON, CLI args, etc.).
_TASK_ID_SLUG_RE: "re.Pattern[str]" = re.compile(
    r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$"
)


def is_safe_task_id(task_id: object) -> bool:
    """Return True if ``task_id`` is a string matching the safe slug regex.

    Use this BEFORE constructing any filesystem path from a task_id that
    originated outside the harness (retrospective JSON, CLI args, postmortem
    payloads, etc.). Path-traversal attempts (``../etc``, ``a/b``) and
    leading-dot components (``..``, ``.hidden``) are rejected.

    The slug pattern is intentionally narrow — it MUST match every legitimate
    task_id the harness produces. If a future task_id format is introduced,
    update _TASK_ID_SLUG_RE in lockstep.
    """
    return isinstance(task_id, str) and bool(_TASK_ID_SLUG_RE.match(task_id))


# Parses an allocated task id back into its (date, seq) fields. The entropy
# suffix is optional so this also reads legacy ``task-YYYYMMDD-NNN`` dirs that
# predate the suffix — they still contribute to the local seq high-water mark.
_TASK_ID_ALLOC_RE: "re.Pattern[str]" = re.compile(
    r"^task-(\d{8})-(\d{3})(?:-[0-9a-f]+)?$"
)


def allocate_task_id(
    dynos_dir: Path,
    *,
    now: "datetime | None" = None,
    rand=None,
) -> "tuple[str, Path]":
    """Atomically allocate a unique task id and create its dir under ``dynos_dir``.

    Returns ``(task_id, task_dir)`` where ``task_id`` has the form
    ``task-{YYYYMMDD}-{seq:03d}-{rand}``:

    - ``YYYYMMDD`` is the **UTC** date. All allocators must agree on UTC so two
      callers near midnight never disagree on the date segment.
    - ``seq`` is a best-effort, *local* monotonic counter derived from sibling
      task dirs in *this* ``dynos_dir``. It is NOT a global uniqueness
      guarantee: concurrent allocations from separate worktrees (each with its
      own root-relative ``.dynos/``) cannot see each other's dirs and will
      reuse the same ``seq``. It exists only for human readability.
    - ``rand`` is 8 hex chars (32 bits) of CSPRNG entropy. This is what makes
      the id collision-resistant across independent ``.dynos`` roots AND the
      shared persistent project store (``~/.dynos/projects/{uuid}/``, keyed by
      task_id), which is what last-writer-wins clobbered when the old
      date+counter scheme handed concurrent worktrees the same id.

    The dir is claimed with ``mkdir()`` (atomic, no ``exist_ok``) inside a
    retry loop; on the rare local entropy clash the suffix is re-rolled. The
    returned id always satisfies :func:`is_safe_task_id`.

    ``now``/``rand`` are injectable for deterministic tests.
    """
    dynos_dir.mkdir(parents=True, exist_ok=True)
    when = now or datetime.now(timezone.utc)
    today = when.strftime("%Y%m%d")
    seq = 1
    for existing in dynos_dir.glob(f"task-{today}-*"):
        match = _TASK_ID_ALLOC_RE.match(existing.name)
        if match and match.group(1) == today:
            seq = max(seq, int(match.group(2)) + 1)
    rand_fn = rand or (lambda: secrets.token_hex(4))
    for _ in range(64):
        task_id = f"task-{today}-{seq:03d}-{rand_fn()}"
        task_dir = dynos_dir / task_id
        try:
            task_dir.mkdir()
        except FileExistsError:
            continue
        return task_id, task_dir
    raise RuntimeError(
        f"could not allocate a unique task id for {today} after 64 attempts"
    )


def _persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project state.

    Pure path resolution — NO side effects. Does NOT create directories.
    Callers that need the directory to exist should call
    ensure_persistent_project_dir() instead.

    Slug derivation (delegated to lib_project_id.resolve_project_id):
    - If `root` is inside a git repository: a UUID4 stored in
      <git-common-dir>/dynos-project-id. All worktrees for the same repo
      share one persistent dir — learning state does not fragment per-branch.
    - Otherwise (not a git repo, git not installed, bare repo): a sanitised
      path slug ``path-{abs-path-slug}``.
    Raises ProjectIdSecurityError on threat detection (symlink, hostile
    content, foreign-uid ownership). Callers must NOT catch it silently.
    """
    dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
    # resolve_project_id returns a UUID4 slug (git repo) or 'path-...' slug
    # (non-git fallback). ProjectIdSecurityError propagates to the caller — do
    # NOT catch it here.
    slug = resolve_project_id(root)
    uuid_dir = dynos_home / "projects" / slug
    # Seg-7 dual-read compat: if the UUID dir is empty and a legacy
    # path-slug dir exists for the same root, prefer the legacy dir and emit
    # an observable event. ``check_dual_read`` returns ``None`` in the
    # steady-state UUID case, in which case we return the UUID dir as before.
    from lib_compat_legacy_slug import check_dual_read  # noqa: PLC0415
    legacy = check_dual_read(root, uuid_dir, dynos_home)
    if legacy is not None:
        return legacy
    return uuid_dir


def ensure_persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/, creating it if needed.

    Use this when you need to WRITE to the persistent directory.
    Use _persistent_project_dir() for read-only path resolution.
    """
    d = _persistent_project_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_learning_enabled(root: Path) -> bool:
    """Check whether the learning layer is enabled for this project.

    Reads ``learning_enabled`` from ``~/.dynos/projects/{slug}/policy.json``.
    Defaults to ``True`` when the file or key is missing — learning is opt-out.
    """
    try:
        policy_path = _persistent_project_dir(root) / "policy.json"
        data = json.loads(policy_path.read_text())
        if isinstance(data, dict):
            return bool(data.get("learning_enabled", True))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return True


def project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project-specific state.

    This is the safe home for postmortems, improvements, and other
    project-specific data that should not live in the repo's .dynos/.
    """
    return _persistent_project_dir(root)


def is_pid_running(pid: int) -> bool:
    """Check whether a PID is alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def trajectories_store_path(root: Path) -> Path:
    """Path to the trajectories store JSON file."""
    return _persistent_project_dir(root) / "trajectories.json"


def learned_agents_root(root: Path) -> Path:
    """Root directory for learned agents."""
    return _persistent_project_dir(root) / "learned-agents"


def learned_registry_path(root: Path) -> Path:
    """Path to the learned agent registry JSON file."""
    return learned_agents_root(root) / "registry.json"


def benchmark_history_path(root: Path) -> Path:
    """Path to benchmark history JSON file."""
    return _persistent_project_dir(root) / "benchmarks" / "history.json"


def benchmark_index_path(root: Path) -> Path:
    """Path to benchmark index JSON file."""
    return _persistent_project_dir(root) / "benchmarks" / "index.json"


def automation_queue_path(root: Path) -> Path:
    """Path to the automation queue JSON file."""
    return root / ".dynos" / "automation" / "queue.json"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def project_policy(root: Path) -> dict:
    """Read project policy from persistent dir. Merges defaults without clobbering existing keys."""
    path = _persistent_project_dir(root) / "policy.json"
    default: dict[str, Any] = {
        "freshness_task_window": 5,
        "active_rebenchmark_task_window": 3,
        "shadow_rebenchmark_task_window": 2,
        "token_budget_multiplier": 1.0,
        "fast_track_skip_plan_audit": False,
    }
    data: dict = {}
    if path.exists() and path.read_text().strip():
        try:
            data = load_json(path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            data = {}
    # Merge: existing keys take precedence over defaults
    merged = {**default, **data}
    # Only write if file doesn't exist yet (never overwrite existing keys)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, merged)
    return merged


def benchmark_policy_config(root: Path) -> dict:
    """Legacy alias. Returns project policy."""
    return project_policy(root)


# ---------------------------------------------------------------------------
# Task state management
# ---------------------------------------------------------------------------

def get_tdd_required(manifest: dict, *, strict: bool = False) -> bool:
    """Return whether the task requires TDD per its classification.

    Reads ``manifest["classification"]["tdd_required"]``. Missing keys (and
    non-dict ``classification``) are treated as ``False`` WITHOUT mutating the
    manifest. Truthy non-bool values are coerced to bool.

    Strict mode (AC 2): when ``strict=True`` AND the manifest's
    ``classification.risk_level`` is in ``{"high", "critical"}`` AND the
    ``tdd_required`` key is absent (i.e. ``classification.get("tdd_required")
    is None``), this raises ``ValueError`` with a message containing the
    literal substring
    ``"tdd_required must be set for risk_level=<level> (strict mode)"``.
    Default behaviour (``strict=False``) is unchanged.
    """
    if not isinstance(manifest, dict):
        return False
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return False
    if strict:
        risk_level = classification.get("risk_level")
        if (
            risk_level in {"high", "critical"}
            and classification.get("tdd_required") is None
        ):
            raise ValueError(
                f"tdd_required must be set for risk_level={risk_level} (strict mode)"
            )
    return bool(classification.get("tdd_required", False))


def _check_audit_routing_receipt(
    task_dir: Path,
    registry_eligible: set[str],
) -> tuple[list[str], dict[str, dict]]:
    """Check (a) audit-routing presence + (b) registry-eligible cross-check
    + legacy spawn-without-registry check.

    Returns ``(gap_list, routing_by_name)``. The ``routing_by_name`` dict
    is the entry index keyed by auditor name; it flows downstream to
    ``_check_ensemble_voting`` so that helper does not have to re-read
    the receipt. Tuple-return is required for that downstream wiring.
    """
    from lib_receipts import read_receipt

    gaps: list[str] = []

    # (a) audit-routing must exist
    audit_routing = read_receipt(task_dir, "audit-routing")
    if audit_routing is None:
        gaps.append("audit-routing missing")
        routing_entries: list[dict] = []
    else:
        routing_raw = audit_routing.get("auditors")
        routing_entries = [e for e in routing_raw if isinstance(e, dict)] if isinstance(routing_raw, list) else []

    # Index routing by auditor name for cross-check.
    routing_by_name: dict[str, dict] = {}
    for entry in routing_entries:
        name = entry.get("name")
        if isinstance(name, str) and name:
            routing_by_name[name] = entry

    # (b) registry-eligible cross-check. Only runs when audit-routing exists.
    if audit_routing is not None:
        for name in sorted(registry_eligible):
            entry = routing_by_name.get(name)
            if entry is None:
                gaps.append(
                    f"audit-routing missing registry-eligible auditor: {name}"
                )
                continue
            action = entry.get("action")
            if action == "skip":
                reason = entry.get("reason")
                from lib_validate import require_nonblank  # noqa: PLC0415
                _reason_ok = True
                try:
                    require_nonblank(reason if isinstance(reason, str) else "", field_name="reason")
                except (TypeError, ValueError):
                    _reason_ok = False
                if not _reason_ok:
                    gaps.append(
                        f"auditor {name} marked skip without reason"
                    )
                continue
            if action == "spawn":
                if entry.get("ensemble") is True:
                    # Ensemble handled below — skip the single-receipt check here.
                    continue
                if read_receipt(task_dir, f"audit-{name}", min_version=2) is None:
                    gaps.append(f"audit-{name} missing")
                continue
            # Unknown action on an eligible entry → treat as gap.
            gaps.append(
                f"auditor {name} has unknown action={action!r} on registry-eligible entry"
            )

    # (b') legacy spawn-without-registry check for non-eligible entries.
    # Preserve existing behaviour for routing entries NOT in
    # registry_eligible: accept them but still require an audit receipt
    # for non-ensemble spawn entries (else a forged extra auditor name in
    # routing could soak up no-receipt). We do NOT emit a "missing
    # registry-eligible" gap for extras — they are accepted as extras.
    for name, entry in routing_by_name.items():
        if name in registry_eligible:
            continue  # handled above
        if entry.get("action") != "spawn":
            continue
        if entry.get("ensemble") is True:
            continue  # handled by ensemble block below
        if read_receipt(task_dir, f"audit-{name}", min_version=2) is None:
            gaps.append(f"audit-{name} missing")

    return gaps, routing_by_name


def _check_ensemble_voting(
    task_dir: Path,
    routing_by_name: dict[str, dict],
    registry_eligible: set[str],  # noqa: ARG001 — kept for API symmetry
) -> list[str]:
    """Check (c) ensemble voting enforcement (AC 7).

    Runs across ALL spawn entries flagged ``ensemble=true``, whether
    registry-eligible or not — the voting contract is identical.
    Delegates to ``lib_ensemble.evaluate_cascade`` so the DONE gate, the
    receipt writer, the prompt injector and ``ctl ensemble-next`` share one
    definition of "the cascade ran". A cascade satisfies the gate only when
    every voting tier it reached has a shard receipt in cascade order:
    fast tier always; balanced tier when the fast tier was clean; deep tier
    when any voting tier found something. An escalation receipt without the
    voting-tier receipts is a gap, not a pass.
    Returns the list of gap strings (possibly empty).
    """
    import lib_ensemble  # noqa: PLC0415

    entries = [
        dict(entry, name=name)
        for name, entry in routing_by_name.items()
        if isinstance(entry, dict)
    ]
    return lib_ensemble.cascade_gaps(task_dir, entries)

def _check_postmortem_receipts(task_dir: Path) -> list[str]:
    """Check (d) postmortem-generated/skipped presence and (e) the
    fail-CLOSED ``anomaly_count`` rule.

    Returns the list of gap strings (possibly empty).
    """
    from lib_receipts import read_receipt

    gaps: list[str] = []

    # (d) postmortem-generated OR postmortem-skipped required
    pm_generated = read_receipt(task_dir, "postmortem-generated")
    pm_skipped = read_receipt(task_dir, "postmortem-skipped")
    if pm_generated is None and pm_skipped is None:
        gaps.append("postmortem-generated or postmortem-skipped missing")

    # (e) AC 11 fail-CLOSED anomaly_count.
    if pm_generated is not None:
        anomaly_count_unknown = False
        if "anomaly_count" not in pm_generated:
            anomaly_count_unknown = True
            anomaly_count = -1
        else:
            raw = pm_generated.get("anomaly_count")
            try:
                anomaly_count = int(raw)
            except (TypeError, ValueError):
                anomaly_count_unknown = True
                anomaly_count = -1

        quality_score = 1.0
        retro_path = task_dir / "task-retrospective.json"
        if retro_path.exists():
            try:
                retro = load_json(retro_path)
                if isinstance(retro, dict):
                    raw_q = retro.get("quality_score", 1.0)
                    quality_score = float(raw_q) if isinstance(raw_q, (int, float)) else 1.0
            except (json.JSONDecodeError, OSError, ValueError):
                quality_score = 1.0

        # Fail-CLOSED: anomaly_count_unknown OR anomaly_count != 0 OR low quality
        # all require analysis/skipped receipt.
        needs_analysis = (
            anomaly_count_unknown
            or anomaly_count != 0
            or quality_score < 0.8
        )
        if needs_analysis:
            pm_analysis = read_receipt(task_dir, "postmortem-analysis")
            if pm_analysis is None and pm_skipped is None:
                gaps.append("postmortem-analysis or postmortem-skipped missing")

    return gaps


def require_receipts_for_done(task_dir: Path) -> list[str]:
    """Return list of receipt-gap strings preventing transition to DONE.

    Hard checks (in order):
      a) ``audit-routing`` receipt MUST be present.
      b) Re-derive the registry-eligible auditor set from
         ``_load_auditor_registry(root)`` + ``manifest.classification.domains``
         + ``manifest.fast_track`` (mirrors ``build_audit_plan`` logic):
           * Every registry-eligible auditor MUST appear in
             ``audit-routing.auditors``. Missing → gap error.
           * Registry-eligible entries with ``action: "skip"`` require a
             non-empty ``reason`` string; missing/empty reason → gap error.
           * Registry-eligible entries with ``action: "spawn"`` require a
             non-None ``read_receipt(task_dir, f"audit-{name}", min_version=2)``.
         Routing entries that are NOT in the registry-eligible set are
         accepted without error (treated as extras).
      c) Ensemble voting (AC 7): when a routing entry has
         ``ensemble: true``, look for per-model receipts
         ``audit-{name}-{model}`` at min_version=2 for every model in
         ``voting_models``. A collapsed ``audit-{name}`` receipt is not
         accepted for ensemble accounting.
         Accept iff EITHER every voting-model receipt reports
         ``finding_count == 0`` OR an escalation receipt exists whose
         ``model_used == escalation_model``. The cascade escalates to the
         deep tier on ANY finding (blocking or not), so the gate binds on
         ``finding_count``, not ``blocking_count``. Each found receipt's
         ``model_used`` MUST be in ``voting_models ∪ {escalation_model}``;
         otherwise a gap error.
      d) Either ``postmortem-generated`` OR ``postmortem-skipped`` MUST be
         present.
      e) When ``postmortem-generated`` is present AND
         (``anomaly_count != 0`` OR ``anomaly_count_unknown`` (coercion
         failed or key missing) OR ``task-retrospective.json``
         ``quality_score < 0.8``), THEN ``postmortem-analysis`` OR
         ``postmortem-skipped`` is required. Fail-CLOSED anomaly_count
         (AC 11): non-int / missing ``anomaly_count`` forces the
         analysis/skipped requirement.

    Returns an empty list when all gates pass.
    """
    # Derive registry-eligible auditor set (AC 6) from authoritative sources.
    manifest_path = task_dir / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = load_json(manifest_path)
        except (json.JSONDecodeError, OSError, ValueError):
            manifest = {}
    classification = manifest.get("classification") if isinstance(manifest, dict) else {}
    if not isinstance(classification, dict):
        classification = {}
    domains = classification.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    fast_track = bool(manifest.get("fast_track", False)) if isinstance(manifest, dict) else False

    try:
        # Deferred import: hooks.router imports from lib_core transitively.
        # Importing at call time keeps the module graph acyclic.
        from router import _load_auditor_registry, _DEFAULT_AUDITOR_REGISTRY  # type: ignore
    except Exception:
        _load_auditor_registry = None  # type: ignore
        _DEFAULT_AUDITOR_REGISTRY = {
            "always": [
                "spec-completion-auditor",
                "security-auditor",
                "claude-md-auditor",
                "architecture-auditor",
                "test-strategy-auditor",
                "docs-accuracy-auditor",
            ],
            "fast_track": ["spec-completion-auditor", "security-auditor"],
            "domain_conditional": {
                "ui": ["ui-auditor", "accessibility-auditor", "code-quality-auditor"],
                "db": ["db-schema-auditor", "data-integrity-auditor", "performance-auditor", "dead-code-auditor", "code-quality-auditor"],
                "backend": ["api-contract-auditor", "observability-auditor", "performance-auditor", "dead-code-auditor", "code-quality-auditor"],
                "ml": ["code-quality-auditor"],
                "testing": ["test-strategy-auditor", "code-quality-auditor"],
                "refactor": ["code-quality-auditor"],
                "infra": ["infrastructure-auditor", "supply-chain-auditor", "release-auditor", "code-quality-auditor"],
                "security": ["threat-model-auditor", "privacy-auditor", "code-quality-auditor"],
                "migration": ["release-auditor", "data-integrity-auditor"],
                "docs": ["docs-accuracy-auditor"],
            },
        }
    try:
        root = task_dir.parent.parent
        registry = _load_auditor_registry(root) if _load_auditor_registry else _DEFAULT_AUDITOR_REGISTRY
        if not isinstance(registry, dict):
            registry = _DEFAULT_AUDITOR_REGISTRY
    except Exception:
        registry = _DEFAULT_AUDITOR_REGISTRY

    if fast_track:
        eligible_list = list(registry.get("fast_track", _DEFAULT_AUDITOR_REGISTRY["fast_track"]))
    else:
        eligible_list = list(registry.get("always", _DEFAULT_AUDITOR_REGISTRY["always"]))
        domain_map = registry.get("domain_conditional", _DEFAULT_AUDITOR_REGISTRY["domain_conditional"])
        if not isinstance(domain_map, dict):
            domain_map = _DEFAULT_AUDITOR_REGISTRY["domain_conditional"]
        for domain in domains:
            if not isinstance(domain, str):
                continue
            for auditor in domain_map.get(domain, []) or []:
                if isinstance(auditor, str) and auditor and auditor not in eligible_list:
                    eligible_list.append(auditor)
    registry_eligible: set[str] = {a for a in eligible_list if isinstance(a, str) and a}

    # Run the three receipt-gap checks. Tuple-unpack the first call so
    # routing_by_name can flow into the ensemble check without a re-read
    # of audit-routing.
    routing_gaps, routing_by_name = _check_audit_routing_receipt(task_dir, registry_eligible)
    ensemble_gaps = _check_ensemble_voting(task_dir, routing_by_name, registry_eligible)
    postmortem_gaps = _check_postmortem_receipts(task_dir)

    return routing_gaps + ensemble_gaps + postmortem_gaps


def _human_approval_err(
    task_dir: Path,
    current_stage: str | None,
    next_stage: str,
    stage_label: str,
    artifact_path: Path,
) -> str | None:
    """Mirror of ``_check_any_approval`` (closure in
    ``_replay_gates_for_transition``); both paths must accept either
    human-approval-{stage} or auto-approval-{stage} receipts. Drift
    between this function and the closure causes
    ``_compute_bypassed_gates_for_force`` to misreport auto-approved
    tasks.

    Returns an error string on failure rather than raising. Promoted
    from a closure in ``_compute_bypassed_gates_for_force`` so the
    captured variables (``task_dir``, ``current_stage``,
    ``next_stage``) become explicit parameters. Never raises: any
    internal exception → return None (treat as no-error so the forced
    transition still proceeds).

    Behavioural mirror of ``_check_any_approval``:
      1. Try human-approval-{stage_label} first; if the receipt exists,
         it is the chosen receipt for the gate.
      2. Otherwise try auto-approval-{stage_label}; if it exists, it is
         the chosen receipt.
      3. Otherwise return a "missing approval receipt" error string
         that names BOTH possibilities so the caller knows either
         receipt would have satisfied the gate.
      4. With a chosen receipt, validate the live artifact's hash
         against the receipt's ``artifact_sha256``; on mismatch return
         a "hash mismatch" error string naming the chosen step.
    """
    try:
        from lib_receipts import read_receipt, hash_file, _receipts_dir
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        return f"Cannot transition {current_stage} -> {next_stage}: validator_failed reason={exc!r}"
    try:
        human_step = f"human-approval-{stage_label}"
        auto_step = f"auto-approval-{stage_label}"
        receipts_dir = _receipts_dir(task_dir)
        receipt_path_human = receipts_dir / f"{human_step}.json"
        receipt_path_auto = receipts_dir / f"{auto_step}.json"

        human_receipt = read_receipt(task_dir, human_step)
        auto_receipt = (
            read_receipt(task_dir, auto_step) if human_receipt is None else None
        )

        if human_receipt is not None:
            chosen_step = human_step
            chosen_path = receipt_path_human
            chosen_receipt = human_receipt
        elif auto_receipt is not None:
            chosen_step = auto_step
            chosen_path = receipt_path_auto
            chosen_receipt = auto_receipt
        else:
            return (
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing approval receipt — expected either "
                f"{human_step} at {receipt_path_human} or "
                f"{auto_step} at {receipt_path_auto} "
                f"(artifact: {artifact_path})"
            )

        if not artifact_path.exists():
            return (
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"receipt {chosen_step} present at {chosen_path} but "
                f"artifact missing at {artifact_path}"
            )
        expected = chosen_receipt.get("artifact_sha256") or ""
        actual = hash_file(artifact_path)
        if not isinstance(expected, str) or expected != actual:
            return (
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"hash mismatch for receipt {chosen_step} "
                f"(receipt: {chosen_path}, artifact: {artifact_path}) "
                f"expected={(expected or '')[:12]} actual={actual[:12]}"
            )
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        return f"Cannot transition {current_stage} -> {next_stage}: validator_failed reason={exc!r}"
    return None


def _rules_check_err(
    task_dir: Path,
    current_stage: str | None,
    next_stage: str,
) -> str | None:
    """Mirror of ``_check_rules_check_passed`` — returns an error
    string when the ``rules-check-passed`` receipt is missing or its
    ``error_violations`` is non-zero. Promoted from a closure in
    ``_compute_bypassed_gates_for_force`` so the captured variables
    become explicit parameters. Never raises.
    """
    try:
        from lib_receipts import read_receipt
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        return f"Cannot transition {current_stage} -> {next_stage}: validator_failed reason={exc!r}"
    try:
        receipt = read_receipt(task_dir, "rules-check-passed")
        receipt_path = task_dir / "receipts" / "rules-check-passed.json"
        if receipt is None:
            return (
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing or failed rules-check-passed receipt at "
                f"{receipt_path} (error_violations=missing)"
            )
        if receipt.get("error_violations", 1) != 0:
            n = receipt.get("error_violations", "missing")
            return (
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing or failed rules-check-passed receipt at "
                f"{receipt_path} (error_violations={n})"
            )
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        return f"Cannot transition {current_stage} -> {next_stage}: validator_failed reason={exc!r}"
    return None


def _compute_bypassed_gates_for_force(
    *,
    task_dir: Path,
    manifest: dict,
    current_stage: str | None,
    next_stage: str,
) -> list[str]:
    """Return the list of gate error strings that WOULD have been raised
    if ``transition_task(..., force=False)`` had been called for this
    edge. Used for the force_override observability receipt and event.

    This MUST mirror the semantics of the ``if not force:`` gate block in
    ``transition_task``. We duplicate the checks rather than share a
    common helper because the gate block today calls ``_refuse()``
    (raises immediately) for several checks; threading a dry-run flag
    through every call site risks silently changing refusal semantics.

    Returns a possibly-empty list[str]. Never raises — all internal
    failures (missing files, unreadable receipts, etc.) are swallowed so
    the forced transition still proceeds.
    """
    try:
        from lib_receipts import (
            read_receipt,
            _receipts_dir,
            plan_validated_receipt_matches,
            plan_audit_matches,
        )
    except Exception:
        return []

    errs: list[str] = []

    # ---- AC 3: SPEC_REVIEW -> PLANNING
    if current_stage == "SPEC_REVIEW" and next_stage == "PLANNING":
        err = _human_approval_err(task_dir, current_stage, next_stage, "SPEC_REVIEW", task_dir / "spec.md")
        if err:
            errs.append(err)

    # ---- AC 4: PLAN_REVIEW -> PLAN_AUDIT
    if current_stage == "PLAN_REVIEW" and next_stage == "PLAN_AUDIT":
        err = _human_approval_err(task_dir, current_stage, next_stage, "PLAN_REVIEW", task_dir / "plan.md")
        if err:
            errs.append(err)

    # ---- F6: planner-spec at SPEC_NORMALIZATION -> SPEC_REVIEW.
    classification = manifest.get("classification") or {}
    fast_track_flag = bool(
        manifest.get("fast_track", False)
        or (
            isinstance(classification, dict)
            and classification.get("fast_track", False)
        )
    )
    if (
        current_stage == "SPEC_NORMALIZATION"
        and next_stage == "SPEC_REVIEW"
        and not fast_track_flag
    ):
        if read_receipt(task_dir, "planner-spec") is None:
            errs.append(
                "receipt: planner-spec (planner spec spawn was never recorded)"
            )

    # ---- F6: planner-plan at PLANNING -> PLAN_REVIEW.
    if current_stage == "PLANNING" and next_stage == "PLAN_REVIEW":
        if read_receipt(task_dir, "planner-plan") is None:
            errs.append(
                "receipt: planner-plan (planner plan spawn was never recorded)"
            )

    # ---- AC 6: TDD_REVIEW -> PRE_EXECUTION_SNAPSHOT
    if current_stage == "TDD_REVIEW" and next_stage == "PRE_EXECUTION_SNAPSHOT":
        err = _human_approval_err(
            task_dir, current_stage, next_stage,
            "TDD_REVIEW", task_dir / "evidence" / "tdd-tests.md"
        )
        if err:
            errs.append(err)

    # ---- PLAN_AUDIT exit gates (F4 + tdd_required)
    if current_stage == "PLAN_AUDIT" and next_stage in {"TDD_REVIEW", "PRE_EXECUTION_SNAPSHOT"}:
        risk_level = classification.get("risk_level") if isinstance(classification, dict) else None
        if risk_level in {"high", "critical"}:
            pa_path = _receipts_dir(task_dir) / "plan-audit-check.json"
            audit_result = plan_audit_matches(task_dir)
            if audit_result is True:
                pass
            elif isinstance(audit_result, str):
                errs.append(f"plan-audit-check: {audit_result}")
            else:
                errs.append(
                    f"Cannot transition {current_stage} -> {next_stage}: "
                    f"missing receipt plan-audit-check at {pa_path} "
                    f"(risk_level={risk_level} requires plan-audit-check)"
                )
        if next_stage == "PRE_EXECUTION_SNAPSHOT" and get_tdd_required(manifest):
            errs.append(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"manifest.classification.tdd_required=true requires "
                f"routing through TDD_REVIEW (manifest: {task_dir / 'manifest.json'})"
            )

    # ---- F1: EXECUTION requires fresh plan-validated receipt.
    if next_stage == "EXECUTION":
        match_result = plan_validated_receipt_matches(task_dir)
        if match_result is True:
            pass
        elif isinstance(match_result, str):
            errs.append(f"plan-validated: {match_result}")
        else:
            errs.append("receipt: plan-validated (plan was never validated)")

    # ---- CHECKPOINT_AUDIT gate — mirror the graph+routing segment check.
    if next_stage == "CHECKPOINT_AUDIT":
        routing_payload = read_receipt(task_dir, "executor-routing")
        if routing_payload is None:
            errs.append("receipt: executor-routing (executor routing was never recorded)")
        elif current_stage in {"TEST_EXECUTION", "REPAIR_EXECUTION"}:
            required_seg_ids: list[str] = []
            seen: set[str] = set()

            # CQ-001: helper name matches the live gate's `_add_seg` in
            # transition_task so drift between dry-run and live is
            # mechanically obvious on review.
            def _add_seg(seg_id: object) -> None:
                if isinstance(seg_id, str) and seg_id and seg_id not in seen:
                    required_seg_ids.append(seg_id)
                    seen.add(seg_id)

            graph_path = task_dir / "execution-graph.json"
            if graph_path.exists():
                try:
                    graph = load_json(graph_path)
                except (OSError, ValueError):
                    graph = None
                if isinstance(graph, dict):
                    graph_segments = graph.get("segments")
                    if isinstance(graph_segments, list):
                        for entry in graph_segments:
                            if isinstance(entry, dict):
                                _add_seg(entry.get("id"))
            routing_segments = routing_payload.get("segments")
            if isinstance(routing_segments, list):
                for entry in routing_segments:
                    if isinstance(entry, dict):
                        _add_seg(entry.get("segment_id"))
            for seg_id in required_seg_ids:
                if read_receipt(task_dir, f"executor-{seg_id}") is None:
                    errs.append(
                        f"receipt: executor-{seg_id} (segment {seg_id} never completed)"
                    )

    # ---- REPAIR cap exhaustion.
    if next_stage == "REPAIR_PLANNING" and current_stage == "REPAIR_EXECUTION":
        repair_log_path = task_dir / "repair-log.json"
        if repair_log_path.exists():
            try:
                repair_log = load_json(repair_log_path)
            except (OSError, ValueError):
                repair_log = None
            if isinstance(repair_log, dict):
                exhausted: list[str] = []
                for batch in repair_log.get("batches", []):
                    if not isinstance(batch, dict):
                        continue
                    for task_entry in batch.get("tasks", []):
                        if not isinstance(task_entry, dict):
                            continue
                        fid = task_entry.get("finding_id", "unknown")
                        retries = task_entry.get("retry_count", 0)
                        try:
                            retries_int = int(retries)
                        except (TypeError, ValueError):
                            retries_int = 0
                        if retries_int >= 3:
                            exhausted.append(f"{fid} (retry_count={retries_int})")
                if exhausted:
                    errs.append(
                        f"repair cap exceeded (max 3 retries) for finding(s): "
                        f"{', '.join(exhausted)}. Transition to FAILED instead — "
                        f"human review needed."
                    )

    # ---- DONE gates.
    if next_stage == "DONE":
        if not (task_dir / "task-retrospective.json").exists():
            errs.append("task-retrospective.json (run /dynos-work:audit to generate)")
        if not list(task_dir.glob("audit-reports/*.json")):
            errs.append("audit-reports/ (no audit reports found — audit was never run)")
        if read_receipt(task_dir, "retrospective") is None:
            errs.append("receipt: retrospective (reward was never computed via receipts)")
        if current_stage in {"CHECKPOINT_AUDIT", "FINAL_AUDIT"}:
            try:
                done_gaps = require_receipts_for_done(task_dir)
            except Exception:
                done_gaps = []
            if done_gaps:
                rd = _receipts_dir(task_dir)
                errs.append(
                    f"Cannot transition {current_stage} -> {next_stage}: "
                    f"receipt gaps in {rd}: " + "; ".join(done_gaps)
                )

    # ---- DONE -> CALIBRATED requires calibration-applied.
    if current_stage == "DONE" and next_stage == "CALIBRATED":
        ca_path = _receipts_dir(task_dir) / "calibration-applied.json"
        if read_receipt(task_dir, "calibration-applied") is None:
            errs.append(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing receipt calibration-applied at {ca_path}"
            )

    # ---- rules-check-passed at TEST_EXECUTION entry and CHECKPOINT_AUDIT -> DONE.
    if next_stage == "TEST_EXECUTION":
        err = _rules_check_err(task_dir, current_stage, next_stage)
        if err:
            errs.append(err)
    if current_stage == "CHECKPOINT_AUDIT" and next_stage == "DONE":
        err = _rules_check_err(task_dir, current_stage, next_stage)
        if err:
            errs.append(err)

    # ---- Illegal transition itself is a bypassed "gate" under force=True.
    if next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage or "", set()):
        errs.append(f"Illegal stage transition: {current_stage} -> {next_stage}")

    return errs


def _flush_retrospective_on_done(*, task_dir: Path, manifest: dict) -> None:
    """Copy task-retrospective.json into the persistent project dir so
    the nightly calibration pipeline can find it after worktree removal.

    Invariant: this function NEVER raises. Emits
    ``retrospective_flushed`` on success, ``retrospective_flush_failed``
    on OSError. A missing source file is a silent skip (the DONE gate
    already requires its presence; callers rely on the gate's error
    path, not on this function's side effects).
    """
    src = task_dir / "task-retrospective.json"
    if not src.exists():
        return
    root = task_dir.parent.parent
    task_id = manifest.get("task_id") or task_dir.name

    # CQ-002: single helper for the three identical failure-emit blocks
    # below. Invariant: NEVER raises — best-effort log event with a
    # swallow-all outer try/except.
    def _log_flush_failed(destination: str, error: str) -> None:
        try:
            from lib_log import log_event as _log_flush
            _log_flush(
                root,
                "retrospective_flush_failed",
                task=str(task_id),
                task_id=str(task_id),
                source=str(src),
                destination=destination,
                error=error,
            )
        except Exception:
            pass

    # SEC-001 hardening: validate task_id as a safe slug BEFORE path join.
    # A crafted manifest with "task_id": "../../evil" would otherwise escape
    # the persistent retrospectives dir. Accepts current dynos format
    # (task-YYYYMMDD-NNN) plus test-style ids (task-T, task-A).
    import re as _re_slug
    if not isinstance(task_id, str) or not _re_slug.match(
        r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$", task_id
    ):
        _log_flush_failed(destination="", error=f"invalid task_id slug: {task_id!r}")
        return
    try:
        dst_dir = _persistent_project_dir(root) / "retrospectives"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{task_id}.json"
        # Defense-in-depth: assert resolved dst is under dst_dir.
        try:
            dst.resolve().relative_to(dst_dir.resolve())
        except ValueError:
            raise OSError(f"resolved dst escapes retrospectives dir: {dst}")
    except OSError as exc:
        # Can't even compute or create the destination dir — log and bail.
        _log_flush_failed(destination="", error=str(exc))
        return
    try:
        from lib_receipts import _atomic_write_text
        _atomic_write_text(dst, src.read_text("utf-8"))
    except OSError as exc:
        _log_flush_failed(destination=str(dst), error=str(exc))
        return
    except Exception as exc:
        # Non-OSError (unexpected) — still must not block DONE.
        _log_flush_failed(destination=str(dst), error=f"unexpected: {exc}")
        return
    # Success path — hash the newly-written destination for the event.
    try:
        from lib_receipts import hash_file
        dst_hash = hash_file(dst)
    except Exception:
        dst_hash = ""
    try:
        from lib_log import log_event as _log_flush
        # NOTE: do NOT pass task=task_id here. log_event routes events with a
        # `task` arg to the TASK-SCOPED .dynos/{task}/events.jsonl, but the
        # SEC-003 verifier `_flushed_sha_by_task_id` reads only the GLOBAL
        # .dynos/events.jsonl. Routing this event task-scoped made every
        # persistent retrospective `persistent-unverified`, so
        # collect_retrospectives() filtered them all out and calibration's
        # generation gate could never see them. The task_id is still recorded
        # in the payload below for the verifier's {task_id: sha256} map.
        _log_flush(
            root,
            "retrospective_flushed",
            task_id=task_id,
            source=str(src),
            destination=str(dst),
            sha256=dst_hash,
        )
    except Exception:
        pass


def _write_pre_repair_blocking_snapshot(task_dir: Path) -> Path | None:
    """Write the pre-repair blocking finding set to
    ``task_dir / "repair" / "pre-repair-blocking.json"``.

    Scans ``task_dir / "audit-reports" / "*.json"`` and extracts the set
    of ``finding.id`` values where ``blocking=True``. The set is written
    as a JSON array (sorted for determinism) via an atomic
    tempfile+os.replace write.

    Idempotent: when the target file already exists, this function
    returns without writing — the first repair cycle's snapshot is
    preserved as ground truth. Later repair cycles must NOT overwrite it
    because compute_reward compares post-repair state against the
    original pre-repair set to compute surviving_blocking.
    """
    target = task_dir / "repair" / "pre-repair-blocking.json"
    if target.exists():
        return None

    audit_dir = task_dir / "audit-reports"
    blocking_ids: list[str] = []
    seen: set[str] = set()
    if audit_dir.is_dir():
        for report_path in sorted(audit_dir.glob("*.json")):
            try:
                payload = load_json(report_path)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            findings = payload.get("findings") or []
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                if finding.get("blocking") is not True:
                    continue
                fid = finding.get("id")
                if isinstance(fid, str) and fid and fid not in seen:
                    seen.add(fid)
                    blocking_ids.append(fid)

    blocking_ids.sort()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tempfile in same dir then os.replace.
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pre-repair-blocking.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(blocking_ids, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _validate_force_justification(
    force: bool,
    force_reason: str | None,
    force_approver: str | None,
) -> None:
    """Validate ``force_reason`` / ``force_approver`` when ``force=True``.

    Raises ``ValueError`` whose message names the FIRST offending kwarg —
    reason is checked before approver so the error surfaces the
    earlier-missing arg. ``None`` / empty / non-string / whitespace-only
    values are all rejected. When ``force=False`` this is a no-op (the
    justification kwargs are accepted for call-site uniformity but
    ignored).

    Validation MUST run BEFORE any gate-replay or receipt-write work so
    a malformed break-glass call cannot ever reach the receipt writer.
    """
    if not force:
        return
    from lib_validate import require_nonblank  # noqa: PLC0415 (local import avoids circular dep)
    try:
        require_nonblank(force_reason if isinstance(force_reason, str) else "", field_name="force_reason")
    except (TypeError, ValueError):
        raise ValueError(
            "force_reason must be a non-empty string when force=True "
            "(whitespace-only values are rejected — whitespace carries no "
            "human-readable justification)"
        )
    try:
        require_nonblank(force_approver if isinstance(force_approver, str) else "", field_name="force_approver")
    except (TypeError, ValueError):
        raise ValueError(
            "force_approver must be a non-empty string when force=True "
            "(whitespace-only values are rejected)"
        )


def _backfill_tdd_required(
    task_dir: Path,
    manifest: dict,
    manifest_path: Path,
) -> dict:
    """Backfill ``classification.tdd_required`` for any post-classify
    manifest that has a classification but no ``tdd_required`` field.

    Runs OUTSIDE the ``if not force:`` block in ``transition_task`` so
    even forced transitions get the backfill — the fail-closed gates
    (and downstream code) require the field to be populated by the time
    they read the manifest.

    Best-effort: any exception → emit ``tdd_required_backfill_failed``
    event and return the original (un-backfilled) manifest. Returns the
    manifest dict (possibly re-read after a successful backfill).
    """
    try:
        _classification = manifest.get("classification") if isinstance(manifest, dict) else None
        _stage = manifest.get("stage") if isinstance(manifest, dict) else None
        _SKIP_BACKFILL_STAGES = {"FOUNDRY_INITIALIZED", "CLASSIFY_AND_SPEC"}
        if (
            isinstance(_classification, dict)
            and _classification.get("tdd_required") is None
            and _stage not in _SKIP_BACKFILL_STAGES
        ):
            from lib_validate import apply_fast_track  # deferred import; lib_validate imports from lib_core
            apply_fast_track(task_dir)
            # Re-read manifest so subsequent gate code sees the backfilled field.
            return load_json(manifest_path)
    except Exception as _backfill_exc:
        try:
            from lib_log import log_event as _log_backfill
            _log_backfill(
                task_dir.parent.parent,
                "tdd_required_backfill_failed",
                task=task_dir.name,
                error=str(_backfill_exc),
            )
        except Exception:
            pass  # Logging failure must never block the transition.
    return manifest


def _replay_gates_for_transition(
    task_dir: Path,
    manifest: dict,
    current_stage: str | None,
    next_stage: str,
    force: bool,
) -> None:
    """Replay the receipt-based transition gates that fire on the
    ``force=False`` path.

    When ``force=True`` this is a no-op (gates are bypassed; observability
    is handled by ``_dispatch_force_override_observability``).
    When ``force=False`` this raises ``ValueError`` (via the nested
    ``_refuse`` closure) on the first hard refusal, or aggregates
    ``gate_errors`` and raises one combined ``ValueError`` at the end.

    The nested closures (``_refuse``, ``_check_any_approval``,
    ``_check_tdd_tests``, ``_check_rules_check_passed``) intentionally
    stay nested — they share ``task_dir``, ``current_stage``,
    ``next_stage``, ``manifest_path`` via closure, and ``_refuse`` calls
    ``_log_event`` with bound ``_root``. Promoting them would require
    threading those arguments through every call site and risk silently
    changing refusal semantics.
    """
    if force:
        return

    manifest_path = task_dir / "manifest.json"

    from lib_receipts import (
        read_receipt,
        hash_file,
        plan_validated_receipt_matches,
        plan_audit_matches,
    )
    from lib_log import log_event as _log_event
    gate_errors: list[str] = []
    _root = task_dir.parent.parent

    def _refuse(reason: str) -> "None":
        """Emit gate_refused event then raise ValueError(reason)."""
        try:
            _log_event(
                _root,
                "gate_refused",
                task=task_dir.name,
                stage_from=current_stage,
                stage_to=next_stage,
                reason=reason,
            )
        except Exception:
            pass  # Logging must never block the refusal itself.
        raise ValueError(reason)

    def _check_any_approval(stage_label: str, artifact_path: Path) -> None:
        """Refuse the current transition unless an approval receipt for
        ``stage_label`` exists and its ``artifact_sha256`` matches the
        current hash of ``artifact_path``.

        Accepts EITHER ``human-approval-{stage_label}`` OR
        ``auto-approval-{stage_label}``. Human-approval is checked
        first; if absent, auto-approval is checked. When neither is
        found the refusal message names BOTH possibilities so the
        operator knows either receipt would have satisfied the gate.
        Hash-mismatch refusal applies identically to both stems
        (2026-04-30 forgery hardening invariant).

        Mirrored by ``_human_approval_err`` (in this module) for the
        force-override observability path; drift between this closure
        and that helper causes ``_compute_bypassed_gates_for_force`` to
        misreport auto-approved tasks as having human-bypassed gates.
        """
        from lib_receipts import _receipts_dir  # type: ignore

        human_step = f"human-approval-{stage_label}"
        auto_step = f"auto-approval-{stage_label}"
        receipts_dir = _receipts_dir(task_dir)
        receipt_path_human = receipts_dir / f"{human_step}.json"
        receipt_path_auto = receipts_dir / f"{auto_step}.json"

        human_receipt = read_receipt(task_dir, human_step)
        auto_receipt = (
            read_receipt(task_dir, auto_step) if human_receipt is None else None
        )

        if human_receipt is not None:
            chosen_step = human_step
            chosen_path = receipt_path_human
            chosen_receipt = human_receipt
        elif auto_receipt is not None:
            chosen_step = auto_step
            chosen_path = receipt_path_auto
            chosen_receipt = auto_receipt
        else:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing approval receipt — expected either "
                f"{human_step} at {receipt_path_human} or "
                f"{auto_step} at {receipt_path_auto} "
                f"(artifact: {artifact_path})"
            )
            return  # _refuse raises; this satisfies type-narrowing

        if not artifact_path.exists():
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"receipt {chosen_step} present at {chosen_path} but "
                f"artifact missing at {artifact_path}"
            )
        expected = chosen_receipt.get("artifact_sha256") or ""
        actual = hash_file(artifact_path)
        if not isinstance(expected, str) or expected != actual:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"hash mismatch for receipt {chosen_step} "
                f"(receipt: {chosen_path}, artifact: {artifact_path}) "
                f"expected={(expected or '')[:12]} actual={actual[:12]}"
            )

    def _check_tdd_tests(
        task_dir: Path, current_stage: str, next_stage: str
    ) -> None:
        """Refuse the current transition unless — when the manifest says
        ``classification.tdd_required == True`` — a ``tdd-tests`` receipt
        exists at ``min_version=2`` AND its
        ``tests_evidence_sha256`` matches the live sha256 of
        ``evidence/tdd-tests.md``.

        When ``tdd_required != True`` this check returns without
        refusing (gate is inert). Mirrors the ``_check_human_approval``
        pattern: missing receipt names the receipt path; hash drift
        emits a message containing the literal substrings
        ``tdd-tests`` and ``hash mismatch`` plus the 12-char prefixes
        of expected / actual digests.
        """
        from lib_receipts import read_receipt, hash_file, _receipts_dir  # type: ignore

        # Re-read manifest inline so downstream upstream edits to the
        # classification dict are visible to this gate without leaking
        # state through the outer closure.
        try:
            mf = load_json(task_dir / "manifest.json")
        except (json.JSONDecodeError, OSError, ValueError):
            mf = {}
        cls = mf.get("classification") if isinstance(mf, dict) else None
        if not isinstance(cls, dict):
            return
        if cls.get("tdd_required") is not True:
            return

        receipt_path = _receipts_dir(task_dir) / "tdd-tests.json"
        receipt = read_receipt(task_dir, "tdd-tests", min_version=2)
        if receipt is None:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing receipt tdd-tests at {receipt_path} "
                f"(classification.tdd_required=true requires tdd-tests receipt)"
            )
        evidence_path = task_dir / "evidence" / "tdd-tests.md"
        if not evidence_path.exists():
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"receipt tdd-tests present at {receipt_path} but "
                f"evidence missing at {evidence_path}"
            )
        expected = receipt.get("tests_evidence_sha256") or ""
        try:
            actual = hash_file(evidence_path)
        except (FileNotFoundError, OSError) as exc:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"receipt tdd-tests hash mismatch (unable to hash "
                f"{evidence_path}: {exc})"
            )
            return
        if not isinstance(expected, str) or expected != actual:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"tdd-tests hash mismatch "
                f"(receipt: {receipt_path}, evidence: {evidence_path}) "
                f"expected={(expected or '')[:12]} actual={actual[:12]}"
            )

    def _check_rules_check_passed(
        task_dir: Path, current_stage: str, next_stage: str
    ) -> None:
        """Refuse the current transition unless a ``rules-check-passed``
        receipt exists and reports ``error_violations == 0``.

        Fail-closed semantics: when the receipt is missing entirely the
        count is reported as ``"missing"`` in the refusal message, and
        when the ``error_violations`` key is absent from the receipt the
        default value used is ``1`` so a malformed receipt still refuses
        the transition.
        """
        from lib_receipts import read_receipt, hash_file as _hash_file  # noqa: F811 — lazy re-import per spec

        receipt_path = task_dir / "receipts" / "rules-check-passed.json"
        receipt = read_receipt(task_dir, "rules-check-passed")
        if receipt is None:
            n = "missing"
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing or failed rules-check-passed receipt at "
                f"{receipt_path} (error_violations={n})"
            )
        if receipt.get("error_violations", 1) != 0:
            n = receipt.get("error_violations", "missing")
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing or failed rules-check-passed receipt at "
                f"{receipt_path} (error_violations={n})"
            )

        # AC 16: live-hash drift check. After confirming error_violations
        # == 0 above, recompute the live sha256 of prevention-rules.json
        # and compare against the receipt-recorded hash. A mismatch means
        # the rules file has changed since the receipt was written; the
        # receipt is stale and rules-check must be re-run.
        try:
            live_rules_hash = _hash_file(_persistent_project_dir(task_dir.parent.parent) / "prevention-rules.json")
        except FileNotFoundError:
            live_rules_hash = "none"
        receipt_rules_hash = receipt.get("rules_file_sha256", "none")
        if not isinstance(receipt_rules_hash, str) or not receipt_rules_hash:
            receipt_rules_hash = "none"
        if receipt_rules_hash != live_rules_hash:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"rules_file drift "
                f"(receipt={receipt_rules_hash[:12]}, live={live_rules_hash[:12]}); "
                f"rerun rules-check"
            )

    # ---- AC 9: CLASSIFY_AND_SPEC -> SPEC_NORMALIZATION requires
    # classification.tdd_required to be explicitly set (True or False)
    # when risk_level ∈ {"high", "critical"}. Absent tdd_required on a
    # high-risk task is refused so the operator cannot accidentally
    # skip the TDD decision. Low/medium risk: tdd_required is optional.
    if current_stage == "CLASSIFY_AND_SPEC" and next_stage == "SPEC_NORMALIZATION":
        classification = manifest.get("classification") or {}
        if not isinstance(classification, dict):
            classification = {}
        risk_level = classification.get("risk_level")
        if risk_level in {"high", "critical"} and classification.get("tdd_required") is None:
            _refuse(
                f"CLASSIFY_AND_SPEC -> SPEC_NORMALIZATION refused: "
                f"classification.tdd_required must be set (True or False) "
                f"for risk_level={risk_level}; manifest={manifest_path}"
            )

    # ---- AC 3: SPEC_REVIEW -> PLANNING requires either
    # human-approval-SPEC_REVIEW or auto-approval-SPEC_REVIEW whose
    # artifact_sha256 matches sha256(spec.md).
    if current_stage == "SPEC_REVIEW" and next_stage == "PLANNING":
        _check_any_approval("SPEC_REVIEW", task_dir / "spec.md")

    # ---- AC 4: PLAN_REVIEW -> PLAN_AUDIT requires either
    # human-approval-PLAN_REVIEW or auto-approval-PLAN_REVIEW whose
    # artifact_sha256 matches sha256(plan.md).
    if current_stage == "PLAN_REVIEW" and next_stage == "PLAN_AUDIT":
        _check_any_approval("PLAN_REVIEW", task_dir / "plan.md")

    # ---- F6: planner-spec receipt required at SPEC_NORMALIZATION ->
    # SPEC_REVIEW (skipped when manifest.fast_track is True — fast-track
    # writes only a planner-plan receipt for the combined Spec+Plan
    # spawn). Fast-track detection mirrors hooks/planner.py:
    # manifest.get("fast_track", False), with a fallback through
    # classification["fast_track"] for any older manifests that stored
    # the flag under the classification subtree.
    _classification = manifest.get("classification") or {}
    _fast_track_flag = bool(
        manifest.get("fast_track", False)
        or (
            isinstance(_classification, dict)
            and _classification.get("fast_track", False)
        )
    )
    if (
        current_stage == "SPEC_NORMALIZATION"
        and next_stage == "SPEC_REVIEW"
        and not _fast_track_flag
    ):
        if read_receipt(task_dir, "planner-spec") is None:
            gate_errors.append(
                "receipt: planner-spec (planner spec spawn was never recorded)"
            )

    # ---- F6: planner-plan receipt required at PLANNING -> PLAN_REVIEW.
    # Applies to both normal and fast-track paths (fast-track writes a
    # planner-plan receipt for the combined Spec+Plan spawn).
    if current_stage == "PLANNING" and next_stage == "PLAN_REVIEW":
        if read_receipt(task_dir, "planner-plan") is None:
            gate_errors.append(
                "receipt: planner-plan (planner plan spawn was never recorded)"
            )

    # ---- AC 6: TDD_REVIEW -> PRE_EXECUTION_SNAPSHOT requires either
    # human-approval-TDD_REVIEW or auto-approval-TDD_REVIEW whose
    # artifact_sha256 matches sha256(evidence/tdd-tests.md).
    if current_stage == "TDD_REVIEW" and next_stage == "PRE_EXECUTION_SNAPSHOT":
        _check_any_approval("TDD_REVIEW", task_dir / "evidence" / "tdd-tests.md")

    # ---- AC 11 + tdd_required gate: EXIT from PLAN_AUDIT.
    # (a) high/critical risk tasks MUST have a plan-audit-check receipt
    #     before leaving PLAN_AUDIT towards either TDD_REVIEW or
    #     PRE_EXECUTION_SNAPSHOT.
    # (b) When tdd_required=true, PLAN_AUDIT MUST route through TDD_REVIEW
    #     — direct PLAN_AUDIT -> PRE_EXECUTION_SNAPSHOT is refused.
    if current_stage == "PLAN_AUDIT" and next_stage in {"TDD_REVIEW", "PRE_EXECUTION_SNAPSHOT"}:
        classification = manifest.get("classification") or {}
        risk_level = classification.get("risk_level") if isinstance(classification, dict) else None
        if risk_level in {"high", "critical"}:
            from lib_receipts import _receipts_dir  # type: ignore
            pa_path = _receipts_dir(task_dir) / "plan-audit-check.json"
            # Hash-bound freshness: the presence of a plan-audit-check
            # receipt is necessary but no longer sufficient. The audit
            # must have been computed over the current spec.md /
            # plan.md / execution-graph.json. plan_audit_matches
            # returns True (fresh), str (drift reason), or False
            # (missing/legacy).
            audit_result = plan_audit_matches(task_dir)
            if audit_result is True:
                pass  # fresh audit; gate passes.
            elif isinstance(audit_result, str):
                gate_errors.append(f"plan-audit-check: {audit_result}")
            else:
                # Preserve the existing missing-receipt refuse path
                # (raises immediately via _refuse).
                _refuse(
                    f"Cannot transition {current_stage} -> {next_stage}: "
                    f"missing receipt plan-audit-check at {pa_path} "
                    f"(risk_level={risk_level} requires plan-audit-check)"
                )
        if next_stage == "PRE_EXECUTION_SNAPSHOT" and get_tdd_required(manifest):
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"manifest.classification.tdd_required=true requires "
                f"routing through TDD_REVIEW (manifest: {task_dir / 'manifest.json'})"
            )

    # AC 8 (task-006): PRE_EXECUTION_SNAPSHOT -> EXECUTION requires a
    # tdd-tests receipt whose tests_evidence_sha256 matches the current
    # evidence/tdd-tests.md, but ONLY when
    # classification.tdd_required == true. Inert otherwise.
    if current_stage == "PRE_EXECUTION_SNAPSHOT" and next_stage == "EXECUTION":
        _check_tdd_tests(task_dir, current_stage, next_stage)

    # EXECUTION requires plan-validated receipt AND the captured artifact
    # hashes MUST match current disk. Presence alone is insufficient —
    # a stale receipt whose plan.md/spec.md/execution-graph.json have
    # drifted since validation cannot authorize the transition.
    # plan_validated_receipt_matches returns:
    #   True         -> all artifacts unchanged; gate passes.
    #   str (reason) -> a tracked artifact drifted (e.g. "plan.md hash
    #                   drift"); append prefixed error.
    #   False        -> receipt missing or malformed; legacy message.
    if next_stage == "EXECUTION":
        match_result = plan_validated_receipt_matches(task_dir)
        if match_result is True:
            pass  # fresh receipt; gate passes.
        elif isinstance(match_result, str):
            gate_errors.append(f"plan-validated: {match_result}")
        else:
            gate_errors.append(
                "receipt: plan-validated (plan was never validated)"
            )

        # Re-verify plan.md against the human-approval-PLAN_REVIEW receipt.
        # The plan-validated receipt is re-writable (validate_task_artifacts
        # auto-emits it after passing validation), so an executor can mutate
        # plan.md, re-run validation, and launder the mutation through a
        # fresh plan-validated receipt. The human-approval receipt is stored
        # in receipts/ which executor roles cannot write, so its hash is the
        # immutable anchor. A mismatch here means plan.md changed after the
        # human approved it — block regardless of plan-validated state.
        from lib_receipts import hash_file, read_receipt  # noqa: PLC0415
        approval_receipt = read_receipt(task_dir, "human-approval-PLAN_REVIEW")
        if approval_receipt is not None:
            approved_sha = approval_receipt.get("artifact_sha256", "")
            plan_path = task_dir / "plan.md"
            try:
                current_sha = hash_file(plan_path)
            except OSError:
                current_sha = ""
            if approved_sha and current_sha and approved_sha != current_sha:
                gate_errors.append(
                    "plan.md was mutated after human approval at PLAN_REVIEW "
                    f"(approved={approved_sha[:12]}… current={current_sha[:12]}…); "
                    "restore plan.md to its approved state before advancing to EXECUTION"
                )

    # CHECKPOINT_AUDIT requires executor-routing receipt + per-segment
    # executor-{seg_id} receipts proving every planned segment actually
    # completed. The per-segment enforcement only fires for transitions
    # from TEST_EXECUTION or REPAIR_EXECUTION (the stages where
    # per-segment completion is required). If executor-routing itself
    # is missing we emit only the routing-missing message (no
    # double-complaint). An empty segments list is treated as
    # "no per-segment receipts required" and passes.
    #
    # SEC-002 hardening: cross-check executor-routing.segments against
    # execution-graph.json. The graph is the plan's authoritative source
    # of truth; executor-routing is what the router actually recorded.
    # A tampered/truncated routing receipt can NOT drop segments off
    # the required-receipt list. We require executor-{seg_id} receipts
    # for the UNION of (graph segments ∪ routing segments).
    if next_stage == "CHECKPOINT_AUDIT":
        routing_payload = read_receipt(task_dir, "executor-routing")
        if routing_payload is None:
            gate_errors.append("receipt: executor-routing (executor routing was never recorded)")
        elif current_stage in {"TEST_EXECUTION", "REPAIR_EXECUTION"}:
            required_seg_ids: list[str] = []
            seen: set[str] = set()

            def _add_seg(seg_id: object) -> None:
                if isinstance(seg_id, str) and seg_id and seg_id not in seen:
                    required_seg_ids.append(seg_id)
                    seen.add(seg_id)

            # Primary source: execution-graph.json (authoritative plan).
            graph_path = task_dir / "execution-graph.json"
            if graph_path.exists():
                try:
                    graph = load_json(graph_path)
                except (OSError, ValueError):
                    graph = None
                if isinstance(graph, dict):
                    graph_segments = graph.get("segments")
                    if isinstance(graph_segments, list):
                        for entry in graph_segments:
                            if isinstance(entry, dict):
                                _add_seg(entry.get("id"))

            # Secondary source: routing receipt (union, never subset).
            routing_segments = routing_payload.get("segments")
            if isinstance(routing_segments, list):
                for entry in routing_segments:
                    if isinstance(entry, dict):
                        _add_seg(entry.get("segment_id"))

            for seg_id in required_seg_ids:
                if read_receipt(task_dir, f"executor-{seg_id}") is None:
                    gate_errors.append(
                        f"receipt: executor-{seg_id} (segment {seg_id} never completed)"
                    )

    # REPAIR_PLANNING requires no finding has exceeded max retries.
    # This is the deterministic hard cap: the LLM cannot loop past 3
    # repair attempts because the state machine itself refuses the
    # transition. Code enforces; prompts advise.
    MAX_REPAIR_RETRIES = 3
    if next_stage == "REPAIR_PLANNING" and current_stage == "REPAIR_EXECUTION":
        repair_log_path = task_dir / "repair-log.json"
        if repair_log_path.exists():
            try:
                repair_log = load_json(repair_log_path)
            except (OSError, ValueError):
                repair_log = None
            if isinstance(repair_log, dict):
                exhausted: list[str] = []
                for batch in repair_log.get("batches", []):
                    if not isinstance(batch, dict):
                        continue
                    for task_entry in batch.get("tasks", []):
                        if not isinstance(task_entry, dict):
                            continue
                        fid = task_entry.get("finding_id", "unknown")
                        retries = task_entry.get("retry_count", 0)
                        try:
                            retries_int = int(retries)
                        except (TypeError, ValueError):
                            retries_int = 0
                        if retries_int >= MAX_REPAIR_RETRIES:
                            exhausted.append(f"{fid} (retry_count={retries_int})")
                if exhausted:
                    gate_errors.append(
                        f"repair cap exceeded (max {MAX_REPAIR_RETRIES} retries) "
                        f"for finding(s): {', '.join(exhausted)}. "
                        f"Transition to FAILED instead — human review needed."
                    )

    # DONE requires retrospective + audit reports (post-completion receipt
    # is written AFTER the transition by the event bus, so cannot be gated here)
    if next_stage == "DONE":
        if not (task_dir / "task-retrospective.json").exists():
            gate_errors.append("task-retrospective.json (run /dynos-work:audit to generate)")
        if not list(task_dir.glob("audit-reports/*.json")):
            gate_errors.append("audit-reports/ (no audit reports found — audit was never run)")
        if read_receipt(task_dir, "retrospective") is None:
            gate_errors.append("receipt: retrospective (reward was never computed via receipts)")

        # AC 10 + AC 19: receipt-based DONE gates (audit-routing + per-auditor
        # spawn receipts + postmortem trio). Refuses on FIRST gap with
        # explicit log_event so the operator can see exactly which
        # receipt is missing. Only fires when reaching DONE from
        # CHECKPOINT_AUDIT or FINAL_AUDIT.
        if current_stage in {"CHECKPOINT_AUDIT", "FINAL_AUDIT"}:
            done_gaps = require_receipts_for_done(task_dir)
            if done_gaps:
                from lib_receipts import _receipts_dir  # type: ignore
                rd = _receipts_dir(task_dir)
                _refuse(
                    f"Cannot transition {current_stage} -> {next_stage}: "
                    f"receipt gaps in {rd}: " + "; ".join(done_gaps)
                )

    # AC 23 + AC 10: DONE -> CALIBRATED requires EITHER a
    # calibration-applied OR calibration-noop receipt (whichever has
    # the later ts wins), AND the receipt's policy hash
    # (``policy_sha256_after`` for applied, ``policy_sha256`` for
    # noop) must match a LIVE re-computation of the policy hash at
    # transition time. A drift refuses the transition — this is the
    # defence against stale/forged calibration receipts.
    if current_stage == "DONE" and next_stage == "CALIBRATED":
        from lib_receipts import _receipts_dir  # type: ignore
        ca_path = _receipts_dir(task_dir) / "calibration-applied.json"
        cn_path = _receipts_dir(task_dir) / "calibration-noop.json"
        applied = read_receipt(task_dir, "calibration-applied")
        noop = read_receipt(task_dir, "calibration-noop")
        if applied is None and noop is None:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"missing calibration receipt (expected one of "
                f"{ca_path} or {cn_path})"
            )
        # Pick the later-ts receipt. If tied (or ts missing), prefer
        # calibration-noop per spec direction.
        chosen = None
        chosen_is_noop = False
        if applied is not None and noop is not None:
            ts_a = applied.get("ts") if isinstance(applied, dict) else None
            ts_n = noop.get("ts") if isinstance(noop, dict) else None
            if isinstance(ts_n, str) and isinstance(ts_a, str) and ts_n >= ts_a:
                chosen, chosen_is_noop = noop, True
            elif isinstance(ts_n, str) and not isinstance(ts_a, str):
                chosen, chosen_is_noop = noop, True
            else:
                chosen, chosen_is_noop = applied, False
        elif noop is not None:
            chosen, chosen_is_noop = noop, True
        else:
            chosen, chosen_is_noop = applied, False

        # Extract the receipt-side hash.
        if chosen_is_noop:
            receipt_hash = chosen.get("policy_sha256") if isinstance(chosen, dict) else None
        else:
            receipt_hash = chosen.get("policy_sha256_after") if isinstance(chosen, dict) else None
        if not isinstance(receipt_hash, str) or not receipt_hash:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"calibration receipt missing policy hash "
                f"(receipt: {cn_path if chosen_is_noop else ca_path})"
            )

        # Live-compute the policy hash.
        try:
            from eventbus import _compute_policy_hash  # type: ignore
            live_hash = _compute_policy_hash(_root)
        except Exception as exc:
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"failed to compute live policy hash: {exc}"
            )
            live_hash = ""  # unreachable, but satisfies type checker

        if receipt_hash != live_hash:
            receipt_name = "calibration-noop" if chosen_is_noop else "calibration-applied"
            _refuse(
                f"Cannot transition {current_stage} -> {next_stage}: "
                f"{receipt_name} policy hash mismatch "
                f"(receipt={receipt_hash[:12]} live={live_hash[:12]}). "
                f"The persistent policy has drifted since the calibration "
                f"receipt was written — re-run calibration."
            )

    # AC 19: rules-check-passed receipt required at two transition points.
    # Fires AFTER all other gate clauses so existing checks order is
    # preserved. Bypassed by force=True (we are inside `if not force:`).
    if next_stage == "TEST_EXECUTION":
        _check_rules_check_passed(task_dir, current_stage, next_stage)
    if current_stage == "CHECKPOINT_AUDIT" and next_stage == "DONE":
        _check_rules_check_passed(task_dir, current_stage, next_stage)

    if gate_errors:
        raise ValueError(
            f"Cannot transition to {next_stage} — missing required artifacts:\n"
            + "\n".join(f"  - {e}" for e in gate_errors)
        )

    # ---- task-20260419-002 G4: deferred-findings TTL gate on DONE ----
    # Fire AFTER every other gate has passed. If ANY deferred finding
    # whose `files` intersects THIS task's changed files has exceeded
    # its TTL without re-acknowledgment, refuse the transition. The
    # check module is imported inline (not subprocess) for test
    # determinism; a missing module (broken install) is fail-open so
    # a busted framework does not wedge unrelated DONE transitions.
    if next_stage == "DONE":
        try:
            from check_deferred_findings import (
                check_deferred_findings as _check_deferred,
            )
        except ImportError as exc:
            # F1 AC13: surface the fail-open silent-skip via an
            # observability event BEFORE flipping _check_deferred to
            # None. D3 contract: log_event failures must never block
            # the fail-open path, so wrap the emit in a best-effort
            # try/except.
            try:
                from lib_log import log_event as _log_cdf_unavail
                _log_cdf_unavail(
                    task_dir.parent.parent,
                    "deferred_findings_check_unavailable",
                    task=task_dir.name,
                    reason="import_error",
                    error=str(exc),
                )
            except Exception:
                pass  # log_event must never block the fail-open.
            _check_deferred = None  # type: ignore

        if _check_deferred is not None:
            # Build the changed-files list. Primary source: the
            # executor-{seg} receipts' files_expected payloads (the
            # routing receipt names the segments that actually ran).
            # Fallback source: execution-graph.json's segments — so
            # the gate still fires when the executor receipt schema
            # did not yet carry files_expected at the time those
            # segments ran.
            changed_files: list[str] = []
            seen_files: set[str] = set()

            def _add_files(paths: object) -> None:
                if not isinstance(paths, list):
                    return
                for p in paths:
                    if isinstance(p, str) and p and p not in seen_files:
                        seen_files.add(p)
                        changed_files.append(p)

            # (1) Walk executor-routing → executor-{seg} receipts.
            try:
                _exec_routing = read_receipt(task_dir, "executor-routing")
            except Exception:
                _exec_routing = None
            if isinstance(_exec_routing, dict):
                _routing_segments = _exec_routing.get("segments")
                if isinstance(_routing_segments, list):
                    # SEC-002 hardening: seg_id is interpolated into the
                    # receipt step_name. Validate against a strict slug
                    # regex so a crafted routing payload cannot inject
                    # path separators or unexpected chars into the
                    # receipt filename construction.
                    import re as _re_seg
                    _SEG_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
                    for _entry in _routing_segments:
                        if not isinstance(_entry, dict):
                            continue
                        _seg_id = _entry.get("segment_id")
                        if not isinstance(_seg_id, str) or not _seg_id:
                            continue
                        if not _re_seg.match(_SEG_RE, _seg_id):
                            # Crafted / invalid seg_id — skip silently.
                            # A legitimate segment follows the slug rule.
                            continue
                        try:
                            _seg_receipt = read_receipt(
                                task_dir, f"executor-{_seg_id}"
                            )
                        except Exception:
                            _seg_receipt = None
                        if isinstance(_seg_receipt, dict):
                            _add_files(_seg_receipt.get("files_expected"))

            # (2) Fallback: execution-graph.json segments.
            _graph_path = task_dir / "execution-graph.json"
            if _graph_path.exists():
                try:
                    _graph = load_json(_graph_path)
                except (OSError, ValueError):
                    _graph = None
                if isinstance(_graph, dict):
                    _graph_segments = _graph.get("segments")
                    if isinstance(_graph_segments, list):
                        for _entry in _graph_segments:
                            if isinstance(_entry, dict):
                                _add_files(_entry.get("files_expected"))

            try:
                _expired = _check_deferred(
                    task_dir.parent.parent, changed_files
                )
            except Exception as exc:
                # check_deferred_findings is designed to never raise;
                # if it does (unforeseen bug), treat as no signal and
                # fail open rather than wedge the DONE gate.
                # F1 AC14: surface the fail-open silent-skip via an
                # observability event BEFORE the fail-open
                # assignment. D3 contract: swallow log_event
                # failures so a broken logger does not block the
                # fail-open path.
                try:
                    from lib_log import log_event as _log_cdf_err
                    _log_cdf_err(
                        task_dir.parent.parent,
                        "deferred_findings_check_errored",
                        task=task_dir.name,
                        error=str(exc),
                    )
                except Exception:
                    pass  # log_event must never block the fail-open.
                _expired = []

            if _expired:
                _ids = [
                    str(e.get("id", ""))
                    for e in _expired
                    if isinstance(e, dict)
                ]
                _refuse(
                    f"Cannot transition {current_stage} -> {next_stage}: "
                    f"deferred findings expired: {_ids}. "
                    f"Close them, re-acknowledge via "
                    f".dynos/deferred-findings.json, or use --force "
                    f"(which writes a force_override receipt)."
                )


def _dispatch_force_override_observability(
    task_dir: Path,
    manifest: dict,
    current_stage: str | None,
    next_stage: str,
    force_reason: str | None,
    force_approver: str | None,
) -> None:
    """Compute bypassed-gate strings, emit ``force_override`` event, and
    write the ``force-override-{from}-{to}`` receipt.

    All errors are swallowed: the forced transition MUST proceed even
    when observability writes fail (force is a break-glass door and
    observability cannot wedge recovery).
    """
    bypassed_gates: list[str] = _compute_bypassed_gates_for_force(
        task_dir=task_dir,
        manifest=manifest,
        current_stage=current_stage,
        next_stage=next_stage,
    )
    _root_force = task_dir.parent.parent
    _task_id_force = manifest.get("task_id") or task_dir.name
    try:
        from lib_log import log_event as _log_force
        _log_force(
            _root_force,
            "force_override",
            task=_task_id_force,
            task_id=_task_id_force,
            from_stage=current_stage,
            to_stage=next_stage,
            bypassed_gates=list(bypassed_gates),
            reason=force_reason,
            approver=force_approver,
        )
    except Exception:
        pass  # Logging must never block the forced transition.
    try:
        from lib_receipts import receipt_force_override
        receipt_force_override(
            task_dir,
            from_stage=current_stage or "UNKNOWN",
            to_stage=next_stage,
            bypassed_gates=list(bypassed_gates),
            reason=force_reason,
            approver=force_approver,
        )
    except OSError as _force_rcpt_exc:
        try:
            from lib_log import log_event as _log_force_fail
            _log_force_fail(
                _root_force,
                "force_override_receipt_write_failed",
                task=_task_id_force,
                task_id=_task_id_force,
                error=str(_force_rcpt_exc),
            )
        except Exception:
            pass
    except Exception as _force_rcpt_unexpected:
        # Non-OSError receipt-write failures are ALSO non-blocking
        # (e.g. validation bugs should not lock out recovery) — we
        # log best-effort and proceed. CQ-003: include str(exc) so
        # the event payload carries the actual failure detail
        # (the OSError branch above already does).
        try:
            from lib_log import log_event as _log_force_fail
            _log_force_fail(
                _root_force,
                "force_override_receipt_write_failed",
                task=_task_id_force,
                task_id=_task_id_force,
                error=f"unexpected: {_force_rcpt_unexpected}",
            )
        except Exception:
            pass


def _flush_and_record_transition(
    task_dir: Path,
    manifest: dict,
    next_stage: str,
) -> None:
    """Apply the post-gate side effects of a transition.

    In order:
      1. On DONE: copy retrospective into the persistent project dir
         (best-effort).
      2. Mutate the manifest dict (stage, completion_at, blocked_reason).
      3. Persist the manifest via ``write_ctl_json``.
      4. Update the active-task pointer (None on terminal stages).
      5. On REPAIR_PLANNING: write the pre-repair blocking snapshot
         (best-effort).
      6. Auto-append to execution-log.md.
      7. Emit ``stage_transition`` event (best-effort).
      8. Record a ``stage-transition`` token-ledger entry (best-effort).

    The manifest dict is mutated by reference so the caller sees the
    updated stage/completion_at fields. ``manifest_path`` is derived
    internally as ``task_dir / "manifest.json"`` — callers do not pass
    it. Caller passes ``current_stage`` separately because we no longer
    need it after the manifest mutation; it is computed for the auto-log
    line via the previous manifest stage. Errors in steps 1, 4, 5, 6,
    7, 8 are swallowed — the state-machine mutation in step 3 is the
    source of truth and observability layers cannot wedge it.
    """
    # Snapshot the pre-mutation stage so the auto-log line can name the
    # source stage. Once we mutate manifest["stage"] = next_stage the
    # original is gone.
    _pre_stage = manifest.get("stage")
    manifest_path = task_dir / "manifest.json"

    # ---- F10: Retrospective flush on DONE ----
    if next_stage == "DONE":
        _flush_retrospective_on_done(task_dir=task_dir, manifest=manifest)

    manifest["stage"] = next_stage
    if next_stage == "DONE":
        manifest["completed_at"] = now_iso()
    if next_stage == "FAILED" and manifest.get("blocked_reason") is None:
        manifest["blocked_reason"] = "transitioned to FAILED"
    write_ctl_json(task_dir, manifest_path, manifest)

    # ---- Session-task binding cleanup ----
    if next_stage in TERMINAL_STAGES:
        try:
            import actor_identity as _actor_identity  # noqa: PLC0415

            _actor_identity.clear_session_task_bindings(task_dir.parent.parent, task_dir)
        except Exception:
            pass  # Never block a terminal transition.

    # ---- Active-task pointer for O(1) SubagentStop attribution ----
    try:
        _pointer_path = task_dir.parent / "active-task.json"
        _terminal = next_stage in TERMINAL_STAGES
        _pointer_data: dict = {"task_id": None} if _terminal else {
            "task_id": manifest.get("task_id") or task_dir.name,
            "task_dir": str(task_dir),
            "stage": next_stage,
        }
        write_json(_pointer_path, _pointer_data)
    except Exception:
        pass  # Never block a completed transition.

    # ---- AC 15 side-effect: pre-repair blocking snapshot ----
    if next_stage == "REPAIR_PLANNING":
        try:
            _write_pre_repair_blocking_snapshot(task_dir)
        except Exception as exc:  # snapshot must never block transition
            try:
                from lib_log import log_event as _log_snapshot
                _log_snapshot(
                    task_dir.parent.parent,
                    "pre_repair_snapshot_failed",
                    task=task_dir.name,
                    error=str(exc),
                )
            except Exception:
                pass

    # ---- Auto-append to execution-log.md ----
    # The forced flag is intentionally always False here: the live
    # transition_task() now owns the (forced) tag via _auto_log called
    # from itself — we keep the behavior under this helper signature,
    # but the caller dispatches the auto-log invocation. We still emit
    # the stage_transition event below.
    _auto_log(task_dir, _pre_stage, next_stage, False)

    # ---- Log stage transition to events.jsonl ----
    try:
        from lib_log import log_event
        log_event(
            task_dir.parent.parent,
            "stage_transition",
            task=manifest["task_id"],
            from_stage=_pre_stage,
            to_stage=next_stage,
            forced=False,
        )
    except Exception:
        pass  # log_event must never block a completed transition.

    # ---- Auto-emit stage-transition event to per-task token ledger ----
    try:
        from lib_tokens import record_tokens, phase_for_stage
        record_tokens(
            task_dir=task_dir,
            agent="transition_task",
            model="none",
            input_tokens=0,
            output_tokens=0,
            phase=phase_for_stage(next_stage),
            stage=next_stage,
            event_type="stage-transition",
            detail=f"{_pre_stage} → {next_stage}",
        )
    except Exception:
        pass  # Never block a stage transition for a logging failure


def _fire_done_pipeline(task_dir: Path, next_stage: str) -> None:
    """Fire the post-completion pipeline on DONE — emits the synchronous
    event and dispatches the detached drain. No-op on non-DONE
    transitions. Errors are swallowed: post-completion observability
    must never block a completed transition.
    """
    if next_stage != "DONE":
        return
    try:
        _fire_task_completed(task_dir)
    except Exception:
        pass  # post-completion drain must never block the return.


def transition_task(
    task_dir: Path,
    next_stage: str,
    *,
    force: bool = False,
    force_reason: str | None = None,
    force_approver: str | None = None,
) -> tuple[str, dict]:
    """Transition a task to a new stage, enforcing allowed transitions.

    When ``force=True`` both ``force_reason`` and ``force_approver`` MUST
    be non-empty ``str`` instances. ``None`` / empty / non-string values
    raise ``ValueError`` whose message names the specific offending arg.
    Validation runs BEFORE any gate-replay or receipt-write work so a
    malformed break-glass call cannot ever reach the receipt writer.

    When ``force=False`` the two justification kwargs are accepted for
    call-site uniformity but ignored — the gate-replay path is the same
    as the pre-F1 contract.
    """
    _validate_force_justification(force, force_reason, force_approver)

    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    manifest = _backfill_tdd_required(task_dir, manifest, manifest_path)

    current_stage = manifest.get("stage")
    if next_stage not in ALLOWED_STAGE_TRANSITIONS:
        raise ValueError(f"Unknown stage: {next_stage}")
    if not force and next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise ValueError(f"Illegal stage transition: {current_stage} -> {next_stage}")

    _replay_gates_for_transition(task_dir, manifest, current_stage, next_stage, force)

    if force:
        _dispatch_force_override_observability(
            task_dir, manifest, current_stage, next_stage, force_reason, force_approver
        )

    _flush_and_record_transition(task_dir, manifest, next_stage)
    _fire_done_pipeline(task_dir, next_stage)

    return current_stage, manifest


def append_execution_log(task_dir: Path, message: str) -> None:
    """Append a timestamped line to the task's execution-log.md.

    This is the ONLY function that should write to execution-log.md.
    Format: {ISO timestamp} {message}\n
    """
    try:
        log_path = task_dir / "execution-log.md"
        line = f"{now_iso()} {message}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Never block pipeline for log write failure


def _auto_log(task_dir: Path, from_stage: str, to_stage: str, forced: bool) -> None:
    """Auto-append stage transition to execution-log.md. Called by transition_task()."""
    force_tag = " (forced)" if forced else ""
    if to_stage == "DONE":
        append_execution_log(task_dir, f"[ADVANCE] {from_stage} → DONE{force_tag}")
    elif to_stage == "FAILED":
        append_execution_log(task_dir, f"[FAILED] {from_stage} → FAILED{force_tag}")
    else:
        append_execution_log(task_dir, f"[STAGE] → {to_stage}{force_tag}")


def _fire_task_completed(task_dir: Path) -> None:
    """Run the post-completion pipeline: emit event synchronously, then dispatch
    the drain in a detached background process and return immediately.

    The drain (policy_engine, postmortem, dashboard, register, improve,
    agent_generator, benchmark_scheduler) used to run synchronously inside
    transition_task, which kept the user blocked on system self-maintenance
    even after coding/audit was complete. Latency was wasted on work that
    aggregates value across many tasks rather than affecting any single task's
    outcome.

    Now: emit stays synchronous (must precede drain — drain has nothing to
    consume otherwise), drain is fire-and-forget via Popen with
    start_new_session=True so it survives the parent's exit and is detached
    from the parent's process group. Output is redirected to
    .dynos/events/drain.log so post-completion failures remain inspectable.
    """
    import os
    import subprocess
    from pathlib import Path as _Path

    root = task_dir.parent.parent  # .dynos/task-xxx -> repo root
    # Resolve hooks_dir from PLUGIN_HOOKS env var first so external projects
    # (whose own repo root does NOT contain a dynos-work hooks/ directory) can
    # still dispatch the drain. Fall back to `root/hooks` only when the env
    # var is unset AND that directory exists — otherwise the drain is a no-op
    # rather than a silent crash in drain.log.
    _plugin_hooks = os.environ.get("PLUGIN_HOOKS", "").strip()
    if _plugin_hooks and _Path(_plugin_hooks).is_dir():
        hooks_dir = _Path(_plugin_hooks)
    else:
        _repo_hooks = root / "hooks"
        if (_repo_hooks / "eventbus.py").is_file():
            hooks_dir = _repo_hooks
        else:
            # Neither PLUGIN_HOOKS nor root/hooks/eventbus.py is available.
            # Log the skip and return — don't Popen into a broken path.
            try:
                log_dir = root / ".dynos" / "events"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "drain.log").open("a").write(
                    f"\n=== drain SKIPPED for {task_dir.name} at {now_iso()} "
                    f"(PLUGIN_HOOKS unset and {root}/hooks/eventbus.py not found) ===\n"
                )
            except OSError:
                pass
            return
    env_path = f"{hooks_dir}:{os.environ.get('PYTHONPATH', '')}"

    # Step 1: Emit task-completed event with task identity (sync — small write)
    task_id = task_dir.name
    payload = json.dumps({"task_id": task_id, "task_dir": str(task_dir)})
    try:
        subprocess.run(
            [_PYTHON3, str(hooks_dir / "lib_events.py"), "emit",
             "--root", str(root), "--type", "task-completed", "--source", "task",
             "--payload", payload],
            env={**__import__("os").environ, "PYTHONPATH": env_path},
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        print(f"[dynos] event emit failed: {exc}")

    # Step 2: Dispatch drain in a detached background process (fire-and-forget).
    # The parent returns immediately; the child runs the full handler chain in
    # the background, writing its output to .dynos/events/drain.log.
    try:
        log_dir = root / ".dynos" / "events"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "drain.log"
        # Open as binary append; the with-block closes the parent's fd after
        # Popen has dup'd it for the child. The child keeps writing to the file.
        with open(log_path, "ab") as log_fh:
            log_fh.write(f"\n=== drain dispatched for {task_id} at {now_iso()} ===\n".encode())
            log_fh.flush()
            subprocess.Popen(
                [_PYTHON3, str(hooks_dir / "eventbus.py"), "drain",
                 "--root", str(root)],
                env={**__import__("os").environ, "PYTHONPATH": env_path},
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as exc:
        print(f"[dynos] event drain dispatch failed: {exc}")


def next_command_for_stage(stage: str) -> str:
    """Return the next CLI command for a given stage."""
    return NEXT_COMMAND.get(stage, "Unknown stage")


def find_active_tasks(root: Path) -> list[Path]:
    """Find all active (non-terminal) tasks under .dynos/."""
    dynos_dir = root / ".dynos"
    if not dynos_dir.exists():
        return []
    tasks: list[Path] = []
    for manifest_path in dynos_dir.glob("task-*/manifest.json"):
        try:
            manifest = load_json(manifest_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        if manifest.get("stage") not in TERMINAL_STAGES:
            tasks.append(manifest_path.parent)
    tasks.sort()
    return tasks


# ---------------------------------------------------------------------------
# Retrospective helpers
# ---------------------------------------------------------------------------

def _flushed_sha_by_task_id(root: Path) -> dict[str, str]:
    """Parse ``.dynos/events.jsonl`` once and return
    ``{task_id: last_flushed_sha256}`` from ``retrospective_flushed``
    events. LAST event wins — re-plays overwrite.

    Malformed lines are skipped silently. Missing file → empty dict.
    Used by SEC-003 cross-check on persistent retrospectives.
    """
    out: dict[str, str] = {}
    events_path = root / ".dynos" / "events.jsonl"
    if not events_path.exists():
        return out
    try:
        with events_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("event") != "retrospective_flushed":
                    continue
                tid = ev.get("task_id")
                sha = ev.get("sha256")
                if isinstance(tid, str) and tid and isinstance(sha, str) and sha:
                    out[tid] = sha
    except OSError:
        return {}
    return out


# PERF-003: per-process memo for collect_retrospectives. Key is a stat
# fingerprint of the two source dirs + events.jsonl so any mutation
# invalidates. Small dict — at most one entry per unique root path.
_COLLECT_RETRO_CACHE: dict[Path, tuple[tuple, list[dict]]] = {}


def _retros_stat_fingerprint(root: Path) -> tuple:
    """Return a tuple summarizing the current on-disk state of the three
    inputs to collect_retrospectives. Any change (new file, edit, delete)
    changes the tuple, invalidating the cache.

    The fingerprint uses stat() calls only — no file reads.

    Cost reduction vs. original: ~18 stats instead of ~59.
    - .dynos dir itself (1 stat): detects new/removed task dirs.
    - Each task-*/task-retrospective.json (N stats, one per file): catches
      content changes (parent dir mtime does not update when a file inside
      a subdir changes).
    - Persistent retros dir itself (1 stat): detects new persistent retros;
      persistent retros are write-once so individual-file stats are skipped.
    - events.jsonl (1 stat): changes on every new event.
    """
    def _dir_mtime(d: Path) -> tuple:
        if not d.exists():
            return ("MISSING",)
        try:
            st = d.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ("UNREADABLE",)

    # .dynos dir: single stat detects task dir additions/removals.
    dynos_sig = _dir_mtime(root / ".dynos")

    # Individual retro files: single stat() per file (fixes prior double-stat).
    try:
        worktree_retros = tuple(
            (p.name, st.st_mtime_ns, st.st_size)
            for p in sorted((root / ".dynos").glob("task-*/task-retrospective.json"))
            for st in (p.stat(),)
        )
    except OSError:
        worktree_retros = ()

    # Persistent retros dir: single stat detects additions (files are write-once).
    try:
        persistent = _dir_mtime(_persistent_project_dir(root) / "retrospectives")
    except OSError:
        persistent = ("ERROR",)

    events_path = root / ".dynos" / "events.jsonl"
    try:
        if events_path.exists():
            est = events_path.stat()
            events_sig = (est.st_mtime_ns, est.st_size)
        else:
            events_sig = ("MISSING",)
    except OSError:
        events_sig = ("UNREADABLE",)

    return (dynos_sig, worktree_retros, persistent, events_sig)


def append_deferred_findings(
    root: Path,
    task_id: str,
    findings: list[dict],
) -> None:
    """Append non-blocking findings to ``root/.dynos/deferred-findings.json``.

    task-20260419-002 G4: this is the persistence path for the deferred-
    findings registry. Each entry in ``findings`` must be a dict shaped as:

        {"id": <non-empty str>,
         "category": <non-empty str>,
         "files": [<non-empty str>, ...]}  # non-empty list of non-empty strs

    The writer augments every entry with:
      - ``task_id``            (from the ``task_id`` arg)
      - ``first_seen_at``      (ISO-8601 UTC from ``now_iso()``)
      - ``first_seen_at_task_count``
            (count of ``_persistent_project_dir(root)/retrospectives/*.json``
             files at append time)
      - ``acknowledged_until_task_count``  (constant ``3``)

    Validation is all-or-nothing: every entry is validated BEFORE any write
    happens. The FIRST invalid entry raises ``ValueError`` and the registry
    file on disk is unchanged (no partial mutation). The four validation
    rules fire in order: (a) top-level ``findings`` must be a list;
    (b) each entry must be a dict; (c) each entry must carry the required
    keys with the correct types; (d) registry file, if present, must parse.

    Missing registry file (cold start) is treated as ``{"findings": []}``
    and a new registry is created. Malformed existing registry raises
    ``ValueError`` — operator must repair; dropping entries silently would
    erase prior work.

    Atomic write via ``_atomic_write_text`` so a crash mid-write cannot
    tear the registry file. The read→mutate→write sequence itself is NOT
    locked; in the single-orchestrator invariant model this is fine.
    """
    # Import _atomic_write_text lazily — lib_receipts imports lib_core at
    # module load time (circular otherwise).
    from lib_receipts import _atomic_write_text

    # SEC-003 hardening: task_id is used to tag every appended entry.
    # A malicious caller supplying "../../evil" or "" would poison the
    # registry with entries that reference invalid task ids. Validate
    # against the same slug regex used for F10 / SEC-001 / planner-
    # inject-prompt.
    import re as _re_tid
    if not isinstance(task_id, str) or not _re_tid.match(
        r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$", task_id
    ):
        raise ValueError(
            f"task_id must match ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$ (got {task_id!r})"
        )

    # Rule (a): findings must be a list.
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")

    # Rules (b) + (c): validate every entry up front — no partial write.
    for i, entry in enumerate(findings):
        if not isinstance(entry, dict):
            raise ValueError(f"findings[{i}] must be a dict")
        # Required-key presence check — raise with the first missing key.
        for key in ("id", "category", "files"):
            if key not in entry:
                raise ValueError(
                    f"findings[{i}] missing required key: {key!r}"
                )
        # Type + non-empty checks.
        id_val = entry["id"]
        if not isinstance(id_val, str) or not id_val:
            raise ValueError(f"findings[{i}].id must be a non-empty str")
        cat_val = entry["category"]
        if not isinstance(cat_val, str) or not cat_val:
            raise ValueError(
                f"findings[{i}].category must be a non-empty str"
            )
        files_val = entry["files"]
        if not isinstance(files_val, list) or not files_val:
            raise ValueError(
                f"findings[{i}].files must be a non-empty list"
            )
        for j, f in enumerate(files_val):
            if not isinstance(f, str) or not f:
                raise ValueError(
                    f"findings[{i}].files[{j}] must be a non-empty str"
                )

    registry_path = Path(root) / ".dynos" / "deferred-findings.json"

    # Load existing registry (or cold-start). Parse failure → hard error
    # per spec (operator repairs; silent drop would erase prior entries).
    if registry_path.exists():
        try:
            registry_text = registry_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"cannot parse .dynos/deferred-findings.json: {exc}"
            ) from exc
        try:
            registry = json.loads(registry_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"cannot parse .dynos/deferred-findings.json: {exc}"
            ) from exc
        if not isinstance(registry, dict):
            raise ValueError(
                "cannot parse .dynos/deferred-findings.json: "
                "root value must be an object"
            )
        existing = registry.get("findings")
        if not isinstance(existing, list):
            # Cold-start-ish: missing or malformed `findings` key. We
            # treat this as an empty list rather than a hard error so a
            # file with other keys but no findings can still be appended.
            registry["findings"] = []
    else:
        registry = {"findings": []}

    # Count retrospectives AT append time — the snapshot anchors the TTL
    # baseline for every entry added in this call.
    try:
        retro_dir = _persistent_project_dir(Path(root)) / "retrospectives"
        first_seen_at_task_count = (
            len(list(retro_dir.glob("*.json"))) if retro_dir.exists() else 0
        )
    except OSError:
        first_seen_at_task_count = 0

    first_seen_at = now_iso()

    for entry in findings:
        # Copy + augment so the caller's dict is NOT mutated (safer for
        # callers that reuse the list).
        registry["findings"].append({
            "id": entry["id"],
            "category": entry["category"],
            "files": list(entry["files"]),
            "task_id": task_id,
            "first_seen_at": first_seen_at,
            "first_seen_at_task_count": first_seen_at_task_count,
            "acknowledged_until_task_count": 3,
        })

    _atomic_write_text(
        registry_path,
        json.dumps(registry, indent=2) + "\n",
    )


def collect_retrospectives(root: Path, *, include_unverified: bool = False) -> list[dict]:
    """Collect all task retrospective JSON files from both the worktree
    and the project-persistent directory.

    Reads from:
      - ``root/.dynos/task-*/task-retrospective.json`` (in-tree, written
        by the audit skill).
      - ``_persistent_project_dir(root)/retrospectives/*.json`` (written
        by ``transition_task`` on the DONE edge — survives worktree
        removal).

    Dedupe policy: entries are keyed by ``retro["task_id"]`` (string).
    When the same task appears in both sources, the PERSISTENT copy wins
    because it is the hash-verified final state at DONE time; the
    worktree copy may have been edited post-DONE (not supposed to
    happen, but possible).

    SEC-003 hardening: for each persistent retro, the content sha256 is
    re-computed and compared to the ``retrospective_flushed`` event
    recorded in ``.dynos/events.jsonl`` at DONE time. A mismatch means
    someone tampered with the persistent file after flush; that retro
    is SKIPPED (the worktree copy, if present, is used instead). Absence
    of a flush event for a given task_id is NOT a rejection — cold-
    start / pre-SEC-003-code retros are trusted by default.

    PERF-003 hardening: results are memoized per-root, keyed by a stat
    fingerprint of the two source dirs and events.jsonl. Any mutation
    of any file in those paths invalidates the cache on the next call.

    Missing persistent dir (new project, no DONE tasks yet) is treated
    as empty. Malformed JSON is skipped silently on either side. Entries
    without a string ``task_id`` are kept under synthetic keys so a
    malformed registry cannot drop a legitimate worktree entry.

    AC14: ``include_unverified`` (keyword-only, default ``False``) controls
    whether persistent-unverified entries (``_source == "persistent-unverified"``)
    are included in the result.  When ``False`` (the default) such entries are
    filtered out before returning.  Pass ``True`` only when the caller
    explicitly needs to inspect or audit unverified entries.  The full
    (unfiltered) collection is always stored in the memo cache; the filter is
    applied at return time so filter-on and filter-off callers never interfere
    with each other's cache state.
    """
    root = Path(root)
    fingerprint = _retros_stat_fingerprint(root)
    cached = _COLLECT_RETRO_CACHE.get(root)
    if cached is not None and cached[0] == fingerprint:
        full = list(cached[1])
        if include_unverified:
            return full
        return [
            entry for entry in full
            if entry.get("_source") != "persistent-unverified"
        ]

    flushed_shas = _flushed_sha_by_task_id(root)

    # Import locally to avoid hashlib-at-top-level cost on import
    from lib_receipts import hash_file

    by_task_id: dict[str, dict] = {}
    synth_counter = 0

    def _ingest(path: Path, persistent: bool) -> None:
        nonlocal synth_counter
        try:
            data = load_json(path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            return
        if not isinstance(data, dict):
            return

        # AC-20: when the source retrospective JSON carries
        # ``gates_auto_approved`` and/or ``auto_approved_stages`` (written
        # by ``receipt_retrospective`` after seg-1), propagate them
        # verbatim into the aggregated output dict for this task. The
        # whole-dict ingestion below already preserves arbitrary keys,
        # but we explicitly pin these two so a future refactor that
        # narrows the kept keys cannot silently drop the auto-approval
        # fields needed for grep-style audit.
        _auto_approval_passthrough: dict[str, Any] = {}
        if "gates_auto_approved" in data:
            _auto_approval_passthrough["gates_auto_approved"] = data["gates_auto_approved"]
        if "auto_approved_stages" in data:
            _auto_approval_passthrough["auto_approved_stages"] = data["auto_approved_stages"]

        tid = data.get("task_id")

        # SEC-003: verify persistent retros against the flushed-event
        # sha256. Tampered content (content-hash != event-hash) →
        # skip. No-event-known → trust (cold-start compat).
        matched_flush_event = False
        if persistent and isinstance(tid, str) and tid:
            expected = flushed_shas.get(tid)
            if expected:
                try:
                    actual = hash_file(path)
                except OSError:
                    actual = ""
                if actual != expected:
                    # Skip this persistent copy entirely — the worktree
                    # copy (if present) will stand.
                    return
                matched_flush_event = True

        data["_path"] = str(path)
        # F1 AC21: a persistent retro without a known flushed-event match
        # (either no flush event for tid, or tid is not a non-empty
        # string) is trusted for backward compat but labelled
        # ``persistent-unverified`` so downstream consumers can audit
        # the weaker provenance. Emit a one-shot observability event
        # naming the path. D3: swallow log_event failures so a broken
        # logger does not block retro collection.
        if persistent and not matched_flush_event:
            data["_source"] = "persistent-unverified"
            try:
                from lib_log import log_event as _log_retro_unverified
                _log_retro_unverified(
                    root,
                    "retrospective_trusted_without_flush_event",
                    task=tid if isinstance(tid, str) and tid else None,
                    path=str(path),
                )
            except Exception:
                pass  # log_event must never block collection.
        elif persistent:
            data["_source"] = "persistent"
        else:
            data["_source"] = "worktree"

        # AC-20: re-pin the auto-approval pass-through so the aggregated
        # entry always carries ``gates_auto_approved`` /
        # ``auto_approved_stages`` verbatim when the source had them.
        # Re-application is a no-op when the keys already round-tripped
        # untouched, but it is a hard guarantee against any future code
        # path that narrows ``data`` between ingestion and storage.
        for _k, _v in _auto_approval_passthrough.items():
            data[_k] = _v

        if isinstance(tid, str) and tid:
            key = tid
        else:
            # No task_id — give a synthetic, path-stable key so we never
            # accidentally collapse legitimate-but-malformed entries.
            synth_counter += 1
            key = f"__no_task_id__::{data['_path']}::{synth_counter}"

        existing = by_task_id.get(key)
        if existing is None:
            by_task_id[key] = data
            return
        existing_is_persistent = existing.get("_source") == "persistent"
        if persistent and not existing_is_persistent:
            # Persistent beats worktree.
            by_task_id[key] = data
        elif not persistent and existing_is_persistent:
            # Worktree loses to persistent — keep existing.
            return
        else:
            # Same source twice — last-write wins (deterministic given
            # sorted iteration).
            by_task_id[key] = data

    # 1) Worktree retros.
    try:
        dynos_dir = root / ".dynos"
        if dynos_dir.exists():
            for path in sorted(dynos_dir.glob("task-*/task-retrospective.json")):
                _ingest(path, persistent=False)
    except OSError:
        pass

    # 2) Persistent retros — may not exist for new projects (cold start).
    try:
        persistent_dir = _persistent_project_dir(root) / "retrospectives"
        if persistent_dir.exists():
            for path in sorted(persistent_dir.glob("*.json")):
                _ingest(path, persistent=True)
    except OSError:
        pass

    result = list(by_task_id.values())
    # Recompute fingerprint after ingestion so the stored key reflects any
    # events.jsonl writes made by log_event during _ingest (e.g.
    # retrospective_trusted_without_flush_event). A subsequent call's
    # pre-ingestion fingerprint will match this post-ingestion snapshot,
    # allowing the cache hit. Using the pre-ingestion fingerprint here
    # would permanently defeat the cache because log_event mutates the
    # very file included in the fingerprint.
    _COLLECT_RETRO_CACHE[root] = (_retros_stat_fingerprint(root), result)
    if include_unverified:
        return list(result)
    return [
        entry for entry in result
        if entry.get("_source") != "persistent-unverified"
    ]


def _retrospective_task_ids(root: Path) -> list[str]:
    """Return list of task IDs from retrospectives."""
    return [item.get("task_id") for item in collect_retrospectives(root) if isinstance(item.get("task_id"), str)]


def _task_recency_index(root: Path, task_id: Optional[str]) -> Optional[int]:
    """Return how many tasks ago a given task_id appeared (0 = most recent)."""
    if not task_id:
        return None
    task_ids = _retrospective_task_ids(root)
    if task_id not in task_ids:
        return None
    return len(task_ids) - 1 - task_ids.index(task_id)


def tasks_since(root: Path, task_id: Optional[str]) -> Optional[int]:
    """Return the number of tasks since a given task_id."""
    return _task_recency_index(root, task_id)
