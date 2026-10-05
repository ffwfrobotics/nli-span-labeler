"""
The onboarding and retraining quiz (requirements FR-27, FR-30, FR-58).

A quiz is N gold items (default 12, ``E13_QUIZ_SIZE``): at least one per
reason, candidates included, plus 2 answerable items, the rest at random. After
each answer the labeler sees the gold reasons, spans and explanation. They pass
when:
- at least 75% of items match gold (equal sets, or Jaccard >= 0.8, after the
  gold's acceptable alternatives); and
- no established reason is missed on more than one of its quiz items.

Onboarding: fail once, re-read the guideline, retake once; a second fail
pauses the account for the owner's review. Retraining (after an auto-pause,
FR-30): pass and the account is active again; fail and it waits for review.

Gold shown to a labeler is limited to their clearance, so a ``public``
labeler's quiz is libre-only (FR-58).
"""

import json
import os
import random
import sqlite3
from typing import Optional

from . import guideline, quality
from .db import audit
from .gold import get_gold
from .labelling import Span, blind_question, item_asof, validate_span
from .reasons import ESTABLISHED, REASONS
from .tiers import visible_to

PASS_ACCURACY = 0.75
MAX_MISSES_PER_REASON = 1


class QuizError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def quiz_size() -> int:
    return max(len(REASONS) + 2, int(os.environ.get("E13_QUIZ_SIZE", "12")))


def _gold_pool(conn: sqlite3.Connection, clearance: str) -> list[dict]:
    allowed = visible_to(clearance)
    rows = conn.execute(
        f"""SELECT g.item_id FROM gold g JOIN items i ON i.item_id = g.item_id
            WHERE g.retired = 0 AND i.visibility IN ({','.join('?' * len(allowed))}) ORDER BY g.item_id""",
        allowed).fetchall()
    return [get_gold(conn, r["item_id"]) for r in rows]


def coverage_gaps(conn: sqlite3.Connection, clearance: str) -> list[str]:
    """What a quiz for this clearance can't cover yet: reasons with no gold, or fewer than 2 answerable."""
    pool = _gold_pool(conn, clearance)
    gaps = [r for r in REASONS if not any(r in g["reasons"] for g in pool)]
    if sum(g["answerable"] for g in pool) < 2:
        gaps.append("answerable (2 needed)")
    if len(pool) < quiz_size():
        gaps.append(f"{quiz_size()} gold items (have {len(pool)})")
    return gaps


def select_items(pool: list[dict], size: int, rng: random.Random, avoid: set = frozenset()) -> list[str]:
    """
    Cover every reason, then 2 answerable items, then fill at random. Items in
    ``avoid`` (a previous attempt's) are used only when nothing else covers a slot.
    """
    order = sorted(pool, key=lambda g: (g["item_id"] in avoid, rng.random()))
    chosen: list[dict] = []

    def take(pred) -> bool:
        for g in order:
            if g not in chosen and pred(g):
                chosen.append(g)
                return True
        return False

    for reason in REASONS:
        if not any(reason in g["reasons"] for g in chosen) and not take(lambda g: reason in g["reasons"]):
            raise QuizError(f"not enough gold for a quiz: no gold item with {reason}", 503)
    for _ in range(2 - sum(g["answerable"] for g in chosen)):
        if not take(lambda g: g["answerable"]):
            raise QuizError("not enough gold for a quiz: 2 answerable gold items needed", 503)
    while len(chosen) < size and take(lambda g: True):
        pass
    if len(chosen) < size:
        raise QuizError(f"not enough gold for a quiz: {size} items needed, {len(chosen)} available", 503)
    rng.shuffle(chosen)
    return [g["item_id"] for g in chosen]


# ============================================================================
# Attempts
# ============================================================================

def _attempts(conn, labeler_id: int, kind: Optional[str] = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM quiz_attempts WHERE labeler_id = ?"
    params: list = [labeler_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return conn.execute(sql + " ORDER BY id", params).fetchall()


def open_attempt(conn, labeler_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM quiz_attempts WHERE labeler_id = ? AND finished_at IS NULL ORDER BY id DESC",
                        (labeler_id,)).fetchone()


def kind_for(labeler: dict) -> Optional[str]:
    """Which quiz this account may take now, if any."""
    if labeler["status"] == "onboarding":
        return "onboarding"
    if labeler["status"] == "paused" and labeler.get("pause_reason") == "gold_accuracy":
        return "retraining"
    return None


def stage(conn: sqlite3.Connection, labeler: dict) -> dict:
    """Where this account stands in onboarding: what the UI shows next."""
    kind = kind_for(labeler)
    attempts = _attempts(conn, labeler["id"])
    summary = [{"id": a["id"], "kind": a["kind"], "score": a["score"], "passed": None if a["passed"] is None
                else bool(a["passed"]), "finished_at": a["finished_at"]} for a in attempts]
    out = {"status": labeler["status"], "pause_reason": labeler.get("pause_reason"), "quiz": kind,
           "guideline_version": guideline.version(), "attempts": summary, "quiz_size": quiz_size()}
    if kind is None:
        out["next"] = "label" if labeler["status"] == "active" else "wait"
        return out
    if open_attempt(conn, labeler["id"]):
        out["next"] = "quiz"
        return out
    failed = [a for a in attempts if a["kind"] == kind and a["finished_at"] and not a["passed"]]
    since = failed[-1]["finished_at"] if failed else None
    out["next"] = "quiz" if guideline.viewed_since(conn, labeler["id"], since) else "guideline"
    out["retake"] = bool(failed)
    return out


def start(conn: sqlite3.Connection, labeler: dict, seed: Optional[int] = None) -> dict:
    kind = kind_for(labeler)
    if kind is None:
        raise QuizError("no quiz is due for this account", 403)
    current = open_attempt(conn, labeler["id"])
    if current:
        return {"attempt_id": current["id"], "resumed": True}
    if stage(conn, labeler)["next"] != "quiz":
        raise QuizError("open the guideline first" + (" again before the retake" if _attempts(
            conn, labeler["id"], kind) else ""), 403)
    previous = {a["item_id"] for a in conn.execute(
        """SELECT q.item_id FROM quiz_answers q JOIN quiz_attempts t ON t.id = q.attempt_id
           WHERE t.labeler_id = ?""", (labeler["id"],))}
    rng = random.Random(seed)
    items = select_items(_gold_pool(conn, labeler["clearance"]), quiz_size(), rng, avoid=previous)
    cur = conn.execute("INSERT INTO quiz_attempts (labeler_id, guideline_version, items_json, kind) VALUES (?, ?, ?, ?)",
                       (labeler["id"], guideline.version(), json.dumps(items), kind))
    conn.executemany("INSERT INTO quiz_answers (attempt_id, item_id, position) VALUES (?, ?, ?)",
                     [(cur.lastrowid, item_id, i) for i, item_id in enumerate(items)])
    audit(conn, labeler["id"], "quiz_start", labeler["pseudonym"], {"kind": kind, "attempt": cur.lastrowid})
    return {"attempt_id": cur.lastrowid, "resumed": False}


def current(conn: sqlite3.Connection, labeler: dict) -> dict:
    """The next unanswered question of the open attempt, or the last attempt's result."""
    attempt = open_attempt(conn, labeler["id"])
    if attempt is None:
        last = _attempts(conn, labeler["id"])
        if not last:
            raise QuizError("no quiz started", 404)
        return {"finished": True, **result(conn, last[-1]["id"])}
    row = conn.execute("""SELECT q.*, i.state, i.state_format, i.question_json, i.e13_json FROM quiz_answers q
                          JOIN items i ON i.item_id = q.item_id
                          WHERE q.attempt_id = ? AND q.answered_at IS NULL ORDER BY q.position LIMIT 1""",
                       (attempt["id"],)).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?", (attempt["id"],)).fetchone()[0]
    return {
        "finished": False, "attempt_id": attempt["id"], "kind": attempt["kind"], "position": row["position"],
        "total": total,
        "item": {"item_id": row["item_id"], "state": row["state"], "state_format": row["state_format"],
                 "question": blind_question(json.loads(row["question_json"])), "reason_set": list(REASONS),
                 "span_policy": {}, "require_note": False, "asof": item_asof(row), "task_type": "reasons"},
    }


def answer(conn: sqlite3.Connection, labeler: dict, item_id: str, answerable: bool, reasons: list,
           spans: list) -> dict:
    """Score one answer; feedback is the gold reasons, spans and explanation (FR-27)."""
    attempt = open_attempt(conn, labeler["id"])
    if attempt is None:
        raise QuizError("no quiz in progress", 404)
    row = conn.execute("""SELECT * FROM quiz_answers WHERE attempt_id = ? AND answered_at IS NULL
                          ORDER BY position LIMIT 1""", (attempt["id"],)).fetchone()
    if row["item_id"] != item_id:
        raise QuizError("answer the current question", 409)
    reasons = list(dict.fromkeys(reasons))
    problems = [f"unknown reason {r!r}" for r in reasons if r not in REASONS]
    if answerable and reasons:
        problems.append("answerable excludes every reason")
    if not answerable and not reasons:
        problems.append("choose answerable or at least one reason")
    item = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    for i, s in enumerate(spans):
        problems += validate_span(Span(**s), i, item["state"], item["state_format"],
                                  json.loads(item["question_json"]), set(reasons))
    if problems:
        raise QuizError("; ".join(problems), 422)
    gold = get_gold(conn, item_id)
    scored = quality.score(quality.answer_set(answerable, reasons), gold)
    conn.execute("""UPDATE quiz_answers SET answer_json = ?, score = ?, answered_at = datetime('now')
                    WHERE id = ?""",
                 (json.dumps({"answerable": answerable, "reasons": reasons, "spans": spans,
                              "missed": scored["missed"]}), float(scored["correct"]), row["id"]))
    feedback = {"correct": scored["correct"], "jaccard": scored["jaccard"], "missed": scored["missed"],
                "extra": scored["extra"],
                "gold": {k: gold[k] for k in ("answerable", "reasons", "spans", "alternatives", "explanation")}}
    if conn.execute("SELECT 1 FROM quiz_answers WHERE attempt_id = ? AND answered_at IS NULL",
                    (attempt["id"],)).fetchone() is None:
        feedback["result"] = finish(conn, labeler, attempt["id"])
    return feedback


def result(conn: sqlite3.Connection, attempt_id: int) -> dict:
    rows = conn.execute("SELECT * FROM quiz_answers WHERE attempt_id = ? ORDER BY position", (attempt_id,)).fetchall()
    answered = [r for r in rows if r["answered_at"]]
    accuracy = sum(r["score"] for r in answered) / len(rows) if rows else 0.0
    misses: dict[str, int] = {}
    for r in answered:
        for reason in json.loads(r["answer_json"])["missed"]:
            misses[reason] = misses.get(reason, 0) + 1
    too_many = sorted(r for r, n in misses.items() if r in ESTABLISHED and n > MAX_MISSES_PER_REASON)
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return {"attempt_id": attempt_id, "kind": attempt["kind"], "accuracy": round(accuracy, 4),
            "n_items": len(rows), "missed": misses, "missed_too_often": too_many,
            "passed": accuracy >= PASS_ACCURACY and not too_many}


def finish(conn: sqlite3.Connection, labeler: dict, attempt_id: int) -> dict:
    out = result(conn, attempt_id)
    conn.execute(f"UPDATE quiz_attempts SET score = ?, passed = ?, finished_at = {guideline.NOW_MS} WHERE id = ?",
                 (out["accuracy"], int(out["passed"]), attempt_id))
    kind = out["kind"]
    fails = sum(1 for a in _attempts(conn, labeler["id"], kind) if a["finished_at"] and not a["passed"])
    if out["passed"]:
        status, reason = "active", None
    elif kind == "onboarding" and fails < 2:
        status, reason = "onboarding", None  # one retake, after re-reading the guideline
    else:
        status, reason = "paused", "quiz_failed" if kind == "onboarding" else "review"
    conn.execute("UPDATE labelers SET status = ?, pause_reason = ? WHERE id = ?", (status, reason, labeler["id"]))
    audit(conn, labeler["id"], "quiz_pass" if out["passed"] else "quiz_fail", labeler["pseudonym"],
          {"kind": kind, "attempt": attempt_id, "accuracy": out["accuracy"], "status": status,
           "missed_too_often": out["missed_too_often"]})
    out.update(status=status, pause_reason=reason, retake=status == "onboarding")
    return out
