"""
M2: edits and versions (FR-21), progress and ETA (FR-35), resolving flags (FR-44).
"""
import json

from fastapi.testclient import TestClient

from tests.conftest import insert_item, login, make_labeler


def open_batch(n_items: int, name: str = "b", overlap: int = 2, prefix: str = "it"):
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.reasons import REASONS

    for i in range(n_items):
        insert_item(f"{prefix}/{i}#q", state=f"state number {i}")
    with get_db() as conn:
        cur = conn.execute("INSERT INTO batches (name, reason_set_json, overlap_target) VALUES (?, ?, ?)",
                           (name, json.dumps(list(REASONS)), overlap))
        conn.executemany("INSERT INTO batch_items (batch_id, item_id) VALUES (?, ?)",
                         [(cur.lastrowid, f"{prefix}/{i}#q") for i in range(n_items)])
        set_status(conn, name, "open")


def label_one(client: TestClient, **body) -> dict:
    item = client.get("/api/next").json()
    response = client.post("/api/annotations", json={"item_id": item["item_id"], **(body or {"answerable": True})})
    assert response.status_code == 200, response.text
    return {**response.json(), "item_id": item["item_id"]}


class TestEdits:
    def test_edit_creates_version_two(self, db, owner_client: TestClient):
        """FR-21 test: an edit produces version 2, and the export has version: 2."""
        from e13_labeler.db import get_db
        from e13_labeler.records import Filters, load_annotations

        open_batch(3)
        saved = label_one(owner_client)
        entry = owner_client.get("/api/history").json()["submissions"][0]
        assert entry["annotation_id"] == saved["annotation_id"] and entry["version"] == 1
        detail = owner_client.get(f"/api/history/{saved['annotation_id']}").json()
        assert detail["edit"]["answerable"] is True and detail["state"] == "state number " + saved["item_id"][3]
        response = owner_client.put(f"/api/annotations/{saved['annotation_id']}", json={
            "item_id": saved["item_id"], "reasons": ["not_enough_info"], "note": "on second thought"})
        assert response.status_code == 200 and response.json()["version"] == 2
        with get_db() as conn:
            versions = conn.execute("SELECT version, answerable FROM annotations WHERE item_id = ? ORDER BY version",
                                    (saved["item_id"],)).fetchall()
            records = load_annotations(conn, Filters())
        assert [tuple(v) for v in versions] == [(1, 1), (2, 0)]  # the old version is kept
        assert len(records) == 1 and records[0]["version"] == 2 and records[0]["reasons"]["not_enough_info"]
        # history shows the latest; editing the latest again gives version 3
        entry = owner_client.get("/api/history").json()["submissions"][0]
        assert entry["version"] == 2
        assert owner_client.put(f"/api/annotations/{entry['annotation_id']}", json={
            "item_id": saved["item_id"], "answerable": True}).json()["version"] == 3

    def test_edit_counts_once_toward_overlap(self, db, owner_client: TestClient):
        from e13_labeler.db import get_db
        from e13_labeler.progress import progress

        open_batch(1, overlap=2)
        saved = label_one(owner_client)
        owner_client.put(f"/api/annotations/{saved['annotation_id']}", json={"item_id": saved["item_id"],
                                                                            "answerable": True})
        with get_db() as conn:
            assert progress(conn)["batches"][0]["items_by_label_count"]["1"] == 1

    def test_only_last_twenty(self, db, owner_client: TestClient):
        open_batch(22)
        saved = [label_one(owner_client) for _ in range(21)]
        listed = owner_client.get("/api/history").json()["submissions"]
        assert len(listed) == 20 and listed[0]["annotation_id"] == saved[-1]["annotation_id"]
        oldest = saved[0]
        response = owner_client.put(f"/api/annotations/{oldest['annotation_id']}",
                                    json={"item_id": oldest["item_id"], "answerable": True})
        assert response.status_code == 404

    def test_closed_batch_not_editable(self, db, owner_client: TestClient):
        from e13_labeler.batches import set_status
        from e13_labeler.db import get_db

        open_batch(2)
        saved = label_one(owner_client)
        with get_db() as conn:
            set_status(conn, "b", "closed")
        assert owner_client.get("/api/history").json()["submissions"][0]["editable"] is False
        response = owner_client.put(f"/api/annotations/{saved['annotation_id']}",
                                    json={"item_id": saved["item_id"], "answerable": True})
        assert response.status_code == 409

    def test_cannot_edit_someone_elses(self, db, owner_client: TestClient):
        open_batch(2)
        saved = label_one(owner_client)
        make_labeler("pub")
        login(owner_client, "pub")
        assert owner_client.get(f"/api/history/{saved['annotation_id']}").status_code == 404
        assert owner_client.put(f"/api/annotations/{saved['annotation_id']}",
                                json={"item_id": saved["item_id"], "answerable": True}).status_code == 404

    def test_edit_validated(self, db, owner_client: TestClient):
        open_batch(2)
        saved = label_one(owner_client)
        response = owner_client.put(f"/api/annotations/{saved['annotation_id']}",
                                    json={"item_id": saved["item_id"], "reasons": ["stale_state"]})
        assert response.status_code == 422  # hard span rule, as on first submit

    def test_history_hides_items_out_of_clearance(self, db, fresh_client: TestClient):
        from e13_labeler.db import get_db

        open_batch(2)
        make_labeler("pub")
        login(fresh_client, "pub")
        saved = label_one(fresh_client)
        with get_db() as conn:
            conn.execute("UPDATE items SET visibility = 'restricted' WHERE item_id = ?", (saved["item_id"],))
        assert fresh_client.get("/api/history").json()["submissions"] == []
        assert fresh_client.get(f"/api/history/{saved['annotation_id']}").status_code == 404


class TestProgress:
    def test_counts_match_database(self, db, owner_client: TestClient):
        """FR-35 test: the counts match the database after a simulated session."""
        from e13_labeler.db import get_db

        open_batch(10, overlap=2)
        make_labeler("a")
        make_labeler("c")
        for name, n in (("a", 6), ("c", 3)):
            with TestClient(owner_client.app) as client:
                login(client, name)
                for _ in range(n):
                    label_one(client)
        progress = owner_client.get("/api/admin/progress").json()
        batch = progress["batches"][0]
        with get_db() as conn:
            per_item = dict(conn.execute(
                """SELECT i.item_id, (SELECT COUNT(*) FROM annotations a WHERE a.item_id = i.item_id
                   AND a.skipped_code IS NULL) FROM items i""").fetchall())
        expected = {"0": 0, "1": 0, "2": 0, "3+": 0}
        for n in per_item.values():
            expected[str(n) if n < 3 else "3+"] += 1
        assert batch["items_by_label_count"] == expected
        assert batch["n_complete"] == expected["2"] and batch["labels_done"] == 9
        assert batch["labels_remaining"] == 20 - 9
        assert batch["pct_complete"] == expected["2"] / 10
        assert batch["pace_window"] == "24h" and batch["eta_hours"] is not None
        labelers = {l["pseudonym"]: l for l in progress["labelers"]}
        assert labelers["L02"]["n_labelled"] == 6 and labelers["L02"]["n_today"] == 6
        assert labelers["L03"]["n_labelled"] == 3

    def test_no_pace_no_eta(self, db, owner_client: TestClient):
        open_batch(3)
        batch = owner_client.get("/api/admin/progress").json()["batches"][0]
        assert batch["eta_hours"] is None and batch["items_by_label_count"]["0"] == 3


class TestFlags:
    def test_resolve(self, db, owner_client: TestClient):
        insert_item("r#q")
        flag = owner_client.post("/api/flag", json={"item_id": "r#q", "kind": "bad_item", "note": "typo"}).json()
        response = owner_client.post(f"/api/admin/flags/{flag['flag_id']}/resolve",
                                     json={"status": "dismissed", "resolution": "fine as is"})
        assert response.status_code == 200
        assert owner_client.get("/api/admin/flags").json()["count"] == 0
        done = owner_client.get("/api/admin/flags?status=dismissed").json()["flags"][0]
        assert done["resolution"] == "fine as is" and done["resolved_by"] == "L01"
        assert owner_client.post(f"/api/admin/flags/{flag['flag_id']}/resolve",
                                 json={"status": "deleted"}).status_code == 422

    def test_public_admin_sees_libre_flags_only(self, db, owner_client: TestClient):
        insert_item("r#q", "restricted")
        owner_client.post("/api/flag", json={"item_id": "r#q", "kind": "bad_item"})
        make_labeler("padm", clearance="public", role="admin")
        login(owner_client, "padm")
        assert owner_client.get("/api/admin/flags").json()["count"] == 0
        assert owner_client.post("/api/admin/flags/1/resolve", json={}).status_code == 404
