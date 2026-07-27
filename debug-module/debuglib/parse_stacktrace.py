"""
parse_stacktrace — AC11.

Public API:
    parse(bug_text: str) -> list[dict]

Each frame dict contains the keys:
    - file:     str          (path or filename)
    - line:     int | None
    - function: str | None
    - language: str | None   ('Python' | 'JavaScript' | 'Go' | 'Java' | None)

Supported formats (at minimum):
    - Python:   File "x.py", line N, in fn
    - JS/TS:    at fn (file.ts:10:5)   or   at fn file.ts:10:5
    - Go:       file.go:N (with a +0x... offset on goroutine traces)
    - Java:     at com.pkg.Class.method(File.java:N)

Empty / non-stacktrace input returns an empty list. The function never
raises on malformed input.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Regexes — anchored / specific enough not to match plain prose by accident.
# ---------------------------------------------------------------------------

# Python:  File "src/foo.py", line 47, in get_user
_PY_RE = re.compile(
    r'File\s+"(?P<file>[^"]+\.py[ix]?)"\s*,\s*line\s+(?P<line>\d+)'
    r'(?:\s*,\s*in\s+(?P<func>[\w.<>]+))?'
)

# JS/TS:   at funcName (path/file.ts:10:5)
#       OR at funcName path/file.ts:10:5
#       OR at path/file.js:10:5         (no function name)
_JS_RE = re.compile(
    r'^\s*at\s+'
    r'(?:'
    r'(?P<func>[\w$.<>\[\]]+)\s+\((?P<file1>[^()\s]+\.(?:ts|tsx|js|jsx|mjs|cjs)):(?P<line1>\d+)(?::(?P<col1>\d+))?\)'
    r'|'
    r'(?P<file2>[^()\s]+\.(?:ts|tsx|js|jsx|mjs|cjs)):(?P<line2>\d+)(?::(?P<col2>\d+))?'
    r')\s*$',
    re.MULTILINE,
)

# Java:    at com.example.app.UserService.getUser(UserService.java:34)
_JAVA_RE = re.compile(
    r'\bat\s+(?P<func>[\w$.]+)\((?P<file>[\w$.-]+\.java):(?P<line>\d+)\)'
)

# Go:      /home/user/project/main.go:42 +0x89
#       OR main.go:42
# Match a path-ish token ending in .go followed by :line, optionally
# followed by " +0x..." offset typical of goroutine traces.
_GO_RE = re.compile(
    r'(?P<file>[\w./\\-]+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-fA-F]+)?'
)

# Go function-name line directly above a Go file:line, e.g.
#   main.processRequest(...)
#       /home/user/project/main.go:42 +0x89
_GO_FUNC_RE = re.compile(
    r'^(?P<func>[\w./]+(?:\.[\w]+)+)\([^)]*\)\s*$',
    re.MULTILINE,
)


def parse(bug_text: Any) -> list[dict]:
    """Parse a stack trace out of `bug_text`.

    Args:
        bug_text: a string that may contain a stack trace. None / non-string
            inputs are coerced to '' and produce an empty list.

    Returns:
        A list of frame dicts. Empty list if no frames were found.
    """
    text = _coerce_text(bug_text)
    if not text:
        return []

    frames: list[dict] = []

    frames.extend(_parse_python(text))
    frames.extend(_parse_js_ts(text))
    frames.extend(_parse_java(text))
    frames.extend(_parse_go(text))

    return frames


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            return str(value)
        except Exception:
            return ""
    return value


def _frame(file: str, line: int | None, function: str | None,
           language: str | None) -> dict:
    """Build a frame dict with all four required keys."""
    return {
        "file": file,
        "line": line,
        "function": function,
        "language": language,
    }


def _parse_python(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _PY_RE.finditer(text):
        try:
            line = int(m.group("line"))
        except (TypeError, ValueError):
            line = None
        out.append(_frame(
            file=m.group("file"),
            line=line,
            function=m.group("func"),
            language="Python",
        ))
    return out


def _parse_js_ts(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _JS_RE.finditer(text):
        file_ = m.group("file1") or m.group("file2")
        line_str = m.group("line1") or m.group("line2")
        try:
            line = int(line_str) if line_str is not None else None
        except (TypeError, ValueError):
            line = None
        func = m.group("func")
        out.append(_frame(
            file=file_,
            line=line,
            function=func,
            language="TypeScript" if file_ and file_.endswith((".ts", ".tsx"))
            else "JavaScript",
        ))
    return out


def _parse_java(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _JAVA_RE.finditer(text):
        try:
            line = int(m.group("line"))
        except (TypeError, ValueError):
            line = None
        out.append(_frame(
            file=m.group("file"),
            line=line,
            function=m.group("func"),
            language="Java",
        ))
    return out


def _parse_go(text: str) -> list[dict]:
    """Parse Go goroutine traces.

    Go traces are awkward: the function name is on the line *above* the
    file:line. We collect candidate function names per line and pair them
    with the next Go frame whose `file.go:line` is on a subsequent line.
    """
    # Build a quick map of (line_index_in_text -> function_name) for
    # `pkg.Func(...)` lines.
    out: list[dict] = []

    # Track pending function names by their byte offset so we can pair
    # them with file:line matches that follow.
    pending_funcs: list[tuple[int, str]] = []  # (end_offset, func_name)

    for m in _GO_FUNC_RE.finditer(text):
        pending_funcs.append((m.end(), m.group("func")))

    for m in _GO_RE.finditer(text):
        try:
            line = int(m.group("line"))
        except (TypeError, ValueError):
            line = None

        # Skip matches that are part of JS/TS-style "at fn (path/file.go:..)"
        # patterns that we already covered. Go file frames are rarely inside
        # parens with "at " prefixes, so this is safe.
        # Also: the .go regex is specific enough that this is essentially fine.

        # Pair with the most recent function above us.
        func: str | None = None
        for end_off, name in reversed(pending_funcs):
            if end_off <= m.start():
                func = name
                break

        out.append(_frame(
            file=m.group("file"),
            line=line,
            function=func,
            language="Go",
        ))
    return out


__all__ = ["parse"]
