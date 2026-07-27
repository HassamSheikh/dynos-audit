#!/usr/bin/env python3
"""Policy engine — computes routing policies from task retrospectives."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
import math
import os
import statistics
from pathlib import Path

from lib_core import collect_retrospectives, now_iso, _persistent_project_dir, load_json, write_json, VALID_EXECUTORS
from lib_log import log_event, verify_signed_events
from lib_project_id import sanitize_path_for_slug
from lib_registry import ensure_learned_registry

DEFAULT_TASK_TYPES = ["feature", "bugfix", "refactor", "migration", "ml", "full-stack"]
DEFAULT_EXECUTOR_ROLES = sorted(VALID_EXECUTORS)  # auto-discovered from agents/
DEFAULT_AUDITOR_ROLES = [
    "ui-auditor",
    "db-schema-auditor",
    "dead-code-auditor",
    "security-auditor",
    "spec-completion-auditor",
    "code-quality-auditor",
    "architecture-auditor",
    "threat-model-auditor",
    "api-contract-auditor",
    "test-strategy-auditor",
    "accessibility-auditor",
    "privacy-auditor",
    "supply-chain-auditor",
    "infrastructure-auditor",
    "observability-auditor",
    "release-auditor",
    "data-integrity-auditor",
    "docs-accuracy-auditor",
]
SKIP_EXEMPT_AUDITORS = {"security-auditor", "spec-completion-auditor", "threat-model-auditor"}
from lib_models import (
    ALL_TIERS as _ALL_TIERS,
    valid_models_for_host as _valid_models_for_host,
    HOST_CLAUDE as _HOST_CLAUDE,
)
# Dual-accept: old Claude model literals + new tier names.
# IR-1: every call site that tests `model in VALID_MODELS` continues to work.
VALID_MODELS: frozenset = _valid_models_for_host(_HOST_CLAUDE) | frozenset(_ALL_TIERS)
from lib_defaults import (
    EMA_ALPHA,
    EMA_COLD_START_MINIMUM,
    EMA_MAX_EFFECTIVENESS_ROWS,
    EMA_TIE_BREAKING_THRESHOLD,
    ROUTER_WEIGHT_QUALITY,
    ROUTER_WEIGHT_COST,
    ROUTER_WEIGHT_EFFICIENCY,
    ROUTING_WEIGHT_QUALITY,
    ROUTING_WEIGHT_EFFICIENCY,
    ROUTING_WEIGHT_COST,
    SKIP_THRESHOLD_BASE,
    SKIP_THRESHOLD_SLOPE,
    SKIP_THRESHOLD_MIN,
    SKIP_THRESHOLD_MAX,
    CONFIDENCE_MAX_CLAMP,
    UCB_COLD_START_MINIMUM,
)
COMPOSITE_WEIGHTS = (ROUTER_WEIGHT_QUALITY, ROUTER_WEIGHT_COST, ROUTER_WEIGHT_EFFICIENCY)
# RN-6: keys are tier names (fast/balanced/deep) — model literals moved to lib_models.
# IR-1: callers use MODEL_COST_ORDER.get(model_or_tier, 99) — tier names sort correctly.
from lib_models import (
    TIER_FAST as _TIER_FAST,
    TIER_BALANCED as _TIER_BALANCED,
    TIER_DEEP as _TIER_DEEP,
    TIER_FRONTIER as _TIER_FRONTIER,
    model_rank as _model_rank,
    tier_rank as _tier_rank,
)
MODEL_COST_ORDER: dict[str, int] = {
    _TIER_FAST: 0,
    _TIER_BALANCED: 1,
    _TIER_DEEP: 2,
    _TIER_FRONTIER: 3,
}
# Backwards compat aliases
COLD_START_MINIMUM = EMA_COLD_START_MINIMUM
MAX_EFFECTIVENESS_ROWS = EMA_MAX_EFFECTIVENESS_ROWS


def project_slug(root: Path) -> str:
    # T-18: validate the RAW input string BEFORE Path.resolve() collapses '..'
    # traversal components. resolve() silently turns '/a/../etc' into '/etc',
    # which would let a hostile path bypass the sanitizer that runs on the
    # post-resolve string.
    raw = str(root)
    sanitize_path_for_slug(raw)
    return sanitize_path_for_slug(str(root.resolve()))


def local_patterns_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "project_rules.md"


def claude_patterns_path(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / project_slug(root) / "memory" / "project_rules.md"


def pattern_paths(root: Path) -> list[Path]:
    paths = [local_patterns_path(root), claude_patterns_path(root)]
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _load_prevention_rules(root: Path) -> list[dict]:
    """Load learned prevention rules written by postmortem_improve."""
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    try:
        data = load_json(rules_path)
        if isinstance(data, dict):
            return data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def _observed_task_types(retrospectives: list[dict], registry: dict) -> list[str]:
    task_types = {
        str(item.get("task_type"))
        for item in retrospectives
        if isinstance(item.get("task_type"), str) and item.get("task_type")
    }
    task_types.update(
        str(item.get("task_type"))
        for item in registry.get("agents", [])
        if isinstance(item.get("task_type"), str) and item.get("task_type")
    )
    if not task_types:
        task_types.update(DEFAULT_TASK_TYPES)
    return sorted(task_types)


def _observed_executor_roles(retrospectives: list[dict], registry: dict) -> list[str]:
    roles = set(DEFAULT_EXECUTOR_ROLES)
    for item in retrospectives:
        for role in item.get("executor_repair_frequency", {}):
            if isinstance(role, str) and role:
                roles.add(role)
    for item in registry.get("agents", []):
        role = item.get("role")
        if isinstance(role, str) and role.endswith("-executor"):
            roles.add(role)
    return sorted(roles)


def _observed_auditor_roles(retrospectives: list[dict], registry: dict) -> list[str]:
    roles = set(DEFAULT_AUDITOR_ROLES)
    for item in retrospectives:
        for role in item.get("auditor_zero_finding_streaks", {}):
            if isinstance(role, str) and role:
                roles.add(role)
        for role in item.get("findings_by_auditor", {}):
            if isinstance(role, str) and role.endswith("-auditor"):
                roles.add(role)
    for item in registry.get("agents", []):
        role = item.get("role")
        if isinstance(role, str) and role.endswith("-auditor"):
            roles.add(role)
    return sorted(roles)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# EMA computation (deterministic, replaces inline SKILL.md math)
# ---------------------------------------------------------------------------

QuadKey = tuple[str, str, str, str]  # (role, model, task_type, source)


def _build_events_by_task(
    root: Path,
    task_ids: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Build {task_id -> [event, ...]} using per-task HMAC-verified reads.

    When ``task_ids`` is ``None`` or empty, returns ``{}`` immediately
    without reading any file. This is the conservative/secure behavior:
    the caller cannot safely enumerate all task directories without
    scanning, and returning an empty dict causes every ``learned:X``
    source to be reclassified as generic (the correct conservative
    outcome).

    For each task_id in ``task_ids``, resolves ``root / ".dynos" /
    task_id``. If that directory does not exist, the task_id is omitted
    from the result — **no fallback to the project-global**
    ``events.jsonl`` occurs. If the directory exists,
    :func:`verify_signed_events` is called with ``strict=False`` to
    return only HMAC-verified records. Events that fail signature
    verification are silently dropped.

    ``task_dir missing → zero events (no global fallback)`` is the
    documented contract. Any future caller expecting cross-check
    behavior on a missing task directory should be treated as a caller
    error, not as a signal to fall back to the global log.
    """
    if not task_ids:
        return {}
    # Path-safety gate: task_ids originate in retrospective JSON which is
    # operator-side state but is not HMAC-signed. A crafted task_id like
    # "../etc" or "task-x/../events" would otherwise resolve outside .dynos
    # via the path join below and could cause verify_signed_events(strict=False)
    # to read parseable records from arbitrary readable JSONL files.
    # Reject anything that doesn't match the safe slug regex BEFORE the join.
    from lib_core import is_safe_task_id  # noqa: PLC0415 — local to break import cycle
    secret = os.environ.get("DYNOS_EVENT_SECRET", "")
    result: dict[str, list[dict]] = {}
    for task_id in task_ids:
        if not is_safe_task_id(task_id):
            log_event(
                root,
                "policy_engine_unsafe_task_id_rejected",
                task=str(task_id) if task_id is not None else None,
                reason="task_id failed slug validation",
            )
            continue
        task_dir = root / ".dynos" / task_id
        if not task_dir.is_dir():
            continue
        try:
            verified = verify_signed_events(task_dir, secret, strict=False)
        except OSError:
            continue
        result[task_id] = verified
    return result


def _extract_quads(
    retrospectives: list[dict],
    events_by_task: dict[str, list[dict]] | None = None,
    root: Path | None = None,
) -> list[tuple[QuadKey, float, float, float]]:
    """Extract (quad_key, quality, cost, efficiency) from validated retrospectives.

    Sorted by task_id ascending for deterministic replay.

    When ``events_by_task`` is not None, each quad whose
    ``agent_source[role]`` starts with ``"learned:"`` is cross-checked
    against the matching task's event stream. A quad is verified iff at
    least one entry in ``events_by_task.get(retro["task_id"], [])`` has
    ``event == "learned_agent_applied"`` AND ``agent_name == <claimed>``
    AND (``segment_id == role`` OR
    ``segment_id == role.removeprefix("audit-")``). Unmatched quads have
    their ``source`` rewritten to ``"generic"``; when ``root`` is also
    provided, a single ``agent_source_reclassified`` event is emitted via
    :func:`log_event` per rewritten quad. When ``events_by_task is None``,
    the cross-check is skipped entirely (legacy path).
    """
    # PERF-002: collect reclassification events during extraction and
    # flush them once after the loop completes. Prevents the synchronous
    # open+append+close per reclassified quad inside the hot path — which
    # at N retros * M roles (all learned-unmatched) would fire N*M serial
    # file writes before any quad is returned.
    _reclass_events: list[dict] = []
    # Filter and validate
    valid = []
    for retro in retrospectives:
        q = retro.get("quality_score")
        c = retro.get("cost_score")
        e = retro.get("efficiency_score")
        if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (q, c, e) if v is not None):
            continue
        if q is None or c is None or e is None:
            continue
        models = retro.get("model_used_by_agent", {})
        if not isinstance(models, dict):
            continue
        task_type = retro.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            continue
        valid.append(retro)

    valid.sort(key=lambda r: str(r.get("task_id", "")))

    quads: list[tuple[QuadKey, float, float, float]] = []
    for retro in valid:
        q = float(retro["quality_score"])
        c = float(retro["cost_score"])
        e = float(retro["efficiency_score"])
        task_type = retro["task_type"]
        models = retro.get("model_used_by_agent", {})
        sources = retro.get("agent_source", {})
        task_id = retro.get("task_id")
        task_events = (
            events_by_task.get(task_id, [])
            if events_by_task is not None and isinstance(task_id, str)
            else []
        )

        for role, model in models.items():
            if not isinstance(model, str) or model not in VALID_MODELS:
                continue
            source = sources.get(role, "generic") if isinstance(sources, dict) else "generic"

            # Cross-check learned:X claims against events.jsonl (fix E).
            # Only active when caller opted in via events_by_task != None.
            if (
                events_by_task is not None
                and isinstance(source, str)
                and source.startswith("learned:")
            ):
                claimed_agent = source[len("learned:"):]
                role_stripped = role[len("audit-"):] if role.startswith("audit-") else role
                matched = False
                for entry in task_events:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("event") != "learned_agent_applied":
                        continue
                    if entry.get("agent_name") != claimed_agent:
                        continue
                    seg_id = entry.get("segment_id")
                    if seg_id == role or seg_id == role_stripped:
                        matched = True
                        break
                if not matched:
                    original_source = source
                    source = "generic"
                    if root is not None:
                        _reclass_events.append({
                            "task_id": task_id,
                            "role": role,
                            "original_source": original_source,
                            "reclassified_to": "generic",
                            "reason": "no_matching_learned_agent_applied_event",
                        })

            quads.append(((role, model, task_type, source), q, c, e))

    # Flush batched reclassification events after the extraction loop.
    if root is not None and _reclass_events:
        for ev in _reclass_events:
            log_event(root, "agent_source_reclassified", **ev)

    return quads


def compute_effectiveness_scores(
    retrospectives: list[dict],
    baseline: dict[QuadKey, tuple[float, float, float]] | None = None,
    root: Path | None = None,
) -> list[dict]:
    """Compute EMA effectiveness scores from retrospectives.

    Returns list of dicts with: role, model, task_type, source,
    quality_ema, cost_ema, efficiency_ema, sample_count, updated.

    When ``root`` is provided, this function builds ``events_by_task`` once
    from ``<root>/.dynos/events.jsonl`` and passes it to
    :func:`_extract_quads` so that retrospective ``learned:X`` claims are
    cross-checked against the event stream. Unmatched claims are
    reclassified to ``"generic"`` with an ``agent_source_reclassified``
    event. When ``events.jsonl`` does not exist, an empty dict is still
    passed (cross-check runs with zero signal — every ``learned:X`` gets
    reclassified). Legacy callers that omit ``root`` skip the cross-check
    entirely.
    """
    if root is not None:
        # PERF-001: pre-filter events.jsonl to only the task_ids that
        # appear in this retrospective set. Bounds memory at O(events-
        # for-retros) instead of O(all-events-ever-emitted-by-project).
        needed_ids: set[str] = {
            r["task_id"]
            for r in retrospectives
            if isinstance(r, dict) and isinstance(r.get("task_id"), str)
        }
        events_by_task: dict[str, list[dict]] | None = _build_events_by_task(
            root, task_ids=needed_ids
        )
    else:
        events_by_task = None
    quads = _extract_quads(retrospectives, events_by_task=events_by_task, root=root)

    # EMA state per quad
    state: dict[QuadKey, dict] = {}
    # Track consecutive quality drops for regression detection
    prev_quality: dict[QuadKey, float] = {}
    drop_streak: dict[QuadKey, int] = {}

    for key, q, c, e in quads:
        if key not in state:
            # Cold-start: alpha = 1.0
            state[key] = {
                "quality_ema": q,
                "cost_ema": c,
                "efficiency_ema": e,
                "sample_count": 1,
            }
            prev_quality[key] = q
            drop_streak[key] = 0
        else:
            s = state[key]
            # Update: alpha = 0.3
            s["quality_ema"] = EMA_ALPHA * q + (1 - EMA_ALPHA) * s["quality_ema"]
            s["cost_ema"] = EMA_ALPHA * c + (1 - EMA_ALPHA) * s["cost_ema"]
            s["efficiency_ema"] = EMA_ALPHA * e + (1 - EMA_ALPHA) * s["efficiency_ema"]
            s["sample_count"] += 1

            # Regression detection: 2+ consecutive quality drops
            if q < prev_quality.get(key, q):
                drop_streak[key] = drop_streak.get(key, 0) + 1
            else:
                drop_streak[key] = 0
            prev_quality[key] = q

            if drop_streak[key] >= 2 and baseline:
                bq, bc, be = baseline.get(key, (s["quality_ema"], s["cost_ema"], s["efficiency_ema"]))
                s["quality_ema"] = EMA_ALPHA * bq + (1 - EMA_ALPHA) * s["quality_ema"]
                s["cost_ema"] = EMA_ALPHA * bc + (1 - EMA_ALPHA) * s["cost_ema"]
                s["efficiency_ema"] = EMA_ALPHA * be + (1 - EMA_ALPHA) * s["efficiency_ema"]

            # Clamp to [0, 1]
            s["quality_ema"] = max(0.0, min(1.0, s["quality_ema"]))
            s["cost_ema"] = max(0.0, min(1.0, s["cost_ema"]))
            s["efficiency_ema"] = max(0.0, min(1.0, s["efficiency_ema"]))

    ts = now_iso()
    rows = []
    for (role, model, task_type, source), s in state.items():
        rows.append({
            "role": role,
            "model": model,
            "task_type": task_type,
            "source": source,
            "quality_ema": round(s["quality_ema"], 4),
            "cost_ema": round(s["cost_ema"], 4),
            "efficiency_ema": round(s["efficiency_ema"], 4),
            "sample_count": s["sample_count"],
            "updated": ts,
        })

    # Row cap: keep most recent 50 by sample_count descending
    rows.sort(key=lambda r: (-r["sample_count"], r["role"], r["model"]))
    return rows[:MAX_EFFECTIVENESS_ROWS]


def derive_model_policy(effectiveness_scores: list[dict]) -> dict[str, dict]:
    """Derive Model Policy from effectiveness scores using composite scoring.

    Returns {role:task_type -> {model, confidence, updated}}.
    """
    # Group by (role, task_type), aggregate across source
    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for row in effectiveness_scores:
        key = (row["role"], row["task_type"])
        groups.setdefault(key, {}).setdefault(row["model"], []).append(row)

    result: dict[str, dict] = {}
    for (role, task_type), model_rows in groups.items():
        candidates = []
        for model, rows in model_rows.items():
            if model not in VALID_MODELS:
                continue
            # Weighted average across sources
            total_samples = sum(r["sample_count"] for r in rows)
            if total_samples == 0:
                continue
            q = sum(r["quality_ema"] * r["sample_count"] for r in rows) / total_samples
            c = sum(r["cost_ema"] * r["sample_count"] for r in rows) / total_samples
            e = sum(r["efficiency_ema"] * r["sample_count"] for r in rows) / total_samples
            wq, wc, we = COMPOSITE_WEIGHTS
            composite = wq * q + wc * c + we * e
            candidates.append({
                "model": model,
                "composite": composite,
                "quality_ema": q,
                "efficiency_ema": e,
                "sample_count": total_samples,
            })

        if not candidates:
            continue

        # Deterministic ranking
        candidates.sort(key=lambda x: (
            -x["composite"],
            -x["quality_ema"],
            -x["efficiency_ema"],
            MODEL_COST_ORDER.get(x["model"], 99),
            x["model"],
        ))

        # Tie-breaking: if top two within threshold, prefer higher quality
        if len(candidates) >= 2:
            if abs(candidates[0]["composite"] - candidates[1]["composite"]) < EMA_TIE_BREAKING_THRESHOLD:
                if candidates[1]["quality_ema"] > candidates[0]["quality_ema"]:
                    candidates[0], candidates[1] = candidates[1], candidates[0]

        winner = candidates[0]
        model = winner["model"]

        # Monotonicity: security-auditor never below TIER_DEEP. This is a
        # floor, not a pin — a rank comparison so that a frontier-tier winner
        # survives instead of being downgraded to deep by an equality check.
        if role == "security-auditor" and _model_rank(model) < _tier_rank(_TIER_DEEP):
            model = _TIER_DEEP

        # Confidence
        confidence = min(1.0, winner["quality_ema"] * min(1.0, winner["sample_count"] / UCB_COLD_START_MINIMUM))
        confidence = max(0.0, min(1.0, confidence))

        policy_key = f"{role}:{task_type}"
        result[policy_key] = {
            "model": model,
            "confidence": round(confidence, 4),
            "updated": now_iso(),
        }

    return result


def derive_skip_policy(effectiveness_scores: list[dict]) -> dict[str, dict]:
    """Derive Skip Policy from effectiveness scores.

    Returns {auditor -> {threshold, confidence, updated}}.
    """
    # Average quality_ema per auditor across all quads
    auditor_quality: dict[str, list[float]] = {}
    for row in effectiveness_scores:
        role = row["role"]
        if not role.endswith("-auditor"):
            continue
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        auditor_quality.setdefault(role, []).append(row["quality_ema"])

    result: dict[str, dict] = {}
    for role, qualities in auditor_quality.items():
        avg_quality = _mean(qualities)
        threshold = round(SKIP_THRESHOLD_BASE + SKIP_THRESHOLD_SLOPE * (1 - avg_quality))
        threshold = max(SKIP_THRESHOLD_MIN, min(SKIP_THRESHOLD_MAX, threshold))
        result[role] = {
            "threshold": threshold,
            "confidence": round(avg_quality, 4),
            "updated": now_iso(),
        }

    return result


def compute_routing_composite(effectiveness_scores: list[dict]) -> dict[str, float]:
    """Compute routing composite for each (role, task_type, source).

    Uses calibration weights: quality + efficiency + cost.
    Returns {role:task_type:source -> composite_score}.
    """
    result: dict[str, float] = {}
    for row in effectiveness_scores:
        key = f"{row['role']}:{row['task_type']}:{row['source']}"
        composite = ROUTING_WEIGHT_QUALITY * row["quality_ema"] + ROUTING_WEIGHT_EFFICIENCY * row["efficiency_ema"] + ROUTING_WEIGHT_COST * row["cost_ema"]
        result[key] = round(composite, 4)
    return result


def _build_model_policy_data(
    retrospectives: list[dict],
) -> dict[str, dict]:
    """Compute model policy data: {role:task_type -> {model, sample_count, mean_quality}}.

    Only includes entries with >= 2 observations (enough signal to recommend).
    """
    observations: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for retro in retrospectives:
        task_type = retro.get("task_type")
        quality = retro.get("quality_score")
        models = retro.get("model_used_by_agent", {})
        if not isinstance(task_type, str) or not isinstance(models, dict):
            continue
        if not isinstance(quality, (int, float)):
            quality = 0.0
        for role, model in models.items():
            if isinstance(model, str) and model:
                observations.setdefault((role, task_type), []).append((model, float(quality)))
    result: dict[str, dict] = {}
    for (role, task_type), obs_list in observations.items():
        model_scores: dict[str, list[float]] = {}
        for model, quality in obs_list:
            model_scores.setdefault(model, []).append(quality)
        eligible = [(m, s) for m, s in model_scores.items() if len(s) >= 2]
        if not eligible:
            continue
        ranked = sorted(eligible, key=lambda x: -_mean(x[1]))
        best_model = ranked[0][0]
        scores = ranked[0][1]
        key = f"{role}:{task_type}"
        result[key] = {
            "model": best_model,
            "sample_count": len(scores),
            "mean_quality": round(_mean(scores), 4),
        }
    return result


def _build_skip_policy_data(
    retrospectives: list[dict],
    auditor_roles: list[str],
) -> dict[str, dict]:
    """Compute skip policy data: {auditor -> {threshold, confidence}}.

    Skip-exempt auditors are excluded.
    """
    streaks: dict[str, list[float]] = {}
    for item in retrospectives:
        for role, value in item.get("auditor_zero_finding_streaks", {}).items():
            if isinstance(role, str) and isinstance(value, (int, float)):
                streaks.setdefault(role, []).append(float(value))
    result: dict[str, dict] = {}
    for role in auditor_roles:
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        avg_streak = _mean(streaks.get(role, []))
        threshold = max(3, min(6, int(round(avg_streak)) or 3))
        confidence = min(CONFIDENCE_MAX_CLAMP, round(avg_streak / UCB_COLD_START_MINIMUM, 2))
        result[role] = {
            "threshold": threshold,
            "confidence": confidence,
        }
    return result


def _build_route_policy_data(
    task_types: list[str],
    executor_roles: list[str],
    auditor_roles: list[str],
    registry: dict,
) -> dict[str, dict]:
    """Compute route policy data: {role:task_type -> {mode, agent_path, agent_name, composite_score}}."""
    result: dict[str, dict] = {}
    for item in registry.get("agents", []):
        role = str(item.get("role", ""))
        task_type = str(item.get("task_type", ""))
        if not role or not task_type:
            continue
        key = f"{role}:{task_type}"
        composite = float(item.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
        result[key] = {
            "mode": str(item.get("mode", "shadow")),
            "agent_path": str(item.get("path", "")),
            "agent_name": str(item.get("agent_name", "")),
            "composite_score": round(composite, 4),
        }
    for role in sorted(set(executor_roles + auditor_roles)):
        for task_type in task_types:
            key = f"{role}:{task_type}"
            if key not in result:
                result[key] = {
                    "mode": "generic",
                    "agent_path": None,
                    "agent_name": None,
                    "composite_score": 0.0,
                }
    return result


def _migrate_model_overrides(root: Path, model_policy_data: dict[str, dict]) -> dict[str, dict]:
    """Migrate model_overrides from policy.json into model-policy.json data.

    Entries from policy.json are seeded with source "explicit_policy".
    Existing explicit_policy entries in model-policy.json are preserved.
    After successful migration, model_overrides is removed from policy.json.
    Returns the merged model policy data.
    """
    persistent = _persistent_project_dir(root)
    policy_path = persistent / "policy.json"
    mp_path = persistent / "model-policy.json"

    # Load existing model-policy.json to preserve explicit_policy entries
    existing_mp: dict[str, dict] = {}
    if mp_path.exists():
        try:
            existing_mp = load_json(mp_path)
        except (json.JSONDecodeError, OSError):
            existing_mp = {}

    # Load policy.json for migration
    policy: dict = {}
    if policy_path.exists():
        try:
            policy = load_json(policy_path)
        except (json.JSONDecodeError, OSError):
            policy = {}

    overrides = policy.get("model_overrides", {})

    # Start with computed data
    merged = dict(model_policy_data)

    # Layer on migrated overrides with source "explicit_policy",
    # but do not overwrite existing explicit_policy entries
    for key, model in overrides.items():
        if isinstance(model, str) and key not in existing_mp:
            merged[key] = {
                "model": model,
                "source": "explicit_policy",
                "sample_count": 0,
                "mean_quality": 0.0,
            }

    # Preserve existing explicit_policy entries from model-policy.json
    for key, entry in existing_mp.items():
        if isinstance(entry, dict) and entry.get("source") == "explicit_policy":
            merged[key] = entry

    # Remove model_overrides from policy.json after migration
    if "model_overrides" in policy:
        del policy["model_overrides"]
        write_json(policy_path, policy)

    return merged


# Current wasted_spawns signal version. Keep in lockstep with
# hooks/lib_validate.WASTED_SPAWNS_SIGNAL_VERSION. When the signal's meaning
# changes, only retrospectives stamped with the current version are aggregated,
# so the learned thresholds cold-start instead of mixing incomparable
# observations. See docs/spawn-budget-convergence-design.md.
_WASTED_SPAWNS_SIGNAL_VERSION = 2


def _build_spawn_budget_policy_data(retrospectives: list[dict]) -> dict:
    """Compute spawn-budget policy data from task retrospectives.

    Returns a dict matching this exact schema:

        {
          "version": 1,
          "computed_at": "<ISO 8601 UTC string>",
          "per_task_class": {
            "<task_type>:<risk_level>": {
              "waste_count_baseline": <float>,
              "waste_count_stddev": <float>,
              "n_observations": <int>,
              "threshold_count": <int>
            }
          },
          "global_fallback": {"threshold_count": 2},
          "exempt_auditors": ["<auditor_name>", ...]
        }

    Aggregation rules (AC-2 / AC-3):

    * The ``wasted_spawns`` field is sourced from each retrospective dict
      (top-level field). Source-of-truth: ``task-retrospective.json``
      schema, top-level ``wasted_spawns`` (int|float). Missing or
      non-numeric values are coerced to ``0``.
    * Retrospectives missing ``task_type`` or ``risk_level`` are SKIPPED
      entirely — no ``unknown:unknown`` aggregation is performed.
    * Observations are grouped by ``f"{task_type}:{risk_level}"``.
    * A ``per_task_class`` entry is emitted only when ``n_observations
      >= 3``. Classes with fewer observations are omitted.
    * ``baseline = statistics.mean(values)``. ``stddev =
      statistics.stdev(values)`` (sample stddev, requires n>=2). For
      n==1 the stddev is forced to ``0.0``. n>=3 with all-identical
      observations yields ``stddev == 0.0`` and
      ``threshold_count = max(2, ceil(baseline))``.
    * ``threshold_count = max(2, min(10, math.ceil(baseline + 2 *
      stddev)))`` — capped to the inclusive range [2, 10].

    Exempt-auditor rules (AC-4):

    * For each task (keyed by ``task_id``), select the most-recent
      retrospective by ``computed_at`` (ISO descending). When two
      retros for the same task share an identical ``computed_at``,
      the tiebreaker prefers the retro with the higher
      ``wasted_spawns`` count (more conservative — picks the noisier
      observation).
    * From the selected retro's ``auditor_zero_finding_streaks`` dict
      (top-level, ``dict[str, int|float]``), collect every key whose
      value is ``>= 5``.
    * Union across all tasks; deduplicate; sort ascending.

    Source-of-truth field paths (``task-retrospective.json``):
    ``wasted_spawns``, ``task_type``, ``risk_level``, ``task_id``,
    ``computed_at``, ``auditor_zero_finding_streaks``.
    """
    # ----- per-task-class aggregation (AC-2 / AC-3) --------------------
    grouped: dict[str, list[float]] = {}
    for retro in retrospectives:
        if not isinstance(retro, dict):
            continue
        # Cold-start guard: only aggregate observations recorded under the
        # current wasted_spawns signal. Pre-migration retrospectives (missing
        # the tag, or an older version) counted clean audits — incomparable to
        # the repair-non-convergence signal — and are skipped here so the
        # learned thresholds re-learn cleanly.
        if retro.get("wasted_spawns_signal_version") != _WASTED_SPAWNS_SIGNAL_VERSION:
            continue
        task_type = retro.get("task_type")
        risk_level = retro.get("risk_level")
        # Hard rule: skip retrospectives missing task_type / risk_level
        # — never silently default to "unknown:unknown".
        if not isinstance(task_type, str) or not task_type:
            continue
        if not isinstance(risk_level, str) or not risk_level:
            continue
        raw_waste = retro.get("wasted_spawns", 0)
        if isinstance(raw_waste, bool) or not isinstance(raw_waste, (int, float)):
            # bool is a subclass of int — exclude explicitly. Non-numeric
            # values become 0 per AC-2 ("default 0 when absent or
            # non-numeric").
            waste = 0.0
        else:
            waste = float(raw_waste)
        key = f"{task_type}:{risk_level}"
        grouped.setdefault(key, []).append(waste)

    per_task_class: dict[str, dict] = {}
    for key, values in grouped.items():
        n = len(values)
        if n < 3:
            # AC-2: only emit entries when n_observations >= 3.
            continue
        baseline = statistics.mean(values)
        # statistics.stdev requires n >= 2; n is already >= 3 here, but
        # we keep the n==1 branch documented for AC-3 completeness.
        stddev = statistics.stdev(values) if n >= 2 else 0.0
        threshold_count = max(2, min(10, math.ceil(baseline + 2 * stddev)))
        per_task_class[key] = {
            "waste_count_baseline": float(baseline),
            "waste_count_stddev": float(stddev),
            "n_observations": int(n),
            "threshold_count": int(threshold_count),
        }

    # ----- exempt auditors (AC-4) --------------------------------------
    # Pick the most-recent retrospective per task_id by computed_at (ISO
    # descending). Tiebreaker on identical computed_at: higher
    # wasted_spawns wins (the more conservative — noisier — observation).
    most_recent_per_task: dict[str, dict] = {}
    for retro in retrospectives:
        if not isinstance(retro, dict):
            continue
        task_id = retro.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        computed_at = retro.get("computed_at")
        if not isinstance(computed_at, str):
            computed_at = ""
        existing = most_recent_per_task.get(task_id)
        if existing is None:
            most_recent_per_task[task_id] = retro
            continue
        existing_ts = existing.get("computed_at")
        if not isinstance(existing_ts, str):
            existing_ts = ""
        if computed_at > existing_ts:
            most_recent_per_task[task_id] = retro
        elif computed_at == existing_ts:
            new_waste = retro.get("wasted_spawns", 0)
            old_waste = existing.get("wasted_spawns", 0)
            if not isinstance(new_waste, (int, float)) or isinstance(new_waste, bool):
                new_waste = 0
            if not isinstance(old_waste, (int, float)) or isinstance(old_waste, bool):
                old_waste = 0
            if float(new_waste) > float(old_waste):
                most_recent_per_task[task_id] = retro

    exempt: set[str] = set()
    for retro in most_recent_per_task.values():
        streaks = retro.get("auditor_zero_finding_streaks", {})
        if not isinstance(streaks, dict):
            continue
        for auditor_name, value in streaks.items():
            if not isinstance(auditor_name, str) or not auditor_name:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if float(value) >= 5:
                exempt.add(auditor_name)

    return {
        "version": _WASTED_SPAWNS_SIGNAL_VERSION,
        "computed_at": now_iso(),
        "per_task_class": per_task_class,
        "global_fallback": {"threshold_count": 2},
        "exempt_auditors": sorted(exempt),
    }


def _write_policy_json_files(
    root: Path,
    model_policy_data: dict[str, dict],
    skip_policy_data: dict[str, dict],
    route_policy_data: dict[str, dict],
    spawn_budget_policy_data: dict,
) -> None:
    """Write all four JSON policy files atomically."""
    persistent = _persistent_project_dir(root)
    write_json(persistent / "model-policy.json", model_policy_data)
    write_json(persistent / "skip-policy.json", skip_policy_data)
    write_json(persistent / "route-policy.json", route_policy_data)
    write_json(persistent / "spawn-budget-policy.json", spawn_budget_policy_data)


def build_patterns_markdown(
    root: Path,
    *,
    model_policy_data: dict[str, dict] | None = None,
    skip_policy_data: dict[str, dict] | None = None,
    route_policy_data: dict[str, dict] | None = None,
) -> str:
    """Build the project_rules.md content.

    When policy data dicts are provided, uses them directly (data-first path).
    When called without them, computes the data inline (backward compat).
    """
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)

    if model_policy_data is None:
        model_policy_data = _build_model_policy_data(retrospectives)
    if skip_policy_data is None:
        skip_policy_data = _build_skip_policy_data(retrospectives, auditor_roles)
    if route_policy_data is None:
        route_policy_data = _build_route_policy_data(
            task_types, executor_roles, auditor_roles, registry
        )

    active_routes = sum(
        1 for item in registry.get("agents", []) if item.get("route_allowed")
    )
    lines = [
        "# Project Rules",
        "",
        f"Generated at: {now_iso()}",
        f"Source task count: {len(retrospectives)}",
        f"Learned component count: {len(registry.get('agents', []))}",
        f"Active learned routes: {active_routes}",
        "",
        "## Prevention Rules",
        "",
        "| Executor | Rule | Source |",
        "|----------|------|--------|",
        "| ui-executor | Validate visual intent against existing dashboard structure before broad styling changes. | default |",
        "| backend-executor | Prefer narrower edits with explicit acceptance coverage and test verification. | default |",
        "| testing-executor | Capture failing expectations before broad test rewrites. | default |",
    ]
    # Append learned prevention rules from postmortem_improve
    learned_rules = _load_prevention_rules(root)
    for rule in learned_rules:
        cat = rule.get("category", "unknown")
        text = rule.get("rule", "")
        lines.append(f"| all | {text} | learned:{cat} |")
    lines.extend([
        "",
        "## Gold Standard Instances",
        "",
        "| Task ID | Type | Why It Matters |",
        "|---------|------|----------------|",
    ])
    scored_tasks = [
        item
        for item in retrospectives
        if isinstance(item.get("task_id"), str)
        and isinstance(item.get("task_type"), str)
        and isinstance(item.get("quality_score"), (int, float))
    ]
    top_tasks = sorted(
        scored_tasks,
        key=lambda item: (
            -float(item.get("quality_score", 0.0)),
            -float(item.get("efficiency_score", 0.0) or 0.0),
            str(item.get("task_id")),
        ),
    )[:5]
    if top_tasks:
        for item in top_tasks:
            task_id = str(item.get("task_id"))
            task_type = str(item.get("task_type"))
            score = float(item.get("quality_score", 0.0))
            lines.append(
                f"| {task_id} | {task_type} | High-quality completed task (quality {score:.2f}). |"
            )
    else:
        lines.append("| none | n/a | No completed retrospectives available yet. |")
    # Data tables (effectiveness scores, model policy, skip policy, agent routing)
    # are NOT included in the markdown. They are written as JSON files
    # (model-policy.json, skip-policy.json, route-policy.json) which the
    # router reads directly. Keeping them out of the markdown avoids
    # injecting ~60 lines of numeric tables into the LLM context window
    # where they serve no purpose (the LLM can't act on EMA scores).

    return "\n".join(lines) + "\n"


def write_patterns(root: Path) -> dict:
    """Generate policy data, write JSON files, then render and write markdown."""
    steps_completed: list[str] = []

    # Step 1: Collect source data
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)

    if not retrospectives:
        log_event(root, "learn_step_failed", step="collect", reason="no retrospectives found")
        return {"written_at": now_iso(), "written_paths": [], "failed_paths": [], "error": "no retrospectives"}

    steps_completed.append(f"collect:{len(retrospectives)} retros")
    log_event(root, "learn_step", step="collect", retrospective_count=len(retrospectives),
              task_types=list(task_types), executor_roles=list(executor_roles), auditor_roles=list(auditor_roles))

    # Step 2: Compute effectiveness scores (EMA) and derive policies.
    # Pass `root` so the EMA ingest cross-checks retrospective
    # `learned:X` agent_source claims against .dynos/events.jsonl.
    effectiveness_scores = compute_effectiveness_scores(retrospectives, root=root)
    ema_model_policy = derive_model_policy(effectiveness_scores)
    ema_skip_policy = derive_skip_policy(effectiveness_scores)

    steps_completed.append(f"ema:{len(effectiveness_scores)} scores, {len(ema_model_policy)} model, {len(ema_skip_policy)} skip")
    log_event(root, "learn_step", step="ema_compute", effectiveness_count=len(effectiveness_scores),
              model_policy_count=len(ema_model_policy), skip_policy_count=len(ema_skip_policy))

    # Step 2b: Legacy policy computation (backward compat, feeds markdown tables)
    model_policy_data = _build_model_policy_data(retrospectives)
    skip_policy_data = _build_skip_policy_data(retrospectives, auditor_roles)

    # Merge EMA-derived policies into JSON files (EMA takes precedence)
    for key, entry in ema_model_policy.items():
        if key not in model_policy_data:
            model_policy_data[key] = {"model": entry["model"], "sample_count": 0, "mean_quality": 0.0}
        model_policy_data[key]["model"] = entry["model"]
    for key, entry in ema_skip_policy.items():
        skip_policy_data[key] = entry
    route_policy_data = _build_route_policy_data(
        task_types, executor_roles, auditor_roles, registry
    )

    # Step 3: Migrate model_overrides from policy.json (merges with computed data)
    model_policy_data = _migrate_model_overrides(root, model_policy_data)

    steps_completed.append(f"route:{len(route_policy_data)} routes")

    # Step 4: Write JSON policy files + effectiveness scores.
    # spawn_budget_policy_data is computed here (not passed as None) so
    # that empty retrospectives still yield a valid schema with
    # per_task_class={}, exempt_auditors=[].
    spawn_budget_policy_data = _build_spawn_budget_policy_data(retrospectives)
    _write_policy_json_files(
        root,
        model_policy_data,
        skip_policy_data,
        route_policy_data,
        spawn_budget_policy_data,
    )
    persistent = _persistent_project_dir(root)
    write_json(persistent / "effectiveness-scores.json", effectiveness_scores)
    steps_completed.append("json_written")
    log_event(root, "learn_step", step="write_json_policies",
              model_count=len(model_policy_data), skip_count=len(skip_policy_data),
              route_count=len(route_policy_data),
              spawn_budget_class_count=len(spawn_budget_policy_data.get("per_task_class", {})),
              spawn_budget_exempt_count=len(spawn_budget_policy_data.get("exempt_auditors", [])))

    # Step 5: Render markdown from the same data
    content = build_patterns_markdown(
        root,
        model_policy_data=model_policy_data,
        skip_policy_data=skip_policy_data,
        route_policy_data=route_policy_data,
    )

    # Step 6: Write markdown files
    written: list[str] = []
    failed: list[dict] = []
    for path in pattern_paths(root):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            written.append(str(path))
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})
    steps_completed.append(f"markdown:{len(written)} written, {len(failed)} failed")
    log_event(root, "patterns_written", written_count=len(written), failed_count=len(failed),
              retrospective_count=len(retrospectives), local_path=str(local_patterns_path(root)),
              steps_completed=steps_completed)
    return {
        "written_at": now_iso(),
        "written_paths": written,
        "failed_paths": failed,
        "local_path": str(local_patterns_path(root)),
    }


def cmd_write_patterns(args: argparse.Namespace) -> int:
    result = write_patterns(Path(args.root).resolve())
    print(json.dumps(result, indent=2))
    return 0


def cmd_effectiveness(args: argparse.Namespace) -> int:
    """Compute and print effectiveness scores from retrospectives."""
    root = Path(args.root).resolve()
    retrospectives = collect_retrospectives(root)
    scores = compute_effectiveness_scores(retrospectives, root=root)
    output = {
        "effectiveness_scores": scores,
        "model_policy": derive_model_policy(scores),
        "skip_policy": derive_skip_policy(scores),
        "routing_composites": compute_routing_composite(scores),
    }
    print(json.dumps(output, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    # Default (no subcommand): write patterns
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_write_patterns)

    eff = sub.add_parser("effectiveness", help="Compute effectiveness scores from retrospectives")
    eff.add_argument("--root", default=".")
    eff.set_defaults(func=cmd_effectiveness)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
