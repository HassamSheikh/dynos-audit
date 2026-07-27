"""
git_forensics — AC15.

Deterministic git-history forensics for the debug-module triage pipeline.

Public API:
    analyze(repo_path: str, files: list[str], since: str | None) -> dict

Returns a dict with keys:
    - blame_ranges: dict[str, list[dict]]  # file -> blame line records
    - recent_commits: list[dict]           # commit records
    - co_change_pairs: list[dict]          # files that co-changed >= 2 times

Failure mode: NEVER raises. On missing git, missing repo, or any subprocess
error, returns the same shape with an extra "error" key (non-empty string).
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from itertools import combinations
from typing import Any

# Hard caps to keep the function bounded on huge repos / huge commits.
_DEFAULT_COMMIT_LIMIT = 30
_MAX_COMMITS = 5000
_MAX_BLAME_LINES_PER_FILE = 5000
_SUBPROCESS_TIMEOUT_SECONDS = 60
_CO_CHANGE_MIN_COUNT = 2
# A commit touching this many files is treated as a bulk/refactor commit and
# excluded from co-change pairing to avoid quadratic explosion.
_CO_CHANGE_MAX_FILES_PER_COMMIT = 100


def _empty_result(error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "blame_ranges": {},
        "recent_commits": [],
        "co_change_pairs": [],
    }
    if error:
        out["error"] = error
    return out


def _run_git(
    repo_path: str, args: list[str]
) -> tuple[bool, str, str]:
    """
    Run a git command. Returns (ok, stdout, stderr).

    Catches FileNotFoundError (git missing), TimeoutExpired, OSError, and any
    other subprocess error. Never raises.
    """
    cmd = ["git", "-C", repo_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return False, "", f"git not available: {exc}"
    except subprocess.TimeoutExpired as exc:
        return False, "", f"git command timed out: {exc}"
    except OSError as exc:
        return False, "", f"git invocation failed: {exc}"
    except Exception as exc:  # defensive: never propagate
        return False, "", f"git unexpected error: {exc}"

    if proc.returncode != 0:
        return False, proc.stdout or "", (proc.stderr or "").strip() or f"git exited {proc.returncode}"
    return True, proc.stdout or "", proc.stderr or ""


def _is_git_repo(repo_path: str) -> tuple[bool, str]:
    """Return (is_repo, error_message_if_not)."""
    if not isinstance(repo_path, str) or not repo_path:
        return False, "repo_path must be a non-empty string"
    if not os.path.isdir(repo_path):
        return False, f"repo_path is not a directory: {repo_path}"
    ok, stdout, stderr = _run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    if not ok:
        return False, stderr or "not a git repository"
    if stdout.strip() != "true":
        return False, "not a git repository"
    return True, ""


def _file_line_count(path: str) -> int | None:
    """Count lines in a text file. Returns None on any failure."""
    try:
        with open(path, "rb") as fh:
            count = 0
            for _ in fh:
                count += 1
            return count
    except (OSError, UnicodeDecodeError):
        return None


def _blame_file(
    repo_path: str, file_rel: str
) -> list[dict[str, Any]]:
    """
    Run `git blame -L 1,N --line-porcelain` for one file, return blame records.
    Returns [] on any error (including binary file, file missing in HEAD, etc.).
    """
    if not isinstance(file_rel, str) or not file_rel:
        return []

    abs_path = file_rel
    if not os.path.isabs(abs_path):
        abs_path = os.path.join(repo_path, file_rel)

    line_count = _file_line_count(abs_path)
    if line_count is None or line_count <= 0:
        # Try blame without -L bounds; git can still handle it.
        line_range = None
    else:
        end = min(line_count, _MAX_BLAME_LINES_PER_FILE)
        line_range = f"1,{end}"

    args = ["blame", "--line-porcelain"]
    if line_range:
        args.extend(["-L", line_range])
    args.extend(["--", file_rel])

    ok, stdout, _ = _run_git(repo_path, args)
    if not ok or not stdout:
        return []

    return _parse_blame_porcelain(stdout)


def _parse_blame_porcelain(text: str) -> list[dict[str, Any]]:
    """
    Parse `git blame --line-porcelain` output.

    Each blame entry begins with a header line:
        <40-char-sha> <orig_line> <final_line> [<group_size>]
    followed by metadata lines (author, author-time, ...) and a tab-prefixed
    content line. We extract: commit, author, date (ISO-ish from author-time),
    line_no (final line number).
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    in_entry = False

    for raw_line in text.splitlines():
        if not in_entry:
            parts = raw_line.split(" ")
            # Header: 40-hex sha + ints.
            if (
                len(parts) >= 3
                and len(parts[0]) == 40
                and all(c in "0123456789abcdef" for c in parts[0])
            ):
                try:
                    final_line = int(parts[2])
                except ValueError:
                    continue
                cur = {
                    "commit": parts[0],
                    "author": "",
                    "date": "",
                    "line_no": final_line,
                }
                in_entry = True
            continue

        # Inside an entry, until we hit the tab-prefixed content line.
        if raw_line.startswith("\t"):
            if cur:
                out.append(cur)
            cur = {}
            in_entry = False
            continue

        if raw_line.startswith("author "):
            cur["author"] = raw_line[len("author "):].strip()
        elif raw_line.startswith("author-time "):
            cur["_author_time"] = raw_line[len("author-time "):].strip()
        elif raw_line.startswith("author-tz "):
            cur["_author_tz"] = raw_line[len("author-tz "):].strip()
        elif raw_line.startswith("committer-time ") and not cur.get("_author_time"):
            cur["_author_time"] = raw_line[len("committer-time "):].strip()

    # Compose date from author-time epoch + tz when present.
    finalized: list[dict[str, Any]] = []
    for entry in out:
        epoch = entry.pop("_author_time", "")
        tz = entry.pop("_author_tz", "")
        date_str = ""
        if epoch:
            try:
                from datetime import datetime, timezone, timedelta

                ts = int(epoch)
                offset_minutes = 0
                if tz and len(tz) >= 5 and tz[0] in "+-":
                    sign = 1 if tz[0] == "+" else -1
                    try:
                        hh = int(tz[1:3])
                        mm = int(tz[3:5])
                        offset_minutes = sign * (hh * 60 + mm)
                    except ValueError:
                        offset_minutes = 0
                tzinfo = timezone(timedelta(minutes=offset_minutes))
                date_str = datetime.fromtimestamp(ts, tz=tzinfo).isoformat()
            except (ValueError, OverflowError, OSError):
                date_str = epoch
        entry["date"] = date_str
        finalized.append(entry)

    return finalized


def _collect_recent_commits(
    repo_path: str, since: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Collect recent commits with file lists using `git log -z --name-only`.

    Output structure with `-z`:
        <header>NUL\n<file1>NUL<file2>NUL...<header2>NUL\n<fileA>NUL...
    where header is our pipe-delimited SHA|MSG|AUTHOR|DATE.
    For commits with no files (root commit, empty merges), the trailing "\n"
    may be missing — handle that defensively.

    When since is None, takes the last _DEFAULT_COMMIT_LIMIT commits.
    When since looks like a revision (contains ~, ^, .., or HEAD-ish),
    passes it as a `<since>..HEAD` revision range.
    Otherwise, passes it to `--since=<date>`.

    Returns (commits, error_or_None). On error returns ([], err).
    """
    field_sep = "\x1f"  # unit separator — unlikely in commit metadata
    fmt = f"%H{field_sep}%s{field_sep}%an{field_sep}%ai"

    args = ["log", "-z", f"--format={fmt}", "--name-only"]

    if since is None or since == "":
        args.append(f"-n{_DEFAULT_COMMIT_LIMIT}")
    else:
        if (
            ".." in since
            or "~" in since
            or "^" in since
            or since.upper() == "HEAD"
        ):
            args.append(f"{since}..HEAD")
        else:
            args.extend([f"--since={since}", f"-n{_MAX_COMMITS}"])

    ok, stdout, stderr = _run_git(repo_path, args)
    if not ok:
        return [], stderr or "git log failed"

    commits: list[dict[str, Any]] = []
    if not stdout:
        return [], None

    # Split entire stream on NUL. Tokens alternate between headers (the first
    # token, and any token that begins with a "\n" because the previous file
    # list ended) and file paths. We detect headers by the presence of three
    # field-separators.
    tokens = stdout.split("\x00")
    current: dict[str, Any] | None = None

    for tok in tokens:
        # A header may be prefixed by "\n" because git emits "\n" between the
        # previous commit's file list and the next commit's header.
        candidate = tok.lstrip("\n")
        if not candidate:
            # Empty token (trailing NUL or blank). If we have a current commit
            # and the original token had a leading "\n", that just terminates
            # the previous file list — nothing to do here.
            continue

        if candidate.count(field_sep) >= 3:
            # New commit header. Flush any prior commit.
            if current is not None:
                commits.append(current)
                if len(commits) >= _MAX_COMMITS:
                    current = None
                    break
            parts = candidate.split(field_sep)
            sha = parts[0]
            message = parts[1]
            author = parts[2]
            date = parts[3] if len(parts) > 3 else ""
            current = {
                "sha": sha,
                "message": message,
                "author": author,
                "date": date,
                "files_changed": [],
            }
        else:
            # File path belonging to the current commit.
            if current is not None and candidate:
                # Strip any stray leading newline that wasn't part of the path.
                path = candidate.lstrip("\n").rstrip()
                if path:
                    current["files_changed"].append(path)

    if current is not None and len(commits) < _MAX_COMMITS:
        commits.append(current)

    return commits, None


def _co_change_pairs(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Count file pairs that changed together in the same commit, return pairs
    with count >= _CO_CHANGE_MIN_COUNT. Pairs are ordered (file_a < file_b)
    for determinism. Bulk commits exceeding _CO_CHANGE_MAX_FILES_PER_COMMIT
    are skipped to bound runtime.
    """
    counter: Counter[tuple[str, str]] = Counter()
    for commit in commits:
        files = commit.get("files_changed") or []
        if not isinstance(files, list):
            continue
        # Dedup within a single commit, then sort for determinism.
        unique = sorted({f for f in files if isinstance(f, str) and f})
        if len(unique) < 2 or len(unique) > _CO_CHANGE_MAX_FILES_PER_COMMIT:
            continue
        for a, b in combinations(unique, 2):
            counter[(a, b)] += 1

    pairs: list[dict[str, Any]] = []
    for (a, b), count in counter.items():
        if count >= _CO_CHANGE_MIN_COUNT:
            pairs.append({"file_a": a, "file_b": b, "count": count})

    # Stable, deterministic ordering: count desc, then file_a, then file_b.
    pairs.sort(key=lambda p: (-p["count"], p["file_a"], p["file_b"]))
    return pairs


def analyze(
    repo_path: str, files: list[str], since: str | None
) -> dict[str, Any]:
    """
    Run git forensics for a repo.

    Parameters
    ----------
    repo_path : str
        Absolute or relative path to a git working tree.
    files : list[str]
        Optional list of file paths (relative to repo) to blame. Empty list
        means no blame is performed (only commits + co-change).
    since : str | None
        A git --since date string (e.g. "2 weeks ago") or a revision ref
        (e.g. "HEAD~10"). When None, defaults to the last 30 commits.

    Returns
    -------
    dict with keys: blame_ranges, recent_commits, co_change_pairs.
    On any failure adds an "error" key. Never raises.
    """
    # Validate repo_path early.
    is_repo, repo_err = _is_git_repo(repo_path)
    if not is_repo:
        return _empty_result(error=repo_err)

    # Defensive: coerce 'files' to a list of strings.
    if files is None:
        files = []
    elif not isinstance(files, list):
        return _empty_result(error="files must be a list of strings")

    # Validate 'since' type.
    if since is not None and not isinstance(since, str):
        return _empty_result(error="since must be a string or None")

    # 1) Recent commits + their file lists.
    commits, log_err = _collect_recent_commits(repo_path, since)

    # 2) Blame for requested files (best-effort).
    blame_ranges: dict[str, list[dict[str, Any]]] = {}
    blame_errors: list[str] = []
    for f in files:
        if not isinstance(f, str) or not f:
            continue
        try:
            blame_ranges[f] = _blame_file(repo_path, f)
        except Exception as exc:  # defensive: never propagate
            blame_errors.append(f"blame {f}: {exc}")
            blame_ranges[f] = []

    # 3) Co-change pairs from the commits we collected.
    try:
        pairs = _co_change_pairs(commits)
    except Exception as exc:  # defensive
        pairs = []
        log_err = (log_err or "") + f"; co-change failed: {exc}"

    result: dict[str, Any] = {
        "blame_ranges": blame_ranges,
        "recent_commits": commits,
        "co_change_pairs": pairs,
    }

    error_parts: list[str] = []
    if log_err:
        error_parts.append(log_err)
    if blame_errors:
        error_parts.extend(blame_errors)
    if error_parts:
        result["error"] = "; ".join(error_parts)

    return result
