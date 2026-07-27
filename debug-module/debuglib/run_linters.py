"""
run_linters — Dispatch language-appropriate linters across a repository.

Public API:
    run(repo_path: str, languages: list[str]) -> list[dict]

Behaviour:
    - For each language in `languages`, dispatch the canonical linter:
        JavaScript / TypeScript -> eslint
        Python                  -> ruff
        Go                      -> golangci-lint
        Rust                    -> cargo clippy
        Java                    -> pmd
        Dart                    -> dart analyze
        Ruby                    -> rubocop
    - Always also run lizard (cyclomatic complexity, language-agnostic).
    - When a linter binary is NOT in PATH, return a skip record:
        {"tool": "<name>", "skipped": True, "reason": "not installed"}
      No exception is raised in that case.
    - Each finding dict has the schema:
        {
          "tool": str,
          "file": str,
          "line": int | None,
          "severity": str,
          "message": str,
        }
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

# ---------------------------------------------------------------------------
# Canonical mapping of language -> linter binary name(s).
# Languages are matched case-insensitively to avoid coupling callers to a
# particular casing.
# ---------------------------------------------------------------------------

_LANGUAGE_LINTERS: dict[str, list[str]] = {
    "javascript": ["eslint"],
    "typescript": ["eslint"],
    "python": ["ruff"],
    "go": ["golangci-lint"],
    "rust": ["cargo-clippy"],  # invoked as `cargo clippy`
    "java": ["pmd"],
    "dart": ["dart"],          # invoked as `dart analyze`
    "ruby": ["rubocop"],
}

# Lizard runs across all languages so it's added unconditionally.
_UNIVERSAL_LINTERS: list[str] = ["lizard"]

# Subprocess timeouts in seconds. Linters can be slow on big repos but we
# don't want to hang the pipeline forever.
_LINTER_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run(repo_path: str, languages: list[str]) -> list[dict]:
    """Dispatch linters appropriate to `languages` against `repo_path`.

    Returns a list of finding dicts and/or skip records. Never raises for
    missing binaries, missing paths, timeouts, or non-zero exit codes from
    the linters themselves — those become structured records in the result.
    """
    if not isinstance(repo_path, str):
        repo_path = str(repo_path)

    if languages is None:
        languages = []

    # De-duplicate the set of linters to actually invoke. A repo with both
    # JavaScript and TypeScript should only run eslint once.
    linters_to_run: list[str] = []
    seen: set[str] = set()
    for lang in languages:
        if not isinstance(lang, str):
            continue
        for linter in _LANGUAGE_LINTERS.get(lang.lower(), []):
            if linter not in seen:
                seen.add(linter)
                linters_to_run.append(linter)

    # Lizard always runs (universal complexity metric).
    for linter in _UNIVERSAL_LINTERS:
        if linter not in seen:
            seen.add(linter)
            linters_to_run.append(linter)

    findings: list[dict] = []
    for linter in linters_to_run:
        try:
            findings.extend(_dispatch_linter(linter, repo_path))
        except Exception as exc:  # defensive: linters must never crash run()
            findings.append({
                "tool": _display_name(linter),
                "skipped": True,
                "reason": f"linter dispatch error: {exc}",
            })
    return findings


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _display_name(binary: str) -> str:
    """Map an internal binary key to the user-facing tool name."""
    if binary == "cargo-clippy":
        return "cargo clippy"
    if binary == "dart":
        return "dart analyze"
    return binary


def _dispatch_linter(binary: str, repo_path: str) -> list[dict]:
    """Run a single linter and normalise its output into finding dicts."""
    # Resolve the actual binary we need to look up in PATH.
    lookup = "cargo" if binary == "cargo-clippy" else binary

    if shutil.which(lookup) is None:
        return [{
            "tool": _display_name(binary),
            "skipped": True,
            "reason": "not installed",
        }]

    runner = _LINTER_RUNNERS.get(binary)
    if runner is None:
        return [{
            "tool": _display_name(binary),
            "skipped": True,
            "reason": "no runner registered",
        }]

    try:
        return runner(repo_path)
    except FileNotFoundError:
        # Race: binary disappeared between which() and exec.
        return [{
            "tool": _display_name(binary),
            "skipped": True,
            "reason": "not installed",
        }]
    except subprocess.TimeoutExpired:
        return [{
            "tool": _display_name(binary),
            "skipped": True,
            "reason": "timeout",
        }]
    except OSError as exc:
        return [{
            "tool": _display_name(binary),
            "skipped": True,
            "reason": f"os error: {exc}",
        }]


def _safe_run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess with a timeout and capture stdout/stderr.

    Linters legitimately exit non-zero when they find issues, so we never
    raise on returncode here.
    """
    return subprocess.run(
        cmd,
        cwd=cwd if cwd and os.path.isdir(cwd) else None,
        capture_output=True,
        text=True,
        timeout=_LINTER_TIMEOUT_SECONDS,
        check=False,
    )


# ---------------------------------------------------------------------------
# Per-linter runners — each returns a list[dict] of findings (or skip recs).
# ---------------------------------------------------------------------------

def _run_eslint(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "eslint", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(["eslint", "--format", "json", "."], cwd=repo_path)
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{
            "tool": "eslint",
            "skipped": True,
            "reason": "invalid json output",
        }]
    findings: list[dict] = []
    if isinstance(data, list):
        for file_entry in data:
            if not isinstance(file_entry, dict):
                continue
            file_path = file_entry.get("filePath", "")
            for msg in file_entry.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                findings.append({
                    "tool": "eslint",
                    "file": file_path,
                    "line": msg.get("line"),
                    "severity": _eslint_severity(msg.get("severity")),
                    "message": msg.get("message", ""),
                })
    return findings


def _eslint_severity(value: Any) -> str:
    if value == 2:
        return "error"
    if value == 1:
        return "warning"
    return "info"


def _run_ruff(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "ruff", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(["ruff", "check", "--output-format", "json", "."], cwd=repo_path)
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{"tool": "ruff", "skipped": True, "reason": "invalid json output"}]
    findings: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            location = item.get("location") or {}
            findings.append({
                "tool": "ruff",
                "file": item.get("filename", ""),
                "line": location.get("row") if isinstance(location, dict) else None,
                "severity": "warning",
                "message": item.get("message", ""),
            })
    return findings


def _run_golangci_lint(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "golangci-lint", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(["golangci-lint", "run", "--out-format", "json", "./..."], cwd=repo_path)
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{
            "tool": "golangci-lint",
            "skipped": True,
            "reason": "invalid json output",
        }]
    findings: list[dict] = []
    issues = data.get("Issues") if isinstance(data, dict) else None
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            pos = issue.get("Pos") or {}
            findings.append({
                "tool": "golangci-lint",
                "file": pos.get("Filename", "") if isinstance(pos, dict) else "",
                "line": pos.get("Line") if isinstance(pos, dict) else None,
                "severity": issue.get("Severity", "warning") or "warning",
                "message": issue.get("Text", ""),
            })
    return findings


def _run_cargo_clippy(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "cargo clippy", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(
        ["cargo", "clippy", "--message-format=json", "--quiet"],
        cwd=repo_path,
    )
    findings: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        spans = message.get("spans") or []
        primary = next(
            (s for s in spans if isinstance(s, dict) and s.get("is_primary")),
            spans[0] if spans else None,
        )
        if not isinstance(primary, dict):
            continue
        findings.append({
            "tool": "cargo clippy",
            "file": primary.get("file_name", ""),
            "line": primary.get("line_start"),
            "severity": message.get("level", "warning") or "warning",
            "message": message.get("message", ""),
        })
    return findings


def _run_pmd(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "pmd", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run([
        "pmd", "check", "-d", repo_path, "-f", "json",
        "-R", "rulesets/java/quickstart.xml",
    ])
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{"tool": "pmd", "skipped": True, "reason": "invalid json output"}]
    findings: list[dict] = []
    files = data.get("files", []) if isinstance(data, dict) else []
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            file_path = file_entry.get("filename", "")
            for v in file_entry.get("violations") or []:
                if not isinstance(v, dict):
                    continue
                findings.append({
                    "tool": "pmd",
                    "file": file_path,
                    "line": v.get("beginline"),
                    "severity": str(v.get("priority", "warning")),
                    "message": v.get("description", ""),
                })
    return findings


def _run_dart_analyze(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "dart analyze", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(["dart", "analyze", "--format=json", "."], cwd=repo_path)
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{
            "tool": "dart analyze",
            "skipped": True,
            "reason": "invalid json output",
        }]
    findings: list[dict] = []
    diagnostics = data.get("diagnostics", []) if isinstance(data, dict) else []
    if isinstance(diagnostics, list):
        for d in diagnostics:
            if not isinstance(d, dict):
                continue
            location = d.get("location") or {}
            range_ = location.get("range") if isinstance(location, dict) else None
            start = range_.get("start") if isinstance(range_, dict) else None
            line = start.get("line") if isinstance(start, dict) else None
            findings.append({
                "tool": "dart analyze",
                "file": location.get("file", "") if isinstance(location, dict) else "",
                "line": line,
                "severity": d.get("severity", "warning") or "warning",
                "message": (d.get("problemMessage")
                            or d.get("message", "")),
            })
    return findings


def _run_rubocop(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "rubocop", "skipped": True, "reason": "repo path not found"}]
    proc = _safe_run(["rubocop", "--format", "json"], cwd=repo_path)
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{"tool": "rubocop", "skipped": True, "reason": "invalid json output"}]
    findings: list[dict] = []
    files = data.get("files", []) if isinstance(data, dict) else []
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            file_path = file_entry.get("path", "")
            for offense in file_entry.get("offenses") or []:
                if not isinstance(offense, dict):
                    continue
                location = offense.get("location") or {}
                findings.append({
                    "tool": "rubocop",
                    "file": file_path,
                    "line": location.get("line") if isinstance(location, dict) else None,
                    "severity": offense.get("severity", "warning") or "warning",
                    "message": offense.get("message", ""),
                })
    return findings


def _run_lizard(repo_path: str) -> list[dict]:
    if not os.path.isdir(repo_path):
        return [{"tool": "lizard", "skipped": True, "reason": "repo path not found"}]
    # Lizard does not have a JSON formatter by default in all versions; we
    # parse its CSV output (-X is XML, --csv is CSV). CSV is the most stable.
    proc = _safe_run(["lizard", "--csv", repo_path])
    findings: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        # Expected columns:
        # nloc, ccn, token, param, length, location, file, function, ...
        if len(parts) < 7:
            continue
        ccn_raw = parts[1]
        try:
            ccn = int(ccn_raw)
        except ValueError:
            # Header row or non-numeric — skip.
            continue
        # Only flag functions whose cyclomatic complexity exceeds the
        # commonly-used threshold of 10.
        if ccn <= 10:
            continue
        file_path = parts[6]
        location = parts[5]
        line_no: int | None = None
        # location format: "function@line-line@file" or similar; take the
        # first integer we can find.
        for token in location.replace("@", " ").replace("-", " ").split():
            if token.isdigit():
                line_no = int(token)
                break
        findings.append({
            "tool": "lizard",
            "file": file_path,
            "line": line_no,
            "severity": "warning",
            "message": f"high cyclomatic complexity ({ccn})",
        })
    return findings


_LINTER_RUNNERS: dict[str, Any] = {
    "eslint": _run_eslint,
    "ruff": _run_ruff,
    "golangci-lint": _run_golangci_lint,
    "cargo-clippy": _run_cargo_clippy,
    "pmd": _run_pmd,
    "dart": _run_dart_analyze,
    "rubocop": _run_rubocop,
    "lizard": _run_lizard,
}
