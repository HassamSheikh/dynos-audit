"""
log_surface — AC16.

surface(repo_path, bug_text) auto-detects the project's logging surface,
filters error-level lines, sorts by recency, and returns up to 50 entries.

Detection (presence-based, in order):
    logs/                directory of *.log and *.txt files
    *.log                top-level log files
    pm2 logs             ~/.pm2/logs/ when pm2 is on PATH
    docker compose       docker-compose.yml or compose.yaml
    journald             journalctl on PATH

Each entry dict has keys: source, line_no, timestamp, message, level.

Hard rules:
  * Never raises for missing dirs, unreadable files, or absent tools.
  * Caps at 50 entries.
  * Only emits entries whose level contains "error" (case-insensitive).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

MAX_ENTRIES = 50

# Match an ISO-8601-ish timestamp at the start of the line.
_TS_PAT = re.compile(
    r"^\s*("
    r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}:\d{2}:\d{2}"
    r")"
)
# Match a log level token (ERROR/ERR/SEVERE/CRITICAL/FATAL/WARN/INFO/DEBUG/TRACE).
_LEVEL_PAT = re.compile(
    r"\b(?P<lvl>ERROR|ERR|SEVERE|CRITICAL|FATAL|WARN(?:ING)?|NOTICE|INFO|DEBUG|TRACE)\b",
    re.IGNORECASE,
)


def _is_error_level(level: str) -> bool:
    if not level:
        return False
    lower = level.lower()
    if "error" in lower or "err" == lower:
        return True
    return lower in {"fatal", "critical", "severe"}


def _extract_level(line: str) -> str:
    match = _LEVEL_PAT.search(line)
    if not match:
        return ""
    raw = match.group("lvl").upper()
    # Normalise so callers see strings that contain "error" when appropriate.
    aliases = {
        "ERR": "ERROR",
        "SEVERE": "ERROR",
        "CRITICAL": "ERROR",
        "FATAL": "ERROR",
    }
    return aliases.get(raw, raw)


def _extract_timestamp(line: str) -> str | None:
    m = _TS_PAT.match(line)
    if not m:
        return None
    return m.group(1)


def _safe_iter_lines(path: Path) -> Iterable[tuple[int, str]]:
    """Yield (1-based line_no, line) for a text file; silent on errors."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                yield idx, line.rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return


def _parse_log_file(path: Path) -> list[dict]:
    """Return all error-level entries in path with mtime-derived sort hints."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    out: list[dict] = []
    for line_no, raw_line in _safe_iter_lines(path):
        if not raw_line.strip():
            continue
        level = _extract_level(raw_line)
        if not _is_error_level(level):
            continue
        ts = _extract_timestamp(raw_line)
        message = raw_line.strip()
        out.append(
            {
                "source": str(path),
                "line_no": line_no,
                "timestamp": ts,
                "message": message,
                "level": level or "ERROR",
                # Internal sort key — stripped before returning to the caller.
                "_mtime": mtime,
            }
        )
    return out


def _gather_log_files(root: Path) -> list[Path]:
    """Discover *.log files in root and its logs/ subdirectory."""
    found: list[Path] = []
    try:
        for entry in root.iterdir():
            try:
                if entry.is_file() and entry.suffix == ".log":
                    found.append(entry)
            except OSError:
                continue
    except (OSError, FileNotFoundError):
        pass

    logs_dir = root / "logs"
    try:
        if logs_dir.is_dir():
            for entry in logs_dir.rglob("*"):
                try:
                    if entry.is_file() and entry.suffix in {".log", ".txt"}:
                        found.append(entry)
                except OSError:
                    continue
    except (OSError, FileNotFoundError):
        pass
    return found


def _collect_pm2_logs() -> list[Path]:
    """Look up ~/.pm2/logs when pm2 is on PATH."""
    if not shutil.which("pm2"):
        return []
    home = os.path.expanduser("~")
    pm2_dir = Path(home) / ".pm2" / "logs"
    out: list[Path] = []
    try:
        if pm2_dir.is_dir():
            for entry in pm2_dir.iterdir():
                try:
                    if entry.is_file() and entry.suffix == ".log":
                        out.append(entry)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _collect_docker_compose_logs(root: Path) -> list[dict]:
    """If docker-compose.yml exists and `docker` is on PATH, pull recent logs."""
    if not shutil.which("docker"):
        return []
    compose_files = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    if not any((root / f).is_file() for f in compose_files):
        return []
    try:
        proc = subprocess.run(
            ["docker", "compose", "logs", "--tail", "200", "--no-color"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for idx, raw in enumerate(proc.stdout.splitlines(), start=1):
        if not raw.strip():
            continue
        level = _extract_level(raw)
        if not _is_error_level(level):
            continue
        out.append(
            {
                "source": "docker-compose",
                "line_no": idx,
                "timestamp": _extract_timestamp(raw),
                "message": raw.strip(),
                "level": level,
                "_mtime": 0.0,
            }
        )
    return out


def _collect_journald() -> list[dict]:
    """Pull a small slice of recent priority<=err entries from journalctl."""
    if not shutil.which("journalctl"):
        return []
    try:
        proc = subprocess.run(
            ["journalctl", "-p", "err", "-n", "200", "--no-pager", "-o", "short-iso"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for idx, raw in enumerate(proc.stdout.splitlines(), start=1):
        if not raw.strip():
            continue
        out.append(
            {
                "source": "journald",
                "line_no": idx,
                "timestamp": _extract_timestamp(raw),
                "message": raw.strip(),
                "level": "ERROR",
                "_mtime": 0.0,
            }
        )
    return out


def surface(repo_path: str, bug_text: str) -> list[dict]:
    """Return up to 50 most-recent error-level log entries.

    Args:
        repo_path: Filesystem path to project root.
        bug_text: Free-text bug description used for soft scoring; never causes
            failures when empty or non-string.

    Returns:
        List of entry dicts sorted with most recent first. Empty list when no
        log surface is available. Never raises.
    """
    if not isinstance(repo_path, str) or not repo_path:
        return []

    root = Path(repo_path)
    repo_exists = False
    try:
        repo_exists = root.exists() and root.is_dir()
    except OSError:
        repo_exists = False

    bug_terms: list[str] = []
    if isinstance(bug_text, str) and bug_text.strip():
        bug_terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{4,}", bug_text)]

    entries: list[dict] = []

    if repo_exists:
        for path in _gather_log_files(root):
            entries.extend(_parse_log_file(path))
        # Optional surfaces — best-effort, never block the result.
        try:
            entries.extend(_collect_docker_compose_logs(root))
        except Exception:
            pass

    try:
        for path in _collect_pm2_logs():
            entries.extend(_parse_log_file(path))
    except Exception:
        pass
    try:
        entries.extend(_collect_journald())
    except Exception:
        pass

    # Sort: prefer entries that mention any bug term, then by mtime/line_no.
    def sort_key(entry: dict) -> tuple[int, float, int]:
        msg = entry.get("message", "").lower()
        match_score = 1 if any(term in msg for term in bug_terms) else 0
        line_no = entry.get("line_no") or 0
        return (-match_score, -float(entry.get("_mtime", 0.0)), -int(line_no))

    entries.sort(key=sort_key)

    cleaned: list[dict] = []
    for entry in entries[:MAX_ENTRIES]:
        entry.pop("_mtime", None)
        cleaned.append(entry)
    return cleaned
