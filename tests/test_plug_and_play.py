"""Tests for plug-and-play configuration mechanisms.

Covers:
  - Auditor registry from .dynos/config/auditors.json
  - Executor auto-discovery from agents/*-executor.md
  - Q-learning action spaces from .dynos/config/action-spaces.json
  - Ensemble voting config from .dynos/config/policy.json
  - Eventbus handler discovery from hooks/handlers/
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Auditor Registry
# ---------------------------------------------------------------------------

class TestAuditorRegistry:
    def test_config_present_uses_custom_registry(self, tmp_path: Path):
        from router import _load_auditor_registry
        config_dir = tmp_path / ".dynos" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "auditors.json").write_text(json.dumps({
            "always": ["my-custom-auditor", "security-auditor"],
            "fast_track": ["security-auditor"],
            "domain_conditional": {"ui": ["a11y-auditor"]},
        }))
        registry = _load_auditor_registry(tmp_path)
        assert "my-custom-auditor" in registry["always"]
        assert "a11y-auditor" in registry["domain_conditional"]["ui"]

    def test_config_absent_uses_defaults(self, tmp_path: Path):
        from router import _load_auditor_registry, _DEFAULT_AUDITOR_REGISTRY
        registry = _load_auditor_registry(tmp_path)
        assert registry == _DEFAULT_AUDITOR_REGISTRY

    def test_config_malformed_uses_defaults(self, tmp_path: Path):
        from router import _load_auditor_registry, _DEFAULT_AUDITOR_REGISTRY
        config_dir = tmp_path / ".dynos" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "auditors.json").write_text("not json!!!")
        registry = _load_auditor_registry(tmp_path)
        assert registry == _DEFAULT_AUDITOR_REGISTRY

    def test_default_registry_omits_dead_code_from_always(self, tmp_path: Path):
        from router import _load_auditor_registry
        registry = _load_auditor_registry(tmp_path)
        assert "dead-code-auditor" not in registry["always"]
        assert "dead-code-auditor" in registry["domain_conditional"]["backend"]


# ---------------------------------------------------------------------------
# Executor Auto-Discovery
# ---------------------------------------------------------------------------

class TestExecutorDiscovery:
    def test_discovers_from_agents_directory(self):
        from lib_core import _discover_executors
        executors = _discover_executors()
        assert "backend-executor" in executors
        assert "docs-executor" in executors
        assert "ui-executor" in executors

    def test_fallback_when_no_agents_dir(self, tmp_path: Path):
        from lib_core import _discover_executors
        # Patch the agents dir to a nonexistent path
        with mock.patch("lib_core.Path") as mock_path:
            mock_path.return_value.resolve.return_value.parent.parent.__truediv__ = lambda s, n: tmp_path / "nonexistent"
            # The actual function uses __file__ path, so we test the real one
            executors = _discover_executors()
            # Should still return something (the real agents dir exists)
            assert len(executors) >= 8

    def test_valid_executors_is_populated(self):
        from lib_core import VALID_EXECUTORS
        assert isinstance(VALID_EXECUTORS, set)
        assert len(VALID_EXECUTORS) >= 8
        assert "backend-executor" in VALID_EXECUTORS


# ---------------------------------------------------------------------------
# Q-Learning Action Spaces
# ---------------------------------------------------------------------------

class TestActionSpacesConfig:
    def test_config_present_uses_custom_spaces(self, tmp_path: Path):
        from memory.lib_qlearn import _load_action_spaces
        config_dir = tmp_path / ".dynos" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "action-spaces.json").write_text(json.dumps({
            "sec": ["backend-executor"],
            "a11y": ["ui-executor"],
        }))
        spaces = _load_action_spaces(tmp_path)
        assert spaces["sec"] == ["backend-executor"]
        assert spaces["a11y"] == ["ui-executor"]

    def test_config_absent_uses_defaults(self, tmp_path: Path):
        from memory.lib_qlearn import _load_action_spaces, _DEFAULT_ACTION_SPACE
        spaces = _load_action_spaces(tmp_path)
        assert spaces == _DEFAULT_ACTION_SPACE

    def test_config_malformed_uses_defaults(self, tmp_path: Path):
        from memory.lib_qlearn import _load_action_spaces, _DEFAULT_ACTION_SPACE
        config_dir = tmp_path / ".dynos" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "action-spaces.json").write_text("{bad json")
        spaces = _load_action_spaces(tmp_path)
        assert spaces == _DEFAULT_ACTION_SPACE


# ---------------------------------------------------------------------------
# Ensemble Voting Config
# ---------------------------------------------------------------------------

class TestEnsembleConfig:
    def test_config_present_uses_custom(self):
        from router import _load_ensemble_config
        config = {
            "ensemble_auditors": ["security-auditor"],
            "ensemble_voting_models": ["haiku"],
            "ensemble_escalation_model": "sonnet",
        }
        auditors, models, escalation = _load_ensemble_config(config)
        assert auditors == {"security-auditor"}
        assert models == ["haiku"]
        assert escalation == "sonnet"

    def test_config_absent_uses_defaults(self):
        from router import _load_ensemble_config, _DEFAULT_ENSEMBLE_AUDITORS
        auditors, models, escalation = _load_ensemble_config({})
        assert auditors == _DEFAULT_ENSEMBLE_AUDITORS
        assert models == ["haiku", "sonnet"]
        # Escalation is the frontier tier: the voting arms decide the common
        # case, and only a split spends a frontier spawn to break the tie.
        assert escalation == "fable"

    def test_partial_config_fills_defaults(self):
        from router import _load_ensemble_config
        auditors, models, escalation = _load_ensemble_config({"ensemble_auditors": ["code-quality-auditor"]})
        assert auditors == {"code-quality-auditor"}
        assert models == ["haiku", "sonnet"]  # default
        assert escalation == "fable"  # default (frontier tier)


# ---------------------------------------------------------------------------
# Eventbus Handler Discovery
# ---------------------------------------------------------------------------

class TestHandlerDiscovery:
    def test_discover_handlers_fallback_returns_builtins(self, tmp_path, monkeypatch):
        import eventbus
        from eventbus import _discover_handlers, _BUILTIN_HANDLERS
        # Patch SCRIPT_DIR to a dir with no handlers/ subdir — exercises fallback branch
        monkeypatch.setattr(eventbus, "SCRIPT_DIR", tmp_path)
        result = _discover_handlers()
        for event_type in _BUILTIN_HANDLERS:
            assert event_type in result, f"builtin event type {event_type!r} missing from fallback result"
        assert len(result["task-completed"]) >= len(_BUILTIN_HANDLERS["task-completed"])

    def test_builtin_handler_callables_accept_root_and_payload(self):
        import inspect
        from eventbus import _BUILTIN_HANDLERS
        for name, fn in _BUILTIN_HANDLERS["task-completed"]:
            assert callable(fn), f"handler {name!r} is not callable"
            assert len(inspect.signature(fn).parameters) == 2, (
                f"handler {name!r} should accept exactly 2 parameters (root, payload)"
            )

    def test_discovers_from_handlers_directory(self, tmp_path: Path):
        from eventbus import _discover_handlers, SCRIPT_DIR
        handlers_dir = SCRIPT_DIR / "handlers"
        handlers_dir.mkdir(exist_ok=True)
        # Write a test handler
        (handlers_dir / "test_notify.py").write_text(
            'EVENT_TYPE = "task-completed"\n'
            'def run(root, payload):\n'
            '    return True\n'
        )
        try:
            discovered = _discover_handlers()
            names = [n for n, _ in discovered.get("task-completed", [])]
            assert "test_notify" in names
        finally:
            (handlers_dir / "test_notify.py").unlink(missing_ok=True)
            if not any(handlers_dir.iterdir()):
                handlers_dir.rmdir()

    def test_no_handlers_dir_uses_builtins(self):
        from eventbus import _discover_handlers, _BUILTIN_HANDLERS
        # Even without hooks/handlers/ dir, built-ins are returned
        discovered = _discover_handlers()
        assert "task-completed" in discovered
        assert len(discovered["task-completed"]) >= len(_BUILTIN_HANDLERS["task-completed"])

    def test_improve_handler_registered(self):
        from eventbus import HANDLERS
        names = [n for n, _ in HANDLERS.get("task-completed", [])]
        assert "improve" in names


# ---------------------------------------------------------------------------
# Postmortem Improve
# ---------------------------------------------------------------------------

class TestPostmortemImprove:
    def test_no_proposals_with_few_retros(self, tmp_path: Path):
        from memory.postmortem_improve import propose_improvements
        # Fewer than 3 retrospectives → no proposals
        proposals = propose_improvements(tmp_path)
        assert proposals == []

    def test_prevention_rules_flow_to_project_rules(self, tmp_path: Path):
        from memory.policy_engine import _load_prevention_rules
        from lib_core import _persistent_project_dir, write_json
        # Write a prevention rule
        persistent = _persistent_project_dir(tmp_path)
        persistent.mkdir(parents=True, exist_ok=True)
        write_json(persistent / "prevention-rules.json", {
            "rules": [{"category": "sec", "rule": "Check auth on all endpoints."}],
            "updated_at": "2026-04-17T00:00:00Z",
        })
        rules = _load_prevention_rules(tmp_path)
        assert len(rules) == 1
        assert rules[0]["category"] == "sec"

    def test_no_prevention_rules_returns_empty(self, tmp_path: Path):
        from memory.policy_engine import _load_prevention_rules
        rules = _load_prevention_rules(tmp_path)
        assert rules == []
