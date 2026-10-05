"""
M2 accounts: invites (FR-51), labeler management (FR-52), audit (FR-53), the
contributor agreement (FR-60), CSRF (NFR-5) and no hard deletes (NFR-6).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest import LABELER_PASSWORD, OWNER_PASSWORD, login, make_labeler, use_csrf


def _app_client() -> TestClient:
    from e13_labeler.app import app

    return TestClient(app)


def _audit(action: str) -> list:
    from e13_labeler.db import get_db

    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ?", (action,))]


def _register(token: str, login_name: str = "newbie") -> TestClient:
    client = _app_client()
    response = client.post("/api/auth/register",
                           json={"token": token, "login_name": login_name, "password": LABELER_PASSWORD})
    assert response.status_code == 200, response.text
    return use_csrf(client)


class TestInvites:
    def test_invite_register_flow(self, owner_client: TestClient):
        invite = owner_client.post("/api/admin/invites", json={"role": "labeler", "clearance": "internal"}).json()
        assert invite["path"].startswith("/?invite=")
        with _register(invite["token"]) as client:
            me = client.get("/api/me").json()
        assert (me["role"], me["clearance"], me["status"]) == ("labeler", "internal", "onboarding")
        assert me["needs_agreement"] is True
        # single use
        response = _app_client().post("/api/auth/register", json={
            "token": invite["token"], "login_name": "second", "password": LABELER_PASSWORD})
        assert response.status_code == 403
        listed = owner_client.get("/api/admin/invites").json()["invites"]
        assert listed[0]["state"] == "used" and listed[0]["used_by"] == me["pseudonym"]
        assert "token" not in listed[0] and "token_hash" not in listed[0]
        assert _audit("register") and _audit("invite_create")

    def test_token_stored_hashed(self, owner_client: TestClient):
        from e13_labeler.db import get_db

        token = owner_client.post("/api/admin/invites", json={}).json()["token"]
        with get_db() as conn:
            stored = [r[0] for r in conn.execute("SELECT token_hash FROM invites")]
        assert token not in stored and len(stored) == 1

    def test_expired_invite(self, owner_client: TestClient):
        from e13_labeler.db import get_db

        token = owner_client.post("/api/admin/invites", json={}).json()["token"]
        with get_db() as conn:
            conn.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00Z'")
        response = _app_client().post("/api/auth/register",
                                      json={"token": token, "login_name": "late", "password": LABELER_PASSWORD})
        assert response.status_code == 403

    def test_revoked_invite(self, owner_client: TestClient):
        token = owner_client.post("/api/admin/invites", json={}).json()["token"]
        invite_id = owner_client.get("/api/admin/invites").json()["invites"][0]["id"]
        assert owner_client.post(f"/api/admin/invites/{invite_id}/revoke").status_code == 200
        response = _app_client().post("/api/auth/register",
                                      json={"token": token, "login_name": "late", "password": LABELER_PASSWORD})
        assert response.status_code == 403

    def test_admin_limits(self, owner, fresh_client: TestClient):
        make_labeler("adm", clearance="internal", role="admin")
        login(fresh_client, "adm")
        assert fresh_client.post("/api/admin/invites", json={}).status_code == 200
        assert fresh_client.post("/api/admin/invites", json={"role": "admin"}).status_code == 403
        assert fresh_client.post("/api/admin/invites", json={"clearance": "internal"}).status_code == 403

    def test_labeler_cannot_invite(self, public_client: TestClient):
        assert public_client.post("/api/admin/invites", json={}).status_code == 403

    def test_short_password_and_taken_name(self, owner_client: TestClient):
        token = owner_client.post("/api/admin/invites", json={}).json()["token"]
        client = _app_client()
        assert client.post("/api/auth/register",
                           json={"token": token, "login_name": "abc", "password": "short"}).status_code == 422
        assert client.post("/api/auth/register",
                           json={"token": token, "login_name": "owner", "password": LABELER_PASSWORD}
                           ).status_code == 409


class TestManagement:
    def test_revoke_ends_sessions_immediately(self, owner, fresh_client: TestClient):
        """FR-52 test: revoking invalidates existing sessions immediately."""
        make_labeler("pub")
        login(fresh_client, "pub")
        assert fresh_client.get("/api/me").status_code == 200
        with _app_client() as admin:
            login(admin, "owner", OWNER_PASSWORD)
            assert admin.post("/api/admin/labelers/L02/revoke", json={}).json()["status"] == "revoked"
            assert admin.post("/api/admin/labelers/L02/resume", json={}).status_code == 409
        assert fresh_client.get("/api/me").status_code == 401
        response = _app_client().post("/api/auth/login", json={"login_name": "pub", "password": LABELER_PASSWORD})
        assert response.status_code == 403

    def test_pause_and_resume(self, owner_client: TestClient):
        make_labeler("pub")
        assert owner_client.post("/api/admin/labelers/L02/pause", json={"reason": "check"}).json()["status"] == "paused"
        # No passed quiz: resume goes back to onboarding, unless the owner skips the quiz
        assert owner_client.post("/api/admin/labelers/L02/resume", json={}).json()["status"] == "onboarding"
        response = owner_client.post("/api/admin/labelers/L02/resume", json={"skip_quiz": True})
        assert response.json()["status"] == "active"
        assert [a["target"] for a in _audit("labeler_pause")] == ["L02"]

    def test_paused_labeler_cannot_label(self, owner, fresh_client: TestClient):
        make_labeler("pub", status="paused")
        login(fresh_client, "pub")
        response = fresh_client.get("/api/next")
        assert response.status_code == 403 and response.json()["detail"]["status"] == "paused"

    def test_clearance_owner_only_and_audited(self, owner, fresh_client: TestClient):
        make_labeler("adm", clearance="internal", role="admin")
        make_labeler("pub")
        login(fresh_client, "adm")
        assert fresh_client.post("/api/admin/labelers/L03/clearance", json={"clearance": "internal"}
                                 ).status_code == 403
        with _app_client() as client:
            login(client, "owner", OWNER_PASSWORD)
            assert client.post("/api/admin/labelers/L03/clearance", json={"clearance": "internal"}
                               ).json()["clearance"] == "internal"
            assert client.post("/api/admin/labelers/L01/clearance", json={"clearance": "public"}
                               ).status_code == 403  # not on yourself
        entry = _audit("clearance_change")[0]
        assert entry["target"] == "L03" and entry["actor_id"] == owner["id"]

    def test_admin_cannot_manage_admin(self, owner, fresh_client: TestClient):
        make_labeler("adm", clearance="internal", role="admin")
        make_labeler("adm2", clearance="internal", role="admin")
        login(fresh_client, "adm")
        assert fresh_client.post("/api/admin/labelers/L03/pause", json={}).status_code == 403
        assert fresh_client.post("/api/admin/labelers/L01/pause", json={}).status_code == 403

    def test_password_reset(self, owner_client: TestClient):
        make_labeler("pub")
        with _app_client() as old:
            login(old, "pub")
            link = owner_client.post("/api/admin/labelers/L02/reset", json={}).json()
            assert link["path"].startswith("/?reset=")
            new = "a-brand-new-password"
            assert _app_client().post("/api/auth/reset", json={"token": link["token"], "password": new}
                                      ).status_code == 200
            assert old.get("/api/me").status_code == 401  # sessions end with the reset
        assert _app_client().post("/api/auth/reset", json={"token": link["token"], "password": new}
                                  ).status_code == 403  # single use
        login(_app_client(), "pub", new)

    def test_stats(self, owner_client: TestClient):
        make_labeler("pub")
        stats = owner_client.get("/api/admin/labelers/L02/stats").json()
        assert stats["n_labelled"] == 0 and stats["gold"]["n_probes"] == 0
        listed = owner_client.get("/api/admin/labelers").json()["labelers"]
        assert "median_active_ms" in listed[1] and "gold_accuracy" in listed[1]


class TestAgreement:
    def test_gate(self, owner, fresh_client: TestClient):
        """FR-60 test: the app can't be used before acceptance."""
        from e13_labeler import contributor

        make_labeler("pub", agreed=False)
        login(fresh_client, "pub")
        assert fresh_client.get("/api/me").json()["needs_agreement"] is True
        response = fresh_client.get("/api/next")
        assert response.status_code == 403 and response.json()["detail"]["agreement"] is True
        text = fresh_client.get("/api/agreement").json()
        assert text["version"] == contributor.VERSION and "pseudonym" in text["text"]
        assert fresh_client.post("/api/agreement", json={"version": "0"}).status_code == 409
        assert fresh_client.post("/api/agreement", json={"version": contributor.VERSION}).status_code == 200
        assert fresh_client.get("/api/me").json()["needs_agreement"] is False
        assert fresh_client.get("/api/next").status_code == 404  # past the gate: just nothing to label
        assert _audit("agreement_accept")[0]["detail_json"] == f'{{"version": "{contributor.VERSION}"}}'

    def test_owner_exempt(self, owner_client: TestClient):
        assert owner_client.get("/api/me").json()["needs_agreement"] is False


class TestCSRF:
    def test_missing_or_wrong_token_refused(self, owner, fresh_client: TestClient):
        login(fresh_client, "owner", OWNER_PASSWORD)
        token = fresh_client.headers.pop("X-CSRF-Token")
        assert fresh_client.post("/api/admin/invites", json={}).status_code == 403
        assert fresh_client.post("/api/admin/invites", json={}, headers={"X-CSRF-Token": "x" * 64}
                                 ).status_code == 403
        assert fresh_client.post("/api/admin/invites", json={}, headers={"X-CSRF-Token": token}
                                 ).status_code == 200
        assert fresh_client.get("/api/admin/invites").status_code == 200  # reads need no token

    def test_token_bound_to_session(self, owner, fresh_client: TestClient):
        """Another session's token doesn't work (no cookie tossing)."""
        make_labeler("pub")
        with _app_client() as other:
            login(other, "pub")
            foreign = other.cookies.get("e13_csrf")
        login(fresh_client, "owner", OWNER_PASSWORD)
        assert foreign != fresh_client.cookies.get("e13_csrf")
        assert fresh_client.post("/api/admin/invites", json={}, headers={"X-CSRF-Token": foreign}
                                 ).status_code == 403

    def test_single_user_needs_token(self, owner, monkeypatch):
        monkeypatch.setenv("SINGLE_USER", "1")
        from e13_labeler.app import app

        with TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1") as client:
            assert client.post("/api/admin/invites", json={}).status_code == 403
            use_csrf(client)
            assert client.post("/api/admin/batches/none/status", json={"status": "open"}).status_code == 422

    def test_cookie_readable_but_strict(self, owner, fresh_client: TestClient):
        response = fresh_client.post("/api/auth/login", json={"login_name": "owner", "password": OWNER_PASSWORD})
        csrf = [c for c in response.headers.get_list("set-cookie") if c.startswith("e13_csrf=")][0].lower()
        assert "samesite=strict" in csrf and "httponly" not in csrf


class TestNoHardDeletes:
    @pytest.mark.parametrize("table", ["annotations", "spans", "gold", "items", "labelers", "audit_log",
                                       "adjudications", "flags"])
    def test_delete_refused(self, owner, table):
        from e13_labeler.db import audit, get_db
        from tests.conftest import insert_item

        insert_item("r#q")
        with get_db() as conn:
            conn.execute("INSERT INTO annotations (item_id, labeler_id, answerable) VALUES ('r#q', 1, 1)")
            conn.execute("INSERT INTO spans (annotation_id, side, text, role) VALUES (1, 'state', 's', 'support')")
            conn.execute("INSERT INTO gold (item_id, reasons_json) VALUES ('r#q', '{}')")
            conn.execute("INSERT INTO adjudications (item_id, reasons_json) VALUES ('r#q', '{}')")
            conn.execute("INSERT INTO flags (item_id, labeler_id, kind) VALUES ('r#q', 1, 'other')")
            audit(conn, 1, "x")
        with pytest.raises(sqlite3.IntegrityError, match="no hard deletes"):
            with get_db() as conn:
                conn.execute(f"DELETE FROM {table}")

    def test_audit_log_append_only(self, owner):
        from e13_labeler.db import audit, get_db

        with get_db() as conn:
            audit(conn, owner["id"], "x")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with get_db() as conn:
                conn.execute("UPDATE audit_log SET action = 'y'")
