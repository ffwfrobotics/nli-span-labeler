"""
Labelling designs for few labelers (owner, 2026-10-05: "3 is ideal, but we might
have to make 1 work"): overlap 1..n, a reliability subset, and re-label batches.
"""
import json

import pytest
from fastapi.testclient import TestClient

from e13_labeler.agreement import alpha_nominal, by_item, by_item_and_labeler, reason_units
from tests.conftest import login, make_labeler


def rows(n, prefix="snli/r"):
    return [json.dumps({"id": f"{prefix}/{i}", "source": "snli", "state": f"state {i}",
                        "questions": {"q": {"type": "noul"}}}) for i in range(n)]


@pytest.fixture
def batch100(db):
    from e13_labeler.db import get_db
    from e13_labeler.importer import import_rows

    with get_db() as conn:
        import_rows(conn, rows(100), "generated", "x", batch="b")


def label_all(client, body=None):
    """Label everything /api/next hands this client; returns the item ids in order."""
    seen = []
    while (r := client.get("/api/next")).status_code == 200:
        item_id = r.json()["item_id"]
        seen.append(item_id)
        assert client.post("/api/annotations", json={"item_id": item_id, **(body or {"answerable": True})}
                           ).status_code == 200
    return seen


def label_counts(batch_name="b"):
    from e13_labeler.db import get_db

    with get_db() as conn:
        return {r[0]: r[1] for r in conn.execute(
            """SELECT bi.item_id, (SELECT COUNT(*) FROM annotations a WHERE a.item_id = bi.item_id
                                   AND a.batch_id = bi.batch_id AND a.skipped_code IS NULL)
               FROM batch_items bi JOIN batches b ON b.id = bi.batch_id WHERE b.name = ?""", (batch_name,))}


class TestReliabilitySubset:
    def test_sampling_is_deterministic(self):
        from e13_labeler.batches import in_reliability_subset

        picks = [in_reliability_subset("b", f"x/{i}#q", 0.2) for i in range(2000)]
        assert picks == [in_reliability_subset("b", f"x/{i}#q", 0.2) for i in range(2000)]
        assert 0.17 < sum(picks) / 2000 < 0.23
        assert not any(in_reliability_subset("b", f"x/{i}#q", 0.0) for i in range(100))

    def test_overlap_one_with_subset(self, batch100, fresh_client: TestClient):
        """Overlap 1, 20% at 3 labelers: the subset items end with 3 labels, the rest with 1."""
        from e13_labeler.batches import configure, set_status
        from e13_labeler.db import get_db

        with get_db() as conn:
            result = configure(conn, "b", overlap_target=1, reliability_fraction=0.2, reliability_overlap=3)
            warnings = set_status(conn, "b", "open")
            subset = {r[0] for r in conn.execute("SELECT item_id FROM batch_items WHERE target = 3")}
        assert result["reliability_subset"] == len(subset) and 10 <= len(subset) <= 30
        assert "reliability subset" in warnings[0]

        from e13_labeler.app import app

        clients = []
        for n in ("a", "b2", "c"):
            make_labeler(n)
            clients.append(TestClient(app))
            login(clients[-1], n)
        while any([label_all(c) for c in clients]):  # until nobody gets anything
            pass
        counts = label_counts()
        assert {i for i, n in counts.items() if n == 3} == subset
        assert all(n == 1 for i, n in counts.items() if i not in subset)

    def test_items_imported_later_join_the_subset(self, batch100):
        from e13_labeler.batches import configure, in_reliability_subset
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_rows

        with get_db() as conn:
            configure(conn, "b", overlap_target=1, reliability_fraction=0.5)
            import_rows(conn, rows(40, "snli/late"), "late", "y", batch="b")
            late = conn.execute(
                "SELECT item_id, target FROM batch_items WHERE item_id LIKE 'snli/late/%'").fetchall()
        assert all((t is not None) == in_reliability_subset("b", i, 0.5) for i, t in late)

    def test_overlap_one_without_subset_warns(self, batch100):
        from e13_labeler.batches import configure, set_status
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "b", overlap_target=1)
            assert "re-label" in set_status(conn, "b", "open")[0]

    @pytest.mark.parametrize("kwargs, message", [
        ({"overlap_target": 0}, "at least 1"),
        ({"reliability_fraction": 1.5}, "between 0 and 1"),
        ({"reliability_overlap": 1}, "at least 2"),
        ({"tier_ceiling": "public"}, "tier_ceiling"),
    ])
    def test_bad_config(self, batch100, kwargs, message):
        from e13_labeler.batches import configure
        from e13_labeler.db import get_db

        with get_db() as conn, pytest.raises(ValueError, match=message):
            configure(conn, "b", **kwargs)


class TestRelabel:
    @pytest.fixture
    def first_pass(self, batch100, owner_client: TestClient):
        """The owner alone labels a 1-overlap batch, then a re-label batch is created."""
        from e13_labeler.batches import configure, create_relabel, set_status
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "b", overlap_target=1)
            set_status(conn, "b", "open")
        labelled = label_all(owner_client, {"reasons": ["not_enough_info"]})
        with get_db() as conn:
            info = create_relabel(conn, "b", "b-r2", fraction=0.3, after_days=7)
            set_status(conn, "b-r2", "open")
        return labelled, info

    def backdate(self, days):
        from e13_labeler.db import get_db

        with get_db() as conn:
            conn.execute(f"UPDATE annotations SET created_at = datetime(created_at, '-{days} days')")

    def test_not_served_before_the_gap(self, first_pass, owner_client: TestClient):
        labelled, info = first_pass
        assert len(labelled) == 100 and info["relabel_of"] == "b" and 15 <= info["n_items"] <= 45
        assert owner_client.get("/api/next").status_code == 404
        self.backdate(6)
        assert owner_client.get("/api/next").status_code == 404

    def test_served_once_after_the_gap_and_blind(self, first_pass, owner_client: TestClient):
        from e13_labeler.db import get_db

        _, info = first_pass
        self.backdate(8)
        r = owner_client.get("/api/next")
        assert r.status_code == 200
        payload = r.json()
        assert payload["progress"]["batch"] == "b-r2"
        # Blind: nothing of the first pass comes back (span_policy and reason_set name every reason anyway)
        assert set(payload) == {"item_id", "lock_until", "state", "state_format", "question", "reason_set",
                                "task_type", "span_policy", "require_note", "asof", "progress"}
        second = [payload["item_id"]]
        assert owner_client.post("/api/annotations",
                                 json={"item_id": second[0], "answerable": True}).status_code == 200
        second += label_all(owner_client)
        assert len(second) == len(set(second)) == info["n_items"]
        with get_db() as conn:
            rows_ = conn.execute(
                """SELECT b.name, COUNT(*) FROM annotations a JOIN batches b ON b.id = a.batch_id
                   GROUP BY b.name""").fetchall()
        assert dict(rows_) == {"b": 100, "b-r2": info["n_items"]}

    def test_other_labelers_get_nothing_from_a_relabel_batch(self, first_pass, fresh_client: TestClient):
        self.backdate(8)
        make_labeler("other", clearance="internal")
        login(fresh_client, "other")
        assert fresh_client.get("/api/next").status_code == 404

    def test_relabel_of_relabel_refused(self, first_pass):
        from e13_labeler.batches import create_relabel
        from e13_labeler.db import get_db

        with get_db() as conn, pytest.raises(ValueError, match="re-label a re-label"):
            create_relabel(conn, "b-r2", "b-r3")

    def test_relabel_defaults_to_the_reliability_subset(self, batch100):
        from e13_labeler.batches import configure, create_relabel
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "b", overlap_target=1, reliability_fraction=0.25)
            subset = {r[0] for r in conn.execute("SELECT item_id FROM batch_items WHERE target IS NOT NULL")}
            create_relabel(conn, "b", "b-r2")
            got = {r[0] for r in conn.execute(
                "SELECT item_id FROM batch_items bi JOIN batches b ON b.id = bi.batch_id WHERE b.name = 'b-r2'")}
        assert got == subset

    def test_admin_api(self, batch100, owner_client: TestClient):
        r = owner_client.post("/api/admin/batches/b/config", json={"overlap_target": 1, "reliability_fraction": 0.1})
        assert r.status_code == 200 and r.json()["overlap_target"] == 1
        assert owner_client.post("/api/admin/batches/b/config", json={"overlap_target": 0}).status_code == 422
        r = owner_client.post("/api/admin/batches/b/relabel", json={"name": "b-r2", "after_days": 14})
        assert r.status_code == 200 and r.json()["relabel_after_days"] == 14
        r = owner_client.post("/api/admin/batches/b/status", json={"status": "open"})
        assert r.json()["warnings"]
        names = [b["name"] for b in owner_client.get("/api/admin/batches").json()["batches"]]
        assert names == ["b", "b-r2"]


class TestIntraRaterAlpha:
    def test_units_by_item_and_labeler(self):
        """Re-label passes pair by (item, labeler); mixing them into item units would blend inter and intra."""
        anns = [
            {"item_id": "x", "labeler": "L01", "reasons": {"stale_state": True}},
            {"item_id": "x", "labeler": "L01", "reasons": {"stale_state": True}},
            {"item_id": "x", "labeler": "L02", "reasons": {"stale_state": False}},
            {"item_id": "x", "labeler": "L02", "reasons": {"stale_state": False}},
            {"item_id": "y", "labeler": "L01", "reasons": {"stale_state": False}},
            {"item_id": "y", "labeler": "L01", "reasons": {"stale_state": False}},
        ]
        intra = reason_units(anns, "stale_state", by_item_and_labeler)
        assert intra == [[True, True], [False, False], [False, False]]
        assert alpha_nominal(intra) == pytest.approx(1.0)
        assert reason_units(anns, "stale_state", by_item)[0] == [True, True, False, False]

    def test_single_labels_drop_out_of_alpha(self):
        units = [[True], [False], [True, True], [False, False]]
        assert alpha_nominal(units) == alpha_nominal([[True, True], [False, False]])


class TestMigrationV5:
    def test_data_survives_the_rebuild(self, tmp_path, monkeypatch):
        """v5 rebuilds batches and annotations; rows, ids and spans must survive."""
        import sqlite3

        from e13_labeler import db

        path = tmp_path / "v4.db"
        monkeypatch.setenv("E13_DB", str(path))
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        for number, script in enumerate(db.MIGRATIONS[:4], start=1):
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        conn.executescript("""
            INSERT INTO labelers (pseudonym, kind, role) VALUES ('L01', 'human', 'owner');
            INSERT INTO items (item_id, row_id, qid, source, state, state_format, state_sha256, question_json,
                               permissions, visibility) VALUES ('r#q', 'r', 'q', 's', 'x', 'text', 'h', '{}',
                               'libre', 'libre');
            INSERT INTO batches (name, reason_set_json, overlap_target, require_note) VALUES ('old', '[]', 2, 1);
            INSERT INTO batch_items (batch_id, item_id) VALUES (1, 'r#q');
            INSERT INTO annotations (item_id, batch_id, labeler_id, answerable, reasons_json, note)
                VALUES ('r#q', 1, 1, 0, '{"unrelated": true}', 'kept');
            INSERT INTO spans (annotation_id, side, text, role) VALUES (1, 'state', 'x', 'support');
        """)
        conn.commit()
        conn.close()
        db.init_db()
        with db.get_db() as c:
            batch = c.execute("SELECT * FROM batches").fetchone()
            ann = c.execute("SELECT * FROM annotations").fetchone()
            span = c.execute("SELECT annotation_id FROM spans").fetchone()
            assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert (batch["name"], batch["overlap_target"], batch["require_note"], batch["reliability_fraction"]) == \
            ("old", 2, 1, 0)
        assert (ann["id"], ann["note"], span[0]) == (1, "kept", 1)
