#!/usr/bin/env python3
"""Deterministic routing decisions for dynos-work.

Reads project-local policy, patterns, and learned-agent registry.
Returns structured spawn decisions. No prompt interpretation needed.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import hashlib
import math
import os
import shutil
import tempfile
import zlib
from pathlib import Path

# PRO-007: pin git binary to absolute path resolved at import time.
_GIT: str | None = shutil.which("git")

from lib_core import (
    _persistent_project_dir,
    _safe_float,
    benchmark_history_path,
    collect_retrospectives,
    is_learning_enabled,
    load_json,
    now_iso,
    project_policy,
    write_json,
)
from lib_defaults import (
    DEFAULT_SKIP_THRESHOLD as _DEFAULT_SKIP_THRESHOLD,
    ROUTER_WEIGHT_COST,
    ROUTER_WEIGHT_EFFICIENCY,
    ROUTER_WEIGHT_QUALITY,
    UCB_COLD_START_MINIMUM,
    UCB_EXPLORATION_CONSTANT,
)
from lib_models import (
    TIER_DEEP as _TIER_DEEP,
    resolve_model_for_tier as _resolve_model_for_tier,
    ROLE_DEFAULT_TIERS as _ROLE_DEFAULT_TIERS,
    TIER_BALANCED as _TIER_BALANCED,
    valid_models_for_host as _valid_models_for_host,
    ALL_TIERS as _ALL_TIERS,
    HOST_CLAUDE as _HOST_CLAUDE,
    TIER_FRONTIER as _TIER_FRONTIER,
    clamp_model_to_role_ceiling as _clamp_model_to_role_ceiling,
    max_tier_for_role as _max_tier_for_role,
    model_rank as _model_rank,
    tier_rank as _tier_rank,
)
from lib_host import detect_host as _detect_host
import lib_ensemble as _lib_ensemble
from lib_log import log_event
from lib_receipts import (
    INJECTED_AUDITOR_PROMPTS_DIR,
    INJECTED_PLANNER_PROMPTS_DIR,
    INJECTED_PROMPTS_DIR,
)
from lib_tool_budget import compute_segment_budget
from lib_registry import ensure_learned_registry
from write_policy import WriteAttempt, _get_capability_key, require_write_allowed


# ---------------------------------------------------------------------------
# Router context — cached data for a single plan build
# ---------------------------------------------------------------------------

class RouterContext:
    """Pre-loads all data needed for routing decisions once per plan build.

    Eliminates redundant file reads: policy, patterns, retrospectives,
    and registry are each read exactly once and shared across all
    resolve_model / resolve_route / resolve_skip calls.
    """

    def __init__(self, root: Path):
        self.root = root
        self._policy: dict | None = None
        self._patterns_text: str | None = None
        self._retrospectives: list[dict] | None = None
        self._registry: dict | None = None
        self._learning: bool | None = None

    @property
    def policy(self) -> dict:
        if self._policy is None:
            self._policy = project_policy(self.root)
        return self._policy

    @property
    def patterns_text(self) -> str | None:
        """Deprecated — data tables removed from markdown. Use effectiveness_scores instead."""
        if self._patterns_text is None:
            path = _persistent_project_dir(self.root) / "project_rules.md"
            try:
                self._patterns_text = path.read_text()
            except (FileNotFoundError, OSError):
                self._patterns_text = ""
        return self._patterns_text or None

    @property
    def effectiveness_scores(self) -> list[dict]:
        """Read effectiveness scores from JSON (no longer parsed from markdown)."""
        if not hasattr(self, "_effectiveness"):
            path = _persistent_project_dir(self.root) / "effectiveness-scores.json"
            try:
                data = load_json(path)
                self._effectiveness = data if isinstance(data, list) else []
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self._effectiveness = []
        return self._effectiveness

    @property
    def retrospectives(self) -> list[dict]:
        if self._retrospectives is None:
            self._retrospectives = collect_retrospectives(self.root)
        return self._retrospectives

    @property
    def registry(self) -> dict:
        if self._registry is None:
            self._registry = _read_learned_registry(self.root)
        return self._registry

    @property
    def learning_enabled(self) -> bool:
        if self._learning is None:
            self._learning = is_learning_enabled(self.root)
        return self._learning

    @property
    def benchmark_history(self) -> dict:
        """Cached read of benchmark history JSON.

        Avoids re-reading benchmark/history.json once per role inside the
        same plan build. Returns an empty dict-shaped record on miss.
        """
        if not hasattr(self, "_benchmark_history"):
            try:
                self._benchmark_history = load_json(benchmark_history_path(self.root))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self._benchmark_history = {"runs": []}
        return self._benchmark_history


def _read_learned_registry(root: Path) -> dict:
    """Read the learned agent registry without creating files (pure read)."""
    from lib_registry import learned_registry_path
    path = learned_registry_path(root)
    if not path.exists():
        return {"agents": [], "benchmarks": []}
    try:
        data = load_json(path)
        if isinstance(data, dict) and "agents" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"agents": [], "benchmarks": []}


# ---------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------

import random

VALID_MODELS = list(_valid_models_for_host(_HOST_CLAUDE))
DEFAULT_EPSILON = 0.1  # 10% exploration rate


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

# PRO-006: non-blocking inject-prompt size guardrail. Both
# cmd_planner_inject_prompt and cmd_audit_inject_prompt emit a
# planner_prompt_oversize event when stdin exceeds this threshold; the
# sidecar is still written and the command still exits 0.
_MAX_INJECTED_PROMPT_BYTES: int = 100_000


def _default_model_for_role(role: str, host: str) -> str | None:
    """Return the default model for a role on the given host.

    Looks up the role's default tier from ROLE_DEFAULT_TIERS, then
    resolves to a model via TIER_TO_MODEL for the given host.
    Returns None when the host has no model mapping (e.g. Codex).
    """
    tier = _ROLE_DEFAULT_TIERS.get(role, _TIER_BALANCED)
    return _resolve_model_for_tier(host, tier)


def _explorable_models_for_role(role: str, host: str = _HOST_CLAUDE) -> list[str]:
    """Return the epsilon-greedy exploration arms permitted for *role*.

    Excludes models above the role's tier ceiling and the security floor
    model. Sorted so the arm list is deterministic across processes —
    ``random.choice`` over a set iteration order is not reproducible.
    """
    ceiling_rank = _tier_rank(_max_tier_for_role(role))
    floor_model = _resolve_model_for_tier(_HOST_CLAUDE, _TIER_DEEP)
    return sorted(
        m
        for m in _valid_models_for_host(host)
        if m != floor_model and _model_rank(m) <= ceiling_rank
    )


def _build_ensemble_context(host: str) -> dict:
    """Return the ensemble context dict for the given host.

    When all tier arms resolve to None for the host (null mapping,
    e.g. Codex), ensemble is disabled with reason='host_null_mapping'.
    Otherwise returns {'ensemble': True} so callers can proceed normally.
    """
    all_null = all(_resolve_model_for_tier(host, tier) is None for tier in _ALL_TIERS)
    if all_null:
        return {"ensemble": False, "reason": "host_null_mapping"}
    return {"ensemble": True, "reason": "host_has_models"}


def _build_escalation_result(host: str) -> dict:
    """Return the escalation result dict for the given host.

    When the deep-tier model resolves to None for the host (null mapping,
    e.g. Codex), escalation is unavailable. The returned dict contains
    escalation_unavailable=True and does NOT include a model_override key.
    Otherwise returns {'model_override': <deep_model>}.
    """
    deep_model = _resolve_model_for_tier(host, _TIER_DEEP)
    if deep_model is None:
        return {"escalation_unavailable": True}
    return {"model_override": deep_model}


def _read_policy_json(root: Path, filename: str, key: str) -> dict | None:
    """Read a value from a JSON policy file in the persistent project dir.

    Returns the value dict for *key* if found, None otherwise.
    Gracefully handles missing files and corrupt JSON.
    """
    try:
        policy_path = _persistent_project_dir(root) / filename
        data = json.loads(policy_path.read_text())
        if isinstance(data, dict) and key in data:
            return data[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


COMPOSITE_WEIGHTS = (ROUTER_WEIGHT_QUALITY, ROUTER_WEIGHT_COST, ROUTER_WEIGHT_EFFICIENCY)
DEFAULT_UCB_C = UCB_EXPLORATION_CONSTANT
COLD_START_MINIMUM = UCB_COLD_START_MINIMUM


def _parse_effectiveness_scores(
    path: Path, role: str, task_type: str,
) -> list[dict]:
    """Parse the Effectiveness Scores table from a file path."""
    try:
        text = path.read_text()
    except OSError:
        return []
    return _parse_effectiveness_scores_from_text(text, role, task_type)


def _parse_effectiveness_scores_from_text(
    text: str, role: str, task_type: str,
) -> list[dict]:
    """Parse the Effectiveness Scores table for a given role and task_type.

    Returns a list of dicts with keys: model, quality, cost, efficiency, samples.
    Aggregates across source values (generic + learned) per model.
    """
    rows: dict[str, dict] = {}  # keyed by model

    in_table = False
    for line in text.splitlines():
        if "## Effectiveness Scores" in line:
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|") or "---" in line or "Role" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        # Columns: Role, Model, Task Type, Source, Quality EMA, Cost EMA, Efficiency EMA, Sample Count, Updated
        if len(parts) < 8:
            continue
        if parts[0] != role or parts[2] != task_type:
            continue

        m = parts[1]
        try:
            quality = float(parts[4])
            cost = float(parts[5])
            efficiency = float(parts[6])
            samples = int(float(parts[7]))
        except (ValueError, IndexError):
            continue

        if m in rows:
            # Aggregate: weighted average by sample count
            existing = rows[m]
            total = existing["samples"] + samples
            if total > 0:
                w_old = existing["samples"] / total
                w_new = samples / total
                existing["quality"] = existing["quality"] * w_old + quality * w_new
                existing["cost"] = existing["cost"] * w_old + cost * w_new
                existing["efficiency"] = existing["efficiency"] * w_old + efficiency * w_new
                existing["samples"] = total
        else:
            rows[m] = {
                "model": m,
                "quality": quality,
                "cost": cost,
                "efficiency": efficiency,
                "samples": max(samples, 1),
            }

    return list(rows.values())


def _filter_effectiveness_scores(
    scores: list[dict], role: str, task_type: str,
) -> list[dict]:
    """Filter and aggregate effectiveness scores for a given role and task_type.

    Reads from the JSON effectiveness-scores.json (no markdown parsing).
    Aggregates across source values (generic + learned) per model.
    """
    rows: dict[str, dict] = {}
    for entry in scores:
        if entry.get("role") != role or entry.get("task_type") != task_type:
            continue
        m = entry.get("model", "")
        if not m or m not in _valid_models_for_host(_HOST_CLAUDE):
            continue
        try:
            quality = float(entry.get("quality_ema", 0))
            cost = float(entry.get("cost_ema", 0))
            efficiency = float(entry.get("efficiency_ema", 0))
            samples = int(entry.get("sample_count", 1))
        except (ValueError, TypeError):
            continue

        if m in rows:
            existing = rows[m]
            total = existing["samples"] + samples
            if total > 0:
                w_old = existing["samples"] / total
                w_new = samples / total
                existing["quality"] = existing["quality"] * w_old + quality * w_new
                existing["cost"] = existing["cost"] * w_old + cost * w_new
                existing["efficiency"] = existing["efficiency"] * w_old + efficiency * w_new
                existing["samples"] = total
        else:
            rows[m] = {
                "model": m,
                "quality": quality,
                "cost": cost,
                "efficiency": efficiency,
                "samples": max(samples, 1),
            }
    return list(rows.values())


def _ucb_select_model(
    candidates: list[dict], exploration_c: float,
) -> dict | None:
    """Select the best model using UCB1.

    Each candidate has: model, quality, cost, efficiency, samples.
    Returns the winning candidate dict with added ucb_score and exploration_bonus,
    or None if no candidates.
    """
    if not candidates:
        return None

    total_pulls = sum(c["samples"] for c in candidates)
    if total_pulls < COLD_START_MINIMUM:
        return None

    best = None
    best_ucb = -1.0

    for c in candidates:
        wq, wc, we = COMPOSITE_WEIGHTS
        composite = wq * c["quality"] + wc * c["cost"] + we * c["efficiency"]

        if c["samples"] > 0 and total_pulls > 0:
            exploration = exploration_c * math.sqrt(math.log(total_pulls) / c["samples"])
        else:
            exploration = float("inf")  # untried arm gets maximum exploration

        ucb_score = composite + exploration

        if ucb_score > best_ucb:
            best_ucb = ucb_score
            best = {**c, "ucb_score": round(ucb_score, 4), "exploration_bonus": round(exploration, 4)}

    return best


BENCHMARK_MODEL_MIN_SAMPLES = 2


def _benchmark_model_for_agent(root: Path, role: str, task_type: str, ctx: RouterContext | None = None) -> dict | None:
    """Find the best model for a role based on learned agent benchmark runs.

    Looks up the active learned agent for (role, task_type), then filters
    benchmark history runs for that agent. Groups by model, computes mean
    composite score, and returns the best model with >= BENCHMARK_MODEL_MIN_SAMPLES.

    When *ctx* is provided, registry and benchmark history are read from the
    shared cache instead of re-reading the JSON files for every role lookup.

    Returns {"model": str, "mean_composite": float, "sample_count": int} or None.
    """
    registry = ctx.registry if ctx else _read_learned_registry(root)
    # Find matching active learned agent
    agent_name = None
    for agent in registry.get("agents", []):
        if (
            agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("status") not in ("archived", "demoted_on_regression")
            and agent.get("mode") in ("replace", "alongside")
        ):
            agent_name = agent.get("agent_name")
            break
    if not agent_name:
        return None

    # Load benchmark history (shared via ctx when available, else fresh read)
    if ctx is not None:
        history = ctx.benchmark_history
    else:
        try:
            history = load_json(benchmark_history_path(root))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
    runs = history.get("runs", [])

    # Group composite scores by model for matching runs
    model_scores: dict[str, list[float]] = {}
    wq, wc, we = COMPOSITE_WEIGHTS
    for run in runs:
        if run.get("target_name") != agent_name:
            continue
        if run.get("role") != role or run.get("task_type") != task_type:
            continue
        model = run.get("model")
        if not model:
            continue
        # Compute composite from run-level scores or evaluation candidate
        q = _safe_float(run.get("quality_score"))
        c = _safe_float(run.get("cost_score"))
        e = _safe_float(run.get("efficiency_score"))
        if q or c or e:
            composite = wq * q + wc * c + we * e
        else:
            evaluation = run.get("evaluation", {})
            candidate = evaluation.get("candidate", {})
            composite = candidate.get("mean_composite")
        if composite is None:
            continue
        model_scores.setdefault(model, []).append(float(composite))

    # Pick the model with highest mean composite (min samples required)
    best_model = None
    best_composite = -1.0
    best_count = 0
    for model, scores in model_scores.items():
        if len(scores) < BENCHMARK_MODEL_MIN_SAMPLES:
            continue
        mean = sum(scores) / len(scores)
        if mean > best_composite:
            best_composite = mean
            best_model = model
            best_count = len(scores)

    if best_model is None:
        return None
    return {
        "model": best_model,
        "source": "benchmark_model",
        "mean_composite": round(best_composite, 4),
        "sample_count": best_count,
    }


def _resolve_model_uncapped(
    root: Path,
    role: str,
    task_type: str,
    ctx: RouterContext | None = None,
    host: str | None = None,
) -> dict:
    """Determine which model an agent should use, before ceiling enforcement.

    Callers want ``resolve_model``, which wraps this with the role tier
    ceiling. This inner function exists so that every one of the selection
    paths below funnels through exactly one enforcement point, rather than
    each return site having to remember to clamp.

    Priority order:
      0.  Epsilon-greedy exploration       -> source: "exploration"
      1.  policy.json model_overrides     -> source: "explicit_policy"
      2.  UCB1 over effectiveness scores  -> source: "ucb"
      2b. Benchmark model performance     -> source: "benchmark_model"
      3.  model-policy.json fallback      -> source: "learned_history"
      4.  Patterns markdown table         -> source: "learned_history"
      5.  No match                        -> source: "default"
      *   Security floor enforcement      -> source: "security_floor"

    The ``host`` parameter controls security-floor resolution. When not
    provided (or None), the active host is resolved internally via
    _detect_host() so callers that monkeypatch this function with a
    host-less signature continue to work. Under hosts whose TIER_DEEP
    mapping is None (e.g. codex), the security-auditor floor cannot be
    satisfied. In that case the returned dict carries ``model=None`` and
    ``floor_unmet=True`` so callers and receipt writers can record the
    condition without raising.

    Returns {"model": str|None, "source": str, ...}.
    """
    # Resolve host internally when not supplied so callers do not need to pass
    # it. This keeps monkeypatched call surfaces (e.g. lambda root, role,
    # task_type, ctx=None: ...) backward-compatible.
    if host is None:
        host = _detect_host()

    policy = ctx.policy if ctx else project_policy(root)
    key = f"{role}:{task_type}"

    # Security floor pre-check: when host cannot satisfy the deep-tier floor
    # for security-auditor, emit floor_unmet immediately (before any learned
    # data path). This is the only wired production location that sets the flag.
    if role == "security-auditor":
        _host_floor_model = _resolve_model_for_tier(host, _TIER_DEEP)
        if _host_floor_model is None:
            result: dict = {
                "model": None,
                "resolved_model": None,
                "floor_unmet": True,
                "source": "security_floor",
            }
            log_event(
                root, "router_model_decision",
                role=role, task_type=task_type,
                model=None, source="security_floor",
                floor_unmet=True,
            )
            return result

    # 1. Explicit policy.json overrides (highest priority — never overridden)
    model_overrides = policy.get("model_overrides", {})
    model = model_overrides.get(key) or model_overrides.get(role)
    if model:
        result = {"model": model, "source": "explicit_policy"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 1b. Epsilon-greedy exploration — randomly try a different model
    # to feed the UCB1 bandit with multi-arm data. Fires AFTER explicit
    # policy (user config always wins) but BEFORE learned data tiers.
    epsilon = float(policy.get("exploration_epsilon", DEFAULT_EPSILON))
    _security_floor_model = _resolve_model_for_tier(_HOST_CLAUDE, _TIER_DEEP)
    if (
        epsilon > 0
        and role != "security-auditor"
        and (ctx.learning_enabled if ctx else is_learning_enabled(root))
        and random.random() < epsilon
    ):
        # Exploration arms are bounded by the role's tier ceiling. Drawing an
        # above-ceiling arm and clamping it afterwards would burn the draw and
        # write a duplicate observation for the ceiling model instead.
        _arms = _explorable_models_for_role(role)
        if not _arms:
            _arms = [m for m in VALID_MODELS if m != _security_floor_model]
        model = random.choice(_arms)
        result = {"model": model, "source": "exploration", "epsilon": epsilon}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=model, source="exploration")
        return result

    # Steps 2-4 use learned data — skip when learning is disabled.
    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        result = {"model": _default_model_for_role(role, _HOST_CLAUDE), "source": "default"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source="default (learning_enabled=false)")
        return result

    # 2. UCB1 over effectiveness scores (read from JSON, not markdown)
    if ctx:
        all_scores = ctx.effectiveness_scores
    else:
        try:
            all_scores = load_json(_persistent_project_dir(root) / "effectiveness-scores.json")
            if not isinstance(all_scores, list):
                all_scores = []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            all_scores = []
    # Below-floor is a rank comparison, not an inequality against the deep-tier
    # literal. With a tier above deep in the ladder, `m != <deep model>` would
    # classify the frontier model as below-floor and downgrade
    # security-auditor from frontier back to deep.
    _floor_rank = _tier_rank(_TIER_DEEP)
    _non_floor_claude_models = {
        m for m in _valid_models_for_host(_HOST_CLAUDE) if _model_rank(m) < _floor_rank
    }
    if all_scores:
        candidates = _filter_effectiveness_scores(all_scores, role, task_type)
        if candidates:
            exploration_c = float(policy.get("ucb_exploration_constant", DEFAULT_UCB_C))
            winner = _ucb_select_model(candidates, exploration_c)
            if winner:
                model = winner["model"]
                source = "ucb"
                result = {"model": model, "source": source,
                          "ucb_score": winner["ucb_score"],
                          "exploration_bonus": winner["exploration_bonus"]}
                # Security floor: security-auditor never below deep-tier model
                if role == "security-auditor" and model in _non_floor_claude_models:
                    result["model"] = _security_floor_model
                    result["source"] = "security_floor"
                log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
                return result

    # 2b. Benchmark model performance — learned agent benchmark runs grouped by model
    benchmark_result = _benchmark_model_for_agent(root, role, task_type, ctx=ctx)
    if benchmark_result:
        model = benchmark_result["model"]
        result = {"model": model, "source": "benchmark_model",
                  "mean_composite": benchmark_result["mean_composite"],
                  "sample_count": benchmark_result["sample_count"]}
        # Security floor: security-auditor never below deep-tier model
        if role == "security-auditor" and model in _non_floor_claude_models:
            result["model"] = _security_floor_model
            result["source"] = "security_floor"
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 3. model-policy.json fallback (backward compat for pre-UCB projects)
    entry = _read_policy_json(root, "model-policy.json", key)
    if entry and isinstance(entry, dict) and entry.get("model"):
        model = entry["model"]
        # Security floor
        if role == "security-auditor" and model in _non_floor_claude_models:
            result = {"model": _security_floor_model, "source": "security_floor"}
            log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
            return result
        result = {"model": model, "source": "learned_history"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 4. No data — default
    if role == "security-auditor":
        result = {"model": _security_floor_model, "source": "security_floor"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    result = {"model": _default_model_for_role(role, _HOST_CLAUDE), "source": "default"}
    log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
    return result


def resolve_model(
    root: Path,
    role: str,
    task_type: str,
    ctx: RouterContext | None = None,
    host: str | None = None,
) -> dict:
    """Determine which model an agent should use, capped at the role ceiling.

    Thin wrapper over ``_resolve_model_uncapped``. Every selection path —
    explicit policy override, exploration, UCB, benchmark, learned history,
    default — passes through here, so no path can hand a role a model above
    the tier ceiling declared in lib_models.ROLE_TIER_CEILINGS. In practice
    this is what keeps the frontier tier off executor roles even when a
    project's policy.json or learned effectiveness data asks for it.

    When a clamp fires, ``source`` is suffixed with ``+tier_ceiling`` and the
    pre-clamp value is preserved on ``uncapped_model`` so receipts still show
    what the selector actually wanted.
    """
    result = _resolve_model_uncapped(root, role, task_type, ctx=ctx, host=host)
    if not isinstance(result, dict):
        return result

    selected = result.get("model")
    capped = _clamp_model_to_role_ceiling(role, selected, _HOST_CLAUDE)
    if capped == selected:
        return result

    result["uncapped_model"] = selected
    result["model"] = capped
    if "resolved_model" in result:
        result["resolved_model"] = capped
    result["tier_ceiling"] = _max_tier_for_role(role)
    result["source"] = f"{result.get('source', 'unknown')}+tier_ceiling"
    log_event(
        root,
        "router_model_ceiling_clamp",
        role=role,
        task_type=task_type,
        requested_model=selected,
        model=capped,
        tier_ceiling=result["tier_ceiling"],
    )
    return result


def _parse_model_from_patterns(path: Path, role: str, task_type: str) -> str | None:
    """Parse Model Policy table from project_rules.md."""
    text = path.read_text()
    in_table = False
    for line in text.splitlines():
        if "## Model Policy" in line:
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|") or "---" in line or "Role" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3 and parts[0] == role and parts[1] == task_type:
            model = parts[2]
            return model if model != "default" else None
    return None


# ---------------------------------------------------------------------------
# Skip decisions
# ---------------------------------------------------------------------------

SKIP_EXEMPT = {"security-auditor", "spec-completion-auditor", "claude-md-auditor"}
DEFAULT_SKIP_THRESHOLD = _DEFAULT_SKIP_THRESHOLD


def resolve_skip(root: Path, auditor: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine whether an auditor should be skipped.

    Returns {"skip": bool, "reason": str, "streak": int, "threshold": int}.
    """
    if auditor in SKIP_EXEMPT:
        return {"skip": False, "reason": "skip-exempt", "streak": 0, "threshold": 0}

    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        return {"skip": False, "reason": "learning_enabled=false (no skip)", "streak": 0, "threshold": 0}

    # Get streak from most recent prior task (cached via ctx)
    retros = ctx.retrospectives if ctx else collect_retrospectives(root)
    streak = 0
    if retros:
        latest = retros[-1]
        streaks = latest.get("auditor_zero_finding_streaks", {})
        if isinstance(streaks, dict):
            streak = int(streaks.get(auditor, 0) or 0)

    # Get threshold from patterns or policy
    threshold = _get_skip_threshold(root, auditor)

    skip = streak >= threshold
    reason = f"streak {streak} >= threshold {threshold}" if skip else f"streak {streak} < threshold {threshold}"
    return {"skip": skip, "reason": reason, "streak": streak, "threshold": threshold}


def _get_skip_threshold(root: Path, auditor: str) -> int:
    """Read skip threshold for *auditor*.

    Priority: skip-policy.json -> DEFAULT_SKIP_THRESHOLD.
    """
    entry = _read_policy_json(root, "skip-policy.json", auditor)
    if entry and isinstance(entry, dict) and "threshold" in entry:
        return int(entry["threshold"])

    return DEFAULT_SKIP_THRESHOLD


# ---------------------------------------------------------------------------
# Agent routing
# ---------------------------------------------------------------------------


def resolve_route(root: Path, role: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine whether to use generic, learned, or alongside agent.

    Returns {
        "mode": "generic"|"learned"|"alongside",
        "agent_path": str|None,
        "agent_name": str|None,
        "composite_score": float,
        "source": str
    }.
    """
    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "learning_enabled=false",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode="generic", agent_name=None, composite_score=0.0, source="learning_enabled=false")
        return result

    registry = ctx.registry if ctx else _read_learned_registry(root)
    agents = registry.get("agents", [])

    # Find matching learned agent
    learned = None
    for agent in agents:
        if (
            agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("status") not in ("archived", "demoted_on_regression")
        ):
            learned = agent
            break

    if not learned:
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "no learned agent",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
        return result

    mode = learned.get("mode", "shadow")
    composite = float(learned.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
    agent_path = learned.get("path", "")
    agent_name = learned.get("agent_name", "")

    # Security-auditor can never be replaced
    if role == "security-auditor" and mode == "replace":
        mode = "alongside"

    # Shadow mode means it's not yet proven — use generic
    if mode == "shadow":
        result = {
            "mode": "generic",
            "agent_path": agent_path,
            "agent_name": agent_name,
            "composite_score": composite,
            "source": f"shadow (not yet promoted): {agent_name}",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
        return result

    # Path validation — resolve against persistent dir
    if agent_path:
        p = Path(agent_path)
        # Try as absolute first, then persistent dir, then repo-relative
        if p.is_absolute():
            full_path = p
        else:
            persistent_root = _persistent_project_dir(root) / "learned-agents"
            full_path = persistent_root / p.name
            if not full_path.exists():
                # Try relative to persistent learned-agents
                full_path = persistent_root / p
            if not full_path.exists():
                # Last resort: repo-relative
                full_path = root / p
        if not full_path.exists():
            result = {
                "mode": "generic",
                "agent_path": None,
                "agent_name": agent_name,
                "composite_score": composite,
                "source": f"learned agent file not found: {agent_path}",
            }
            log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
            return result
        agent_path = str(full_path)

    result = {
        "mode": mode,
        "agent_path": agent_path,
        "agent_name": agent_name,
        "composite_score": composite,
        "source": f"learned:{agent_name}",
    }
    log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
    return result


# ---------------------------------------------------------------------------
# Full spawn plan
# ---------------------------------------------------------------------------

def load_prevention_rules(root: Path) -> list[dict]:
    """Load project-local prevention rules from persistent storage.

    Error semantics (AC 17):
      - If the file is absent → return ``[]``. An un-configured project
        legitimately has no prevention rules; this is not an error.
      - If the file exists but is corrupt (``json.JSONDecodeError``) or
        otherwise unreadable (``OSError`` other than ``FileNotFoundError``,
        e.g. permission denied, I/O error) → emit a ``prevention_rules_corrupt``
        event and RE-RAISE. Silently returning ``[]`` on corruption hides a
        misconfiguration that would otherwise short-circuit every learned
        rule for the session; a loud failure forces operator intervention.

    Callers outside the CLI layer MUST NOT wrap this call in a swallow
    ``try/except`` — the propagation is intentional. CLI dispatchers
    (``cmd_executor_plan`` / ``cmd_inject_prompt``) catch at the top level
    and exit 2 per the documented exit-code contract.
    """
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    if not rules_path.exists():
        return []
    try:
        data = load_json(rules_path)
    except FileNotFoundError:
        # Race between exists() and load — treat as absent. No event
        # because this is the benign "file was removed mid-read" case.
        return []
    except (json.JSONDecodeError, OSError) as exc:
        log_event(
            root,
            "prevention_rules_corrupt",
            path=str(rules_path),
            error=str(exc),
        )
        raise
    if not isinstance(data, dict):
        # Malformed top-level shape (e.g. a bare list). Treat as corrupt:
        # emit the event and raise so callers surface the misconfiguration
        # the same way they would for JSONDecodeError.
        exc = ValueError(
            f"prevention-rules.json top-level must be an object (got {type(data).__name__})"
        )
        log_event(
            root,
            "prevention_rules_corrupt",
            path=str(rules_path),
            error=str(exc),
        )
        raise exc
    return data.get("rules", [])


# Ensemble voting defaults — overridable via .dynos/config/policy.json
ENSEMBLE_SAMPLE_RATE: float = 0.20
_DEFAULT_ENSEMBLE_AUDITORS = {
    "security-auditor",
    "db-schema-auditor",
    "threat-model-auditor",
    "supply-chain-auditor",
}

# Auditors whose verdict comes from deterministic analysis (AST, glob,
# rule lookup, structured JSON) rather than from LLM judgment that varies
# across model tiers. Running the fast→balanced→deep tier cascade against
# deterministic input is pure waste — the second and third spawns see the
# same evidence and produce the same verdict. These auditors are pinned
# to ensemble=false regardless of risk_level, sampling roll, or any
# user-provided ensemble_auditors override (task-20260430-008).
_DETERMINISTIC_FIRST_AUDITORS: frozenset[str] = frozenset({
    "claude-md-auditor",
})
_DEFAULT_ENSEMBLE_VOTING_MODELS = [
    _resolve_model_for_tier(_HOST_CLAUDE, "fast"),
    _resolve_model_for_tier(_HOST_CLAUDE, "balanced"),
]
# Ensemble escalation is the one path that puts the frontier tier in front of
# auditors: the fast/balanced arms vote first, and only a disagreement spends a
# frontier spawn to break the tie. Auditors keep their ROLE_DEFAULT_TIERS
# defaults, so a clean first pass never pays for it. Falls back to the deep
# tier on hosts with no frontier mapping (e.g. codex resolves both to None,
# which _build_escalation_result already reports as escalation_unavailable).
_DEFAULT_ENSEMBLE_ESCALATION_MODEL = (
    _resolve_model_for_tier(_HOST_CLAUDE, _TIER_FRONTIER)
    or _resolve_model_for_tier(_HOST_CLAUDE, _TIER_DEEP)
)

# Default auditor registry — overridable via .dynos/config/auditors.json
_DEFAULT_AUDITOR_REGISTRY = {
    "always": [
        "spec-completion-auditor",
        "security-auditor",
        "claude-md-auditor",
        "architecture-auditor",
        "test-strategy-auditor",
        "docs-accuracy-auditor",
    ],
    "fast_track": ["spec-completion-auditor", "security-auditor", "claude-md-auditor"],
    "domain_conditional": {
        "ui": ["ui-auditor", "accessibility-auditor", "code-quality-auditor"],
        "db": [
            "db-schema-auditor",
            "data-integrity-auditor",
            "performance-auditor",
            "dead-code-auditor",
            "code-quality-auditor",
        ],
        "backend": [
            "api-contract-auditor",
            "observability-auditor",
            "performance-auditor",
            "dead-code-auditor",
            "code-quality-auditor",
        ],
        "ml": ["code-quality-auditor"],
        "testing": ["test-strategy-auditor", "code-quality-auditor"],
        "refactor": ["code-quality-auditor"],
        "infra": [
            "infrastructure-auditor",
            "supply-chain-auditor",
            "release-auditor",
            "code-quality-auditor",
        ],
        "security": ["threat-model-auditor", "privacy-auditor", "code-quality-auditor"],
        "migration": ["release-auditor", "data-integrity-auditor"],
        "docs": ["docs-accuracy-auditor"],
    },
}


def _load_auditor_registry(root: Path) -> dict:
    """Load auditor registry from .dynos/config/auditors.json with fallback to defaults."""
    config_path = root / ".dynos" / "config" / "auditors.json"
    try:
        data = load_json(config_path)
        if isinstance(data, dict) and "always" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return _DEFAULT_AUDITOR_REGISTRY


def _load_ensemble_config(config: dict) -> tuple[set[str], list[str], str]:
    """Load ensemble voting config from .dynos/config/policy.json with fallback to defaults."""
    auditors = set(config.get("ensemble_auditors", _DEFAULT_ENSEMBLE_AUDITORS))
    models = config.get("ensemble_voting_models", _DEFAULT_ENSEMBLE_VOTING_MODELS)
    escalation = config.get("ensemble_escalation_model", _DEFAULT_ENSEMBLE_ESCALATION_MODEL)
    return auditors, models, escalation


def _claude_md_risk_gate_skip(
    risk_level: str, diff_files: list[str] | None
) -> tuple[bool, str]:
    """Decide whether claude-md-auditor can be skipped on perf grounds.

    Returns (should_skip, reason). The auditor was introduced on 2026-04-30
    as a third mandatory blocking auditor with a "cannot be skipped"
    directive. Per the latency investigation, it is the largest single
    driver of audit-phase slowdown when applied to low/medium-risk tasks
    that do not touch CLAUDE.md — the auditor exists to catch drift
    between code and project rules; if neither is plausibly in scope the
    cost is wasted.

    Skip iff: risk_level in {low, medium} AND CLAUDE.md is not in the
    diff. Otherwise run. Fail-open when diff_files is None: partial
    information must not silently skip a blocking auditor.
    """
    if diff_files is None:
        return False, "diff_files unavailable; fail-open and run auditor"
    if risk_level in {"high", "critical"}:
        return False, f"risk_level={risk_level} requires the auditor"
    # Match CLAUDE.md at any path depth (root or nested), case-sensitive.
    for f in diff_files:
        if f == "CLAUDE.md" or f.endswith("/CLAUDE.md"):
            return False, f"CLAUDE.md in diff ({f})"
    return (
        True,
        f"risk_level={risk_level} and CLAUDE.md not in diff "
        f"({len(diff_files)} files); skipping perf-non-essential auditor",
    )


def build_audit_plan(
    root: Path,
    task_type: str,
    domains: list[str],
    fast_track: bool = False,
    *,
    risk_level: str = "medium",
    task_id: str = "",
    ctx: RouterContext | None = None,
    diff_files: list[str] | None = None,
) -> dict:
    """Build a complete, deterministic audit spawn plan.

    Reads .dynos/config/auditors.json for the auditor registry and
    .dynos/config/policy.json for ensemble voting config. Falls back
    to hardcoded defaults when config files are missing.

    When *ctx* is provided, its cached reads (policy, retrospectives,
    registry, effectiveness scores, benchmark history) are reused.
    When *ctx* is None, a fresh RouterContext is constructed locally
    so external callers keep working unchanged.
    """
    ctx = ctx or RouterContext(root)
    registry = _load_auditor_registry(root)

    # Load user config from .dynos/config/policy.json
    config_policy_path = root / ".dynos" / "config" / "policy.json"
    try:
        user_config = load_json(config_policy_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        user_config = {}

    ensemble_auditors, ensemble_models, ensemble_escalation = _load_ensemble_config(user_config)

    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "domains": domains,
        "fast_track": fast_track,
        "auditors": [],
    }

    # Determine which auditors are eligible from the registry
    if fast_track:
        eligible = list(registry.get("fast_track", _DEFAULT_AUDITOR_REGISTRY["fast_track"]))
    else:
        eligible = list(registry.get("always", _DEFAULT_AUDITOR_REGISTRY["always"]))
        domain_map = registry.get("domain_conditional", _DEFAULT_AUDITOR_REGISTRY["domain_conditional"])
        for domain in domains:
            for auditor in domain_map.get(domain, []):
                if auditor not in eligible:
                    eligible.append(auditor)

    for auditor in eligible:
        # Perf risk gate for claude-md-auditor (task-20260430-007). Runs
        # before the regular skip check because SKIP_EXEMPT would otherwise
        # force-include this auditor on every task.
        if auditor == "claude-md-auditor":
            should_skip, gate_reason = _claude_md_risk_gate_skip(risk_level, diff_files)
            if should_skip:
                plan["auditors"].append({
                    "name": auditor,
                    "action": "skip",
                    "reason": f"claude-md risk-gate: {gate_reason}",
                    "streak": 0,
                    "threshold": 0,
                })
                continue
        # Skip check (uses cached retrospectives via ctx)
        skip_decision = resolve_skip(root, auditor, task_type, ctx=ctx)
        if skip_decision["skip"]:
            plan["auditors"].append({
                "name": auditor,
                "action": "skip",
                "reason": skip_decision["reason"],
                "streak": skip_decision["streak"],
                "threshold": skip_decision["threshold"],
            })
            continue

        # Model selection (uses cached policy + patterns via ctx).
        # resolve_model detects the active host internally; floor_unmet=True
        # flows through when the security-auditor floor cannot be satisfied.
        model_decision = resolve_model(root, auditor, task_type, ctx=ctx)

        # Fast-track model override: fast-tier for spec-completion
        if fast_track and auditor == "spec-completion-auditor" and model_decision["source"] == "default":
            model_decision = {"model": _resolve_model_for_tier(_HOST_CLAUDE, "fast"), "source": "fast_track_override"}
        if auditor in _DETERMINISTIC_FIRST_AUDITORS and model_decision["source"] != "explicit_policy":
            model_decision = {
                "model": _default_model_for_role(auditor, _HOST_CLAUDE),
                "source": "deterministic_default",
            }

        # Route selection (uses cached registry via ctx)
        route_decision = resolve_route(root, auditor, task_type, ctx=ctx)

        entry: dict = {
            "name": auditor,
            "action": "spawn",
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
        }
        # Carry floor_unmet into the plan entry when the security floor cannot
        # be satisfied (binding decision 2: artifact/receipt records the flag).
        if model_decision.get("floor_unmet"):
            entry["floor_unmet"] = True

        # Ensemble sampling — risk_level-gated with deterministic CRC32 tie-break
        if auditor in _DETERMINISTIC_FIRST_AUDITORS:
            # Pinned off: deterministic-first auditors don't benefit from
            # the fast→balanced→deep tier cascade. See _DETERMINISTIC_FIRST_AUDITORS
            # comment for rationale.
            entry["ensemble"] = False
            reason = "deterministic_first_auditor"
        elif auditor in ensemble_auditors:
            # Baseline ensemble auditors always run ensemble
            entry["ensemble"] = True
            reason = "always_ensemble_auditor"
        elif fast_track:
            entry["ensemble"] = False
            reason = "fast_track"
        elif risk_level in {"high", "critical"}:
            entry["ensemble"] = True
            reason = "high_risk"
        else:
            # Medium / low: probabilistic sampling keyed on task_id + auditor
            seed_key = f"{task_id}|{auditor}".encode()
            sampled = zlib.crc32(seed_key) % 10000 < int(ENSEMBLE_SAMPLE_RATE * 10000)
            entry["ensemble"] = sampled
            reason = "sampled_in" if sampled else "sampled_out"

        # Wire _build_ensemble_context(host): when the active host maps all
        # ensemble tiers to None (null mapping, e.g. Codex), override ensemble
        # to False and set reason to host_null_mapping (binding decision 5).
        # Resolve host internally so callers don't need to pass it — same
        # pattern as resolve_model which also detects internally.
        active_host = _detect_host()
        ensemble_ctx = _build_ensemble_context(active_host)
        if not ensemble_ctx.get("ensemble", True):
            entry["ensemble"] = False
            reason = ensemble_ctx.get("reason", "host_null_mapping")

        if entry["ensemble"]:
            entry["ensemble_voting_models"] = list(ensemble_models)
            entry["ensemble_escalation_model"] = ensemble_escalation

        log_event(root, "auditor_ensemble_decision",
                  auditor=auditor, sampled=entry["ensemble"],
                  reason=reason, risk_level=risk_level)

        plan["auditors"].append(entry)

    log_event(root, "router_audit_plan", task_type=task_type, domains=domains, fast_track=fast_track, auditor_count=len(plan["auditors"]), auditors=[{"name": a["name"], "action": a["action"], "model": a.get("model")} for a in plan["auditors"]])
    return plan


def _measure_loc(path: Path) -> int:
    """Return the number of lines in *path*, or 0 on any OSError.

    Used to compute a per-file LOC surcharge for compute_segment_budget.
    Returns 0 for files that do not yet exist or cannot be read.
    """
    try:
        return sum(1 for _ in path.open("rb"))
    except OSError:
        return 0


def build_executor_plan(
    root: Path,
    task_type: str,
    segments: list[dict],
    *,
    ctx: RouterContext | None = None,
    include_enforced: bool = False,
) -> dict:
    """Build a complete, deterministic execution spawn plan.

    Returns structured decisions for each segment's executor.

    When *ctx* is provided, its cached reads are reused. When *ctx* is None,
    a fresh RouterContext is constructed locally so external callers keep
    working unchanged.

    Prevention-rule filtering (AC 13):
        Rules with a structured `template` other than "advisory" are
        considered "enforced" — they have a runtime / static-check
        backstop and shipping their text into the executor prompt
        wastes tokens. By default only `advisory` rules (and legacy
        rules missing a `template` field, for backward-compat during
        the migration window) reach the prompt.

        Pass `include_enforced=True` to disable the filter and include
        every rule, e.g. for diagnostics or for audits that want to
        verify the LLM still sees enforced-rule rationale.

        Each segment plan entry exposes `prevention_rules_omitted: int`
        so callers can prove the filter ran.
    """
    ctx = ctx or RouterContext(root)
    all_rules = load_prevention_rules(root)

    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "segments": [],
    }

    for seg in segments:
        executor = seg.get("executor", "")
        seg_id = seg.get("id", "")

        # resolve_model detects the active host internally; floor_unmet=True
        # flows through when the security-auditor floor cannot be satisfied.
        model_decision = resolve_model(root, executor, task_type, ctx=ctx)
        route_decision = resolve_route(root, executor, task_type, ctx=ctx)

        # Step 1: filter to rules that target this executor (or all).
        # `auditor-only` rules (aggregate category-trend signals) are
        # excluded — executors cannot act on cross-task category warnings
        # the way auditors reviewing code can.
        executor_scoped: list[dict] = [
            r for r in all_rules
            if isinstance(r, dict) and r.get("rule")
            and r.get("executor") != "auditor-only"
            and (not r.get("executor") or r.get("executor") == executor)
        ]

        # Step 2: AC 13 template filter. Advisory + missing-template
        # stay; everything else is omitted unless include_enforced is set.
        prevention_rules_omitted = 0
        if include_enforced:
            kept = executor_scoped
        else:
            kept = []
            for r in executor_scoped:
                tmpl = r.get("template")
                # Missing template → backward-compat advisory; keep.
                # Explicit "advisory" → keep.
                # Anything else (every_name_in_X_satisfies_Y, signature_lock, ...)
                # has a runtime/static backstop, so omit it from the prompt.
                if tmpl is None or tmpl == "advisory":
                    kept.append(r)
                else:
                    prevention_rules_omitted += 1

        executor_rules = [r["rule"] for r in kept]

        # AC-4: per-segment tool-call budget. Computed from files_expected
        # length and the resolved model so executors get a deterministic,
        # spec-derived ceiling instead of an unbounded tool quota.
        files_expected = seg.get("files_expected") or []
        files_loc = [_measure_loc(Path(f)) for f in files_expected]
        tool_budget = compute_segment_budget(len(files_expected), model_decision["model"], files_loc=files_loc)

        seg_entry: dict = {
            "segment_id": seg_id,
            "executor": executor,
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
            "prevention_rules": executor_rules,
            "prevention_rules_omitted": prevention_rules_omitted,
            "tool_budget": tool_budget,
        }
        # Carry floor_unmet into the segment entry when the security floor cannot
        # be satisfied (binding decision 2: receipt records the flag).
        if model_decision.get("floor_unmet"):
            seg_entry["floor_unmet"] = True

        plan["segments"].append(seg_entry)

    log_event(root, "router_executor_plan", task_type=task_type, segment_count=len(plan["segments"]), include_enforced=include_enforced, segments=[{"segment_id": s["segment_id"], "executor": s["executor"], "model": s.get("model"), "model_source": s.get("model_source"), "prevention_rules_omitted": s.get("prevention_rules_omitted", 0)} for s in plan["segments"]])
    return plan


# ---------------------------------------------------------------------------
# Learned agent prompt injection
# ---------------------------------------------------------------------------


def build_executor_prompt(
    root: Path,
    segment: dict,
    plan_entry: dict,
    base_prompt: str,
) -> str:
    """Build the complete executor prompt with learned agent rules injected.

    This is the ONLY function that should be used to construct executor prompts.
    It deterministically injects learned agent instructions when the router
    assigns mode=replace or mode=alongside.

    Args:
        root: Project root.
        segment: The segment dict from execution-graph.json.
        plan_entry: The segment entry from build_executor_plan() output.
        base_prompt: The base instruction prompt for the executor.

    Returns:
        The complete prompt string with learned rules appended.
    """
    agent_path = plan_entry.get("agent_path")
    route_mode = plan_entry.get("route_mode", "generic")
    agent_name = plan_entry.get("agent_name")
    prevention_rules = plan_entry.get("prevention_rules", [])

    parts = [base_prompt]

    # Inject learned agent instructions
    if route_mode in ("replace", "alongside") and agent_path:
        try:
            p = Path(agent_path)
            if not p.is_absolute():
                p = root / p
            if p.exists():
                agent_content = p.read_text().strip()
                # Strip frontmatter if present
                if agent_content.startswith("---"):
                    end = agent_content.find("---", 3)
                    if end != -1:
                        agent_content = agent_content[end + 3:].strip()
                parts.append(
                    f"\n\n## Learned Agent Instructions ({agent_name})\n"
                    f"You are running as the **{agent_name}** learned agent (mode={route_mode}). "
                    f"These rules were learned from past failures and regressions. Treat them as hard constraints unless the current spec explicitly overrides them. "
                    f"If one of these rules is violated without justification, assume you are recreating a known bug.\n\n"
                    f"{agent_content}"
                )
                log_event(
                    root,
                    "learned_agent_applied",
                    agent_name=agent_name,
                    agent_path=str(agent_path),
                    route_mode=route_mode,
                    segment_id=plan_entry.get("segment_id", ""),
                )
            else:
                log_event(
                    root,
                    "learned_agent_missing",
                    agent_name=agent_name,
                    agent_path=str(agent_path),
                    segment_id=plan_entry.get("segment_id", ""),
                )
        except Exception as exc:
            log_event(
                root,
                "learned_agent_error",
                agent_name=agent_name,
                error=str(exc),
            )

    # Inject prevention rules
    if prevention_rules:
        rules_text = "\n".join(f"- {r}" for r in prevention_rules)
        parts.append(
            f"\n\n## Prevention Rules\n"
            f"These patterns have already caused audit findings in real tasks. Do not repeat them. "
            f"Treat each rule below as a known failure mode that must be actively prevented, not passively remembered. "
            f"If you violate one without an explicit, spec-backed reason, assume you are shipping a regression:\n{rules_text}"
        )

    # AC-7: append the tool-use budget block. Read the value straight from
    # plan_entry — never recompute here, so the spawned executor sees the
    # exact same budget that the executor-plan record committed to.
    tool_budget = plan_entry.get("tool_budget")
    if tool_budget is not None:
        seg_id = str(segment.get("id", "") or "")
        evidence_target = f"evidence/{seg_id}.md" if seg_id else "your evidence file"
        expected_artifact = segment.get("expected_artifact") or plan_entry.get("expected_artifact")
        ledger_target = expected_artifact or evidence_target
        # The tool_budget is shown for scoping, but it is deliberately NOT a
        # cutoff: finishing the segment is the priority. Instead of pacing the
        # executor to stop at a budget checkpoint (which left segments abandoned
        # mid-edit with no evidence), the block asks for a resumable progress
        # ledger so a continuation executor can pick the work up where an
        # interrupted one stopped. See `ctl next-continuation`.
        parts.append(
            f"\n\n## Tool-Use Budget\n"
            f"The ~{tool_budget}-tool-call figure for this spawn is an ESTIMATE for "
            f"scoping — NOT a cap. Finishing the segment is the priority: do not stop "
            f"early or leave work half-done to stay under it.\n"
            f"Keep your work resumable in case you are interrupted before finishing. "
            f"Write {ledger_target} EARLY and update it incrementally with a progress "
            f"ledger (done / in-flight / next). Work not written to disk does not exist — "
            f"if you stop, a continuation executor resumes from exactly what is on disk "
            f"and from your ledger, so keep the remaining work recorded there."
        )

    return "\n".join(parts)



# ---------------------------------------------------------------------------
# Executor plan cache — task-local, fingerprint-keyed, replayable
#
# Why this exists:
#   The execute skill calls `inject-prompt` once per segment. Each invocation
#   is a separate Python process that previously rebuilt the executor plan
#   from scratch — re-reading policy.json, effectiveness-scores.json, the
#   retrospective glob, the learned-agent registry, and benchmark history
#   for every single segment.
#
#   For a 10-segment task that means 10 redundant rebuilds of work that is
#   100% deterministic given the same inputs. Worse, when epsilon-greedy
#   exploration is enabled, the per-segment rebuild rolls the dice again,
#   so the model the executor was spawned with (from `executor-plan`) can
#   silently disagree with the model the prompt was injected for.
#
#   The cache:
#     - hashes every input that drives the plan
#     - stores the cached plan under `.dynos/task-{id}/router-cache/`
#     - lets `inject-prompt` reuse the plan when fingerprint matches
#     - guarantees executor-plan and inject-prompt see the same routing
#
# When the cache misses (different fingerprint / cache absent / corrupt),
# inject-prompt falls back to the live `build_executor_plan` path. The cache
# is an optimization; correctness still holds without it.
# ---------------------------------------------------------------------------


_ROUTER_CACHE_VERSION = "1"


def _hash_path(h: "hashlib._Hash", path: Path) -> None:
    """Mix a path's bytes (or marker if missing) into the hash digest."""
    try:
        data = path.read_bytes()
        h.update(b"FILE:")
        h.update(str(path).encode())
        h.update(b":")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    except (FileNotFoundError, IsADirectoryError, OSError):
        h.update(b"MISSING:")
        h.update(str(path).encode())


def _router_inputs_fingerprint(root: Path, task_type: str, graph_path: Path) -> str:
    """Compute a stable SHA256 over every input that drives the executor plan.

    Inputs covered:
      - cache version (so a code change invalidates old caches)
      - task_type
      - graph file contents
      - policy.json (router epsilon, weights, model overrides)
      - effectiveness-scores.json (UCB candidates)
      - model-policy.json (fallback)
      - learned registry.json (route mode / agent path)
      - benchmark history.json (benchmark_model selection)
      - prevention-rules.json (filtered into plan_entry)
      - skip-policy.json (audit-plan parity, harmless to include)

    Any change to any input flips the fingerprint, forcing a rebuild.
    """
    persistent = _persistent_project_dir(root)
    h = hashlib.sha256()
    h.update(b"V:")
    h.update(_ROUTER_CACHE_VERSION.encode())
    h.update(b"\nTASK_TYPE:")
    h.update(task_type.encode())
    h.update(b"\n")
    inputs = [
        graph_path,
        persistent / "policy.json",
        persistent / "effectiveness-scores.json",
        persistent / "model-policy.json",
        persistent / "skip-policy.json",
        persistent / "prevention-rules.json",
        persistent / "learned-agents" / "registry.json",
        persistent / "benchmarks" / "history.json",
        # AC-5: include lib_tool_budget.py content hash so adoption (and any
        # subsequent constant change) invalidates pre-existing routing caches.
        Path(__file__).resolve().parent / "lib_tool_budget.py",
    ]
    for p in inputs:
        _hash_path(h, p)
    return h.hexdigest()


def _task_id_from_graph_path(graph_path: Path) -> str | None:
    """Derive task_id from a graph path like .dynos/task-{id}/execution-graph.json."""
    try:
        parent = graph_path.resolve().parent
        if parent.name.startswith("task-"):
            return parent.name
    except OSError:
        pass
    return None


def _executor_plan_cache_path(root: Path, task_id: str) -> Path:
    """Path to the cached executor plan for this task."""
    return root / ".dynos" / task_id / "router-cache" / "executor-plan.json"


def _write_executor_plan_cache(
    root: Path, task_id: str, plan: dict, fingerprint: str, graph_path: Path,
) -> Path:
    """Write the executor plan + fingerprint to the task-local cache."""
    cache_path = _executor_plan_cache_path(root, task_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": _ROUTER_CACHE_VERSION,
        "fingerprint": fingerprint,
        "generated_at": now_iso(),
        "task_id": task_id,
        "task_type": plan.get("task_type"),
        "graph_path": str(graph_path),
        "plan": plan,
    }
    write_json(cache_path, record)
    return cache_path


def _read_executor_plan_cache(
    root: Path, task_id: str, expected_fingerprint: str,
) -> dict | None:
    """Return the cached plan if fingerprint matches and version is current."""
    cache_path = _executor_plan_cache_path(root, task_id)
    if not cache_path.exists():
        return None
    try:
        record = load_json(cache_path)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("version") != _ROUTER_CACHE_VERSION:
        return None
    if record.get("fingerprint") != expected_fingerprint:
        return None
    plan = record.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list):
        return None
    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_diff_files(root: Path, args: argparse.Namespace) -> list[str] | None:
    """Resolve diff_files for the audit plan from CLI args.

    --diff-files takes precedence (explicit comma-separated list).
    --diff-base SHA falls back to `git diff --name-only <SHA>`.
    Returns None on both-absent or git-failure (fail-open).
    """
    explicit = getattr(args, "diff_files", "") or ""
    if explicit:
        files = [s.strip() for s in explicit.split(",") if s.strip()]
        return files if files else None

    base = getattr(args, "diff_base", "") or ""
    if not base:
        return None

    import subprocess
    try:
        proc = subprocess.run(
            [_GIT or "git", "diff", "--name-only", base],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def cmd_audit_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else []
    diff_files = _resolve_diff_files(root, args)
    plan = build_audit_plan(
        root,
        args.task_type,
        domains,
        fast_track=args.fast_track,
        risk_level=getattr(args, "risk_level", "medium"),
        task_id=getattr(args, "task_id", ""),
        diff_files=diff_files,
    )
    print(json.dumps(plan, indent=2))
    return 0


def cmd_executor_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(json.dumps({"error": f"graph not found: {graph_path}"}))
        return 1
    graph = load_json(graph_path)

    # Build plan once. Write to the task-local cache so per-segment
    # `inject-prompt` invocations can reuse it without rebuilding.
    # AC 13: --include-enforced overrides the template filter so audits
    # / debugging can see every rule, not just advisory ones.
    # AC 19: a corrupt prevention-rules.json raises from
    # load_prevention_rules (via build_executor_plan). Catch at this top
    # level, surface on stderr, and exit 2 — internal-config error.
    try:
        plan = build_executor_plan(
            root,
            args.task_type,
            graph.get("segments", []),
            include_enforced=getattr(args, "include_enforced", False),
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        import sys as _sys
        print(
            json.dumps({"error": f"prevention-rules corrupt: {exc}"}),
            file=_sys.stderr,
        )
        return 2

    task_id = _task_id_from_graph_path(graph_path)
    if task_id:
        try:
            fingerprint = _router_inputs_fingerprint(root, args.task_type, graph_path)
            cache_path = _write_executor_plan_cache(
                root, task_id, plan, fingerprint, graph_path,
            )
            log_event(
                root, "router_cache_write",
                task_id=task_id,
                fingerprint=fingerprint[:12],
                cache_path=str(cache_path),
                segment_count=len(plan.get("segments", [])),
            )
        except OSError as exc:
            # Cache write is best-effort — never block plan delivery on it.
            log_event(root, "router_cache_write_failed", task_id=task_id, error=str(exc))

    print(json.dumps(plan, indent=2))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = {
        "model": resolve_model(root, args.role, args.task_type, host=_detect_host()),
        "skip": resolve_skip(root, args.role, args.task_type),
        "route": resolve_route(root, args.role, args.task_type),
    }
    print(json.dumps(result, indent=2))
    return 0


def _atomic_write_bytes(path: Path, data: bytes, *, attempt: WriteAttempt | None = None) -> None:
    """Atomically write *data* to *path* via tempfile + os.replace.

    Creates parent directories with ``mkdir(parents=True, exist_ok=True)``.
    A retry overwrites the previous file because ``os.replace`` is atomic.
    The temp file lives next to the destination so the replace is on the
    same filesystem (otherwise it would not be atomic on POSIX).
    """
    if attempt is not None:
        require_write_allowed(
            attempt,
            capability_key=_get_capability_key("receipt-writer"),
        )
    else:
        _system_attempt = WriteAttempt(
            role="system",
            task_dir=None,
            path=path,
            operation="modify",
            source="system",
        )
        require_write_allowed(
            _system_attempt,
            capability_key=_get_capability_key("system"),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort cleanup of the orphaned tempfile, then re-raise.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _write_prompt_sidecar(
    sidecar_dir: Path,
    base_name: str,
    prompt_bytes: bytes,
    *,
    task_dir: Path,
) -> str:
    """Write `{base_name}.sha256` and `{base_name}.txt` sidecars atomically.

    Returns the hex digest. The `.sha256` file contains a single line of
    lowercase hex with no trailing newline (per AC 13). The `.txt` file
    contains the raw bytes that hash to that digest.
    """
    digest = hashlib.sha256(prompt_bytes).hexdigest()
    sha_path = sidecar_dir / f"{base_name}.sha256"
    txt_path = sidecar_dir / f"{base_name}.txt"
    # Write the .txt FIRST so a reader that sees the .sha256 can always
    # find the matching bytes. os.replace is atomic on the same FS.
    _atomic_write_bytes(
        txt_path,
        prompt_bytes,
        attempt=WriteAttempt(
            role="receipt-writer",
            task_dir=task_dir,
            path=txt_path,
            operation="modify" if txt_path.exists() else "create",
            source="system",
        ),
    )
    _atomic_write_bytes(
        sha_path,
        digest.encode("ascii"),
        attempt=WriteAttempt(
            role="receipt-writer",
            task_dir=task_dir,
            path=sha_path,
            operation="modify" if sha_path.exists() else "create",
            source="system",
        ),
    )
    return digest


def cmd_inject_prompt(args: argparse.Namespace) -> int:
    """Read base prompt from stdin, inject learned agent rules, print result.

    Plan resolution order:
      1. Try the task-local executor-plan cache (written by `executor-plan`).
         If the fingerprint matches, reuse the cached plan entry — no
         re-derivation, no re-rolled exploration dice, guaranteed agreement
         with the model the executor was spawned under.
      2. On cache miss / corruption / fingerprint drift, fall back to
         building a single-segment plan live. Correctness is preserved.

    At print time also writes atomic sidecars at
    ``.dynos/task-{id}/receipts/_injected-prompts/{segment_id}.sha256``
    and ``.txt`` so receipt validation (AC 12 / AC 13) can prove what
    was actually printed.
    """
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(json.dumps({"error": f"graph not found: {graph_path}"}))
        return 1
    graph = load_json(graph_path)
    segments = graph.get("segments", [])

    # Find the target segment
    target_seg = None
    for seg in segments:
        if seg.get("id") == args.segment_id:
            target_seg = seg
            break
    if not target_seg:
        print(json.dumps({"error": f"segment not found: {args.segment_id}"}))
        return 1

    plan_entry: dict | None = None
    cache_status = "miss"

    # Cache lookup — only attempt if we can derive task_id from graph path
    task_id = _task_id_from_graph_path(graph_path)
    if task_id:
        try:
            fingerprint = _router_inputs_fingerprint(root, args.task_type, graph_path)
        except OSError:
            fingerprint = None
        if fingerprint:
            cached_plan = _read_executor_plan_cache(root, task_id, fingerprint)
            if cached_plan is not None:
                for entry in cached_plan.get("segments", []):
                    if entry.get("segment_id") == args.segment_id:
                        plan_entry = entry
                        cache_status = "hit"
                        break
                if plan_entry is None:
                    cache_status = "stale_segment"
                else:
                    # AC-6: legacy cached plan entries (written before
                    # tool_budget existed) lack the field. Recompute from
                    # the segment's files_expected and attach without
                    # discarding any other plan_entry data.
                    if "tool_budget" not in plan_entry:
                        files_expected = target_seg.get("files_expected") or []
                        files_loc = [_measure_loc(Path(f)) for f in files_expected]
                        plan_entry["tool_budget"] = compute_segment_budget(
                            len(files_expected),
                            plan_entry.get("model", ""),
                            files_loc=files_loc,
                        )
            else:
                cache_status = "fingerprint_drift"

    if plan_entry is None:
        # Fallback: live build for this single segment.
        # AC 13: respect --include-enforced so the fallback path agrees
        # with what `executor-plan` would have produced.
        # AC 19: a corrupt prevention-rules.json raises from
        # load_prevention_rules (via build_executor_plan). Catch here,
        # surface on stderr, and exit 2 — internal-config error.
        try:
            plan = build_executor_plan(
                root,
                args.task_type,
                [target_seg],
                include_enforced=getattr(args, "include_enforced", False),
            )
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            import sys as _sys
            print(
                json.dumps({"error": f"prevention-rules corrupt: {exc}"}),
                file=_sys.stderr,
            )
            return 2
        if not plan["segments"]:
            print(json.dumps({"error": "no plan entry for segment"}))
            return 1
        plan_entry = plan["segments"][0]

    log_event(
        root, "router_cache_lookup",
        scope="inject_prompt",
        task_id=task_id,
        segment_id=args.segment_id,
        status=cache_status,
    )

    # Read base prompt from stdin
    import sys as _sys
    base_prompt = _sys.stdin.read()

    # Build complete prompt
    result = build_executor_prompt(root, target_seg, plan_entry, base_prompt)

    # Bytes that will be emitted to stdout. `print()` appends a newline,
    # so the sidecar must hash exactly those bytes (raw text + "\n") to
    # match what receipt validation re-hashes.
    printed_bytes = (result + "\n").encode("utf-8")

    # Sidecar write — task-local. Only attempted when we can derive a
    # task_id (graph path under .dynos/task-{id}/). Without a task_id
    # there is nowhere to write a per-task receipt sidecar.
    if task_id:
        sidecar_dir = root / ".dynos" / task_id / "receipts" / INJECTED_PROMPTS_DIR
        try:
            digest = _write_prompt_sidecar(
                sidecar_dir,
                args.segment_id,
                printed_bytes,
                task_dir=root / ".dynos" / task_id,
            )
            log_event(
                root, "injected_prompt_sidecar_written",
                task_id=task_id,
                segment_id=args.segment_id,
                sha256=digest,
                sidecar_dir=str(sidecar_dir),
            )
        except OSError as exc:
            # Sidecar write failure must not silently corrupt the receipt
            # chain. Surface it on stderr and fail the command.
            print(
                json.dumps({"error": f"sidecar write failed: {exc}"}),
                file=_sys.stderr,
            )
            return 1

    _sys.stdout.write(result + "\n")
    _sys.stdout.flush()
    return 0


def _safe_repo_line_count(root: Path, rel_path: str) -> int | None:
    try:
        path = (root / rel_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _read_task_json_object(task_dir: Path, name: str) -> dict:
    try:
        data = load_json(task_dir / name)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_task_text_preview(task_dir: Path, name: str, *, max_chars: int = 180) -> str:
    try:
        text = (task_dir / name).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for p in path.glob(pattern) if p.is_file())
    except OSError:
        return 0


def _build_ruthless_audit_brief(root: Path, task_dir: Path, plan: dict, auditor_name: str) -> str:
    """Build a self-implicating audit brief from deterministic task evidence."""
    manifest = _read_task_json_object(task_dir, "manifest.json")
    classification = manifest.get("classification") if isinstance(manifest, dict) else {}
    if not isinstance(classification, dict):
        classification = {}

    raw_input = _read_task_text_preview(task_dir, "raw-input.md")
    task_type = str(classification.get("type") or "unknown")
    domains = classification.get("domains")
    domain_text = ", ".join(str(d) for d in domains) if isinstance(domains, list) and domains else "unknown"
    risk_level = str(classification.get("risk_level") or "unknown")

    diff_base = plan.get("diff_base")
    diff_base_text = str(diff_base) if diff_base else "unknown"
    diff_error = plan.get("diff_error")
    diff_loc = plan.get("diff_loc")
    diff_files_raw = plan.get("diff_files")
    diff_files = [str(f) for f in diff_files_raw] if isinstance(diff_files_raw, list) else []

    file_lines: list[str] = []
    for rel_path in diff_files[:25]:
        line_count = _safe_repo_line_count(root, rel_path)
        suffix = f" ({line_count} lines)" if line_count is not None else ""
        file_lines.append(f"- {rel_path}{suffix}")
    if len(diff_files) > 25:
        file_lines.append(f"- ... {len(diff_files) - 25} more files omitted from this prompt; inspect audit-plan.json.")
    if not file_lines:
        file_lines.append("- No diff_files in audit-plan; establish the changed-file scope before trusting any DONE/PASS claim.")

    artifact_lines = []
    for name in ("spec.md", "plan.md", "execution-graph.json", "repair-log.json", "audit-summary.json", "task-retrospective.json"):
        if (task_dir / name).exists():
            artifact_lines.append(f"- {name}")
    evidence_count = _count_files(task_dir / "evidence", "*")
    report_count = _count_files(task_dir / "audit-reports", "*.json")
    receipt_count = _count_files(task_dir / "receipts", "*.json")
    if evidence_count:
        artifact_lines.append(f"- evidence/* ({evidence_count} files)")
    if report_count:
        artifact_lines.append(f"- audit-reports/*.json ({report_count} files)")
    if receipt_count:
        artifact_lines.append(f"- receipts/*.json ({receipt_count} files)")
    if not artifact_lines:
        artifact_lines.append("- No process artifacts found beyond audit-plan.json.")

    lines = [
        "## Ruthless Audit Brief",
        "Default posture: distrust self-reported DONE/PASS. Treat specs, plans, evidence, receipts, audit reports, retrospectives, and validator output as claims to verify, not proof.",
        "",
        f"Auditor hat: {auditor_name}. Keep this specialist lens, and also audit process integrity.",
        "",
        "Target facts:",
        f"- task_dir: {task_dir.name}",
        f"- task_type: {task_type}; domains: {domain_text}; risk_level: {risk_level}",
        f"- diff_base: {diff_base_text}; changed_files: {len(diff_files)}; diff_loc: {diff_loc if diff_loc is not None else 'unknown'}",
    ]
    if raw_input:
        lines.append(f"- raw request preview: {raw_input}")
    if diff_error:
        lines.append(f"- diff_error: {diff_error}")

    lines.extend([
        "",
        "Changed files to attack first:",
        *file_lines,
        "",
        "Process artifacts to distrust first:",
        *artifact_lines,
        "",
        "Attack directives:",
        "- Reproduce the risky behavior from clean evidence; do not accept narrative summaries.",
        "- Exercise untested runtime surfaces named by the diff, spec, plan, and evidence. Hit valid, empty, malformed, missing-field, wrong-type, nonexistent-ID, and unauthorized cases when applicable.",
        "- Treat inferred contracts, validator accommodations, skipped auditors, cached evidence, carry-forward receipts, and self-graded pass/fail as suspect until verified.",
        "- Every finding must cite file:line evidence or a command plus output. If you cannot cite it, keep digging.",
        "",
        "Process-integrity checks:",
        "- Did any gate pass because an artifact was shaped to satisfy the validator rather than because the underlying behavior is correct?",
        "- Were blockers downgraded, deferred, hidden in residual work, or converted into non-blocking advisories without proof?",
        "- Do evidence files prove behavior with commands and outputs, or merely narrate completion?",
        "",
        "Output pressure: if you report no blocking findings, explicitly name the untested or highest-risk surface you exercised and why it is trustworthy.",
    ])
    return "\n".join(lines)


# Tool-call floors for auditor spawns, keyed by either model-family name
# or routing-tier name (fast/balanced/deep). Mirrors
# lib_tool_budget.STATIC_CAPS_BY_MODEL / STATIC_CAPS_BY_TIER so the auditor
# prompt advertises the same numbers the budget arithmetic uses elsewhere.
_AUDIT_TOOL_BUDGET_BY_LABEL: dict[str, int] = {
    "haiku": 15, "sonnet": 20, "opus": 25,  # noqa: model-literal
    "fast": 15, "balanced": 20, "deep": 25,
}


def _audit_tool_budget(model: "str | None") -> int:
    """Return the tool-call budget advertised to an auditor for this spawn.

    Accepts a model-family name, a routing-tier name, or None. Unknown or
    null labels fall back to the balanced floor (20) — never lower, so the
    write-first checkpoint never collapses to an impossibly small value.
    """
    if model and model in _AUDIT_TOOL_BUDGET_BY_LABEL:
        return _AUDIT_TOOL_BUDGET_BY_LABEL[model]
    return 20


def _build_audit_budget_block(auditor_name: str, model: "str | None") -> str:
    """Deterministic write-first turn-budget block appended to EVERY auditor.

    Auditors run under a hard ``maxTurns`` cap. The recurring failure mode is
    an auditor that reads the entire diff before writing and is force-stopped
    with NO report on disk — which counts as an audit failure and forces a
    re-spawn. This block is injected for every auditor regardless of whether
    its agent file carries a Turn Budget Discipline section, so the discipline
    can never drift out of an individual auditor file again. The numbers track
    the model/tier the router selected for this spawn.
    """
    budget = _audit_tool_budget(model)
    checkpoint = math.ceil(budget / 3)
    return (
        "\n\n## Turn Budget Discipline (mandatory — injected)\n"
        f"You run under a hard limit of about **{budget}** tool calls and are "
        "force-terminated when you reach it. There are no extra turns. Auditors "
        "that read the whole diff before writing routinely hit this limit and "
        "produce NO report — that is an audit failure and forces a re-spawn.\n\n"
        "1. Your FIRST or SECOND tool call MUST create your audit-report JSON "
        "file (the path named in your output contract) containing a "
        "`## Progress Ledger` with `### Done`, `### In-Flight`, and `### Next` "
        "subsections and `status=\"partial\"`. Write this skeleton BEFORE "
        "reading the diff in depth.\n"
        "2. Update that file incrementally as you confirm findings. Work that is "
        "not on disk does not exist.\n"
        f"3. By tool call {checkpoint} the file must already hold real content "
        "(the Progress Ledger plus any findings so far).\n"
        "4. When within 2 tool calls of the limit, STOP investigating and "
        "finalize the report with `status=\"complete\"`. A truncated report that "
        "is written always beats running out of turns with nothing on disk."
    )


def cmd_audit_inject_prompt(args: argparse.Namespace) -> int:
    """Inject learned-auditor instructions into a base auditor prompt.

    Reads the audit plan JSON, locates the named auditor entry, reads the
    base prompt from stdin, and (when ``route_mode in {"replace",
    "alongside"}`` and ``agent_path`` is non-null) appends the learned
    auditor file under a ``## Learned Auditor Instructions`` heading.

    At print time also writes atomic sidecars at
    ``.dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor}-{model}.sha256``
    and ``.txt``. The ``-{model}`` suffix is required to disambiguate
    ensemble auditors that spawn the same auditor name across multiple
    models. When ``--model`` is unspecified or null, the literal string
    ``default`` is used in the filename.

    Final prompt is printed to stdout; sidecar receipts are atomic
    (tempfile + os.replace).
    """
    import sys as _sys
    root = Path(args.root).resolve()
    plan_path = Path(args.audit_plan)
    if not plan_path.exists():
        print(json.dumps({"error": f"audit plan not found: {plan_path}"}))
        return 1

    try:
        plan = load_json(plan_path)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": f"audit plan unreadable: {exc}"}))
        return 1
    if not isinstance(plan, dict):
        print(json.dumps({"error": "audit plan must be a JSON object"}))
        return 1

    auditors = plan.get("auditors")
    if not isinstance(auditors, list):
        print(json.dumps({"error": "audit plan missing 'auditors' list"}))
        return 1

    auditor_name = args.auditor_name
    target: dict | None = None
    for entry in auditors:
        if isinstance(entry, dict) and entry.get("name") == auditor_name:
            target = entry
            break
    if target is None:
        print(json.dumps({"error": f"auditor not found in plan: {auditor_name}"}))
        return 1

    # Ensemble cascade enforcement: an ensemble auditor may only be prepared
    # at the tier `lib_ensemble` says comes next (or at the fast tier, which
    # starts/restarts the cascade). The plan's `model` field is NOT the spawn
    # model for ensemble auditors; refusing here stops the single-spawn
    # shortcut before an Agent call is ever issued.
    if _lib_ensemble.is_ensemble_entry(target):
        cascade = _lib_ensemble.ensemble_next(plan_path.resolve().parent, auditor_name, target)
        voting = cascade.get("voting_models") or []
        fast_tier = voting[0] if voting else None
        requested = args.model
        if cascade.get("status") == _lib_ensemble.STATUS_INVALID:
            print(json.dumps({
                "error": f"ensemble auditor {auditor_name} cannot be evaluated: "
                         + "; ".join(cascade.get("gaps") or []),
                "cascade": cascade,
            }))
            return 1
        if not requested:
            print(json.dumps({
                "error": f"ensemble auditor {auditor_name} requires --model; "
                         f"next cascade step is {cascade.get('model') or fast_tier!r}",
                "cascade": cascade,
            }))
            return 1
        if requested != fast_tier and (
            cascade.get("status") != _lib_ensemble.STATUS_SPAWN
            or cascade.get("model") != requested
        ):
            expected = (
                f"cascade complete (verdict={cascade.get('verdict')!r}); restart at {fast_tier!r}"
                if cascade.get("status") == _lib_ensemble.STATUS_COMPLETE
                else f"next required tier is {cascade.get('model')!r} ({cascade.get('reason')})"
            )
            print(json.dumps({
                "error": f"ensemble auditor {auditor_name}: refusing to prepare a "
                         f"{requested!r} spawn — {expected}. Run `ctl ensemble-next` "
                         "and spawn the model it returns.",
                "cascade": cascade,
            }))
            return 1

    # Read base prompt from stdin
    base_prompt = _sys.stdin.read()

    route_mode = target.get("route_mode", "generic")
    agent_path = target.get("agent_path")

    final_text = base_prompt
    task_dir = plan_path.resolve().parent

    # Ruthless-review style framing: ground each auditor in the real diff and
    # force process artifacts to be treated as claims until verified.
    final_text = (
        final_text.rstrip()
        + "\n\n"
        + _build_ruthless_audit_brief(root, task_dir, plan, auditor_name)
    )

    # Inject aggregate category-trend prevention rules (executor="auditor-only").
    # These are cross-task signals that auditors can act on when reviewing
    # code; executors writing code cannot. They are excluded from every
    # executor prompt by build_executor_plan and surface here instead.
    aggregate_rules = [
        r for r in load_prevention_rules(root)
        if isinstance(r, dict) and r.get("rule")
        and r.get("executor") == "auditor-only"
    ]
    if aggregate_rules:
        aggregate_block = "\n\n## Aggregate Findings Trends\n"
        aggregate_block += (
            "Cross-task signals to scrutinize during this audit. Each entry "
            "names a category whose finding count has crossed the learned "
            "threshold across recent tasks.\n\n"
        )
        for r in aggregate_rules:
            aggregate_block += f"- {r['rule']}\n"
        final_text = final_text.rstrip() + aggregate_block

    if route_mode in ("replace", "alongside") and agent_path:
        try:
            p = Path(agent_path)
            if not p.is_absolute():
                p = root / p
            if p.exists():
                agent_content = p.read_text().strip()
                # Strip frontmatter if present
                if agent_content.startswith("---"):
                    end = agent_content.find("---", 3)
                    if end != -1:
                        agent_content = agent_content[end + 3:].strip()
                final_text = (
                    final_text.rstrip()
                    + "\n\n## Learned Auditor Instructions\n"
                    + f"You are running as the **{auditor_name}** learned auditor "
                    + f"(mode={route_mode}). These rules were learned from past audit "
                    + "findings. Treat them as hard constraints unless the current "
                    + "spec explicitly overrides them.\n\n"
                    + agent_content
                )
                log_event(
                    root, "learned_auditor_applied",
                    auditor_name=auditor_name,
                    agent_path=str(agent_path),
                    route_mode=route_mode,
                )
            else:
                log_event(
                    root, "learned_auditor_missing",
                    auditor_name=auditor_name,
                    agent_path=str(agent_path),
                )
        except OSError as exc:
            log_event(
                root, "learned_auditor_error",
                auditor_name=auditor_name,
                error=str(exc),
            )

    # Deterministic write-first turn-budget block (every auditor, every model).
    # Prefer the model the router selected for this spawn; fall back to the
    # plan entry's model. Injected last so it survives all the prepended
    # framing and is the final, most salient instruction the auditor reads.
    spawn_model = args.model if args.model not in (None, "default") else target.get("model")
    final_text = final_text.rstrip() + _build_audit_budget_block(auditor_name, spawn_model)

    printed_bytes = (final_text + "\n").encode("utf-8")

    # Locate task_dir from the audit plan path: the plan lives at
    # .dynos/task-{id}/audit-plan.json so task_dir is plan.parent.
    if not task_dir.name.startswith("task-"):
        print(json.dumps({
            "error": f"audit plan not under a task dir: {task_dir}",
        }))
        return 1

    # PRO-006: non-blocking size guardrail. Threshold sourced from
    # hooks/router.py::_MAX_INJECTED_PROMPT_BYTES (module-level constant,
    # 100_000 bytes). base_prompt is read as text via _sys.stdin.read(),
    # so encode("utf-8") to measure byte length. Sidecar is still
    # written and exit 0 regardless.
    _prompt_bytes_len = len(base_prompt.encode("utf-8"))
    if _prompt_bytes_len > _MAX_INJECTED_PROMPT_BYTES:
        log_event(
            root,
            "planner_prompt_oversize",
            task_id=task_dir.name,
            size_bytes=_prompt_bytes_len,
            threshold_bytes=_MAX_INJECTED_PROMPT_BYTES,
            phase="auditor",
            auditor_name=args.auditor_name,
        )

    # Per-model disambiguation for ensemble auditors. Falls back to the
    # literal "default" sentinel when --model is unspecified or empty.
    model_label = args.model if args.model else "default"
    base_name = f"{auditor_name}-{model_label}"

    sidecar_dir = task_dir / "receipts" / INJECTED_AUDITOR_PROMPTS_DIR
    try:
        digest = _write_prompt_sidecar(
            sidecar_dir,
            base_name,
            printed_bytes,
            task_dir=task_dir,
        )
    except OSError as exc:
        print(
            json.dumps({"error": f"sidecar write failed: {exc}"}),
            file=_sys.stderr,
        )
        return 1

    log_event(
        root, "injected_auditor_prompt_sidecar_written",
        auditor_name=auditor_name,
        model=model_label,
        sha256=digest,
        sidecar_dir=str(sidecar_dir),
    )

    _sys.stdout.write(final_text + "\n")
    _sys.stdout.flush()
    return 0


def cmd_planner_inject_prompt(args: argparse.Namespace) -> int:
    """Write a per-phase injected-prompt sidecar for a planner spawn.

    Reads the prompt body from stdin as raw bytes, writes atomic sidecar
    files at ``.dynos/task-{task_id}/receipts/_injected-planner-prompts/
    {phase}.sha256`` and ``.txt`` via ``_write_prompt_sidecar`` (the same
    atomic helper the executor and auditor sidecars use). Prints the
    sha256 hex digest to stdout as a single line so the orchestrator can
    capture it and pass it to ``receipt_planner_spawn(...,
    injected_prompt_sha256=<digest>)``.

    The sidecar directory name ``_injected-planner-prompts`` is imported
    from ``lib_receipts.INJECTED_PLANNER_PROMPTS_DIR`` so writer and
    reader share one source of truth for the filename schema.

    The four recognized phases are ``discovery``, ``arch-design``, ``spec``,
    and ``plan`` — one sidecar per phase, matching the planner spawn sites
    in ``skills/start/SKILL.md``. ``arch-design`` is the conditional
    high/critical-risk gate that produces ``design-doc.md`` before spec
    normalization.
    """
    import sys as _sys
    import re as _re
    # Validate --task-id against a strict slug: task- prefix + alphanum +
    # [A-Za-z0-9_.-]. Rejects absolute paths, ``..`` traversal, leading
    # ``.``, and null bytes. Defense against SEC-001.
    if not _re.match(r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$", args.task_id):
        print(
            json.dumps({"error": f"invalid task-id (must match ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$): {args.task_id!r}"}),
            file=_sys.stderr,
        )
        return 1

    try:
        stdin_bytes = _sys.stdin.buffer.read()
    except OSError as exc:
        print(
            json.dumps({"error": f"stdin read failed: {exc}"}),
            file=_sys.stderr,
        )
        return 1

    root = Path(args.root).resolve()

    # PRO-006: non-blocking size guardrail. Threshold sourced from
    # hooks/router.py::_MAX_INJECTED_PROMPT_BYTES (module-level constant,
    # 100_000 bytes). Sidecar is still written and exit 0 regardless.
    if len(stdin_bytes) > _MAX_INJECTED_PROMPT_BYTES:
        log_event(
            root,
            "planner_prompt_oversize",
            task_id=args.task_id,
            size_bytes=len(stdin_bytes),
            threshold_bytes=_MAX_INJECTED_PROMPT_BYTES,
            phase="planner",
        )

    sidecar_dir = (
        root / ".dynos" / args.task_id / "receipts"
        / INJECTED_PLANNER_PROMPTS_DIR
    ).resolve()
    # Defense in depth: assert the resolved path is still under root/.dynos/.
    dynos_root = (root / ".dynos").resolve()
    try:
        sidecar_dir.relative_to(dynos_root)
    except ValueError:
        print(
            json.dumps({"error": f"resolved sidecar path escapes .dynos/: {sidecar_dir}"}),
            file=_sys.stderr,
        )
        return 1

    try:
        digest = _write_prompt_sidecar(
            sidecar_dir,
            args.phase,
            stdin_bytes,
            task_dir=root / ".dynos" / args.task_id,
        )
    except OSError as exc:
        print(
            json.dumps({"error": f"sidecar write failed: {exc}"}),
            file=_sys.stderr,
        )
        return 1

    log_event(
        root,
        "planner_inject_prompt_sidecar_written",
        task_id=args.task_id,
        phase=args.phase,
        sha256=digest,
        sidecar_dir=str(sidecar_dir),
    )

    print(digest)
    return 0


def cmd_router_cache(args: argparse.Namespace) -> int:
    """Inspect the executor-plan cache for a task.

    Prints a JSON record with cache presence, fingerprint, freshness against
    current inputs, and how many segments are stored. Useful for verifying
    that `inject-prompt` will hit cache before spawning N executor agents.
    """
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    task_id = _task_id_from_graph_path(graph_path) if graph_path.exists() else None
    if not task_id:
        print(json.dumps({"error": "could not derive task_id from --graph"}))
        return 1

    cache_path = _executor_plan_cache_path(root, task_id)
    if not cache_path.exists():
        print(json.dumps({
            "task_id": task_id,
            "cache_present": False,
            "cache_path": str(cache_path),
            "status": "absent",
        }, indent=2))
        return 0

    try:
        record = load_json(cache_path)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({
            "task_id": task_id,
            "cache_present": True,
            "cache_path": str(cache_path),
            "status": "corrupt",
            "error": str(exc),
        }, indent=2))
        return 0

    current_fp = _router_inputs_fingerprint(root, args.task_type, graph_path)
    stored_fp = record.get("fingerprint", "")
    fresh = stored_fp == current_fp and record.get("version") == _ROUTER_CACHE_VERSION
    out = {
        "task_id": task_id,
        "cache_present": True,
        "cache_path": str(cache_path),
        "status": "fresh" if fresh else "stale",
        "stored_fingerprint": stored_fp[:16],
        "current_fingerprint": current_fp[:16],
        "version": record.get("version"),
        "generated_at": record.get("generated_at"),
        "segment_count": len(record.get("plan", {}).get("segments", [])),
    }
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ap = subparsers.add_parser("audit-plan", help="Build deterministic audit spawn plan")
    ap.add_argument("--root", default=".")
    ap.add_argument("--task-type", required=True)
    ap.add_argument("--domains", default="")
    ap.add_argument("--fast-track", action="store_true")
    ap.add_argument("--risk-level", dest="risk_level", default="medium",
                    choices=("low", "medium", "high", "critical"),
                    help="Task risk level; controls ensemble sampling gates")
    ap.add_argument("--task-id", dest="task_id", default="",
                    help="Task ID for deterministic CRC32 ensemble sampling")
    ap.add_argument("--diff-base", dest="diff_base", default="",
                    help="Git base SHA; computes diff_files via "
                         "`git diff --name-only <SHA>` for the claude-md "
                         "risk gate. Optional — fail-open without it.")
    ap.add_argument("--diff-files", dest="diff_files", default="",
                    help="Comma-separated explicit diff_files (overrides "
                         "--diff-base). Mainly for tests.")
    ap.set_defaults(func=cmd_audit_plan)

    ep = subparsers.add_parser("executor-plan", help="Build deterministic executor spawn plan")
    ep.add_argument("--root", default=".")
    ep.add_argument("--task-type", required=True)
    ep.add_argument("--graph", required=True)
    ep.add_argument(
        "--include-enforced",
        action="store_true",
        default=False,
        help=(
            "Include enforced (template != advisory) prevention rules in "
            "executor prompts. Default: omit them, since they have a "
            "runtime/static backstop."
        ),
    )
    ep.set_defaults(func=cmd_executor_plan)

    ip = subparsers.add_parser("inject-prompt", help="Inject learned agent rules into executor prompt")
    ip.add_argument("--root", default=".")
    ip.add_argument("--task-type", required=True)
    ip.add_argument("--graph", required=True)
    ip.add_argument("--segment-id", required=True)
    ip.add_argument(
        "--include-enforced",
        action="store_true",
        default=False,
        help=(
            "Include enforced (template != advisory) prevention rules in "
            "the injected prompt. Default: omit them."
        ),
    )
    ip.set_defaults(func=cmd_inject_prompt)

    aip = subparsers.add_parser(
        "audit-inject-prompt",
        help="Inject learned auditor rules into auditor prompt and write per-model sidecar receipts",
    )
    aip.add_argument("--root", default=".")
    aip.add_argument("--task-type", required=True)
    aip.add_argument("--audit-plan", required=True, help="Path to .dynos/task-{id}/audit-plan.json")
    aip.add_argument("--auditor-name", required=True)
    aip.add_argument("--model", default=None, help="Model label for sidecar disambiguation; 'default' if unset")
    aip.set_defaults(func=cmd_audit_inject_prompt)

    pip = subparsers.add_parser(
        "planner-inject-prompt",
        help="Write per-phase planner injected-prompt sidecar (stdin) and print sha256 digest",
    )
    pip.add_argument("--root", default=".")
    pip.add_argument("--task-id", required=True)
    pip.add_argument(
        "--phase",
        required=True,
        choices=("discovery", "arch-design", "spec", "plan"),
        help="Planner phase this sidecar corresponds to",
    )
    pip.set_defaults(func=cmd_planner_inject_prompt)

    rc = subparsers.add_parser("router-cache-status", help="Inspect executor-plan cache freshness for a task")
    rc.add_argument("--root", default=".")
    rc.add_argument("--task-type", required=True)
    rc.add_argument("--graph", required=True)
    rc.set_defaults(func=cmd_router_cache)

    res = subparsers.add_parser("resolve", help="Resolve model/skip/route for one role")
    res.add_argument("role")
    res.add_argument("task_type")
    res.add_argument("--root", default=".")
    res.set_defaults(func=cmd_resolve)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
