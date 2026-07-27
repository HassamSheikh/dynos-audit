"""
coverage_gaps — AC12.

find_gaps(repo_path, languages) walks a project root, auto-detects supported
coverage report files by presence, and returns one gap dict per file that has
at least one uncovered line. Supported formats:

    Istanbul         coverage/coverage-final.json
    pytest-cov JSON  coverage.json
    Go cover.out     coverage.out
    Tarpaulin JSON   tarpaulin-report.json
    LCOV             lcov.info
    SimpleCov        .resultset.json

Each gap dict has keys: file, uncovered_lines, coverage_pct, format.

Hard rules:
  * No exception escapes for missing/malformed files — return [] or skip the
    bad format silently.
  * coverage_pct is clamped to [0.0, 100.0].
  * uncovered_lines contains only ``int`` line numbers (1-based).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

ALLOWED_FORMATS = {
    "istanbul",
    "pytest-cov-json",
    "go-cover",
    "tarpaulin",
    "lcov",
    "simplecov",
}


def _safe_read_text(path: Path) -> str | None:
    """Read a text file, returning None on any IO error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def _safe_load_json(path: Path):
    text = _safe_read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _clamp_pct(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return float(value)


# ---------------------------------------------------------------------------
# Istanbul: coverage/coverage-final.json
# ---------------------------------------------------------------------------

def _parse_istanbul(path: Path) -> list[dict]:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return []

    gaps: list[dict] = []
    for file_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        file_name = entry.get("path") if isinstance(entry.get("path"), str) else file_key
        statement_map = entry.get("statementMap") or {}
        s_counts = entry.get("s") or {}
        if not isinstance(statement_map, dict) or not isinstance(s_counts, dict):
            continue

        uncovered_lines: set[int] = set()
        total = 0
        covered = 0
        for sid, count in s_counts.items():
            try:
                count_int = int(count)
            except (TypeError, ValueError):
                continue
            total += 1
            if count_int > 0:
                covered += 1
                continue
            stmt = statement_map.get(sid)
            if not isinstance(stmt, dict):
                continue
            start = stmt.get("start") or {}
            end = stmt.get("end") or {}
            try:
                start_line = int(start.get("line"))
                end_line = int(end.get("line", start_line))
            except (TypeError, ValueError):
                continue
            if end_line < start_line:
                end_line = start_line
            for line_no in range(start_line, end_line + 1):
                uncovered_lines.add(line_no)

        if not uncovered_lines:
            continue

        pct = _clamp_pct((covered / total) * 100.0) if total else 0.0
        gaps.append(
            {
                "file": str(file_name),
                "uncovered_lines": sorted(uncovered_lines),
                "coverage_pct": pct,
                "format": "istanbul",
            }
        )
    return gaps


# ---------------------------------------------------------------------------
# pytest-cov JSON: coverage.json
# ---------------------------------------------------------------------------

def _parse_pytest_cov(path: Path) -> list[dict]:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return []
    files = data.get("files")
    if not isinstance(files, dict):
        return []
    gaps: list[dict] = []
    for file_name, entry in files.items():
        if not isinstance(entry, dict):
            continue
        missing = entry.get("missing_lines") or []
        if not isinstance(missing, list):
            continue
        uncovered = [int(n) for n in missing if isinstance(n, (int, float)) and not isinstance(n, bool)]
        if not uncovered:
            continue
        summary = entry.get("summary") or {}
        pct_raw = summary.get("percent_covered") if isinstance(summary, dict) else None
        try:
            pct = _clamp_pct(float(pct_raw))
        except (TypeError, ValueError):
            pct = 0.0
        gaps.append(
            {
                "file": str(file_name),
                "uncovered_lines": sorted(set(uncovered)),
                "coverage_pct": pct,
                "format": "pytest-cov-json",
            }
        )
    return gaps


# ---------------------------------------------------------------------------
# Go cover.out (line-mode, atomic, count)
# Format: "mode: <mode>" then lines like "path/file.go:S.C,E.C N C"
# Where uncovered when C (count) == 0.
# ---------------------------------------------------------------------------

def _parse_go_cover(path: Path) -> list[dict]:
    text = _safe_read_text(path)
    if not text:
        return []
    by_file: dict[str, dict] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("mode:"):
            continue
        try:
            location, _stmts, count_str = line.rsplit(" ", 2)
            count = int(count_str)
        except ValueError:
            continue
        if ":" not in location:
            continue
        file_name, span = location.split(":", 1)
        if "," not in span:
            continue
        start_part, end_part = span.split(",", 1)
        try:
            start_line = int(start_part.split(".", 1)[0])
            end_line = int(end_part.split(".", 1)[0])
        except (ValueError, IndexError):
            continue
        if end_line < start_line:
            end_line = start_line
        bucket = by_file.setdefault(
            file_name, {"uncovered": set(), "total": 0, "covered": 0}
        )
        bucket["total"] += 1
        if count > 0:
            bucket["covered"] += 1
        else:
            for line_no in range(start_line, end_line + 1):
                bucket["uncovered"].add(line_no)

    gaps: list[dict] = []
    for file_name, bucket in by_file.items():
        if not bucket["uncovered"]:
            continue
        pct = (
            _clamp_pct((bucket["covered"] / bucket["total"]) * 100.0)
            if bucket["total"]
            else 0.0
        )
        gaps.append(
            {
                "file": file_name,
                "uncovered_lines": sorted(bucket["uncovered"]),
                "coverage_pct": pct,
                "format": "go-cover",
            }
        )
    return gaps


# ---------------------------------------------------------------------------
# Tarpaulin (Rust) JSON
# ---------------------------------------------------------------------------

def _parse_tarpaulin(path: Path) -> list[dict]:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return []
    files = data.get("files")
    if not isinstance(files, list):
        return []
    gaps: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path_parts = entry.get("path")
        if isinstance(path_parts, list):
            file_name = "/".join(str(p) for p in path_parts)
        elif isinstance(path_parts, str):
            file_name = path_parts
        else:
            continue
        traces = entry.get("traces") or []
        if not isinstance(traces, list):
            continue
        uncovered: set[int] = set()
        total = 0
        covered = 0
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            try:
                line_no = int(trace.get("line"))
            except (TypeError, ValueError):
                continue
            stats = trace.get("stats") or {}
            try:
                hits = int(stats.get("Line", 0)) if isinstance(stats, dict) else 0
            except (TypeError, ValueError):
                hits = 0
            total += 1
            if hits > 0:
                covered += 1
            else:
                uncovered.add(line_no)
        if not uncovered:
            continue
        pct = _clamp_pct((covered / total) * 100.0) if total else 0.0
        gaps.append(
            {
                "file": file_name,
                "uncovered_lines": sorted(uncovered),
                "coverage_pct": pct,
                "format": "tarpaulin",
            }
        )
    return gaps


# ---------------------------------------------------------------------------
# LCOV (lcov.info)
# Records start with "SF:<file>" and end with "end_of_record".
# Line hits: "DA:<line>,<count>"
# ---------------------------------------------------------------------------

def _parse_lcov(path: Path) -> list[dict]:
    text = _safe_read_text(path)
    if not text:
        return []
    gaps: list[dict] = []
    current_file: str | None = None
    uncovered: set[int] = set()
    total = 0
    covered = 0

    def flush():
        nonlocal current_file, uncovered, total, covered
        if current_file and uncovered:
            pct = _clamp_pct((covered / total) * 100.0) if total else 0.0
            gaps.append(
                {
                    "file": current_file,
                    "uncovered_lines": sorted(uncovered),
                    "coverage_pct": pct,
                    "format": "lcov",
                }
            )
        current_file = None
        uncovered = set()
        total = 0
        covered = 0

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            flush()
            current_file = line[3:]
        elif line.startswith("DA:"):
            payload = line[3:]
            parts = payload.split(",", 2)
            if len(parts) < 2:
                continue
            try:
                line_no = int(parts[0])
                count = int(parts[1])
            except ValueError:
                continue
            total += 1
            if count > 0:
                covered += 1
            else:
                uncovered.add(line_no)
        elif line == "end_of_record":
            flush()
    flush()
    return gaps


# ---------------------------------------------------------------------------
# SimpleCov (Ruby): .resultset.json
# Structure: { "<command>": { "coverage": { "<file>": { "lines": [hits...] } } } }
# Older SimpleCov stored "coverage": { "<file>": [hits...] } directly.
# ---------------------------------------------------------------------------

def _parse_simplecov(path: Path) -> list[dict]:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return []
    gaps: list[dict] = []
    for _command, payload in data.items():
        if not isinstance(payload, dict):
            continue
        coverage = payload.get("coverage")
        if not isinstance(coverage, dict):
            continue
        for file_name, hits_entry in coverage.items():
            if isinstance(hits_entry, dict):
                hits = hits_entry.get("lines")
            else:
                hits = hits_entry
            if not isinstance(hits, list):
                continue
            uncovered: list[int] = []
            total = 0
            covered = 0
            for idx, h in enumerate(hits, start=1):
                if h is None:  # not executable
                    continue
                try:
                    h_int = int(h)
                except (TypeError, ValueError):
                    continue
                total += 1
                if h_int > 0:
                    covered += 1
                else:
                    uncovered.append(idx)
            if not uncovered:
                continue
            pct = _clamp_pct((covered / total) * 100.0) if total else 0.0
            gaps.append(
                {
                    "file": str(file_name),
                    "uncovered_lines": uncovered,
                    "coverage_pct": pct,
                    "format": "simplecov",
                }
            )
    return gaps


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_DETECTORS: list[tuple[str, callable]] = [
    ("coverage/coverage-final.json", _parse_istanbul),
    ("coverage.json", _parse_pytest_cov),
    ("coverage.out", _parse_go_cover),
    ("tarpaulin-report.json", _parse_tarpaulin),
    ("lcov.info", _parse_lcov),
    (".resultset.json", _parse_simplecov),
    ("coverage/.resultset.json", _parse_simplecov),
]


def find_gaps(repo_path: str, languages: Iterable[str]) -> list[dict]:
    """Walk repo_path looking for known coverage reports and return gap dicts.

    Args:
        repo_path: Filesystem path to project root.
        languages: List of detected languages (informational; format is selected
            by file presence rather than language list).

    Returns:
        List of gap dicts. Empty list when no coverage files exist or path is
        invalid. Never raises.
    """
    if not isinstance(repo_path, str) or not repo_path:
        return []
    root = Path(repo_path)
    try:
        if not root.exists() or not root.is_dir():
            return []
    except OSError:
        return []

    # `languages` is currently informational only — kept for forward
    # compatibility and to satisfy the documented signature. Reference it so
    # static analysis does not flag it as unused.
    _ = list(languages) if languages is not None else []

    seen_files: set[Path] = set()
    gaps: list[dict] = []
    for relative, parser in _DETECTORS:
        candidate = root / relative
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        if candidate in seen_files:
            continue
        seen_files.add(candidate)
        try:
            parsed = parser(candidate)
        except Exception:
            # Hard rule: never let a malformed coverage file crash the caller.
            parsed = []
        for gap in parsed:
            if gap.get("format") in ALLOWED_FORMATS:
                gaps.append(gap)
    return gaps
