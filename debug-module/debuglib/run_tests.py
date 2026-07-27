"""
run_tests — library module for running per-language test suites.

NOT wired into triage.py default pipeline. Callers may import and dispatch
manually.

Per-framework dispatch:
    Python  → pytest
    JS/TS   → jest, vitest (whichever is available; both can run when present)
    Go      → go test
    Rust    → cargo test
    Java    → mvn test
    Dart    → dart test
    Ruby    → rspec

Each result dict has keys:
    tool     str — name of the test runner
    passed   int — number of passed tests (best-effort parse)
    failed   int — number of failed tests
    output   str — stdout+stderr or skip-record explanation

When a tool is not installed, a skip record is emitted instead of running:
    {"tool": "<name>", "passed": 0, "failed": 0, "output": "skipped: not installed"}

Hard rules:
  * Never raises. All subprocess errors are captured into the output field.
  * Subprocess timeouts are bounded to keep triage runs interactive.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

# A generous but bounded timeout — test suites can be slow but we cannot block
# the triage pipeline indefinitely.
_TIMEOUT_SECONDS = 600


def _normalize_languages(languages: Iterable[str]) -> set[str]:
    """Return a lower-cased set of detected language identifiers."""
    if not languages:
        return set()
    out: set[str] = set()
    for lang in languages:
        if not isinstance(lang, str):
            continue
        out.add(lang.strip().lower())
    return out


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Execute a command with a bounded timeout. Never raises."""
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


def _skip_record(tool: str, reason: str) -> dict:
    return {
        "tool": tool,
        "passed": 0,
        "failed": 0,
        "output": f"skipped: {reason}",
    }


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_PYTEST_SUMMARY = re.compile(
    r"(?P<num>\d+)\s+(?P<word>passed|failed|errors|error|skipped)",
    re.IGNORECASE,
)
_JEST_PASS = re.compile(r"Tests:.*?(\d+)\s+passed", re.IGNORECASE | re.DOTALL)
_JEST_FAIL = re.compile(r"Tests:.*?(\d+)\s+failed", re.IGNORECASE | re.DOTALL)
_GO_PASS = re.compile(r"^---\s+PASS:", re.MULTILINE)
_GO_FAIL = re.compile(r"^---\s+FAIL:", re.MULTILINE)
_CARGO_SUMMARY = re.compile(
    r"test result:.*?(\d+)\s+passed.*?(\d+)\s+failed",
    re.IGNORECASE | re.DOTALL,
)
_MAVEN_SUMMARY = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)",
    re.IGNORECASE,
)
# Dart summary lines look like: "+N: desc", "+N -M: desc", or "HH:MM +N: desc".
# +N must appear at line-start (with optional whitespace) or after a time token
# (digits:digits whitespace). -N must appear at line-start or after a +digits token.
_DART_PASS = re.compile(
    r"(?:(?:^[ \t]*)|(?:\d+:\d+[ \t]))\+(\d+)(?=[ \t:]|$)",
    re.MULTILINE,
)
_DART_FAIL = re.compile(
    r"(?:(?:^[ \t]*)|(?:\+\d+[ \t]))-(\d+)(?=[ \t:]|$)",
    re.MULTILINE,
)
_RSPEC_SUMMARY = re.compile(
    r"(\d+)\s+examples?,\s*(\d+)\s+failures?",
    re.IGNORECASE,
)


def _parse_pytest(output: str) -> tuple[int, int]:
    passed = failed = 0
    for m in _PYTEST_SUMMARY.finditer(output):
        try:
            n = int(m.group("num"))
        except ValueError:
            continue
        word = m.group("word").lower()
        if word == "passed":
            passed = max(passed, n)
        elif word in {"failed", "error", "errors"}:
            failed += n
    return passed, failed


def _parse_jest(output: str) -> tuple[int, int]:
    p = _JEST_PASS.search(output)
    f = _JEST_FAIL.search(output)
    return (int(p.group(1)) if p else 0, int(f.group(1)) if f else 0)


def _parse_go(output: str) -> tuple[int, int]:
    return (len(_GO_PASS.findall(output)), len(_GO_FAIL.findall(output)))


def _parse_cargo(output: str) -> tuple[int, int]:
    m = _CARGO_SUMMARY.search(output)
    if not m:
        return 0, 0
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return 0, 0


def _parse_maven(output: str) -> tuple[int, int]:
    total = failed = errors = 0
    for m in _MAVEN_SUMMARY.finditer(output):
        try:
            total = max(total, int(m.group(1)))
            failed += int(m.group(2))
            errors += int(m.group(3))
        except ValueError:
            continue
    fails = failed + errors
    passed = max(total - fails, 0)
    return passed, fails


def _parse_dart(output: str) -> tuple[int, int]:
    passed = failed = 0
    for m in _DART_PASS.finditer(output):
        try:
            passed = max(passed, int(m.group(1)))
        except ValueError:
            continue
    for m in _DART_FAIL.finditer(output):
        try:
            failed = max(failed, int(m.group(1)))
        except ValueError:
            continue
    return passed, failed


def _parse_rspec(output: str) -> tuple[int, int]:
    m = _RSPEC_SUMMARY.search(output)
    if not m:
        return 0, 0
    try:
        total = int(m.group(1))
        fails = int(m.group(2))
        return max(total - fails, 0), fails
    except ValueError:
        return 0, 0


# ---------------------------------------------------------------------------
# Per-framework runners
# ---------------------------------------------------------------------------

def _has_any(root: Path, names: Iterable[str]) -> bool:
    for n in names:
        if (root / n).exists():
            return True
    return False


def _run_pytest(root: Path) -> dict | None:
    if not _has_any(root, ["pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "tests", "test"]):
        return None
    if not shutil.which("pytest"):
        return _skip_record("pytest", "not installed")
    rc, out, err = _run(["pytest", "-q", "--no-header"], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_pytest(output)
    return {"tool": "pytest", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_jest(root: Path) -> dict | None:
    if not (root / "package.json").is_file():
        return None
    try:
        pkg_text = (root / "package.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "jest" not in pkg_text.lower():
        return None
    if not shutil.which("npx"):
        return _skip_record("jest", "npx not installed")
    rc, out, err = _run(["npx", "--no", "jest", "--ci", "--reporters=default"], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_jest(output)
    return {"tool": "jest", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_vitest(root: Path) -> dict | None:
    if not (root / "package.json").is_file():
        return None
    try:
        pkg_text = (root / "package.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "vitest" not in pkg_text.lower():
        return None
    if not shutil.which("npx"):
        return _skip_record("vitest", "npx not installed")
    rc, out, err = _run(["npx", "--no", "vitest", "run"], root)
    output = (out + "\n" + err).strip()
    # Vitest prints similar summary to jest.
    passed, failed = _parse_jest(output)
    return {"tool": "vitest", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_go(root: Path) -> dict | None:
    if not (root / "go.mod").is_file():
        return None
    if not shutil.which("go"):
        return _skip_record("go test", "not installed")
    rc, out, err = _run(["go", "test", "./..."], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_go(output)
    return {"tool": "go test", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_cargo(root: Path) -> dict | None:
    if not (root / "Cargo.toml").is_file():
        return None
    if not shutil.which("cargo"):
        return _skip_record("cargo test", "not installed")
    rc, out, err = _run(["cargo", "test", "--quiet"], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_cargo(output)
    return {"tool": "cargo test", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_maven(root: Path) -> dict | None:
    if not (root / "pom.xml").is_file():
        return None
    if not shutil.which("mvn"):
        return _skip_record("mvn test", "not installed")
    rc, out, err = _run(["mvn", "-q", "test"], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_maven(output)
    return {"tool": "mvn test", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_dart(root: Path) -> dict | None:
    if not (root / "pubspec.yaml").is_file():
        return None
    if not shutil.which("dart"):
        return _skip_record("dart test", "not installed")
    rc, out, err = _run(["dart", "test"], root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_dart(output)
    return {"tool": "dart test", "passed": passed, "failed": failed, "output": output, "_rc": rc}


def _run_rspec(root: Path) -> dict | None:
    if not _has_any(root, ["Gemfile", "spec"]):
        return None
    if not shutil.which("rspec") and not shutil.which("bundle"):
        return _skip_record("rspec", "not installed")
    cmd = ["rspec"] if shutil.which("rspec") else ["bundle", "exec", "rspec"]
    rc, out, err = _run(cmd, root)
    output = (out + "\n" + err).strip()
    passed, failed = _parse_rspec(output)
    return {"tool": "rspec", "passed": passed, "failed": failed, "output": output, "_rc": rc}


# Mapping from normalised language -> ordered runner list.
_LANG_DISPATCH: dict[str, list[callable]] = {
    "python": [_run_pytest],
    "javascript": [_run_jest, _run_vitest],
    "typescript": [_run_jest, _run_vitest],
    "js": [_run_jest, _run_vitest],
    "ts": [_run_jest, _run_vitest],
    "go": [_run_go],
    "rust": [_run_cargo],
    "java": [_run_maven],
    "kotlin": [_run_maven],
    "dart": [_run_dart],
    "ruby": [_run_rspec],
}


def run(repo_path: str, languages: Iterable[str]) -> list[dict]:
    """Dispatch test runners per language and collect results.

    Args:
        repo_path: Filesystem path to project root.
        languages: List of detected language identifiers (case-insensitive).

    Returns:
        List of result dicts, one per attempted runner. Never raises.
    """
    if not isinstance(repo_path, str) or not repo_path:
        return []
    root = Path(repo_path)
    try:
        if not root.exists() or not root.is_dir():
            return []
    except OSError:
        return []

    lang_set = _normalize_languages(languages)
    runners: list[callable] = []
    for lang in lang_set:
        for fn in _LANG_DISPATCH.get(lang, []):
            if fn not in runners:
                runners.append(fn)

    results: list[dict] = []
    for fn in runners:
        try:
            result = fn(root)
        except Exception as exc:
            result = {
                "tool": getattr(fn, "__name__", "unknown"),
                "passed": 0,
                "failed": 0,
                "output": f"runner crashed: {exc}",
            }
        if result is None:
            continue
        # Strip internal fields like _rc before returning.
        result.pop("_rc", None)
        results.append(result)
    return results
