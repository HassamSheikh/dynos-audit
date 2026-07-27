"""
Tests for debug-module/debuglib/language_detect.py — AC10.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_language_detect():
    try:
        from debuglib import language_detect
        return language_detect
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"language_detect module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/language_detect.py to make this test pass."
        )


def test_ac10_detects_typescript_and_go_from_mentioned_files(tmp_path):
    """detect() with mentioned_files=['app.ts', 'main.go'] returns list containing TypeScript and Go."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["app.ts", "main.go"])
    assert isinstance(result, list)
    assert "TypeScript" in result, f"Expected 'TypeScript' in {result}"
    assert "Go" in result, f"Expected 'Go' in {result}"


def test_ac10_detects_python_from_mentioned_files(tmp_path):
    """detect() with mentioned_files=['script.py'] returns list containing Python."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["script.py"])
    assert "Python" in result, f"Expected 'Python' in {result}"


def test_ac10_empty_mentioned_files_returns_list(tmp_path):
    """detect() with empty mentioned_files returns a list without raising."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), [])
    assert isinstance(result, list)


def test_ac10_result_is_deduplicated(tmp_path):
    """detect() deduplicates language names — no duplicates in output."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["app.ts", "other.ts", "main.ts"])
    assert result.count("TypeScript") == 1, (
        f"TypeScript appeared {result.count('TypeScript')} times — must be deduplicated"
    )


def test_ac10_detects_javascript(tmp_path):
    """detect() with a .js file returns JavaScript in the list."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["index.js"])
    assert "JavaScript" in result, f"Expected 'JavaScript' in {result}"


def test_ac10_detects_rust(tmp_path):
    """detect() with a .rs file returns Rust in the list."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["main.rs"])
    assert "Rust" in result, f"Expected 'Rust' in {result}"


def test_ac10_detects_java(tmp_path):
    """detect() with a .java file returns Java in the list."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["App.java"])
    assert "Java" in result, f"Expected 'Java' in {result}"


def test_ac10_detects_ruby(tmp_path):
    """detect() with a .rb file returns Ruby in the list."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["app.rb"])
    assert "Ruby" in result, f"Expected 'Ruby' in {result}"


def test_ac10_unknown_extension_does_not_crash(tmp_path):
    """detect() with an unrecognised extension does not raise."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["data.xyz123"])
    assert isinstance(result, list)


def test_ac10_returns_list_not_set(tmp_path):
    """detect() always returns a list, not a set or other iterable."""
    m = _import_language_detect()
    result = m.detect(str(tmp_path), ["app.ts"])
    assert isinstance(result, list)


def test_ac10_only_returns_allowed_language_names(tmp_path):
    """Every language name in the result belongs to the allowed set."""
    m = _import_language_detect()
    allowed = {"JavaScript", "TypeScript", "Python", "Go", "Rust", "Java", "Dart", "Ruby"}
    result = m.detect(str(tmp_path), ["app.ts", "main.go", "script.py"])
    for lang in result:
        assert lang in allowed, f"Unexpected language name {lang!r} — not in allowed set"
