"""
run_semgrep — Invoke semgrep against a repository with a given ruleset.

Public API:
    run(
        repo_path: str,
        rules_path: str,
        languages: list[str],
        rule_ids: list[str] | None,
    ) -> list[dict]

Behaviour:
    - Invokes `semgrep --json --config <rules_path> <repo_path>`, with the
      common build/dependency directories excluded.
    - When `rule_ids` is provided, only findings whose rule_id is in that
      list are returned.
    - Each finding dict has keys:
        rule_id (str), file (str), line (int), message (str), severity (str)
    - When semgrep is NOT in PATH, returns:
        [{"tool": "semgrep", "skipped": True, "reason": "not installed"}]
    - When semgrep exits non-zero, captures stderr and returns the findings
      parsed so far plus an error record. No exception is raised.
"""

from __future__ import annotations

import json
import subprocess

# Common directories that produce false positives or massive noise.
_EXCLUDE_DIRS: tuple[str, ...] = ("node_modules", "dist", "build", "vendor")

# Allow generous time for large repos but never hang the pipeline.
_SEMGREP_TIMEOUT_SECONDS = 300


def run(
    repo_path: str,
    rules_path: str,
    languages: list[str],
    rule_ids: list[str] | None,
) -> list[dict]:
    """Run semgrep and normalise its output.

    Returns a list of finding dicts and/or skip / error records. The function
    never raises for missing binaries, missing paths, timeouts, malformed
    JSON, or non-zero semgrep exit codes.
    """
    if not isinstance(repo_path, str):
        repo_path = str(repo_path)
    if not isinstance(rules_path, str):
        rules_path = str(rules_path)

    cmd: list[str] = [
        "semgrep",
        "--json",
        "--config",
        rules_path,
    ]
    for ex in _EXCLUDE_DIRS:
        cmd.extend(["--exclude", ex])
    cmd.append(repo_path)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SEMGREP_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        # Race between which() and exec.
        return [{
            "tool": "semgrep",
            "skipped": True,
            "reason": "not installed",
        }]
    except subprocess.TimeoutExpired as exc:
        return [{
            "tool": "semgrep",
            "skipped": True,
            "reason": f"timeout after {exc.timeout}s",
        }]
    except OSError as exc:
        return [{
            "tool": "semgrep",
            "skipped": True,
            "reason": f"os error: {exc}",
        }]

    findings = _parse_findings(proc.stdout, rule_ids)

    # semgrep returns 1 when findings exist and >=2 for errors. Treat
    # anything outside {0, 1} as an error condition and surface stderr.
    returncode = getattr(proc, "returncode", 0)
    if returncode not in (0, 1):
        findings.append({
            "tool": "semgrep",
            "error": True,
            "returncode": returncode,
            "stderr": (proc.stderr or "").strip(),
        })

    return findings


def _parse_findings(
    stdout: str,
    rule_ids: list[str] | None,
) -> list[dict]:
    """Parse semgrep --json stdout into our finding dict shape.

    Malformed JSON is reported as an error record rather than raising, so
    the caller still receives a well-formed list.
    """
    if not stdout or not stdout.strip():
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [{
            "tool": "semgrep",
            "error": True,
            "reason": f"invalid json output: {exc}",
        }]

    if not isinstance(data, dict):
        return [{
            "tool": "semgrep",
            "error": True,
            "reason": "unexpected json shape (not an object)",
        }]

    rule_id_filter: set[str] | None
    if rule_ids:
        rule_id_filter = {rid for rid in rule_ids if isinstance(rid, str)}
    else:
        rule_id_filter = None

    raw_results = data.get("results", [])
    findings: list[dict] = []
    if not isinstance(raw_results, list):
        return findings

    for r in raw_results:
        if not isinstance(r, dict):
            continue
        rule_id = r.get("check_id", "")
        if rule_id_filter is not None and rule_id not in rule_id_filter:
            continue
        extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        start = r.get("start") if isinstance(r.get("start"), dict) else {}
        line_val = start.get("line") if isinstance(start, dict) else None
        try:
            line = int(line_val) if line_val is not None else 0
        except (TypeError, ValueError):
            line = 0
        findings.append({
            "rule_id": rule_id,
            "file": r.get("path", "") or "",
            "line": line,
            "message": (extra.get("message", "") if isinstance(extra, dict) else "") or "",
            "severity": (extra.get("severity", "INFO") if isinstance(extra, dict) else "INFO") or "INFO",
        })

    return findings
