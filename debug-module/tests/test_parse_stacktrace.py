"""
Tests for debug-module/debuglib/parse_stacktrace.py — AC11.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)

PYTHON_TRACEBACK = """\
Traceback (most recent call last):
  File "src/service/user_service.py", line 47, in get_user
    return self.db.query(User).filter_by(id=user_id).one()
  File "lib/db/session.py", line 12, in one
    raise NoResultFound
sqlalchemy.orm.exc.NoResultFound
"""

JS_TS_TRACE = """\
TypeError: Cannot read properties of undefined (reading 'id')
    at UserService.getUser (src/service/userService.ts:47:18)
    at async AuthController.login (src/controllers/auth.ts:23:22)
"""

GO_TRACE = """\
goroutine 1 [running]:
main.processRequest(...)
\t/home/user/project/main.go:42 +0x89
"""

JAVA_TRACE = """\
Exception in thread "main" java.lang.NullPointerException
\tat com.example.app.UserService.getUser(UserService.java:34)
\tat com.example.app.Main.main(Main.java:10)
"""


def _import_parse_stacktrace():
    try:
        from debuglib import parse_stacktrace
        return parse_stacktrace
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"parse_stacktrace module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/parse_stacktrace.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# Python traceback parsing
# ---------------------------------------------------------------------------

def test_ac11_python_traceback_returns_py_frame():
    """parse() on a Python traceback returns at least one frame with file ending in .py."""
    m = _import_parse_stacktrace()
    frames = m.parse(PYTHON_TRACEBACK)
    assert isinstance(frames, list)
    assert len(frames) >= 1
    py_frames = [f for f in frames if f.get("file", "").endswith(".py")]
    assert py_frames, f"No .py frame found in: {frames}"


def test_ac11_python_frame_has_non_null_line():
    """Python traceback frames include a non-null integer line number."""
    m = _import_parse_stacktrace()
    frames = m.parse(PYTHON_TRACEBACK)
    py_frames = [f for f in frames if f.get("file", "").endswith(".py")]
    assert all(f.get("line") is not None for f in py_frames), (
        f"Some Python frames have null line: {py_frames}"
    )


def test_ac11_python_frame_has_required_keys():
    """Every frame dict contains the four required keys."""
    m = _import_parse_stacktrace()
    frames = m.parse(PYTHON_TRACEBACK)
    for frame in frames:
        for key in ("file", "line", "function", "language"):
            assert key in frame, f"Frame missing key {key!r}: {frame}"


# ---------------------------------------------------------------------------
# JS/TS stack trace parsing
# ---------------------------------------------------------------------------

def test_ac11_js_ts_trace_returns_ts_frame():
    """parse() on a JS/TS trace returns at least one frame with file ending in .ts."""
    m = _import_parse_stacktrace()
    frames = m.parse(JS_TS_TRACE)
    assert isinstance(frames, list)
    ts_frames = [f for f in frames if f.get("file", "").endswith(".ts")]
    assert ts_frames, f"No .ts frame found in: {frames}"


def test_ac11_js_ts_frame_has_line_number():
    """JS/TS frame parsed from 'at function (file.ts:10:5)' has a non-null line."""
    m = _import_parse_stacktrace()
    frames = m.parse(JS_TS_TRACE)
    ts_frames = [f for f in frames if f.get("file", "").endswith(".ts")]
    assert all(f.get("line") is not None for f in ts_frames), (
        f"Some TS frames have null line: {ts_frames}"
    )


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

def test_ac11_empty_string_returns_empty_list():
    """parse('') returns an empty list without raising."""
    m = _import_parse_stacktrace()
    result = m.parse("")
    assert result == [], f"Expected [], got {result!r}"


def test_ac11_no_traceback_text_returns_empty_list():
    """parse() on plain prose with no traceback returns an empty list."""
    m = _import_parse_stacktrace()
    result = m.parse("The calculation gives wrong results on Sundays.")
    assert isinstance(result, list)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Go traceback (regression — make sure it doesn't crash)
# ---------------------------------------------------------------------------

def test_ac11_go_trace_does_not_crash():
    """parse() on a Go goroutine trace does not raise an exception."""
    m = _import_parse_stacktrace()
    result = m.parse(GO_TRACE)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Java traceback
# ---------------------------------------------------------------------------

def test_ac11_java_trace_does_not_crash():
    """parse() on a Java stack trace does not raise an exception."""
    m = _import_parse_stacktrace()
    result = m.parse(JAVA_TRACE)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Frame structure integrity
# ---------------------------------------------------------------------------

def test_ac11_all_frames_have_required_keys():
    """Every frame returned by parse() for any input has all four required keys."""
    m = _import_parse_stacktrace()
    for trace in (PYTHON_TRACEBACK, JS_TS_TRACE, GO_TRACE, JAVA_TRACE):
        frames = m.parse(trace)
        for frame in frames:
            for key in ("file", "line", "function", "language"):
                assert key in frame, (
                    f"Frame missing required key {key!r}: {frame!r}"
                )
