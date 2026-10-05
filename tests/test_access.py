"""
Tests for tiers (requirements §8), item visibility, locks and flags.
"""
import pytest
from fastapi.testclient import TestClient

from e13_labeler import tiers
from tests.conftest import insert_item, login, make_labeler


class TestTiers:
    @pytest.mark.parametrize(
        "parts, expected",
        [
            (("libre",), "libre"),
            (("libre", "restricted"), "restricted"),
            (("libre", "jev"), "jev"),
            (("restricted", "jev"), "jev+restricted"),
            (("jev+restricted", "libre"), "jev+restricted"),
        ],
    )
    def test_max_tier(self, parts, expected):
        """FR-56: an item's tier is the maximum of its parts."""
        assert tiers.max_tier(*parts) == expected

    def test_unknown_source_is_restricted(self):
        """FR-6: unknown sources map to restricted."""
        assert tiers.source_tier("no_such_source_xyz")[0] == "restricted"

    def test_source_classes(self):
        perms = {"a": {"class": "libre", "licence": "CC-BY"}, "b": {"class": "unverified"}, "c": {"class": "restricted"}}
        assert tiers.source_tier("a", perms) == ("libre", "CC-BY")
        assert tiers.source_tier("b", perms)[0] == "restricted"
        assert tiers.source_tier("c", perms)[0] == "restricted"

    def test_source_permissions_file_loads(self):
        perms = tiers.load_source_permissions()
        assert len(perms) > 50
        assert {e["class"] for e in perms.values()} <= {"libre", "restricted", "unverified"}

    def test_eval_only(self):
        """FR-55 seed list."""
        assert tiers.is_eval_only("llm_aggrefact")
        assert tiers.is_eval_only("HaluBench")
        assert not tiers.is_eval_only("snli")

    def test_ceiling(self):
        assert tiers.tiers_within("restricted") == ("libre", "restricted")
        assert tiers.tiers_within("jev+restricted") == tiers.TIERS
        assert tiers.tiers_within("libre") == ("libre",)


class TestVisibility:
    def test_public_gets_404_for_restricted(self, public_client: TestClient):
        """FR-50: a public labeler gets 404 for any item above libre, even by direct id."""
        insert_item("src/1#q0", "restricted")
        insert_item("src/2#q0", "libre")
        assert public_client.get("/api/lock/status/src/1#q0").status_code == 404
        assert public_client.get("/api/lock/status/src/2%23q0").status_code == 200

    @pytest.mark.parametrize("tier", ["restricted", "jev", "jev+restricted"])
    def test_public_cannot_touch_hidden_tiers(self, public_client: TestClient, tier):
        insert_item("src/9#q0", tier)
        for method, url in [
            ("get", "/api/lock/status/src/9%23q0"),
            ("post", "/api/lock/release/src/9%23q0"),
            ("post", "/api/lock/extend/src/9%23q0"),
        ]:
            assert getattr(public_client, method)(url).status_code == 404, url
        response = public_client.post("/api/flag", json={"item_id": "src/9#q0", "kind": "bad_item"})
        assert response.status_code == 404

    def test_internal_sees_restricted(self, owner_client: TestClient):
        insert_item("src/1#q0", "jev+restricted")
        assert owner_client.get("/api/lock/status/src/1%23q0").status_code == 200


class TestLocks:
    def test_lock_is_exclusive_and_expires(self, db):
        from e13_labeler.app import acquire_lock
        from e13_labeler.db import get_db

        insert_item("src/1#q0")
        a, b = make_labeler("a"), make_labeler("b")
        with get_db() as conn:
            assert acquire_lock(conn, "src/1#q0", a["id"])
            assert acquire_lock(conn, "src/1#q0", b["id"]) is None
            assert acquire_lock(conn, "src/1#q0", a["id"])  # own lock extends
            conn.execute("UPDATE locks SET until = '2000-01-01T00:00:00Z'")
            assert acquire_lock(conn, "src/1#q0", b["id"])  # expired lock is taken over

    def test_release_and_status(self, db, fresh_client: TestClient):
        from e13_labeler.app import acquire_lock
        from e13_labeler.db import get_db

        insert_item("src/1#q0")
        a = make_labeler("a")
        make_labeler("b")
        with get_db() as conn:
            acquire_lock(conn, "src/1#q0", a["id"])
        login(fresh_client, "b")
        status = fresh_client.get("/api/lock/status/src/1%23q0").json()
        assert status["locked"] and status["locked_by"] == a["pseudonym"] and not status["is_own_lock"]
        assert fresh_client.post("/api/lock/release/src/1%23q0").status_code == 403


class TestFlags:
    def test_flag_created_and_visible(self, db, fresh_client: TestClient):
        """FR-44: a flag is created and visible to admins."""
        insert_item("src/1#q0")
        make_labeler("pub")
        make_labeler("adm", role="admin", clearance="internal")
        login(fresh_client, "pub")
        response = fresh_client.post("/api/flag", json={"item_id": "src/1#q0", "kind": "guideline_unclear", "note": "?"})
        assert response.status_code == 200
        assert fresh_client.post("/api/flag", json={"item_id": "src/1#q0", "kind": "guideline_unclear"}).status_code == 400
        login(fresh_client, "adm")
        flags = fresh_client.get("/api/admin/flags").json()["flags"]
        assert [(f["item_id"], f["kind"], f["pseudonym"]) for f in flags] == [("src/1#q0", "guideline_unclear", "L01")]

    def test_bad_kind(self, public_client: TestClient):
        insert_item("src/1#q0")
        assert public_client.post("/api/flag", json={"item_id": "src/1#q0", "kind": "meh"}).status_code == 422


class TestPage:
    def test_index_served(self, fresh_client: TestClient):
        page = fresh_client.get("/").text
        assert "/static/style.css" in page and "/static/app.js" in page
        assert fresh_client.get("/static/style.css").status_code == 200

    def test_no_external_assets(self, fresh_client: TestClient):
        """NFR-3: no CDN at runtime."""
        import re

        for path in ("/", "/static/app.js", "/static/label.js", "/static/style.css"):
            body = fresh_client.get(path).text
            assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", body), path
            assert "@import" not in body and "url(http" not in body, path
