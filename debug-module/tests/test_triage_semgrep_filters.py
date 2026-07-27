"""
Regression tests for debug-module/triage.py _SEMGREP_RULE_FILTERS — AC 1 & AC 2
(task-20260616-002, finding #11).

The eight stale rule IDs currently in _SEMGREP_RULE_FILTERS do not exist in
rules/silent-accomplices.yml, so every data-corruption / race-condition
semgrep finding is silently dropped. These tests encode the FIXED behaviour:
every filter ID must be a real ID from the ruleset, and findings carrying the
real IDs must NOT be dropped by the filter logic.
"""
import sys
from pathlib import Path

import pytest

# Ensure debug-module/ is on sys.path so 'import triage' and 'from debuglib import ...' work.
_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - PyYAML is a project dependency
    yaml = None


def _import_triage():
    try:
        import triage
        return triage
    except ModuleNotFoundError as exc:  # pragma: no cover
        pytest.fail(f"triage module not importable: {exc}")


def _import_run_semgrep():
    try:
        from debuglib import run_semgrep
        return run_semgrep
    except ModuleNotFoundError as exc:  # pragma: no cover
        pytest.fail(f"run_semgrep module not importable: {exc}")


def _all_filter_ids(triage) -> set[str]:
    ids: set[str] = set()
    for value in triage._SEMGREP_RULE_FILTERS.values():
        if value is None:
            continue
        for rid in value:
            ids.add(rid)
    return ids


def _ruleset_ids(triage) -> set[str]:
    if yaml is None:
        pytest.skip("PyYAML not available")
    rules_path = Path(triage.RULES_PATH)
    assert rules_path.exists(), f"ruleset YAML missing at {rules_path}"
    data = yaml.safe_load(rules_path.read_text())
    rules = data.get("rules", []) if isinstance(data, dict) else []
    return {r["id"] for r in rules if isinstance(r, dict) and isinstance(r.get("id"), str)}


# ---------------------------------------------------------------------------
# AC 1: drift test — every filter ID exists in the loaded ruleset
# ---------------------------------------------------------------------------

def test_semgrep_filter_ids_exist_in_ruleset():
    """Every ID across all lists in _SEMGREP_RULE_FILTERS must be a real ID
    in rules/silent-accomplices.yml (set containment)."""
    triage = _import_triage()
    filter_ids = _all_filter_ids(triage)
    yaml_ids = _ruleset_ids(triage)

    assert filter_ids, "_SEMGREP_RULE_FILTERS produced no IDs to validate"
    missing = filter_ids - yaml_ids
    assert not missing, (
        "stale/nonexistent semgrep rule IDs in _SEMGREP_RULE_FILTERS "
        f"(not present in {triage.RULES_PATH}): {sorted(missing)}"
    )


def test_no_stale_rule_ids_remain():
    """The four named stale IDs must not appear anywhere in the filters."""
    triage = _import_triage()
    filter_ids = _all_filter_ids(triage)
    stale = {
        "silent-accomplices.swallowed-exception",
        "silent-accomplices.python-bare-except",
        "silent-accomplices.js-floating-promise",
        "silent-accomplices.python-asyncio-create-task-orphan",
    }
    leaked = stale & filter_ids
    assert not leaked, f"stale rule IDs still present in filters: {sorted(leaked)}"


def test_expected_real_ids_are_mapped():
    """The fixed mapping must contain the confirmed real IDs for both bug types."""
    triage = _import_triage()
    data_corruption = set(triage._SEMGREP_RULE_FILTERS.get("data-corruption") or [])
    race_condition = set(triage._SEMGREP_RULE_FILTERS.get("race-condition") or [])

    expected_dc = {
        "silent-accomplices.swallowed-error-python",
        "silent-accomplices.swallowed-error-js",
        "silent-accomplices.swallowed-error-java",
        "silent-accomplices.swallowed-error-ruby",
        "silent-accomplices.default-masking-python",
        "silent-accomplices.default-masking-js",
    }
    expected_rc = {
        "silent-accomplices.missing-await-js",
        "silent-accomplices.missing-await-python",
        "silent-accomplices.go-map-range-order",
    }
    assert expected_dc <= data_corruption, (
        f"data-corruption filter missing real IDs: {sorted(expected_dc - data_corruption)}"
    )
    assert expected_rc <= race_condition, (
        f"race-condition filter missing real IDs: {sorted(expected_rc - race_condition)}"
    )


# ---------------------------------------------------------------------------
# AC 2: findings carrying the real IDs are NOT dropped by the filter logic
# ---------------------------------------------------------------------------

def _semgrep_json_for(rule_id: str) -> str:
    import json
    return json.dumps(
        {
            "results": [
                {
                    "check_id": rule_id,
                    "path": "src/example.py",
                    "start": {"line": 12},
                    "extra": {"message": "found", "severity": "ERROR"},
                }
            ]
        }
    )


def test_semgrep_findings_not_dropped_for_data_corruption_and_race_condition():
    """A semgrep finding whose rule_id is one of the configured filter IDs for
    data-corruption / race-condition must survive _parse_findings (i.e. the
    filter IDs match real check_id values). If the stale-ID bug is reintroduced,
    the configured filter IDs will not match these real findings and the
    findings will be dropped -> this test fails."""
    triage = _import_triage()
    run_semgrep = _import_run_semgrep()

    dc_ids = triage._SEMGREP_RULE_FILTERS.get("data-corruption") or []
    rc_ids = triage._SEMGREP_RULE_FILTERS.get("race-condition") or []
    assert dc_ids and rc_ids, "filters for data-corruption / race-condition are empty"

    # Use a real ruleset ID as the finding's check_id, filtered by the configured list.
    dc_finding_id = "silent-accomplices.swallowed-error-python"
    rc_finding_id = "silent-accomplices.missing-await-python"

    dc_findings = run_semgrep._parse_findings(_semgrep_json_for(dc_finding_id), list(dc_ids))
    rc_findings = run_semgrep._parse_findings(_semgrep_json_for(rc_finding_id), list(rc_ids))

    dc_kept = [f for f in dc_findings if f.get("rule_id") == dc_finding_id]
    rc_kept = [f for f in rc_findings if f.get("rule_id") == rc_finding_id]

    assert dc_kept, (
        "data-corruption finding was dropped: configured filter IDs "
        f"{sorted(dc_ids)} do not include the real ID {dc_finding_id!r}"
    )
    assert rc_kept, (
        "race-condition finding was dropped: configured filter IDs "
        f"{sorted(rc_ids)} do not include the real ID {rc_finding_id!r}"
    )
