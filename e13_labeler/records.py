"""
Annotation records in the §5.4 export shape, read from the database.

Exports (FR-45/46/48) and the agreement report (FR-37/38) both build on
``load_annotations``, so the numbers on the dashboard and in the files come
from the same rows.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .reasons import REASONS
from .tiers import TIERS, visible_to

ANNOTATION_SCHEMA = "e13.annotation/1"


@dataclass
class Filters:
    """FR-47 export filters. Empty means no restriction."""
    batches: Sequence[str] = ()
    permissions: Sequence[str] = ()          # exact release-tier values (RELEASE_POLICY.md §2)
    since: Optional[str] = None              # annotation created_at >= since (YYYY-MM-DD or ISO)
    until: Optional[str] = None              # annotation created_at < until
    include_models: bool = True              # model pseudo-labelers (FR-9)
    include_gold: bool = False               # gold probes and items that are gold
    include_skipped: bool = True
    clearance: str = "internal"              # the exporter's clearance (FR-50, FR-57)
    extra: dict = field(default_factory=dict)

    def validate(self) -> None:
        bad = [p for p in self.permissions if p not in TIERS]
        if bad:
            raise ValueError(f"unknown permissions value(s): {', '.join(bad)}")

    def as_dict(self) -> dict:
        return {"batches": list(self.batches), "permissions": list(self.permissions), "since": self.since,
                "until": self.until, "include_models": self.include_models, "include_gold": self.include_gold,
                "include_skipped": self.include_skipped, "clearance": self.clearance}


def _iso(ts: Optional[str]) -> Optional[str]:
    """SQLite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS', UTC) to ISO 8601 with Z."""
    if not ts:
        return ts
    return ts.replace(" ", "T") + ("" if ts.endswith("Z") else "Z")


def item_filter_sql(filters: Filters, alias: str = "i") -> tuple[str, list]:
    """Item-level conditions shared by every export: clearance (FR-57), permissions, gold."""
    allowed = visible_to(filters.clearance)
    sql = f"{alias}.visibility IN ({','.join('?' * len(allowed))})"
    params = list(allowed)
    if filters.permissions:
        sql += f" AND {alias}.permissions IN ({','.join('?' * len(filters.permissions))})"
        params += list(filters.permissions)
    if not filters.include_gold:
        sql += f" AND {alias}.item_id NOT IN (SELECT item_id FROM gold WHERE retired = 0)"
    return sql, params


def load_annotations(conn: sqlite3.Connection, filters: Filters = Filters()) -> list[dict]:
    """
    One record per (item, labeler, batch) at its latest version (FR-45), in the
    §5.4 shape, plus a few internal keys prefixed with ``_`` (dropped on export).
    """
    filters.validate()
    item_sql, params = item_filter_sql(filters)
    sql = f"""
        SELECT a.*, i.row_id, i.qid, i.permissions, i.state_sha256, l.pseudonym, l.kind AS labeler_kind,
               b.name AS batch_name, sb.name AS relabel_of, b.show_model_answer, b.reason_set_json
        FROM annotations a
        JOIN items i ON i.item_id = a.item_id
        JOIN labelers l ON l.id = a.labeler_id
        LEFT JOIN batches b ON b.id = a.batch_id
        LEFT JOIN batches sb ON sb.id = b.relabel_of
        WHERE {item_sql}
          AND a.version = (SELECT MAX(v.version) FROM annotations v WHERE v.item_id = a.item_id
                           AND v.labeler_id = a.labeler_id AND v.batch_id IS a.batch_id)"""
    if filters.batches:
        sql += f" AND b.name IN ({','.join('?' * len(filters.batches))})"
        params += list(filters.batches)
    if filters.since:
        sql += " AND a.created_at >= ?"
        params.append(filters.since.replace("T", " ").rstrip("Z"))
    if filters.until:
        sql += " AND a.created_at < ?"
        params.append(filters.until.replace("T", " ").rstrip("Z"))
    if not filters.include_models:
        sql += " AND l.kind = 'human'"
    if not filters.include_gold:
        sql += " AND a.is_gold_probe = 0"
    if not filters.include_skipped:
        sql += " AND a.skipped_code IS NULL"
    rows = conn.execute(sql + " ORDER BY a.item_id, l.pseudonym, a.id", params).fetchall()

    spans_by_ann: dict[int, list] = {}
    if rows:
        ids = [r["id"] for r in rows]
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            for s in conn.execute(
                f"""SELECT * FROM spans WHERE annotation_id IN ({','.join('?' * len(part))}) ORDER BY id""", part
            ):
                spans_by_ann.setdefault(s["annotation_id"], []).append({
                    "side": s["side"], "pointer": s["pointer"], "start": s["start"], "end": s["end"],
                    "text": s["text"], "role": s["role"], "option": s["option"],
                    "reasons": json.loads(s["reasons_json"]),
                })
    return [_record(r, spans_by_ann.get(r["id"], [])) for r in rows]


def _record(r: sqlite3.Row, spans: list) -> dict:
    skipped = r["skipped_code"]
    reasons = json.loads(r["reasons_json"]) if r["reasons_json"] else None
    if reasons is not None:
        reasons = {k: reasons.get(k) for k in REASONS}
    return {
        "schema": ANNOTATION_SCHEMA,
        "item_id": r["item_id"],
        "row_id": r["row_id"],
        "qid": r["qid"],
        "batch": r["batch_name"],
        "labeler": r["pseudonym"],
        "labeler_kind": r["labeler_kind"],
        "version": r["version"],
        "guideline_version": r["guideline_version"],
        "permissions": r["permissions"],
        "state_sha256": r["state_sha256"],
        "answerable": None if skipped else (None if r["answerable"] is None else bool(r["answerable"])),
        "reasons": None if skipped else reasons,
        "spans": [] if skipped else spans,
        "relation": json.loads(r["relation_json"]) if r["relation_json"] else None,
        "note": r["note"],
        "skipped": skipped,
        "policy_override": bool(r["policy_override"]),
        "asof": r["asof"],
        "timing": {"active_ms": r["active_ms"], "wall_ms": r["wall_ms"]},
        "app_version": r["app_version"],
        "created_at": _iso(r["created_at"]),
        # internal (not exported)
        "_batch_id": r["batch_id"],
        "_relabel_of": r["relabel_of"],
        "_blind": r["batch_name"] is None or r["show_model_answer"] is None,
        "_gold_probe": bool(r["is_gold_probe"]),
    }


def public(record: dict) -> dict:
    """The record as exported: internal keys dropped."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def load_probes(conn: sqlite3.Connection, filters: Filters = Filters()) -> list[dict]:
    """
    Hidden gold answers (FR-28) for per-labeler gold accuracy (FR-41): the latest
    version of each human probe annotation, with the gold it is scored against.
    Same clearance and batch filters as the annotations.
    """
    from .gold import get_gold

    filters.validate()
    allowed = visible_to(filters.clearance)
    sql = f"""
        SELECT a.item_id, a.answerable, a.reasons_json, l.pseudonym, b.name AS batch_name
        FROM annotations a JOIN items i ON i.item_id = a.item_id JOIN labelers l ON l.id = a.labeler_id
        LEFT JOIN batches b ON b.id = a.batch_id
        WHERE a.is_gold_probe = 1 AND a.skipped_code IS NULL AND l.kind = 'human'
          AND i.visibility IN ({','.join('?' * len(allowed))})
          AND a.version = (SELECT MAX(v.version) FROM annotations v WHERE v.item_id = a.item_id
                           AND v.labeler_id = a.labeler_id AND v.batch_id IS a.batch_id)"""
    params = list(allowed)
    if filters.batches:
        sql += f" AND b.name IN ({','.join('?' * len(filters.batches))})"
        params += list(filters.batches)
    out = []
    for r in conn.execute(sql + " ORDER BY a.item_id, l.pseudonym", params).fetchall():
        gold = get_gold(conn, r["item_id"])
        if gold is None:
            continue
        answer = ["answerable"] if r["answerable"] else sorted(k for k, v in json.loads(r["reasons_json"]).items() if v)
        out.append({"labeler": r["pseudonym"], "item_id": r["item_id"], "batch": r["batch_name"], "answer": answer,
                    "gold": {k: gold[k] for k in ("answerable", "reasons", "alternatives")}})
    return out


def agreement_inputs(conn: sqlite3.Connection, filters: Filters = Filters()) -> dict:
    """
    Everything analysis.report needs, from the database (FR-48 "data"): the
    agreement records with each annotation's span word units, the gold probes,
    and the state word universe of items with three or more labelers (for AP).
    """
    from collections import defaultdict

    from .analysis import agreement_record
    from .spans_agreement import span_units, state_universe

    loaded = load_annotations(conn, filters)
    items: dict = {}
    records = []
    labelers = defaultdict(set)
    for r in loaded:
        rec = agreement_record(r)
        if r["item_id"] not in items:
            items[r["item_id"]] = conn.execute("SELECT state, state_format, question_json FROM items WHERE item_id = ?",
                                               (r["item_id"],)).fetchone()
        it = items[r["item_id"]]
        question = json.loads(it["question_json"])
        rec["span_units"] = [{"role": s["role"], "reasons": s["reasons"],
                              "units": span_units(s, it["state"], it["state_format"], question)} for s in r["spans"]]
        records.append(rec)
        if r["labeler_kind"] == "human":
            labelers[r["item_id"]].add(r["labeler"])
    universe = {i: state_universe(items[i]["state"], items[i]["state_format"])
                for i, ls in sorted(labelers.items()) if len(ls) >= 3}
    return {"records": records, "gold_probes": load_probes(conn, filters), "token_universe": universe}
