"""
Batch lifecycle and labelling design (requirements FR-31, FR-32).

Owner decision, 2026-10-05: "3 is ideal, but we might have to make 1 work."
Three ways to get agreement data, from best to fallback:

1. ``overlap_target`` 2-3: every item gets that many different labelers.
2. ``overlap_target`` 1 with a **reliability subset**: a deterministic sample of
   ``reliability_fraction`` of the items gets ``reliability_overlap`` labelers,
   the rest get one. α comes from the subset; single-labelled items are training
   data only.
3. A **re-label batch** (``relabel_of``): the same labeler labels a sample of
   their own items again, blind, after ``relabel_after_days``. This is the only
   option with a single labeler. It gives intra-rater α, which must be reported
   apart from inter-rater α (and alongside human-vs-committee α, FR-9).
"""

import hashlib
import json
import sqlite3
from typing import Optional

from .db import audit
from .reasons import DEFAULT_SPAN_POLICY, REASONS
from .tiers import TIERS

STATUSES = ("draft", "open", "closed")


def get_batch(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    batch = conn.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()
    if not batch:
        raise ValueError(f"no batch named {name!r}")
    return batch


def ensure_batch(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM batches WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO batches (name, reason_set_json, span_policy_json) VALUES (?, ?, ?)",
        (name, json.dumps(list(REASONS)), json.dumps(DEFAULT_SPAN_POLICY)),
    )
    return cur.lastrowid


def in_reliability_subset(batch_name: str, item_id: str, fraction: float) -> bool:
    """Deterministic sample: the same batch, item and fraction always give the same answer."""
    if fraction <= 0:
        return False
    digest = hashlib.sha256(f"{batch_name}\0{item_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < fraction


def resample(conn: sqlite3.Connection, batch: sqlite3.Row) -> int:
    """Set per-item targets for the reliability subset. Returns the subset size."""
    n = 0
    for (item_id,) in conn.execute("SELECT item_id FROM batch_items WHERE batch_id = ?", (batch["id"],)).fetchall():
        chosen = in_reliability_subset(batch["name"], item_id, batch["reliability_fraction"])
        target = batch["reliability_overlap"] if chosen and batch["reliability_overlap"] > batch["overlap_target"] else None
        conn.execute("UPDATE batch_items SET target = ? WHERE batch_id = ? AND item_id = ?",
                     (target, batch["id"], item_id))
        n += chosen
    return n


def configure(conn: sqlite3.Connection, name: str, actor_id=None, *, overlap_target: Optional[int] = None,
              reliability_fraction: Optional[float] = None, reliability_overlap: Optional[int] = None,
              priority: Optional[int] = None, tier_ceiling: Optional[str] = None,
              require_note: Optional[bool] = None, relabel_after_days: Optional[int] = None) -> dict:
    """Change a batch's labelling design. Returns the new settings and the subset size."""
    batch = get_batch(conn, name)
    changes = {k: v for k, v in {
        "overlap_target": overlap_target, "reliability_fraction": reliability_fraction,
        "reliability_overlap": reliability_overlap, "priority": priority, "tier_ceiling": tier_ceiling,
        "require_note": None if require_note is None else int(require_note),
        "relabel_after_days": relabel_after_days,
    }.items() if v is not None}
    if overlap_target is not None and overlap_target < 1:
        raise ValueError("overlap_target must be at least 1")
    if reliability_fraction is not None and not 0 <= reliability_fraction <= 1:
        raise ValueError("reliability_fraction must be between 0 and 1")
    if reliability_overlap is not None and reliability_overlap < 2:
        raise ValueError("reliability_overlap must be at least 2")
    if tier_ceiling is not None and tier_ceiling not in TIERS:
        raise ValueError(f"tier_ceiling must be one of {', '.join(TIERS)}")
    if relabel_after_days is not None and relabel_after_days < 0:
        raise ValueError("relabel_after_days must be >= 0")
    if batch["status"] == "closed" and changes:
        raise ValueError("a closed batch can't be changed")
    if changes:
        conn.execute(f"UPDATE batches SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                     (*changes.values(), batch["id"]))
        audit(conn, actor_id, "batch_config", name, changes)
    batch = get_batch(conn, name)
    subset = resample(conn, batch) if batch["relabel_of"] is None else 0
    return {**describe(conn, batch), "reliability_subset": subset}


def describe(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
    source = None
    if batch["relabel_of"] is not None:
        source = conn.execute("SELECT name FROM batches WHERE id = ?", (batch["relabel_of"],)).fetchone()["name"]
    n_items = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id = ?", (batch["id"],)).fetchone()[0]
    n_subset = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id = ? AND target IS NOT NULL",
                            (batch["id"],)).fetchone()[0]
    return {
        "name": batch["name"], "status": batch["status"], "task_type": batch["task_type"], "n_items": n_items,
        "overlap_target": batch["overlap_target"], "reliability_fraction": batch["reliability_fraction"],
        "reliability_overlap": batch["reliability_overlap"], "reliability_subset": n_subset,
        "relabel_of": source, "relabel_after_days": batch["relabel_after_days"],
        "tier_ceiling": batch["tier_ceiling"], "priority": batch["priority"],
        "reason_set": json.loads(batch["reason_set_json"]), "require_note": bool(batch["require_note"]),
    }


def create_relabel(conn: sqlite3.Connection, source_name: str, name: str, *, fraction: Optional[float] = None,
                   after_days: int = 7, actor_id=None) -> dict:
    """
    A re-label batch over ``source_name``. Its items are the source's reliability
    subset when it has one, else a deterministic ``fraction`` sample (default: all
    items). Each labeler gets back only items they labelled in the source batch, at
    least ``after_days`` earlier.
    """
    source = get_batch(conn, source_name)
    if source["relabel_of"] is not None:
        raise ValueError("can't re-label a re-label batch")
    if conn.execute("SELECT 1 FROM batches WHERE name = ?", (name,)).fetchone():
        raise ValueError(f"batch {name!r} already exists")
    if after_days < 0:
        raise ValueError("after_days must be >= 0")
    cur = conn.execute(
        """INSERT INTO batches (name, task_type, reason_set_json, overlap_target, tier_ceiling, span_policy_json,
                                priority, guideline_version, require_note, relabel_of, relabel_after_days)
           SELECT ?, task_type, reason_set_json, 1, tier_ceiling, span_policy_json, priority, guideline_version,
                  require_note, id, ? FROM batches WHERE id = ?""",
        (name, after_days, source["id"]),
    )
    batch_id = cur.lastrowid
    rows = conn.execute("SELECT item_id, target FROM batch_items WHERE batch_id = ?", (source["id"],)).fetchall()
    subset = [r["item_id"] for r in rows if r["target"] is not None]
    if fraction is None and subset:
        items = subset
    else:
        share = 1.0 if fraction is None else fraction
        items = [r["item_id"] for r in rows if in_reliability_subset(name, r["item_id"], share)]
    conn.executemany("INSERT INTO batch_items (batch_id, item_id) VALUES (?, ?)", [(batch_id, i) for i in items])
    audit(conn, actor_id, "batch_relabel", name, {"source": source_name, "n_items": len(items),
                                                  "after_days": after_days})
    return describe(conn, conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone())


def open_warnings(conn: sqlite3.Connection, batch: sqlite3.Row) -> list[str]:
    """What opening this batch means for agreement data."""
    if batch["relabel_of"] is not None:
        return ["re-label batch: its α is intra-rater; report it apart from inter-rater α"]
    if batch["overlap_target"] >= 2:
        return []
    subset = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id = ? AND target IS NOT NULL",
                          (batch["id"],)).fetchone()[0]
    if subset:
        return [f"overlap 1: inter-rater α comes from the {subset}-item reliability subset only"]
    return ["overlap 1 with no reliability subset: no item gets two labelers; α must come from a re-label "
            "batch or from model pseudo-labelers (FR-9)"]


def set_status(conn: sqlite3.Connection, name: str, status: str, actor_id=None) -> list[str]:
    """Move a batch through draft/open/closed. Returns warnings about its agreement design."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    batch = get_batch(conn, name)
    conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch["id"]))
    audit(conn, actor_id, f"batch_{status}", name)
    return open_warnings(conn, batch) if status == "open" else []
