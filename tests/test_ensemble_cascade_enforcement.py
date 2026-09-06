"""Deterministic ensemble cascade enforcement.

Covers the four enforcement points that replace the prose-only cascade:

- ``lib_ensemble.evaluate_cascade`` / ``ensemble_next`` (single source of truth)
- the DONE gate (``require_receipts_for_done``) refusing escalation-only and
  balanced-tier-skipped cascades
- ``receipt_audit_done(ensemble_context=True)`` refusing out-of-order shards
- ``router.py audit-inject-prompt`` refusing off-cascade spawn preparation
- ``ctl ensemble-next`` and ``ctl run-audit-summary`` (subprocess)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_ensemble  # noqa: E402
from lib_core import require_receipts_for_done  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_postmortem_skipped,
    write_receipt,
)
from receipts.stage import receipt_audit_done  # noqa: E402

FAST = "haiku"      # noqa: model-literal
BALANCED = "sonnet"  # noqa: model-literal
DEEP = "opus"       # noqa: model-literal
AUDITOR = "sec"


def _entry(name: str = AUDITOR, *, ensemble: bool = True, model: str = DEEP,
           escalation: str = DEEP) -> dict:
    entry = {
        "name": name,
        "action": "spawn",
        "model": model,
        "ensemble": ensemble,
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }
    if ensemble:
        entry["ensemble_voting_models"] = [FAST, BALANCED]
        entry["ensemble_escalation_model"] = escalation
    return entry


def _setup_task(tmp_path: Path, entries: list[dict] | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260906-EN"
    td.mkdir(parents=True)
    (td / "receipts").mkdir()
    (td / "audit-reports").mkdir()
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.95}))
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    entries = entries if entries is not None else [_entry()]
    (td / "audit-plan.json").write_text(json.dumps({"auditors": entries}))
    receipt_audit_routing(td, entries)
    return td


def _mock_empty_registry(monkeypatch):
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def _shard(td: Path, model: str, *, findings: int = 0, name: str = AUDITOR,
           ts: str | None = None) -> Path:
    path = write_receipt(
        td,
        f"audit-{name}-{model}",
        auditor_name=name,
        model_used=model,
        finding_count=findings,
        blocking_count=findings,
        report_path=None,
        report_sha256=None,
        tokens_used=100,
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
    )
    if ts is not None:
        data = json.loads(path.read_text())
        data["ts"] = ts
        path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# lib_ensemble: pure evaluation
# ---------------------------------------------------------------------------

def _receipts(**by_model) -> dict:
    out = {FAST: None, BALANCED: None, DEEP: None}
    for model, findings in by_model.items():
        out[model] = None if findings is None else {"model_used": model, "finding_count": findings}
    return out


def test_evaluate_no_receipts_spawns_fast_tier():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts())
    assert r["status"] == "spawn" and r["model"] == FAST
    assert r["reason"] == "cascade_step" and r["shard_step_name"] == f"{AUDITOR}-{FAST}"
    assert r["gaps"], "an unstarted cascade is a DONE-gate gap"


def test_evaluate_fast_clean_spawns_balanced_tier():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 0}))
    assert r["status"] == "spawn" and r["model"] == BALANCED and r["tier_index"] == 1


def test_evaluate_both_voting_tiers_clean_is_complete_pass():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 0, BALANCED: 0}))
    assert r["status"] == "complete" and r["verdict"] == "pass" and r["gaps"] == []


def test_evaluate_fast_findings_spawns_escalation_and_skips_balanced():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 2}))
    assert r["status"] == "spawn" and r["model"] == DEEP
    assert r["reason"] == "escalate_on_findings" and r["triggered_by"] == FAST


def test_evaluate_fast_findings_with_escalation_is_complete_escalated():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 2, DEEP: 1}))
    assert r["status"] == "complete" and r["verdict"] == "escalated" and r["gaps"] == []


def test_evaluate_balanced_findings_requires_escalation():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 0, BALANCED: 1}))
    assert r["status"] == "spawn" and r["model"] == DEEP and r["triggered_by"] == BALANCED


def test_evaluate_escalation_only_is_a_gap_and_still_requires_fast_tier():
    """The hole this change closes: one deep-tier spawn is not an ensemble."""
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{DEEP: 0}))
    assert r["status"] == "spawn" and r["model"] == FAST
    assert any("escalation receipt" in g and FAST in g for g in r["gaps"]), r["gaps"]


def test_evaluate_balanced_skipped_after_clean_fast_is_a_gap():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), _receipts(**{FAST: 0, DEEP: 0}))
    assert r["status"] == "spawn" and r["model"] == BALANCED
    assert any(BALANCED in g for g in r["gaps"]), r["gaps"]


def test_evaluate_model_used_outside_cascade_is_a_gap():
    receipts = _receipts(**{FAST: 0, BALANCED: 0})
    receipts[BALANCED]["model_used"] = "other-model"
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(), receipts)
    assert any("not in voting set" in g for g in r["gaps"]), r["gaps"]


def test_evaluate_non_ensemble_entry_is_single_with_plan_model():
    r = lib_ensemble.evaluate_cascade(AUDITOR, _entry(ensemble=False, model=BALANCED), {})
    assert r["status"] == "single" and r["model"] == BALANCED and r["gaps"] == []


def test_evaluate_empty_voting_models_is_invalid():
    entry = _entry()
    entry["ensemble_voting_models"] = []
    r = lib_ensemble.evaluate_cascade(AUDITOR, entry, {})
    assert r["status"] == "invalid" and r["gaps"]


# ---------------------------------------------------------------------------
# lib_ensemble: receipts on disk, staleness
# ---------------------------------------------------------------------------

def test_ensemble_next_ignores_later_tier_shards_older_than_fast_tier(tmp_path):
    """A re-audit restarts the cascade: stale balanced/deep shards do not count."""
    td = _setup_task(tmp_path)
    _shard(td, BALANCED, findings=0, ts="2026-09-06T10:00:00Z")
    _shard(td, DEEP, findings=0, ts="2026-09-06T10:05:00Z")
    _shard(td, FAST, findings=0, ts="2026-09-06T11:00:00Z")
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "spawn" and r["model"] == BALANCED, r


def test_ensemble_next_keeps_later_tier_shards_newer_than_fast_tier(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-09-06T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-09-06T10:05:00Z")
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "complete" and r["verdict"] == "pass"


def test_ensemble_next_unknown_auditor_is_invalid(tmp_path):
    td = _setup_task(tmp_path)
    r = lib_ensemble.ensemble_next(td, "nope")
    assert r["status"] == "invalid" and not r["ensemble"]


def test_ensemble_next_all_reports_pending_and_complete(tmp_path):
    td = _setup_task(tmp_path, [_entry(), _entry("cq", ensemble=False, model=BALANCED)])
    r = lib_ensemble.ensemble_next_all(td)
    assert r["pending"] == [AUDITOR] and r["complete"] is False
    by_name = {a["auditor"]: a for a in r["auditors"]}
    assert by_name["cq"]["status"] == "single" and by_name["cq"]["model"] == BALANCED
    _shard(td, FAST, findings=0)
    _shard(td, BALANCED, findings=0)
    r = lib_ensemble.ensemble_next_all(td)
    assert r["pending"] == [] and r["complete"] is True


# ---------------------------------------------------------------------------
# DONE gate
# ---------------------------------------------------------------------------

def test_gate_refuses_escalation_only_receipt(tmp_path, monkeypatch):
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _shard(td, DEEP, findings=0)
    gaps = require_receipts_for_done(td)
    assert any(AUDITOR in g and FAST in g for g in gaps), gaps


def test_gate_refuses_balanced_tier_skipped_after_clean_fast(tmp_path, monkeypatch):
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0)
    _shard(td, DEEP, findings=0)
    gaps = require_receipts_for_done(td)
    assert any(AUDITOR in g and BALANCED in g for g in gaps), gaps


def test_gate_accepts_full_cascade(tmp_path, monkeypatch):
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=1)
    _shard(td, DEEP, findings=1)
    gaps = require_receipts_for_done(td)
    assert not any(AUDITOR in g for g in gaps), gaps


# ---------------------------------------------------------------------------
# receipt_audit_done: cascade order at the writer boundary
# ---------------------------------------------------------------------------

def _spawn_log(td: Path, name: str = AUDITOR) -> None:
    lines = [
        json.dumps({"event": "agent_spawn_pre", "subagent_type": name, "phase": "pre"}),
        json.dumps({"event": "agent_spawn_post", "subagent_type": name, "phase": "post",
                    "truncated": False, "stop_reason": "end_turn"}),
    ]
    (td / "spawn-log.jsonl").write_text("\n".join(lines) + "\n")


def _report(td: Path, model: str, findings: int = 0) -> Path:
    path = td / "audit-reports" / f"{AUDITOR}-{model}.json"
    path.write_text(json.dumps({
        "auditor_name": AUDITOR,
        "status": "complete",
        "verdict": "pass" if findings == 0 else "fail",
        "findings": [{"id": f"X-{i}", "blocking": False} for i in range(findings)],
        "blocking_count": 0,
    }))
    return path


def _write_shard_receipt(td: Path, model: str, findings: int = 0) -> Path:
    return receipt_audit_done(
        td,
        auditor_name=AUDITOR,
        model_used=model,
        finding_count=findings,
        blocking_count=0,
        report_path=str(_report(td, model, findings)),
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
        ensemble_context=True,
        shard_step_name=f"{AUDITOR}-{model}",
        tier=None,
    )


def test_receipt_refuses_deep_tier_before_fast_tier(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    with pytest.raises(ValueError, match="cascade order violation"):
        _write_shard_receipt(td, DEEP)
    assert not (td / "receipts" / f"audit-{AUDITOR}-{DEEP}.json").exists()


def test_receipt_refuses_balanced_tier_before_fast_tier(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    with pytest.raises(ValueError, match="cascade order violation"):
        _write_shard_receipt(td, BALANCED)


def test_receipt_accepts_cascade_in_order_and_refuses_after_complete(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST, findings=0)
    with pytest.raises(ValueError, match="next required tier is"):
        _write_shard_receipt(td, DEEP)  # fast was clean -> balanced is next
    _write_shard_receipt(td, BALANCED, findings=0)
    assert lib_ensemble.ensemble_next(td, AUDITOR)["status"] == "complete"
    with pytest.raises(ValueError, match="already complete"):
        _write_shard_receipt(td, DEEP)
    # A re-audit may always restart at the fast tier.
    _write_shard_receipt(td, FAST, findings=1)
    assert lib_ensemble.ensemble_next(td, AUDITOR)["model"] == DEEP


def test_receipt_fails_closed_without_ensemble_plan_entry(tmp_path):
    td = _setup_task(tmp_path, [_entry(ensemble=False, model=BALANCED)])
    _spawn_log(td)
    with pytest.raises(ValueError, match="ensemble=false"):
        _write_shard_receipt(td, FAST)
    (td / "audit-plan.json").unlink()
    (td / "receipts" / "audit-routing.json").unlink()
    with pytest.raises(ValueError, match="requires an ensemble entry"):
        _write_shard_receipt(td, FAST)


# ---------------------------------------------------------------------------
# router.py audit-inject-prompt
# ---------------------------------------------------------------------------

def _inject(td: Path, model: str | None) -> subprocess.CompletedProcess:
    args = [
        sys.executable, str(ROOT / "hooks" / "router.py"), "audit-inject-prompt",
        "--root", str(td.parent.parent),
        "--task-type", "feature",
        "--audit-plan", str(td / "audit-plan.json"),
        "--auditor-name", AUDITOR,
    ]
    if model is not None:
        args.extend(["--model", model])
    env = {**os.environ, "PYTHONPATH": str(ROOT / "hooks")}
    return subprocess.run(args, input="base prompt", text=True, capture_output=True,
                          check=False, env=env, cwd=str(ROOT))


def test_inject_prompt_refuses_off_cascade_tier(tmp_path):
    td = _setup_task(tmp_path)
    for model in (BALANCED, DEEP):
        res = _inject(td, model)
        assert res.returncode == 1, res.stdout + res.stderr
        payload = json.loads(res.stdout)
        assert "refusing to prepare" in payload["error"]
        assert payload["cascade"]["model"] == FAST
    assert not list((td / "receipts").glob("_injected-auditor-prompts/*"))


def test_inject_prompt_requires_model_for_ensemble_auditor(tmp_path):
    td = _setup_task(tmp_path)
    res = _inject(td, None)
    assert res.returncode == 1
    assert "requires --model" in json.loads(res.stdout)["error"]


def test_inject_prompt_accepts_next_cascade_step(tmp_path):
    td = _setup_task(tmp_path)
    res = _inject(td, FAST)
    assert res.returncode == 0, res.stdout + res.stderr
    _shard(td, FAST, findings=0)
    assert _inject(td, DEEP).returncode == 1
    res = _inject(td, BALANCED)
    assert res.returncode == 0, res.stdout + res.stderr
    # Fast tier is always allowed: a re-audit restarts the cascade.
    assert _inject(td, FAST).returncode == 0


# ---------------------------------------------------------------------------
# ctl ensemble-next / run-audit-summary
# ---------------------------------------------------------------------------

def _ctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=str(ROOT), text=True, capture_output=True, check=False,
    )


def test_ctl_ensemble_next_reports_pending_then_complete(tmp_path):
    td = _setup_task(tmp_path, [_entry(), _entry("cq", ensemble=False, model=BALANCED)])
    res = _ctl("ensemble-next", str(td))
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["status"] == "ok" and payload["complete"] is False
    assert payload["pending"] == [AUDITOR]
    by_name = {a["auditor"]: a for a in payload["auditors"]}
    assert by_name[AUDITOR]["status"] == "spawn" and by_name[AUDITOR]["model"] == FAST
    assert by_name["cq"]["status"] == "single" and by_name["cq"]["model"] == BALANCED

    _shard(td, FAST, findings=3)
    payload = json.loads(_ctl("ensemble-next", str(td), AUDITOR).stdout)
    assert payload["auditors"][0]["model"] == DEEP and payload["complete"] is False
    _shard(td, DEEP, findings=1)
    payload = json.loads(_ctl("ensemble-next", str(td)).stdout)
    assert payload["complete"] is True and payload["pending"] == []


def test_ctl_ensemble_next_without_plan_is_blocked(tmp_path):
    td = tmp_path / "project" / ".dynos" / "task-20260906-NP"
    td.mkdir(parents=True)
    res = _ctl("ensemble-next", str(td))
    assert res.returncode == 1
    assert json.loads(res.stdout)["status"] == "blocked"


def test_ctl_run_audit_summary_blocked_while_cascade_incomplete(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, DEEP, findings=0)  # escalation-only: the old shortcut
    res = _ctl("run-audit-summary", str(td))
    assert res.returncode == 1, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["status"] == "blocked" and payload["ensemble_gaps"]
    assert not (td / "audit-summary.json").exists()


def test_ctl_run_audit_summary_proceeds_when_cascade_complete(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0)
    _shard(td, BALANCED, findings=0)
    res = _ctl("run-audit-summary", str(td))
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(res.stdout)["status"] == "audit_summary_ready"
