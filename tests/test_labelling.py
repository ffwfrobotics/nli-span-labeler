"""
Tests for the reasons task: blind payload, submission rules, skip, timing and
the assignment queue (requirements §4.2, §4.5, §5.3).
"""
import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e13_labeler.labelling import (
    PolicyViolation,
    Span,
    Submission,
    SubmissionError,
    reasons_json,
    resolve_pointer,
    validate_submission,
)
from e13_labeler.reasons import DEFAULT_SPAN_POLICY, REASONS
from tests.conftest import login, make_labeler

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_pool.jsonl"
ALERT = "typed_decisions/security_incidents_000085"
BBC = "bbc_news/eval/73#q0"
FEVER_STATE = "The tower was completed in 1889 and is 330 metres tall."
Y0, Y1 = FEVER_STATE.index("1889"), FEVER_STATE.index("1889") + 4
H0, H1 = FEVER_STATE.index("330"), FEVER_STATE.index("330") + 3


@pytest.fixture
def pilot(db):
    """The synthetic pool imported into an open batch 'pilot'."""
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.importer import import_file

    with get_db() as conn:
        import_file(conn, FIXTURE, batch="pilot")
        set_status(conn, "pilot", "open")


def only_item(item_id):
    """Shrink the pilot batch to one item so /api/next is deterministic."""
    from e13_labeler.db import get_db

    with get_db() as conn:
        conn.execute("DELETE FROM batch_items WHERE item_id != ?", (item_id,))


def submit(client, item_id, **body):
    return client.post("/api/annotations", json={"item_id": item_id, **body})


# ============================================================================
# Pure validation
# ============================================================================

TEXT_Q = {"type": "noul", "instructions": "The tower is over 300 metres tall."}


def check(sub, state=FEVER_STATE, state_format="text", question=TEXT_Q, reason_set=REASONS,
          span_policy=DEFAULT_SPAN_POLICY, require_note=False):
    validate_submission(sub, state=state, state_format=state_format, question=question,
                        reason_set=list(reason_set), span_policy=span_policy, require_note=require_note)


def sub(answerable=False, reasons=(), note=None, spans=(), override=False):
    return Submission(answerable, list(reasons), note, list(spans), override, None)


class TestRules:
    def test_answerable_is_exclusive(self):
        """FR-13: reject empty submissions and answerable plus a reason."""
        with pytest.raises(SubmissionError, match="answerable or at least one reason"):
            check(sub())
        with pytest.raises(SubmissionError, match="excludes"):
            check(sub(answerable=True, reasons=["ambiguous"]))
        check(sub(answerable=True))
        check(sub(reasons=["ambiguous", "underspecified"]))

    def test_reason_must_be_in_batch(self):
        with pytest.raises(SubmissionError, match="not in this batch"):
            check(sub(reasons=["subjective"]), reason_set=REASONS[:8])

    def test_note_rules(self):
        """FR-14: max length; required for prompting reasons when the batch says so."""
        with pytest.raises(SubmissionError, match="longer than"):
            check(sub(answerable=True, note="x" * 2001))
        with pytest.raises(SubmissionError, match="note is required"):
            check(sub(reasons=["ambiguous"]), require_note=True)
        check(sub(reasons=["ambiguous"], note="two readings"), require_note=True)
        check(sub(reasons=["not_enough_info"]), require_note=True)

    def test_role_side_rules(self):
        """FR-15: unsupported is option-only, framing is state-only."""
        bad = Span(side="state", role="unsupported", text="tower", start=4, end=9)
        with pytest.raises(SubmissionError, match="option-side spans only"):
            check(sub(answerable=True, spans=[bad]))
        q = {"type": "choice", "instructions": "?", "criteria": {"a": "alpha", "b": "beta"}}
        bad = Span(side="option", role="framing", text="alpha", option="a", start=0, end=5)
        with pytest.raises(SubmissionError, match="state-side spans only"):
            check(sub(answerable=True, spans=[bad]), question=q)

    def test_span_reasons_must_be_checked(self):
        """FR-16."""
        span = Span(side="state", role="support", text="1889", start=Y0, end=Y1, reasons=["stale_state"])
        with pytest.raises(SubmissionError, match="unchecked reason"):
            check(sub(reasons=["not_enough_info"], spans=[span]))
        check(sub(reasons=["stale_state"], spans=[span]))

    def test_option_names(self):
        span = Span(side="state", role="support", text="1889", start=Y0, end=Y1, option="maybe")
        with pytest.raises(SubmissionError, match="unknown option"):
            check(sub(answerable=True, spans=[span]))
        check(sub(answerable=True, spans=[Span(side="state", role="support", text="1889", start=Y0, end=Y1,
                                                option="true")]))

    def test_text_slice(self):
        """FR-17 for text states."""
        with pytest.raises(SubmissionError, match="not the slice"):
            check(sub(answerable=True, spans=[Span(side="state", role="support", text="1890", start=Y0, end=Y1)]))
        with pytest.raises(SubmissionError, match="start and end"):
            check(sub(answerable=True, spans=[Span(side="state", role="support", text="1889", start=26)]))

    def test_offsets_count_code_points(self):
        """Offsets are code points, as Python slices them, not UTF-16 units (emoji are 2 in JS)."""
        state = "Party 🎉🎉 time:\r\nthe café opens at 9 😀 sharp."
        start = state.index("sharp")
        assert start == 38  # in UTF-16 units (JS string indices) it would be 41: three emoji precede it
        check(sub(answerable=True, spans=[Span(side="state", role="support", text="sharp", start=start, end=start + 5)]),
              state=state)
        with pytest.raises(SubmissionError, match="not the slice"):
            check(sub(answerable=True, spans=[Span(side="state", role="support", text="sharp", start=41, end=46)]),
                  state=state)

    def test_json_pointer_spans(self):
        """FR-17 for JSON states: pointer + offsets in a string value, or a bare pointer."""
        state = json.dumps({"alert": {"evidence": "84 failed attempts", "count": 84, "tags": ["a"]}})
        q = {"type": "noul"}
        ok = [
            Span(side="state", role="support", text="failed", pointer="/alert/evidence", start=3, end=9),
            Span(side="state", role="support", text="84", pointer="/alert/count"),
            Span(side="state", role="support", text="tags", pointer="/alert/tags"),
        ]
        check(sub(answerable=True, spans=ok), state=state, state_format="json", question=q)
        for bad, msg in [
            (Span(side="state", role="support", text="failed", pointer="/alert/nope", start=3, end=9), "does not resolve"),
            (Span(side="state", role="support", text="84", pointer="/alert/count", start=0, end=2), "string value"),
            (Span(side="state", role="support", text="failed", start=3, end=9), "need a pointer"),
            (Span(side="state", role="support", text="fail", pointer="/alert/evidence", start=3, end=9), "not the slice"),
        ]:
            with pytest.raises(SubmissionError, match=msg):
                check(sub(answerable=True, spans=[bad]), state=state, state_format="json", question=q)

    def test_rfc6901_escapes(self):
        doc = {"a/b": {"m~n": [10, 20]}}
        assert resolve_pointer(doc, "/a~1b/m~0n/1")[2] == 20
        with pytest.raises(KeyError):
            resolve_pointer(doc, "/a~1b/m~0n/01")

    def test_option_side_spans(self):
        """FR-17 / rule 9: offsets into the option's description."""
        q = {"type": "score", "instructions": "?", "criteria": ["Low: minor", "High: major"]}
        check(sub(answerable=True, spans=[Span(side="option", role="unsupported", text="major", option="1",
                                                start=6, end=11)]), question=q)
        with pytest.raises(SubmissionError, match="name their option"):
            check(sub(answerable=True, spans=[Span(side="option", role="unsupported", text="major",
                                                    start=6, end=11)]), question=q)
        with pytest.raises(SubmissionError, match="no description"):
            check(sub(answerable=True, spans=[Span(side="option", role="refute", text="x", option="true",
                                                    start=0, end=1)]))

    def test_span_policy_and_override(self):
        """FR-19: conflicting_evidence needs support + refute on one option; override is recorded."""
        s = sub(reasons=["conflicting_evidence"])
        with pytest.raises(PolicyViolation, match="same option"):
            check(s)
        s = sub(reasons=["conflicting_evidence"], override=True)
        check(s)
        assert s.policy_override is True
        spans = [Span(side="state", role="support", text="330", start=H0, end=H1, option="true",
                      reasons=["conflicting_evidence"]),
                 Span(side="state", role="refute", text="1889", start=Y0, end=Y1, option="true",
                      reasons=["conflicting_evidence"])]
        s = sub(reasons=["conflicting_evidence"], spans=spans, override=True)
        check(s)
        assert s.policy_override is False  # nothing was overridden

    def test_policy_non_factual_support(self):
        with pytest.raises(PolicyViolation, match="framing"):
            check(sub(reasons=["non_factual_support"],
                      spans=[Span(side="state", role="support", text="330", start=H0, end=H1,
                                  reasons=["non_factual_support"])]))

    def test_reasons_json_marks_unasked_as_null(self):
        """§5.1 / FR-33: reasons outside the reason set are null, not false."""
        got = reasons_json(["ambiguous"], REASONS[:8])
        assert got["ambiguous"] is True and got["unrelated"] is False
        assert got["no_option_fits"] is None and got["subjective"] is None


# ============================================================================
# API
# ============================================================================

class TestNextIsBlind:
    def test_payload_shape(self, pilot, owner_client: TestClient):
        """FR-12 / §5.3: none of gold, source, e13, model_answers, is_gold_probe, others' labels."""
        only_item(f"{ALERT}#severity")
        payload = owner_client.get("/api/next").json()
        assert set(payload) == {"item_id", "lock_until", "state", "state_format", "question", "reason_set",
                                "task_type", "span_policy", "require_note", "asof", "progress"}
        assert set(payload["question"]) <= {"type", "instructions", "criteria"}
        assert payload["asof"] == "2026-10-05"  # from the row's e13.asof
        text = json.dumps(payload)
        for hidden in ('"gold', '"source', '"e13', '"model_answers', '"is_gold_probe', "candidate_for",
                       "clefflash", "typed_decisions\"", "abc123", "labels_of_others", "generator"):
            assert hidden not in text, hidden
        assert payload["progress"]["batch"] == "pilot"
        assert payload["reason_set"] == list(REASONS)

    def test_public_never_served_hidden_tiers(self, pilot, public_client: TestClient):
        """FR-50 / FR-57 for next: a public labeler only gets items whose text is libre."""
        from e13_labeler.db import get_db

        seen = set()
        while (r := public_client.get("/api/next")).status_code == 200:
            item_id = r.json()["item_id"]
            seen.add(item_id)
            assert submit(public_client, item_id, answerable=True).status_code == 200
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT visibility, permissions FROM items WHERE item_id IN ({','.join('?' * len(seen))})",
                tuple(seen)).fetchall()
        assert {r[0] for r in rows} == {"libre"}
        # 4 typed_decisions + 1 snli + fever, whose Jev answer only marks its release tier;
        # bbc_news (unverified) and mystery_source (unknown) stay internal
        assert {r[1] for r in rows} == {"libre", "jev"} and len(seen) == 6

    def test_asof_defaults_to_today(self, pilot, owner_client: TestClient):
        """§11 Q7: items without e13.asof are asked as of today, so generated items don't stand out."""
        from e13_labeler.auth import utcnow

        only_item(BBC)
        assert owner_client.get("/api/next").json()["asof"] == utcnow().date().isoformat()

    def test_jev_answer_batch_is_internal_only(self, pilot, fresh_client: TestClient):
        """A batch that shows Jev's answer makes the items carrying it internal-only."""
        from e13_labeler.db import get_db

        only_item("fever/eval/5#tall")
        make_labeler("pub")
        make_labeler("int", clearance="internal")
        with get_db() as conn:
            conn.execute("UPDATE batches SET show_model_answer = 'jev'")
        login(fresh_client, "pub")
        assert fresh_client.get("/api/next").status_code == 404
        assert submit(fresh_client, "fever/eval/5#tall", answerable=True).status_code == 404
        login(fresh_client, "int")
        assert fresh_client.get("/api/next").json()["item_id"] == "fever/eval/5#tall"

    def test_non_jev_answer_batch_stays_public(self, pilot, public_client: TestClient):
        from e13_labeler.db import get_db

        only_item(f"{ALERT}#credential_compromise")  # carries a clefflash answer
        with get_db() as conn:
            conn.execute("UPDATE batches SET show_model_answer = 'clefflash'")
        assert public_client.get("/api/next").status_code == 200

    def test_tier_ceiling_narrows(self, pilot, owner_client: TestClient):
        from e13_labeler.db import get_db

        with get_db() as conn:
            conn.execute("UPDATE batches SET tier_ceiling = 'libre'")
        while (r := owner_client.get("/api/next")).status_code == 200:
            assert not r.json()["item_id"].startswith(("bbc_news", "fever", "mystery"))
            submit(owner_client, r.json()["item_id"], answerable=True)

    def test_paused_labeler_gets_nothing(self, pilot, fresh_client: TestClient):
        from e13_labeler.db import get_db

        labeler = make_labeler("p")
        login(fresh_client, "p")
        with get_db() as conn:
            conn.execute("UPDATE labelers SET status = 'paused' WHERE id = ?", (labeler["id"],))
        assert fresh_client.get("/api/next").status_code == 403

    def test_draft_batch_not_served(self, db, owner_client: TestClient):
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_file

        with get_db() as conn:
            import_file(conn, FIXTURE, batch="draft-only")
        assert owner_client.get("/api/next").status_code == 404


class TestSubmit:
    def test_submit_stores_annotation_and_spans(self, pilot, owner_client: TestClient):
        only_item("fever/eval/5#tall")
        item = owner_client.get("/api/next").json()
        response = submit(owner_client, item["item_id"], reasons=["stale_state"], note=" old figure ",
                          active_ms=4200,
                          spans=[{"side": "state", "role": "support", "text": "1889", "start": Y0, "end": Y1,
                                  "option": "true", "reasons": ["stale_state"]}])
        assert response.status_code == 200, response.text
        from e13_labeler.db import get_db

        with get_db() as conn:
            ann = conn.execute("SELECT * FROM annotations").fetchone()
            spans = conn.execute("SELECT * FROM spans").fetchall()
            lock = conn.execute("SELECT * FROM locks").fetchone()
        reasons = json.loads(ann["reasons_json"])
        assert reasons["stale_state"] is True and reasons["unrelated"] is False
        assert ann["answerable"] == 0 and ann["note"] == "old figure" and ann["active_ms"] == 4200
        assert ann["wall_ms"] is not None and ann["app_version"]
        assert [(s["text"], s["role"], json.loads(s["reasons_json"])) for s in spans] == [("1889", "support", ["stale_state"])]
        assert FEVER_STATE[spans[0]["start"]:spans[0]["end"]] == spans[0]["text"]
        assert lock is None  # released on submit

    def test_validation_errors_are_422(self, pilot, owner_client: TestClient):
        only_item("fever/eval/5#tall")
        item_id = owner_client.get("/api/next").json()["item_id"]
        response = submit(owner_client, item_id)
        assert response.status_code == 422
        assert response.json()["detail"]["policy"] is False

    def test_policy_block_then_override(self, pilot, owner_client: TestClient):
        """FR-19: blocked, then saved with the override flag."""
        only_item("fever/eval/5#tall")
        item_id = owner_client.get("/api/next").json()["item_id"]
        response = submit(owner_client, item_id, reasons=["conflicting_evidence"])
        assert response.status_code == 422 and response.json()["detail"]["policy"] is True
        response = submit(owner_client, item_id, reasons=["conflicting_evidence"], policy_override=True)
        assert response.status_code == 200 and response.json()["policy_override"] is True

    def test_cannot_label_twice(self, pilot, owner_client: TestClient):
        only_item("fever/eval/5#tall")
        item_id = owner_client.get("/api/next").json()["item_id"]
        assert submit(owner_client, item_id, answerable=True).status_code == 200
        assert submit(owner_client, item_id, answerable=True).status_code == 409

    def test_cannot_submit_someone_elses_lock(self, pilot, fresh_client: TestClient):
        only_item(f"{ALERT}#severity")
        make_labeler("a")
        make_labeler("b")
        login(fresh_client, "a")
        item_id = fresh_client.get("/api/next").json()["item_id"]
        login(fresh_client, "b")
        assert submit(fresh_client, item_id, answerable=True).status_code == 409

    def test_public_cannot_submit_hidden_item(self, pilot, public_client: TestClient):
        assert submit(public_client, BBC, answerable=True).status_code == 404


class TestSkip:
    def test_skip_never_served_again(self, pilot, owner_client: TestClient):
        """FR-20."""
        only_item(BBC)
        assert owner_client.get("/api/next").json()["item_id"] == BBC
        assert owner_client.post("/api/skip", json={"item_id": BBC, "code": "too_long"}).status_code == 200
        assert owner_client.get("/api/next").status_code == 404

    def test_skip_needs_known_code(self, pilot, owner_client: TestClient):
        only_item(BBC)
        owner_client.get("/api/next")
        assert owner_client.post("/api/skip", json={"item_id": BBC, "code": "bored"}).status_code == 422


class TestBatches:
    def test_overlap_must_be_positive(self, db):
        """Overlap 1 is allowed (owner, 2026-10-05: 'we might have to make 1 work'); 0 is not."""
        import sqlite3

        from e13_labeler.db import get_db

        with get_db() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO batches (name, reason_set_json, overlap_target) VALUES ('x', '[]', 0)")
        with get_db() as conn:
            conn.execute("INSERT INTO batches (name, reason_set_json, overlap_target) VALUES ('y', '[]', 1)")

    def test_admin_opens_and_lists(self, db, owner_client: TestClient):
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_file

        with get_db() as conn:
            import_file(conn, FIXTURE, batch="b1")
        assert owner_client.post("/api/admin/batches/b1/status", json={"status": "open"}).status_code == 200
        batches = owner_client.get("/api/admin/batches").json()["batches"]
        assert [(b["name"], b["status"], b["n_items"]) for b in batches] == [("b1", "open", 8)]
        assert owner_client.post("/api/admin/batches/b1/status", json={"status": "gone"}).status_code == 422


class TestAssignment:
    @pytest.mark.parametrize("overlap", [1, 2, 3])
    def test_property_five_labelers_two_hundred_items(self, db, fresh_client: TestClient, overlap):
        """
        FR-32: with 5 labelers over 200 items, no labeler gets an item twice, and
        every item ends with exactly overlap_target labels, for overlap 1 to 3.
        """
        from e13_labeler.batches import configure, set_status
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_rows

        n_items = 200 if overlap == 2 else 60  # the spec's case at full size; the others smaller
        rows = [json.dumps({"id": f"snli/p/{i}", "source": "snli", "state": f"state {i}",
                            "questions": {"q": {"type": "noul"}}}) for i in range(n_items)]
        with get_db() as conn:
            import_rows(conn, rows, "generated", "x", batch="prop")
            configure(conn, "prop", overlap_target=overlap)
            set_status(conn, "prop", "open")
        from e13_labeler.app import app

        names = [f"lab{i}" for i in range(5)]
        clients = {}
        for n in names:
            make_labeler(n)
            clients[n] = TestClient(app)  # one session per labeler, so each logs in once
            login(clients[n], n)

        rng = random.Random(3)
        served = {n: [] for n in names}
        active = list(names)
        while active:
            n = rng.choice(active)
            client = clients[n]
            response = client.get("/api/next")
            if response.status_code == 404:
                active.remove(n)
                continue
            item_id = response.json()["item_id"]
            served[n].append(item_id)
            if rng.random() < 0.05:
                client.post("/api/skip", json={"item_id": item_id, "code": "cannot_judge"})
            else:
                body = {"answerable": True} if rng.random() < 0.5 else {"reasons": ["not_enough_info"]}
                assert submit(client, item_id, **body).status_code == 200

        for n in names:
            assert len(served[n]) == len(set(served[n])), n
        with get_db() as conn:
            counts = [r[0] for r in conn.execute(
                """SELECT (SELECT COUNT(*) FROM annotations a WHERE a.item_id = i.item_id
                           AND a.skipped_code IS NULL) FROM items i""")]
        assert counts == [overlap] * n_items

    def test_pairs_completed_first(self, db, fresh_client: TestClient):
        """FR-32 ordering: an item someone else labelled comes before a fresh one."""
        from e13_labeler.batches import set_status
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_rows

        rows = [json.dumps({"id": f"snli/p/{i}", "source": "snli", "state": f"s{i}",
                            "questions": {"q": {"type": "noul"}}}) for i in range(30)]
        with get_db() as conn:
            import_rows(conn, rows, "generated", "x", batch="prop")
            set_status(conn, "prop", "open")
        make_labeler("a")
        make_labeler("b")
        login(fresh_client, "a")
        first = fresh_client.get("/api/next").json()["item_id"]
        submit(fresh_client, first, answerable=True)
        login(fresh_client, "b")
        assert fresh_client.get("/api/next").json()["item_id"] == first

    def test_held_lock_returned(self, pilot, owner_client: TestClient):
        first = owner_client.get("/api/next").json()["item_id"]
        assert owner_client.get("/api/next").json()["item_id"] == first
