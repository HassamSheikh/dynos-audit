"""Shared project-root discrimination for `.dynos` ancestor walks.

The directory name ``.dynos`` is overloaded: it names BOTH the framework home
(``~/.dynos`` or ``$DYNOS_HOME``) and each project's control plane
(``<repo>/.dynos``). Every governance decision resolves a "project root" by
walking upward from the current path and stopping at the first ancestor that
contains a ``.dynos`` directory.

That bare-name match has two failure modes, both observed in the field:

  1. The framework home itself. ``~/.dynos`` always exists, so a walk from any
     folder under ``$HOME`` climbs to ``$HOME`` and treats the *entire home
     directory* as one governed project — pulling every unrelated repo into
     the write-boundary policy.

  2. Stray / orphan control planes. A one-off session started directly in a
     parent directory (e.g. ``~/code``) leaves a ``~/code/.dynos`` behind. That
     orphan then captures every sibling repository beneath it.

This module is the single discriminator all ancestor walks use so those two
cases are rejected consistently. It depends only on the standard library — it
must never import another hooks module, because it is imported by the
lowest-level policy code (``write_policy``) and a cycle there would break the
pre-tool-use hook.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path


@functools.lru_cache(maxsize=1)
def dynos_home() -> Path:
    """Resolve the framework home (``$DYNOS_HOME`` or ``~/.dynos``).

    Cached for the lifetime of the (short-lived) hook process; the environment
    does not change mid-invocation.
    """
    raw = os.environ.get("DYNOS_HOME")
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:
            pass
    return (Path.home() / ".dynos").resolve()


@functools.lru_cache(maxsize=1)
def _registered_project_roots() -> frozenset[str] | None:
    """Resolved path strings of every registered project root.

    Returns ``None`` (not an empty set) when the registry cannot be read, so
    callers can distinguish "no projects registered" from "registry unknown"
    and fall back to on-disk evidence instead of silently de-governing.
    """
    try:
        raw = (dynos_home() / "registry.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    roots: set[str] = set()
    try:
        for entry in data.get("projects", []) or []:
            for rec in entry.get("paths", []) or []:
                path = rec.get("path")
                if not path:
                    continue
                try:
                    roots.add(str(Path(path).resolve()))
                except Exception:
                    continue
    except Exception:
        return None
    return frozenset(roots)


# Sub-directories that only appear once a project has done real dynos work.
# A stray orphan `.dynos` left by a one-off session (its contents are limited
# to orchestrator-session.json, dashboard*, routing-context.json, automation/)
# has none of these, so it stays rejected; a legitimate project that predates
# its registry entry is recognised by them.
_PROJECT_STATE_DIRS = frozenset({"investigations"})


def _has_project_state(dynos_dir: Path) -> bool:
    """True when the control plane carries on-disk evidence of a real project:
    a ``task-*`` directory or another recognised project-state directory."""
    try:
        for child in dynos_dir.iterdir():
            name = child.name
            if name.startswith("task-") and child.is_dir():
                return True
            if name in _PROJECT_STATE_DIRS and child.is_dir():
                return True
    except Exception:
        return False
    return False


def is_project_dynos_dir(dynos_dir: Path) -> bool:
    """Return True iff ``dynos_dir`` legitimately governs its parent directory.

    A ``.dynos`` directory governs its parent only when it is a real project
    control plane — NOT the framework home, and NOT a stray orphan that would
    otherwise capture unrelated sibling repositories.

    A directory qualifies when it is not the framework home AND either:
      * its parent is a registered project root (the authoritative signal), or
      * it carries on-disk task state (the fallback used when the registry is
        unreadable or a legitimate project predates its registration).

    An orphan such as ``~/code/.dynos`` — unregistered and taskless — is
    rejected, so it no longer hijacks the repositories beneath it.
    """
    try:
        if not dynos_dir.is_dir():
            return False
        resolved = dynos_dir.resolve()
    except Exception:
        return False
    # Hard exclusion: the framework home is never a project control plane.
    if resolved == dynos_home():
        return False
    registered = _registered_project_roots()
    if registered is not None and str(resolved.parent) in registered:
        return True
    return _has_project_state(resolved)
