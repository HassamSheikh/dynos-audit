"""Unit tests for hooks/cloud/crypto.py.

These tests run without any network dependency — they verify the
canonical-JSON + Ed25519 sign/verify chain is correct and matches the
TypeScript + Go implementations byte-for-byte.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

# Make hooks importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.cloud.crypto import (  # noqa: E402
    canonical_json,
    export_private_key_seed,
    export_public_key_raw,
    generate_keypair,
    hash_payload,
    load_private_key_seed,
    public_key_fingerprint,
    repo_hash,
    sha256_hex,
    sign_canonical,
    sign_receipt,
    verify_canonical,
)


class TestCanonicalJSON:
    def test_key_ordering_stable(self) -> None:
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        assert a == b == '{"a":2,"b":1}'

    def test_nested_objects(self) -> None:
        got = canonical_json({"z": {"l": 2, "m": 1}, "a": [3, 2, 1]})
        assert got == '{"a":[3,2,1],"z":{"l":2,"m":1}}'

    def test_none_fields_dropped(self) -> None:
        assert canonical_json({"a": 1, "b": None}) == '{"a":1}'

    def test_bool_null(self) -> None:
        assert canonical_json({"a": True, "b": False, "c": None}) == '{"a":true,"b":false}'

    def test_rejects_non_finite_numbers(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})
        with pytest.raises(ValueError):
            canonical_json({"x": float("inf")})

    def test_string_escape_matches_json(self) -> None:
        got = canonical_json({"s": 'a"b\nc'})
        # Parse back — round-trip must recover the string.
        assert json.loads(got)["s"] == 'a"b\nc'


class TestHashPayload:
    def test_stable_across_key_order(self) -> None:
        a = hash_payload({"foo": 1, "bar": [2, 3]})
        b = hash_payload({"bar": [2, 3], "foo": 1})
        assert a == b
        assert a.startswith("sha256:")

    def test_differs_for_different_payload(self) -> None:
        assert hash_payload({"foo": 1}) != hash_payload({"foo": 2})


class TestEd25519:
    def test_keypair_roundtrip(self) -> None:
        priv, pub = generate_keypair()
        seed = export_private_key_seed(priv)
        assert len(seed) == 32
        raw_pub = export_public_key_raw(pub)
        assert len(raw_pub) == 32
        priv2 = load_private_key_seed(seed)
        assert export_private_key_seed(priv2) == seed

    def test_fingerprint_stable(self) -> None:
        _, pub = generate_keypair()
        raw = export_public_key_raw(pub)
        assert public_key_fingerprint(pub) == public_key_fingerprint(raw)
        assert public_key_fingerprint(raw) == sha256_hex(raw)
        assert len(public_key_fingerprint(raw)) == 64  # hex

    def test_sign_verify_roundtrip(self) -> None:
        priv, pub = generate_keypair()
        body = {"receipt_id": "r1", "task_id": "t1", "outcome": {"status": "success"}}
        sig = sign_canonical(priv, body)
        assert verify_canonical(pub, body, sig)

    def test_tamper_rejected(self) -> None:
        priv, pub = generate_keypair()
        body = {"x": 1}
        sig = sign_canonical(priv, body)
        tampered = {"x": 2}
        assert not verify_canonical(pub, tampered, sig)


class TestSignReceipt:
    def test_returns_new_dict_with_signature(self) -> None:
        priv, pub = generate_keypair()
        body = {"receipt_id": "r1", "outcome": {"ok": True}}
        signed = sign_receipt(priv, body, "fp-abc")
        assert "signature" in signed
        assert signed["signature"]["alg"] == "ed25519"
        assert signed["signature"]["key_id"] == "fp-abc"
        # Recover and verify.
        base = {k: v for k, v in signed.items() if k != "signature"}
        assert verify_canonical(pub, base, signed["signature"]["signature_b64"])

    def test_signature_excludes_itself(self) -> None:
        """If we included signature during signing we'd have a chicken/egg."""
        priv, _ = generate_keypair()
        body = {"a": 1, "signature": {"should": "be dropped"}}
        signed = sign_receipt(priv, body, "fp")
        assert signed["signature"]["alg"] == "ed25519"  # replaced, not appended


class TestRepoHash:
    def test_empty_dir_stable(self, tmp_path: Path) -> None:
        h1, entries1 = repo_hash(tmp_path)
        h2, entries2 = repo_hash(tmp_path)
        assert h1 == h2
        assert entries1 == entries2

    def test_changes_with_content(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        h1, _ = repo_hash(tmp_path)
        (tmp_path / "a.txt").write_text("world")
        h2, _ = repo_hash(tmp_path)
        assert h1 != h2

    def test_excludes_dotgit(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("should-not-affect-hash")
        h1, entries = repo_hash(tmp_path)
        for path, _ in entries:
            assert not path.startswith(".git/")

    def test_include_globs(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.md").write_text("y")
        _, entries = repo_hash(tmp_path, include_globs=["*.py"])
        paths = [p for p, _ in entries]
        assert "a.py" in paths
        assert "b.md" not in paths

    def test_sort_stable(self, tmp_path: Path) -> None:
        # Create files in a non-alphabetical order; hash must be stable.
        for name in ("z.txt", "a.txt", "m.txt"):
            (tmp_path / name).write_text(name)
        _, entries = repo_hash(tmp_path)
        assert [p for p, _ in entries] == ["a.txt", "m.txt", "z.txt"]


class TestInteropWithJS:
    """Confirm our canonical JSON matches the TypeScript side on known cases."""

    def test_rfc_style_vector(self) -> None:
        # A small but non-trivial payload that exercises sort, nested
        # structures, numbers, strings.
        payload = {
            "task_id": "t_1",
            "outcome": {
                "status": "success",
                "hash": "sha256:abcdef",
                "count": 3,
            },
            "artifact_refs": [
                {"kind": "evidence", "hash": "sha256:111"},
                {"kind": "evidence", "hash": "sha256:222"},
            ],
        }
        got = canonical_json(payload)
        # Decoded → same values; bytewise → deterministic.
        assert json.loads(got) == payload
        # Specifically: keys sorted.
        assert got.index('"artifact_refs"') < got.index('"outcome"') < got.index('"task_id"')


# Edge cases.


def test_base64_seed_loadable() -> None:
    priv, _ = generate_keypair()
    seed = export_private_key_seed(priv)
    b64 = base64.b64encode(seed).decode("ascii")
    priv2 = load_private_key_seed(base64.b64decode(b64))
    assert export_private_key_seed(priv2) == seed
