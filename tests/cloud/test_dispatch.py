"""Tests for hooks/cloud/dispatch.py — verifies handlers produce the
right outcome shape and precondition enforcement."""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.cloud import dispatch as D  # noqa: E402
from hooks.cloud.config import Config  # noqa: E402


def _ctx(tmp_path: Path) -> D.DispatchContext:
    cfg = Config(
        enabled=True,
        api_url="http://127.0.0.1:9999",
        ws_url=None,
        tenant_id=None,
        device_name="test",
        offline_queue_path=tmp_path / "queue.sqlite3",
        cache_dir=tmp_path / "cache",
        log_level="info",
    )
    return D.DispatchContext(tmp_path, cfg)


class _StateWithCtx:
    def __init__(self, ctx: D.DispatchContext) -> None:
        self.context = ctx
        self.config = ctx.config
        self.task_id = "t-1"
        self.session_key_fingerprint = "fp"


def _run(coro):
    return asyncio.run(coro)


def test_unknown_type_raises(tmp_path: Path) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    with pytest.raises(ValueError):
        _run(D.dispatch({"body": {"type": "bogus"}}, state))


def test_run_tests_captures_exit_code(tmp_path: Path) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    inst = {
        "body": {
            "type": "run_tests",
            "cmd": "python3 -c 'import sys; sys.exit(0)'",
            "timeout_s": 10,
            "env_allowlist": ["PATH"],
            "cache_output": False,
        },
        "precondition": {},
    }
    result = _run(D.dispatch(inst, state))
    assert result["exit_code"] == 0
    assert result["stdout_hash"].startswith("sha256:")
    assert result["__precondition_met"] is True


def test_run_tests_nonzero_exit(tmp_path: Path) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    inst = {
        "body": {
            "type": "run_tests",
            "cmd": "python3 -c 'import sys; sys.exit(3)'",
            "timeout_s": 10,
            "env_allowlist": [],
            "cache_output": False,
        },
        "precondition": {},
    }
    result = _run(D.dispatch(inst, state))
    assert result["exit_code"] == 3


def test_precondition_drift_flagged(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    state = _StateWithCtx(_ctx(tmp_path))
    inst = {
        "body": {"type": "run_tests", "cmd": "true", "timeout_s": 5, "env_allowlist": [], "cache_output": False},
        "precondition": {"repo_hash_must_equal": "sha256:ffffffff"},
    }
    result = _run(D.dispatch(inst, state))
    assert result["__precondition_met"] is False


def test_human_review_surfaces_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    inst = {
        "body": {
            "type": "request_human_review",
            "gate_id": "g1",
            "summary_hash": "sha256:abc",
            "required_signers": 1,
            "review_url": "https://app.dynos.work/review/t1",
        },
        "precondition": {},
    }
    result = _run(D.dispatch(inst, state))
    assert result["acknowledged"] is True
    assert "https://app.dynos.work/review/t1" in capsys.readouterr().out


def test_run_local_validator_json_schema_ok(tmp_path: Path) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    # Seed the local cache.
    data = b'{"ok": true}'
    h = "sha256:" + hashlib.sha256(data).hexdigest()
    state.context.cas.put(data, h)
    # Stub fetch_artifact to bypass cloud.
    with patch.object(D, "fetch_artifact", lambda _cfg, _h, _cache: data):
        result = _run(
            D.dispatch(
                {"body": {"type": "run_local_validator", "validator_id": "json_schema", "input_hash": h}, "precondition": {}},
                state,
            )
        )
    assert result["ok"] is True
    assert result["errors"] == []


def test_run_local_validator_json_schema_fail(tmp_path: Path) -> None:
    state = _StateWithCtx(_ctx(tmp_path))
    bad = b"not json"
    h = "sha256:" + hashlib.sha256(bad).hexdigest()
    state.context.cas.put(bad, h)
    with patch.object(D, "fetch_artifact", lambda _cfg, _h, _cache: bad):
        result = _run(
            D.dispatch(
                {"body": {"type": "run_local_validator", "validator_id": "json_schema", "input_hash": h}, "precondition": {}},
                state,
            )
        )
    assert result["ok"] is False
    assert result["errors"]


def test_collect_context_computes_repo_hash(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    state = _StateWithCtx(_ctx(tmp_path))
    # Don't actually hit the network — stub negotiate.
    with patch.object(D, "negotiate_and_upload", lambda _cfg, refs, local_only=False: {r.hash: "local_only" for r in refs}):
        result = _run(
            D.dispatch(
                {
                    "body": {
                        "type": "collect_context",
                        "include_globs": ["**/*"],
                        "exclude_globs": [],
                        "max_bytes": 100_000,
                        "storage_mode": "local_only",
                    },
                    "precondition": {},
                },
                state,
            )
        )
    assert result["repo_hash"].startswith("")  # Just verify it's a non-empty str
    assert len(result["repo_hash"]) == 64
    assert result["file_count"] == 1
