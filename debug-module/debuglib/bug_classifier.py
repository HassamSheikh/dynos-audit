"""
bug_classifier — AC9.

Deterministic, keyword-regex driven classifier for bug descriptions.

Public API:
    classify(bug_text: str) -> dict
        Returns a dict with keys:
            - bug_type: str, one of the values in ALLOWED_BUG_TYPES
            - mentioned_files: list[dict] with keys path, line, col
            - mentioned_symbols: list[str]

No network, no LLM, no third-party dependencies.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Allowed bug_type values (single source of truth for callers / tests).
# ---------------------------------------------------------------------------
ALLOWED_BUG_TYPES: tuple[str, ...] = (
    "runtime-error",
    "logic-bug",
    "test-failure",
    "race-condition",
    "state-corruption",
    "performance",
    "data-corruption",
    "schema-drift",
    "ci-failure",
    "config-error",
    "unknown",
)

# ---------------------------------------------------------------------------
# Classification keyword groups. Each entry pairs a bug_type with a list
# of compiled regex patterns. The CLASSIFICATION_PRIORITY order determines
# precedence: the first group with any matching pattern wins.
# ---------------------------------------------------------------------------

# Priority order (highest first) per AC9:
#   security, data-corruption, race-condition, resource-leak, performance,
#   runtime-error, test-failure, logic-bug
# We do not emit "security" or "resource-leak" as bug_type values (not in the
# allowed enum), but if security/resource-leak is detected we map them to the
# nearest allowed enum value (state-corruption / performance respectively).

_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    # security -> state-corruption (closest enum value; security is not in enum)
    "security": [
        re.compile(r"\b(sql\s*injection|xss|csrf|rce|ssrf|auth\s*bypass|"
                   r"privilege\s*escalation|cve-\d{4}-\d+)\b", re.I),
    ],
    "data-corruption": [
        re.compile(r"\b(data\s*corrupt(?:ion|ed)?|corrupt(?:ed|ion)?\s*"
                   r"(?:data|database|record|row|file)|"
                   r"database\s*is\s*corrupted?|"
                   r"data\s*got\s*overwritten|"
                   r"overwritten\s*data|"
                   r"row\s*lost|missing\s*rows?)\b", re.I),
    ],
    "race-condition": [
        re.compile(r"\b(race\s*condition|deadlock|livelock|"
                   r"concurrent\s*(?:write|access|modification)|"
                   r"thread\s*safety|data\s*race|"
                   r"intermittent(?:ly)?|flaky|"
                   r"sometimes\s+(?:fails?|shows?|returns?|breaks?|works?|"
                   r"crashes?|hangs?|happens?|0|null|undefined)|"
                   r"sometimes\s+\d+|"
                   r"only\s*sometimes|works\s*on\s*retry)\b", re.I),
    ],
    # resource-leak -> performance (closest enum value)
    "resource-leak": [
        re.compile(r"\b(memory\s*leak|file\s*descriptor\s*leak|fd\s*leak|"
                   r"connection\s*leak|goroutine\s*leak|"
                   r"unclosed\s*(?:file|connection|socket|stream))\b", re.I),
    ],
    "performance": [
        re.compile(r"\b(slow|latency|performance|too\s*slow|"
                   r"timeout|timed\s*out|n\+1\s*query|"
                   r"high\s*cpu|high\s*memory|oom|out\s*of\s*memory|"
                   r"app\s*is\s*slow|takes\s*too\s*long)\b", re.I),
    ],
    "runtime-error": [
        re.compile(r"\b(TypeError|ReferenceError|SyntaxError|RangeError|"
                   r"NullPointerException|"
                   r"AttributeError|KeyError|IndexError|ValueError|"
                   r"NameError|ImportError|ModuleNotFoundError|"
                   r"ZeroDivisionError|OSError|IOError|RuntimeError|"
                   r"AssertionError|UnboundLocalError|"
                   r"panic:|runtime\s*error|unhandled\s*exception|"
                   r"segmentation\s*fault|segfault|core\s*dumped|"
                   r"stack\s*overflow|"
                   r"cannot\s+read\s+propert(?:y|ies)\s+of\s+(?:undefined|null)|"
                   r"is\s+not\s+a\s+function|undefined\s+is\s+not)\b"),
    ],
    "test-failure": [
        re.compile(r"\b(test\s*(?:suite|case|fail(?:ed|ure|ing)?)|"
                   r"failing\s*tests?|"
                   r"assert(?:ion)?\s*fail(?:ed|ure)?|"
                   r"expected\s+.+\s+(?:to|but)\s+(?:be|got|received)|"
                   r"\bit\(['\"]|\bdescribe\(['\"])\b", re.I),
        # AuthController > login (test-runner > breadcrumb)
        re.compile(r"\b\w+\s*>\s*\w+", re.I),
        re.compile(r"\bspec(?:s)?\s+fail", re.I),
    ],
    "schema-drift": [
        re.compile(r"\b(schema\s*drift|schema\s*mismatch|"
                   r"migration\s*(?:pending|out\s*of\s*sync|missing|failed)|"
                   r"column\s+.+\s+does\s+not\s+exist|"
                   r"unknown\s*column|"
                   r"missing\s*column|extra\s*column|"
                   r"table\s+.+\s+does\s+not\s+exist)\b", re.I),
    ],
    "state-corruption": [
        re.compile(r"\b(state\s*corrupt(?:ion|ed)?|"
                   r"invalid\s*state|stale\s*state|"
                   r"redux\s*state|store\s*corrupted)\b", re.I),
    ],
    "logic-bug": [
        re.compile(r"\b(wrong|incorrect|invalid\s*result|off[-\s]?by[-\s]?one|"
                   r"miscalculat(?:ed|ion)|bad\s*output|"
                   r"returns?\s*wrong|calculation\s*is\s*wrong|"
                   r"calc\s*is\s*wrong|"
                   r"should\s*(?:return|be|equal)|"
                   r"expected\s+.+\s+but\s+got)\b", re.I),
    ],
    # CI/pipeline failures — process-shaped, not code-shaped. Checked BEFORE
    # test-failure so "the CI job fails" doesn't collapse into test-failure.
    "ci-failure": [
        re.compile(r"\b(ci|cd|ci/cd)\s*(?:job|run|pipeline|workflow|build|check|stage)s?\s*"
                   r"(?:fail(?:ed|ing|s|ure)?|brok(?:e|en)|red|stuck|hang(?:s|ing)?)\b", re.I),
        re.compile(r"\b(github\s*actions?|gitlab\s*ci|jenkins|circleci|buildkite|"
                   r"travis|azure\s*pipelines?|teamcity)\b", re.I),
        re.compile(r"\b(pipeline|workflow)\s+(?:is\s+)?(?:fail(?:ed|ing|s)?|broken|red)\b", re.I),
        re.compile(r"\bfail(?:s|ed|ing)?\s+(?:only\s+)?(?:in|on)\s+ci\b", re.I),
        re.compile(r"\bworks\s+locally\b.{0,40}\bfails?\b", re.I),
    ],
    # Configuration / environment errors — also process-shaped.
    "config-error": [
        re.compile(r"\b(misconfigur(?:ed|ation)|configuration\s*(?:error|issue|problem)|"
                   r"wrong\s*config(?:uration)?|bad\s*config(?:uration)?|"
                   r"missing\s*(?:env(?:ironment)?\s*var(?:iable)?s?|secret|api\s*key|credential)s?|"
                   r"env(?:ironment)?\s*var(?:iable)?s?\s*(?:not\s*set|missing|unset|undefined)|"
                   r"\.env\s*(?:file\s*)?(?:missing|not\s*loaded|ignored)|"
                   r"invalid\s*(?:yaml|toml|ini|json)\s*config)\b", re.I),
    ],
}

# Priority of detection — first match wins.
# Entries that are NOT in ALLOWED_BUG_TYPES get mapped via _GROUP_TO_BUG_TYPE.
CLASSIFICATION_PRIORITY: tuple[str, ...] = (
    "security",
    "data-corruption",
    "race-condition",
    "resource-leak",
    "ci-failure",
    "config-error",
    "performance",
    "runtime-error",
    "test-failure",
    "schema-drift",
    "state-corruption",
    "logic-bug",
)

# Map internal group names to allowed enum values.
_GROUP_TO_BUG_TYPE: dict[str, str] = {
    "security": "state-corruption",  # not in enum; closest match
    "resource-leak": "performance",  # not in enum; closest match
}

# ---------------------------------------------------------------------------
# Extraction regexes
# ---------------------------------------------------------------------------

# File paths like "src/foo.py:42:7" or "lib/bar.ts:10" or "Main.java:34"
# - path component: at least one slash OR an extension, no whitespace
# - extension: 1-6 alnum chars (covers ts, tsx, py, go, java, rs, rb, dart, js, jsx, kt)
# - optional :line and :col
_FILE_PATH_RE = re.compile(
    r"""
    (?<![\w/])                        # not preceded by a word char or slash
    (
        (?:[\w./-]+/)?[\w.-]+         # optional dir segments + filename
        \.
        (?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|rb|dart|c|cc|cpp|h|hpp|cs|php|swift|scala|sql|json|ya?ml|toml)
    )
    (?::(\d+))?                       # optional :line
    (?::(\d+))?                       # optional :col
    """,
    re.VERBOSE,
)

# Symbols inside backticks: `name`, `name()`, `Class.method()`
_BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_][\w.]*)\s*(?:\(\s*\))?`")

# Bare Name() patterns — capitalised or lower-case identifier followed by ()
# Only outside backticks (we run this on text with backticks already consumed
# OR in addition; dedupe at the end).
_CALL_SYMBOL_RE = re.compile(r"\b([A-Za-z_][\w]{1,})\s*\(\s*\)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(bug_text: Any) -> dict[str, Any]:
    """Classify a bug description.

    Args:
        bug_text: the natural-language bug description. None and empty are
            treated as empty input — the function still returns a dict.

    Returns:
        dict with keys:
            - bug_type: str (one of ALLOWED_BUG_TYPES)
            - mentioned_files: list[dict] each {"path", "line", "col"}
            - mentioned_symbols: list[str]
    """
    text = _coerce_text(bug_text)

    return {
        "bug_type": _classify_type(text),
        "mentioned_files": _extract_files(text),
        "mentioned_symbols": _extract_symbols(text),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _coerce_text(value: Any) -> str:
    """Coerce arbitrary input to a string. None / non-string -> ''."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            return str(value)
        except Exception:
            return ""
    return value


def _classify_type(text: str) -> str:
    """Run priority-ordered keyword matching against `text`."""
    if not text or not text.strip():
        return "unknown"

    for group in CLASSIFICATION_PRIORITY:
        patterns = _PATTERNS.get(group, [])
        for pat in patterns:
            if pat.search(text):
                return _GROUP_TO_BUG_TYPE.get(group, group)

    return "unknown"


def _extract_files(text: str) -> list[dict[str, Any]]:
    """Extract file-path-like tokens with optional :line:col into dicts."""
    if not text:
        return []

    seen: set[tuple[str, int | None, int | None]] = set()
    out: list[dict[str, Any]] = []

    for match in _FILE_PATH_RE.finditer(text):
        path = match.group(1)
        line_str = match.group(2)
        col_str = match.group(3)

        line = int(line_str) if line_str is not None else None
        col = int(col_str) if col_str is not None else None

        key = (path, line, col)
        if key in seen:
            continue
        seen.add(key)

        out.append({"path": path, "line": line, "col": col})

    return out


def _extract_symbols(text: str) -> list[str]:
    """Extract function/class names from backticks and bare Name() patterns."""
    if not text:
        return []

    seen: set[str] = set()
    out: list[str] = []

    # 1. Backtick-wrapped symbols.
    for match in _BACKTICK_SYMBOL_RE.finditer(text):
        sym = match.group(1)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    # 2. Bare Name() patterns. Strip backticked regions first to avoid
    #    double-counting and then scan the remainder.
    stripped = re.sub(r"`[^`]*`", " ", text)
    for match in _CALL_SYMBOL_RE.finditer(stripped):
        sym = match.group(1)
        # Skip common english words that can look like calls (best-effort).
        if sym.lower() in {"is", "if", "or", "and", "not", "the", "in", "on",
                           "of", "to", "a", "an"}:
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)

    return out


__all__ = ["classify", "ALLOWED_BUG_TYPES"]
