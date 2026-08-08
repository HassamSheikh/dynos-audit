"""
Canonical JSON + Ed25519 signing for plugin receipts.

Matches the TypeScript `src/core/hashing.ts::canonicalJson` and the
Go runner's `internal/sign/canonical.go` byte-for-byte, so signatures
produced here verify on the cloud side and vice versa.

Rules (RFC 8785-ish, restricted):
    - Object keys sorted lexicographically (UTF-16 code-unit order).
    - No whitespace.
    - Strings JSON-escaped (standard, via json.dumps with ensure_ascii=False).
    - Integers without exponent; floats with Python's default repr.
    - `None`-valued keys are dropped (matches JS `undefined` behavior).
    - Non-finite numbers (NaN, +/-Infinity) raise ValueError.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)

# ─────────────────────── canonical JSON ──


def canonical_json(value: Any) -> str:
    """Produce a deterministic JSON string matching TypeScript canonicalJson."""
    return _encode(value)


def _encode(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"canonical_json: non-finite number {v}")
        # Fall back to JSON.stringify-equivalent for floats.
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, bytes):
        return json.dumps(v.decode("utf-8", errors="replace"), ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_encode(x) for x in v) + "]"
    if isinstance(v, dict):
        items = []
        for k in sorted(v.keys()):
            val = v[k]
            if val is None:
                continue
            items.append(json.dumps(k, ensure_ascii=False, separators=(",", ":")) + ":" + _encode(val))
        return "{" + ",".join(items) + "}"
    raise TypeError(f"canonical_json: unsupported type {type(v).__name__}")


# ─────────────────────── hashing ──


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_payload(obj: Any) -> str:
    """Produce sha256:<hex> over the canonical JSON of obj."""
    return "sha256:" + sha256_hex(canonical_json(obj))


# ─────────────────────── session keys ──


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def export_private_key_seed(priv: Ed25519PrivateKey) -> bytes:
    """Export the 32-byte raw seed. Keychain stores this, nothing else."""
    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def export_public_key_raw(pub: Ed25519PublicKey) -> bytes:
    """Export the 32-byte raw public key."""
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


def public_key_fingerprint(pub: Ed25519PublicKey | bytes) -> str:
    """Matches TS `keyFingerprint(pubkey)` — sha256 hex of raw pubkey bytes."""
    raw = pub if isinstance(pub, bytes) else export_public_key_raw(pub)
    return sha256_hex(raw)


def load_private_key_seed(seed: bytes) -> Ed25519PrivateKey:
    if len(seed) != 32:
        raise ValueError(f"expected 32-byte seed, got {len(seed)}")
    return Ed25519PrivateKey.from_private_bytes(seed)


# ─────────────────────── signing ──


def sign_canonical(priv: Ed25519PrivateKey, body: Any) -> str:
    """Canonicalize + sign + return base64 signature."""
    payload = canonical_json(body).encode("utf-8")
    sig = priv.sign(payload)
    return base64.b64encode(sig).decode("ascii")


def verify_canonical(pub: Ed25519PublicKey, body: Any, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        pub.verify(
            base64.b64decode(signature_b64),
            canonical_json(body).encode("utf-8"),
        )
        return True
    except InvalidSignature:
        return False


def sign_receipt(priv: Ed25519PrivateKey, receipt_body: dict[str, Any], key_id: str) -> dict[str, Any]:
    """Mutate-free: returns a new dict with `signature` filled.

    Matches the TypeScript `signReceipt` — we sign over the receipt body
    with `signature` omitted. The key_id here is the fingerprint string.
    """
    base = {k: v for k, v in receipt_body.items() if k != "signature"}
    sig = sign_canonical(priv, base)
    return {
        **base,
        "signature": {
            "alg": "ed25519",
            "key_id": key_id,
            "signature_b64": sig,
        },
    }


# ─────────────────────── repo Merkle root ──


def repo_hash(
    root: Path,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Compute a deterministic working-tree hash.

    Returns `(repo_hash, [(path, file_hash), ...])`. Walks `root` for
    regular files, skipping exclude_globs (applied to relative paths).
    `.git/` and `.dynos/` are always excluded by default.

    Globs support `**` (any number of path segments, including zero)
    and `*` (any chars except `/`). Matching is a local implementation
    because stdlib `fnmatch` does not treat `**` specially.

    `repo_hash` = sha256 of the sorted "path\\0hash\\n" concatenation.
    """
    inc = include_globs if include_globs else ["**"]
    exc = list(exclude_globs or []) + [".git/**", ".dynos/**", "node_modules/**"]

    entries: list[tuple[str, str]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(_glob_match(rel, g) for g in exc):
            continue
        if not any(_glob_match(rel, g) for g in inc):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        entries.append((rel, sha256_hex(data)))

    entries.sort(key=lambda e: e[0])
    combined = "\n".join(f"{path}\x00{h}" for path, h in entries).encode("utf-8")
    return sha256_hex(combined), entries


def _glob_match(path: str, pattern: str) -> bool:
    """Match a POSIX-style relative path against a glob supporting
    `**` (any number of segments, possibly zero) and `*` (any chars
    except `/`). Kept minimal and self-contained."""
    import re

    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # `**/` and `**`: anything incl. slashes; also match zero segments.
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.fullmatch("".join(out), path) is not None
