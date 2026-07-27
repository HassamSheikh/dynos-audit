#!/usr/bin/env python3
"""triage.py — Deterministic orchestrator. Emits evidence_dossier.json.

Runs the fixed-order debug-module pipeline. Every step is wrapped in
try/except — any failure is recorded to pipeline_outputs["pipeline_errors"]
and the pipeline continues. Output is written atomically.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import traceback
from typing import Any

# CRITICAL: insert own directory into sys.path[0] so `from debuglib import X`
# works regardless of the caller's working directory.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from debuglib import (  # noqa: E402  (sys.path tweak above is intentional)
    bug_classifier,
    coverage_gaps,
    dossier,
    git_forensics,
    language_detect,
    log_surface,
    parse_stacktrace,
    run_linters,
    run_semgrep,
    run_tests,
    schema_drift,
)

# Resolve the semgrep rules path relative to this script (NOT cwd).
RULES_PATH = os.path.join(_SCRIPT_DIR, "rules", "silent-accomplices.yml")

# Bug-type-specific semgrep rule_id filters. None means "no filter".
_SEMGREP_RULE_FILTERS: dict[str, list[str] | None] = {
    "data-corruption": [
        "silent-accomplices.swallowed-error-python",
        "silent-accomplices.swallowed-error-js",
        "silent-accomplices.swallowed-error-java",
        "silent-accomplices.swallowed-error-ruby",
        "silent-accomplices.default-masking-python",
        "silent-accomplices.default-masking-js",
    ],
    "race-condition": [
        "silent-accomplices.missing-await-js",
        "silent-accomplices.missing-await-python",
        "silent-accomplices.go-map-range-order",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="triage.py",
        description=(
            "Deterministic debug-module orchestrator. Runs the bug triage "
            "pipeline and emits an evidence dossier as JSON."
        ),
    )
    p.add_argument(
        "--bug",
        required=True,
        help="Bug description (free-text). Required.",
    )
    p.add_argument(
        "--repo",
        required=True,
        help="Path to the repository to analyse. Required.",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output path for the evidence_dossier.json file. Required.",
    )
    p.add_argument(
        "--since",
        default=None,
        help=(
            "Optional git --since reference (e.g. '2 weeks ago' or 'HEAD~10') "
            "passed through to git_forensics. Defaults to last 30 commits."
        ),
    )
    p.add_argument(
        "--run-tests",
        action="store_true",
        default=False,
        help=(
            "Optional flag — when set, the pipeline also runs the language-"
            "appropriate test runners. Off by default (tests can be slow)."
        ),
    )
    return p


def _short_sha(repo_path: str) -> str:
    """Return a 7-char git short sha for HEAD, or a random hex fallback."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        sha = (proc.stdout or "").strip()
        if proc.returncode == 0 and sha:
            # Defensive: only allow hex-like content, exactly 7 chars.
            sha = "".join(c for c in sha if c.isalnum())[:7]
            if len(sha) == 7:
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    except Exception:
        pass
    # Fallback: random 7-char hex.
    return secrets.token_hex(4)[:7]


def _make_investigation_id(repo_path: str) -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime())
    sha = _short_sha(repo_path)
    return f"INV-{ts}-{sha}"


def _record_error(
    pipeline_errors: list[dict[str, str]],
    step: str,
    exc: BaseException,
) -> None:
    pipeline_errors.append({
        "step": step,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
    })


def _atomic_write_json(path: str, data: Any) -> None:
    """Serialize `data` to JSON and replace `path` atomically.

    Writes to `path + ".tmp"` first, then `os.replace`s onto `path`. The
    parent directory is created if missing. Errors propagate.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    payload = json.dumps(data, indent=2, sort_keys=True, default=str)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # fsync may fail on some FS (e.g. /tmp tmpfs); not fatal.
            pass
    os.replace(tmp, path)


def _extract_file_paths(mentioned: Any) -> list[str]:
    """Pull plain file path strings out of bug_classifier mentioned_files."""
    out: list[str] = []
    if not isinstance(mentioned, list):
        return out
    seen: set[str] = set()
    for item in mentioned:
        path: str | None = None
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict):
            raw = item.get("path") or item.get("file")
            if isinstance(raw, str):
                path = raw
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _merge_stack_files(
    file_list: list[str], stack_frames: list[dict[str, Any]]
) -> list[str]:
    """Add stack-frame file paths to file_list (deduped, order-preserving)."""
    seen: set[str] = set(file_list)
    out: list[str] = list(file_list)
    if not isinstance(stack_frames, list):
        return out
    for frame in stack_frames:
        if not isinstance(frame, dict):
            continue
        f = frame.get("file")
        if isinstance(f, str) and f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    bug_text: str,
    repo_path: str,
    since: str | None,
    run_tests_flag: bool,
) -> dict[str, Any]:
    """Execute the fixed-order pipeline. NEVER raises — all errors are
    captured into pipeline_outputs["pipeline_errors"] and the run continues.
    """
    pipeline_errors: list[dict[str, str]] = []
    pipeline_outputs: dict[str, Any] = {
        "investigation_id": _make_investigation_id(repo_path),
        "bug_text": bug_text,
        "repo_path": repo_path,
        "pipeline_errors": pipeline_errors,
    }

    # Step 1: classify bug.
    classification: dict[str, Any] = {
        "bug_type": "unknown",
        "mentioned_files": [],
        "mentioned_symbols": [],
    }
    try:
        result = bug_classifier.classify(bug_text)
        if isinstance(result, dict):
            classification.update(result)
    except Exception as exc:
        _record_error(pipeline_errors, "bug_classifier.classify", exc)

    bug_type = str(classification.get("bug_type") or "unknown")
    mentioned_files_struct = classification.get("mentioned_files") or []
    mentioned_symbols = classification.get("mentioned_symbols") or []
    pipeline_outputs["bug_type"] = bug_type
    pipeline_outputs["mentioned_files"] = mentioned_files_struct
    pipeline_outputs["mentioned_symbols"] = mentioned_symbols

    mentioned_paths = _extract_file_paths(mentioned_files_struct)

    # Step 2: detect languages.
    languages: list[str] = []
    try:
        detected = language_detect.detect(repo_path, mentioned_paths)
        if isinstance(detected, list):
            languages = [str(x) for x in detected]
    except Exception as exc:
        _record_error(pipeline_errors, "language_detect.detect", exc)
    pipeline_outputs["languages"] = languages

    # Step 3: parse stack trace.
    stack_frames: list[dict[str, Any]] = []
    try:
        frames = parse_stacktrace.parse(bug_text)
        if isinstance(frames, list):
            stack_frames = frames
    except Exception as exc:
        _record_error(pipeline_errors, "parse_stacktrace.parse", exc)
    pipeline_outputs["stack_frames"] = stack_frames

    # Step 3.5: branch dispatch — set per-bug-type pipeline flags.
    semgrep_rule_ids = _SEMGREP_RULE_FILTERS.get(bug_type)  # may be None
    enable_run_tests = bool(run_tests_flag)
    if bug_type == "test-failure":
        # Test failures benefit most from run_tests; only honour the CLI flag.
        enable_run_tests = bool(run_tests_flag)
    # "performance" -> lizard is universal in run_linters and will run anyway.
    # "data-corruption" -> schema_drift always runs (Step 11 below).

    # Step 4.5: coverage gaps.
    cov_gaps: list[dict[str, Any]] = []
    try:
        result = coverage_gaps.find_gaps(repo_path, languages)
        if isinstance(result, list):
            cov_gaps = result
    except Exception as exc:
        _record_error(pipeline_errors, "coverage_gaps.find_gaps", exc)
    pipeline_outputs["coverage_gaps"] = cov_gaps

    # Step 5: linters.
    linter_findings: list[dict[str, Any]] = []
    try:
        result = run_linters.run(repo_path, languages)
        if isinstance(result, list):
            linter_findings = result
    except Exception as exc:
        _record_error(pipeline_errors, "run_linters.run", exc)
    pipeline_outputs["linter_findings"] = linter_findings

    # Step 6: semgrep.
    semgrep_findings: list[dict[str, Any]] = []
    try:
        result = run_semgrep.run(
            repo_path, RULES_PATH, languages, semgrep_rule_ids
        )
        if isinstance(result, list):
            semgrep_findings = result
    except Exception as exc:
        _record_error(pipeline_errors, "run_semgrep.run", exc)
    pipeline_outputs["semgrep_findings"] = semgrep_findings

    # Step 7: git forensics — blame mentioned files + stack-frame files.
    forensic_files = _merge_stack_files(mentioned_paths, stack_frames)
    git_result: dict[str, Any] = {
        "blame_ranges": {},
        "recent_commits": [],
        "co_change_pairs": [],
    }
    try:
        result = git_forensics.analyze(repo_path, forensic_files, since)
        if isinstance(result, dict):
            git_result = result
    except Exception as exc:
        _record_error(pipeline_errors, "git_forensics.analyze", exc)
    pipeline_outputs["git_forensics"] = git_result

    # Step 8: log surface.
    log_entries: list[dict[str, Any]] = []
    try:
        result = log_surface.surface(repo_path, bug_text)
        if isinstance(result, list):
            log_entries = result
    except Exception as exc:
        _record_error(pipeline_errors, "log_surface.surface", exc)
    pipeline_outputs["log_entries"] = log_entries

    # Step 9 (optional): run_tests.
    test_results: list[dict[str, Any]] = []
    if enable_run_tests:
        try:
            result = run_tests.run(repo_path, languages)
            if isinstance(result, list):
                test_results = result
        except Exception as exc:
            _record_error(pipeline_errors, "run_tests.run", exc)
    pipeline_outputs["test_results"] = test_results

    # Step 10: schema drift.
    drift_results: list[dict[str, Any]] = []
    try:
        result = schema_drift.check(repo_path)
        if isinstance(result, list):
            drift_results = result
    except Exception as exc:
        _record_error(pipeline_errors, "schema_drift.check", exc)
    pipeline_outputs["schema_drift"] = drift_results

    # Step 11: assemble dossier.
    try:
        assembled = dossier.assemble(pipeline_outputs)
    except Exception as exc:
        _record_error(pipeline_errors, "dossier.assemble", exc)
        # Build a minimal valid-shape dossier so the schema is still satisfied.
        assembled = {
            "investigation_id": pipeline_outputs["investigation_id"],
            "bug_type": bug_type,
            "bug_text": bug_text,
            "repo_path": repo_path,
            "languages_detected": languages,
            "pipeline_errors": [
                f"{e.get('step', '?')}: {e.get('error', '')}"
                for e in pipeline_errors
            ],
            "evidence_index": {},
        }

    # Ensure pipeline_errors in the assembled dossier reflect ALL errors,
    # including any that occurred during dossier.assemble itself.
    if isinstance(assembled, dict):
        assembled["pipeline_errors"] = [
            f"{e.get('step', '?')}: {e.get('error', '')}"
            for e in pipeline_errors
        ]

    return assembled


# ---------------------------------------------------------------------------
# finalize — deterministic persistence of the investigator's returned JSON
# ---------------------------------------------------------------------------
# The @investigator agent is read-only (tools: Read, Grep) and RETURNS its
# bug-report JSON as its final message; it never writes a file. The
# orchestrator pipes that JSON here. This step owns schema validation,
# citation validation (every cited evidence ID must exist in the dossier),
# and persistence of both the JSON and the rendered Markdown — so the
# "citation validation third" guarantee is unskippable: it sits inside the
# only sanctioned persistence path.

def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _validate_bug_report_structure(report: Any) -> list[str]:
    """Minimal structural validation of bug_report.schema.json invariants."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["bug report must be a JSON object"]
    for key in ("investigation_id", "causal_chain", "root_cause", "recommended_fix"):
        if key not in report:
            errors.append(f"missing required field: {key}")
    chain = report.get("causal_chain")
    if not isinstance(chain, list) or not chain:
        errors.append("causal_chain must be a non-empty array")
    else:
        for idx, step in enumerate(chain):
            if not isinstance(step, dict):
                errors.append(f"causal_chain[{idx}] must be an object")
                continue
            if not str(step.get("description", "")).strip():
                errors.append(f"causal_chain[{idx}].description must be non-empty")
            ids = step.get("evidence_ids")
            if not isinstance(ids, list) or not ids:
                errors.append(
                    f"causal_chain[{idx}].evidence_ids must be non-empty "
                    "(an uncited claim is a contract violation)"
                )
    for section in ("root_cause", "recommended_fix"):
        block = report.get(section)
        if not isinstance(block, dict):
            errors.append(f"{section} must be an object")
            continue
        if not str(block.get("description", "")).strip():
            errors.append(f"{section}.description must be non-empty")
        ids = block.get("evidence_ids")
        if not isinstance(ids, list) or not ids:
            errors.append(f"{section}.evidence_ids must be non-empty")
    return errors


def _collect_all_cited_ids(report: dict) -> list[str]:
    cited: list[str] = []
    for step in report.get("causal_chain") or []:
        if isinstance(step, dict):
            cited.extend(str(x) for x in (step.get("evidence_ids") or []))
    for section in ("root_cause", "recommended_fix"):
        block = report.get(section)
        if isinstance(block, dict):
            cited.extend(str(x) for x in (block.get("evidence_ids") or []))
    return cited


def _finalize_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="triage.py finalize",
        description=(
            "Validate the investigator's returned bug-report JSON against the "
            "dossier and persist report + rendered Markdown."
        ),
    )
    parser.add_argument(
        "--report", required=True,
        help="Bug-report JSON file path, or '-' to read from stdin",
    )
    parser.add_argument("--dossier", required=True, help="Evidence dossier JSON path")
    parser.add_argument(
        "--out-dir", default=None,
        help="Output directory (default: the dossier's directory)",
    )
    args = parser.parse_args(argv)

    if args.report == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(args.report, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            sys.stderr.write(f"finalize: cannot read report: {exc}\n")
            return 2
    try:
        report = json.loads(_strip_markdown_fences(raw))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"finalize: report is not valid JSON: {exc}\n")
        return 1

    try:
        with open(args.dossier, "r", encoding="utf-8") as fh:
            dossier_data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"finalize: cannot load dossier: {exc}\n")
        return 2

    structural = _validate_bug_report_structure(report)
    if structural:
        sys.stderr.write(
            "finalize: bug report rejected (structural violations):\n"
            + "".join(f"  - {e}\n" for e in structural)
        )
        return 1

    evidence_index = dossier_data.get("evidence_index") or {}
    known_ids = {str(k) for k in evidence_index.keys()} if isinstance(evidence_index, dict) else set()
    unknown = sorted({eid for eid in _collect_all_cited_ids(report) if eid not in known_ids})
    if unknown:
        sys.stderr.write(
            "finalize: bug report rejected — citations not present in the "
            f"dossier: {', '.join(unknown)}\n"
            "The investigator may only cite pre-minted evidence IDs.\n"
        )
        return 1

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.dossier))
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, "bug_report.json")
    _atomic_write_json(report_path, report)

    from debuglib import render_report as _render_report  # noqa: PLC0415
    markdown = _render_report.render(report, dossier_data)
    md_path = os.path.join(out_dir, "report.md")
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    os.replace(tmp_md, md_path)

    print(json.dumps({
        "status": "report_finalized",
        "report_path": report_path,
        "markdown_path": md_path,
        "citations_validated": len(set(_collect_all_cited_ids(report))),
    }, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "finalize":
        return _finalize_main(raw_argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)

    bug_text: str = args.bug if isinstance(args.bug, str) else str(args.bug)
    repo_path: str = args.repo if isinstance(args.repo, str) else str(args.repo)
    out_path: str = args.out if isinstance(args.out, str) else str(args.out)
    since: str | None = args.since
    run_tests_flag: bool = bool(args.run_tests)

    # Run the pipeline. run_pipeline must never raise.
    try:
        result = run_pipeline(bug_text, repo_path, since, run_tests_flag)
    except Exception as exc:
        # Defensive: if the orchestrator itself somehow crashed, still emit a
        # minimal dossier so downstream consumers see something.
        result = {
            "investigation_id": _make_investigation_id(repo_path),
            "bug_type": "unknown",
            "bug_text": bug_text,
            "repo_path": repo_path,
            "languages_detected": [],
            "pipeline_errors": [f"orchestrator: {type(exc).__name__}: {exc}"],
            "evidence_index": {},
        }

    # Atomic write to --out. If the write itself fails, surface a clean error.
    try:
        _atomic_write_json(out_path, result)
    except Exception as exc:
        sys.stderr.write(
            f"triage.py: failed to write output to {out_path}: "
            f"{type(exc).__name__}: {exc}\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
