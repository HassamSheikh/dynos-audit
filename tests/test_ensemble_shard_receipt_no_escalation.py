"""Tests for AC 3: Ensemble shard receipts satisfy _check_ensemble_voting with zero gaps.

This test is RED by design until seg-1 adds:
  - shard_step_name parameter to receipt_audit_done in hooks/receipts/stage.py
  - Receipt written to audit-{shard_step_name} when ensemble_context=True and
    shard_step_name is provided

The test calls receipt_audit_done twice with ensemble_context=True and
shard_step_name="sc-haiku" / "sc-sonnet" respectively, using a fixture task dir
with matching spawn-log entries. Then calls _check_ensemble_voting and asserts
the returned gaps list is empty.

Per Finding B and plan.md fixtures section:
  - spawn-log.jsonl requires matching agent_spawn_post entries for anti-forgery
  - _assert_spawn_log_evidence is unconditional when spawn-log.jsonl exists
  - route_mode="generic" allows injected_agent_sha256=None (no sidecar needed)
  - ensemble_context=True requires a real report_path pointing at an existing file
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from receipts.stage import receipt_audit_done  # noqa: E402
from lib_core import _check_ensemble_voting  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

AUDITOR_NAME = "sc"          # short name; will produce receipts/audit-sc-haiku etc.
MODEL_HAIKU = "haiku"        # noqa: model-literal
MODEL_SONNET = "sonnet"      # noqa: model-literal
SHARD_STEP_HAIKU = f"sc-{MODEL_HAIKU}"   # noqa: model-literal
SHARD_STEP_SONNET = f"sc-{MODEL_SONNET}" # noqa: model-literal


def _create_task_dir(tmp_path: Path) -> Path:
    """Create a task dir with receipts/ directory and spawn-log.jsonl."""
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20260612-ensemble"
    task_dir.mkdir(parents=True)
    (task_dir / "receipts").mkdir()
    (task_dir / "audit-reports").mkdir()
    # receipt_audit_done(ensemble_context=True) fails closed unless the
    # auditor has an ensemble entry in audit-plan.json (cascade-order check).
    (task_dir / "audit-plan.json").write_text(json.dumps({
        "auditors": [{
            "name": AUDITOR_NAME,
            "action": "spawn",
            "ensemble": True,
            "ensemble_voting_models": [MODEL_HAIKU, MODEL_SONNET],  # noqa: model-literal
            "ensemble_escalation_model": "",
        }]
    }), encoding="utf-8")
    return task_dir


def _write_spawn_log(task_dir: Path, auditor_name: str) -> None:
    """Write a spawn-log.jsonl with a valid agent_spawn_post entry for the auditor.

    The normalized key for 'sc' is 'sc' (no audit- prefix, no -auditor suffix).
    _normalize_auditor_key matches subagent_type against auditor_name.
    We use auditor_name as subagent_type to ensure exact match.
    """
    # Write both pre and post entries so the cross-check passes
    pre_entry = json.dumps({
        "event": "agent_spawn_pre",
        "subagent_type": auditor_name,
        "phase": "pre",
    })
    post_entry = json.dumps({
        "event": "agent_spawn_post",
        "subagent_type": auditor_name,
        "phase": "post",
        "truncated": False,
        "stop_reason": "end_turn",
    })
    spawn_log = task_dir / "spawn-log.jsonl"
    spawn_log.write_text(f"{pre_entry}\n{post_entry}\n", encoding="utf-8")


def _write_report(task_dir: Path, filename: str, blocking_count: int = 0) -> Path:
    """Write a minimal audit report file with the given findings count."""
    report_path = task_dir / "audit-reports" / filename
    report_data = {
        "auditor_name": AUDITOR_NAME,
        "status": "complete",
        "verdict": "pass" if blocking_count == 0 else "fail",
        "findings": [],
        "blocking_count": blocking_count,
    }
    report_path.write_text(json.dumps(report_data), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# AC 3: receipt_audit_done with shard_step_name produces receipts at
#        audit-{shard_step_name}; _check_ensemble_voting finds them; gaps=[]
# ---------------------------------------------------------------------------


def test_ensemble_shard_receipt_no_escalation(tmp_path: Path) -> None:
    """Call receipt_audit_done twice with shard_step_name; _check_ensemble_voting returns gaps=[].

    AC 3:
    1. receipt_audit_done(shard_step_name="sc-haiku") writes receipt at receipts/audit-sc-haiku
    2. receipt_audit_done(shard_step_name="sc-sonnet") writes receipt at receipts/audit-sc-sonnet
    3. _check_ensemble_voting with routing configured for haiku+sonnet ensemble reads those
       receipts via its existing shard-read path (audit-{name}-{model}) at lib_core.py:614
    4. Returns gaps=[] (zero escalation needed)

    The _check_ensemble_voting reads at f"audit-{name}-{model}" (audit-sc-haiku, audit-sc-sonnet).
    So the shard_step_name used in receipt_audit_done MUST be f"sc-{model}" = "sc-haiku"/"sc-sonnet".
    """
    task_dir = _create_task_dir(tmp_path)

    # The spawn-log cross-check keys on auditor_name (NOT shard_step_name)
    _write_spawn_log(task_dir, AUDITOR_NAME)

    # Write real report files (required when ensemble_context=True per _validate_audit_done_args)
    report_haiku = _write_report(task_dir, "sc-haiku-attempt-1.json", blocking_count=0)  # noqa: model-literal
    report_sonnet = _write_report(task_dir, "sc-sonnet-attempt-1.json", blocking_count=0)  # noqa: model-literal

    # Call 1: shard_step_name="sc-haiku" → receipt at receipts/audit-sc-haiku
    # Use route_mode="generic" so injected_agent_sha256=None is allowed (no sidecar needed)
    receipt_audit_done(
        task_dir,
        auditor_name=AUDITOR_NAME,
        model_used=MODEL_HAIKU,  # noqa: model-literal
        finding_count=0,
        blocking_count=0,
        report_path=str(report_haiku),
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
        ensemble_context=True,
        shard_step_name=SHARD_STEP_HAIKU,  # noqa: model-literal
        tier=None,
    )

    # Verify receipt was written at audit-sc-haiku (not audit-sc)
    haiku_receipt_path = task_dir / "receipts" / f"audit-{SHARD_STEP_HAIKU}.json"
    assert haiku_receipt_path.exists(), (
        f"Receipt must be written at receipts/audit-{SHARD_STEP_HAIKU}.json, "
        f"but it doesn't exist. (Receipts dir contains: "
        f"{list((task_dir / 'receipts').iterdir())})"
    )

    # Call 2: shard_step_name="sc-sonnet" → receipt at receipts/audit-sc-sonnet
    receipt_audit_done(
        task_dir,
        auditor_name=AUDITOR_NAME,
        model_used=MODEL_SONNET,  # noqa: model-literal
        finding_count=0,
        blocking_count=0,
        report_path=str(report_sonnet),
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
        ensemble_context=True,
        shard_step_name=SHARD_STEP_SONNET,  # noqa: model-literal
        tier=None,
    )

    # Verify receipt was written at audit-sc-sonnet
    sonnet_receipt_path = task_dir / "receipts" / f"audit-{SHARD_STEP_SONNET}.json"
    assert sonnet_receipt_path.exists(), (
        f"Receipt must be written at receipts/audit-{SHARD_STEP_SONNET}.json, "
        f"but it doesn't exist."
    )

    # Now call _check_ensemble_voting with a routing entry for this ensemble
    # _check_ensemble_voting reads at audit-{name}-{model} via read_receipt(task_dir, f"audit-{name}-{model}")
    # So it will look for: audit-sc-haiku and audit-sc-sonnet → matches our shard_step_name receipts
    routing_by_name = {
        AUDITOR_NAME: {
            "action": "spawn",
            "ensemble": True,
            "ensemble_voting_models": [MODEL_HAIKU, MODEL_SONNET],  # noqa: model-literal
            "ensemble_escalation_model": "",  # no escalation model needed (all zero-blocking)
        }
    }
    registry_eligible: set[str] = {AUDITOR_NAME}

    gaps = _check_ensemble_voting(task_dir, routing_by_name, registry_eligible)

    assert gaps == [], (
        f"_check_ensemble_voting must return empty gaps when both shard receipts "
        f"exist with zero blocking. Got gaps: {gaps}"
    )


def test_ensemble_shard_receipt_spawn_log_keys_on_auditor_name(tmp_path: Path) -> None:
    """The spawn-log cross-check keys on auditor_name (D3: NOT shard_step_name).

    AC 3 (D3): The anti-forgery spawn-log check is unconditional. It uses
    auditor_name for matching — NOT shard_step_name. A receipt with
    shard_step_name="sc-haiku" still requires a spawn-log entry for "sc".
    """
    task_dir = _create_task_dir(tmp_path)

    # Write spawn-log for the WRONG auditor (not matching "sc")
    wrong_auditor_log = json.dumps({
        "event": "agent_spawn_post",
        "subagent_type": "completely-different-auditor",
        "phase": "post",
        "truncated": False,
    })
    (task_dir / "spawn-log.jsonl").write_text(wrong_auditor_log + "\n")

    report_haiku = _write_report(task_dir, "sc-haiku-attempt-1.json", blocking_count=0)  # noqa: model-literal

    # Should raise ValueError because spawn-log doesn't have an entry for "sc"
    with pytest.raises(ValueError, match="spawn-log"):
        receipt_audit_done(
            task_dir,
            auditor_name=AUDITOR_NAME,
            model_used=MODEL_HAIKU,  # noqa: model-literal
            finding_count=0,
            blocking_count=0,
            report_path=str(report_haiku),
            route_mode="generic",
            agent_path=None,
            injected_agent_sha256=None,
            ensemble_context=True,
            shard_step_name=SHARD_STEP_HAIKU,  # noqa: model-literal
            tier=None,
        )


def test_ensemble_shard_receipt_rejects_audit_prefixed_name(tmp_path: Path) -> None:
    """Prevent audit-audit-* receipts by refusing pre-prefixed shard names."""
    task_dir = _create_task_dir(tmp_path)
    _write_spawn_log(task_dir, AUDITOR_NAME)
    report_haiku = _write_report(task_dir, "sc-haiku-attempt-1.json", blocking_count=0)  # noqa: model-literal

    with pytest.raises(ValueError, match="shard_step_name must be"):
        receipt_audit_done(
            task_dir,
            auditor_name=AUDITOR_NAME,
            model_used=MODEL_HAIKU,  # noqa: model-literal
            finding_count=0,
            blocking_count=0,
            report_path=str(report_haiku),
            route_mode="generic",
            agent_path=None,
            injected_agent_sha256=None,
            ensemble_context=True,
            shard_step_name=f"audit-{SHARD_STEP_HAIKU}",  # noqa: model-literal
            tier=None,
        )
