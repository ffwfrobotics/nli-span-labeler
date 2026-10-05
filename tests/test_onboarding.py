"""
M2 onboarding and gold: the guideline gate (FR-26), the quiz (FR-27), hidden
gold (FR-28), auto-pause and the retraining quiz (FR-30), libre-only gold for
public labelers (FR-58).
"""
import json
import random

import pytest
from fastapi.testclient import TestClient

from tests.conftest import insert_item, login, make_labeler

# Gold covering every reason (conflicting_evidence on two items, so missing it
# twice fails the established-reason rule), plus answerable items.
GOLD = [
    ("g/ce1#q", ["conflicting_evidence"]),
    ("g/ce2#q", ["conflicting_evidence", "not_enough_info"]),
    ("g/un#q", ["unrelated"]),
    ("g/nfs#q", ["non_factual_support"]),
    ("g/ss#q", ["stale_state"]),
    ("g/am#q", ["ambiguous"]),
    ("g/us#q", ["underspecified"]),
    ("g/fp#q", ["false_premise"]),
    ("g/no#q", ["no_option_fits"]),
    ("g/sb#q", ["subjective"]),
    ("g/a1#q", []),
    ("g/a2#q", []),
]


def seed_gold(gold=GOLD, permissions="libre", prefix=""):
    """Gold rows straight into the table (spans aren't scored, so none are needed)."""
    from e13_labeler.db import get_db

    for item_id, reasons in gold:
        insert_item(prefix + item_id, permissions, state=f"state of {item_id}")
        with get_db() as conn:
            conn.execute("INSERT INTO gold (item_id, reasons_json, explanation) VALUES (?, ?, ?)",
                         (prefix + item_id, json.dumps({"answerable": not reasons, "reasons": reasons}),
                          f"why {item_id}"))


def gold_answer(item_id: str, gold=GOLD, prefix="") -> dict:
    reasons = dict((prefix + i, r) for i, r in gold)[item_id]
    return {"item_id": item_id, "answerable": not reasons, "reasons": reasons}


def newcomer(client: TestClient, login_name="newbie", clearance="public") -> TestClient:
    make_labeler(login_name, clearance=clearance, status="onboarding")
    login(client, login_name)
    return client


def take_quiz(client: TestClient, answer_fn) -> dict:
    assert client.post("/api/quiz/start").status_code == 200
    while True:
        q = client.get("/api/quiz").json()
        if q["finished"]:
            return q
        feedback = client.post("/api/quiz/answer", json=answer_fn(q["item"]["item_id"])).json()
        assert "gold" in feedback and feedback["gold"]["explanation"].startswith("why")
        if "result" in feedback:
            return feedback["result"]


class TestGuideline:
    def test_page_has_every_reason(self, owner_client: TestClient):
        from e13_labeler.reasons import REASONS

        page = owner_client.get("/api/guideline").json()
        assert [r["key"] for r in page["reasons"]] == list(REASONS)
        for r in page["reasons"]:
            assert r["definition"] and r["span_rule"] and r["positive"]["why"] and r["negative"]["why"]
        assert {r["key"] for r in page["reasons"] if r["span_rule_hard"]} == {"conflicting_evidence", "stale_state"}

    def test_new_account_cannot_label_before_guideline(self, db, fresh_client: TestClient):
        """FR-26 test: a new account cannot reach /label before viewing the guideline."""
        seed_gold()
        newcomer(fresh_client)
        response = fresh_client.get("/api/next")
        assert response.status_code == 403 and response.json()["detail"]["quiz"] == "onboarding"
        assert fresh_client.get("/api/onboarding").json()["next"] == "guideline"
        assert fresh_client.post("/api/quiz/start").status_code == 403
        fresh_client.get("/api/guideline")
        assert fresh_client.get("/api/onboarding").json()["next"] == "quiz"
        assert fresh_client.post("/api/quiz/start").status_code == 200

    def test_annotation_records_guideline_version(self, owner_client: TestClient):
        from e13_labeler import guideline
        from e13_labeler.batches import set_status
        from e13_labeler.db import get_db

        insert_item("r#q")
        with get_db() as conn:
            conn.execute("INSERT INTO batches (name, reason_set_json) VALUES ('b', '[\"unrelated\"]')")
            conn.execute("INSERT INTO batch_items (batch_id, item_id) VALUES (1, 'r#q')")
            set_status(conn, "b", "open")
        owner_client.get("/api/next")
        owner_client.post("/api/annotations", json={"item_id": "r#q", "answerable": True})
        with get_db() as conn:
            assert conn.execute("SELECT guideline_version FROM annotations").fetchone()[0] == guideline.version()


class TestQuiz:
    def test_all_correct_passes(self, db, fresh_client: TestClient):
        """FR-27 test: a scripted user with all-correct answers passes."""
        seed_gold()
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")
        result = take_quiz(fresh_client, gold_answer)
        assert result["passed"] and result["accuracy"] == 1.0 and result["status"] == "active"
        assert fresh_client.get("/api/me").json()["status"] == "active"
        assert fresh_client.get("/api/next").status_code == 404  # active; just nothing to label

    def test_never_checking_conflicting_evidence_fails(self, db, fresh_client: TestClient):
        """FR-27 test: one who never checks conflicting_evidence fails, even at >= 75% accuracy."""
        seed_gold()
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")

        def answer(item_id):
            a = gold_answer(item_id)
            a["reasons"] = [r for r in a["reasons"] if r != "conflicting_evidence"]
            a["answerable"] = not a["reasons"]
            return a

        result = take_quiz(fresh_client, answer)
        assert result["accuracy"] >= 0.83
        assert not result["passed"] and result["missed_too_often"] == ["conflicting_evidence"]
        assert result["retake"] is True

    def test_retake_once_then_review(self, db, fresh_client: TestClient):
        seed_gold()
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")
        wrong = lambda item_id: {"item_id": item_id, "answerable": False, "reasons": ["subjective"]}  # noqa: E731
        assert not take_quiz(fresh_client, wrong)["passed"]
        # the retake needs the guideline again
        assert fresh_client.get("/api/onboarding").json()["next"] == "guideline"
        assert fresh_client.post("/api/quiz/start").status_code == 403
        fresh_client.get("/api/guideline")
        result = take_quiz(fresh_client, wrong)
        assert (result["status"], result["pause_reason"]) == ("paused", "quiz_failed")
        assert fresh_client.post("/api/quiz/start").status_code == 403

    def test_alternatives_and_jaccard(self):
        from e13_labeler.quality import answer_set, score

        gold = {"answerable": False, "reasons": ["ambiguous"], "alternatives": {"ambiguous": ["underspecified"]}}
        assert score(answer_set(False, ["underspecified"]), gold)["correct"]
        assert not score(answer_set(False, ["subjective"]), gold)["correct"]
        five = {"answerable": False, "reasons": ["unrelated", "ambiguous", "subjective", "stale_state", "false_premise"],
                "alternatives": {}}
        assert score(answer_set(False, five["reasons"][:4]), five)["jaccard"] == 0.8
        assert score(answer_set(False, five["reasons"][:4]), five)["correct"]
        assert not score(answer_set(False, five["reasons"][:3]), five)["correct"]
        assert score(answer_set(True, []), {"answerable": True, "reasons": [], "alternatives": {}})["correct"]

    def test_selection_covers_reasons(self, db):
        from e13_labeler.db import get_db
        from e13_labeler.quiz import _gold_pool, select_items
        from e13_labeler.reasons import REASONS

        seed_gold()
        seed_gold(prefix="x")  # 24 to choose from
        with get_db() as conn:
            pool = _gold_pool(conn, "internal")
        by_id = {g["item_id"]: g for g in pool}
        for seed in range(20):
            chosen = [by_id[i] for i in select_items(pool, 12, random.Random(seed))]
            assert len(set(g["item_id"] for g in chosen)) == 12
            assert all(any(r in g["reasons"] for g in chosen) for r in REASONS)
            assert sum(g["answerable"] for g in chosen) >= 2

    def test_not_enough_gold(self, db, fresh_client: TestClient):
        seed_gold(GOLD[:5])
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")
        response = fresh_client.post("/api/quiz/start")
        assert response.status_code == 503 and "not enough gold" in response.json()["detail"]

    def test_public_quiz_is_libre_only(self, db, fresh_client: TestClient):
        """FR-58 test: the quiz for a public user draws only libre gold."""
        seed_gold(permissions="restricted", prefix="r")
        seed_gold(permissions="libre")
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")
        seen = []

        def answer(item_id):
            seen.append(item_id)
            return gold_answer(item_id)

        assert take_quiz(fresh_client, answer)["passed"]
        assert seen and not any(i.startswith("rg/") for i in seen)

    def test_libre_gold_warning(self, db):
        from e13_labeler.batches import libre_gold_warning
        from e13_labeler.db import get_db

        make_labeler("pub")
        seed_gold(GOLD[:5])
        with get_db() as conn:
            assert "FR-58" in libre_gold_warning(conn)[0]
        seed_gold(GOLD[5:], prefix="")
        seed_gold(GOLD[:1], prefix="z")
        with get_db() as conn:
            assert libre_gold_warning(conn) == []


def _batch(n_items: int, name="b"):
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.reasons import REASONS

    for i in range(n_items):
        insert_item(f"plain/{i}#q")
    with get_db() as conn:
        conn.execute("INSERT INTO batches (name, reason_set_json, overlap_target) VALUES (?, ?, 1)",
                     (name, json.dumps(list(REASONS))))
        conn.executemany("INSERT INTO batch_items (batch_id, item_id) VALUES (1, ?)",
                         [(f"plain/{i}#q",) for i in range(n_items)])
        set_status(conn, name, "open")


class TestHiddenGold:
    def test_rate_after_warmup(self):
        """FR-28 test: over 1,000 next calls after warm-up, the gold share is 5% ± 2%."""
        from e13_labeler.app import should_probe

        rng = random.Random(7)
        os_env = {"E13_GOLD_RATE_NEW": "0.20", "E13_GOLD_RATE": "0.05"}
        with pytest.MonkeyPatch.context() as mp:
            for k, v in os_env.items():
                mp.setenv(k, v)
            share = sum(should_probe(rng, 50 + i) for i in range(1000)) / 1000
            warm = sum(should_probe(rng, i % 50) for i in range(1000)) / 1000
        assert 0.03 <= share <= 0.07
        assert 0.16 <= warm <= 0.24

    def test_probe_served_blind_and_excluded(self, db, fresh_client: TestClient, monkeypatch):
        """Probes look like ordinary items, give no feedback, and stay out of α and exports."""
        from e13_labeler.db import get_db
        from e13_labeler.records import load_annotations

        monkeypatch.setenv("E13_GOLD_RATE_NEW", "1")
        seed_gold(GOLD[:1])
        _batch(3)
        make_labeler("pub")
        login(fresh_client, "pub")
        payload = fresh_client.get("/api/next").json()
        assert payload["item_id"] == "g/ce1#q"
        plain = None
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "0")
        assert fresh_client.get("/api/next").json()["item_id"] == "g/ce1#q"  # the held lock comes back
        response = fresh_client.post("/api/annotations", json={"item_id": "g/ce1#q", "answerable": True})
        assert response.status_code == 200 and set(response.json()) == {"status", "annotation_id", "version",
                                                                         "policy_override"}
        plain = fresh_client.get("/api/next").json()
        assert set(plain) == set(payload)  # same shape as a probe
        with get_db() as conn:
            probe = conn.execute("SELECT * FROM annotations WHERE item_id = 'g/ce1#q'").fetchone()
            assert probe["is_gold_probe"] == 1 and probe["batch_id"] == 1
            from e13_labeler.records import Filters

            assert load_annotations(conn, Filters()) == []  # probes stay out of α and exports by default
            assert len(load_annotations(conn, Filters(include_gold=True))) == 1  # FR-47 "include gold"
        # never served twice
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "1")
        fresh_client.post("/api/annotations", json={"item_id": plain["item_id"], "answerable": True})
        assert fresh_client.get("/api/next").json()["item_id"].startswith("plain/")

    def test_no_probe_of_quiz_items(self, db, fresh_client: TestClient, monkeypatch):
        seed_gold()
        _batch(2)
        newcomer(fresh_client)
        fresh_client.get("/api/guideline")
        assert take_quiz(fresh_client, gold_answer)["passed"]
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "1")
        assert fresh_client.get("/api/next").json()["item_id"].startswith("plain/")


class TestAutoPause:
    def test_ten_failed_probes_pause_then_retraining(self, db, fresh_client: TestClient, monkeypatch):
        """FR-30 test: a labeler failing 10 gold items in a row gets paused; next offers the retraining quiz."""
        seed_gold()
        seed_gold(prefix="x")
        _batch(30)
        make_labeler("pub")
        login(fresh_client, "pub")
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "1")
        for n in range(10):
            item_id = fresh_client.get("/api/next").json()["item_id"]
            wrong = ["subjective"] if "subjective" not in str(gold_answer_any(item_id)) else ["unrelated"]
            assert fresh_client.post("/api/annotations", json={"item_id": item_id, "reasons": wrong,
                                                               "spans": []}).status_code == 200, n
        me = fresh_client.get("/api/me").json()
        assert (me["status"], me["pause_reason"]) == ("paused", "gold_accuracy")
        response = fresh_client.get("/api/next")
        assert response.status_code == 403 and response.json()["detail"]["quiz"] == "retraining"
        assert fresh_client.get("/api/onboarding").json()["quiz"] == "retraining"
        fresh_client.get("/api/guideline")
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "0")
        result = take_quiz(fresh_client, gold_answer_any)
        assert result["passed"] and result["kind"] == "retraining" and result["status"] == "active"
        # old failures don't count against the fresh start
        from e13_labeler.db import get_db
        from e13_labeler.quality import gold_accuracy

        with get_db() as conn:
            acc = gold_accuracy(conn, me["id"])
        assert acc["n_probes"] == 10 and acc["accuracy"] == 0 and acc["rolling_n"] == 0

    def test_nine_failures_do_not_pause(self, db, fresh_client: TestClient, monkeypatch):
        seed_gold()
        _batch(30)
        make_labeler("pub")
        login(fresh_client, "pub")
        monkeypatch.setenv("E13_GOLD_RATE_NEW", "1")
        for _ in range(9):
            item_id = fresh_client.get("/api/next").json()["item_id"]
            fresh_client.post("/api/annotations", json={"item_id": item_id, "reasons": ["unrelated"]
                              if "unrelated" not in str(gold_answer_any(item_id)) else ["subjective"]})
        assert fresh_client.get("/api/me").json()["status"] == "active"


def gold_answer_any(item_id: str) -> dict:
    prefix = "x" if item_id.startswith("xg/") else ""
    return gold_answer(item_id, prefix=prefix)
