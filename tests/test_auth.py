"""
Tests for authentication: login, logout, sessions, roles and SINGLE_USER mode.
"""
from fastapi.testclient import TestClient

from tests.conftest import LABELER_PASSWORD, OWNER_PASSWORD, login, make_labeler


class TestLogin:
    def test_login_success(self, owner, fresh_client: TestClient):
        data = login(fresh_client, "owner", OWNER_PASSWORD)
        assert data["status"] == "logged_in"
        assert data["labeler"]["pseudonym"] == owner["pseudonym"]
        assert data["labeler"]["role"] == "owner"

    def test_cookie_secure_by_default(self, owner, fresh_client: TestClient, monkeypatch):
        """NFR-5: Secure unless SINGLE_USER, or explicitly COOKIE_SECURE=0."""
        from e13_labeler import config

        monkeypatch.delenv("COOKIE_SECURE")
        assert config.cookie_secure() is True
        response = fresh_client.post("/api/auth/login", json={"login_name": "owner", "password": OWNER_PASSWORD})
        assert "secure" in response.headers["set-cookie"].lower()
        monkeypatch.setenv("SINGLE_USER", "1")
        assert config.cookie_secure() is False

    def test_login_cookie_flags(self, owner, fresh_client: TestClient):
        """NFR-5: HttpOnly and SameSite=Strict."""
        response = fresh_client.post("/api/auth/login", json={"login_name": "owner", "password": OWNER_PASSWORD})
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie

    def test_login_wrong_password(self, owner, fresh_client: TestClient):
        response = fresh_client.post("/api/auth/login", json={"login_name": "owner", "password": "nope"})
        assert response.status_code == 401

    def test_login_nonexistent_user(self, fresh_client: TestClient):
        response = fresh_client.post("/api/auth/login", json={"login_name": "ghost", "password": "x"})
        assert response.status_code == 401

    def test_register_without_token_is_403(self, fresh_client: TestClient):
        """FR-51: self-registration is off; registering needs an invite."""
        response = fresh_client.post("/api/auth/register", json={"login_name": "xyz", "password": "y" * 12})
        assert response.status_code == 403
        response = fresh_client.post("/api/auth/register",
                                     json={"token": "made-up", "login_name": "xyz", "password": "y" * 12})
        assert response.status_code == 403
        assert fresh_client.get("/api/auth/status").json()["registration_enabled"] is False


class TestSessions:
    def test_me_authenticated(self, owner_client: TestClient):
        me = owner_client.get("/api/me").json()
        assert me["role"] == "owner"
        assert me["clearance"] == "internal"
        assert me["pseudonym"] == "L01"

    def test_me_unauthenticated(self, fresh_client: TestClient):
        assert fresh_client.get("/api/me").status_code == 401

    def test_logout_clears_session(self, owner_client: TestClient):
        assert owner_client.post("/api/auth/logout").json()["status"] == "logged_out"
        assert owner_client.get("/api/me").status_code == 401

    def test_sessions_stored_hashed(self, owner_client: TestClient):
        """NFR-5: the cookie value never appears in the database."""
        from e13_labeler.config import SESSION_COOKIE
        from e13_labeler.db import get_db

        token = owner_client.cookies.get(SESSION_COOKIE)
        with get_db() as conn:
            hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
        assert token and token not in hashes and len(hashes) == 1

    def test_revoke_ends_sessions(self, db, fresh_client: TestClient):
        """FR-52: revoking invalidates existing sessions immediately."""
        from e13_labeler.auth import revoke_sessions
        from e13_labeler.db import get_db

        labeler = make_labeler("pub")
        login(fresh_client, "pub")
        with get_db() as conn:
            conn.execute("UPDATE labelers SET status = 'revoked' WHERE id = ?", (labeler["id"],))
            revoke_sessions(conn, labeler["id"])
        assert fresh_client.get("/api/me").status_code == 401
        response = fresh_client.post("/api/auth/login", json={"login_name": "pub", "password": LABELER_PASSWORD})
        assert response.status_code == 403


class TestPasswords:
    def test_argon2id(self, owner):
        from e13_labeler.db import get_db

        with get_db() as conn:
            stored = conn.execute("SELECT password_hash FROM labelers WHERE id = ?", (owner["id"],)).fetchone()[0]
        assert stored.startswith("$argon2id$")

    def test_pseudonyms_are_sequential(self, owner):
        assert make_labeler("a")["pseudonym"] == "L02"
        assert make_labeler("b")["pseudonym"] == "L03"


class TestRoles:
    def test_admin_endpoint_requires_admin(self, public_client: TestClient):
        assert public_client.get("/api/admin/labelers").status_code == 403

    def test_owner_lists_labelers(self, owner_client: TestClient):
        make_labeler("pub")
        labelers = owner_client.get("/api/admin/labelers").json()["labelers"]
        assert [l["pseudonym"] for l in labelers] == ["L01", "L02"]
        assert all("password_hash" not in l for l in labelers)


class TestSingleUser:
    def test_auto_login_on_loopback(self, owner, monkeypatch):
        monkeypatch.setenv("SINGLE_USER", "1")
        from e13_labeler.app import app

        with TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1") as client:
            me = client.get("/api/me").json()
        assert me["role"] == "owner" and me["single_user"] is True

    def test_dns_rebinding_host_refused(self, owner, monkeypatch):
        """A page on evil.example resolving to 127.0.0.1 must not reach the auto-logged-in owner."""
        monkeypatch.setenv("SINGLE_USER", "1")
        from e13_labeler.app import app

        with TestClient(app, client=("127.0.0.1", 50000), base_url="http://evil.example") as client:
            assert client.get("/api/me").status_code == 400
        with TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:8000") as client:
            assert client.get("/api/me").status_code == 200

    def test_allowed_hosts_config(self, owner, monkeypatch, fresh_client):
        monkeypatch.setenv("ALLOWED_HOSTS", "labels.example.org")
        assert fresh_client.get("/api/auth/status").status_code == 400
        from e13_labeler.app import app

        with TestClient(app, base_url="https://labels.example.org") as client:
            assert client.get("/api/auth/status").status_code == 200

    def test_non_loopback_refused(self, owner, monkeypatch):
        """FR-54: requests from a non-loopback address are refused."""
        monkeypatch.setenv("SINGLE_USER", "1")
        from e13_labeler.app import app

        with TestClient(app, client=("192.168.1.50", 50000)) as client:
            assert client.get("/api/me").status_code == 403
            assert client.get("/").status_code == 403

    def test_serve_disables_uvicorn_proxy_headers(self, monkeypatch):
        """Proxy trust is ours alone; uvicorn would otherwise trust X-Forwarded-For from 127.0.0.1."""
        import uvicorn

        from e13_labeler.__main__ import main

        seen = {}
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
        main(["serve"])
        assert seen["proxy_headers"] is False and seen["forwarded_allow_ips"] == ""

    def test_forwarded_header_not_trusted(self, owner, monkeypatch):
        """A spoofed X-Forwarded-For can't make a remote client look local (or dodge rate limits)."""
        monkeypatch.setenv("SINGLE_USER", "1")
        from e13_labeler.app import app

        with TestClient(app, client=("192.168.1.50", 50000)) as client:
            response = client.get("/api/me", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 403
