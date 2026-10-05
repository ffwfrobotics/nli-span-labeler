"""
Tests for pool import (requirements §4.1, §8.2) using tests/fixtures/synthetic_pool.jsonl.

The fixture is synthetic: invented rows that follow the §5.2 format and the
examples in the requirements. Tests against real pool rows go in
test_import_pool.py once fixtures are available.
"""
import json
from pathlib import Path

import pytest

from e13_labeler import importer
from e13_labeler.importer import RowError, import_file, parse_row

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_pool.jsonl"
ROWS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def by_id(row_id):
    return next(r for r in ROWS if r["id"] == row_id)


@pytest.fixture
def imported(db):
    from e13_labeler.db import get_db

    with get_db() as conn:
        report = import_file(conn, FIXTURE, batch="pilot")
    return report


def items():
    from e13_labeler.db import get_db

    with get_db() as conn:
        return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM items")}


class TestImport:
    def test_imports_everything_without_rejects(self, imported):
        """FR-1."""
        assert imported.n_rejected == 0 and imported.errors == []
        assert imported.n_rows == len(ROWS)
        assert imported.n_items == sum(len(r["questions"]) for r in ROWS)

    def test_one_item_per_question(self, imported):
        """FR-2: a row with 4 questions yields 4 items, id '<row id>#<qid>'."""
        ids = [i for i in items() if i.startswith("typed_decisions/security_incidents_000085#")]
        assert sorted(ids) == sorted(
            f"typed_decisions/security_incidents_000085#{q}"
            for q in ("severity", "credential_compromise", "response", "escalate")
        )

    def test_state_format_detection(self, imported):
        """FR-4: JSON-encoded string and object states are json; prose and bare scalars are text."""
        got = items()
        assert got["typed_decisions/security_incidents_000085#severity"]["state_format"] == "json"
        assert got["bbc_news/eval/73#q0"]["state_format"] == "text"
        assert got["snli/eval/12#outdoors"]["state_format"] == "json"
        assert got["mystery_source/eval/1#q"]["state_format"] == "text"  # "42" parses, but not to an object

    def test_state_stored_exactly(self, imported):
        """FR-5: string states are stored byte-identical, with their hash."""
        got = items()["typed_decisions/security_incidents_000085#severity"]
        assert got["state"] == by_id("typed_decisions/security_incidents_000085")["state"]
        assert got["state_sha256"] == importer.sha256_text(got["state"])

    def test_tiers(self, imported):
        """FR-6, FR-8, FR-56."""
        got = items()
        assert got["typed_decisions/security_incidents_000085#severity"]["permissions"] == "libre"
        assert got["bbc_news/eval/73#q0"]["permissions"] == "restricted"  # unverified source
        assert got["mystery_source/eval/1#q"]["permissions"] == "restricted"  # unknown source
        assert got["fever/eval/5#tall"]["permissions"] == "jev"  # libre + Jev answer
        assert got["fever/eval/5#tall"]["source_license"] == "CC-BY-SA-3.0"

    def test_hidden_fields_kept_per_item(self, imported):
        """FR-7 storage: candidate_for is split per question; other e13 fields kept."""
        got = items()
        sev = json.loads(got["typed_decisions/security_incidents_000085#severity"]["e13_json"])
        assert sev == {"candidate_for": ["stale_state"], "generator": "e13/gen_candidates.py@abc123",
                       "asof": "2026-10-05", "batch": "pilot"}
        cred = json.loads(got["typed_decisions/security_incidents_000085#credential_compromise"]["e13_json"])
        assert "candidate_for" not in cred
        answers = json.loads(got["typed_decisions/security_incidents_000085#credential_compromise"]["model_answers_json"])
        assert answers == {"clefflash": {"type": "noul", "noul": 0.91}}
        assert got["typed_decisions/security_incidents_000085#severity"]["model_answers_json"] is None
        assert json.loads(got["bbc_news/eval/73#q0"]["gold_json"]) == "business"

    def test_batch_membership(self, imported):
        from e13_labeler.db import get_db

        with get_db() as conn:
            batch = conn.execute("SELECT * FROM batches WHERE name = 'pilot'").fetchone()
            n = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id = ?", (batch["id"],)).fetchone()[0]
        assert batch["status"] == "draft" and batch["overlap_target"] == 2
        assert n == imported.n_items


class TestIdempotency:
    def test_double_import_is_noop(self, imported):
        """FR-10: double import gives an identical item count, and both runs are recorded."""
        from e13_labeler.db import get_db

        before = items()
        with get_db() as conn:
            second = import_file(conn, FIXTURE)
            runs = conn.execute("SELECT n_rows, n_items, n_rejected FROM import_runs ORDER BY id").fetchall()
        assert second.n_items == 0 and second.n_unchanged == imported.n_items
        assert items() == before
        assert [tuple(r) for r in runs] == [(5, 8, 0), (5, 0, 0)]

    def test_changed_state_rejected_unless_replace(self, imported, tmp_path):
        from e13_labeler.db import get_db

        row = dict(by_id("fever/eval/5"), state="The tower is 330 metres tall.")
        changed = tmp_path / "changed.jsonl"
        changed.write_text(json.dumps(row) + "\n")
        with get_db() as conn:
            report = import_file(conn, changed)
        assert report.n_rejected == 1 and "--replace" in report.errors[0][1]
        with get_db() as conn:
            report = import_file(conn, changed, replace=True)
        assert report.n_items == 1
        assert items()["fever/eval/5#tall"]["state"] == "The tower is 330 metres tall."

    def test_lowering_tier_needs_owner_flag(self, imported, tmp_path):
        """FR-56: tiers can only be lowered by re-import with an explicit, logged flag."""
        from e13_labeler.db import get_db

        row = {k: v for k, v in by_id("fever/eval/5").items() if k != "model_answers"}
        lower = tmp_path / "lower.jsonl"
        lower.write_text(json.dumps(row) + "\n")
        with get_db() as conn:
            report = import_file(conn, lower, replace=True)
        assert report.n_rejected == 1 and "allow-lower-tier" in report.errors[0][1]
        with get_db() as conn:
            report = import_file(conn, lower, replace=True, allow_lower_tier=True)
            log = conn.execute("SELECT action, target FROM audit_log WHERE action = 'lower_tier'").fetchall()
        assert report.n_items == 1 and items()["fever/eval/5#tall"]["permissions"] == "libre"
        assert [tuple(r) for r in log] == [("lower_tier", "fever/eval/5#tall")]


class TestRejections:
    def test_eval_only_source_refused(self):
        """FR-55: an llm_aggrefact row fails with a clear message."""
        row = dict(by_id("fever/eval/5"), id="llm_aggrefact/1", source="llm_aggrefact")
        with pytest.raises(RowError, match="eval-only"):
            parse_row(row)

    def test_configured_eval_only_source(self, monkeypatch):
        monkeypatch.setenv("E13_EVAL_ONLY_SOURCES", "fever")
        with pytest.raises(RowError, match="eval-only"):
            parse_row(by_id("fever/eval/5"))

    @pytest.mark.parametrize(
        "patch, message",
        [
            ({"questions": {}}, "non-empty"),
            ({"questions": {"q": {"type": "boolean"}}}, "unknown type"),
            ({"questions": {"q": {"type": "choice", "criteria": ["a", "b"]}}}, "criteria object"),
            ({"questions": {"q": {"type": "score", "criteria": {"a": 1}}}}, "criteria list"),
            ({"permissions": "public"}, "unknown permissions"),
            ({"state_format": "json"}, "not a JSON object"),
            ({"e13": {"candidate_for": {"tall": ["boring"]}}}, "unknown reasons"),
            ({"id": "fever#5"}, "contains '#'"),
        ],
    )
    def test_bad_rows(self, patch, message):
        with pytest.raises(RowError, match=message):
            parse_row(dict(by_id("fever/eval/5"), **patch))

    def test_rejected_rows_do_not_stop_the_rest(self, db, tmp_path):
        from e13_labeler.db import get_db

        mixed = tmp_path / "mixed.jsonl"
        mixed.write_text(FIXTURE.read_text() + "not json\n" + json.dumps({"id": "x"}) + "\n")
        with get_db() as conn:
            report = import_file(conn, mixed)
        assert report.n_rejected == 2 and report.n_items == 8
        assert [lineno for lineno, _ in report.errors] == [6, 7]

    def test_jev_teacher_config(self, monkeypatch):
        """FR-8: teachers configured as Jev raise the tier."""
        row = dict(by_id("snli/eval/12"), model_answers={"teacher_x": {"outdoors": {"noul": 0.5}}})
        assert parse_row(row)[0].permissions == "libre"
        monkeypatch.setenv("E13_JEV_TEACHERS", "teacher_x")
        assert parse_row(row)[0].permissions == "jev"


class TestCli:
    def test_import_command(self, db, capsys):
        """FR-11: python -m e13_labeler import FILE --batch NAME."""
        from e13_labeler.__main__ import main

        assert main(["import", str(FIXTURE), "--batch", "pilot"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["n_items"] == 8 and summary["n_rejected"] == 0


class TestPoolShapes:
    """Row shapes described in docs/e13/fixtures/README.md."""

    def test_ids_with_slash_and_colon(self, db, owner_client):
        """Pool ids contain '/' and ':' (x_snli/snli:246); routes must take them."""
        from e13_labeler.batches import set_status
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_rows

        row = {"id": "x_snli/snli:246", "source": "snli", "split": "eval", "heldout": False,
               "state": "Two dogs run.", "questions": {"snli_test_8206": {"type": "noul", "instructions": "Animals move."}},
               "gold": {"snli_test_8206": True}}
        with get_db() as conn:
            import_rows(conn, [json.dumps(row)], "x", "x", batch="b")
            set_status(conn, "b", "open")
        item_id = owner_client.get("/api/next").json()["item_id"]
        assert item_id == "x_snli/snli:246#snli_test_8206"
        assert owner_client.get("/api/lock/status/x_snli/snli:246%23snli_test_8206").json()["locked"]

    def test_gold_absent_or_partial(self):
        row = dict(by_id("fever/eval/5"))
        row.pop("gold")
        assert parse_row(row)[0].gold is None
        assert parse_row(dict(row, gold={}))[0].gold is None

    def test_sentence_choice_keys_take_option_spans(self):
        """piqa-style rows use the answer text as key and description."""
        from e13_labeler.labelling import Span, Submission, validate_submission

        key = "Use a spoon to stir the paint."
        q = {"type": "choice", "instructions": "Which works?", "criteria": {key: key, "Use a fork.": "Use a fork."}}
        span = Span(side="option", role="unsupported", text="spoon", option=key, start=6, end=11)
        validate_submission(Submission(True, [], None, [span], False, None), state="s", state_format="text",
                            question=q, reason_set=["unrelated"], span_policy={}, require_note=False)
