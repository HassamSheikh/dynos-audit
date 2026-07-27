"""
Tests for debug-module/debuglib/schema_drift.py — AC17.
"""
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = str(Path(__file__).parent.parent)
if _DEBUG_MODULE_DIR not in sys.path:
    sys.path.insert(0, _DEBUG_MODULE_DIR)


def _import_schema_drift():
    try:
        from debuglib import schema_drift
        return schema_drift
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"schema_drift module not yet implemented: {exc}\n"
            f"Implement debug-module/debuglib/schema_drift.py to make this test pass."
        )


# ---------------------------------------------------------------------------
# No migration framework sentinel → empty list, no exception
# ---------------------------------------------------------------------------

def test_ac17_no_sentinel_returns_empty_list(no_migration_repo):
    """check() on a repo with none of the 4 sentinel files returns empty list."""
    m = _import_schema_drift()
    result = m.check(str(no_migration_repo))
    assert result == [], f"Expected [], got {result!r}"


def test_ac17_no_sentinel_no_exception(no_migration_repo):
    """check() on a repo with no migration framework does not raise."""
    m = _import_schema_drift()
    result = m.check(str(no_migration_repo))
    assert isinstance(result, list)


def test_ac17_nonexistent_path_does_not_raise():
    """check() on a non-existent path does not raise an exception."""
    m = _import_schema_drift()
    result = m.check("/nonexistent/path/xyz")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Alembic sentinel present → framework detected
# ---------------------------------------------------------------------------

def test_ac17_alembic_ini_detected(alembic_repo):
    """check() on a repo with alembic.ini detects 'alembic' as the framework."""
    m = _import_schema_drift()
    result = m.check(str(alembic_repo))
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    if result:
        frameworks = [r.get("framework") for r in result]
        assert "alembic" in frameworks, (
            f"Expected 'alembic' framework in {frameworks}"
        )


def test_ac17_alembic_result_has_framework_key(alembic_repo):
    """Each returned dict has a 'framework' key."""
    m = _import_schema_drift()
    result = m.check(str(alembic_repo))
    for item in result:
        assert "framework" in item, f"Dict missing 'framework': {item}"


def test_ac17_alembic_framework_value_is_alembic(alembic_repo):
    """The framework value in returned dicts is 'alembic'."""
    m = _import_schema_drift()
    result = m.check(str(alembic_repo))
    for item in result:
        assert item["framework"] == "alembic", (
            f"Expected framework='alembic', got {item['framework']!r}"
        )


# ---------------------------------------------------------------------------
# Prisma sentinel
# ---------------------------------------------------------------------------

def test_ac17_prisma_detected(tmp_path):
    """check() on a repo with prisma/schema.prisma detects prisma framework."""
    m = _import_schema_drift()
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "schema.prisma").write_text('datasource db { provider = "postgresql" }')
    result = m.check(str(tmp_path))
    if result:
        frameworks = [r.get("framework") for r in result]
        assert "prisma" in frameworks, (
            f"Expected 'prisma' framework in {frameworks}"
        )


# ---------------------------------------------------------------------------
# Status values
# ---------------------------------------------------------------------------

def test_ac17_status_from_allowed_set(alembic_repo):
    """When status is present, it is from the allowed set."""
    m = _import_schema_drift()
    allowed = {"applied", "pending", "missing", "check-failed"}
    result = m.check(str(alembic_repo))
    for item in result:
        if "status" in item:
            assert item["status"] in allowed, (
                f"Invalid status {item['status']!r}: not in {allowed}"
            )
