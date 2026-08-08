# hooks/cloud/ — dynos-work cloud plane client

This directory is the bridge between the open-source `dynos-work`
plugin and the commercial `cloud-sibyl` foundry. It is **dormant by
default** — every OSS install has this code but nothing in it runs
unless the user enables it.

## How the switch works

The driver (`hooks/driver.py`) checks `~/.dynos/config.toml` and env
vars. If `cloud.enabled = false`, the driver defers to the existing
local-canonical pipeline (`hooks/ctl.py` and friends). The plugin
behaves *identically* to how it did before this module existed.

Flip the switch:

```bash
dynos config set cloud.enabled true
dynos config set cloud.api_url https://cloud.yourcompany.com
dynos login
```

Now `dynos-work:start` etc. route through the cloud coordinator.

## Files

| File | Role |
|---|---|
| `config.py` | Loads `~/.dynos/config.toml` + env overrides. Defines `Config` dataclass. |
| `keychain.py` | OS keychain wrapper (macOS Keychain / Windows Credential Locker / libsecret). File fallback when `DYNOS_KEYCHAIN=file`. |
| `crypto.py` | Canonical JSON + Ed25519 + repo Merkle hash. Matches cloud-sibyl and sibyl-runner byte-for-byte. |
| `client.py` | WebSocket client. Reconnect with backoff + resume by `last_seen_seq`. Signs every receipt. |
| `cas.py` | Content-addressed storage: local cache + presign/upload against `/v1/artifacts/presign`. |
| `dispatch.py` | Instruction handlers: `collect_context`, `run_tests`, `apply_patch`, `gather_evidence`, `request_human_review`, `run_local_validator`, `commit_and_sign`, `fetch_artifact`, `apply_plan_segment`. |
| `queue.py` | SQLite offline queue + per-task `last_seen_seq` cursor. |
| `cli.py` | `dynos login` / `logout` / `status` / `config`. |

## What a receipt looks like

Every instruction the cloud issues produces one receipt. The receipt
is canonical-JSON serialized, Ed25519-signed with the device's session
key, and contains outcome hashes specific to the instruction type.

```python
from hooks.cloud import crypto, keychain
from hooks.cloud.crypto import load_private_key_seed

seed = keychain.get_secret_bytes(keychain.SESSION_PRIVATE_KEY)
priv = load_private_key_seed(seed)

receipt_body = {
    "receipt_id": "rcpt_abc",
    "instruction_id": "inst_xyz",
    "task_id": "t_001",
    "executed_at": "2026-04-19T00:00:00Z",
    "executor_target": "plugin",
    "session_key_fingerprint": keychain.get_secret(keychain.SESSION_KEY_FINGERPRINT),
    "precondition_met": True,
    "outcome": {"exit_code": 0, "stdout_hash": "sha256:..."},
    "artifact_refs": [],
}
signed = crypto.sign_receipt(priv, receipt_body, receipt_body["session_key_fingerprint"])
```

## What never leaves the machine

- The Ed25519 **private key seed** — in the OS keychain only.
- Any file matching `.git/**`, `.dynos/**`, `node_modules/**` — hard-excluded.
- Full source bytes **unless** Zero-Retention is off AND the cloud
  requests them via CAS presign. ZR tenants set `storage_mode =
  local_only` — the cloud records only the hash, never the bytes.

## Tests

```bash
cd /Users/hassam/Documents/dynos-work
python3 -m pytest tests/cloud/ -q
```

39 tests covering canonical JSON stability, Ed25519 sign/verify,
tamper rejection, offline queue idempotency, dispatcher handlers, and
config resolution precedence.
