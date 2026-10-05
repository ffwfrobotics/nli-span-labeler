"""
Pytest configuration and fixtures for the E13 labeler.

Every test gets its own SQLite file (E13_DB) and runs without network access.
"""
import json
import os
from typing import Generator

import pytest
from fastapi.testclient import TestClient

# Set test environment before importing the app
os.environ["RATE_LIMIT_ENABLED"] = "0"  # Disable rate limiting in tests
os.environ["COOKIE_SECURE"] = "0"  # the test client speaks plain HTTP; the default is covered in test_auth
os.environ.pop("SINGLE_USER", None)
# No hidden gold unless a test asks for it (FR-28 is random by design)
os.environ["E13_GOLD_RATE_NEW"] = "0"
os.environ["E13_GOLD_RATE"] = "0"
os.environ["BACKUP_INTERVAL_HOURS"] = "0"  # tests that back up do it explicitly

OWNER_PASSWORD = "owner-pass-123"
LABELER_PASSWORD = "labeler-pass-123"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, migrated database for this test."""
    monkeypatch.setenv("E13_DB", str(tmp_path / "e13.db"))
    from e13_labeler.db import init_db

    init_db()
    return tmp_path / "e13.db"


@pytest.fixture
def owner(db) -> dict:
    from e13_labeler.auth import create_labeler
    from e13_labeler.db import get_db

    with get_db() as conn:
        return create_labeler(conn, "owner", OWNER_PASSWORD, role="owner", clearance="internal", status="active")


@pytest.fixture
def fresh_client(db) -> Generator[TestClient, None, None]:
    """An unauthenticated client on a clean database."""
    from e13_labeler.app import app

    with TestClient(app) as c:
        yield c


def make_labeler(login: str, clearance: str = "public", role: str = "labeler", status: str = "active",
                 agreed: bool = True) -> dict:
    """An account past onboarding by default: active, contributor agreement accepted."""
    from e13_labeler import contributor
    from e13_labeler.auth import create_labeler
    from e13_labeler.db import get_db

    with get_db() as conn:
        labeler = create_labeler(conn, login, LABELER_PASSWORD, role=role, clearance=clearance, status=status)
        if agreed:
            conn.execute("UPDATE labelers SET agreement_version = ? WHERE id = ?",
                         (contributor.VERSION, labeler["id"]))
        return labeler


def use_csrf(client: TestClient) -> TestClient:
    """Echo the CSRF cookie in X-CSRF-Token on every request, as app.js does (NFR-5)."""
    if "e13_csrf" not in client.cookies:
        client.get("/api/me")
    token = client.cookies.get("e13_csrf")
    if token:
        client.headers["X-CSRF-Token"] = token
    return client


def login(client: TestClient, login_name: str, password: str = LABELER_PASSWORD):
    response = client.post("/api/auth/login", json={"login_name": login_name, "password": password})
    assert response.status_code == 200, response.text
    use_csrf(client)
    return response.json()


@pytest.fixture
def owner_client(owner, fresh_client) -> TestClient:
    login(fresh_client, "owner", OWNER_PASSWORD)
    return fresh_client


@pytest.fixture
def public_client(db, fresh_client) -> TestClient:
    make_labeler("pub", clearance="public")
    login(fresh_client, "pub")
    return fresh_client


def insert_item(item_id: str, permissions: str = "libre", state: str = "some state",
                model_answers: dict = None) -> None:
    """Minimal item row for endpoint tests (the importer has its own tests)."""
    from e13_labeler.db import get_db
    from e13_labeler.tiers import visibility_of

    row_id, qid = item_id.split("#")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO items (item_id, row_id, qid, source, state, state_format, state_sha256,
                                  question_json, permissions, visibility, model_answers_json)
               VALUES (?, ?, ?, 'test', ?, 'text', 'sha256:x', ?, ?, ?, ?)""",
            (item_id, row_id, qid, state, json.dumps({"type": "noul"}), permissions, visibility_of(permissions),
             json.dumps(model_answers) if model_answers else None),
        )
