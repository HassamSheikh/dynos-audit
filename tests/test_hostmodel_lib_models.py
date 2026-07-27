"""Tests for hooks/lib_models.py (AC-3, AC-4, AC-28 partial).

Covers:
  AC-3  — lib_models exported API shape (tier constants, mappings, functions)
  AC-4  — ROLE_DEFAULT_TIERS completeness
  AC-28 — lib_models.py is a leaf module (no forbidden imports)
"""
from __future__ import annotations

import ast
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

import lib_models  # noqa: E402  (production module to be created — RED phase)


# ---------------------------------------------------------------------------
# AC-3: tier constants
# ---------------------------------------------------------------------------

class TestLibModelsTierConstants:
    def test_lib_models_tier_constants(self) -> None:
        """AC-3: ALL_TIERS is the ordered tier ladder, as a list."""
        assert lib_models.TIER_FAST == "fast"
        assert lib_models.TIER_BALANCED == "balanced"
        assert lib_models.TIER_DEEP == "deep"
        assert lib_models.TIER_FRONTIER == "frontier"
        assert lib_models.ALL_TIERS == ["fast", "balanced", "deep", "frontier"]
        assert isinstance(lib_models.ALL_TIERS, list), "ALL_TIERS must be a list, not a set"

    def test_all_tiers_ordering(self) -> None:
        """AC-3: ALL_TIERS must be ordered fast→balanced→deep→frontier."""
        tiers = lib_models.ALL_TIERS
        assert tiers[0] == "fast"
        assert tiers[1] == "balanced"
        assert tiers[2] == "deep"
        assert tiers[3] == "frontier"
        assert len(tiers) == 4

    def test_tier_rank_matches_all_tiers_order(self) -> None:
        """TIER_RANK must agree with ALL_TIERS ordering, with no gaps.

        Every rank comparison in the codebase (security floor, ceiling
        clamp, circuit-breaker predicate) reads TIER_RANK, so a rank that
        disagrees with the declared ladder order would invert those checks
        silently.
        """
        assert set(lib_models.TIER_RANK) == set(lib_models.ALL_TIERS)
        ranks = [lib_models.TIER_RANK[t] for t in lib_models.ALL_TIERS]
        assert ranks == sorted(ranks), "TIER_RANK must ascend in ALL_TIERS order"
        assert ranks == list(range(len(lib_models.ALL_TIERS))), "ranks must be 0..n-1"

    def test_host_constants(self) -> None:
        """AC-3: HOST_CLAUDE, HOST_CODEX, ALL_HOSTS must have correct values."""
        assert lib_models.HOST_CLAUDE == "claude"
        assert lib_models.HOST_CODEX == "codex"
        assert isinstance(lib_models.ALL_HOSTS, frozenset)
        assert lib_models.ALL_HOSTS == frozenset({"claude", "codex"})


# ---------------------------------------------------------------------------
# AC-3: Claude host mapping
# ---------------------------------------------------------------------------

class TestLibModelsClaudeMapping:
    def test_lib_models_claude_mapping(self) -> None:
        """AC-3: resolve_model_for_tier returns correct vendor literals for claude."""
        assert lib_models.resolve_model_for_tier("claude", "fast") == "haiku"
        assert lib_models.resolve_model_for_tier("claude", "balanced") == "sonnet"
        assert lib_models.resolve_model_for_tier("claude", "deep") == "opus"

    def test_tier_to_model_claude_dict(self) -> None:
        """AC-3: TIER_TO_MODEL["claude"] maps fast→haiku, balanced→sonnet, deep→opus."""
        mapping = lib_models.TIER_TO_MODEL
        assert mapping["claude"]["fast"] == "haiku"
        assert mapping["claude"]["balanced"] == "sonnet"
        assert mapping["claude"]["deep"] == "opus"


# ---------------------------------------------------------------------------
# AC-3: Codex host mapping (all null)
# ---------------------------------------------------------------------------

class TestLibModelsCodexMappingNull:
    def test_lib_models_codex_mapping_null(self) -> None:
        """AC-3: resolve_model_for_tier returns None for all tiers under codex."""
        assert lib_models.resolve_model_for_tier("codex", "fast") is None
        assert lib_models.resolve_model_for_tier("codex", "balanced") is None
        assert lib_models.resolve_model_for_tier("codex", "deep") is None

    def test_tier_to_model_codex_dict_all_none(self) -> None:
        """AC-3: TIER_TO_MODEL['codex'] must map all tiers to None."""
        mapping = lib_models.TIER_TO_MODEL
        for tier in lib_models.ALL_TIERS:
            assert mapping["codex"][tier] is None, (
                f"TIER_TO_MODEL['codex']['{tier}'] must be None, got {mapping['codex'][tier]!r}"
            )


# ---------------------------------------------------------------------------
# AC-3: model_to_tier reverse mapping
# ---------------------------------------------------------------------------

class TestLibModelsModelToTierRoundtrip:
    def test_lib_models_model_to_tier_roundtrip(self) -> None:
        """AC-3: model_to_tier is a clean reverse map for Claude literals."""
        assert lib_models.model_to_tier("haiku") == "fast"
        assert lib_models.model_to_tier("sonnet") == "balanced"
        assert lib_models.model_to_tier("opus") == "deep"

    def test_model_to_tier_roundtrip_all_tiers(self) -> None:
        """AC-3: model_to_tier(resolve_model_for_tier('claude', t)) == t for all t."""
        for tier in lib_models.ALL_TIERS:
            model = lib_models.resolve_model_for_tier("claude", tier)
            assert model is not None
            assert lib_models.model_to_tier(model) == tier, (
                f"Roundtrip failed for tier={tier!r}: "
                f"resolve→{model!r}, back→{lib_models.model_to_tier(model)!r}"
            )

    def test_model_to_tier_unknown_returns_none(self) -> None:
        """AC-3: model_to_tier returns None for unknown model strings."""
        assert lib_models.model_to_tier("gpt4") is None
        assert lib_models.model_to_tier("unknown-model") is None
        assert lib_models.model_to_tier("") is None


# ---------------------------------------------------------------------------
# AC-3: valid_models_for_host
# ---------------------------------------------------------------------------

class TestLibModelsValidModelsForHost:
    def test_lib_models_valid_models_for_host(self) -> None:
        """AC-3: valid_models_for_host returns correct frozensets."""
        claude_valid = lib_models.valid_models_for_host("claude")
        assert claude_valid == frozenset({"haiku", "sonnet", "opus", "fable"})
        assert isinstance(claude_valid, frozenset)

        codex_valid = lib_models.valid_models_for_host("codex")
        assert codex_valid == frozenset()
        assert isinstance(codex_valid, frozenset)

    def test_valid_models_for_host_unknown_host(self) -> None:
        """AC-3: valid_models_for_host with unknown host returns empty frozenset (fail-closed)."""
        result = lib_models.valid_models_for_host("unknown-host")
        # fail-closed: unknown host has no valid models
        assert isinstance(result, frozenset), (
            f"valid_models_for_host must return a frozenset, got {type(result)}"
        )
        assert "opus" not in result, (
            "fail-closed: unknown host must not include 'opus' in valid models"
        )


# ---------------------------------------------------------------------------
# AC-4: ROLE_DEFAULT_TIERS completeness
# ---------------------------------------------------------------------------

# Runtime roles and their expected default tiers. Keep this aligned with
# agents/*-{executor,auditor}.md plus the planner/spec writer roles.
_EXPECTED_ROLE_TIERS = {
    # Deliberately promoted past its original balanced tier: plan quality
    # determines the cost of every downstream executor spawn.
    "planning": "frontier",
    "spec-writer": "balanced",
    "backend-executor": "balanced",
    "ui-executor": "balanced",
    "db-executor": "balanced",
    "integration-executor": "balanced",
    "refactor-executor": "balanced",
    "ml-executor": "balanced",
    "testing-executor": "balanced",
    "docs-executor": "fast",
    "infra-executor": "balanced",
    "security-executor": "deep",
    "data-executor": "balanced",
    "observability-executor": "balanced",
    "release-executor": "balanced",
    "spec-completion-auditor": "balanced",
    "code-quality-auditor": "balanced",
    "dead-code-auditor": "fast",
    "performance-auditor": "fast",
    "db-schema-auditor": "fast",
    "ui-auditor": "fast",
    "claude-md-auditor": "balanced",
    "security-auditor": "deep",
    "architecture-auditor": "balanced",
    "threat-model-auditor": "deep",
    "api-contract-auditor": "balanced",
    "test-strategy-auditor": "balanced",
    "accessibility-auditor": "balanced",
    "privacy-auditor": "balanced",
    "supply-chain-auditor": "deep",
    "infrastructure-auditor": "balanced",
    "observability-auditor": "balanced",
    "release-auditor": "balanced",
    "data-integrity-auditor": "balanced",
    "docs-accuracy-auditor": "balanced",
}

# Original 17 roles from router.py:174-192 ROLE_DEFAULT_MODELS
_ORIGINAL_ROLE_DEFAULT_MODELS = {
    "planning": "sonnet",
    "spec-writer": "sonnet",
    "backend-executor": "sonnet",
    "ui-executor": "sonnet",
    "db-executor": "sonnet",
    "integration-executor": "sonnet",
    "refactor-executor": "sonnet",
    "ml-executor": "sonnet",
    "testing-executor": "sonnet",
    "docs-executor": "haiku",
    "spec-completion-auditor": "sonnet",
    "code-quality-auditor": "sonnet",
    "dead-code-auditor": "haiku",
    "performance-auditor": "haiku",
    "db-schema-auditor": "haiku",
    "ui-auditor": "haiku",
    "claude-md-auditor": "sonnet",
}


class TestRoleDefaultTiersComplete:
    def test_role_default_tiers_complete(self) -> None:
        """AC-4: ROLE_DEFAULT_TIERS contains all runtime roles."""
        rdt = lib_models.ROLE_DEFAULT_TIERS

        # Every key from the original 17-role ROLE_DEFAULT_MODELS must be present.
        for role in _ORIGINAL_ROLE_DEFAULT_MODELS:
            assert role in rdt, f"Role {role!r} missing from ROLE_DEFAULT_TIERS"

        # security-auditor is present and maps to TIER_DEEP.
        assert "security-auditor" in rdt, "security-auditor missing from ROLE_DEFAULT_TIERS"
        assert rdt["security-auditor"] == lib_models.TIER_DEEP, (
            f"security-auditor must map to TIER_DEEP, got {rdt['security-auditor']!r}"
        )

        # Roles that mapped to "haiku" in ROLE_DEFAULT_MODELS must now be TIER_FAST.
        haiku_roles = {r for r, m in _ORIGINAL_ROLE_DEFAULT_MODELS.items() if m == "haiku"}
        for role in haiku_roles:
            assert rdt[role] == lib_models.TIER_FAST, (
                f"Role {role!r} mapped to haiku should map to TIER_FAST, got {rdt[role]!r}"
            )

        # Roles that mapped to "sonnet" in ROLE_DEFAULT_MODELS must now be
        # TIER_BALANCED — except roles deliberately re-tiered since the
        # original migration, which _EXPECTED_ROLE_TIERS pins explicitly.
        _RETIERED_SINCE_MIGRATION = {"planning"}
        sonnet_roles = {
            r for r, m in _ORIGINAL_ROLE_DEFAULT_MODELS.items()
            if m == "sonnet" and r not in _RETIERED_SINCE_MIGRATION
        }
        for role in sonnet_roles:
            assert rdt[role] == lib_models.TIER_BALANCED, (
                f"Role {role!r} mapped to sonnet should map to TIER_BALANCED, got {rdt[role]!r}"
            )

        # No key in ROLE_DEFAULT_TIERS should be absent from the known role set.
        expected_keys = set(_EXPECTED_ROLE_TIERS.keys())
        for role in rdt:
            assert role in expected_keys, (
                f"Unexpected role {role!r} in ROLE_DEFAULT_TIERS; "
                f"not in the expected runtime role set"
            )

    def test_role_default_tiers_exact_tier_assignments(self) -> None:
        """AC-4: each role maps to exactly the expected tier."""
        rdt = lib_models.ROLE_DEFAULT_TIERS
        for role, expected_tier in _EXPECTED_ROLE_TIERS.items():
            assert rdt.get(role) == expected_tier, (
                f"Role {role!r}: expected tier {expected_tier!r}, got {rdt.get(role)!r}"
            )

    def test_role_default_tiers_total_count(self) -> None:
        """AC-4: ROLE_DEFAULT_TIERS must cover the known runtime role set exactly."""
        assert len(lib_models.ROLE_DEFAULT_TIERS) == len(_EXPECTED_ROLE_TIERS), (
            f"Expected {len(_EXPECTED_ROLE_TIERS)} roles, got {len(lib_models.ROLE_DEFAULT_TIERS)}"
        )


# ---------------------------------------------------------------------------
# AC-28: lib_models.py is a leaf module
# ---------------------------------------------------------------------------

class TestLibModelsLeafModule:
    def test_lib_models_no_forbidden_imports(self) -> None:
        """AC-28: lib_models.py must not import from any dynos-work hooks/ or memory/ module."""
        lib_models_path = HOOKS_DIR / "lib_models.py"
        src = lib_models_path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        _FORBIDDEN = {
            "router", "lib_defaults", "lib_tokens", "lib_validate",
            "lib_tool_budget", "circuit_breaker", "receipts", "ctl",
            "lib_qlearn", "policy_engine", "postmortem", "lib_migrate",
            "lib_log", "lib_receipts", "lib_core", "lib_tokens_hook",
        }

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in getattr(node, "names", [])]
                combined = mod + " " + " ".join(names)
                for bad in _FORBIDDEN:
                    if bad in combined:
                        violations.append(
                            f"lib_models.py: forbidden import: {combined.strip()!r}"
                        )

        assert not violations, "\n".join(violations)
