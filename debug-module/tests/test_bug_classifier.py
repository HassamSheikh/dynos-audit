"""
Tests for debug-module/debuglib/bug_classifier.py — AC9.

Every test maps to exactly one behaviour specified in AC9.
"""
import sys
from pathlib import Path

import pytest

# Ensure debug-module/ is on sys.path so 'from debuglib import ...' works
_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_classifier():
    """Import bug_classifier, failing the test clearly if not yet implemented."""
    try:
        from debuglib import bug_classifier
        return bug_classifier
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"bug_classifier module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/bug_classifier.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# bug_type classification
# ---------------------------------------------------------------------------

def test_ac9_runtime_error_classification():
    """classify() returns bug_type == 'runtime-error' for a TypeError message."""
    m = _import_classifier()
    result = m.classify("TypeError: Cannot read properties of undefined")
    assert result["bug_type"] == "runtime-error"


def test_ac9_test_failure_classification():
    """classify() returns bug_type == 'test-failure' for a test suite description."""
    m = _import_classifier()
    result = m.classify("test suite: AuthController > login")
    assert result["bug_type"] == "test-failure"


def test_ac9_logic_bug_classification():
    """classify() returns bug_type == 'logic-bug' for a plain wrong-output description."""
    m = _import_classifier()
    result = m.classify("workout volume calc is wrong")
    assert result["bug_type"] == "logic-bug"


def test_ac9_race_condition_or_logic_bug_for_intermittent():
    """classify() returns 'race-condition' or 'logic-bug' for intermittent-behaviour text."""
    m = _import_classifier()
    result = m.classify("sometimes shows 0")
    assert result["bug_type"] in ("race-condition", "logic-bug"), (
        f"Expected 'race-condition' or 'logic-bug', got {result['bug_type']!r}"
    )


# ---------------------------------------------------------------------------
# Edge cases — empty input
# ---------------------------------------------------------------------------

def test_ac9_empty_string_returns_dict_with_bug_type():
    """classify('') must return a dict containing 'bug_type' without raising."""
    m = _import_classifier()
    result = m.classify("")
    assert isinstance(result, dict), "classify() must return a dict"
    assert "bug_type" in result, "dict must contain 'bug_type' key"


# ---------------------------------------------------------------------------
# mentioned_files extraction
# ---------------------------------------------------------------------------

def test_ac9_mentioned_files_extracts_path_with_line():
    """classify() extracts file paths with line numbers into mentioned_files."""
    m = _import_classifier()
    result = m.classify("src/foo.py:42")
    mentioned = result["mentioned_files"]
    assert any(
        entry.get("path") == "src/foo.py" and entry.get("line") == 42
        for entry in mentioned
    ), f"Expected {{'path': 'src/foo.py', 'line': 42}} in mentioned_files, got: {mentioned}"


def test_ac9_mentioned_files_is_always_list():
    """mentioned_files is always a list, even when no files are present."""
    m = _import_classifier()
    result = m.classify("totally unrelated prose with no paths")
    assert isinstance(result["mentioned_files"], list)


# ---------------------------------------------------------------------------
# mentioned_symbols extraction
# ---------------------------------------------------------------------------

def test_ac9_mentioned_symbols_extracts_backtick_symbol():
    """classify() extracts backtick-wrapped function names into mentioned_symbols."""
    m = _import_classifier()
    result = m.classify("`calculate()` returns wrong value")
    assert isinstance(result["mentioned_symbols"], list)
    assert len(result["mentioned_symbols"]) > 0, (
        "mentioned_symbols must be non-empty when a backtick symbol is present"
    )


def test_ac9_mentioned_symbols_is_always_list():
    """mentioned_symbols is always a list, even when no symbols are present."""
    m = _import_classifier()
    result = m.classify("no symbols here at all")
    assert isinstance(result["mentioned_symbols"], list)


# ---------------------------------------------------------------------------
# Return structure completeness
# ---------------------------------------------------------------------------

def test_ac9_classify_always_returns_required_keys():
    """classify() always returns a dict with all three required keys."""
    m = _import_classifier()
    result = m.classify("some random text")
    for key in ("bug_type", "mentioned_files", "mentioned_symbols"):
        assert key in result, f"Missing required key: {key!r}"


def test_ac9_bug_type_is_always_from_allowed_set():
    """bug_type is always one of the 9 allowed enum values."""
    m = _import_classifier()
    allowed = {
        "runtime-error", "logic-bug", "test-failure", "race-condition",
        "state-corruption", "performance", "data-corruption", "schema-drift", "unknown",
    }
    inputs = [
        "TypeError in user service",
        "test failed: should return 401",
        "calculation is sometimes wrong",
        "database is corrupted",
        "app is slow",
        "data got overwritten",
        "migration pending",
        "",
        "gibberish fjdkslafjd",
    ]
    for text in inputs:
        result = m.classify(text)
        assert result["bug_type"] in allowed, (
            f"classify({text!r}) returned invalid bug_type {result['bug_type']!r}"
        )


# ---------------------------------------------------------------------------
# AC 11 (task-20260616-002, finding #66): the dead `if not path or ...: pass`
# block and its comment must be deleted; behaviour for dotted-version tokens
# is unchanged (they are already filtered by the extension whitelist upstream).
# ---------------------------------------------------------------------------

_BUG_CLASSIFIER_SRC = (
    Path(__file__).parent.parent / "debuglib" / "bug_classifier.py"
).read_text(encoding="utf-8")


def test_bug_classifier_dead_pass_block_removed():
    """The dead no-op branch must be gone from the source text. FAILS while
    the `if not path or path.startswith(...)` block remains."""
    assert "if not path or path.startswith" not in _BUG_CLASSIFIER_SRC, (
        "dead `if not path or path.startswith(...)` block still present in "
        "bug_classifier.py — AC 11 requires its deletion"
    )
    assert "Skip pure dotted-version-like tokens" not in _BUG_CLASSIFIER_SRC, (
        "orphaned comment for the dead block still present — AC 11 requires "
        "both the comment and the if/pass block to be removed"
    )


def test_bug_classifier_dotted_version_token_handling_unchanged():
    """A dotted-version-like token (e.g. '1.2.3') is still NOT extracted as a
    file path (filtered by the extension whitelist), while a real path token
    IS extracted — confirming the dead-block deletion introduces no behaviour
    change."""
    m = _import_classifier()

    version_files = m._extract_files("version 1.2.3 crashed at startup")
    assert version_files == [], (
        f"dotted-version token leaked as a file path: {version_files!r}"
    )

    real_files = m._extract_files("error in app.py:12:3 failed")
    assert real_files == [{"path": "app.py", "line": 12, "col": 3}], (
        f"real path extraction changed unexpectedly: {real_files!r}"
    )


# ---------------------------------------------------------------------------
# AC 12 (finding #67): module docstring must not hardcode the stale '9' count.
# ---------------------------------------------------------------------------

def test_bug_classifier_docstring_no_stale_count():
    """The source must not contain the stale '9 allowed' substring; the count
    of allowed bug types is 11, so the hardcoded '9' is wrong."""
    m = _import_classifier()
    assert "9 allowed" not in _BUG_CLASSIFIER_SRC, (
        "stale '9 allowed' count still present in bug_classifier.py docstring "
        f"(ALLOWED_BUG_TYPES has {len(m.ALLOWED_BUG_TYPES)} entries)"
    )
    # Guard the premise: the real count must not be 9 (else '9' would be valid).
    assert len(m.ALLOWED_BUG_TYPES) != 9
