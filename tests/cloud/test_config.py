"""Tests for hooks/cloud/config.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.cloud.config import Config, is_enabled, load  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("DYNOS_CLOUD_"):
            monkeypatch.delenv(k, raising=False)


def test_default_disabled(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load(project_root=tmp_path)
    assert cfg.enabled is False
    assert cfg.api_url.startswith("https://")


def test_env_override_enables(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DYNOS_CLOUD_ENABLED", "true")
    monkeypatch.setenv("DYNOS_CLOUD_API_URL", "https://cloud.test")
    cfg = load(tmp_path)
    assert cfg.enabled is True
    assert cfg.api_url == "https://cloud.test"


def test_toml_loaded_and_project_override_wins(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".dynos").mkdir()
    (home / ".dynos" / "config.toml").write_text(
        '[cloud]\nenabled = false\napi_url = "https://user"\n'
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".dynos").mkdir()
    (proj / ".dynos" / "cloud.toml").write_text(
        '[cloud]\nenabled = true\napi_url = "https://project"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    cfg = load(proj)
    assert cfg.enabled is True
    assert cfg.api_url == "https://project"


def test_ws_url_derived_from_api(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DYNOS_CLOUD_API_URL", "https://cloud.example.com")
    cfg = load(tmp_path)
    assert cfg.derived_ws_url == "wss://cloud.example.com"


def test_is_enabled_shorthand(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_enabled(tmp_path) is False
    monkeypatch.setenv("DYNOS_CLOUD_ENABLED", "1")
    assert is_enabled(tmp_path) is True
