"""
Cloud config loader.

Resolution order (later wins):
    1. ~/.dynos/config.toml
    2. ./.dynos/cloud.toml (project-local override)
    3. environment variables (DYNOS_CLOUD_*)

Everything is optional. If `cloud.enabled` is not true by any of these,
the driver routes to local-canonical mode and NOTHING in this module
is invoked.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]  # py 3.11+
except ImportError:  # pragma: no cover — py 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]


DEFAULT_API_URL = "https://api.dynos.work"
DEFAULT_WS_SCHEME = "wss"


@dataclass(frozen=True, slots=True)
class Config:
    enabled: bool
    api_url: str
    ws_url: str | None  # overrides api_url scheme if set
    tenant_id: str | None  # usually resolved from the JWT
    device_name: str
    offline_queue_path: Path
    cache_dir: Path
    log_level: str

    @property
    def derived_ws_url(self) -> str:
        """Return the WS URL to connect to. Overrides > api_url upgrade."""
        if self.ws_url:
            return self.ws_url
        if self.api_url.startswith("https://"):
            return "wss://" + self.api_url[len("https://"):]
        if self.api_url.startswith("http://"):
            return "ws://" + self.api_url[len("http://"):]
        return DEFAULT_WS_SCHEME + "://" + self.api_url


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge — src wins on conflicts."""
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def load(project_root: Path | None = None) -> Config:
    user_cfg = _read_toml(Path.home() / ".dynos" / "config.toml")
    project_cfg: dict[str, Any] = {}
    if project_root:
        project_cfg = _read_toml(project_root / ".dynos" / "cloud.toml")

    merged: dict[str, Any] = {}
    _deep_merge(merged, user_cfg)
    _deep_merge(merged, project_cfg)

    cloud = merged.get("cloud", {}) if isinstance(merged.get("cloud"), dict) else {}

    # Env overrides.
    enabled_env = os.environ.get("DYNOS_CLOUD_ENABLED")
    if enabled_env is not None:
        cloud["enabled"] = enabled_env.lower() in ("1", "true", "yes", "on")
    api_env = os.environ.get("DYNOS_CLOUD_API_URL")
    if api_env:
        cloud["api_url"] = api_env
    ws_env = os.environ.get("DYNOS_CLOUD_WS_URL")
    if ws_env:
        cloud["ws_url"] = ws_env

    home_dynos = Path.home() / ".dynos"
    return Config(
        enabled=bool(cloud.get("enabled", False)),
        api_url=str(cloud.get("api_url", DEFAULT_API_URL)),
        ws_url=cloud.get("ws_url"),
        tenant_id=cloud.get("tenant_id"),
        device_name=str(cloud.get("device_name", os.uname().nodename if hasattr(os, "uname") else "dev")),
        offline_queue_path=Path(cloud.get("offline_queue_path", home_dynos / "offline-queue.sqlite3")),
        cache_dir=Path(cloud.get("cache_dir", home_dynos / "cache")),
        log_level=str(cloud.get("log_level", "info")),
    )


def is_enabled(project_root: Path | None = None) -> bool:
    """Fast-path check — lets the driver decide routing without a full load."""
    try:
        return load(project_root).enabled
    except Exception:
        return False
