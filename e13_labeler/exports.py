"""
Exports (requirements FR-45 to FR-49, §5.4, §5.5).

``write_export`` writes one directory per run, under
``outputs/e13_labeler/exports/<timestamp>/``, and never deletes or overwrites
anything (owner policy: keep all artifacts). It can contain:

- ``annotations.jsonl``: one row per (item, labeler, batch), latest version (FR-45, §5.4)
- ``training.jsonl``: one row per item with enough human labels (FR-46, §5.5)
- ``agreement.json``: every agreement number plus the records behind them (FR-48)
- ``items.jsonl``: the items in the E09 pool import format, so the data round-trips (FR-5)
- ``manifest.json``: app version, guideline versions, DB checksum, filters, files (FR-49)
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import config
from .analysis import agreement_record, report
from .db import audit
from .reasons import REASONS
from .records import Filters, item_filter_sql, load_annotations, public

TRAIN_SCHEMA = "e13.train/1"
MANIFEST_SCHEMA = "e13.export-manifest/1"
KINDS = ("annotations", "training", "agreement", "items")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ============================================================================
# FR-45 annotations
# ============================================================================

def annotation_rows(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    return [public(r) for r in load_annotations(conn, filters)]


# ============================================================================
# FR-46 training rows
# ============================================================================

def _span_key(s: dict) -> tuple:
    return (s["side"], s["pointer"], s["start"], s["end"], s["role"], s["option"])


def merge_spans(records: Sequence[dict]) -> tuple[list, list]:
    """
    Merge spans by exact coordinates (word-snapped in the UI). ``votes`` counts
    labelers. Returns (abstain evidence: spans linked to reasons, plain evidence).
    """
    merged: dict = {}
    for r in records:
        for s in r["spans"]:
            m = merged.setdefault(_span_key(s), {"span": s, "labelers": set(), "reasons": set()})
            m["labelers"].add(r["labeler"])
            m["reasons"].update(s["reasons"])
    abstain, plain = [], []
    for m in merged.values():
        s = m["span"]
        out = {"side": s["side"], "pointer": s["pointer"], "start": s["start"], "end": s["end"],
               "text": s["text"], "role": s["role"], "option": s["option"]}
        if m["reasons"]:
            abstain.append({**out, "reasons": sorted(m["reasons"], key=REASONS.index), "votes": len(m["labelers"])})
        else:
            plain.append({**out, "votes": len(m["labelers"])})
    order = lambda x: (x["side"], x["pointer"] or "", x["start"] if x["start"] is not None else -1, x["role"])
    return sorted(abstain, key=order), sorted(plain, key=order)


def _majority(votes: int, n: int) -> Optional[bool]:
    if votes * 2 > n:
        return True
    if votes * 2 < n:
        return False
    return None  # tie (§5.5: ties stay null)


def training_rows(conn: sqlite3.Connection, filters: Filters, text_included: bool = True) -> list[dict]:
    """
    One row per item whose human, blind, first-pass labels reach the item's
    overlap target (FR-46). Gold items, gold probes, re-label passes and model
    labels never enter training targets.
    """
    records = [r for r in load_annotations(conn, Filters(**{**filters.__dict__, "include_models": False,
                                                            "include_skipped": False}))
               if r["labeler_kind"] == "human" and r["_relabel_of"] is None and r["_blind"]
               and not r["_gold_probe"] and r["skipped"] is None]
    by_item: dict = {}
    for r in records:
        by_item.setdefault(r["item_id"], []).append(r)
    if not by_item:
        return []

    item_sql, params = item_filter_sql(filters)
    rows = []
    for item_id in sorted(by_item):
        recs = by_item[item_id]
        item = conn.execute(f"SELECT * FROM items i WHERE i.item_id = ? AND {item_sql}",
                            (item_id, *params)).fetchone()
        if item is None:
            continue
        batch_ids = sorted({r["_batch_id"] for r in recs})
        batches = conn.execute(
            f"""SELECT b.*, COALESCE(bi.target, b.overlap_target) AS target FROM batches b
                JOIN batch_items bi ON bi.batch_id = b.id AND bi.item_id = ?
                WHERE b.id IN ({','.join('?' * len(batch_ids))})""", (item_id, *batch_ids)).fetchall()
        target = max((b["target"] for b in batches), default=2)
        labelers = sorted({r["labeler"] for r in recs})
        n = len(labelers)
        if n < target:
            continue

        reason_set = []
        for b in batches:
            for reason in json.loads(b["reason_set_json"]):
                if reason not in reason_set:
                    reason_set.append(reason)
        answerable_votes = sum(1 for r in recs if r["answerable"])
        soft, majority, ties = {}, [], []
        for reason in REASONS:
            asked = [r for r in recs if r["reasons"] and r["reasons"].get(reason) is not None]
            if not asked:
                soft[reason] = None
                continue
            votes = sum(1 for r in asked if r["reasons"][reason])
            soft[reason] = votes / len(asked)
            m = _majority(votes, len(asked))
            if m:
                majority.append(reason)
            elif m is None:
                ties.append(reason)
        abstain_evidence, plain_evidence = merge_spans(recs)
        adjudication = conn.execute("SELECT * FROM adjudications WHERE item_id = ?", (item_id,)).fetchone()
        guideline_versions = sorted({b["guideline_version"] for b in batches if b["guideline_version"]})

        row = {
            "schema": TRAIN_SCHEMA,
            "id": item["row_id"], "qid": item["qid"], "source": item["source"], "split": item["split"],
            "heldout": None if item["heldout"] is None else bool(item["heldout"]),
            "permissions": item["permissions"], "source_license": item["source_license"],
            # Human labels are human-made, so they don't change a libre row's tier (FR-59)
            "label_provenance": "human",
            "text_included": text_included, "state_sha256": item["state_sha256"],
        }
        if text_included:
            row["state"] = item["state"]
            row["question"] = json.loads(item["question_json"])
        row["targets"] = {"mbnli": {
            "abstain": {
                "p": round(1 - answerable_votes / n, 6),
                "reasons": {k: (None if v is None else round(v, 6)) for k, v in soft.items()},
                "evidence": abstain_evidence,
            },
            "evidence": plain_evidence,
        }}
        row["human"] = {
            "n_labelers": n, "labelers": labelers, "answerable_votes": answerable_votes,
            "majority": {"answerable": _majority(answerable_votes, n), "reasons": majority, "tied_reasons": ties},
            "adjudicated": None if adjudication is None else {
                "reasons": json.loads(adjudication["reasons_json"]), "spans": json.loads(adjudication["spans_json"])},
            "batch": recs[0]["batch"] if len(batches) == 1 else sorted(b["name"] for b in batches),
            "guideline_version": guideline_versions[0] if len(guideline_versions) == 1 else (guideline_versions or None),
            "reason_set": "all10" if reason_set == list(REASONS) else reason_set,
        }
        rows.append(row)
    return rows


# ============================================================================
# FR-5 items (pool import format)
# ============================================================================

def item_rows(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    """
    The items in the E09 pool import format (§5.2), one row per row_id, with the
    stored state byte for byte, so an export re-imports to identical states and
    hashes (FR-5). Batch filters select rows with any item in those batches.
    """
    item_sql, params = item_filter_sql(filters)
    sql = f"SELECT i.* FROM items i WHERE {item_sql}"
    if filters.batches:
        sql += f""" AND i.item_id IN (SELECT bi.item_id FROM batch_items bi JOIN batches b ON b.id = bi.batch_id
                                      WHERE b.name IN ({','.join('?' * len(filters.batches))}))"""
        params += list(filters.batches)
    rows: dict = {}
    for i in conn.execute(sql + " ORDER BY i.row_id, i.rowid", params):
        row = rows.setdefault(i["row_id"], {
            "id": i["row_id"], "source": i["source"], "split": i["split"],
            "heldout": None if i["heldout"] is None else bool(i["heldout"]),
            "state": i["state"], "state_format": i["state_format"],
            "questions": {}, "gold": {}, "permissions": i["permissions"],
            "_e13": {}, "_candidate_for": {}, "_answers": {},
        })
        row["questions"][i["qid"]] = json.loads(i["question_json"])
        if i["gold_json"] is not None:
            row["gold"][i["qid"]] = json.loads(i["gold_json"])
        e13 = json.loads(i["e13_json"]) if i["e13_json"] else {}
        if "candidate_for" in e13:
            row["_candidate_for"][i["qid"]] = e13.pop("candidate_for")
        row["_e13"].update(e13)
        for teacher, answer in (json.loads(i["model_answers_json"]) if i["model_answers_json"] else {}).items():
            row["_answers"].setdefault(teacher, {})[i["qid"]] = answer
    out = []
    for row in rows.values():
        e13 = row.pop("_e13")
        candidate_for = row.pop("_candidate_for")
        answers = row.pop("_answers")
        if candidate_for:
            e13["candidate_for"] = candidate_for
        if e13:
            row["e13"] = e13
        if answers:
            row["model_answers"] = answers
        out.append(row)
    return out


# ============================================================================
# FR-48 agreement
# ============================================================================

def agreement_document(conn: sqlite3.Connection, filters: Filters, n_boot: int = 1000, seed: int = 0) -> dict:
    records = [agreement_record(r) for r in load_annotations(conn, filters)]
    doc = report(records, n_boot=n_boot, seed=seed)
    doc["filters"] = filters.as_dict()
    # The exact inputs, so `analysis.report(doc["data"], n_boot=..., seed=...)` reproduces every number
    doc["data"] = records
    return doc


# ============================================================================
# FR-49 the export run
# ============================================================================

def db_checksum(conn: sqlite3.Connection) -> str:
    """sha256 of a consistent snapshot of the database (WAL included)."""
    snapshot = sqlite3.connect(":memory:")
    try:
        conn.backup(snapshot)
        return "sha256:" + hashlib.sha256(snapshot.serialize()).hexdigest()
    finally:
        snapshot.close()


def _new_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path, n = root / stamp, 1
    while path.exists():  # never overwrite an earlier export
        n += 1
        path = root / f"{stamp}-{n}"
    path.mkdir(parents=True)
    return path


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(_dumps(row) + "\n")
            n += 1
    return n


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_export(conn: sqlite3.Connection, kinds: Sequence[str] = KINDS, filters: Filters = Filters(),
                 out_root: Optional[Path] = None, text_included: bool = True, actor_id=None,
                 n_boot: int = 1000, seed: int = 0) -> dict:
    """Write the requested exports into a fresh timestamped directory. Returns the manifest."""
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown export kind(s): {', '.join(sorted(unknown))}")
    filters.validate()
    checksum = db_checksum(conn)  # before anything (the audit row) changes the database
    out = _new_dir(Path(out_root) if out_root else config.OUTPUTS_DIR / "exports")
    files = []
    if "annotations" in kinds:
        files.append(("annotations.jsonl", _write_jsonl(out / "annotations.jsonl", annotation_rows(conn, filters))))
    if "training" in kinds:
        files.append(("training.jsonl", _write_jsonl(out / "training.jsonl",
                                                     training_rows(conn, filters, text_included))))
    if "items" in kinds:
        if not text_included:
            raise ValueError("the items export carries state text; it can't be written with text_included off")
        files.append(("items.jsonl", _write_jsonl(out / "items.jsonl", item_rows(conn, filters))))
    if "agreement" in kinds:
        doc = agreement_document(conn, filters, n_boot=n_boot, seed=seed)
        (out / "agreement.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        files.append(("agreement.json", len(doc["data"])))

    batch_names = list(filters.batches) or [r[0] for r in conn.execute("SELECT name FROM batches")]
    guideline_versions = sorted({r[0] for r in conn.execute(
        f"SELECT guideline_version FROM batches WHERE name IN ({','.join('?' * len(batch_names))})"
        " AND guideline_version IS NOT NULL", batch_names)}) if batch_names else []
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": config.app_version(),
        "guideline_versions": guideline_versions,
        "db_sha256": checksum,
        "filters": filters.as_dict(),
        "text_included": text_included,
        "files": [{"name": name, "rows": n, "sha256": _sha256(out / name)} for name, n in files],
        "directory": str(out),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    audit(conn, actor_id, "export", str(out), {"files": [f["name"] for f in manifest["files"]],
                                               "filters": manifest["filters"]})
    return manifest
