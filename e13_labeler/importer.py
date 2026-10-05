"""
Import E09 pool JSONL rows as E13 items (requirements §4.1, §5.2, §8).

One row with N questions becomes N items, ``item_id = "<row id>#<qid>"`` (FR-2).
Each item gets a permissions tier (FR-6, FR-56). Eval-only sources are refused
(FR-55). Imports are idempotent by ``state_sha256`` (FR-10), and every run is
recorded in ``import_runs``.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import tiers
from .batches import ensure_batch, resample
from .db import audit
from .reasons import QUESTION_TYPES, REASONS


class RowError(ValueError):
    """A row that can't be imported; the message says why."""


def jev_teachers() -> frozenset:
    """
    Teachers whose outputs are Jev outputs (FR-8, §8.1): an explicit list, matched
    exactly. Default ``jev`` (the TypeSafe API); set E13_JEV_TEACHERS=a,b to replace
    it. No prefix or substring matching: ``openjev`` is a local Apache-licensed
    model, not Jev. The E09 teachers are jev, clef, clefflash, decider, laya,
    nimble, openjev and semif.
    """
    configured = os.environ.get("E13_JEV_TEACHERS", "jev")
    return frozenset(t.strip() for t in configured.split(",") if t.strip())


def is_jev(teacher: Optional[str]) -> bool:
    return bool(teacher) and teacher in jev_teachers()


def eval_only_sources() -> frozenset:
    extra = os.environ.get("E13_EVAL_ONLY_SOURCES", "")
    return frozenset(s.strip() for s in extra.split(",") if s.strip())


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise_state(state) -> tuple[str, str]:
    """
    FR-4/FR-5: the stored state string and its format. A string is kept byte for
    byte; it is ``json`` if it parses to an object or array. A state given as an
    object or array is serialised once and stored as that string.
    """
    if isinstance(state, (dict, list)):
        return json.dumps(state, ensure_ascii=False), "json"
    if not isinstance(state, str):
        raise RowError(f"state must be a string, object or array, not {type(state).__name__}")
    try:
        parsed = json.loads(state)
    except ValueError:
        return state, "text"
    return state, ("json" if isinstance(parsed, (dict, list)) else "text")


def validate_question(qid: str, question) -> dict:
    if not isinstance(question, dict):
        raise RowError(f"question {qid!r} must be an object")
    qtype = question.get("type")
    if qtype not in QUESTION_TYPES:
        raise RowError(f"question {qid!r} has unknown type {qtype!r}")
    criteria = question.get("criteria")
    if qtype == "choice" and not (isinstance(criteria, dict) and criteria):
        raise RowError(f"choice question {qid!r} needs a non-empty criteria object")
    if qtype == "score" and not (isinstance(criteria, list) and criteria):
        raise RowError(f"score question {qid!r} needs a non-empty criteria list")
    if qtype == "noul" and criteria is not None and not isinstance(criteria, dict):
        raise RowError(f"noul question {qid!r}: criteria must be an object of true/false descriptions")
    return question


@dataclass
class ItemRecord:
    item_id: str
    row_id: str
    qid: str
    source: str
    split: Optional[str]
    heldout: Optional[bool]
    state: str
    state_format: str
    state_sha256: str
    question: dict
    gold: object
    permissions: str           # release tier of the row's labels
    visibility: str            # who may see it: libre | restricted, from the text's licence
    source_license: Optional[str]
    e13: Optional[dict]
    model_answers: Optional[dict]


def parse_row(row: dict, source_permissions: Optional[dict] = None) -> list[ItemRecord]:
    """Turn one pool row into items. Raises RowError for a row that must be rejected."""
    if not isinstance(row, dict):
        raise RowError("row must be a JSON object")
    for key in ("id", "source", "state", "questions"):
        if key not in row:
            raise RowError(f"missing field {key!r}")
    row_id, source = str(row["id"]), str(row["source"])
    if "#" in row_id:
        raise RowError(f"row id {row_id!r} contains '#', which item ids use as separator")
    if tiers.is_eval_only(source, eval_only_sources()):
        raise RowError(f"source {source!r} is eval-only and bars training (FR-55); refusing to import it")

    questions = row["questions"]
    if not isinstance(questions, dict) or not questions:
        raise RowError("questions must be a non-empty object")

    state, state_format = normalise_state(row["state"])
    if row.get("state_format") is not None:
        if row["state_format"] not in ("text", "json"):
            raise RowError(f"state_format must be text or json, not {row['state_format']!r}")
        state_format = row["state_format"]
        if state_format == "json":
            try:
                if not isinstance(json.loads(state), (dict, list)):
                    raise ValueError
            except ValueError:
                raise RowError("state_format is json but the state is not a JSON object or array")

    # FR-6: explicit permissions, else the source class (unverified/unknown -> restricted)
    src_tier, licence = tiers.source_tier(source, source_permissions)
    if row.get("permissions") is not None:
        if row["permissions"] not in tiers.TIERS:
            raise RowError(f"unknown permissions {row['permissions']!r}")
        base_tier = row["permissions"]
    else:
        base_tier = src_tier

    model_answers = row.get("model_answers") or {}
    if not isinstance(model_answers, dict):
        raise RowError("model_answers must be an object of {teacher: {qid: answer}}")
    # FR-8/FR-56: any Jev output on the row marks every item of the row jev, since
    # the release tier describes the row's labels. Visibility ignores Jev: labelers
    # never see teacher outputs, so only the text's licence decides who sees it.
    has_jev = any(is_jev(t) for t, answers in model_answers.items() if answers)
    permissions = tiers.max_tier(base_tier, "jev") if has_jev else base_tier
    visibility = tiers.visibility_of(base_tier)

    e13 = row.get("e13") or {}
    if not isinstance(e13, dict):
        raise RowError("e13 must be an object")
    candidate_for = e13.get("candidate_for") or {}
    for qid, reasons in candidate_for.items():
        unknown = set(reasons) - set(REASONS)
        if unknown:
            raise RowError(f"e13.candidate_for[{qid!r}] has unknown reasons {sorted(unknown)}")

    gold = row.get("gold") or {}
    items = []
    for qid, question in questions.items():
        qid = str(qid)
        validate_question(qid, question)
        item_e13 = {k: v for k, v in e13.items() if k != "candidate_for"}
        if qid in candidate_for:
            item_e13["candidate_for"] = candidate_for[qid]
        answers = {t: a[qid] for t, a in model_answers.items() if isinstance(a, dict) and qid in a}
        items.append(
            ItemRecord(
                item_id=f"{row_id}#{qid}",
                row_id=row_id,
                qid=qid,
                source=source,
                split=row.get("split"),
                heldout=row.get("heldout"),
                state=state,
                state_format=state_format,
                state_sha256=sha256_text(state),
                question=question,
                gold=gold.get(qid),
                permissions=permissions,
                visibility=visibility,
                source_license=licence,
                e13=item_e13 or None,
                model_answers=answers or None,
            )
        )
    return items


@dataclass
class ImportReport:
    file: str
    n_rows: int = 0
    n_items: int = 0          # new or replaced items
    n_unchanged: int = 0      # already present with the same state hash (FR-10 no-op)
    n_rejected: int = 0       # rows rejected
    errors: list = field(default_factory=list)   # (line number, message)
    import_run_id: Optional[int] = None

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("file", "n_rows", "n_items", "n_unchanged", "n_rejected", "errors", "import_run_id")}


def _dumps(value) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def import_rows(
    conn: sqlite3.Connection,
    lines: Iterable[str],
    file_label: str,
    file_sha256: str,
    batch: Optional[str] = None,
    replace: bool = False,
    allow_lower_tier: bool = False,
    actor_id: Optional[int] = None,
    actor: str = "cli",
    source_permissions: Optional[dict] = None,
) -> ImportReport:
    """
    Import JSONL lines in one transaction. Rejected rows are reported and skipped;
    the rest are imported. ``replace`` lets a changed state overwrite an item;
    ``allow_lower_tier`` (owner only) lets that replacement lower its tier (FR-56).
    """
    report = ImportReport(file=file_label)
    cur = conn.execute(
        "INSERT INTO import_runs (file, sha256, actor) VALUES (?, ?, ?)", (file_label, file_sha256, actor)
    )
    report.import_run_id = run_id = cur.lastrowid
    batch_id = ensure_batch(conn, batch) if batch else None

    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        report.n_rows += 1
        try:
            items = parse_row(json.loads(line), source_permissions)
            pending = []
            for item in items:
                existing = conn.execute(
                    "SELECT state_sha256, permissions FROM items WHERE item_id = ?", (item.item_id,)
                ).fetchone()
                if existing and existing["state_sha256"] == item.state_sha256 and not replace:
                    pending.append((item, "unchanged"))
                    continue
                if existing and existing["state_sha256"] != item.state_sha256 and not replace:
                    raise RowError(f"{item.item_id}: state differs from the stored one; use --replace")
                if existing and not tiers.tier_at_most(existing["permissions"], item.permissions):
                    if not allow_lower_tier:
                        raise RowError(
                            f"{item.item_id}: would lower the tier from {existing['permissions']} to "
                            f"{item.permissions}; needs the owner's --allow-lower-tier"
                        )
                pending.append((item, "replace" if existing else "new"))
        except (RowError, ValueError) as e:
            report.n_rejected += 1
            report.errors.append((lineno, str(e)))
            continue

        for item, action in pending:
            if action == "unchanged":
                report.n_unchanged += 1
            else:
                if action == "replace":
                    old = conn.execute("SELECT permissions FROM items WHERE item_id = ?", (item.item_id,)).fetchone()
                    if old["permissions"] != item.permissions:
                        audit(conn, actor_id, "lower_tier" if allow_lower_tier else "change_tier", item.item_id,
                              {"from": old["permissions"], "to": item.permissions, "import_run": run_id})
                conn.execute(
                    """INSERT INTO items (item_id, row_id, qid, source, split, heldout, state, state_format,
                                          state_sha256, question_json, gold_json, permissions, visibility,
                                          source_license, e13_json, model_answers_json, import_run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(item_id) DO UPDATE SET
                           source = excluded.source, split = excluded.split, heldout = excluded.heldout,
                           state = excluded.state, state_format = excluded.state_format,
                           state_sha256 = excluded.state_sha256, question_json = excluded.question_json,
                           gold_json = excluded.gold_json, permissions = excluded.permissions,
                           visibility = excluded.visibility,
                           source_license = excluded.source_license, e13_json = excluded.e13_json,
                           model_answers_json = excluded.model_answers_json,
                           import_run_id = excluded.import_run_id""",
                    (
                        item.item_id, item.row_id, item.qid, item.source, item.split,
                        None if item.heldout is None else int(bool(item.heldout)),
                        item.state, item.state_format, item.state_sha256, _dumps(item.question),
                        _dumps(item.gold), item.permissions, item.visibility, item.source_license, _dumps(item.e13),
                        _dumps(item.model_answers), run_id,
                    ),
                )
                report.n_items += 1
            if batch_id:
                conn.execute("INSERT OR IGNORE INTO batch_items (batch_id, item_id) VALUES (?, ?)",
                             (batch_id, item.item_id))

    if batch_id:
        # New items join the batch's reliability subset by the same deterministic rule
        resample(conn, conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone())
    conn.execute(
        "UPDATE import_runs SET n_rows = ?, n_items = ?, n_rejected = ? WHERE id = ?",
        (report.n_rows, report.n_items, report.n_rejected, run_id),
    )
    audit(conn, actor_id, "import", file_label, {"import_run": run_id, "batch": batch,
                                                 "n_items": report.n_items, "n_rejected": report.n_rejected})
    return report


def import_file(conn: sqlite3.Connection, path: Path, **kwargs) -> ImportReport:
    data = Path(path).read_bytes()
    file_sha = hashlib.sha256(data).hexdigest()
    return import_rows(conn, data.decode("utf-8").splitlines(), str(path), file_sha, **kwargs)
