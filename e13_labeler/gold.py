"""
Gold items (requirements FR-29): create, edit, retire, and promote from a
labeler's annotation. Gold stores the answer (answerable or the reasons), the
spans, per-reason acceptable alternatives and an explanation. Nothing is
hard-deleted (NFR-6): retiring keeps the row.

Gold items stay out of α (FR-28) and out of the training export by default.
"""

import json
import sqlite3
from typing import Optional

from .db import audit
from .labelling import Span, Submission, SubmissionError, hard_rule_problems, validate_span
from .reasons import REASONS


def _validate(conn: sqlite3.Connection, item_id: str, answerable: bool, reasons: list, spans: list,
              alternatives: dict) -> tuple[sqlite3.Row, list]:
    item = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    if item is None:
        raise ValueError(f"unknown item {item_id!r}")
    reasons = list(dict.fromkeys(reasons))
    problems = []
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        problems.append(f"unknown reason(s): {', '.join(unknown)}")
    if answerable and reasons:
        problems.append("answerable excludes every reason")
    if not answerable and not reasons:
        problems.append("gold needs answerable or at least one reason")
    for reason, alts in (alternatives or {}).items():
        if reason not in REASONS or not isinstance(alts, list) or set(alts) - set(REASONS):
            problems.append(f"alternatives must map reasons to lists of reasons ({reason!r})")
    span_objs = []
    question = json.loads(item["question_json"])
    for i, s in enumerate(spans or []):
        span = Span(**{k: s.get(k) for k in ("side", "role", "text", "option", "pointer", "start", "end")},
                    reasons=s.get("reasons") or [])
        problems += validate_span(span, i, item["state"], item["state_format"], question, set(reasons))
        span_objs.append(span)
    if not problems:
        problems += hard_rule_problems(Submission(answerable, reasons, None, span_objs, False, None))
    if problems:
        raise SubmissionError(problems)
    return item, [s.__dict__ for s in span_objs]


def _row(r: sqlite3.Row) -> dict:
    answer = json.loads(r["reasons_json"])
    return {"item_id": r["item_id"], "answerable": answer["answerable"], "reasons": answer["reasons"],
            "spans": json.loads(r["spans_json"]), "alternatives": json.loads(r["alternatives_json"]),
            "explanation": r["explanation"], "retired": bool(r["retired"]), "created_at": r["created_at"]}


def get_gold(conn: sqlite3.Connection, item_id: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM gold WHERE item_id = ?", (item_id,)).fetchone()
    return _row(r) if r else None


def list_gold(conn: sqlite3.Connection, include_retired: bool = False, visibilities=None) -> list[dict]:
    sql = "SELECT g.* FROM gold g JOIN items i ON i.item_id = g.item_id"
    params: list = []
    clauses = []
    if not include_retired:
        clauses.append("g.retired = 0")
    if visibilities:
        clauses.append(f"i.visibility IN ({','.join('?' * len(visibilities))})")
        params += list(visibilities)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [_row(r) for r in conn.execute(sql + " ORDER BY g.item_id", params)]


def save_gold(conn: sqlite3.Connection, item_id: str, *, answerable: bool = False, reasons: list = (),
              spans: list = (), alternatives: Optional[dict] = None, explanation: Optional[str] = None,
              actor_id: Optional[int] = None) -> dict:
    """Create gold, or replace it (an edit). A retired gold item comes back when saved again."""
    _, span_dicts = _validate(conn, item_id, answerable, list(reasons), list(spans), alternatives or {})
    existing = conn.execute("SELECT 1 FROM gold WHERE item_id = ?", (item_id,)).fetchone()
    values = (json.dumps({"answerable": bool(answerable), "reasons": list(dict.fromkeys(reasons))}),
              json.dumps(span_dicts), json.dumps(alternatives or {}), explanation)
    if existing:
        conn.execute("""UPDATE gold SET reasons_json = ?, spans_json = ?, alternatives_json = ?, explanation = ?,
                        retired = 0 WHERE item_id = ?""", (*values, item_id))
    else:
        conn.execute("""INSERT INTO gold (reasons_json, spans_json, alternatives_json, explanation, created_by,
                        item_id) VALUES (?, ?, ?, ?, ?, ?)""", (*values, actor_id, item_id))
    audit(conn, actor_id, "gold_update" if existing else "gold_create", item_id)
    return get_gold(conn, item_id)


def retire_gold(conn: sqlite3.Connection, item_id: str, actor_id: Optional[int] = None) -> dict:
    if not conn.execute("UPDATE gold SET retired = 1 WHERE item_id = ?", (item_id,)).rowcount:
        raise ValueError(f"no gold for {item_id!r}")
    audit(conn, actor_id, "gold_retire", item_id)
    return get_gold(conn, item_id)


def promote(conn: sqlite3.Connection, item_id: str, labeler: str, explanation: Optional[str] = None,
            alternatives: Optional[dict] = None, actor_id: Optional[int] = None) -> dict:
    """
    Gold from a labeler's latest (non-skipped) annotation of the item, e.g. the
    owner's pilot labels (§7.3 step 2). Adjudicated items (FR-43, M2) will promote
    the same way.
    """
    ann = conn.execute(
        """SELECT a.* FROM annotations a JOIN labelers l ON l.id = a.labeler_id
           WHERE a.item_id = ? AND l.pseudonym = ? AND a.skipped_code IS NULL
           ORDER BY a.created_at DESC, a.version DESC, a.id DESC LIMIT 1""", (item_id, labeler)).fetchone()
    if ann is None:
        raise ValueError(f"{labeler} has no annotation of {item_id!r} to promote")
    reasons = [r for r, v in json.loads(ann["reasons_json"]).items() if v]
    spans = [{"side": s["side"], "role": s["role"], "text": s["text"], "option": s["option"],
              "pointer": s["pointer"], "start": s["start"], "end": s["end"], "reasons": json.loads(s["reasons_json"])}
             for s in conn.execute("SELECT * FROM spans WHERE annotation_id = ? ORDER BY id", (ann["id"],))]
    return save_gold(conn, item_id, answerable=bool(ann["answerable"]), reasons=reasons, spans=spans,
                     alternatives=alternatives, explanation=explanation, actor_id=actor_id)
