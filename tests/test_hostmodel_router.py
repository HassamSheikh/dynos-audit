"""Tests for router.py host-aware changes (AC-2, AC-11, AC-12, AC-13, AC-14).

Covers:
  AC-2  — Claude byte-identical invariant (_default_model_for_role with host=claude)
  AC-11 — floor_unmet: true recorded when floor unsatisfiable under Codex
  AC-12 — Security floor resolves to "opus" under Claude (no regression)
  AC-13 — Ensemble disabled under null mapping; reason field present
  AC-14 — Retry escalation records escalation_unavailable: true under Codex
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import lib_models  # noqa: E402  (production module — RED phase)


# ---------------------------------------------------------------------------
# AC-2: Claude byte-identical invariant
# ---------------------------------------------------------------------------

# The full table from AC-2 spec.
_CLAUDE_EXPECTED_MODELS = [
    # Promoted off the original AC-2 table to the frontier tier — see
    # lib_models.ROLE_DEFAULT_TIERS. The byte-identical invariant below still
    # holds for every role that has not been deliberately re-tiered.
    ("planning", "fable"),
    ("spec-writer", "sonnet"),
    ("backend-executor", "sonnet"),
    ("ui-executor", "sonnet"),
    ("db-executor", "sonnet"),
    ("integration-executor", "sonnet"),
    ("refactor-executor", "sonnet"),
    ("ml-executor", "sonnet"),
    ("testing-executor", "sonnet"),
    ("docs-executor", "haiku"),
    ("spec-completion-auditor", "sonnet"),
    ("code-quality-auditor", "sonnet"),
    ("dead-code-auditor", "haiku"),
    ("performance-auditor", "haiku"),
    ("db-schema-auditor", "haiku"),
    ("ui-auditor", "haiku"),
    ("claude-md-auditor", "sonnet"),
    ("security-auditor", "opus"),
]


def _default_model_for_role_via_lib_models(role: str, host: str) -> str | None:
    """Derive the expected model from ROLE_DEFAULT_TIERS + resolve_model_for_tier.

    This is what the post-task router._default_model_for_role(role, host) must
    implement: look up the tier from ROLE_DEFAULT_TIERS (defaulting to TIER_BALANCED
    for unknown roles), then resolve via TIER_TO_MODEL.
    """
    tier = lib_models.ROLE_DEFAULT_TIERS.get(role, lib_models.TIER_BALANCED)
    return lib_models.resolve_model_for_tier(host, tier)


class TestRouterClaudeModelsUnchanged:
    @pytest.mark.parametrize("role,expected_model", _CLAUDE_EXPECTED_MODELS)
    def test_router_claude_models_unchanged(
        self, role: str, expected_model: str
    ) -> None:
        """AC-2: _default_model_for_role(role, 'claude') is byte-identical to pre-task."""
        result = _default_model_for_role_via_lib_models(role, "claude")
        assert result == expected_model, (
            f"Role {role!r}: expected model {expected_model!r}, "
            f"got {result!r} via lib_models"
        )

    def test_all_claude_roles_produce_non_none_model(self) -> None:
        """AC-2: no Claude role should resolve to None (fail-closed regression check)."""
        for role, _ in _CLAUDE_EXPECTED_MODELS:
            result = _default_model_for_role_via_lib_models(role, "claude")
            assert result is not None, (
                f"Role {role!r} resolved to None under claude host"
            )

    def test_security_auditor_resolves_to_opus_under_claude(self) -> None:
        """AC-2/AC-12: security-auditor must resolve to opus under claude host."""
        result = _default_model_for_role_via_lib_models("security-auditor", "claude")
        assert result == "opus"

    def test_docs_executor_resolves_to_haiku_under_claude(self) -> None:
        """AC-2: docs-executor must resolve to haiku under claude host (TIER_FAST)."""
        result = _default_model_for_role_via_lib_models("docs-executor", "claude")
        assert result == "haiku"


# ---------------------------------------------------------------------------
# AC-11 / AC-12: Floor-unmet under Codex; security floor under Claude
# ---------------------------------------------------------------------------

class TestRouterSecurityFloorCodexFloorUnmet:
    def test_router_security_floor_codex_floor_unmet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-11: security-auditor under codex has model=None and floor_unmet=True
        in the dict returned by the PRODUCTION routing path.

        Drives the real router.resolve_model entry point with host=codex
        (monkeypatched via router._detect_host) and asserts that the returned
        routing decision dict carries floor_unmet=True and model=None without
        raising.

        This is NOT a tautology: it exercises the wired production code path in
        router.py that detects the unsatisfiable floor and stamps floor_unmet.
        """
        import importlib
        router = importlib.import_module("router")

        # Monkeypatch the host detector that build_audit_plan / resolve_model use.
        monkeypatch.setattr(router, "_detect_host", lambda: "codex")

        # tmp_path acts as the project root. No policy files needed — the
        # floor_unmet early-exit fires before any file reads.
        result = router.resolve_model(
            tmp_path,
            "security-auditor",
            "feature",
            host="codex",
        )

        assert result.get("model") is None, (
            f"model must be None when floor unmet under codex, got {result.get('model')!r}"
        )
        assert result.get("floor_unmet") is True, (
            f"floor_unmet must be True under codex, got {result!r}"
        )
        assert result.get("source") == "security_floor", (
            f"source must be 'security_floor', got {result.get('source')!r}"
        )

    def test_router_security_floor_codex_floor_unmet_in_audit_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-11: floor_unmet=True is carried into the audit-plan entry for
        security-auditor when the active host is codex.

        Drives build_audit_plan with _detect_host monkeypatched to 'codex'
        and verifies the security-auditor entry in the resulting plan carries
        floor_unmet=True.
        """
        import importlib
        router = importlib.import_module("router")

        # Monkeypatch the host detector used inside build_audit_plan.
        monkeypatch.setattr(router, "_detect_host", lambda: "codex")

        plan = router.build_audit_plan(
            tmp_path,
            "feature",
            [],
        )

        security_entries = [
            a for a in plan.get("auditors", [])
            if a.get("name") == "security-auditor" and a.get("action") == "spawn"
        ]
        assert security_entries, (
            f"security-auditor must appear as a spawn entry in the audit plan, "
            f"got auditors={[a.get('name') for a in plan.get('auditors', [])]}"
        )
        entry = security_entries[0]
        assert entry.get("floor_unmet") is True, (
            f"audit-plan entry for security-auditor must carry floor_unmet=True under codex, "
            f"got {entry!r}"
        )
        assert entry.get("model") is None, (
            f"audit-plan entry model must be None when floor unmet, got {entry.get('model')!r}"
        )

    def test_router_security_floor_claude_no_floor_unmet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-11/Claude byte-identity: under host=claude the floor is satisfied;
        floor_unmet must be absent (or False) in the routing decision.
        """
        import importlib
        router = importlib.import_module("router")

        monkeypatch.setattr(router, "_detect_host", lambda: "claude")

        result = router.resolve_model(
            tmp_path,
            "security-auditor",
            "feature",
            host="claude",
        )

        assert not result.get("floor_unmet"), (
            f"floor_unmet must be absent/False under claude (floor satisfied), "
            f"got {result!r}"
        )
        assert result.get("model") == "opus", (
            f"security-auditor under claude must resolve to 'opus', got {result.get('model')!r}"
        )


class TestRouterSecurityFloorClaude:
    def test_router_security_floor_claude(self) -> None:
        """AC-12: security-auditor under claude resolves to 'opus'."""
        tier = lib_models.ROLE_DEFAULT_TIERS.get("security-auditor", lib_models.TIER_DEEP)
        resolved = lib_models.resolve_model_for_tier("claude", tier)
        assert resolved == "opus", (
            f"security-auditor under claude must resolve to 'opus', got {resolved!r}"
        )


# ---------------------------------------------------------------------------
# AC-13: Ensemble disabled under null mapping
# ---------------------------------------------------------------------------

class TestEnsembleDisabledUnderNullMapping:
    def test_ensemble_disabled_under_null_mapping(self) -> None:
        """AC-13: when all arms resolve to None (codex host), ensemble must be False.

        The spec requires _build_ensemble_context to return
        {'ensemble': False, 'reason': 'host_null_mapping'} when all arms null.

        This test verifies the lib_models precondition: all codex tiers → None.
        The router implementation must produce that exact dict structure.
        """
        # Verify the precondition: all arms resolve to None under codex.
        null_arms = all(
            lib_models.resolve_model_for_tier("codex", tier) is None
            for tier in lib_models.ALL_TIERS
        )
        assert null_arms, "All codex tiers must resolve to None for ensemble to be disabled"

        # Import router and call _build_ensemble_context with host="codex"
        import importlib
        router = importlib.import_module("router")
        result = router._build_ensemble_context("codex")  # type: ignore[attr-defined]

        assert result.get("ensemble") is False, (
            f"ensemble must be False under codex null mapping, got {result!r}"
        )
        assert result.get("reason") == "host_null_mapping", (
            f"reason must be 'host_null_mapping', got {result.get('reason')!r}"
        )

    def test_ensemble_disabled_null_mapping_exact_fields(self) -> None:
        """AC-13: the returned dict must contain exactly ensemble=False, reason='host_null_mapping'."""
        import importlib
        router = importlib.import_module("router")
        result = router._build_ensemble_context("codex")  # type: ignore[attr-defined]

        assert "ensemble" in result
        assert "reason" in result
        assert result["ensemble"] is False
        assert result["reason"] == "host_null_mapping"

    def test_ensemble_disabled_under_null_mapping_in_audit_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-13: ensemble=False for all auditors via PRODUCTION path under codex.

        This test drives build_audit_plan with _detect_host monkeypatched to 'codex'
        and verifies EVERY auditor entry in the resulting plan has ensemble=False,
        proving the production path (_build_ensemble_context wired into
        build_audit_plan) works correctly under null mapping.
        """
        import importlib
        router = importlib.import_module("router")

        # Monkeypatch the host detector used inside build_audit_plan.
        monkeypatch.setattr(router, "_detect_host", lambda: "codex")

        plan = router.build_audit_plan(
            tmp_path,
            "feature",
            ["backend"],
            fast_track=False,
            risk_level="high",  # Use high risk to trigger ensemble in normal flow
        )

        # Verify all spawn auditor entries have ensemble=False when host is codex.
        # Under normal conditions (high risk), ensemble would be True, but the
        # host_null_mapping check overrides it to False.
        spawn_entries = [
            a for a in plan.get("auditors", [])
            if a.get("action") == "spawn"
        ]
        assert spawn_entries, (
            f"audit plan must contain at least one spawn entry under high risk, "
            f"got auditors={[a.get('name') for a in plan.get('auditors', [])]}"
        )

        for entry in spawn_entries:
            assert entry.get("ensemble") is False, (
                f"auditor {entry.get('name')!r}: ensemble must be False under codex "
                f"null mapping, got ensemble={entry.get('ensemble')!r}"
            )
            # Verify no ensemble_voting_models are present when ensemble is False
            assert "ensemble_voting_models" not in entry, (
                f"auditor {entry.get('name')!r}: ensemble_voting_models must not be "
                f"present when ensemble=False, got {entry!r}"
            )
            assert "ensemble_escalation_model" not in entry, (
                f"auditor {entry.get('name')!r}: ensemble_escalation_model must not be "
                f"present when ensemble=False, got {entry!r}"
            )


# ---------------------------------------------------------------------------
# AC-14: Retry escalation records escalation_unavailable: true under Codex
# ---------------------------------------------------------------------------

class TestRetryEscalationUnavailableRecorded:
    def test_retry_escalation_unavailable_recorded(self) -> None:
        """AC-14: escalation path under codex records escalation_unavailable=True
        and does NOT emit model_override.
        """
        # Under codex, resolve_model_for_tier for TIER_DEEP returns None.
        # This is the condition that triggers escalation_unavailable.
        deep_model = lib_models.resolve_model_for_tier("codex", lib_models.TIER_DEEP)
        assert deep_model is None, "codex TIER_DEEP must be None to trigger escalation_unavailable"

        # Import router and call the escalation path.
        import importlib
        router = importlib.import_module("router")

        # The router's escalation function (_escalate_model or equivalent)
        # must return a dict with escalation_unavailable=True and no model_override.
        result = router._build_escalation_result("codex")  # type: ignore[attr-defined]

        assert result.get("escalation_unavailable") is True, (
            f"escalation_unavailable must be True under codex, got {result!r}"
        )
        assert "model_override" not in result, (
            f"model_override must be absent when escalation_unavailable, got key in {result!r}"
        )
