"""Batch lifecycle (requirements FR-31): draft -> open -> closed."""

import sqlite3

from .db import audit

STATUSES = ("draft", "open", "closed")


def set_status(conn: sqlite3.Connection, name: str, status: str, actor_id=None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    batch = conn.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()
    if not batch:
        raise ValueError(f"no batch named {name!r}")
    if status == "open" and batch["overlap_target"] < 2:
        raise ValueError("a batch can't open with overlap_target < 2")
    conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch["id"]))
    audit(conn, actor_id, f"batch_{status}", name)
