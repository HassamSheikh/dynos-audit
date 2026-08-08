"""
Keychain wrapper. Private session keys + JWT refresh tokens live here,
never on disk in plaintext.

Uses `keyring` (cross-platform: macOS Keychain / Windows Credential
Locker / libsecret on Linux). Degrades gracefully to a dev-only file
(`~/.dynos/insecure-keys.json`) if keyring isn't available — tests use
the fallback, and users who explicitly set `cloud.keychain = "file"`
in their config (Linux headless servers) use it too.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Literal

_SERVICE = "dynos-work.cloud"
_FALLBACK_PATH = Path.home() / ".dynos" / "insecure-keys.json"

KeychainMode = Literal["system", "file"]


def _use_file() -> bool:
    """Return True if we should use the file fallback."""
    if os.environ.get("DYNOS_KEYCHAIN", "").lower() == "file":
        return True
    try:
        import keyring  # noqa: F401
    except ImportError:
        return True
    return False


def _read_file_store() -> dict[str, str]:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(_FALLBACK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_file_store(store: dict[str, str]) -> None:
    _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FALLBACK_PATH.write_text(json.dumps(store, indent=2))
    # Tighten perms — dev-only path but still.
    try:
        _FALLBACK_PATH.chmod(0o600)
    except OSError:
        pass


def set_secret(name: str, value: bytes | str) -> None:
    encoded = value if isinstance(value, str) else base64.b64encode(value).decode("ascii")
    if _use_file():
        store = _read_file_store()
        store[name] = encoded
        _write_file_store(store)
        return
    import keyring  # type: ignore[import-not-found]

    keyring.set_password(_SERVICE, name, encoded)


def get_secret(name: str) -> str | None:
    if _use_file():
        store = _read_file_store()
        return store.get(name)
    import keyring  # type: ignore[import-not-found]

    return keyring.get_password(_SERVICE, name)


def get_secret_bytes(name: str) -> bytes | None:
    v = get_secret(name)
    if v is None:
        return None
    try:
        return base64.b64decode(v)
    except (ValueError, TypeError):
        return None


def delete_secret(name: str) -> None:
    if _use_file():
        store = _read_file_store()
        store.pop(name, None)
        _write_file_store(store)
        return
    import keyring  # type: ignore[import-not-found]

    try:
        keyring.delete_password(_SERVICE, name)
    except Exception:
        pass


# ─────────────────────── keys used by the plugin ──
SESSION_PRIVATE_KEY = "session_private_key_seed"  # base64 of 32-byte seed
SESSION_PUBLIC_KEY = "session_public_key_raw"  # base64 of 32-byte pubkey
SESSION_JWT = "session_jwt"  # the short-lived JWT
SESSION_KEY_ID = "session_key_id"  # the cloud session_keys row id
SESSION_KEY_FINGERPRINT = "session_key_fingerprint"  # sha256 hex
