"""
Offline queue — SQLite-backed durable buffer for receipts that couldn't
be delivered (cloud unreachable, socket dead mid-send). Also tracks
`last_seen_seq` per task so reconnecting clients can resume cleanly.

Every entry is idempotent under `(task_id, instruction_id)` on the cloud
side, so replay after long disconnects is safe.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    instruction_id TEXT NOT NULL,
    frame_json TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS ix_outbox_task ON outbox(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_instruction ON outbox(task_id, instruction_id);

CREATE TABLE IF NOT EXISTS task_cursor (
    task_id TEXT PRIMARY KEY,
    last_seen_seq INTEGER NOT NULL DEFAULT 0
);
"""


class OfflineQueue:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def last_seen_seq(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_seen_seq FROM task_cursor WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def record_seq(self, task_id: str, seq: int) -> None:
        self._conn.execute(
            "INSERT INTO task_cursor(task_id, last_seen_seq) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET last_seen_seq = MAX(task_cursor.last_seen_seq, excluded.last_seen_seq)",
            (task_id, seq),
        )

    def enqueue_receipt(
        self,
        task_id: str,
        instruction_id: str,
        frame: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO outbox(task_id, instruction_id, frame_json) VALUES (?, ?, ?)",
            (task_id, instruction_id, json.dumps(frame)),
        )

    def drain(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            rows = self._conn.execute(
                "SELECT id, frame_json FROM outbox WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT id, frame_json FROM outbox ORDER BY id").fetchall()
        return [{"id": r[0], **json.loads(r[1])} for r in rows]

    def ack(self, outbox_id: int) -> None:
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))

    def close(self) -> None:
        self._conn.close()
