"""
dynos-work cloud module.

Dormant by default. Enables when `~/.dynos/config.toml` has
`[cloud] enabled = true` (or env `DYNOS_CLOUD_ENABLED=1`). When active,
the plugin becomes a thin executor for cloud-issued instructions and
writes its local state through the `driver` router (see hooks/driver.py).

Public API:
    from hooks.cloud import is_enabled, config, open_session, client
"""
from __future__ import annotations

from .config import Config, is_enabled, load as load_config
from . import crypto, keychain, client, cas, dispatch, queue

__all__ = [
    "Config",
    "is_enabled",
    "load_config",
    "crypto",
    "keychain",
    "client",
    "cas",
    "dispatch",
    "queue",
]
