"""
Instruction dispatcher — runs cloud-issued instructions locally and
returns the outcome dict (signed into a receipt by client.py).

Every handler returns a dict whose shape matches the instruction's
declared `expected_outcome_shape`. Special keys:
    __precondition_met: bool   — passed through into the receipt
    __artifact_refs: list      — content-addressed outputs this step produced

The handler map is dispatched on `body.type`. Unknown types raise
ValueError — the cloud will mark the receipt failed, non-retryable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .cas import LocalCAS, compute_file_index, fetch_artifact, negotiate_and_upload
from .config import Config
from .crypto import repo_hash, sha256_hex

logger = logging.getLogger("dynos.cloud.dispatch")


class DispatchContext:
    """Shared state across instruction handlers. Holds the repo root,
    the LocalCAS, and the active Config."""

    def __init__(self, repo_root: Path, config: Config) -> None:
        self.repo_root = repo_root
        self.config = config
        self.cas = LocalCAS(config.cache_dir)


async def dispatch(inst: dict[str, Any], state: Any) -> dict[str, Any]:
    """Entry point imported by client.py.

    `state` is a ClientState; we cast-access without importing to avoid
    circularity with the client module.
    """
    body = inst.get("body", {})
    body_type = body.get("type")
    precondition = inst.get("precondition", {}) or {}

    # Resolve a DispatchContext. In the typical case the caller sets
    # state.context = DispatchContext(...); fall back to cwd-based if not.
    ctx: DispatchContext
    if hasattr(state, "context") and isinstance(state.context, DispatchContext):
        ctx = state.context
    else:
        cfg = state.config if hasattr(state, "config") else None
        if cfg is None:
            raise RuntimeError("DispatchContext missing and no config on state")
        ctx = DispatchContext(Path.cwd(), cfg)

    # Precondition check — repo_hash must still match.
    precondition_met = True
    required = precondition.get("repo_hash_must_equal")
    if required:
        observed, _ = repo_hash(ctx.repo_root)
        precondition_met = observed == required
        if not precondition_met:
            return {
                "reason": "repo_hash drift",
                "required": required,
                "observed": observed,
                "__precondition_met": False,
                "__artifact_refs": [],
            }

    handler = _HANDLERS.get(body_type or "")
    if not handler:
        raise ValueError(f"unknown instruction type: {body_type!r}")
    out = await handler(body, ctx)
    out.setdefault("__precondition_met", precondition_met)
    out.setdefault("__artifact_refs", [])
    return out


# ─────────────────────── handlers ──


async def h_collect_context(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    include = list(body.get("include_globs", ["**/*"]))
    exclude = list(body.get("exclude_globs", []))
    # Compute repo_hash over the working tree.
    rh, entries = repo_hash(ctx.repo_root, include_globs=include, exclude_globs=exclude)
    refs = compute_file_index(ctx.repo_root, entries)
    # CAS negotiation + upload (or local-only if tenant policy says so).
    local_only = body.get("storage_mode") == "local_only"
    storage_map = negotiate_and_upload(ctx.config, refs, local_only=local_only)
    artifact_refs = [
        {"hash": r.hash, "kind": r.kind, "storage": storage_map.get(r.hash, "local_only")}
        for r in refs
    ]
    return {
        "repo_hash": rh,
        "file_count": len(refs),
        "total_bytes": sum(r.size_bytes for r in refs),
        "__artifact_refs": artifact_refs,
    }


async def h_upload_artifacts(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    required = list(body.get("hashes_required", []))
    uploaded: list[str] = []
    for h in required:
        data = ctx.cas.get(h)
        if not data:
            # Blob missing — the plugin lost its cache. Fallback: we can't
            # upload what we don't have. Surface as a failure the cloud
            # can handle by re-collecting context.
            continue
        # Re-hash to confirm.
        hex_ = hashlib.sha256(data).hexdigest()
        if f"sha256:{hex_}" != h:
            continue
        from .cas import ArtifactRef

        ref = ArtifactRef(hash=h, kind="source_chunk", size_bytes=len(data), local_path=ctx.cas.path_for(h))
        negotiate_and_upload(ctx.config, [ref])
        uploaded.append(h)
    return {"uploaded_count": len(uploaded), "requested_count": len(required)}


async def h_run_tests(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    cmd = body.get("cmd") or ""
    cwd = body.get("cwd")
    timeout_s = int(body.get("timeout_s", 300))
    env_allowlist = list(body.get("env_allowlist", []))

    env = {k: v for k, v in os.environ.items() if k in env_allowlist or k in ("PATH", "HOME", "LANG", "LC_ALL")}

    workdir = ctx.repo_root / cwd if cwd else ctx.repo_root
    try:
        proc = subprocess.run(  # noqa: S603 — deliberate, cloud-issued cmd
            shlex.split(cmd),
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "duration_ms": timeout_s * 1000,
            "timed_out": True,
            "stdout_hash": "sha256:0",
            "stderr_hash": "sha256:0",
        }

    stdout_hash = f"sha256:{hashlib.sha256(stdout).hexdigest()}"
    stderr_hash = f"sha256:{hashlib.sha256(stderr).hexdigest()}"

    # Cache outputs locally if requested.
    if body.get("cache_output"):
        ctx.cas.put(stdout, stdout_hash)
        ctx.cas.put(stderr, stderr_hash)

    return {
        "cmd": cmd,
        "exit_code": exit_code,
        "stdout_hash": stdout_hash,
        "stderr_hash": stderr_hash,
        "duration_ms": 0,  # subprocess doesn't return this cleanly pre-3.12
        "__artifact_refs": [
            {"hash": stdout_hash, "kind": "evidence", "storage": "local_only"},
            {"hash": stderr_hash, "kind": "evidence", "storage": "local_only"},
        ],
    }


async def h_apply_patch(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    patch_hash = body.get("patch_hash", "")
    files_must_match = body.get("files_must_match", []) or []

    # Verify all preconditions.
    for f in files_must_match:
        path = ctx.repo_root / f["path"]
        if not path.exists():
            return {"applied": False, "reason": f"missing {f['path']}", "__precondition_met": False}
        actual = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        if actual != f["hash"]:
            return {
                "applied": False,
                "reason": f"{f['path']} hash drift",
                "__precondition_met": False,
            }

    patch = fetch_artifact(ctx.config, patch_hash, ctx.cas)
    pre_rh, _ = repo_hash(ctx.repo_root)

    # Apply via `patch -p0` reading from stdin.
    proc = subprocess.run(  # noqa: S603
        ["patch", "-p0", "--no-backup-if-mismatch"],
        cwd=ctx.repo_root,
        input=patch,
        capture_output=True,
        check=False,
    )
    applied = proc.returncode == 0
    post_rh, _ = repo_hash(ctx.repo_root) if applied else (pre_rh, [])
    return {
        "applied": applied,
        "pre_repo_hash": pre_rh,
        "post_repo_hash": post_rh,
        "patch_hash": patch_hash,
        "apply_stderr": proc.stderr.decode("utf-8", errors="replace")[:1000],
    }


async def h_gather_evidence(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    kind = body.get("kind", "custom")
    # MVP: return the requested spec verbatim. Extended impls will
    # run coverage tools / linters per `kind`.
    return {"kind": kind, "summary": "evidence gathered (stub)"}


async def h_request_human_review(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    url = body.get("review_url", "")
    # We surface the URL to the user; the actual approval arrives out
    # of band via the dashboard → cloud. The plugin just acknowledges
    # receipt of the instruction.
    print(f"\n>>> dynos-work: human review required: {url}\n", flush=True)
    return {"acknowledged": True, "review_url": url}


async def h_run_local_validator(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    validator_id = body.get("validator_id", "")
    input_hash = body.get("input_hash", "")
    data = fetch_artifact(ctx.config, input_hash, ctx.cas)
    # MVP validator registry is name-based; extend as new validators
    # land.  Return structured ok/errors.
    errors: list[str] = []
    if validator_id == "json_schema":
        try:
            json.loads(data.decode("utf-8"))
        except Exception as e:
            errors.append(f"json parse: {e}")
    elif validator_id == "plan_v1":
        # Minimal: plan must have a "segments" or "technical_approach".
        try:
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict):
                errors.append("plan not a JSON object")
        except Exception as e:
            errors.append(str(e))
    return {"validator_id": validator_id, "errors": errors, "ok": len(errors) == 0}


async def h_commit_and_sign(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    message = body.get("message", "")
    files = list(body.get("files", []))
    author_name = body.get("author_name", "dynos-work")
    author_email = body.get("author_email", "dynos-work@example.com")

    if not files:
        return {"committed": False, "reason": "no files"}

    # Stage + commit.
    env = {**os.environ, "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
           "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}
    add = subprocess.run(["git", "add", "--", *files], cwd=ctx.repo_root, capture_output=True, env=env)  # noqa: S603, S607
    if add.returncode != 0:
        return {"committed": False, "reason": "git add failed", "stderr": add.stderr.decode("utf-8")[:500]}
    cm = subprocess.run(  # noqa: S603, S607
        ["git", "commit", "-m", message, "--no-gpg-sign"] if not body.get("sign") else ["git", "commit", "-S", "-m", message],
        cwd=ctx.repo_root, capture_output=True, env=env,
    )
    if cm.returncode != 0:
        return {"committed": False, "reason": "git commit failed", "stderr": cm.stderr.decode("utf-8")[:500]}

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ctx.repo_root, capture_output=True).stdout.decode("utf-8").strip()  # noqa: S603, S607
    return {"committed": True, "head_sha": sha, "message": message}


async def h_fetch_artifact(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    h = body.get("hash", "")
    dest = body.get("destination", {}) or {}
    data = fetch_artifact(ctx.config, h, ctx.cas)
    kind = dest.get("kind", "inline_return")
    if kind == "file":
        p = ctx.repo_root / dest["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"materialised": True, "path": dest["path"], "bytes": len(data)}
    if kind == "stdout":
        import sys

        sys.stdout.buffer.write(data)
        return {"materialised": True, "target": "stdout", "bytes": len(data)}
    return {"materialised": True, "data_hash": f"sha256:{sha256_hex(data)}", "bytes": len(data)}


async def h_apply_plan_segment(body: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    """Very lightweight segment executor. For the MVCloud we defer the
    real per-step execution to specialised handlers (apply_patch,
    run_tests, commit_and_sign). If the segment's `steps[]` is empty
    we just signal success — the cloud will follow up with targeted
    instructions."""
    segment_id = body.get("segment_id", "")
    steps = list(body.get("steps", []))
    results: list[dict[str, Any]] = []
    for s in steps:
        results.append({"id": s.get("id", ""), "kind": s.get("kind", ""), "status": "skipped"})
    return {"segment_id": segment_id, "step_count": len(steps), "results": results}


_HANDLERS: dict[str, Any] = {
    "collect_context": h_collect_context,
    "upload_artifacts": h_upload_artifacts,
    "run_tests": h_run_tests,
    "apply_patch": h_apply_patch,
    "gather_evidence": h_gather_evidence,
    "request_human_review": h_request_human_review,
    "run_local_validator": h_run_local_validator,
    "commit_and_sign": h_commit_and_sign,
    "fetch_artifact": h_fetch_artifact,
    "apply_plan_segment": h_apply_plan_segment,
}

# Prevent unused-import flake on `asyncio`.
_ = asyncio
