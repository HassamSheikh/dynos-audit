"""AC-1: Guard test — zero vendor model literals outside lib_models.py.

Scans all .py files under hooks/ and memory/, excluding:
  - hooks/lib_models.py (the designated module)
  - files matching **/test_*.py or under a tests/ directory
  - files under .dynos/

Lines ending with `# noqa: model-literal` are exempt.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

# The one module allowed to contain vendor literals.
EXEMPT_FILE = HOOKS_DIR / "lib_models.py"

# Regex that detects vendor model literals.
_LITERAL_RE = re.compile(r"\b(haiku|sonnet|opus|fable)\b")

# Lines ending with this comment are exempt from the scan.
_NOQA_SUFFIX = "# noqa: model-literal"


def _collect_py_files() -> list[Path]:
    """Return all .py files under hooks/ and memory/ that are in scope."""
    candidates: list[Path] = []
    for search_root in (HOOKS_DIR, MEMORY_DIR):
        for path in sorted(search_root.rglob("*.py")):
            # Skip the exempt designated module.
            if path.resolve() == EXEMPT_FILE.resolve():
                continue
            # Skip test files.
            if path.name.startswith("test_"):
                continue
            # Skip anything under a tests/ directory.
            if "tests" in path.parts:
                continue
            # Skip anything under .dynos/.
            if ".dynos" in path.parts:
                continue
            candidates.append(path)
    return candidates


def test_zero_vendor_literals_outside_lib_models() -> None:
    """AC-1: regex \\b(haiku|sonnet|opus)\\b must find zero matches
    in hooks/ and memory/ Python files (excluding lib_models.py, test files,
    and .dynos/), except on lines ending with '# noqa: model-literal'.
    """
    scanned_files = _collect_py_files()
    violations: list[str] = []

    for path in scanned_files:
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.rstrip()
            if stripped.endswith(_NOQA_SUFFIX):
                continue
            if _LITERAL_RE.search(stripped):
                rel = path.relative_to(ROOT)
                violations.append(f"{rel}:{lineno}: {stripped.strip()}")

    file_count = len(scanned_files)
    print(f"\nScanned {file_count} file(s) for vendor model literals.")

    if violations:
        violation_report = "\n".join(violations)
        pytest.fail(
            f"{len(violations)} literal violation(s) found:\n{violation_report}\n"
            f"Move all vendor model literals to hooks/lib_models.py or "
            f"add '# noqa: model-literal' to exempt a line."
        )

    print("0 literal violations found.")
