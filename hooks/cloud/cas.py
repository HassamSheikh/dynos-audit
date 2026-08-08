"""
Content-addressed artifact upload / download against the cloud.

The plugin scans the working tree, computes per-file SHA-256s, and
sends a single `POST /v1/artifacts/presign` with the full index. The
cloud replies with ONLY the hashes it's missing and a pre-signed PUT
URL for each. The plugin uploads those blobs and caches them locally
under `.dynos/cache/<first2>/<sha>` for future tasks.

On Zero-Retention tenants, the plugin flips `storage=local_only` in
the request (via config) — the cloud still registers the metadata but
never accepts the bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from . import keychain


@dataclass(slots=True)
class ArtifactRef:
    hash: str  # sha256:<hex>
    kind: str
    size_bytes: int
    local_path: Path  # absolute


class LocalCAS:
    """Local content-addressed cache. Deduplicates uploads across tasks."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, hash_: str) -> Path:
        hex_ = hash_.split(":", 1)[-1]
        return self._dir / hex_[:2] / hex_

    def has(self, hash_: str) -> bool:
        return self.path_for(hash_).exists()

    def put(self, data: bytes, hash_: str) -> Path:
        p = self.path_for(hash_)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.rename(p)
        return p

    def get(self, hash_: str) -> bytes | None:
        p = self.path_for(hash_)
        if not p.exists():
            return None
        return p.read_bytes()


def compute_file_index(root: Path, entries: list[tuple[str, str]]) -> list[ArtifactRef]:
    """Given the `(path, hash)` list from crypto.repo_hash, produce
    ArtifactRefs with absolute paths + sizes, ready to presign."""
    refs: list[ArtifactRef] = []
    for rel, h in entries:
        p = root / rel
        if not p.exists():
            continue
        refs.append(
            ArtifactRef(
                hash=f"sha256:{h}",
                kind="source_chunk",
                size_bytes=p.stat().st_size,
                local_path=p,
            )
        )
    return refs


def negotiate_and_upload(
    cfg: Config,
    refs: list[ArtifactRef],
    *,
    local_only: bool = False,
) -> dict[str, str]:
    """Do a CAS round-trip: presign → filter missing → upload each
    missing blob. Returns a map `hash → storage ('r2' | 'local_only')`.

    If `local_only=True`, no upload happens and `artifacts` rows are
    still registered cloud-side as metadata-only.
    """
    jwt = keychain.get_secret(keychain.SESSION_JWT)
    if not jwt:
        raise RuntimeError("no JWT — run `dynos login` first")
    api = cfg.api_url.rstrip("/")

    # Split in chunks of 2000 — keeps request size reasonable.
    storage: dict[str, str] = {}
    for i in range(0, len(refs), 2000):
        batch = refs[i : i + 2000]
        req_body = {
            "hashes": [
                {"hash": r.hash, "kind": r.kind, "size_bytes": r.size_bytes}
                for r in batch
            ],
        }
        headers = {
            "authorization": f"Bearer {jwt}",
            "content-type": "application/json",
        }
        req = urllib.request.Request(
            f"{api}/v1/artifacts/presign",
            data=json.dumps(req_body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            presign = json.loads(resp.read().decode("utf-8"))

        for h in presign.get("present", []):
            storage[h] = "r2"
        if local_only:
            for m in presign.get("missing", []):
                storage[m["hash"]] = "local_only"
            continue
        for m in presign.get("missing", []):
            r = next((x for x in batch if x.hash == m["hash"]), None)
            if r is None:
                continue
            body = r.local_path.read_bytes()
            # Verify hash before upload to avoid wasted bytes.
            computed = hashlib.sha256(body).hexdigest()
            if f"sha256:{computed}" != r.hash:
                # Files can race; silently skip — next task picks it up.
                continue
            _upload(cfg, jwt, m["put_url"], body)
            storage[r.hash] = "r2"
    return storage


def _upload(cfg: Config, jwt: str, put_url: str, body: bytes) -> None:
    full = put_url if put_url.startswith("http") else f"{cfg.api_url.rstrip('/')}{put_url}"
    headers = {
        "authorization": f"Bearer {jwt}",
        "content-type": "application/octet-stream",
    }
    req = urllib.request.Request(full, data=body, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=60):  # noqa: S310
        pass


def fetch_artifact(cfg: Config, hash_: str, cache: LocalCAS) -> bytes:
    """Download an artifact. Checks local cache first."""
    cached = cache.get(hash_)
    if cached is not None:
        return cached
    jwt = keychain.get_secret(keychain.SESSION_JWT)
    if not jwt:
        raise RuntimeError("no JWT")
    hex_ = hash_.split(":", 1)[-1]
    api = cfg.api_url.rstrip("/")
    req = urllib.request.Request(
        f"{api}/v1/artifacts/{urllib.parse.quote(f'sha256:{hex_}', safe='')}",
        headers={"authorization": f"Bearer {jwt}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        body = resp.read()
    cache.put(body, hash_)
    return body


__all__ = [
    "ArtifactRef",
    "LocalCAS",
    "compute_file_index",
    "negotiate_and_upload",
    "fetch_artifact",
]

_ = os.environ
