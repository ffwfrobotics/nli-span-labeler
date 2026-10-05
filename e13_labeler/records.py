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
