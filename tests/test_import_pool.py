"""
Import tests against the real libre-only E09 pool sample (docs/e13/fixtures/).

The sample is built by docs/e13/fixtures/build_fixtures.py. Its expected counts
come from docs/e13/fixtures/README.md. Per that README, the tests check features,
not row ids, since the sample changes when source permissions are reclassified.
They skip when the JSONL is absent.
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from e13_labeler.importer import import_file, parse_row

SAMPLE = Path(__file__).parent.parent / "docs" / "e13" / "fixtures" / "pool_eval_libre_sample.jsonl"

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="pool_eval_libre_sample.jsonl not present")


@pytest.fixture(scope="module")
def rows():
    return [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def imported(db):
    from e13_labeler.db import get_db

    with get_db() as conn:
        report = import_file(conn, SAMPLE, batch="sample")
        items = {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
    return report, items


def test_first_100_rows_import_with_no_rejects(db, tmp_path):
    """FR-1, as worded: the first 100 rows create one item per (row, qid), 0 rejected."""
    from e13_labeler.db import get_db

    lines = SAMPLE.read_text(encoding="utf-8").splitlines()[:100]
    first = tmp_path / "first100.jsonl"
    first.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with get_db() as conn:
        report = import_file(conn, first)
    assert report.n_rejected == 0, report.errors[:5]
    assert report.n_items == sum(len(json.loads(l)["questions"]) for l in lines)


def test_whole_sample(imported, rows):
    report, items = imported
    assert report.n_rejected == 0, report.errors[:5]
    assert report.n_rows == len(rows)
    assert len(items) == sum(len(r["questions"]) for r in rows)  # 695 for the 384-row build


def test_item_ids_are_row_and_qid(imported, rows):
    """FR-2: one item per (row, qid), in object order."""
    _, items = imported
    for r in rows:
        for qid in r["questions"]:
            assert f"{r['id']}#{qid}" in items


def test_state_formats(imported, rows):
    """FR-4: typed_decisions states are JSON; JSON-looking text that doesn't parse stays text."""
    _, items = imported
    by_row = {i["row_id"]: i["state_format"] for i in items.values()}
    for r in rows:
        expected = "json" if r["source"] == "typed_decisions" else "text"
        assert by_row[r["id"]] == expected, r["id"]
    tricky = [r for r in rows if r["state"].lstrip()[:1] in "{[" and r["source"] != "typed_decisions"]
    assert tricky, "the sample should contain JSON-looking text states"


def test_states_byte_identical(imported, rows):
    """FR-5."""
    _, items = imported
    by_row = {i["row_id"]: i["state"] for i in items.values()}
    assert all(by_row[r["id"]] == r["state"] for r in rows)


def test_all_libre_with_licence(imported):
    """FR-6 rule 1 (explicit field), and the licence looked up from source_permissions.json."""
    _, items = imported
    assert {i["permissions"] for i in items.values()} == {"libre"}
    assert all(i["source_license"] for i in items.values())


def test_question_types_and_gold(imported):
    _, items = imported
    types = Counter(json.loads(i["question_json"])["type"] for i in items.values())
    assert set(types) == {"choice", "noul", "score"}
    score_gold = [json.loads(i["gold_json"]) for i in items.values()
                  if json.loads(i["question_json"])["type"] == "score" and i["gold_json"]]
    assert any(isinstance(g, float) for g in score_gold)  # fractional score gold is kept as is


def test_reimport_is_noop(imported):
    """FR-10."""
    from e13_labeler.db import get_db

    _, before = imported
    with get_db() as conn:
        again = import_file(conn, SAMPLE)
        after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert again.n_items == 0 and again.n_unchanged == len(before) == after


def test_without_permissions_field_falls_back_to_source(rows):
    """Real imports mostly lack `permissions` (fixture README, 'Hedging' 4)."""
    for r in rows[:50]:
        bare = {k: v for k, v in r.items() if k != "permissions"}
        assert {i.permissions for i in parse_row(bare)} == {"libre"}, r["source"]


def test_labeler_payload_is_blind_for_every_item(imported, owner_client):
    """FR-12 over the whole sample: nothing hidden leaks into /api/next."""
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db

    with get_db() as conn:
        set_status(conn, "sample", "open")
    seen = 0
    while (r := owner_client.get("/api/next")).status_code == 200:
        payload = r.json()
        assert set(payload) == {"item_id", "lock_until", "state", "state_format", "question", "reason_set",
                                "task_type", "span_policy", "require_note", "progress"}
        assert set(payload["question"]) <= {"type", "instructions", "criteria"}
        owner_client.post("/api/skip", json={"item_id": payload["item_id"], "code": "other"})
        seen += 1
    assert seen == len(imported[1])
