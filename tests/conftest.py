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
os.environ.pop("SINGLE_USER", None)

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


def make_labeler(login: str, clearance: str = "public", role: str = "labeler") -> dict:
    from e13_labeler.auth import create_labeler
    from e13_labeler.db import get_db

    with get_db() as conn:
        return create_labeler(conn, login, LABELER_PASSWORD, role=role, clearance=clearance, status="active")


def login(client: TestClient, login_name: str, password: str = LABELER_PASSWORD):
    response = client.post("/api/auth/login", json={"login_name": login_name, "password": password})
    assert response.status_code == 200, response.text
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


def insert_item(item_id: str, permissions: str = "libre", state: str = "some state") -> None:
    """Minimal item row for endpoint tests (the importer has its own tests)."""
    from e13_labeler.db import get_db

    row_id, qid = item_id.split("#")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO items (item_id, row_id, qid, source, state, state_format, state_sha256,
                                  question_json, permissions)
               VALUES (?, ?, ?, 'test', ?, 'text', 'sha256:x', ?, ?)""",
            (item_id, row_id, qid, state, json.dumps({"type": "noul"}), permissions),
        )
