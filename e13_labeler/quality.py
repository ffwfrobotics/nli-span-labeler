"""
Scoring answers against gold, shared by the quiz (FR-27), hidden gold probes
(FR-28) and rolling gold accuracy with auto-pause (FR-30).

An answer is the set of checked reasons, or {"answerable"}. It matches gold
when the sets are equal or their Jaccard similarity is at least 0.8 (FR-27),
after the gold's acceptable alternatives are applied: if gold says
``ambiguous`` and lists ``underspecified`` as acceptable, an answer with
``underspecified`` counts as ``ambiguous``.
"""

import json
import os
import sqlite3
from typing import Iterable, Optional

from .reasons import REASONS

JACCARD_MATCH = 0.8
GOLD_WINDOW = 30


def gold_threshold() -> float:
    return float(os.environ.get("E13_GOLD_THRESHOLD", "0.6"))


def gold_min_probes() -> int:
    """Probes needed before auto-pause can fire (10 failures in a row must pause, FR-30)."""
    return int(os.environ.get("E13_GOLD_MIN_PROBES", "10"))


def answer_set(answerable: bool, reasons: Iterable[str]) -> frozenset:
    return frozenset({"answerable"}) if answerable else frozenset(reasons)


def score(answer: frozenset, gold: dict) -> dict:
    """
    ``gold`` as gold.get_gold returns it. Returns whether the item counts as
    correct, the Jaccard similarity, the gold reasons missed, and per-reason
    agreement (True/False) for every reason and ``answerable``.
    """
    target = answer_set(gold["answerable"], gold["reasons"])
    got = set(answer)
    for reason in target:
        if reason not in got:
            for alt in gold.get("alternatives", {}).get(reason, []):
                if alt in got and alt not in target:
                    got.discard(alt)
                    got.add(reason)
                    break
    union = got | target
    jaccard = len(got & target) / len(union) if union else 1.0
    return {
        "correct": got == target or jaccard >= JACCARD_MATCH,
        "jaccard": round(jaccard, 4),
        "missed": sorted(target - got - {"answerable"}),
        "extra": sorted(got - target - {"answerable"}),
        "per_reason": {r: (r in got) == (r in target) for r in (*REASONS, "answerable")},
    }


def _annotation_answer(row: sqlite3.Row) -> frozenset:
    reasons = [r for r, v in json.loads(row["reasons_json"] or "{}").items() if v]
    return answer_set(bool(row["answerable"]), reasons)


def probe_scores(conn: sqlite3.Connection, labeler_id: int, since: Optional[str] = None) -> list[dict]:
    """Each gold probe this labeler answered (latest version), oldest first, scored against current gold."""
    from .gold import get_gold

    sql = """SELECT a.* FROM annotations a
             WHERE a.labeler_id = ? AND a.is_gold_probe = 1 AND a.skipped_code IS NULL
               AND a.version = (SELECT MAX(v.version) FROM annotations v WHERE v.item_id = a.item_id
                                AND v.labeler_id = a.labeler_id AND v.batch_id IS a.batch_id)"""
    params: list = [labeler_id]
    if since:
        sql += """ AND (SELECT MIN(f.created_at) FROM annotations f WHERE f.item_id = a.item_id
                        AND f.labeler_id = a.labeler_id AND f.batch_id IS a.batch_id) >= ?"""
        params.append(since)
    out = []
    for row in conn.execute(sql + " ORDER BY a.item_id", params).fetchall():
        gold = get_gold(conn, row["item_id"])
        if gold is None:
            continue
        first = conn.execute("""SELECT MIN(created_at) FROM annotations WHERE item_id = ? AND labeler_id = ?
                                AND batch_id IS ?""", (row["item_id"], labeler_id, row["batch_id"])).fetchone()[0]
        out.append({"item_id": row["item_id"], "at": first, **score(_annotation_answer(row), gold)})
    return sorted(out, key=lambda s: (s["at"], s["item_id"]))


def _since_last_quiz_pass(conn: sqlite3.Connection, labeler_id: int) -> Optional[str]:
    row = conn.execute("""SELECT MAX(finished_at) FROM quiz_attempts WHERE labeler_id = ? AND passed = 1""",
                       (labeler_id,)).fetchone()
    return row[0].replace("T", " ").rstrip("Z") if row and row[0] else None


def gold_accuracy(conn: sqlite3.Connection, labeler_id: int) -> dict:
    """FR-30 / FR-41: overall and per-reason gold accuracy, plus the rolling window auto-pause watches."""
    scores = probe_scores(conn, labeler_id)
    window = probe_scores(conn, labeler_id, since=_since_last_quiz_pass(conn, labeler_id))[-GOLD_WINDOW:]
    per_reason = {r: (sum(s["per_reason"][r] for s in scores) / len(scores) if scores else None)
                  for r in (*REASONS, "answerable")}
    rolling = sum(s["correct"] for s in window) / len(window) if window else None
    return {
        "n_probes": len(scores),
        "accuracy": sum(s["correct"] for s in scores) / len(scores) if scores else None,
        "per_reason": per_reason,
        "rolling": rolling,
        "rolling_n": len(window),
        "threshold": gold_threshold(),
        "below_threshold": rolling is not None and len(window) >= gold_min_probes() and rolling < gold_threshold(),
    }


def check_auto_pause(conn: sqlite3.Connection, labeler: dict) -> bool:
    """
    FR-30: pause a labeler whose rolling gold accuracy since their last passed
    quiz is below the threshold. ``next`` then offers the retraining quiz.
    """
    if labeler["role"] == "owner" or labeler["status"] != "active":
        return False
    if not gold_accuracy(conn, labeler["id"])["below_threshold"]:
        return False
    from .accounts import set_status

    set_status(conn, None, labeler["pseudonym"], "pause", reason="gold_accuracy")
    return True
