"""
schema_drift — AC17.

check(repo_path) auto-detects the project's migration framework and reports
applied/pending/missing migrations.

Detection order (first-match wins per framework, but all detected frameworks
are checked):
    Alembic   alembic.ini
    Prisma    prisma/schema.prisma
    Rails     db/schema.rb (Ruby on Rails)
    Django    manage.py

Each entry: framework, migration, status — where status is one of:
    "applied" | "pending" | "missing" | "check-failed"

When the status command fails, exactly one entry is returned for that
framework with status="check-failed" and an "error" key holding stderr.

Hard rules:
  * Never raises for missing dirs, missing tools, or process errors.
  * Returns empty list when no sentinel is present.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_TIMEOUT_SECONDS = 15


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a subprocess with a timeout; never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {_TIMEOUT_SECONDS}s: {exc}"
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except (OSError, ValueError) as exc:
        return 1, "", f"exec failed: {exc}"


def _check_failed(framework: str, error: str) -> dict:
    return {
        "framework": framework,
        "migration": "",
        "status": "check-failed",
        "error": (error or "").strip(),
    }


# ---------------------------------------------------------------------------
# Alembic
# ---------------------------------------------------------------------------

def _check_alembic(root: Path) -> list[dict]:
    if not shutil.which("alembic"):
        return [_check_failed("alembic", "alembic not on PATH")]

    rc_cur, out_cur, err_cur = _run(["alembic", "current"], root)
    if rc_cur != 0:
        return [_check_failed("alembic", err_cur or out_cur)]
    current_rev = ""
    for line in out_cur.splitlines():
        token = line.strip().split(" ", 1)[0]
        if token and token.lower() != "info":
            current_rev = token
            break

    rc_hist, out_hist, err_hist = _run(["alembic", "history"], root)
    if rc_hist != 0:
        return [_check_failed("alembic", err_hist or out_hist)]

    revisions: list[str] = []
    for line in out_hist.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Alembic history lines look like:
        #   <down_rev> -> <rev> (head), description
        #   <base> -> <rev>, description
        if "->" in line:
            rhs = line.split("->", 1)[1].strip()
            rev = rhs.split(",", 1)[0].strip().split(" ", 1)[0]
            if rev and rev not in revisions:
                revisions.append(rev)

    results: list[dict] = []
    seen_current = False
    for rev in revisions:
        if rev == current_rev:
            results.append({"framework": "alembic", "migration": rev, "status": "applied"})
            seen_current = True
        elif not seen_current and current_rev:
            # Older heads before current_rev are applied.
            results.append({"framework": "alembic", "migration": rev, "status": "applied"})
        else:
            results.append({"framework": "alembic", "migration": rev, "status": "pending"})

    if not results:
        # No revisions found but command succeeded — return a single applied
        # marker so callers see the framework was checked.
        results.append(
            {"framework": "alembic", "migration": current_rev or "head", "status": "applied"}
        )
    return results


# ---------------------------------------------------------------------------
# Prisma
# ---------------------------------------------------------------------------

def _check_prisma(root: Path) -> list[dict]:
    npx = shutil.which("npx")
    if not npx:
        return [_check_failed("prisma", "npx not on PATH")]
    rc, out, err = _run([npx, "--no", "prisma", "migrate", "status"], root)
    text = out + "\n" + err
    if rc not in (0, 1):
        # rc 1 is used by prisma when drift is detected — still parseable.
        return [_check_failed("prisma", err or out)]

    results: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("- ") or lower.startswith("• "):
            name = line[2:].strip()
        elif "migration_lock" in lower:
            continue
        else:
            continue
        if "pending" in lower or "not yet been applied" in lower:
            status = "pending"
        elif "missing" in lower or "failed" in lower:
            status = "missing"
        else:
            status = "applied"
        results.append({"framework": "prisma", "migration": name, "status": status})

    # Fall back to a summary line if nothing was extracted.
    if not results:
        if "database schema is up to date" in text.lower():
            results.append({"framework": "prisma", "migration": "head", "status": "applied"})
        elif "following migration" in text.lower() and "pending" in text.lower():
            results.append({"framework": "prisma", "migration": "unknown", "status": "pending"})
        else:
            results.append({"framework": "prisma", "migration": "head", "status": "applied"})
    return results


# ---------------------------------------------------------------------------
# Rails
# ---------------------------------------------------------------------------

def _check_rails(root: Path) -> list[dict]:
    bin_rails = root / "bin" / "rails"
    if bin_rails.is_file():
        cmd = [str(bin_rails), "db:migrate:status"]
    elif shutil.which("rails"):
        cmd = ["rails", "db:migrate:status"]
    else:
        return [_check_failed("rails", "rails not on PATH")]

    rc, out, err = _run(cmd, root)
    if rc != 0:
        return [_check_failed("rails", err or out)]

    results: list[dict] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.startswith("Status") or line.startswith("---") or line.startswith("database:"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        status_word, version = parts[0].lower(), parts[1]
        name = parts[2] if len(parts) > 2 else version
        if status_word == "up":
            status = "applied"
        elif status_word == "down":
            status = "pending"
        else:
            continue
        results.append(
            {"framework": "rails", "migration": f"{version} {name}".strip(), "status": status}
        )
    if not results:
        results.append({"framework": "rails", "migration": "head", "status": "applied"})
    return results


# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------

def _check_django(root: Path) -> list[dict]:
    python = shutil.which("python") or shutil.which("python3")
    if not python:
        return [_check_failed("django", "python not on PATH")]
    rc, out, err = _run([python, "manage.py", "showmigrations", "--plan"], root)
    if rc != 0:
        return [_check_failed("django", err or out)]

    results: list[dict] = []
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Plan output: "[X] app.0001_initial" or "[ ] app.0002_next"
        if "[" in line and "]" in line:
            marker_end = line.index("]")
            marker = line[: marker_end + 1].strip()
            name = line[marker_end + 1 :].strip()
            if not name:
                continue
            if "X" in marker.upper():
                status = "applied"
            else:
                status = "pending"
            results.append({"framework": "django", "migration": name, "status": status})
    if not results:
        results.append({"framework": "django", "migration": "head", "status": "applied"})
    return results


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_DETECTORS: list[tuple[str, str, callable]] = [
    ("alembic", "alembic.ini", _check_alembic),
    ("prisma", "prisma/schema.prisma", _check_prisma),
    ("rails", "db/schema.rb", _check_rails),
    ("django", "manage.py", _check_django),
]


def check(repo_path: str) -> list[dict]:
    """Detect migration framework(s) and return drift dicts.

    Args:
        repo_path: Filesystem path to project root.

    Returns:
        List of drift dicts. Empty when no sentinel files are present or path
        is invalid. Never raises.
    """
    if not isinstance(repo_path, str) or not repo_path:
        return []
    root = Path(repo_path)
    try:
        if not root.exists() or not root.is_dir():
            return []
    except OSError:
        return []

    results: list[dict] = []
    for name, sentinel, runner in _DETECTORS:
        candidate = root / sentinel
        try:
            if not candidate.exists():
                continue
        except OSError:
            continue
        try:
            runner_results = runner(root)
        except Exception as exc:
            runner_results = [_check_failed(name, str(exc))]
        for item in runner_results:
            # Defensive normalisation — guarantee shape regardless of branch.
            if not isinstance(item, dict):
                continue
            item.setdefault("framework", name)
            item.setdefault("migration", "")
            status = item.get("status")
            if status not in {"applied", "pending", "missing", "check-failed"}:
                item["status"] = "check-failed"
                item.setdefault("error", f"unknown status: {status!r}")
            results.append(item)
    return results
