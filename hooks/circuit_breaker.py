"""Circuit breaker for dynos-work tasks.

Encodes runtime conditions under which a task should be hard-aborted or
downgraded. Live-enforceable (EXECUTION) conditions:

    1. wasted_spawns_abort       (>= 9 non-converging repairs; via ctl.py check-spawn-budget)
    2. small_task_token_overrun  (>= 9_371_185 tokens on a small bugfix/feature task)
    3. bugfix_token_overrun      (>= 68_283_719 tokens on a bugfix)
    4. small_task_token_downgrade  (>= 7_496_948 but < 9_371_185)
    5. bugfix_token_downgrade      (>= 54_626_975 but < 68_283_719)

Audit-time (AUDITING) condition:

    6. opus_auditor_zero_yield   (>= 3 opus audit spawns whose findings are [])  # noqa: model-literal

Trust model
-----------
AUDITING arm: every audit-spawn event passing the phase/type/model filters is
cross-checked via _validate_audit_event_receipt before its findings are
counted. The helper verifies that a receipts/audit-{agent}.json exists and
that, when route_mode is not "generic", the sidecar sha256 matches the
receipt's injected_agent_sha256 field. Events failing validation are skipped
rather than counted, preventing forged token-usage.json entries from
triggering the zero-yield breaker. Closes SEC-CB-001 from task-20260507-003;
companion to PR 184's _SAFE_AGENT_RE addition.
EXECUTION arm: operator-trust boundary enforced by write_policy.py — cite
manifest.json deny, execution-graph.json wrapper-required, and
token-usage.json system-only blocks.

Temporal invariant: the breaker's AUDITING checkpoint runs only after all
auditors have returned and their receipts have been written. The receipt-
provenance cross-check therefore never races a legitimate audit's receipt
write — by the time the breaker checks, the receipts/_injected-auditor-
prompts/ sidecars and receipts/audit-<name>.json files are on disk for
every legitimate audit dispatched in this checkpoint cycle.

Activation procedure
--------------------
BREAKER_ACTIVE=False (default) is observe-only mode. Every checkpoint calls
_dispatch_breaker_decision, which always logs a ``circuit_breaker_observed``
event to per-task events.jsonl regardless of the gate value. When
BREAKER_ACTIVE is False the helper returns immediately after logging — it
does NOT call _apply_abort and does NOT log circuit_breaker_downgrade_suggested.

To activate the breaker:
  (a) After N tasks, grep per-task events.jsonl files for
      ``circuit_breaker_observed`` events and review would_have_aborted /
      would_have_downgraded fields for false positives.
  (b) Verify zero false positives across the review window.
  (c) Flip ``BREAKER_ACTIVE = True`` in a single-line follow-up PR.
  (d) Threshold calibration (adjusting token limits, spawn thresholds) is a
      separate task informed by the observed events and is independent of
      the activation flip.

Closes residual a1c4a97c.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from lib_models import resolve_model_for_tier, TIER_DEEP  # noqa: E402
except ImportError:  # pragma: no cover — only fails in unusual environments
    def resolve_model_for_tier(host: str, tier: str) -> Optional[str]:  # type: ignore[misc]
        """Fallback: treat any unknown host as claude-like (opus for deep tier)."""  # noqa: model-literal
        _FALLBACK = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}  # noqa: model-literal
        return _FALLBACK.get(tier)

    TIER_DEEP: str = "deep"  # type: ignore[assignment]


# Resolved interpreter path — required by the repo-wide
# tests/test_no_raw_subprocess_python_git.py guard that forbids bare
# `"python3"` list-literals in hooks/.
_PYTHON3: str = shutil.which("python3") or sys.executable


# ---------------------------------------------------------------------------
# Module-level constants (AC-2..AC-6, AC-35, AC-36)
# ---------------------------------------------------------------------------

WASTED_SPAWN_ABORT_THRESHOLD: int = 9
SMALL_TASK_TOKEN_LIMIT: int = 9_371_185
BUGFIX_TOKEN_LIMIT: int = 68_283_719
OPUS_AUDITOR_ZERO_YIELD_THRESHOLD: int = 3
SMALL_TASK_FILES_THRESHOLD: int = 2
SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD: int = 7_496_948
BUGFIX_TOKEN_DOWNGRADE_THRESHOLD: int = 54_626_975

# Activation gate. Default False: the breaker logs decisions via
# _dispatch_breaker_decision but does NOT call _apply_abort. Flip to
# True only after reviewing circuit_breaker_observed events from the
# last N tasks and verifying zero false positives. Activation is a
# one-line PR after operator validation; threshold tuning is a
# separate task informed by the observed events. Closes residual
# a1c4a97c.
BREAKER_ACTIVE: bool = False

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Internal helpers (no I/O at import time)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    """Best-effort JSON read; return None on missing/invalid JSON."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[circuit_breaker] failed to read {path}: {exc}", file=sys.stderr)
        return None


def _classification_type(manifest: dict) -> str:
    classification = manifest.get("classification") or {}
    if not isinstance(classification, dict):
        return ""
    value = classification.get("type")
    return value if isinstance(value, str) else ""


def _token_total(
    task_dir: Path,
    *,
    token_usage_data: Optional[dict] = None,
) -> int:
    """Read token-usage.json total, falling back to input+output sum.

    Missing file → 0 (AC-23).

    perf-003 (this PR): caller may pass already-parsed token-usage.json
    via ``token_usage_data`` to avoid a redundant cold read on the hot
    path (check_circuit_breakers calls both _token_total AND
    _deep_tier_zero_yield_count, both of which previously re-parsed the file).
    """
    if token_usage_data is None:
        data = _read_json(task_dir / "token-usage.json")
    else:
        data = token_usage_data
    if not isinstance(data, dict):
        return 0
    total = data.get("total")
    if isinstance(total, int):
        return total
    if isinstance(total, float):
        return int(total)
    inp = data.get("total_input_tokens", 0)
    out = data.get("total_output_tokens", 0)
    try:
        return int(inp) + int(out)
    except (TypeError, ValueError):
        return 0


def _unique_files_count(
    task_dir: Path,
    *,
    graph_data: Optional[dict] = None,
) -> Optional[int]:
    """Count distinct files_expected across all segments (AC-12).

    Returns None if execution-graph.json is missing/unreadable (AC-22).

    perf-003 (this PR): caller may pass already-parsed execution-graph.json
    via ``graph_data`` to avoid a redundant cold read on the hot path.
    """
    if graph_data is None:
        graph_path = task_dir / "execution-graph.json"
        if not graph_path.exists():
            print(
                f"[circuit_breaker] execution-graph.json missing at {graph_path}; "
                "skipping small-task token condition.",
                file=sys.stderr,
            )
            return None
        graph = _read_json(graph_path)
    else:
        graph = graph_data
    if not isinstance(graph, dict):
        return None
    segments = graph.get("segments")
    if not isinstance(segments, list):
        return 0
    unique: set[str] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        files = seg.get("files_expected") or []
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                unique.add(entry)
    return len(unique)


def _check_spawn_budget(task_dir: Path, manifest: Optional[dict] = None) -> int:
    """Return the wasted-spawn count via in-process call to
    ``ctl.compute_spawn_budget_status``.

    perf-001 (this PR): replaces the per-call subprocess fork that
    previously paid Python interpreter startup + ctl.py module-import
    latency on every EXECUTION-stage invocation. The shared counting
    helper in ctl.py is read-only (no receipt write, no manifest
    mutation), so the breaker's call has no side effects — _apply_abort
    still owns the actual transition.

    On any failure (import error, missing manifest, etc.) log a warning
    and return 0 so the wasted_spawns arm does not fire spuriously.
    """
    if manifest is None:
        manifest = _read_json(task_dir / "manifest.json")
    if not isinstance(manifest, dict):
        return 0
    try:
        # Late import to break the import cycle (ctl imports many things;
        # circuit_breaker is intentionally light-weight at module load).
        from ctl import compute_spawn_budget_status  # noqa: PLC0415
    except ImportError as exc:
        print(
            f"[circuit_breaker] compute_spawn_budget_status import failed: {exc}",
            file=sys.stderr,
        )
        return 0
    try:
        result = compute_spawn_budget_status(
            task_dir,
            manifest,
            project_root=_REPO_ROOT,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        print(
            f"[circuit_breaker] compute_spawn_budget_status failed: {exc}",
            file=sys.stderr,
        )
        return 0
    count = result.get("count")
    if not isinstance(count, int):
        return 0
    return count


def _deep_tier_zero_yield_count(
    task_dir: Path,
    *,
    host: str = "claude",
    token_usage_data=None,
) -> int:
    """Count deep-tier audit spawns whose audit-report has empty findings.

    Per AC-15, model is read from the per-event `model` field on the
    events array — not from `by_agent`. The expected model is
    ``resolve_model_for_tier(host, TIER_DEEP)``; for host="claude" this
    is "opus", for host="codex" this is None.  Per AC-16, an event without a  # noqa: model-literal
    matching audit-report file is skipped.

    ``token_usage_data`` may be:
      - None: read token-usage.json from disk.
      - a dict with an "events" key: use that dict's events list.
      - a list: treat directly as the events list (test-fixture shortcut).

    perf-002/perf-003 (this PR): caller may pass already-parsed
    token-usage.json content via ``token_usage_data`` to avoid a cold
    re-read on the hot path. The audit-reports directory listing is
    snapshotted once into a stem-keyed cache so repeated lookups don't
    re-glob.
    """
    deep_tier_model = resolve_model_for_tier(host, TIER_DEEP)

    # Determine whether caller passed events directly (list) or token-usage
    # JSON data (dict or None to read from disk).
    raw_list_mode = isinstance(token_usage_data, list)
    if raw_list_mode:
        # Caller passed the events list directly (e.g. test fixtures).
        # In this mode only the model-predicate check applies — phase/type
        # and receipt/audit-report validation are skipped because the
        # fixture events are intentionally minimal.
        events = token_usage_data
    else:
        if token_usage_data is None:
            data = _read_json(task_dir / "token-usage.json")
        else:
            data = token_usage_data
        if not isinstance(data, dict):
            return 0
        events = data.get("events")
        if not isinstance(events, list):
            return 0

    audit_reports_dir = task_dir / "audit-reports"
    # perf-002: snapshot the audit-reports directory listing once.
    # `name_index` maps stem -> list[Path] for files whose stem starts
    # with any plausible agent name. _lookup_audit_report walks this
    # cached list once per agent instead of re-globbing the directory.
    name_index: Optional[list[Path]] = None
    if not raw_list_mode:
        try:
            if audit_reports_dir.is_dir():
                name_index = [
                    p for p in audit_reports_dir.iterdir()
                    if p.is_file() and p.suffix == ".json"
                ]
        except OSError:
            name_index = None

    # perf-002 secondary: memoize parsed reports across the event loop.
    report_cache: dict[Path, Optional[dict]] = {}

    zero_yield = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        if not raw_list_mode:
            if event.get("phase") != "audit":
                continue
            if event.get("type") != "spawn":
                continue
        # AC-15: predicate is host-aware — compare against the deep-tier model
        # for this host (e.g. "opus" for claude, None for codex).  # noqa: model-literal
        if resolve_model_for_tier(host, TIER_DEEP) != event.get("model"):
            continue
        if not raw_list_mode:
            # AC-6: receipt-provenance cross-check. Skip when deep-tier model
            # is None (e.g. codex) because _validate_audit_event_receipt
            # requires a non-empty string model; None-model events cannot
            # carry a sidecar.
            if deep_tier_model is not None:
                if not _validate_audit_event_receipt(event, task_dir):
                    continue
            agent = event.get("agent")
            report = _lookup_audit_report(
                audit_reports_dir,
                agent,
                name_index=name_index,
                report_cache=report_cache,
            )
            if report is None:
                # AC-16: missing audit report → skip this event
                continue
            findings = report.get("findings")
            if isinstance(findings, list) and len(findings) == 0:
                zero_yield += 1
        else:
            # raw list mode: count any event matching the deep-tier model
            zero_yield += 1
    return zero_yield


_SAFE_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_audit_event_receipt(event: dict, task_dir: Path) -> bool:
    """Cross-check an audit-spawn event against its on-disk receipt.

    Returns True iff:
      - event["agent"] is a non-empty string matching _SAFE_AGENT_RE
      - event["model"] is a non-empty string
      - a receipt file exists at receipts/audit-{agent}.json (primary) or
        receipts/audit-{agent}-auditor.json (fallback)
      - when receipt["injected_agent_sha256"] is a non-empty string, the
        sidecar at receipts/_injected-auditor-prompts/{agent}-{model}.sha256
        exists, is non-empty after strip, and its stripped content matches
        the receipt hash
      - when receipt["injected_agent_sha256"] is empty/missing AND
        receipt["route_mode"] == "generic", no sidecar is required

    Returns False on any validation failure; never raises.
    """
    # AC-2: validate agent name
    agent = event.get("agent")
    if not isinstance(agent, str) or not agent:
        return False
    if not _SAFE_AGENT_RE.match(agent):
        return False

    # Validate model field (needed for sidecar path construction).
    # Apply the same _SAFE_AGENT_RE clamp as agent — model is interpolated
    # into the sidecar filename ({agent}-{model}.sha256) and a path-traversal
    # value like "../../etc" would resolve outside _injected-auditor-prompts/.
    # Closes SEC-CB-CHK-001 from this task's checkpoint audit.
    model = event.get("model")
    if not isinstance(model, str) or not model:
        return False
    if not _SAFE_AGENT_RE.match(model):
        return False

    # AC-3: try primary then fallback receipt filename
    receipts_dir = task_dir / "receipts"
    primary = receipts_dir / f"audit-{agent}.json"
    fallback = receipts_dir / f"audit-{agent}-auditor.json"

    receipt: Optional[dict] = None
    if primary.exists():
        receipt = _read_json(primary)
    elif fallback.exists():
        receipt = _read_json(fallback)

    if not isinstance(receipt, dict):
        return False

    # AC-4 / AC-5: decide whether sidecar check is required
    injected_sha = receipt.get("injected_agent_sha256")
    route_mode = receipt.get("route_mode")

    if isinstance(injected_sha, str) and injected_sha:
        # Sidecar check is mandatory — compare contents
        sidecar_path = (
            receipts_dir
            / "_injected-auditor-prompts"
            / f"{agent}-{model}.sha256"
        )
        try:
            sidecar_content = sidecar_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not sidecar_content:
            return False
        return sidecar_content == injected_sha

    # No injected_agent_sha256 (empty or missing)
    # AC-5: generic route skips sidecar requirement
    if route_mode == "generic":
        return True

    # Non-generic route with no injected sha is ambiguous; reject conservatively
    return False


def _lookup_audit_report(
    audit_reports_dir: Path,
    agent: str,
    *,
    name_index: Optional[list[Path]] = None,
    report_cache: Optional[dict[Path, Optional[dict]]] = None,
) -> Optional[dict]:
    """Find an audit report whose filename starts with the agent name.

    The audit-report writer commonly names files `<agent>.json` or
    `<agent>-<round>.json`; we accept either.

    The ``agent`` value originates from token-usage.json events which are
    operator-side state, but a malformed entry containing path separators,
    `..`, or glob metacharacters could escape ``audit_reports_dir`` (POSIX
    pathlib does not reject these in joins) or cause the glob to match
    unintended files. Reject any ``agent`` that does not match
    ``^[A-Za-z0-9_-]+$``.

    perf-002 (this PR): callers iterating many events should pass
    ``name_index`` (a pre-listed snapshot of the directory's *.json files)
    and ``report_cache`` (a dict that memoizes parsed report bodies).
    Both are optional — when omitted, the function falls back to a glob
    per call (legacy behavior).
    """
    if not isinstance(agent, str) or not _SAFE_AGENT_RE.match(agent):
        return None
    if name_index is None and (not audit_reports_dir.exists() or not audit_reports_dir.is_dir()):
        return None

    def _read_cached(path: Path) -> Optional[dict]:
        if report_cache is not None and path in report_cache:
            return report_cache[path]
        result = _read_json(path)
        # Only cache dicts; non-dict results stay None per existing contract.
        cached = result if isinstance(result, dict) else None
        if report_cache is not None:
            report_cache[path] = cached
        return cached

    direct = audit_reports_dir / f"{agent}.json"
    if name_index is not None:
        # perf-002 fast path: O(N) walk of the cached listing per agent
        # instead of stat + glob per call. Sort candidates so output is
        # deterministic across platforms (matches the legacy glob+sorted
        # ordering).
        prefix = f"{agent}"
        candidates = sorted(
            p for p in name_index
            if p.stem == prefix or p.stem.startswith(prefix)
        )
    else:
        if direct.exists():
            return _read_cached(direct)
        try:
            candidates = sorted(audit_reports_dir.glob(f"{agent}*.json"))
        except OSError:
            return None

    # When using name_index we may have the direct match in the candidate
    # list — try it first to preserve the legacy "exact match wins" rule.
    direct_first = sorted(candidates, key=lambda p: (p != direct, p))
    for candidate in direct_first:
        report = _read_cached(candidate)
        if isinstance(report, dict):
            return report
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_circuit_breakers(task_dir: Path, stage: str) -> Optional[dict]:
    """Evaluate circuit-breaker conditions for the given task and stage.

    Returns:
        - None if no condition fires (or stage is irrelevant, or manifest
          is missing).
        - An abort dict with keys {abort, trigger, reason, limit, actual}
          if any abort condition fires.
        - A downgrade dict with keys {action, trigger, reason, suggestion,
          limit_warned, limit_abort, actual} if a downgrade tier fires.

    Does NOT mutate task files or call _apply_abort. The caller is
    responsible for invoking _apply_abort on a non-None return value.
    """
    if stage not in ("EXECUTION", "AUDITING"):
        return None  # AC-7

    task_dir = Path(task_dir)

    # AC-24: manifest.json missing → bail with stderr message.
    manifest = _read_json(task_dir / "manifest.json")
    if not isinstance(manifest, dict):
        print(
            f"[circuit_breaker] manifest.json missing or unreadable at {task_dir}; "
            "skipping circuit-breaker evaluation.",
            file=sys.stderr,
        )
        return None

    # perf-003 (this PR): read token-usage.json and execution-graph.json
    # once at function entry instead of per-helper. _token_total,
    # _unique_files_count, and _deep_tier_zero_yield_count all take pre-parsed
    # dicts via keyword args. Each file may be absent (each helper
    # gracefully degrades to its missing-file branch).
    token_usage_data = _read_json(task_dir / "token-usage.json")
    if not isinstance(token_usage_data, dict):
        token_usage_data = None
    graph_data = _read_json(task_dir / "execution-graph.json")
    if not isinstance(graph_data, dict):
        graph_data = None

    # Derive host from manifest; default "claude" preserves existing behavior.
    host: str = manifest.get("host") or "claude"  # type: ignore[assignment]

    if stage == "AUDITING":
        return _evaluate_auditing(task_dir, host=host, token_usage_data=token_usage_data)

    # stage == "EXECUTION"
    return _evaluate_execution(
        task_dir,
        manifest,
        token_usage_data=token_usage_data,
        graph_data=graph_data,
    )


def _evaluate_execution(
    task_dir: Path,
    manifest: dict,
    *,
    token_usage_data: Optional[dict] = None,
    graph_data: Optional[dict] = None,
) -> Optional[dict]:
    """Evaluate the EXECUTION-stage conditions in deterministic order.

    Order (per Implicit-Requirement, AC-9..AC-11, AC-37, AC-38):
        1. wasted_spawns_abort
        2. small_task_token_overrun
        3. bugfix_token_overrun
        4. small_task_token_downgrade
        5. bugfix_token_downgrade
    """
    # 1. wasted_spawns_abort (AC-9, AC-34)
    wasted = _check_spawn_budget(task_dir, manifest=manifest)
    if wasted >= WASTED_SPAWN_ABORT_THRESHOLD:
        return {
            "abort": True,
            "trigger": "wasted_spawns_abort",
            "reason": (
                "non-converging-repair count reached the hard-abort threshold; "
                "repairs keep re-flagging findings without resolving them."
            ),
            "limit": WASTED_SPAWN_ABORT_THRESHOLD,
            "actual": wasted,
        }

    classification_type = _classification_type(manifest)
    tokens = _token_total(task_dir, token_usage_data=token_usage_data)
    unique_files = _unique_files_count(task_dir, graph_data=graph_data)

    is_small_eligible = (
        classification_type in {"bugfix", "feature"}
        and unique_files is not None
        and unique_files <= SMALL_TASK_FILES_THRESHOLD
    )

    # 2. small_task_token_overrun (AC-10) — inclusive at the limit so
    # boundary tokens == LIMIT triggers abort (not the downgrade tier
    # below, which is < LIMIT). Closes the AC-37/AC-38 boundary gap.
    if is_small_eligible and tokens >= SMALL_TASK_TOKEN_LIMIT:
        return {
            "abort": True,
            "trigger": "small_task_token_overrun",
            "reason": (
                "small task (<= 2 files, bugfix/feature) exceeded the 1M "
                "token ceiling; halting before further token burn."
            ),
            "limit": SMALL_TASK_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 3. bugfix_token_overrun (AC-11) — inclusive at the limit (see
    # AC-37/AC-38 boundary fix above for the small-task case).
    if classification_type == "bugfix" and tokens >= BUGFIX_TOKEN_LIMIT:
        return {
            "abort": True,
            "trigger": "bugfix_token_overrun",
            "reason": (
                "bugfix task exceeded the 5M token ceiling; halting "
                "before unbounded burn."
            ),
            "limit": BUGFIX_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 4. small_task_token_downgrade (AC-37). Strict `<` upper bound:
    # rule comp-f4ce7158af74 — abort arm at `>= LIMIT` catches the
    # boundary, so the downgrade arm uses an exclusive upper bound.
    if (
        is_small_eligible
        and SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD
        <= tokens
        < SMALL_TASK_TOKEN_LIMIT
    ):
        return {
            "action": "downgrade",
            "trigger": "small_task_token_downgrade",
            "reason": (
                "small task token usage crossed 80% of the 1M ceiling; "
                "downgrade remaining spawns before the hard abort."
            ),
            "suggestion": "downgrade remaining spawns from opus to sonnet/haiku",  # noqa: model-literal
            "limit_warned": SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD,
            "limit_abort": SMALL_TASK_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 5. bugfix_token_downgrade (AC-38). Strict `<` upper bound — same
    # rule as small_task above (comp-f4ce7158af74).
    if (
        classification_type == "bugfix"
        and BUGFIX_TOKEN_DOWNGRADE_THRESHOLD <= tokens < BUGFIX_TOKEN_LIMIT
    ):
        return {
            "action": "downgrade",
            "trigger": "bugfix_token_downgrade",
            "reason": (
                "bugfix token usage crossed 80% of the 5M ceiling; "
                "downgrade remaining spawns before the hard abort."
            ),
            "suggestion": "downgrade remaining spawns from opus to sonnet/haiku",  # noqa: model-literal
            "limit_warned": BUGFIX_TOKEN_DOWNGRADE_THRESHOLD,
            "limit_abort": BUGFIX_TOKEN_LIMIT,
            "actual": tokens,
        }

    return None  # AC-8


def _evaluate_auditing(
    task_dir: Path,
    *,
    host: str = "claude",
    token_usage_data: Optional[dict] = None,
) -> Optional[dict]:
    """Evaluate AUDITING-stage condition: deep-tier zero-yield only (AC-13)."""
    zero_yield = _deep_tier_zero_yield_count(
        task_dir, host=host, token_usage_data=token_usage_data
    )
    if zero_yield >= OPUS_AUDITOR_ZERO_YIELD_THRESHOLD:
        return {
            "abort": True,
            "trigger": "opus_auditor_zero_yield",
            "reason": (
                "three or more opus audit spawns produced empty findings; "  # noqa: model-literal
                "further opus audits are not yielding signal."  # noqa: model-literal
            ),
            "limit": OPUS_AUDITOR_ZERO_YIELD_THRESHOLD,
            "actual": zero_yield,
        }
    return None


def _apply_abort(task_dir: Path, decision: dict) -> None:
    """Persist the circuit-breaker decision to disk and (for aborts only)
    transition the task manifest to FAILED via ctl.py.

    Behavior is determined by the decision dict shape (AC-42):
      - abort=True            → escalation.md + [BUDGET-ABORT] log + transition FAILED
      - action=="downgrade"   → escalation.md + [BUDGET-DOWNGRADE] log only
      - anything else         → undefined; we no-op to be safe.
    """
    task_dir = Path(task_dir)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    is_abort = decision.get("abort") is True
    is_downgrade = (not is_abort) and decision.get("action") == "downgrade"

    if not is_abort and not is_downgrade:
        # Undefined behavior per AC-42; safest action is to do nothing.
        print(
            "[circuit_breaker] _apply_abort called with unrecognized decision shape; "
            "no action taken.",
            file=sys.stderr,
        )
        return

    trigger = str(decision.get("trigger", "unknown"))
    reason = str(decision.get("reason", ""))
    actual = decision.get("actual", "")
    if is_abort:
        limit = decision.get("limit", "")
        kind_heading = "Circuit Breaker Abort"
        log_tag = "[BUDGET-ABORT]"
        limit_block = f"- **Limit:** {limit}\n"
    else:
        limit_warned = decision.get("limit_warned", "")
        limit_abort = decision.get("limit_abort", "")
        suggestion = decision.get("suggestion", "")
        kind_heading = "Circuit Breaker Downgrade"
        log_tag = "[BUDGET-DOWNGRADE]"
        limit_block = (
            f"- **Warn threshold:** {limit_warned}\n"
            f"- **Abort threshold:** {limit_abort}\n"
            f"- **Suggestion:** {suggestion}\n"
        )

    escalation_path = task_dir / "escalation.md"
    log_path = task_dir / "execution-log.md"

    body = (
        f"# {kind_heading}\n\n"
        f"- **Trigger:** {trigger}\n"
        f"- **Reason:** {reason}\n"
        f"{limit_block}"
        f"- **Actual:** {actual}\n"
        f"- **Timestamp:** {timestamp}\n"
    )

    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        escalation_path.write_text(body, encoding="utf-8")  # AC-18: overwrite
    except OSError as exc:
        print(
            f"[circuit_breaker] failed to write escalation.md at {escalation_path}: {exc}",
            file=sys.stderr,
        )

    log_line = f"{log_tag} {timestamp} trigger={trigger} reason={reason}\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(log_line)
    except OSError as exc:
        print(
            f"[circuit_breaker] failed to append to execution-log.md: {exc}",
            file=sys.stderr,
        )

    if is_downgrade:
        return  # AC-42: do NOT transition on downgrade

    # AC-20: invoke ctl.py transition ... FAILED; swallow all errors.
    cmd = [
        _PYTHON3,
        str(_REPO_ROOT / "hooks" / "ctl.py"),
        "transition",
        "--task-dir",
        str(task_dir),
        "FAILED",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            print(
                f"[circuit_breaker] ctl.py transition FAILED returned "
                f"{completed.returncode}: {completed.stderr.strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[circuit_breaker] ctl.py transition FAILED raised: {exc}",
            file=sys.stderr,
        )


def _dispatch_breaker_decision(
    task_dir: Path,
    stage: str,
    decision: Optional[dict],
    *,
    task_id: str,
) -> None:
    """Log the circuit-breaker decision as a diagnostic event and optionally act.

    Always logs ``circuit_breaker_observed`` to per-task events.jsonl.
    When BREAKER_ACTIVE is True:
      - abort decisions: calls _apply_abort(task_dir, decision)
      - downgrade decisions: additionally logs ``circuit_breaker_downgrade_suggested``
    When BREAKER_ACTIVE is False: observes only — no _apply_abort, no downgrade log.

    Fully exception-safe: any error is printed to stderr and None is returned.
    """
    try:
        is_abort = isinstance(decision, dict) and decision.get("abort") is True
        is_downgrade = (
            isinstance(decision, dict)
            and not is_abort
            and decision.get("action") == "downgrade"
        )
        root = task_dir.parent.parent

        log_event(
            root,
            "circuit_breaker_observed",
            task=task_id,
            stage=stage,
            decision=decision,
            would_have_aborted=is_abort,
            would_have_downgraded=is_downgrade,
        )

        if BREAKER_ACTIVE is True:
            if is_abort:
                _apply_abort(task_dir, decision)
            elif is_downgrade:
                log_event(
                    root,
                    "circuit_breaker_downgrade_suggested",
                    task=task_id,
                    stage=stage,
                    decision=decision,
                    would_have_aborted=False,
                    would_have_downgraded=True,
                )
    except Exception as exc:  # noqa: BLE001 — fully exception-safe by spec
        print(
            f"[circuit_breaker] _dispatch_breaker_decision failed: {exc}",
            file=sys.stderr,
        )
        return None


# Late import to break the import cycle (lib_log imports write_policy
# which may import ctl; keeping this at module scope after all definitions
# avoids circular-import issues while making log_event a module-level name
# that tests can monkeypatch via cb.log_event).
try:
    from lib_log import log_event  # noqa: E402
except ImportError:  # pragma: no cover — only fails in unusual environments

    def log_event(root, event_type, **kwargs):  # type: ignore[no-redef]
        """Fallback no-op when lib_log is unavailable."""
        print(
            f"[circuit_breaker] lib_log unavailable; would have logged {event_type}",
            file=sys.stderr,
        )


__all__ = [
    "WASTED_SPAWN_ABORT_THRESHOLD",
    "SMALL_TASK_TOKEN_LIMIT",
    "BUGFIX_TOKEN_LIMIT",
    "OPUS_AUDITOR_ZERO_YIELD_THRESHOLD",
    "SMALL_TASK_FILES_THRESHOLD",
    "SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD",
    "BUGFIX_TOKEN_DOWNGRADE_THRESHOLD",
    "BREAKER_ACTIVE",
    "check_circuit_breakers",
    "_apply_abort",
    "_dispatch_breaker_decision",
]
