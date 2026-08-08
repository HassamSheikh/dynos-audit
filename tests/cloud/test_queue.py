"""Tests for hooks/cloud/queue.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.cloud.queue import OfflineQueue  # noqa: E402


def test_last_seen_seq_default_zero(tmp_path: Path) -> None:
    q = OfflineQueue(tmp_path / "q.sqlite3")
    assert q.last_seen_seq("t1") == 0


def test_record_seq_monotonic(tmp_path: Path) -> None:
    q = OfflineQueue(tmp_path / "q.sqlite3")
    q.record_seq("t1", 5)
    assert q.last_seen_seq("t1") == 5
    # Recording a lower seq does not regress.
    q.record_seq("t1", 3)
    assert q.last_seen_seq("t1") == 5
    q.record_seq("t1", 10)
    assert q.last_seen_seq("t1") == 10


def test_enqueue_receipt_idempotent(tmp_path: Path) -> None:
    q = OfflineQueue(tmp_path / "q.sqlite3")
    q.enqueue_receipt("t1", "inst-a", {"type": "receipt", "x": 1})
    q.enqueue_receipt("t1", "inst-a", {"type": "receipt", "x": 2})  # dup → ignored
    drained = q.drain("t1")
    assert len(drained) == 1
    assert drained[0]["x"] == 1


def test_drain_and_ack(tmp_path: Path) -> None:
    q = OfflineQueue(tmp_path / "q.sqlite3")
    q.enqueue_receipt("t1", "inst-a", {"x": 1})
    q.enqueue_receipt("t1", "inst-b", {"x": 2})
    drained = q.drain("t1")
    assert len(drained) == 2
    for row in drained:
        q.ack(row["id"])
    assert q.drain("t1") == []


def test_isolated_per_task(tmp_path: Path) -> None:
    q = OfflineQueue(tmp_path / "q.sqlite3")
    q.enqueue_receipt("t1", "i", {})
    q.enqueue_receipt("t2", "i", {})
    assert len(q.drain("t1")) == 1
    assert len(q.drain("t2")) == 1
    assert len(q.drain(None)) == 2
