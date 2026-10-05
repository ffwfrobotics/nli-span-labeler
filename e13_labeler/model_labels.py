"""
Import committee-prompt and teacher reason labels as model pseudo-labelers
(requirements FR-9, §5.4).

Rows use the §5.4 annotation schema with ``"labeler_kind": "model"`` and the
model's name as ``labeler`` (for example ``committee_prompt_a``). Each name
becomes a labeler with kind and role ``model`` and no login. Model labels are
never shown to humans; they feed human-vs-model and model-vs-model α (FR-38).
Labels from a Jev model raise the item's release tier (§8.1: "committee labels
from Jev").
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import tiers
from .db import audit
from .importer import is_jev
from .labelling import Span, validate_span
from .reasons import REASONS

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class LabelRowError(ValueError):
    pass


@dataclass
class LabelImportReport:
    file: str
    n_rows: int = 0
    n_imported: int = 0
    n_duplicate: int = 0
    n_rejected: int = 0
    labelers: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def model_labeler(conn: sqlite3.Connection, name: str) -> int:
    if not NAME_RE.match(name):
        raise LabelRowError(f"model labeler name {name!r} must match {NAME_RE.pattern}")
    row = conn.execute("SELECT id, kind FROM labelers WHERE pseudonym = ?", (name,)).fetchone()
    if row:
        if row["kind"] != "model":
            raise LabelRowError(f"{name!r} is a human pseudonym")
        return row["id"]
    cur = conn.execute(
        """INSERT INTO labelers (pseudonym, kind, role, clearance, status) VALUES (?, 'model', 'model', 'internal',
           'active')""", (name,))
    return cur.lastrowid


def _parse(conn: sqlite3.Connection, row: dict) -> dict:
    if row.get("labeler_kind") != "model":
        raise LabelRowError('only rows with "labeler_kind": "model" can be imported')
    for key in ("item_id", "labeler"):
        if not row.get(key):
            raise LabelRowError(f"missing {key!r}")
    item = conn.execute("SELECT * FROM items WHERE item_id = ?", (row["item_id"],)).fetchone()
    if item is None:
        raise LabelRowError(f"unknown item {row['item_id']!r}")
    reasons = row.get("reasons") or {}
    if not isinstance(reasons, dict) or set(reasons) - set(REASONS):
        raise LabelRowError(f"reasons must be an object over {', '.join(REASONS)}")
    if any(v not in (True, False, None) for v in reasons.values()):
        raise LabelRowError("reason values must be true, false or null")
    checked = [r for r in REASONS if reasons.get(r)]
    answerable = row.get("answerable")
    if answerable is None:
        answerable = not checked
    if answerable and checked:
        raise LabelRowError("answerable excludes every reason")
    batch_id = None
    if row.get("batch"):
        b = conn.execute("SELECT id FROM batches WHERE name = ?", (row["batch"],)).fetchone()
        if b is None:
            raise LabelRowError(f"unknown batch {row['batch']!r}")
        batch_id = b["id"]
    question = json.loads(item["question_json"])
    spans = []
    for i, s in enumerate(row.get("spans") or []):
        span = Span(side=s.get("side"), role=s.get("role"), text=s.get("text") or "", option=s.get("option"),
                    pointer=s.get("pointer"), start=s.get("start"), end=s.get("end"), reasons=s.get("reasons") or [])
        problems = validate_span(span, i, item["state"], item["state_format"], question, set(checked))
        if problems:
            raise LabelRowError("; ".join(problems))
        spans.append(span)
    return {"item": item, "reasons": {r: reasons.get(r) for r in REASONS}, "answerable": bool(answerable),
            "batch_id": batch_id, "spans": spans, "version": int(row.get("version") or 1),
            "note": row.get("note"), "jev": is_jev(row["labeler"]) or is_jev(row.get("teacher"))}


def import_model_labels(conn: sqlite3.Connection, lines: Iterable[str], file_label: str,
                        actor_id: Optional[int] = None) -> LabelImportReport:
    report = LabelImportReport(file=file_label)
    names = set()
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        report.n_rows += 1
        try:
            row = json.loads(line)
            parsed = _parse(conn, row)
            labeler_id = model_labeler(conn, row["labeler"])
        except (LabelRowError, ValueError) as e:
            report.n_rejected += 1
            report.errors.append((lineno, str(e)))
            continue
        names.add(row["labeler"])
        item = parsed["item"]
        if conn.execute(
            "SELECT 1 FROM annotations WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? AND version = ?",
            (item["item_id"], labeler_id, parsed["batch_id"], parsed["version"]),
        ).fetchone():
            report.n_duplicate += 1
            continue
        cur = conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, answerable, reasons_json, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item["item_id"], parsed["batch_id"], labeler_id, parsed["version"], int(parsed["answerable"]),
             json.dumps(parsed["reasons"]), parsed["note"]),
        )
        for s in parsed["spans"]:
            conn.execute(
                """INSERT INTO spans (annotation_id, side, option, pointer, start, "end", text, role, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cur.lastrowid, s.side, s.option, s.pointer, s.start, s.end, s.text, s.role, json.dumps(s.reasons)))
        if parsed["jev"]:
            # Every item of the row; release tier only, since visibility follows the text's
            # licence (owner decision 2026-10-05)
            for sibling in conn.execute("SELECT item_id, permissions FROM items WHERE row_id = ?",
                                        (item["row_id"],)).fetchall():
                raised = tiers.max_tier(sibling["permissions"], "jev")
                if raised != sibling["permissions"]:
                    conn.execute("UPDATE items SET permissions = ? WHERE item_id = ?", (raised, sibling["item_id"]))
                    audit(conn, actor_id, "raise_tier", sibling["item_id"],
                          {"from": sibling["permissions"], "to": raised, "reason": "jev model labels"})
        report.n_imported += 1
    report.labelers = sorted(names)
    audit(conn, actor_id, "import_model_labels", file_label,
          {"n_imported": report.n_imported, "n_rejected": report.n_rejected, "labelers": report.labelers})
    return report
