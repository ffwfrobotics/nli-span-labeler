"""
Adjudication (requirements FR-43, §6.5).

The queue holds items whose human, blind, first-pass labels disagree on any
reason or on ``answerable``, most disagreements first. An internal admin sees
the labels side by side as L-a, L-b, ... (never pseudonyms), picks the final
reasons and spans, and may promote the result to gold (FR-29).

Adjudications are stored apart from the raw labels and are versioned: saving
again adds a version, nothing is overwritten (NFR-6). α always uses the raw
labels; the training export carries the latest adjudication in ``adjudicated``.
"""

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Optional

from .db import audit
from .gold import _validate as validate_answer
from .gold import get_gold, save_gold
from .labelling import blind_question
from .reasons import REASONS
from .records import Filters, load_annotations

KEYS = ("answerable", *REASONS)


def _first_pass(conn: sqlite3.Connection, filters: Filters) -> dict:
    """item_id -> the records that count for agreement (human, blind, first pass, not probes or skips)."""
    by_item = defaultdict(list)
    # Items stay adjudicable after promotion to gold; probe answers are still left out
    for r in load_annotations(conn, Filters(**{**filters.__dict__, "include_models": False,
                                               "include_skipped": False, "include_gold": True})):
        if r["labeler_kind"] == "human" and r["_relabel_of"] is None and r["_blind"] and not r["_gold_probe"]:
            by_item[r["item_id"]].append(r)
    return by_item


def _value(r: dict, key: str):
    return bool(r["answerable"]) if key == "answerable" else (r["reasons"] or {}).get(key)


def disagreements(records: list) -> list[str]:
    """The keys (answerable or a reason) on which these labels differ; reasons not asked are ignored."""
    out = []
    for key in KEYS:
        values = {_value(r, key) for r in records} - {None}
        if len(values) > 1:
            out.append(key)
    return out


def latest(conn: sqlite3.Connection, item_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM adjudications WHERE item_id = ? ORDER BY version DESC, id DESC LIMIT 1",
                        (item_id,)).fetchone()


def as_dict(conn: sqlite3.Connection, row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    who = conn.execute("SELECT pseudonym FROM labelers WHERE id = ?", (row["adjudicator_id"],)).fetchone()
    return {"answerable": bool(row["answerable"]), "reasons": json.loads(row["reasons_json"]),
            "spans": json.loads(row["spans_json"]), "note": row["note"], "version": row["version"],
            "adjudicator": who["pseudonym"] if who else None, "created_at": row["created_at"]}


def queue(conn: sqlite3.Connection, filters: Filters, include_done: bool = False) -> list[dict]:
    out = []
    for item_id, recs in _first_pass(conn, filters).items():
        if len({r["labeler"] for r in recs}) < 2:
            continue
        keys = disagreements(recs)
        if not keys:
            continue
        done = latest(conn, item_id)
        if done and not include_done:
            continue
        out.append({"item_id": item_id, "n_disagreements": len(keys), "disagree_on": keys, "n_labels": len(recs),
                    "batches": sorted({r["batch"] for r in recs if r["batch"]}),
                    "adjudicated_version": done["version"] if done else None})
    return sorted(out, key=lambda d: (-d["n_disagreements"], d["item_id"]))


def _anonymise(item_id: str, pseudonyms: list) -> dict:
    """A stable, item-specific order, so L-a isn't always the same person."""
    order = sorted(pseudonyms, key=lambda p: hashlib.sha256(f"{item_id}\0{p}".encode()).hexdigest())
    return {p: f"L-{chr(ord('a') + i)}" if i < 26 else f"L-{i + 1}" for i, p in enumerate(order)}


def detail(conn: sqlite3.Connection, item_id: str, filters: Filters) -> dict:
    recs = _first_pass(conn, filters).get(item_id)
    if not recs:
        raise ValueError(f"no human labels to adjudicate for {item_id!r}")
    item = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    names = _anonymise(item_id, sorted({r["labeler"] for r in recs}))
    labels = sorted(({"labeler": names[r["labeler"]], "answerable": bool(r["answerable"]),
                      "reasons": [k for k, v in (r["reasons"] or {}).items() if v], "spans": r["spans"],
                      "note": r["note"], "batch": r["batch"]} for r in recs), key=lambda d: d["labeler"])
    asked = [k for k in REASONS if any((r["reasons"] or {}).get(k) is not None for r in recs)]
    return {
        "item_id": item_id, "state": item["state"], "state_format": item["state_format"],
        "question": blind_question(json.loads(item["question_json"])), "reason_set": asked,
        "labels": labels, "disagree_on": disagreements(recs),
        "adjudication": as_dict(conn, latest(conn, item_id)), "gold": get_gold(conn, item_id),
    }


def save(conn: sqlite3.Connection, item_id: str, actor: dict, *, answerable: bool, reasons: list, spans: list,
         note: Optional[str] = None, promote: bool = False, explanation: Optional[str] = None,
         alternatives: Optional[dict] = None, filters: Filters = Filters()) -> dict:
    """A new adjudication version (validated like gold, hard span rules included); optionally gold too."""
    recs = _first_pass(conn, filters).get(item_id)
    if not recs:
        raise ValueError(f"no human labels to adjudicate for {item_id!r}")
    _, span_dicts = validate_answer(conn, item_id, answerable, list(reasons), list(spans), alternatives or {})
    batch_ids = sorted({r["_batch_id"] for r in recs if r["_batch_id"] is not None})
    batch_id = batch_ids[0] if len(batch_ids) == 1 else None
    prev = latest(conn, item_id)
    version = (prev["version"] + 1) if prev else 1
    conn.execute(
        """INSERT INTO adjudications (item_id, batch_id, version, answerable, reasons_json, spans_json, note,
                                      adjudicator_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (item_id, batch_id, version, int(answerable), json.dumps(list(dict.fromkeys(reasons))),
         json.dumps(span_dicts), note, actor["id"]))
    audit(conn, actor["id"], "adjudicate", item_id, {"version": version, "promote": promote,
                                                     "disagree_on": disagreements(recs)})
    out = {"adjudication": as_dict(conn, latest(conn, item_id))}
    if promote:
        out["gold"] = save_gold(conn, item_id, answerable=answerable, reasons=reasons, spans=span_dicts,
                                alternatives=alternatives, explanation=explanation, actor_id=actor["id"])
    return out
