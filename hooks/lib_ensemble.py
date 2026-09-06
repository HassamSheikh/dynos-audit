"""Deterministic ensemble cascade evaluation.

Single source of truth for the audit ensemble cascade
(fast tier -> balanced tier on zero findings -> deep tier on any finding).
Every enforcement point reads the cascade state from this module so the
orchestrator never has to interpret prose to decide which spawn comes next:

- ``ctl ensemble-next`` prints the next required spawn per auditor.
- ``router.py audit-inject-prompt`` refuses to build a prompt for an
  ensemble auditor at any model other than the next cascade step.
- ``receipt_audit_done`` refuses a shard receipt written out of cascade order.
- ``ctl run-audit-summary`` and the DONE gate
  (``lib_core._check_ensemble_voting``) refuse while a cascade is incomplete.

Cascade state is derived purely from the per-model shard receipts
``receipts/audit-{auditor}-{model}.json``. A shard for a later tier that is
older than the current fast-tier shard is treated as stale (the cascade was
restarted, e.g. by a re-audit after repair) and ignored.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATUS_SPAWN = "spawn"
STATUS_COMPLETE = "complete"
STATUS_SINGLE = "single"
STATUS_INVALID = "invalid"

REASON_CASCADE_STEP = "cascade_step"
REASON_ESCALATE = "escalate_on_findings"

VERDICT_PASS = "pass"
VERDICT_ESCALATED = "escalated"


# ---------------------------------------------------------------------------
# Plan lookup
# ---------------------------------------------------------------------------

def _entries_from(obj: Any) -> list[dict]:
    if not isinstance(obj, dict):
        return []
    auditors = obj.get("auditors")
    if not isinstance(auditors, list):
        return []
    return [e for e in auditors if isinstance(e, dict)]


def load_plan_entries(task_dir: Path) -> list[dict]:
    """Return the auditor entries for a task.

    ``audit-plan.json`` (the router's output) is authoritative; the
    ``audit-routing`` receipt is the fallback for tasks whose plan file was
    not persisted. Returns an empty list when neither exists.
    """
    plan_path = task_dir / "audit-plan.json"
    if plan_path.exists():
        try:
            entries = _entries_from(json.loads(plan_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            entries = []
        if entries:
            return entries
    try:
        from lib_receipts import read_receipt  # noqa: PLC0415
        routing = read_receipt(task_dir, "audit-routing")
    except Exception:
        routing = None
    return _entries_from(routing)


def load_plan_entry(task_dir: Path, auditor_name: str) -> dict | None:
    for entry in load_plan_entries(task_dir):
        if entry.get("name") == auditor_name:
            return entry
    return None


# ---------------------------------------------------------------------------
# Cascade configuration
# ---------------------------------------------------------------------------

def cascade_models(entry: dict) -> tuple[list[str], str]:
    """Return ``(voting_models, escalation_model)`` from a plan entry.

    ``escalation_model`` is ``""`` when the entry does not configure one.
    """
    voting_raw = entry.get("ensemble_voting_models")
    if voting_raw is None:
        voting_raw = entry.get("voting_models")
    voting: list[str] = []
    if isinstance(voting_raw, list):
        voting = [m for m in voting_raw if isinstance(m, str) and m]
    escalation = entry.get("ensemble_escalation_model")
    if escalation is None:
        escalation = entry.get("escalation_model")
    if not isinstance(escalation, str):
        escalation = ""
    return voting, escalation


def is_ensemble_entry(entry: dict | None) -> bool:
    return isinstance(entry, dict) and entry.get("ensemble") is True


# ---------------------------------------------------------------------------
# Receipt reading
# ---------------------------------------------------------------------------

def _receipt_ts(receipt: dict | None) -> str:
    if not isinstance(receipt, dict):
        return ""
    ts = receipt.get("ts")
    return ts if isinstance(ts, str) else ""


def read_cascade_receipts(
    task_dir: Path,
    auditor_name: str,
    voting: list[str],
    escalation: str,
) -> dict[str, dict | None]:
    """Read every shard receipt of the cascade, dropping stale later tiers.

    A later-tier (or escalation) shard whose ``ts`` is older than the
    fast-tier shard belongs to a previous run of the cascade and is ignored.
    """
    from lib_receipts import read_receipt  # noqa: PLC0415

    models: list[str] = list(voting)
    if escalation and escalation not in models:
        models.append(escalation)
    receipts: dict[str, dict | None] = {}
    for model in models:
        receipts[model] = read_receipt(
            task_dir, f"audit-{auditor_name}-{model}", min_version=2
        )
    if voting:
        fast_ts = _receipt_ts(receipts.get(voting[0]))
        if fast_ts:
            for model in models[1:]:
                shard = receipts.get(model)
                if shard is not None and _receipt_ts(shard) < fast_ts:
                    receipts[model] = None
    return receipts


def _finding_count(receipt: dict) -> int:
    try:
        return int(receipt.get("finding_count", -1))
    except (TypeError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------

def evaluate_cascade(
    auditor_name: str,
    entry: dict,
    receipts: dict[str, dict | None],
) -> dict:
    """Evaluate the cascade for one auditor from its shard receipts.

    Pure function: ``receipts`` maps model -> receipt dict (or ``None``).
    Returns a dict with at least ``status`` and ``gaps``:

    - ``status == "single"``: not an ensemble auditor; ``model`` is the plan
      model.
    - ``status == "spawn"``: the next required spawn is ``model``
      (``reason`` is ``cascade_step`` or ``escalate_on_findings``).
    - ``status == "complete"``: nothing more to spawn; ``verdict`` is
      ``pass`` (every voting tier clean) or ``escalated`` (deep tier ran).
    - ``status == "invalid"``: the plan entry cannot be evaluated.

    ``gaps`` lists protocol violations visible in the receipts (an
    escalation shard without the fast-tier shard, a receipt whose
    ``model_used`` is outside the cascade). ``gaps`` is non-empty whenever
    the cascade would not satisfy the DONE gate.
    """
    base: dict[str, Any] = {
        "auditor": auditor_name,
        "ensemble": is_ensemble_entry(entry),
        "gaps": [],
    }
    if not is_ensemble_entry(entry):
        plan_model = entry.get("model") if isinstance(entry, dict) else None
        return {**base, "status": STATUS_SINGLE, "model": plan_model}

    voting, escalation = cascade_models(entry)
    base.update({"voting_models": list(voting), "escalation_model": escalation})
    gaps: list[str] = base["gaps"]

    if not voting:
        gaps.append(f"auditor {auditor_name} ensemble=true but voting_models is empty")
        return {**base, "status": STATUS_INVALID, "model": None}

    allowed = set(voting)
    if escalation:
        allowed.add(escalation)
    for _model, shard in receipts.items():
        if shard is None:
            continue
        model_used = shard.get("model_used")
        if isinstance(model_used, str) and model_used and model_used not in allowed:
            gaps.append(
                f"auditor {auditor_name} receipt model_used={model_used} not in voting set"
            )

    escalation_receipt = receipts.get(escalation) if escalation else None

    def _spawn(model: str, tier_index: int | None, reason: str, **extra: Any) -> dict:
        if reason == REASON_CASCADE_STEP:
            missing = [m for m in voting if receipts.get(m) is None]
            gaps.append(
                f"auditor {auditor_name} ensemble missing voting-model receipt(s): "
                f"{', '.join(missing)} (next cascade step: {model})"
            )
        else:
            gaps.append(
                f"auditor {auditor_name} ensemble voting-model receipts found issues "
                f"(non-zero findings) and no escalation receipt for {model!r}"
            )
        return {
            **base,
            "status": STATUS_SPAWN,
            "model": model,
            "tier_index": tier_index,
            "reason": reason,
            "shard_step_name": f"{auditor_name}-{model}",
            **extra,
        }

    for index, model in enumerate(voting):
        shard = receipts.get(model)
        if shard is None:
            if escalation_receipt is not None:
                gaps.append(
                    f"auditor {auditor_name} has an escalation receipt for "
                    f"{escalation!r} but no voting-tier receipt for {model}; "
                    "the cascade must run the voting tiers first"
                )
            return _spawn(model, index, REASON_CASCADE_STEP)
        if _finding_count(shard) != 0:
            if not escalation:
                gaps.append(
                    f"auditor {auditor_name} voting-model receipt {model} found "
                    "issues but no escalation model is configured"
                )
                return {**base, "status": STATUS_INVALID, "model": None}
            if escalation_receipt is None:
                return _spawn(escalation, None, REASON_ESCALATE, triggered_by=model)
            return {
                **base,
                "status": STATUS_COMPLETE,
                "model": None,
                "verdict": VERDICT_ESCALATED,
                "triggered_by": model,
            }

    return {**base, "status": STATUS_COMPLETE, "model": None, "verdict": VERDICT_PASS}


# ---------------------------------------------------------------------------
# Task-dir entry points
# ---------------------------------------------------------------------------

def ensemble_next(task_dir: Path, auditor_name: str, entry: dict | None = None) -> dict:
    """Evaluate one auditor's cascade against the receipts on disk."""
    if entry is None:
        entry = load_plan_entry(task_dir, auditor_name)
    if entry is None:
        return {
            "auditor": auditor_name,
            "ensemble": False,
            "status": STATUS_INVALID,
            "model": None,
            "gaps": [f"auditor {auditor_name} not found in audit-plan.json or audit-routing receipt"],
        }
    if not is_ensemble_entry(entry):
        return evaluate_cascade(auditor_name, entry, {})
    voting, escalation = cascade_models(entry)
    receipts = read_cascade_receipts(task_dir, auditor_name, voting, escalation)
    return evaluate_cascade(auditor_name, entry, receipts)


def ensemble_next_all(task_dir: Path, entries: list[dict] | None = None) -> dict:
    """Evaluate every spawn-action auditor of a task.

    Returns ``{"auditors": [...], "pending": [names], "complete": bool}``.
    ``complete`` is true only when no ensemble auditor still needs a spawn
    and no cascade carries a gap.
    """
    if entries is None:
        entries = load_plan_entries(task_dir)
    results: list[dict] = []
    pending: list[str] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if entry.get("action") != "spawn":
            continue
        result = ensemble_next(task_dir, name, entry)
        results.append(result)
        if result["status"] in (STATUS_SPAWN, STATUS_INVALID) or result["gaps"]:
            pending.append(name)
    return {"auditors": results, "pending": pending, "complete": not pending}


def cascade_gaps(task_dir: Path, entries: list[dict]) -> list[str]:
    """DONE-gate view: every gap across all ensemble spawn entries."""
    gaps: list[str] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if entry.get("action") != "spawn" or not is_ensemble_entry(entry):
            continue
        gaps.extend(ensemble_next(task_dir, name, entry)["gaps"])
    return gaps
