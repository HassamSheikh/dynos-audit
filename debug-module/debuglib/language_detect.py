"""
language_detect — AC10.

Public API:
    detect(repo_path: str, mentioned_files: list[str]) -> list[str]

Returns a deduplicated list of language names from the allowed set:
    {JavaScript, TypeScript, Python, Go, Rust, Java, Dart, Ruby}

Strategy:
    1. Inspect extensions of `mentioned_files`.
    2. If no languages were detected from mentioned_files, sample files
       under `repo_path` (bounded) and inspect their extensions.

No third-party dependencies, no network.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

# Allowed language names — the closed set the function may return.
ALLOWED_LANGUAGES: tuple[str, ...] = (
    "JavaScript",
    "TypeScript",
    "Python",
    "Go",
    "Rust",
    "Java",
    "Dart",
    "Ruby",
)

# Extension -> language name. Lowercased extension WITHOUT leading dot.
_EXT_TO_LANG: dict[str, str] = {
    "js": "JavaScript",
    "jsx": "JavaScript",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "py": "Python",
    "pyi": "Python",
    "go": "Go",
    "rs": "Rust",
    "java": "Java",
    "dart": "Dart",
    "rb": "Ruby",
}

# Directories we never descend into during fallback scanning.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "dist", "build", "target",
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".idea", ".vscode", ".next", ".nuxt", ".cache",
})

# Bound the repo walk so we never blow up on huge repos.
_MAX_FILES_TO_SCAN = 2000


def detect(repo_path: Any, mentioned_files: Any) -> list[str]:
    """Detect languages used in the repo / mentioned files.

    Args:
        repo_path: filesystem path to the repository root. May be invalid
            or non-existent — function will not raise.
        mentioned_files: iterable of file path strings (paths from the bug
            description). May be None or empty.

    Returns:
        A deduplicated list of language names from ALLOWED_LANGUAGES.
        Returns an empty list if nothing detectable was found.
    """
    repo_str = _coerce_path(repo_path)
    files_iter = _coerce_files(mentioned_files)

    languages: list[str] = []
    seen: set[str] = set()

    # 1. Mentioned files first.
    for fname in files_iter:
        lang = _lang_for_path(fname)
        if lang and lang not in seen:
            seen.add(lang)
            languages.append(lang)

    # 2. Fallback: sample repo files if mentioned files yielded nothing.
    if not languages and repo_str:
        for fname in _walk_repo(repo_str):
            lang = _lang_for_path(fname)
            if lang and lang not in seen:
                seen.add(lang)
                languages.append(lang)

    return languages


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _coerce_path(value: Any) -> str:
    """Coerce an arbitrary value to a path string. None -> ''."""
    if value is None:
        return ""
    if isinstance(value, (str, os.PathLike)):
        return str(value)
    return ""


def _coerce_files(value: Any) -> list[str]:
    """Coerce input to a list of strings. None / non-iterable -> []."""
    if value is None:
        return []
    if isinstance(value, str):
        # Defensive: if a single string was passed, treat it as one path.
        return [value]
    try:
        return [str(item) for item in value if item is not None]
    except TypeError:
        return []


def _lang_for_path(path: str) -> str | None:
    """Return the language name for a path, or None if unknown."""
    if not path:
        return None
    # Use only the file portion to avoid matching dirs like 'node_modules'.
    name = os.path.basename(path)
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return _EXT_TO_LANG.get(ext)


def _walk_repo(repo_path: str) -> Iterable[str]:
    """Yield file paths under `repo_path`, bounded and skipping known dirs.

    Errors (permission denied, missing dir, etc.) are swallowed — this
    function must never raise.
    """
    try:
        root = Path(repo_path)
        if not root.exists() or not root.is_dir():
            return
    except OSError:
        return

    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune skip-dirs in place.
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                           and not d.startswith(".")]

            for fname in filenames:
                yield fname
                count += 1
                if count >= _MAX_FILES_TO_SCAN:
                    return
    except OSError:
        return


__all__ = ["detect", "ALLOWED_LANGUAGES"]
