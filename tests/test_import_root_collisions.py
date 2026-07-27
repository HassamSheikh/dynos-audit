"""No two import roots may expose the same top-level module name.

`tests/conftest.py` puts `hooks/` and `memory/` on `sys.path`; every
debug-module test puts `debug-module/` on it. Python caches by name, not by
path, so if two roots both expose `lib`, the first one imported wins for the
whole process and the second is unreachable.

That is exactly what used to happen: `hooks/lib.py` (a re-export facade) and
`debug-module/lib/` (a package) shared the name. Running either suite alone
was fine. Running both in one pytest process meant a `tests/` case imported
`lib` first, `sys.modules["lib"]` pinned to the facade, and all 124
debug-module cases that did `from lib import <step>` failed with a confusing
`ImportError: cannot import name ... from 'lib'`. debug-module's package is
now `debuglib`.

This test is the ratchet. A new collision fails here rather than as a wall
of unrelated-looking import errors in whichever suite loses the race.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Roots that end up on sys.path during a test run.
IMPORT_ROOTS = ("hooks", "memory", "debug-module")

# Pre-existing hooks<->memory collisions, frozen as of the debuglib rename.
# These are latent, not active: tests/conftest.py inserts hooks/ after
# memory/, so hooks wins, and nothing currently needs the memory copy under
# these names. They are recorded rather than silently tolerated — shrinking
# this set is good, growing it means a new module is unreachable from one of
# the two roots.
KNOWN_COLLISIONS: frozenset[tuple[str, str, str]] = frozenset({
    ("hooks", "memory", "agent_generator"),
    ("hooks", "memory", "lib_qlearn"),
    ("hooks", "memory", "postmortem"),
    ("hooks", "memory", "postmortem_analysis"),
    ("hooks", "memory", "postmortem_improve"),
})


def _top_level_names(root: Path) -> set[str]:
    """Importable top-level module and package names directly under *root*."""
    names: set[str] = set()
    if not root.is_dir():
        return names
    for path in root.iterdir():
        if path.name.startswith((".", "_")) or path.name == "__pycache__":
            continue
        if path.suffix == ".py":
            names.add(path.stem)
        elif path.is_dir() and (path / "__init__.py").is_file():
            names.add(path.name)
    return names


def _collisions() -> set[tuple[str, str, str]]:
    by_root = {r: _top_level_names(ROOT / r) for r in IMPORT_ROOTS}
    found: set[tuple[str, str, str]] = set()
    for i, a in enumerate(IMPORT_ROOTS):
        for b in IMPORT_ROOTS[i + 1:]:
            for name in by_root[a] & by_root[b]:
                found.add((a, b, name))
    return found


def test_import_roots_are_populated() -> None:
    """Guard against this module passing vacuously on a bad path."""
    for root in IMPORT_ROOTS:
        assert _top_level_names(ROOT / root), f"no modules found under {root}/"


def test_no_new_import_root_collisions() -> None:
    new = _collisions() - KNOWN_COLLISIONS
    assert not new, (
        "new top-level module name collision between import roots:\n"
        + "\n".join(f"  {a}/ and {b}/ both expose {name!r}" for a, b, name in sorted(new))
        + "\n\nBoth roots go on sys.path during a combined test run, and Python "
        "caches by name — whichever is imported first wins for the entire "
        "process and the other becomes unreachable. Rename one, or add it to "
        "KNOWN_COLLISIONS with a reason if it is genuinely harmless."
    )


def test_known_collisions_have_not_been_silently_resolved() -> None:
    """If a known collision is fixed, remove it from the list.

    Keeps the allowlist honest — a stale entry would mask a genuine
    regression at that name later.
    """
    stale = KNOWN_COLLISIONS - _collisions()
    assert not stale, (
        "these collisions no longer exist and should be removed from "
        f"KNOWN_COLLISIONS: {sorted(stale)}"
    )


def test_debug_module_does_not_collide_with_the_main_suite() -> None:
    """The specific regression that broke the combined run.

    Stated separately from the general rule so the failure names the cause
    directly rather than pointing at an allowlist.
    """
    offenders = {
        (a, b, name) for a, b, name in _collisions()
        if "debug-module" in (a, b)
    }
    assert not offenders, (
        "debug-module shares a top-level module name with the main suite's "
        f"import roots: {sorted(offenders)}. Running "
        "`pytest tests/ debug-module/tests/` in one process will fail for "
        "whichever suite imports second."
    )


@pytest.mark.parametrize("name", ["lib", "debuglib"])
def test_lib_and_debuglib_resolve_to_exactly_one_root(name: str) -> None:
    """The two names at the centre of the original break stay unambiguous."""
    owners = [r for r in IMPORT_ROOTS if name in _top_level_names(ROOT / r)]
    assert len(owners) == 1, (
        f"{name!r} is exposed by {owners or 'no root'}; it must be owned by "
        f"exactly one import root"
    )
