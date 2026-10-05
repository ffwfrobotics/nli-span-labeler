"""
M2 adjudication (FR-43): the queue, anonymised side-by-side labels, versioned
adjudications stored apart from raw labels, promotion to gold.
"""
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import insert_item, login, make_labeler
from tests.test_exports import annotate


@pytest.fixture
def labelled(owner):
    """Three items with two human labels each: one agreed, one disagreeing on one key, one on three."""
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.reasons import REASONS

    for i in ("agree", "one", "three", "single"):
        insert_item(f"t/{i}#q", state=f"The {i} state was dated 2019.")
    with get_db() as conn:
        conn.execute("INSERT INTO batches (name, reason_set_json, overlap_target) VALUES ('pilot', ?, 2)",
                     (json.dumps(list(REASONS)),))
        conn.executemany("INSERT INTO batch_items (batch_id, item_id) VALUES (1, ?)",
                         [(f"t/{i}#q",) for i in ("agree", "one", "three", "single")])
        set_status(conn, "pilot", "open")
    b = make_labeler("b", clearance="internal")["id"]
    annotate("t/agree#q", owner["id"], reasons=["unrelated"])
    annotate("t/agree#q", b, reasons=["unrelated"])
    annotate("t/one#q", owner["id"], reasons=["ambiguous"], note="two readings")
    annotate("t/one#q", b, reasons=["ambiguous", "underspecified"])
    annotate("t/three#q", owner["id"], answerable=True)
    annotate("t/three#q", b, reasons=["not_enough_info", "subjective"])
    annotate("t/single#q", owner["id"], reasons=["unrelated"])
    return owner


def alpha_now():
    from e13_labeler.analysis import report
    from e13_labeler.db import get_db
    from e13_labeler.records import agreement_inputs

    with get_db() as conn:
        return report(agreement_inputs(conn), n_boot=0)["inter_rater"]


class TestQueue:
    def test_sorted_by_disagreement(self, labelled, owner_client: TestClient):
        items = owner_client.get("/api/admin/adjudication").json()["items"]
        assert [i["item_id"] for i in items] == ["t/three#q", "t/one#q"]
        assert items[0]["disagree_on"] == ["answerable", "not_enough_info", "subjective"]
        assert items[1]["disagree_on"] == ["underspecified"]

    def test_detail_is_anonymised(self, labelled, owner_client: TestClient):
        d = owner_client.get("/api/admin/adjudication/t/one%23q").json()
        assert [l["labeler"] for l in d["labels"]] == ["L-a", "L-b"]
        assert "L01" not in json.dumps(d["labels"]) and "L02" not in json.dumps(d["labels"])
        assert {tuple(l["reasons"]) for l in d["labels"]} == {("ambiguous",), ("ambiguous", "underspecified")}
        assert "two readings" in [l["note"] for l in d["labels"]]
        assert d["adjudication"] is None and d["state"].startswith("The one state")

    def test_internal_admins_only(self, labelled, fresh_client: TestClient):
        make_labeler("padm", clearance="public", role="admin")
        login(fresh_client, "padm")
        assert fresh_client.get("/api/admin/adjudication").status_code == 403
        make_labeler("lab", clearance="internal")
        login(fresh_client, "lab")
        assert fresh_client.get("/api/admin/adjudication").status_code == 403


class TestSave:
    def test_alpha_unchanged_and_export_filled(self, labelled, owner_client: TestClient):
        """FR-43 test: after adjudication, α is unchanged and the export has adjudicated filled."""
        from e13_labeler.db import get_db
        from e13_labeler.exports import training_rows
        from e13_labeler.records import Filters

        before = alpha_now()
        response = owner_client.post("/api/admin/adjudication/t/one%23q",
                                     json={"reasons": ["ambiguous"], "note": "underspecified doesn't apply"})
        assert response.status_code == 200 and response.json()["adjudication"]["version"] == 1
        assert alpha_now() == before
        with get_db() as conn:
            rows = {r["id"] + "#" + r["qid"]: r for r in training_rows(conn, Filters())}
            raw = conn.execute("SELECT COUNT(*) FROM annotations WHERE item_id = 't/one#q'").fetchone()[0]
        adj = rows["t/one#q"]["human"]["adjudicated"]
        assert adj["reasons"] == ["ambiguous"] and adj["answerable"] is False and adj["adjudicator"] == "L01"
        assert "note" not in adj
        assert rows["t/agree#q"]["human"]["adjudicated"] is None
        assert raw == 2  # raw labels untouched
        # done items leave the queue unless asked for
        assert [i["item_id"] for i in owner_client.get("/api/admin/adjudication").json()["items"]] == ["t/three#q"]
        done = owner_client.get("/api/admin/adjudication?include_done=true").json()["items"]
        assert {i["item_id"]: i["adjudicated_version"] for i in done}["t/one#q"] == 1

    def test_versions_kept(self, labelled, owner_client: TestClient):
        from e13_labeler.db import get_db

        owner_client.post("/api/admin/adjudication/t/one%23q", json={"reasons": ["ambiguous"]})
        second = owner_client.post("/api/admin/adjudication/t/one%23q", json={"reasons": ["underspecified"]}).json()
        assert second["adjudication"]["version"] == 2
        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM adjudications").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'adjudicate'").fetchone()[0] == 2
        d = owner_client.get("/api/admin/adjudication/t/one%23q").json()
        assert d["adjudication"]["reasons"] == ["underspecified"]

    def test_promote_to_gold(self, labelled, owner_client: TestClient):
        response = owner_client.post("/api/admin/adjudication/t/three%23q", json={
            "reasons": ["not_enough_info"], "promote": True, "explanation": "nothing says which",
            "alternatives": {"not_enough_info": ["underspecified"]}})
        assert response.status_code == 200
        gold = owner_client.get("/api/admin/gold").json()["gold"]
        assert [(g["item_id"], g["reasons"], g["explanation"]) for g in gold] == [
            ("t/three#q", ["not_enough_info"], "nothing says which")]
        # still adjudicable after it became gold
        assert owner_client.get("/api/admin/adjudication/t/three%23q").status_code == 200

    def test_validated_like_gold(self, labelled, owner_client: TestClient):
        response = owner_client.post("/api/admin/adjudication/t/one%23q", json={"reasons": ["stale_state"]})
        assert response.status_code == 422  # stale_state needs its span (hard rule)
        state = "The one state was dated 2019."
        start = state.index("2019")
        span = {"side": "state", "role": "support", "text": "2019", "start": start, "end": start + 4,
                "option": "true", "reasons": ["stale_state"]}
        assert owner_client.post("/api/admin/adjudication/t/one%23q",
                                 json={"reasons": ["stale_state"], "spans": [span]}).status_code == 200

    def test_nothing_to_adjudicate(self, labelled, owner_client: TestClient):
        insert_item("t/none#q")
        assert owner_client.get("/api/admin/adjudication/t/none%23q").status_code == 404
