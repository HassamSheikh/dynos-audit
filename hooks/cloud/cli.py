"""
`dynos` CLI.

Subcommands:
    dynos login              Register a session key with the cloud
    dynos logout             Revoke the session key
    dynos status             Show config + session state
    dynos config             Read / write ~/.dynos/config.toml
    dynos task create <msg>  Create a cloud task (mostly for testing)
    dynos task watch <id>    Stream coordinator events for a task

Reads the same config the driver uses. When cloud.enabled=false, every
command that requires a session bails with a clear message.
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import urllib.request
from pathlib import Path

from .config import load as load_config, Config
from .crypto import (
    export_private_key_seed,
    export_public_key_raw,
    generate_keypair,
    load_private_key_seed,
    public_key_fingerprint,
    sign_canonical,
)
from . import keychain


def _request(
    cfg: Config,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    auth: bool = True,
) -> tuple[int, dict | None]:
    url = cfg.api_url.rstrip("/") + path
    headers = {"content-type": "application/json"}
    if auth:
        jwt = keychain.get_secret(keychain.SESSION_JWT)
        if jwt:
            headers["authorization"] = f"Bearer {jwt}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.request.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = None
        return e.code, payload
    except Exception as e:
        print(f"request failed: {e}", file=sys.stderr)
        return 0, None


# ─────────────────────── login ──


def cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.enabled:
        print(
            "cloud.enabled is false — dynos will stay in local-only mode. "
            "Run `dynos config set cloud.enabled true` to enable.",
            file=sys.stderr,
        )
        return 2

    # Reuse an existing keypair if present; generate otherwise.
    seed_b64 = keychain.get_secret(keychain.SESSION_PRIVATE_KEY)
    pub_b64 = keychain.get_secret(keychain.SESSION_PUBLIC_KEY)

    if not seed_b64 or not pub_b64:
        priv, pub = generate_keypair()
        seed = export_private_key_seed(priv)
        pub_raw = export_public_key_raw(pub)
        keychain.set_secret(keychain.SESSION_PRIVATE_KEY, seed)
        keychain.set_secret(keychain.SESSION_PUBLIC_KEY, pub_raw)
        seed_b64 = base64.b64encode(seed).decode("ascii")
        pub_b64 = base64.b64encode(pub_raw).decode("ascii")

    priv = load_private_key_seed(base64.b64decode(seed_b64))
    pub_bytes = base64.b64decode(pub_b64)
    fingerprint = public_key_fingerprint(pub_bytes)

    # Challenge round trip.
    nonce = secrets.token_hex(16)
    status, ch = _request(cfg, "POST", "/v1/sessions/challenge", body={"nonce": nonce}, auth=False)
    if status != 200 or not ch or "challenge" not in ch:
        print(f"challenge request failed: {status}", file=sys.stderr)
        return 1
    challenge = ch["challenge"]

    # Sign the challenge.
    sig = priv.sign(challenge.encode("utf-8"))
    sig_b64 = base64.b64encode(sig).decode("ascii")

    body = {
        "device_name": cfg.device_name,
        "alg": "ed25519",
        "public_key_b64": pub_b64,
        "challenge": challenge,
        "challenge_sig_b64": sig_b64,
        "tenant_id": args.tenant,
    }
    status, reg = _request(cfg, "POST", "/v1/sessions/", body=body, auth=False)
    if status not in (200, 201) or not reg:
        print(f"registration failed: {status}: {reg}", file=sys.stderr)
        return 1

    keychain.set_secret(keychain.SESSION_JWT, reg["jwt"])
    keychain.set_secret(keychain.SESSION_KEY_ID, reg["session_key_id"])
    keychain.set_secret(keychain.SESSION_KEY_FINGERPRINT, reg["key_fingerprint"])

    print(f"logged in · session_key_id={reg['session_key_id']} fingerprint={fingerprint[:16]}…")
    # The server may have slightly different fingerprint computation;
    # trust the server's reported value going forward.
    if reg["key_fingerprint"] != fingerprint:
        print(
            f"warning: local fingerprint {fingerprint[:16]}… != server {reg['key_fingerprint'][:16]}…",
            file=sys.stderr,
        )
    # Sign-over canonicalization self-check.
    assert sign_canonical(priv, {"a": 1}) == sign_canonical(priv, {"a": 1})
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = load_config()
    session_key_id = keychain.get_secret(keychain.SESSION_KEY_ID)
    if session_key_id:
        _request(cfg, "DELETE", f"/v1/sessions/{session_key_id}")
    for k in (
        keychain.SESSION_JWT,
        keychain.SESSION_KEY_ID,
        keychain.SESSION_KEY_FINGERPRINT,
    ):
        keychain.delete_secret(k)
    if args.keys:
        keychain.delete_secret(keychain.SESSION_PRIVATE_KEY)
        keychain.delete_secret(keychain.SESSION_PUBLIC_KEY)
        print("logged out + forgot keypair")
    else:
        print("logged out · keypair retained for next `dynos login`")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    jwt = keychain.get_secret(keychain.SESSION_JWT)
    fingerprint = keychain.get_secret(keychain.SESSION_KEY_FINGERPRINT)
    session_id = keychain.get_secret(keychain.SESSION_KEY_ID)
    print(f"cloud.enabled      = {cfg.enabled}")
    print(f"cloud.api_url      = {cfg.api_url}")
    print(f"cloud.ws_url       = {cfg.derived_ws_url}")
    print(f"cloud.device_name  = {cfg.device_name}")
    print(f"cloud.cache_dir    = {cfg.cache_dir}")
    print(f"session_jwt        = {'present' if jwt else 'none'}")
    print(f"session_key_id     = {session_id or 'none'}")
    print(f"session_fingerprint= {fingerprint or 'none'}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = Path.home() / ".dynos" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.op == "show":
        if not path.exists():
            print("# (empty)")
            return 0
        print(path.read_text())
        return 0
    if args.op == "set":
        # Simple line-editor: we find the [cloud] section and upsert the key.
        key = args.key or ""
        value = args.value or ""
        if "." not in key:
            print(f"key must be section.name, got {key}", file=sys.stderr)
            return 2
        section, name = key.split(".", 1)
        lines = path.read_text().splitlines() if path.exists() else []

        # Find section start + end.
        start_idx = end_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == f"[{section}]":
                start_idx = i
            elif start_idx >= 0 and line.strip().startswith("[") and i > start_idx:
                end_idx = i
                break
        if start_idx < 0:
            # Append a new section.
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(f"[{section}]")
            start_idx = len(lines) - 1
            end_idx = len(lines)
        if end_idx < 0:
            end_idx = len(lines)

        # Upsert the key.
        value_repr = _toml_literal(value)
        replaced = False
        for i in range(start_idx + 1, end_idx):
            lstrip = lines[i].lstrip()
            if lstrip.startswith(f"{name} ") or lstrip.startswith(f"{name}="):
                lines[i] = f"{name} = {value_repr}"
                replaced = True
                break
        if not replaced:
            lines.insert(end_idx, f"{name} = {value_repr}")

        path.write_text("\n".join(lines) + "\n")
        print(f"set {key} = {value_repr}")
        return 0
    return 2


def _toml_literal(s: str) -> str:
    if s.lower() in ("true", "false"):
        return s.lower()
    try:
        int(s)
        return s
    except ValueError:
        pass
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


# ─────────────────────── argparse ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dynos", description="dynos-work cloud client")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="register a session key with the cloud")
    p_login.add_argument("--tenant", default=None, help="tenant id (required for dev/admin flows)")
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="revoke the current session")
    p_logout.add_argument("--keys", action="store_true", help="also forget the local keypair")
    p_logout.set_defaults(func=cmd_logout)

    p_status = sub.add_parser("status", help="show cloud config + session state")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="read / write ~/.dynos/config.toml")
    cfg_sub = p_config.add_subparsers(dest="op", required=True)
    cfg_sub.add_parser("show")
    p_set = cfg_sub.add_parser("set")
    p_set.add_argument("key", help="dotted key, e.g. cloud.enabled")
    p_set.add_argument("value")
    p_config.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
