"""Lifecycle-stage receipt writers for the dynos-work pipeline.

Each function in this module writes a structured JSON receipt for one
stage of the task lifecycle (search, spec validation, plan validation,
executor routing/done, audit routing/done, retrospective, post-completion).
Function bodies are copied byte-for-byte from ``hooks/lib_receipts.py``
during the receipts package split — see ``hooks/receipts/__init__.py``
for the public re-export surface.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from lib_core import _persistent_project_dir
import lib_ensemble as _lib_ensemble
import lib_host as _lib_host
import lib_models as _lib_models
from lib_log import log_event, verify_signed_events
from lib_validate import require_nonblank_str

from .core import (
    INJECTED_AUDITOR_PROMPTS_DIR,
    INJECTED_PROMPTS_DIR,
    _record_tokens,
    hash_file,
    read_receipt,
    validate_receipt_model_field,
    write_receipt,
)


def _hash_artifact(path: Path) -> str | None:
    """Return sha256 hex of file content, or None if the file is missing."""
    try:
        return hash_file(path)
    except (FileNotFoundError, OSError):
        return None


def _compute_spawn_host_fields(
    task_dir: Path,
    model_used: str | None,
    tier_hint: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Self-compute (host, tier, resolved_model) for a spawn receipt.

    ``host`` is read from the persisted control-plane.json at the project
    root (``task_dir.parent.parent``). If no persisted host is available,
    ``lib_host.detect_host()`` is used as fallback. The caller never
    supplies the host — this is the writer-derived, anti-forgery pattern.

    ``resolved_model`` is normalised from ``model_used`` (None stays None).

    ``tier`` is derived from ``resolved_model`` via
    ``lib_models.model_to_tier``; when ``resolved_model`` is None the
    ``tier_hint`` (e.g. from sidecar naming context) is used if provided,
    otherwise None.

    Raises ``ValueError`` when the resolved_model/host combination is
    invalid (anti-forgery: claiming a claude-tier model under codex must fail at write
    time).
    """
    root = task_dir.parent.parent
    cp_path = _persistent_project_dir(root) / "control-plane.json"
    host = _lib_host.get_persisted_host(cp_path) or _lib_host.detect_host()

    resolved_model: str | None = model_used  # None stays None

    # Validate the model/host combination at write time.
    # Build a minimal receipt dict for the validator.
    probe = {"resolved_model": resolved_model}
    if not validate_receipt_model_field(probe, host):
        raise ValueError(
            f"receipt write refused: resolved_model={resolved_model!r} is "
            f"invalid for host={host!r} (anti-forgery check at write time)"
        )

    # Derive tier from model; fall back to tier_hint when model is None.
    if resolved_model is not None:
        tier: str | None = _lib_models.model_to_tier(resolved_model)
    else:
        tier = tier_hint

    return host, tier, resolved_model


def receipt_search_conducted(
    task_dir: Path,
    *,
    query: str,
    search_used: bool = True,
    urls_consulted: list[str] | None = None,
    findings_summary: str | None = None,
) -> Path:
    """Write receipt proving external research was conducted in response to gate.

    Called by ``ctl.py write-search-receipt`` after the executor performs the
    search recommended by ``external-solution-gate.json``.  ``run-spec-ready``
    asserts this receipt exists before advancing to SPEC_REVIEW when the gate
    wrote ``search_recommended: true``.

    ``query`` is the search string actually used (non-empty, caller-supplied).
    ``search_used`` must be ``True``; passing ``False`` raises ``ValueError``
    so a caller cannot write a vacuous "search conducted" receipt without having
    done any search.

    ``urls_consulted``: list of URLs consulted during the search (AC 6/AC 10).
    ``findings_summary``: text summary of findings (AC 6/AC 10).
    Both are optional at the receipt-writer level; presence and content
    validation occurs in cmd_write_search_receipt (AC 6).
    """
    require_nonblank_str(query, field_name="receipt_search_conducted: query")
    if not search_used:
        raise ValueError(
            "receipt_search_conducted: search_used must be True — "
            "do not write a search-conducted receipt if no search was performed"
        )
    gate_path = task_dir / "external-solution-gate.json"
    gate_sha256 = _hash_artifact(gate_path)
    extra: dict = {}
    if urls_consulted is not None:
        extra["urls_consulted"] = urls_consulted
    if findings_summary is not None:
        extra["findings_summary"] = findings_summary
    return write_receipt(
        task_dir,
        "search-conducted",
        query=query,
        search_used=True,
        gate_sha256=gate_sha256,
        **extra,
    )


def receipt_spec_validated(task_dir: Path, **_legacy: Any) -> Path:
    """Write receipt proving spec.md passed validation.

    Self-computes ``criteria_count`` and ``spec_sha256`` from
    ``task_dir/spec.md`` (v4 contract). Callers no longer supply these
    fields — any legacy kwarg (``criteria_count`` or ``spec_sha256``)
    raises ``TypeError`` so a stale integration cannot silently ship a
    receipt whose counts/hash disagree with the on-disk spec.

    Payload includes {criteria_count, spec_sha256, valid: true}.
    """
    if _legacy:
        raise TypeError(
            "receipt_spec_validated no longer accepts caller-supplied "
            f"{sorted(_legacy)} — counts and hash are now self-computed "
            "from task_dir/spec.md"
        )
    spec_path = task_dir / "spec.md"
    if not spec_path.exists():
        raise ValueError(
            f"receipt_spec_validated: spec.md missing at {spec_path}"
        )
    # Deferred import to avoid an lib_validate <-> lib_receipts cycle
    # (lib_validate.compute_reward imports read_receipt from here).
    from lib_validate import parse_acceptance_criteria  # noqa: PLC0415

    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"receipt_spec_validated: cannot read spec.md at {spec_path}: {exc}"
        ) from exc
    criteria_count = len(parse_acceptance_criteria(spec_text))
    spec_sha256 = hash_file(spec_path)
    return write_receipt(
        task_dir,
        "spec-validated",
        criteria_count=criteria_count,
        spec_sha256=spec_sha256,
    )


def receipt_plan_validated(
    task_dir: Path,
    validation_passed_override: bool | None = None,
    run_gap: bool = True,
    **_legacy: Any,
) -> Path:
    """Write receipt proving plan + execution graph passed validation.

    Self-computes ``segment_count``, ``criteria_coverage``, and
    ``validation_passed`` (v4 contract) by invoking
    ``lib_validate.validate_task_artifacts`` and reading
    ``execution-graph.json``. Callers no longer supply these fields —
    passing any of ``segment_count``, ``criteria_coverage``, or
    ``validation_passed`` raises ``TypeError``.

    ``validation_passed`` is derived as ``len(errors) == 0``. As an
    escape hatch for tests, ``validation_passed_override`` is honoured
    IFF the environment variable ``DYNOS_ALLOW_TEST_OVERRIDE == "1"``;
    otherwise the override is ignored and the computed value wins.

    Captures content hashes of spec.md, plan.md, and execution-graph.json
    so downstream consumers (e.g. execute preflight) can short-circuit
    re-validation when none of the artifacts have changed since the
    receipt was written.
    """
    if _legacy:
        raise TypeError(
            "receipt_plan_validated no longer accepts caller-supplied "
            f"{sorted(_legacy)} — segment_count, criteria_coverage, and "
            "validation_passed are self-computed from task_dir artifacts"
        )

    # Deferred import: validate_task_artifacts lives in lib_validate,
    # which itself imports read_receipt from this module. Importing at
    # call time keeps the module-load graph acyclic.
    from lib_validate import validate_task_artifacts  # noqa: PLC0415

    errors = validate_task_artifacts(task_dir, run_gap=run_gap)
    computed_passed = not errors

    # Honour the test override only when the env knob is explicitly set.
    if (
        validation_passed_override is not None
        and os.environ.get("DYNOS_ALLOW_TEST_OVERRIDE") == "1"
    ):
        validation_passed = bool(validation_passed_override)
    else:
        validation_passed = computed_passed

    # Self-compute segment_count + criteria_coverage from the graph.
    segment_count = 0
    criteria_coverage: list[int] = []
    graph_path = task_dir / "execution-graph.json"
    if graph_path.exists():
        try:
            with graph_path.open("r", encoding="utf-8") as f:
                graph = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"receipt_plan_validated: cannot parse execution-graph.json "
                f"at {graph_path}: {exc}"
            ) from exc
        if isinstance(graph, dict):
            segments = graph.get("segments", [])
            if isinstance(segments, list):
                segment_count = len(segments)
                covered: set[int] = set()
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    for cid in seg.get("acceptance_criteria", []) or []:
                        try:
                            covered.add(int(cid))
                        except (TypeError, ValueError):
                            continue
                criteria_coverage = sorted(covered)

    artifact_hashes = {
        "spec.md": _hash_artifact(task_dir / "spec.md"),
        "plan.md": _hash_artifact(task_dir / "plan.md"),
        "execution-graph.json": _hash_artifact(task_dir / "execution-graph.json"),
    }
    return write_receipt(
        task_dir,
        "plan-validated",
        segment_count=segment_count,
        criteria_coverage=criteria_coverage,
        validation_passed=validation_passed,
        artifact_hashes=artifact_hashes,
    )


def plan_validated_receipt_matches(task_dir: Path) -> "bool | str":
    """Return True if a plan-validated receipt exists AND its captured
    artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing or malformed (e.g. an
    older receipt without an ``artifact_hashes`` payload). Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk. Callers distinguish these
    three outcomes to surface drift-vs-missing distinctly.
    """
    receipt = read_receipt(task_dir, "plan-validated")
    if receipt is None or not receipt.get("validation_passed", False):
        return False
    captured = receipt.get("artifact_hashes")
    if not isinstance(captured, dict):
        # Old receipts written before hashes existed — treat as drift,
        # forcing re-validation. Safer than assuming they match.
        return False
    for name in ("spec.md", "plan.md", "execution-graph.json"):
        current = _hash_artifact(task_dir / name)
        if current != captured.get(name):
            return f"{name} hash drift"
    return True


def plan_audit_matches(task_dir: Path) -> "bool | str":
    """Return ``True`` if a ``plan-audit-check`` receipt exists AND its
    captured artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing entirely. Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk since the audit ran. Callers
    distinguish the three outcomes to surface drift-vs-missing distinctly
    at the PLAN_AUDIT exit gate.

    Receipts written before the hash-binding landed (pre-F2) do not carry
    ``spec_sha256``/``plan_sha256``/``graph_sha256`` fields. Such receipts
    are treated as ``False`` (missing) so the gate forces a fresh audit
    rather than silently trusting a legacy payload.
    """
    receipt = read_receipt(task_dir, "plan-audit-check")
    if receipt is None:
        return False
    # Legacy receipts (pre-F2) lacked the three hash fields. Without
    # hashes we cannot verify freshness — behave like missing.
    expected_spec = receipt.get("spec_sha256")
    expected_plan = receipt.get("plan_sha256")
    expected_graph = receipt.get("graph_sha256")
    if not (
        isinstance(expected_spec, str) and expected_spec
        and isinstance(expected_plan, str) and expected_plan
        and isinstance(expected_graph, str) and expected_graph
    ):
        return False
    # Hash each artifact on disk. Missing files count as drift with a
    # descriptive string (the audit was computed over a file that is now
    # absent — clearly not fresh).
    current_spec = _hash_artifact(task_dir / "spec.md")
    if current_spec != expected_spec:
        return "spec.md hash drift"
    current_plan = _hash_artifact(task_dir / "plan.md")
    if current_plan != expected_plan:
        return "plan.md hash drift"
    current_graph = _hash_artifact(task_dir / "execution-graph.json")
    if current_graph != expected_graph:
        return "execution-graph.json hash drift"
    return True


def receipt_executor_routing(
    task_dir: Path,
    segments: list[dict],
) -> Path:
    """Write receipt proving all executor routing decisions were made.

    task-20260506-001 (AC-8): each segment dict may carry a ``tool_budget`` int
    field computed by ``hooks/router.py::build_executor_plan`` via
    ``hooks/lib_tool_budget.py::compute_segment_budget``. This writer accepts
    the segments list verbatim, so ``tool_budget`` flows through transparently;
    no schema change is required at this writer's signature. Older receipts
    written before the field existed simply lack it — the inject-prompt cache
    miss path in router.py recomputes on load to support in-flight migration.
    """
    return write_receipt(
        task_dir,
        "executor-routing",
        segments=segments,
    )


def receipt_executor_done(
    task_dir: Path,
    segment_id: str,
    executor_type: str,
    model_used: str | None,
    injected_prompt_sha256: str,
    agent_name: str | None,
    evidence_path: str | None,
    tokens_used: int | None,
    *,
    diff_verified_files: list[str],
    no_op_justified: bool,
) -> Path:
    """Write receipt proving an executor segment completed.

    Asserts the per-segment injected prompt sidecar at
    ``task_dir / "receipts" / "_injected-prompts" / f"{segment_id}.sha256"``
    exists and matches `injected_prompt_sha256`. Raises:

    - ``ValueError("... injected_prompt_sha256 sidecar missing ...")`` if
      the sidecar file does not exist.
    - ``ValueError("... injected_prompt_sha256 mismatch ...")`` if the
      sidecar contents do not match the supplied digest.

    Also records token usage to token-usage.json — the only reliable path
    for token recording since receipts are gated.

    ``diff_verified_files`` (keyword-only, required): list of file paths the
    executor verified as changed. Must be a list of strings; raises
    ``ValueError`` on type violation.

    ``no_op_justified`` (keyword-only, required): whether the executor
    determined the segment was legitimately a no-op. Must be a bool; raises
    ``ValueError`` on type violation.
    """
    require_nonblank_str(injected_prompt_sha256, field_name="injected_prompt_sha256")

    if not isinstance(diff_verified_files, list) or not all(
        isinstance(f, str) for f in diff_verified_files
    ):
        raise ValueError("diff_verified_files must be a list of strings")
    if not isinstance(no_op_justified, bool):
        raise ValueError("no_op_justified must be a bool")

    sidecar_dir = task_dir / "receipts" / INJECTED_PROMPTS_DIR
    sidecar_file = sidecar_dir / f"{segment_id}.sha256"

    if not sidecar_file.exists():
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"at {sidecar_file}"
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"(unreadable {sidecar_file}: {e})"
        ) from e
    if on_disk != injected_prompt_sha256:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 mismatch "
            f"(sidecar={on_disk!r}, payload={injected_prompt_sha256!r})"
        )

    # Record tokens deterministically
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"{executor_type}-{segment_id}", model_used or "default", tokens_used)

    # Self-compute spawn identity fields (anti-forgery: writer-derived, not caller-supplied).
    host, tier, resolved_model = _compute_spawn_host_fields(task_dir, model_used)

    return write_receipt(
        task_dir,
        f"executor-{segment_id}",
        segment_id=segment_id,
        executor_type=executor_type,
        model_used=model_used,
        injected_prompt_sha256=injected_prompt_sha256,
        agent_name=agent_name,
        evidence_path=evidence_path,
        tokens_used=tokens_used,
        diff_verified_files=diff_verified_files,
        no_op_justified=no_op_justified,
        host=host,
        tier=tier,
        resolved_model=resolved_model,
    )


def receipt_audit_routing(
    task_dir: Path,
    auditors: list[dict],
) -> Path:
    """Write receipt proving all auditor routing decisions were made.

    Each entry in `auditors` MUST include the keys:
      - injected_agent_sha256: str | None
          (None means routing was recorded before prompt injection; the
          per-auditor audit receipt enforces the sidecar digest for actual
          non-generic spawns)
      - agent_path: str | None
    Callers are responsible for populating these; this writer enforces
    presence so downstream consumers can rely on the schema.
    """
    if not isinstance(auditors, list):
        raise ValueError("auditors must be a list")
    # Normalize entries before write: skip entries don't need injection fields
    # (they weren't injected), so we fill missing keys with None rather than
    # forcing callers to pass boilerplate. Spawn entries still require the
    # fields explicitly (so a missing key is a schema violation, not a typo).
    normalized: list[dict] = []
    for idx, entry in enumerate(auditors):
        if not isinstance(entry, dict):
            raise ValueError(f"auditors[{idx}] must be a dict")
        action = entry.get("action")
        if action == "skip":
            # Skip entries: default the injection fields to None if absent.
            entry = {
                **entry,
                "injected_agent_sha256": entry.get("injected_agent_sha256"),
                "agent_path": entry.get("agent_path"),
            }
        else:
            # Spawn (or unknown) entries: require explicit keys (may be None).
            if "injected_agent_sha256" not in entry:
                raise ValueError(
                    f"auditors[{idx}] missing required key 'injected_agent_sha256' "
                    f"(must be str or None)"
                )
            if "agent_path" not in entry:
                raise ValueError(
                    f"auditors[{idx}] missing required key 'agent_path' "
                    f"(must be str or None)"
                )
        route_mode = entry.get("route_mode")
        injected = entry.get("injected_agent_sha256")
        if injected is not None and not isinstance(injected, str):
            raise ValueError(
                f"auditors[{idx}] injected_agent_sha256 must be str or None"
            )
        agent_path = entry.get("agent_path")
        if agent_path is not None and not isinstance(agent_path, str):
            raise ValueError(
                f"auditors[{idx}] agent_path must be str or None"
            )
        normalized.append(entry)

    return write_receipt(
        task_dir,
        "audit-routing",
        auditors=normalized,
    )


def _validate_audit_done_args(
    route_mode: str,
    injected_agent_sha256: str | None,
    agent_path: str | None,
    report_path: str | None,
    finding_count: int | None,
    blocking_count: int | None,
    auditor_name: str,
    ensemble_context: bool,
) -> tuple[int | None, int | None]:
    """Validate ``receipt_audit_done`` arguments.

    Mirrors the pre-extraction validation block byte-equally. Returns the
    possibly-normalised ``(finding_count, blocking_count)`` pair (the
    "no report -> default to zero" rule). Raises ValueError or TypeError
    with identical existing messages on any violation.
    """
    require_nonblank_str(route_mode, field_name="route_mode")
    if injected_agent_sha256 is None and route_mode != "generic":
        raise ValueError(
            f"injected_agent_sha256 may be None only when route_mode=='generic' "
            f"(got route_mode={route_mode!r})"
        )
    if injected_agent_sha256 is not None and not isinstance(injected_agent_sha256, str):
        raise ValueError("injected_agent_sha256 must be str or None")
    if agent_path is not None and not isinstance(agent_path, str):
        raise ValueError("agent_path must be str or None")

    # AC 17: learned/ensemble auditors require a real report file.
    if route_mode == "learned" or ensemble_context:
        if not isinstance(report_path, str) or not report_path:
            raise ValueError(
                "report_path required for learned/ensemble auditors "
                f"(auditor={auditor_name!r}, route_mode={route_mode!r}, "
                f"ensemble_context={ensemble_context!r})"
            )
        if not Path(report_path).exists():
            raise ValueError(
                "report_path required for learned/ensemble auditors "
                f"(missing file at {report_path} for auditor "
                f"{auditor_name!r}, route_mode={route_mode!r}, "
                f"ensemble_context={ensemble_context!r})"
            )

    if (finding_count is None) ^ (blocking_count is None):
        raise TypeError(
            "finding_count and blocking_count must be provided together "
            "or both omitted"
        )

    # MA-005 hardening: when report_path is None we have no way to verify
    # caller-supplied finding_count / blocking_count. Any non-zero count
    # supplied without a corresponding report file is caller-attested and
    # exactly the TOCTOU pattern SEC-004 closed for receipt_plan_audit.
    # Rule: `report_path=None` demands both counts be zero. If the auditor
    # found anything, they must materialise a report file and pass its
    # path. Learned / ensemble callers already hit the AC 17 guard above
    # (report_path REQUIRED). This rule catches the remaining generic
    # case and legacy voting-harness callers that pass None+counts>0.
    if not (isinstance(report_path, str) and report_path):
        if finding_count is None:
            finding_count = 0
            blocking_count = 0
        if finding_count != 0 or blocking_count != 0:
            raise ValueError(
                f"audit-{auditor_name}: report_path is None but "
                f"finding_count={finding_count}, blocking_count={blocking_count}. "
                "Caller-attested non-zero counts are forbidden — "
                "materialise a report file and pass its path, or pass "
                "(0, 0) and None together."
            )

    return finding_count, blocking_count


def _assert_sidecar_match(
    task_dir: Path,
    auditor_name: str,
    model_used: str | None,
    injected_agent_sha256: str,
    tier: str | None = None,
) -> None:
    """Assert per-(auditor, model) injected-prompt sidecar matches.

    Caller MUST only invoke when ``injected_agent_sha256 is not None``.
    Raises ValueError with byte-identical messages on missing,
    unreadable, or mismatched sidecar.

    D-14 tier-name substitution: when ``model_used`` is ``None`` (e.g. for a
    Codex host spawn where no model is resolved), the sidecar filename uses
    ``tier`` as the discriminator instead of the literal string ``"None"``.
    This produces ``{auditor_name}-{tier}.sha256`` (e.g.
    ``security-auditor-deep.sha256``) which passes ``_SAFE_AGENT_RE`` and is
    meaningful to reviewers. If both ``model_used`` and ``tier`` are None, the
    fallback key ``"unknown"`` is used to avoid a ``-None.sha256`` filename.
    """
    # D-14: use tier name when model is None to avoid *-None.sha256 filenames.
    if model_used is None:
        sidecar_key = tier if (tier and tier != "None") else "unknown"
    else:
        sidecar_key = model_used
    sidecar_file = (
        task_dir / "receipts" / INJECTED_AUDITOR_PROMPTS_DIR
        / f"{auditor_name}-{sidecar_key}.sha256"
    )
    if not sidecar_file.exists():
        raise ValueError(
            f"audit-{auditor_name}: injected auditor prompt sidecar "
            f"missing at {sidecar_file}"
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"audit-{auditor_name}: injected auditor prompt sidecar "
            f"unreadable at {sidecar_file}: {e}"
        ) from e
    if on_disk != injected_agent_sha256:
        raise ValueError(
            f"audit-{auditor_name}: injected_agent_sha256 mismatch "
            f"(sidecar={on_disk!r}, payload={injected_agent_sha256!r})"
        )


def _validate_final_envelope(
    task_dir: Path,
    auditor_name: str,
    route_mode: str,
    report_path: str | None,
    final_envelope: str | None,
) -> None:
    """Validate the auditor's final-message JSON envelope.

    Implements AC 5 sub-rules a–i:
      a. generic + None  -> silent skip.
      b. non-generic + None -> emit ``audit_envelope_mismatch`` event,
         raise ValueError naming auditor + non-generic route_mode.
      c. final_envelope not None -> ``json.loads`` with NO preprocessing.
         JSONDecodeError -> emit + raise.
      d. Result must be a dict containing keys ``report_path``,
         ``findings_count``, ``blocking_count``.
      e. ``findings_count`` and ``blocking_count`` must be int (booleans
         excluded via ``isinstance(v, int) and not isinstance(v, bool)``).
      f. STRICT string equality between envelope.report_path and the
         caller-supplied ``report_path`` arg (no Path.resolve()).
      g. Read on-disk report; assert envelope.findings_count ==
         len(findings).
      h. Assert envelope.blocking_count == count of findings whose
         ``blocking is True`` (mirrors ``_build_audit_receipt_payload``).
      i. Every emitted ``audit_envelope_mismatch`` event carries exactly
         9 fields: task, auditor_name, route_mode,
         expected_report_path, envelope_report_path,
         expected_findings_count, envelope_findings_count,
         expected_blocking_count, envelope_blocking_count. Each
         ``log_event`` call wrapped in ``try/except Exception: pass``.
    """
    # Sub-rule (a): generic + None — silent skip.
    if route_mode == "generic" and final_envelope is None:
        return

    def _emit(
        envelope_report_path: Any,
        expected_findings_count: int | None,
        envelope_findings_count: Any,
        expected_blocking_count: int | None,
        envelope_blocking_count: Any,
    ) -> None:
        try:
            log_event(
                task_dir.parent.parent,
                "audit_envelope_mismatch",
                task=task_dir.name,
                auditor_name=auditor_name,
                route_mode=route_mode,
                expected_report_path=report_path,
                envelope_report_path=envelope_report_path,
                expected_findings_count=expected_findings_count,
                envelope_findings_count=envelope_findings_count,
                expected_blocking_count=expected_blocking_count,
                envelope_blocking_count=envelope_blocking_count,
            )
        except Exception:
            pass

    # Sub-rule (b): non-generic + None — required.
    if final_envelope is None:
        _emit(None, None, None, None, None)
        raise ValueError(
            f"audit-{auditor_name}: final_envelope is required for "
            f"non-generic route_mode (got route_mode={route_mode!r}); "
            f"the auditor's final message must be the bare JSON envelope."
        )

    # Sub-rule (c): json.loads with NO preprocessing.
    try:
        envelope = json.loads(final_envelope)
    except json.JSONDecodeError as exc:
        _emit(None, None, None, None, None)
        raise ValueError(
            f"audit-{auditor_name}: final_envelope is not valid JSON "
            f"(route_mode={route_mode!r}): {exc}. The envelope must be a "
            f"single bare JSON line — no markdown fences, no preprocessing."
        ) from exc

    # Sub-rule (d): must be dict with required keys.
    if not isinstance(envelope, dict):
        _emit(None, None, None, None, None)
        raise ValueError(
            f"audit-{auditor_name}: final_envelope must decode to a JSON "
            f"object (got {type(envelope).__name__})."
        )

    required_keys = ("report_path", "findings_count", "blocking_count")
    missing = [k for k in required_keys if k not in envelope]
    if missing:
        _emit(
            envelope.get("report_path"),
            None,
            envelope.get("findings_count"),
            None,
            envelope.get("blocking_count"),
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope missing required "
            f"key(s) {missing!r}; envelope must contain "
            f"report_path, findings_count, blocking_count."
        )

    env_report_path = envelope.get("report_path")
    env_findings_count = envelope.get("findings_count")
    env_blocking_count = envelope.get("blocking_count")

    # Sub-rule (e): integer (not bool) check on counts.
    if not (isinstance(env_findings_count, int) and not isinstance(env_findings_count, bool)):
        _emit(
            env_report_path,
            None,
            env_findings_count,
            None,
            env_blocking_count,
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope.findings_count must "
            f"be int, got {type(env_findings_count).__name__} "
            f"({env_findings_count!r})."
        )
    if not (isinstance(env_blocking_count, int) and not isinstance(env_blocking_count, bool)):
        _emit(
            env_report_path,
            None,
            env_findings_count,
            None,
            env_blocking_count,
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope.blocking_count must "
            f"be int, got {type(env_blocking_count).__name__} "
            f"({env_blocking_count!r})."
        )

    # Sub-rule (f): STRICT string equality on report_path.
    if env_report_path != report_path:
        _emit(
            env_report_path,
            None,
            env_findings_count,
            None,
            env_blocking_count,
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope.report_path mismatch "
            f"(envelope={env_report_path!r}, expected={report_path!r}). "
            f"Strict string equality required — no path normalization."
        )

    # Sub-rules (g)/(h): cross-check counts against on-disk report.
    # SEC-001 (task-20260506-002): mirror the SEC-004 path-traversal guard
    # used by _build_audit_receipt_payload. The validator must not open a
    # path that escapes task_dir even when env_report_path == report_path
    # (a compromised orchestrator could pass a traversal path that matches
    # itself in both fields).
    # IR 13 (task-20260506-002): missing/unreadable report file is a hard
    # failure for any non-generic auditor or any caller that supplied a
    # report_path. Silent skip on OSError/JSONDecodeError would let an
    # auditor claim arbitrary counts for a report that was never written —
    # exactly the upstream forgery vector this defense closes.
    # SEC-002 (task-20260506-002): error messages must NOT echo
    # ``actual_finding_count`` / ``actual_blocking_count``; the
    # ``audit_envelope_mismatch`` event already records both values in
    # structured form for forensic review. Exception text is propagated to
    # stderr where a hostile orchestrator could read it back to discover
    # the true counts and reconstruct a passing forged envelope.
    actual_finding_count: int | None = None
    actual_blocking_count: int | None = None
    if isinstance(report_path, str) and report_path:
        report_file = Path(report_path)
        try:
            resolved_report = report_file.resolve()
            resolved_task = Path(task_dir).resolve()
            resolved_report.relative_to(resolved_task)
        except (ValueError, OSError) as exc:
            _emit(env_report_path, None, env_findings_count, None, env_blocking_count)
            raise ValueError(
                f"audit-{auditor_name}: final_envelope.report_path must be "
                f"inside task_dir ({resolved_task}); got {report_path!r}"
            ) from exc
        try:
            with report_file.open("r", encoding="utf-8") as fh:
                report_payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _emit(env_report_path, None, env_findings_count, None, env_blocking_count)
            raise ValueError(
                f"audit-{auditor_name}: report file at {report_path!r} is "
                f"missing or unreadable; the report must exist on disk "
                f"before the envelope can be validated. Cause: "
                f"{type(exc).__name__}."
            ) from exc
        findings = report_payload.get("findings", []) if isinstance(report_payload, dict) else []
        if not isinstance(findings, list):
            findings = []
        actual_finding_count = len(findings)
        actual_blocking_count = sum(
            1 for f in findings
            if isinstance(f, dict) and f.get("blocking") is True
        )

    if actual_finding_count is not None and env_findings_count != actual_finding_count:
        _emit(
            env_report_path,
            actual_finding_count,
            env_findings_count,
            actual_blocking_count,
            env_blocking_count,
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope.findings_count does "
            f"not match the on-disk report. See audit_envelope_mismatch "
            f"event for the structured field comparison."
        )

    if actual_blocking_count is not None and env_blocking_count != actual_blocking_count:
        _emit(
            env_report_path,
            actual_finding_count,
            env_findings_count,
            actual_blocking_count,
            env_blocking_count,
        )
        raise ValueError(
            f"audit-{auditor_name}: final_envelope.blocking_count does "
            f"not match the on-disk report. See audit_envelope_mismatch "
            f"event for the structured field comparison."
        )


def _build_audit_receipt_payload(
    task_dir: Path,
    auditor_name: str,
    report_path: str | None,
    finding_count: int | None,
    blocking_count: int | None,
) -> tuple[int | None, int | None, str | None]:
    """Self-verify the report (if present) and derive payload counts.

    Returns ``(finding_count, blocking_count, report_sha256)``. When
    ``report_path`` is a non-null string referring to an existing JSON
    file the report is parsed, caller-supplied counts cross-checked, and
    a sha256 of the file attached. Raises ValueError with byte-identical
    messages on any mismatch / unreadable file / path-escape.
    """
    # AC 2 — self-verify block. When ``report_path`` is a non-null string
    # referring to an existing JSON file, cross-check caller-supplied
    # finding_count / blocking_count against the actual contents of the
    # report and attach a sha256 of the file. Any mismatch aborts the
    # write with ValueError naming the mismatched field and both values.
    #
    # When ``report_path`` is None OR the file does not exist, the MA-005
    # rule above already demands counts=(0, 0). The skip below is now
    # semantically: "no report means literal zero findings."
    report_sha256: str | None = None
    if isinstance(report_path, str) and report_path:
        report_file = Path(report_path)
        # SEC-004 fix: reject report_path that escapes task_dir. A compromised
        # orchestrator could otherwise point report_path at arbitrary files.
        try:
            resolved_report = report_file.resolve()
            resolved_task = Path(task_dir).resolve()
            resolved_report.relative_to(resolved_task)
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"audit-{auditor_name}: report_path must be inside task_dir "
                f"({resolved_task}); got {report_path!r}"
            ) from exc
        if report_file.exists():
            try:
                with report_file.open("r", encoding="utf-8") as f:
                    report_payload = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"audit-{auditor_name}: cannot parse report at "
                    f"{report_path}: {exc}"
                ) from exc
            findings = report_payload.get("findings", []) if isinstance(report_payload, dict) else []
            if not isinstance(findings, list):
                findings = []
            actual_finding_count = len(findings)
            actual_blocking_count = sum(
                1 for f in findings
                if isinstance(f, dict) and f.get("blocking") is True
            )
            if finding_count is None:
                finding_count = actual_finding_count
                blocking_count = actual_blocking_count
            if finding_count != actual_finding_count:
                raise ValueError(
                    f"audit-{auditor_name}: finding_count mismatch — "
                    f"caller-supplied={finding_count}, "
                    f"actual (from {report_path})={actual_finding_count}"
                )
            if blocking_count != actual_blocking_count:
                raise ValueError(
                    f"audit-{auditor_name}: blocking_count mismatch — "
                    f"caller-supplied={blocking_count}, "
                    f"actual (from {report_path})={actual_blocking_count}"
                )
            try:
                report_sha256 = hash_file(report_file)
            except (FileNotFoundError, OSError) as exc:
                raise ValueError(
                    f"audit-{auditor_name}: cannot hash report at "
                    f"{report_path}: {exc}"
                ) from exc
        elif finding_count is None:
            raise ValueError(
                f"audit-{auditor_name}: report_path={report_path!r} does not exist; "
                "cannot derive finding_count/blocking_count automatically"
            )

    return finding_count, blocking_count, report_sha256


def _normalize_auditor_key(s: str) -> str:
    """Normalize an auditor identifier so subagent_type values from the
    spawn-log match auditor_name values from receipt callers.

    Both naming conventions appear in the codebase:
      - role-string form: "audit-spec-completion"  (role written to
        active-segment-role; matches write_policy audit-* prefix)
      - agent-file form:  "spec-completion-auditor"  (matches the
        agents/spec-completion-auditor.md filename and the value passed
        as `subagent_type` to the Agent tool)

    Strip both the "audit-" prefix and the "-auditor" suffix to land on
    a canonical key like "spec-completion" that matches across forms.
    """
    s = s.strip()
    # Plugin-namespaced subagent_types (e.g. "dynos-work:spec-completion-auditor")
    # appear in spawn-log.jsonl when the harness routes through a plugin. Strip
    # the "dynos-work:" prefix so the normalized key matches receipt callers
    # passing the bare auditor name.
    if s.startswith("dynos-work:"):
        s = s[len("dynos-work:"):]
    if s.startswith("audit-"):
        s = s[len("audit-"):]
    if s.endswith("-auditor"):
        s = s[: -len("-auditor")]
    return s


def _assert_spawn_log_evidence(task_dir: Path, auditor_name: str) -> None:
    """Cross-check that an auditor receipt is backed by harness-level
    spawn-log evidence.

    Reads ``task_dir / "spawn-log.jsonl"`` (hook-owned, write-protected
    by write_policy) and looks for an ``agent_spawn_post`` entry whose
    normalized ``subagent_type`` matches the normalized ``auditor_name``.

    Behavior:
      - Missing spawn-log.jsonl: emits ``audit_receipt_spawn_log_missing``
        event and returns. Graceful degradation for old deployments and
        test fixtures. Production deployments always have the file
        because ``hooks.json`` registers the agent-spawn-log hook.
      - Matching post entry with ``truncated == True`` (or
        ``stop_reason == "max_tokens"``): raises ``ValueError`` —
        truncation is audit-fail, not orchestrator's choice to backfill.
        This is the task-11 truncation contract.
      - No matching post entry (or only pre entries): raises
        ``ValueError`` naming the forgery defense. The receipt cannot
        attest to a spawn that the harness never recorded.
    """
    spawn_log = task_dir / "spawn-log.jsonl"
    if not spawn_log.is_file():
        # Graceful path. Emit a warning event so the gap is visible.
        try:
            log_event(
                task_dir.parent.parent,
                "audit_receipt_spawn_log_missing",
                task=task_dir.name,
                auditor_name=auditor_name,
                detail="spawn-log.jsonl absent at receipt-write time; "
                       "audit-chain forgery defense degraded",
            )
        except Exception:
            pass
        return

    target = _normalize_auditor_key(auditor_name)
    matched_post: dict[str, Any] | None = None
    saw_pre: bool = False
    try:
        with spawn_log.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    entry = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                subagent_type = entry.get("subagent_type") or ""
                if _normalize_auditor_key(str(subagent_type)) != target:
                    continue
                phase = entry.get("phase")
                if phase == "pre":
                    saw_pre = True
                elif phase == "post":
                    matched_post = entry
    except OSError as exc:
        raise ValueError(
            f"audit-{auditor_name}: spawn-log read failed ({exc}); "
            f"refusing receipt to avoid silent forgery-defense bypass"
        ) from exc

    if matched_post is None:
        if saw_pre:
            raise ValueError(
                f"audit-{auditor_name}: spawn-log has agent_spawn_pre but no "
                f"matching agent_spawn_post — the spawn never returned, "
                f"so a clean receipt would attest to work that did not "
                f"complete. Refusing the receipt write."
            )
        raise ValueError(
            f"audit-{auditor_name}: no agent_spawn_post entry in "
            f"spawn-log.jsonl matches normalized key '{target}'. The "
            f"receipt cannot attest to an auditor spawn that the harness "
            f"never recorded — this is the audit-chain forgery defense "
            f"(see memory/project_audit_forgery_incident.md)."
        )

    if matched_post.get("truncated") is True or matched_post.get("stop_reason") == "max_tokens":
        raise ValueError(
            f"audit-{auditor_name}: spawn-log entry shows truncation "
            f"(stop_reason=max_tokens). Truncation is an audit failure — "
            f"the orchestrator must re-spawn with smaller scope or report "
            f"the auditor as failed. Backfilling a clean receipt from a "
            f"truncated spawn is the exact pattern behind the 2026-04-30 "
            f"audit-chain forgery incident."
        )


def _emit_content_pairing_event(
    task_dir: Path, auditor_name: str, report_path: str | None
) -> None:
    """Emit `audit_receipt_content_paired` when both a matching spawn-log
    post entry AND the on-disk audit-report file are present at receipt-write
    time.

    Defense-in-depth on top of `_assert_spawn_log_evidence`: that function
    enforces the presence of harness-level proof of spawn; this function
    captures the file's content sha256 alongside the post entry's
    result_sha256 so a forensic reviewer can later spot mismatches between
    "what the agent returned" and "what landed on disk." Implemented as
    telemetry, not enforcement: the agent's return text is prose with JSON
    embedded; the on-disk file is the extracted JSON. A strict sha256
    equality would always fail; the event-based pairing creates the paper
    trail without breaking the legitimate write path.

    Silent no-op when:
      - report_path is None (no file to bind to)
      - report file does not exist
      - spawn-log.jsonl is absent
      - no matching post entry can be found

    None of these are errors at this layer — they are handled (or
    intentionally tolerated) by `_assert_spawn_log_evidence` and the
    receipt's own report-existence checks.
    """
    if not report_path:
        return
    report_p = Path(report_path)
    if not report_p.is_file():
        return
    spawn_log = task_dir / "spawn-log.jsonl"
    if not spawn_log.is_file():
        return
    target = _normalize_auditor_key(auditor_name)
    matched_post: dict[str, Any] | None = None
    try:
        with spawn_log.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    entry = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("phase") != "post":
                    continue
                subagent_type = entry.get("subagent_type") or ""
                if _normalize_auditor_key(str(subagent_type)) != target:
                    continue
                matched_post = entry
    except OSError:
        return
    if matched_post is None:
        return

    try:
        report_bytes = report_p.read_bytes()
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    except OSError:
        return

    try:
        log_event(
            task_dir.parent.parent,
            "audit_receipt_content_paired",
            task=task_dir.name,
            auditor_name=auditor_name,
            report_path=str(report_p),
            report_sha256=report_sha256,
            result_sha256=str(matched_post.get("result_sha256") or ""),
            result_excerpt_match=(
                report_sha256 in str(matched_post.get("result_excerpt") or "")
            ),
        )
    except Exception:
        # Telemetry failure must not break receipt write — content pairing
        # is observability, the receipt itself is what gates the pipeline.
        pass


def _assert_cascade_order(task_dir: Path, auditor_name: str, model_used: str | None) -> None:
    """Refuse an ensemble shard receipt written out of cascade order.

    The cascade state is computed by ``lib_ensemble.ensemble_next`` from the
    shard receipts already on disk. The fast tier is always accepted (it
    starts or restarts the cascade); every other tier must be exactly the
    next spawn the cascade requires. This is what makes a single deep-tier
    spawn insufficient for an ensemble auditor: its receipt is refused until
    the voting tiers have run. Fails closed when the auditor has no ensemble
    entry in ``audit-plan.json`` or the ``audit-routing`` receipt.
    """
    cascade = _lib_ensemble.ensemble_next(task_dir, auditor_name)
    status = cascade.get("status")
    if not cascade.get("ensemble"):
        if status == _lib_ensemble.STATUS_SINGLE:
            raise ValueError(
                f"audit-{auditor_name}: plan entry has ensemble=false; write a "
                "plain audit receipt without --ensemble-context"
            )
        raise ValueError(
            f"audit-{auditor_name}: ensemble_context requires an ensemble entry "
            "for the auditor in audit-plan.json or the audit-routing receipt"
        )
    voting = cascade.get("voting_models") or []
    fast_tier = voting[0] if voting else None
    if status == _lib_ensemble.STATUS_INVALID:
        raise ValueError(
            f"audit-{auditor_name}: ensemble cascade cannot be evaluated: "
            + "; ".join(cascade.get("gaps") or [])
        )
    if model_used == fast_tier:
        return
    if status == _lib_ensemble.STATUS_COMPLETE:
        raise ValueError(
            f"audit-{auditor_name}: ensemble cascade already complete "
            f"(verdict={cascade.get('verdict')!r}); a re-audit must start "
            f"again at the fast tier {fast_tier!r}, got {model_used!r}"
        )
    expected = cascade.get("model")
    if status != _lib_ensemble.STATUS_SPAWN or expected != model_used:
        raise ValueError(
            f"audit-{auditor_name}: ensemble cascade order violation — the next "
            f"required tier is {expected!r} ({cascade.get('reason')}), got "
            f"{model_used!r}. Run `ctl ensemble-next` before each spawn."
        )


def receipt_audit_done(
    task_dir: Path,
    auditor_name: str,
    model_used: str | None,
    finding_count: int | None = None,
    blocking_count: int | None = None,
    report_path: str | None = None,
    tokens_used: int | None = None,
    *,
    route_mode: str,
    agent_path: str | None,
    injected_agent_sha256: str | None,
    ensemble_context: bool = False,
    final_envelope: str | None = None,
    tier: str | None = None,
    shard_step_name: str | None = None,
    stage: str | None = None,
) -> Path:
    """Write receipt proving an auditor completed.

    Asserts the per-(auditor, model) sidecar at
    ``task_dir / "receipts" / "_injected-auditor-prompts"
    / f"{auditor_name}-{model_used}.sha256"`` matches
    `injected_agent_sha256` when non-null. Per-model disambiguation lets
    ensemble voting compare distinct injected prompts per model.

    Raises ValueError on sidecar mismatch or missing. There is no env
    bypass — sidecar enforcement is unconditional.

    Cross-checks the spawn-log: when ``task_dir / "spawn-log.jsonl"``
    exists, requires a matching ``agent_spawn_post`` entry for the
    auditor before accepting the write. Truncated entries are refused.
    See ``_assert_spawn_log_evidence`` for the full contract; this is
    the receipt-side closure of the audit-chain forgery defense.

    v4 AC 17: when ``route_mode == "learned"`` or ``ensemble_context`` is
    True the caller MUST supply a non-null ``report_path`` pointing at an
    existing file (the voting harness materialises this file before the
    receipt is written). The pre-escalation ensemble-vote bypass is thus
    no longer available for learned/ensemble auditors — ship a real
    report or fail the write.

    Also records token usage — same enforcement path as executor receipts.
    """
    finding_count, blocking_count = _validate_audit_done_args(
        route_mode, injected_agent_sha256, agent_path, report_path,
        finding_count, blocking_count, auditor_name, ensemble_context,
    )
    if injected_agent_sha256 is not None:
        _assert_sidecar_match(task_dir, auditor_name, model_used, injected_agent_sha256, tier=tier)
    _assert_spawn_log_evidence(task_dir, auditor_name)
    _validate_final_envelope(
        task_dir, auditor_name, route_mode, report_path, final_envelope,
    )
    if final_envelope is not None:
        envelope_sha256: str | None = hashlib.sha256(
            final_envelope.encode("utf-8")
        ).hexdigest()
    else:
        envelope_sha256 = None
    _emit_content_pairing_event(task_dir, auditor_name, report_path)
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, auditor_name, model_used or "default", tokens_used)
    finding_count, blocking_count, report_sha256 = _build_audit_receipt_payload(
        task_dir, auditor_name, report_path, finding_count, blocking_count,
    )
    # Defense-in-depth post-condition: helper-returned counts must be
    # non-negative (helper raises on real mismatch; this guards against
    # future helper bugs and keeps the in-body self-verify AST pattern).
    expected_min_count = 0
    if (finding_count is not None and finding_count < expected_min_count) or (
        blocking_count is not None and blocking_count < expected_min_count
    ):
        raise ValueError(
            f"audit-{auditor_name}: count post-condition violated "
            f"(finding={finding_count}, blocking={blocking_count}, "
            f"expected >= {expected_min_count})"
        )

    # Self-compute spawn identity fields (anti-forgery: writer-derived, not caller-supplied).
    # The sidecar uses a tier-name fallback when model_used is None (D-14); mirror that
    # here so the receipt tier field is consistent with the sidecar naming context.
    _sidecar_tier: str | None = tier
    if model_used is None:
        # Derive tier hint from sidecar key logic mirroring _assert_sidecar_match D-14.
        _sidecar_tier = None  # No sidecar-derived tier available at this call site.
    host, tier, resolved_model = _compute_spawn_host_fields(task_dir, model_used, _sidecar_tier)

    # Ensemble DONE accounting is per model. The DONE gate reads
    # receipts/audit-{auditor}-{model}.json, so reject ambiguous or
    # already-prefixed shard names at the writer boundary.
    if ensemble_context:
        if not isinstance(model_used, str) or not model_used:
            raise ValueError(
                f"audit-{auditor_name}: ensemble_context requires model_used"
            )
        expected_shard_step_name = f"{auditor_name}-{model_used}"
        if shard_step_name != expected_shard_step_name:
            raise ValueError(
                f"audit-{auditor_name}: ensemble shard_step_name must be "
                f"{expected_shard_step_name!r} so the receipt is named "
                f"audit-{expected_shard_step_name}.json"
            )
        _assert_cascade_order(task_dir, auditor_name, model_used)
        receipt_step_name = f"audit-{shard_step_name}"
    else:
        receipt_step_name = f"audit-{auditor_name}"

    # Build optional extra fields for the receipt payload
    extra_fields: dict = {}
    if stage is not None:
        extra_fields["stage"] = stage

    return write_receipt(
        task_dir,
        receipt_step_name,
        auditor_name=auditor_name,
        model_used=model_used,
        finding_count=finding_count,
        blocking_count=blocking_count,
        report_path=report_path,
        report_sha256=report_sha256,
        tokens_used=tokens_used,
        route_mode=route_mode,
        agent_path=agent_path,
        injected_agent_sha256=injected_agent_sha256,
        envelope_sha256=envelope_sha256,
        host=host,
        tier=tier,
        resolved_model=resolved_model,
        **extra_fields,
    )


def receipt_retrospective(task_dir: Path, **_legacy: Any) -> Path:
    """Write receipt proving retrospective was computed.

    Self-computes ``quality_score``, ``cost_score``, ``efficiency_score``,
    and ``total_tokens`` (v4 contract) by invoking
    ``lib_validate.compute_reward(task_dir)``. Callers no longer supply
    these fields — any legacy kwarg (``quality_score``, ``cost_score``,
    ``efficiency_score``, ``total_tokens``) raises ``TypeError``.
    """
    if _legacy:
        raise TypeError(
            "receipt_retrospective no longer accepts caller-supplied "
            f"{sorted(_legacy)} — scores and tokens are now computed via "
            "lib_validate.compute_reward(task_dir)"
        )

    # Deferred import to avoid an lib_validate <-> lib_receipts cycle
    # (lib_validate.compute_reward imports read_receipt from this module).
    from lib_validate import compute_reward  # noqa: PLC0415

    result = compute_reward(task_dir)
    if not isinstance(result, dict):
        raise ValueError(
            f"receipt_retrospective: compute_reward returned non-dict "
            f"{type(result).__name__}"
        )
    # compute_reward uses `total_token_usage`; receipt schema uses
    # `total_tokens`. Accept either to stay resilient to refactors.
    total_tokens = result.get("total_tokens")
    if total_tokens is None:
        total_tokens = result.get("total_token_usage", 0)

    # task-20260505-001 AC-19: self-compute auto-approval gate counts by
    # scanning the receipts directory at write time. Callers do NOT
    # supply these fields (consistent with the v4 anti-falsification
    # pattern). Counts only auto-approval receipts whose JSON parses and
    # carries `valid: true`; the stage labels are extracted from the
    # filename stem and sorted alphabetically.
    receipts_dir = task_dir / "receipts"
    gates_auto_approved = 0
    auto_approved_stages: list[str] = []
    if receipts_dir.is_dir():
        for receipt_path in sorted(receipts_dir.glob("auto-approval-*.json")):
            try:
                payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("valid") is True:
                gates_auto_approved += 1
            stem = receipt_path.stem
            if stem.startswith("auto-approval-"):
                stage_label = stem[len("auto-approval-"):]
                if stage_label and stage_label not in auto_approved_stages:
                    auto_approved_stages.append(stage_label)
    auto_approved_stages.sort()

    return write_receipt(
        task_dir,
        "retrospective",
        quality_score=result.get("quality_score", 0.0),
        cost_score=result.get("cost_score", 0.0),
        efficiency_score=result.get("efficiency_score", 0.0),
        total_tokens=total_tokens,
        retrospective_path=str(task_dir / "task-retrospective.json"),
        gates_auto_approved=gates_auto_approved,
        auto_approved_stages=auto_approved_stages,
    )


def receipt_post_completion(
    task_dir: Path,
    handlers_run: list[dict],
) -> Path:
    """Write receipt proving post-completion pipeline ran.

    Per the v2 contract, post-completion no longer carries postmortem or
    pattern-update flags — those concerns belong to dedicated postmortem
    receipts (see `receipt_postmortem_*`).

    v4 self-verify: when ``handlers_run`` is non-empty, cross-check each
    declared handler name against ``eventbus_handler`` events in the
    task-scoped ``events.jsonl`` at ``task_dir/events.jsonl``. Each
    entry in ``handlers_run`` must resolve to an event whose ``handler``
    or ``name`` field matches; a missing handler raises
    ``ValueError("post-completion handler not in events: <name>")``.

    v5 self_verify enum: the receipt payload now carries a top-level
    ``self_verify: str`` field with one of three values:
      * ``"passed"`` — events.jsonl was readable AND every handler name
        in ``handlers_run`` was matched against an ``eventbus_handler``
        record.
      * ``"skipped-handlers-empty"`` — ``handlers_run`` list was empty
        (no handlers to verify).

    Missing or unreadable task-scoped events are a hard error when
    ``handlers_run`` is non-empty. A post-completion receipt without an
    inspectable task-local event log is caller-attested rather than
    self-verifying and would reopen a trust-me-bro bypass.
    """
    if not isinstance(handlers_run, list):
        raise ValueError("handlers_run must be a list")

    # Default: empty handlers_run short-circuits to the skipped enum and
    # no file IO happens.
    self_verify: str = "skipped-handlers-empty"

    if handlers_run:
        task_id = task_dir.name
        task_events = task_dir / "events.jsonl"

        if not task_events.exists():
            raise ValueError(
                f"post-completion task events log missing: {task_events}"
            )

        try:
            verified_records = verify_signed_events(
                task_dir,
                os.environ.get("DYNOS_EVENT_SECRET", ""),
                strict=False,
            )
        except OSError as exc:
            raise ValueError(
                f"post-completion task events log unreadable: {task_events}: {exc}"
            ) from exc

        seen_handlers: set[str] = set()
        for record in verified_records:
            if record.get("event") != "eventbus_handler":
                continue
            if record.get("task") != task_id:
                continue
            for key in ("handler", "name"):
                hname = record.get(key)
                if isinstance(hname, str) and hname:
                    seen_handlers.add(hname)


        for idx, entry in enumerate(handlers_run):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"handlers_run[{idx}] must be a dict (got "
                    f"{type(entry).__name__})"
                )
            name = entry.get("name") or entry.get("handler")
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"handlers_run[{idx}] missing 'name'/'handler' key"
                )
            if name not in seen_handlers:
                raise ValueError(
                    f"post-completion handler not in events: {name}"
                )
        self_verify = "passed"

    return write_receipt(
        task_dir,
        "post-completion",
        handlers_run=handlers_run,
        self_verify=self_verify,
    )
