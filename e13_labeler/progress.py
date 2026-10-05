"""
Progress and ETA (requirements FR-35).

Per batch: how many items have 0, 1, 2 or more human labels, how many have
reached their target (the batch's overlap_target, or the reliability-subset
target), and an ETA from the labelling pace. Per labeler: done today and in
total, and median active time (accounts.labeler_stats).

Labels here are what the queue counts toward a target: human, not skipped, not
gold probes, one per labeler (first versions; edits don't add labels).
"""

import sqlite3
from datetime import timedelta
from typing import Optional

from .accounts import labeler_stats
from .auth import iso, utcnow


def _pace_per_hour(conn: sqlite3.Connection, batch_id: int) -> tuple[Optional[float], str]:
    """Labels per hour over the last 24 hours, else the last 7 days."""
    for hours, window in ((24, "24h"), (24 * 7, "7d")):
        n = conn.execute(
            """SELECT COUNT(*) FROM annotations a JOIN labelers l ON l.id = a.labeler_id
               WHERE a.batch_id = ? AND a.version = 1 AND a.skipped_code IS NULL AND a.is_gold_probe = 0
                 AND l.kind = 'human' AND a.created_at >= datetime('now', ?)""",
            (batch_id, f"-{hours} hours")).fetchone()[0]
        if n:
            return n / hours, window
    return None, "none"


def batch_progress(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
    rows = conn.execute(
        """SELECT bi.item_id, COALESCE(bi.target, b.overlap_target) AS target,
                  (SELECT COUNT(DISTINCT a.labeler_id) FROM annotations a JOIN labelers h ON h.id = a.labeler_id
                   WHERE a.item_id = bi.item_id AND a.batch_id = b.id AND a.skipped_code IS NULL
                     AND a.is_gold_probe = 0 AND h.kind = 'human') AS n
           FROM batch_items bi JOIN batches b ON b.id = bi.batch_id WHERE b.id = ?""", (batch["id"],)).fetchall()
    by_count = {"0": 0, "1": 0, "2": 0, "3+": 0}
    complete = remaining = labels = 0
    for r in rows:
        by_count[str(r["n"]) if r["n"] < 3 else "3+"] += 1
        complete += r["n"] >= r["target"]
        remaining += max(0, r["target"] - r["n"])
        labels += min(r["n"], r["target"])
    pace, window = _pace_per_hour(conn, batch["id"])
    eta_hours = None if not remaining or not pace else remaining / pace
    return {
        "name": batch["name"], "status": batch["status"], "n_items": len(rows),
        "items_by_label_count": by_count, "n_complete": complete,
        "pct_complete": round(complete / len(rows), 4) if rows else 0.0,
        "labels_done": labels, "labels_remaining": remaining,
        "pace_per_hour": None if pace is None else round(pace, 3), "pace_window": window,
        "eta_hours": None if eta_hours is None else round(eta_hours, 1),
        "eta_at": None if eta_hours is None else iso(utcnow() + timedelta(hours=eta_hours)),
    }


def progress(conn: sqlite3.Connection) -> dict:
    batches = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    labelers = conn.execute("SELECT * FROM labelers WHERE kind = 'human' ORDER BY id").fetchall()
    return {
        "batches": [batch_progress(conn, b) for b in batches],
        "labelers": [{"pseudonym": l["pseudonym"], "status": l["status"], **labeler_stats(conn, l["id"])}
                     for l in labelers],
    }
