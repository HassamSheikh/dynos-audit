"""
Driver router.

Reads cloud config and picks one of two paths:

    local-canonical (default, open source):
        existing `hooks/ctl.py` code paths run unchanged;
        state lives in `.dynos/task-*/`.

    cloud-canonical (enterprise, opt-in):
        session JWT + Ed25519 key from keychain; connect to
        the PluginCoordinator DO via WebSocket; execute
        cloud-issued instructions; sign receipts.

This file is the *only* file that knows about both modes. All
other modules continue to work as they did — the cloud path is
additive.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def route_stage(task_dir: Path, stage: str) -> int:
    """Return process exit code for the stage. For local-canonical, we
    defer to ctl.py; for cloud-canonical, we short-circuit to signal
    the driver took over (the cloud issues the instructions).
    """
    try:
        from hooks.cloud import is_enabled  # type: ignore[import-not-found]
    except ImportError:
        is_enabled = lambda _root=None: False  # noqa: E731

    project_root = task_dir.parent.parent
    if not is_enabled(project_root):
        # Defer to local-canonical implementation.
        import hooks.ctl as ctl  # type: ignore[import-not-found]

        argv = ["ctl", "validate-task", str(task_dir)]
        if stage:
            argv = ["ctl", "transition", str(task_dir), stage]
        old_argv, sys.argv = sys.argv, argv
        try:
            return int(ctl.main() if hasattr(ctl, "main") else 0)
        finally:
            sys.argv = old_argv

    # Cloud-canonical path.
    return _cloud_stage(task_dir, stage)


def _cloud_stage(task_dir: Path, stage: str) -> int:
    from hooks.cloud.config import load as load_config  # type: ignore[import-not-found]
    from hooks.cloud import keychain, client as _client, dispatch as _dispatch  # type: ignore[import-not-found]

    cfg = load_config(task_dir.parent.parent)
    jwt = keychain.get_secret(keychain.SESSION_JWT)
    fp = keychain.get_secret(keychain.SESSION_KEY_FINGERPRINT)
    if not jwt or not fp:
        print("cloud enabled but not logged in — run `dynos login`", file=sys.stderr)
        return 2

    # MVP: the driver just boots a watch loop for the task. A more
    # integrated implementation would tie this to the Claude Code skill
    # dispatcher.
    task_id = task_dir.name.replace("task-", "")
    state = _client.ClientState(task_id=task_id, session_key_fingerprint=fp, config=cfg)
    state.context = _dispatch.DispatchContext(task_dir.parent.parent, cfg)  # type: ignore[attr-defined]

    stop = asyncio.Event()
    try:
        asyncio.run(_client.run_client(state, _dispatch.dispatch, stop))
    except KeyboardInterrupt:
        stop.set()
    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="dynos-driver")
    p.add_argument("task_dir", type=Path)
    p.add_argument("--stage", default="")
    args = p.parse_args()
    return route_stage(args.task_dir.resolve(), args.stage)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
