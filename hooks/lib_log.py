"""Unified structured event logging for dynos-work.

Writes JSONL events to per-task log files at .dynos/task-{id}/events.jsonl.
When no task context is available, falls back to .dynos/events.jsonl (global).
Thread-safe via fcntl advisory locking.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from lib_core import _persistent_project_dir, now_iso
from write_policy import WriteAttempt, _get_capability_key, require_write_allowed


# Event names whose sole purpose is operator visibility / forensic trace.
# These events do NOT participate in receipts, stage transitions, or any
# downstream deterministic gate — they are diagnostic-only. Auditors and
# retrospectives may read them, but nothing in the state machine blocks on
# their presence or absence. Kept as a frozenset so the constant cannot be
# mutated at runtime (tests that want to simulate an extra event-name add
# to a local copy, not this module's constant).
DIAGNOSTIC_ONLY_EVENTS: frozenset[str] = frozenset({
    "gate_refused",
    "receipt_refused",
    "finding_contradiction",
    "auditor_not_in_routing",
    "auditor_cross_check_skipped",
    "prevention_rules_corrupt",
    "prevention_rules_corrupt_bootstrap",
    "prevention_rules_healed",
    "pre_repair_snapshot_failed",
    "learned_agent_missing",
    "learned_agent_error",
    "learned_auditor_error",
    "learned_auditor_missing",
    "learned_agent_applied",
    "learned_auditor_applied",
    "router_cache_write_failed",
    "router_cache_lookup",
    "router_cache_write",
    "router_audit_plan",
    "router_executor_plan",
    "router_model_decision",
    "router_model_ceiling_clamp",
    "router_route_decision",
    "plan_audit_skipped_by_risk",
    "planner_spawn_zero_tokens",
    "planner_inject_prompt_sidecar_written",
    "injected_prompt_sidecar_written",
    "injected_auditor_prompt_sidecar_written",
    "write_policy_allowed",
    "write_policy_wrapper_required",
    "write_policy_denied",
    "calibration_recovery_attempted",
    "sidecar_assert_skipped",
    "tdd_required_backfill_failed",
    "receipt_written",
    "receipt_missing",
    "stage_transition",
    "eventbus_handler",
    "maintenance_cycle",
    "scheduler_transition_refused",
    "scheduler_transition_race",
    "verify_signed_events_no_secret",
    "verify_signed_events_mismatch",
    "verify_signed_events_migration_attempted",
    "verify_signed_events_migration_failed",
    # Per-auditor ensemble routing decision (router.py build_audit_plan).
    # Diagnostic trace — no gate or state machine depends on this event.
    "auditor_ensemble_decision",
    # PreToolUse hook diagnostics (pre_tool_use.py).
    # These are forensic traces — no gate or state machine depends on them.
    "pre_tool_use_role_missing",
    "pre_tool_use_bash_check",
    "pre_tool_use_role_file_missing",
    # Audit-plan presence/shape diagnostics (pre_tool_use.py audit-plan
    # check, task-20260501-002). Emitted when audit-plan.json is missing
    # or malformed at PreToolUse time. Forensic trace only — no gate or
    # state machine depends on these events.
    "pre_tool_use_audit_plan_missing",
    "pre_tool_use_audit_plan_invalid",
    # Orchestrator adopted the stamped active-segment-role under a
    # non-isolating harness (Claude Code; pre_tool_use.py _subagent_isolation
    # = False). Forensic trace only — no gate depends on it.
    "orchestrator_role_adopted",
    # Grant binding trace (pre_tool_use.py): a subagent session consumed a
    # pending role grant. Forensic only — role resolution does not depend
    # on the event, and on Claude Code (shared session_id) it is not emitted.
    "role_grant_consumed",
    # ProjectIdSecurityError caught while resolving the persistent project
    # dir (daemon.py check_prevention_rules_bootstrap / rules_healed_check /
    # policy_path). Each site already handles the error inline (bail or
    # stub); the event is security observability only, with no consumer.
    "project_id_security_error",
    # Read-policy decision observability (read_policy gate, task-20260501-002).
    # Forensic trace recording allow/deny decisions for read attempts.
    # Observability only — no gate or state machine depends on these events.
    "read_policy_allowed",
    "read_policy_denied",
    # Risk level override observability (ctl._normalize_classification_payload).
    # Forensic trace — records when an observed_floor overrides the planner's
    # risk_level upward. No gate or state machine blocks on this event.
    "risk_level_upgrade_blocked",
    # Risk keyword match observability (task-20260508-009,
    # ctl._normalize_classification_payload). Emitted when keyword scan
    # matches but no risk-level upgrade occurs (planner already at or above
    # the keyword-only floor). Forensic trace only — no gate or state
    # machine blocks on this event.
    "risk_keyword_match_observed",
    # Inject-prompt size guardrail (router.cmd_planner_inject_prompt /
    # cmd_audit_inject_prompt). Non-blocking warning emitted when the
    # injected prompt exceeds _MAX_INJECTED_PROMPT_BYTES (100 KB). Forensic
    # trace for prompt-size regressions; no gate or state machine blocks.
    "planner_prompt_oversize",
    # Pre-existing forensic trace from lib_receipts.py auditor receipt
    # writer. Emitted when an auditor's spawn-log.jsonl is missing during
    # receipt write; observability only, no gate blocks on it.
    "audit_receipt_spawn_log_missing",
    # Envelope cross-check failure (task-20260506-002). Emitted by
    # _validate_final_envelope when the auditor's final-message envelope
    # JSON does not match the on-disk report (path/findings_count/
    # blocking_count) or fails parse/missing-keys/non-int validation.
    # The receipt write also raises ValueError, so the gate path is the
    # ValueError; this event is the forensic counterpart for diagnosing
    # which of the 9 envelope fields diverged. Observability only.
    "audit_envelope_mismatch",
    # Defense-in-depth content pairing (task-20260430-021). Emitted when
    # both spawn-log post entry AND on-disk audit-report file are present
    # at receipt time, recording the report file's sha256 alongside the
    # post entry's result_sha256 + result_excerpt_match flag. A reviewer
    # can spot mismatches forensically. Observability only — the binding
    # is intentionally not enforced because agent return text is prose
    # with JSON embedded; strict sha256 equality between the two would
    # always fail.
    "audit_receipt_content_paired",
    # Task-receipt-chain (task-20260503-001) — tamper-detection for the
    # full per-stage receipt sequence. Diagnostic-only — no gate blocks.
    "task_receipt_chain_extension_failed",
    "task_receipt_chain_write_failed",
    "task_receipt_chain_validated",
    # Circuit-breaker observe-only events (task-20260508-002). Emitted by
    # _dispatch_breaker_decision at every EXECUTION and AUDITING checkpoint.
    # circuit_breaker_observed is always logged regardless of BREAKER_ACTIVE.
    # circuit_breaker_downgrade_suggested is additionally logged when
    # BREAKER_ACTIVE=True and the decision action is "downgrade". Neither
    # event participates in receipts, stage transitions, or any gate —
    # they are diagnostic-only for operator review before activation.
    "circuit_breaker_observed",
    "circuit_breaker_downgrade_suggested",
})


__all__ = [
    "DIAGNOSTIC_ONLY_EVENTS",
    "log_event",
    "sign_event",
    "verify_signed_events",
]

_WRITE_ROLE = "eventbus"

_EVENT_SECRET_CACHE: dict[str, str] = {}


def _derive_per_task_secret(project_secret: str, task_id: str) -> str:
    """Derive a per-task HMAC secret via RFC 5869 HKDF-SHA256.

    Salt is the task_id; info is a versioned domain-separation label.
    Single expand block since output length (32 bytes) equals SHA256
    digest size.
    """
    prk = hmac.new(
        task_id.encode("utf-8"),
        project_secret.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    okm = hmac.new(
        prk,
        b"dynos-work/v1/per-task-event-secret" + b"\x01",
        hashlib.sha256,
    ).digest()
    return okm.hex()


def _resolve_event_secret(root: Path, *, task_id: str | None = None) -> str:
    """Resolve the HMAC-SHA256 secret used to sign events.

    Resolution order:
    (a) DYNOS_EVENT_SECRET env var — if non-empty, return it.
    (b) In-process cache keyed on str(root.resolve()).
    (c) Cache file at _persistent_project_dir(root)/'event-secret' with strict
        0o600 permission check.
    (d) Generate a fresh secrets.token_hex(32) and write atomically via
        os.open(O_WRONLY|O_CREAT|O_EXCL, 0o600), handle FileExistsError by
        re-reading (branch c).

    When ``task_id`` is a non-empty string the resolved project secret is
    further derived via :func:`_derive_per_task_secret` so each task's events
    are signed under an isolated HMAC namespace. The ``_EVENT_SECRET_CACHE``
    continues to cache the project secret only — never the per-task
    derivation — so cross-task derivations remain pure functions of the
    project secret.
    """
    env_secret = os.environ.get("DYNOS_EVENT_SECRET")
    if env_secret:
        if task_id:
            return _derive_per_task_secret(env_secret, task_id)
        return env_secret

    cache_key = str(root.resolve())

    if cache_key in _EVENT_SECRET_CACHE:
        secret = _EVENT_SECRET_CACHE[cache_key]
        if task_id:
            return _derive_per_task_secret(secret, task_id)
        return secret

    cache_path = _persistent_project_dir(root) / "event-secret"

    if cache_path.exists():
        st_mode = os.stat(cache_path).st_mode & 0o777
        if st_mode != 0o600:
            raise ValueError(f"event-secret perms unsafe: {cache_path}")
        secret = cache_path.read_text(encoding="utf-8").strip()
        _EVENT_SECRET_CACHE[cache_key] = secret
        if task_id:
            return _derive_per_task_secret(secret, task_id)
        return secret

    # Generate a fresh random secret for this project. The atomic O_EXCL
    # write below ensures only one process wins on first-run; concurrent
    # processes fall through the FileExistsError path and re-read.
    secret = secrets.token_hex(32)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(cache_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secret.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Another thread/process wrote the file concurrently — re-read it.
        st_mode = os.stat(cache_path).st_mode & 0o777
        if st_mode != 0o600:
            raise ValueError(f"event-secret perms unsafe: {cache_path}")
        secret = cache_path.read_text(encoding="utf-8").strip()

    _EVENT_SECRET_CACHE[cache_key] = secret
    if task_id:
        return _derive_per_task_secret(secret, task_id)
    return secret


def sign_event(payload: dict, secret: str, *, task_id: str | None = None) -> str:
    """Return the hex digest of HMAC-SHA256 over the canonical JSON of payload.

    The ``_sig`` key is excluded from the canonical serialization before
    computing the digest. The input payload is never mutated.

    Canonical form: ``json.dumps(filtered, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False, default=str)``.

    When ``task_id`` is a non-empty string the supplied ``secret`` is
    additionally derived via :func:`_derive_per_task_secret` before signing,
    yielding per-task namespace isolation. Empty string and ``None`` both
    fall back to the raw project secret (no derivation).
    """
    without_sig = {k: v for k, v in payload.items() if k != "_sig"}
    effective_secret = _derive_per_task_secret(secret, task_id) if task_id else secret
    canonical = json.dumps(
        without_sig,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    digest = hmac.new(
        effective_secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return digest


def verify_signed_events(
    task_dir: Path,
    secret: str,
    *,
    strict: bool = False,
) -> list[dict]:
    """Read task_dir/events.jsonl and return only records whose _sig is valid.

    Parameters
    ----------
    task_dir:
        Directory containing ``events.jsonl``.
    secret:
        HMAC-SHA256 secret used to verify signatures.
    strict:
        When ``True``, any record with a missing or mismatched ``_sig``
        raises ``ValueError``.  When ``False`` (default), such records are
        silently excluded from the result.

    Special-case: when ``secret`` is empty or ``None``:
    - ``strict=True``: returns an empty list immediately (no records can be
      verified without a secret).
    - ``strict=False``: logs a ``verify_signed_events_no_secret`` event and
      returns all parseable records unchanged (documented fallback).
    """
    if not secret:
        if strict:
            # Cannot verify anything without a secret — return empty list.
            return []
        # Non-strict fallback: read all parseable records first, then log event.
        # Reading before logging ensures the no_secret event itself is not
        # included in the returned records.
        records: list[dict] = []
        events_path = task_dir / "events.jsonl"
        if events_path.exists():
            # OSError propagates to caller.
            with events_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        log_event(
            task_dir.parent.parent,
            "verify_signed_events_no_secret",
            task=task_dir.name,
        )
        return records

    task_id = task_dir.name if task_dir.name else None
    per_task_secret = _derive_per_task_secret(secret, task_id) if task_id else secret

    events_path = task_dir / "events.jsonl"
    if not events_path.exists():
        return []

    # sec-007: only task-scoped directories may migrate. The global .dynos dir
    # and any non-task path uses dual-verify only (read), no rewrite.
    migration_eligible = (
        task_id is not None
        and task_id != ".dynos"
        and task_id.startswith("task-")
        and per_task_secret != secret
    )

    # sec-006: persistent migration failure sentinel disables retry storm.
    sentinel = task_dir / ".events-migration-disabled"
    if migration_eligible and sentinel.exists():
        migration_eligible = False

    # sec-003: refuse migration when path is a symlink — os.replace would
    # follow the symlink and clobber the target.
    if migration_eligible:
        try:
            if events_path.is_symlink() or task_dir.is_symlink():
                migration_eligible = False
        except OSError:
            migration_eligible = False

    # Capture original raw lines (sec-002) so a future rewrite preserves
    # malformed/mismatched/unparseable lines verbatim and only replaces
    # legacy-signed records.
    verified: list[dict] = []
    raw_lines: list[str] = []
    migration_line_set: set[int] = set()  # 1-indexed line numbers needing re-sign

    # sec-001: hold LOCK_EX on events.jsonl for the read+rewrite pair so
    # concurrent log_event appends (which take LOCK_EX in _append_jsonl)
    # cannot interleave between our read and our rewrite. Open r+ to allow
    # the optional in-place rewrite later.
    with events_path.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for n, raw in enumerate(f, start=1):
                raw_lines.append(raw if raw.endswith("\n") else raw + "\n")
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    if strict:
                        raise ValueError(
                            f"event signature invalid at line {n}: unparseable JSON"
                        )
                    continue
                if not isinstance(record, dict):
                    if strict:
                        raise ValueError(
                            f"event signature invalid at line {n}: not a JSON object"
                        )
                    continue

                stored_sig = record.get("_sig")
                if stored_sig is None:
                    reason = "missing_sig"
                    if strict:
                        raise ValueError(
                            f"event signature invalid at line {n}: {reason}"
                        )
                    log_event(
                        task_dir.parent.parent,
                        "verify_signed_events_mismatch",
                        task_dir=str(task_dir),
                        line_number=n,
                        reason=reason,
                    )
                    continue

                expected_per_task = sign_event(record, per_task_secret)
                if hmac.compare_digest(expected_per_task, stored_sig):
                    verified.append(record)
                    continue

                # Fall back to project-secret retry
                expected_project = sign_event(record, secret)
                if hmac.compare_digest(expected_project, stored_sig):
                    if strict:
                        raise ValueError(
                            f"event signature invalid at line {n}: legacy_project_secret_in_strict_mode"
                        )
                    verified.append(record)
                    migration_line_set.add(n)
                    continue

                # Neither matched → existing mismatch handling
                reason = "signature_mismatch"
                if strict:
                    raise ValueError(
                        f"event signature invalid at line {n}: {reason}"
                    )
                log_event(
                    task_dir.parent.parent,
                    "verify_signed_events_mismatch",
                    task_dir=str(task_dir),
                    line_number=n,
                    reason=reason,
                )

            # Migration: rewrite while still holding LOCK_EX (sec-001).
            if migration_line_set and migration_eligible:
                # sec-005: route through the same write-policy gate as
                # _append_jsonl uses for log_event appends.
                try:
                    require_write_allowed(
                        WriteAttempt(
                            role=_WRITE_ROLE,
                            task_dir=task_dir,
                            path=events_path,
                            operation="modify",
                            source=_WRITE_ROLE,
                        ),
                        capability_key=_get_capability_key(_WRITE_ROLE),
                        emit_event=False,
                    )
                except Exception as exc:
                    log_event(
                        task_dir.parent.parent,
                        "verify_signed_events_migration_failed",
                        task_dir=str(task_dir),
                        error=f"write_policy_denied: {exc}",
                    )
                else:
                    log_event(
                        task_dir.parent.parent,
                        "verify_signed_events_migration_attempted",
                        task_dir=str(task_dir),
                        migrated_count=len(migration_line_set),
                    )
                    tmp_path: str | None = None
                    try:
                        # Capture original mode for restoration after replace.
                        try:
                            original_mode = events_path.stat().st_mode & 0o777
                        except OSError:
                            original_mode = 0o600
                        with tempfile.NamedTemporaryFile(
                            mode="w",
                            dir=task_dir,
                            delete=False,
                            suffix=".tmp",
                            encoding="utf-8",
                        ) as tmp:
                            tmp_path = tmp.name
                            # sec-002: rewrite using ORIGINAL raw lines, only
                            # replacing migrated records. Mismatched/missing-
                            # sig/unparseable lines are preserved verbatim.
                            for line_no, line in enumerate(raw_lines, start=1):
                                if line_no in migration_line_set:
                                    try:
                                        original = json.loads(line.strip())
                                        if isinstance(original, dict):
                                            original["_sig"] = sign_event(
                                                original, per_task_secret
                                            )
                                            tmp.write(
                                                json.dumps(
                                                    original,
                                                    default=str,
                                                    ensure_ascii=False,
                                                )
                                                + "\n"
                                            )
                                            continue
                                    except (json.JSONDecodeError, ValueError):
                                        pass
                                # Preserve verbatim.
                                tmp.write(line)
                            tmp.flush()
                            os.fsync(tmp.fileno())
                        # sec-003 re-check: between our earlier check and now,
                        # ensure events_path still isn't a symlink.
                        if events_path.is_symlink():
                            raise OSError(
                                "events_path became a symlink mid-flight"
                            )
                        os.replace(tmp_path, events_path)
                        try:
                            os.chmod(events_path, original_mode)
                        except OSError:
                            pass
                        tmp_path = None  # successfully renamed; nothing to clean
                    except Exception as exc:
                        log_event(
                            task_dir.parent.parent,
                            "verify_signed_events_migration_failed",
                            task_dir=str(task_dir),
                            error=str(exc),
                        )
                        # sec-006: persistent failure → write sentinel to
                        # disable retry. Best-effort.
                        try:
                            sentinel.write_text(
                                json.dumps(
                                    {
                                        "ts": now_iso(),
                                        "error": str(exc),
                                    }
                                )
                            )
                            os.chmod(sentinel, 0o600)
                        except OSError:
                            pass
                    finally:
                        # sec-004: best-effort cleanup of leaked tmpfile.
                        if tmp_path is not None:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return verified


def _append_jsonl(path: Path, line: str, *, attempt: WriteAttempt, capability_key: object) -> None:
    """Thread-safe append a single line to a JSONL file."""
    require_write_allowed(
        attempt,
        capability_key=capability_key,
        emit_event=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def log_event(root: Path, event_type: str, *, task: str | None = None, **payload: Any) -> None:
    """Append one structured JSONL event to the task's events.jsonl.

    If `task` is provided and the task directory exists, writes to
    .dynos/{task}/events.jsonl (task-scoped). Otherwise writes to
    .dynos/events.jsonl (global fallback for daemon/system events).

    Args:
        root: Project root directory (contains .dynos/).
        event_type: Event name (e.g. "stage_transition", "router_model_decision").
        task: Optional task ID. When provided, events go to that task's log.
        **payload: Arbitrary key-value pairs merged into the JSON line.
    """
    try:
        record: dict[str, Any] = {"ts": now_iso(), "event": event_type}
        if task is not None:
            record["task"] = task
        record.update(payload)

        try:
            secret = _resolve_event_secret(root, task_id=task)
            record["_sig"] = sign_event(record, secret)
        except Exception as exc:
            print(
                f"[dynos-log] WARNING: _resolve_event_secret failed: {exc}",
                file=sys.stderr,
            )

        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        # Task-scoped log when task ID is known and dir exists
        if task is not None:
            task_dir = root / ".dynos" / task
            if task_dir.is_dir():
                _append_jsonl(
                    task_dir / "events.jsonl",
                    line,
                    attempt=WriteAttempt(
                        role=_WRITE_ROLE,
                        task_dir=task_dir,
                        path=task_dir / "events.jsonl",
                        operation="modify" if (task_dir / "events.jsonl").exists() else "create",
                        source=_WRITE_ROLE,
                    ),
                    capability_key=_get_capability_key(_WRITE_ROLE),
                )
                return

        # Global fallback
        global_path = root / ".dynos" / "events.jsonl"
        _append_jsonl(
            global_path,
            line,
            attempt=WriteAttempt(
                role="system",
                task_dir=None,
                path=global_path,
                operation="modify" if global_path.exists() else "create",
                source="system",
            ),
            capability_key=_get_capability_key("system"),
        )
    except Exception as exc:
        print(f"[dynos-log] WARNING: log_event failed: {exc}", file=sys.stderr)
