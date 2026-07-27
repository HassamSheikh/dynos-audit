"""Role tier ceiling invariant — the frontier tier must never reach executors.

The frontier tier exists for two roles only: the planner (whose output
determines the cost of every downstream spawn) and, via ensemble escalation,
auditors that disagree. Execution is deliberately excluded: executor token
spend scales with diff size, so a frontier-tier executor multiplies the
largest line item in the pipeline.

"Excluded" here means enforced, not defaulted. Every selection path in
router.resolve_model — explicit policy override, epsilon-greedy exploration,
UCB winner, benchmark selection, learned history, plain default — funnels
through a single clamp. These tests drive that clamp through the real
production entry point rather than asserting on the constants.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "hooks"), str(ROOT / "memory")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lib_models  # noqa: E402


EXECUTOR_ROLES = sorted(
    r for r in lib_models.ROLE_DEFAULT_TIERS if r.endswith("-executor")
)
AUDITOR_ROLES = sorted(
    r for r in lib_models.ROLE_DEFAULT_TIERS if r.endswith("-auditor")
)

FRONTIER_MODEL = lib_models.resolve_model_for_tier(
    lib_models.HOST_CLAUDE, lib_models.TIER_FRONTIER
)
DEEP_MODEL = lib_models.resolve_model_for_tier(
    lib_models.HOST_CLAUDE, lib_models.TIER_DEEP
)


def _write_policy(root: Path, policy: dict) -> None:
    """Write .dynos/config/policy.json under *root*."""
    import router

    target_dir = router._persistent_project_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "policy.json").write_text(json.dumps(policy))


# ---------------------------------------------------------------------------
# Ceiling declarations
# ---------------------------------------------------------------------------


class TestCeilingDeclarations:
    def test_sanity_frontier_tier_is_populated(self) -> None:
        """Guard against the suite silently passing on a null mapping."""
        assert FRONTIER_MODEL is not None
        assert DEEP_MODEL is not None
        assert FRONTIER_MODEL != DEEP_MODEL
        assert lib_models.model_rank(FRONTIER_MODEL) > lib_models.model_rank(DEEP_MODEL)

    def test_no_executor_role_may_reach_frontier(self) -> None:
        assert EXECUTOR_ROLES, "expected executor roles in ROLE_DEFAULT_TIERS"
        frontier_rank = lib_models.tier_rank(lib_models.TIER_FRONTIER)
        for role in EXECUTOR_ROLES:
            ceiling = lib_models.max_tier_for_role(role)
            assert lib_models.tier_rank(ceiling) < frontier_rank, (
                f"executor role {role!r} has ceiling {ceiling!r}, which permits "
                f"the frontier tier"
            )

    def test_no_executor_default_tier_is_frontier(self) -> None:
        for role in EXECUTOR_ROLES:
            assert lib_models.ROLE_DEFAULT_TIERS[role] != lib_models.TIER_FRONTIER

    def test_planning_is_frontier_by_default(self) -> None:
        assert lib_models.ROLE_DEFAULT_TIERS["planning"] == lib_models.TIER_FRONTIER
        assert lib_models.max_tier_for_role("planning") == lib_models.TIER_FRONTIER

    def test_auditors_may_reach_frontier_but_do_not_default_there(self) -> None:
        """Auditors get frontier via ensemble escalation, not as a default.

        If an auditor ever defaults to frontier, every clean audit pass pays
        frontier rates — which is the cost profile this design rejected.
        """
        assert AUDITOR_ROLES, "expected auditor roles in ROLE_DEFAULT_TIERS"
        for role in AUDITOR_ROLES:
            assert lib_models.max_tier_for_role(role) == lib_models.TIER_FRONTIER
            assert lib_models.ROLE_DEFAULT_TIERS[role] != lib_models.TIER_FRONTIER

    def test_unknown_role_gets_the_conservative_default_ceiling(self) -> None:
        assert lib_models.max_tier_for_role("some-unregistered-role") == (
            lib_models.DEFAULT_TIER_CEILING
        )
        assert lib_models.DEFAULT_TIER_CEILING != lib_models.TIER_FRONTIER


# ---------------------------------------------------------------------------
# clamp_model_to_role_ceiling unit behaviour
# ---------------------------------------------------------------------------


class TestClampHelper:
    def test_clamps_above_ceiling_model_down(self) -> None:
        assert lib_models.clamp_model_to_role_ceiling(
            "backend-executor", FRONTIER_MODEL
        ) == DEEP_MODEL

    def test_leaves_at_ceiling_model_alone(self) -> None:
        assert lib_models.clamp_model_to_role_ceiling(
            "backend-executor", DEEP_MODEL
        ) == DEEP_MODEL

    def test_leaves_permitted_role_alone(self) -> None:
        assert lib_models.clamp_model_to_role_ceiling(
            "planning", FRONTIER_MODEL
        ) == FRONTIER_MODEL
        assert lib_models.clamp_model_to_role_ceiling(
            "security-auditor", FRONTIER_MODEL
        ) == FRONTIER_MODEL

    def test_never_promotes(self) -> None:
        """The clamp is one-directional — it must not raise a low pick."""
        fast = lib_models.resolve_model_for_tier(
            lib_models.HOST_CLAUDE, lib_models.TIER_FAST
        )
        assert lib_models.clamp_model_to_role_ceiling("planning", fast) == fast

    def test_passthrough_for_unrankable_values(self) -> None:
        """Unknown literals and None are validated elsewhere, not clamped here."""
        assert lib_models.clamp_model_to_role_ceiling("backend-executor", None) is None
        assert lib_models.clamp_model_to_role_ceiling(
            "backend-executor", "gpt-9"
        ) == "gpt-9"

    def test_passthrough_when_host_has_no_ceiling_model(self) -> None:
        """Under codex every tier is None — there is nothing to clamp to."""
        assert lib_models.clamp_model_to_role_ceiling(
            "backend-executor", FRONTIER_MODEL, lib_models.HOST_CODEX
        ) == FRONTIER_MODEL

    def test_accepts_tier_names_as_well_as_model_literals(self) -> None:
        """model_override admits both spellings, so the clamp must too."""
        assert lib_models.clamp_model_to_role_ceiling(
            "backend-executor", lib_models.TIER_FRONTIER
        ) == DEEP_MODEL


# ---------------------------------------------------------------------------
# Production path enforcement
# ---------------------------------------------------------------------------


class TestResolveModelEnforcesCeiling:
    @pytest.mark.parametrize("role", EXECUTOR_ROLES)
    def test_explicit_policy_override_cannot_lift_an_executor(
        self, role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project pinning an executor to the frontier model is overruled.

        Explicit policy is the highest-priority selection source, so if the
        ceiling holds here it holds for every weaker source.
        """
        router = importlib.import_module("router")
        monkeypatch.setattr(router, "_detect_host", lambda: lib_models.HOST_CLAUDE)

        _write_policy(tmp_path, {"model_overrides": {role: FRONTIER_MODEL}})

        result = router.resolve_model(tmp_path, role, "feature")

        assert result["model"] == DEEP_MODEL, (
            f"{role} was allowed to run at {result['model']!r} via explicit policy"
        )
        assert result["uncapped_model"] == FRONTIER_MODEL
        assert result["source"].endswith("+tier_ceiling")

    def test_explicit_policy_override_is_honoured_for_planning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clamp must not fire for roles that are allowed up there."""
        router = importlib.import_module("router")
        monkeypatch.setattr(router, "_detect_host", lambda: lib_models.HOST_CLAUDE)

        _write_policy(tmp_path, {"model_overrides": {"planning": FRONTIER_MODEL}})

        result = router.resolve_model(tmp_path, "planning", "feature")

        assert result["model"] == FRONTIER_MODEL
        assert "uncapped_model" not in result
        assert not result["source"].endswith("+tier_ceiling")

    def test_exploration_cannot_hand_an_executor_the_frontier_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Epsilon-greedy draws from a ceiling-bounded arm list.

        Forcing epsilon=1.0 makes exploration fire on every call, so a single
        above-ceiling arm in the pool would surface quickly.
        """
        router = importlib.import_module("router")
        monkeypatch.setattr(router, "_detect_host", lambda: lib_models.HOST_CLAUDE)
        monkeypatch.setattr(router, "is_learning_enabled", lambda root: True)

        _write_policy(tmp_path, {"exploration_epsilon": 1.0})

        for _ in range(60):
            result = router.resolve_model(tmp_path, "backend-executor", "feature")
            assert result["model"] != FRONTIER_MODEL, (
                f"exploration selected {FRONTIER_MODEL!r} for an executor: {result!r}"
            )

    def test_exploration_arms_are_bounded_and_deterministic(self) -> None:
        router = importlib.import_module("router")

        executor_arms = router._explorable_models_for_role("backend-executor")
        assert FRONTIER_MODEL not in executor_arms
        assert DEEP_MODEL not in executor_arms, "security floor model is not an arm"
        assert executor_arms == sorted(executor_arms)

        planner_arms = router._explorable_models_for_role("planning")
        assert FRONTIER_MODEL in planner_arms

    def test_default_path_gives_planning_the_frontier_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router = importlib.import_module("router")
        monkeypatch.setattr(router, "_detect_host", lambda: lib_models.HOST_CLAUDE)
        monkeypatch.setattr(router, "is_learning_enabled", lambda root: False)

        result = router.resolve_model(tmp_path, "planning", "feature")
        assert result["model"] == FRONTIER_MODEL


# ---------------------------------------------------------------------------
# Interaction with the security floor
# ---------------------------------------------------------------------------


class TestSecurityFloorIsAFloorNotAPin:
    def test_frontier_is_not_classified_below_the_security_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """security-auditor pinned to frontier must stay there.

        The floor check used to be `model != <deep model>`, which would treat
        the frontier model as below-floor and downgrade it.
        """
        router = importlib.import_module("router")
        monkeypatch.setattr(router, "_detect_host", lambda: lib_models.HOST_CLAUDE)

        _write_policy(
            tmp_path, {"model_overrides": {"security-auditor": FRONTIER_MODEL}}
        )

        result = router.resolve_model(tmp_path, "security-auditor", "feature")
        assert result["model"] == FRONTIER_MODEL

    def test_policy_engine_floor_does_not_downgrade_frontier(self) -> None:
        import policy_engine

        rank = policy_engine._model_rank
        assert rank(FRONTIER_MODEL) >= rank(DEEP_MODEL)


# ---------------------------------------------------------------------------
# Ensemble escalation
# ---------------------------------------------------------------------------


class TestEnsembleEscalationUsesFrontier:
    def test_escalation_model_is_the_frontier_tier(self) -> None:
        router = importlib.import_module("router")
        assert router._DEFAULT_ENSEMBLE_ESCALATION_MODEL == FRONTIER_MODEL

    def test_voting_arms_stay_below_the_escalation_tier(self) -> None:
        """Voting must be cheaper than escalation or the ladder is pointless."""
        router = importlib.import_module("router")
        escalation_rank = lib_models.model_rank(
            router._DEFAULT_ENSEMBLE_ESCALATION_MODEL
        )
        for model in router._DEFAULT_ENSEMBLE_VOTING_MODELS:
            assert lib_models.model_rank(model) < escalation_rank
