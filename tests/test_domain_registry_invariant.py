"""Guards the closed-vocabulary invariant behind domain-conditional auditing.

The audit roster is chosen deterministically in
`router.build_audit_plan`: for each domain on the task's classification it
appends `_DEFAULT_AUDITOR_REGISTRY["domain_conditional"][domain]`. That lookup
is `dict.get(domain, [])`, so a domain with no registry entry silently adds
zero auditors — no error, no log. The only reason that can't happen today is a
structural invariant nobody enforces:

    set(lib_core.VALID_DOMAINS) == set(domain_conditional.keys())

`VALID_DOMAINS` is the closed vocabulary the classifier is allowed to emit
(hard-enforced in `_persist_classification` / `lib_validate`), so as long as it
matches the registry keys exactly, every legal domain maps to a non-empty
auditor list and every registry key is reachable. Add a domain to one side but
not the other and coverage drifts silently:

  - a new VALID_DOMAIN with no registry entry -> that domain runs only the
    `always` auditors, quietly under-covered;
  - a new registry key that isn't a VALID_DOMAIN -> a dead entry that can never
    be selected (a typo like "databse" would look wired-up but never fire).

If a valid domain is *intentionally* covered entirely by the `always` roster,
express that by giving it an explicit empty-list entry in `domain_conditional`
rather than omitting the key — that keeps the decision visible and this
invariant honest.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))

import router  # noqa: E402
from lib_core import VALID_DOMAINS  # noqa: E402


def _domain_conditional_keys() -> set[str]:
    return set(router._DEFAULT_AUDITOR_REGISTRY["domain_conditional"].keys())


def test_domain_conditional_keys_match_valid_domains_exactly() -> None:
    """Every legal domain has a registry entry and vice versa."""
    valid = set(VALID_DOMAINS)
    registry_keys = _domain_conditional_keys()

    missing_from_registry = valid - registry_keys
    dead_registry_keys = registry_keys - valid

    assert not missing_from_registry, (
        "VALID_DOMAINS entries with no domain_conditional registry entry "
        "(these domains would silently run only the `always` roster): "
        f"{sorted(missing_from_registry)}. Add an entry in "
        "router._DEFAULT_AUDITOR_REGISTRY['domain_conditional'] (use an "
        "explicit empty list if `always` is intended to cover it)."
    )
    assert not dead_registry_keys, (
        "domain_conditional registry keys that are not in VALID_DOMAINS "
        "(dead entries that can never be selected — likely a typo): "
        f"{sorted(dead_registry_keys)}. Add them to lib_core.VALID_DOMAINS "
        "or remove them from the registry."
    )


def test_domain_conditional_lists_reference_real_auditor_names() -> None:
    """Registry auditor lists must be non-null lists of non-empty strings.

    A malformed entry (None, a bare string, an empty name) would make the
    per-domain append in build_audit_plan a no-op or crash; assert the shape
    so the invariant above is checked against well-formed data.
    """
    domain_map = router._DEFAULT_AUDITOR_REGISTRY["domain_conditional"]
    for domain, auditors in domain_map.items():
        assert isinstance(auditors, list), (
            f"domain_conditional[{domain!r}] must be a list, got {type(auditors)}"
        )
        for name in auditors:
            assert isinstance(name, str) and name, (
                f"domain_conditional[{domain!r}] contains a non-string/empty "
                f"auditor name: {name!r}"
            )
