"""
Tests for debug-module/debuglib/git_forensics.py — AC15.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)

REPO_ROOT = str(Path(__file__).parent.parent.parent)


def _import_git_forensics():
    try:
        from debuglib import git_forensics
        return git_forensics
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"git_forensics module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/git_forensics.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# Valid git repo (dynos-work itself)
# ---------------------------------------------------------------------------

def test_ac15_analyze_real_repo_returns_dict():
    """analyze() on the dynos-work repo root returns a dict."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], None)
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


def test_ac15_recent_commits_non_empty_on_real_repo():
    """analyze() on the dynos-work repo returns dict where recent_commits has >= 1 entry."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], None)
    assert "recent_commits" in result, f"'recent_commits' key missing: {result.keys()}"
    assert len(result["recent_commits"]) >= 1, (
        f"Expected at least 1 recent commit, got {len(result['recent_commits'])}"
    )


def test_ac15_recent_commit_has_required_keys():
    """Each entry in recent_commits has sha, message, author, date, files_changed."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], None)
    commits = result.get("recent_commits", [])
    if not commits:
        pytest.skip("No recent commits returned — cannot check commit structure")
    for commit in commits[:3]:
        for key in ("sha", "message", "author", "date", "files_changed"):
            assert key in commit, f"Commit missing key {key!r}: {commit}"


def test_ac15_result_has_blame_ranges_key():
    """analyze() result always contains a 'blame_ranges' key."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], None)
    assert "blame_ranges" in result, f"'blame_ranges' key missing: {result.keys()}"


def test_ac15_result_has_co_change_pairs_key():
    """analyze() result always contains a 'co_change_pairs' key."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], None)
    assert "co_change_pairs" in result, f"'co_change_pairs' key missing: {result.keys()}"


# ---------------------------------------------------------------------------
# Non-git path → structured error dict, no exception
# ---------------------------------------------------------------------------

def test_ac15_non_git_path_returns_dict_with_error(tmp_path):
    """analyze() on a non-git directory returns a dict with an 'error' key without raising."""
    m = _import_git_forensics()
    result = m.analyze(str(tmp_path), [], None)
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "error" in result, (
        f"Expected 'error' key for non-git path, got keys: {list(result.keys())}"
    )


def test_ac15_non_git_path_no_exception(tmp_path):
    """analyze() on a non-git path does not raise any exception."""
    m = _import_git_forensics()
    result = m.analyze(str(tmp_path), [], None)
    assert result is not None


def test_ac15_non_git_path_error_value_is_string(tmp_path):
    """The 'error' field for a non-git path is a non-empty string."""
    m = _import_git_forensics()
    result = m.analyze(str(tmp_path), [], None)
    assert isinstance(result.get("error"), str) and result["error"], (
        f"'error' must be a non-empty string, got {result.get('error')!r}"
    )


def test_ac15_non_existent_path_returns_error_dict():
    """analyze() on a path that does not exist returns error dict without raising."""
    m = _import_git_forensics()
    result = m.analyze("/this/path/does/not/exist/at/all", [], None)
    assert isinstance(result, dict)
    assert "error" in result


# ---------------------------------------------------------------------------
# since parameter
# ---------------------------------------------------------------------------

def test_ac15_since_parameter_accepted():
    """analyze() accepts a 'since' string parameter without crashing."""
    m = _import_git_forensics()
    result = m.analyze(REPO_ROOT, [], "HEAD~5")
    assert isinstance(result, dict)
