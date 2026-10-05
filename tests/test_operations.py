"""
M2 operations: online backups (NFR-6) and importing from the admin page (FR-11).
"""
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import login, make_labeler

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_pool.jsonl"


class TestBackup:
    def test_backup_is_a_consistent_copy(self, owner, tmp_path, monkeypatch):
        from e13_labeler.backup import backup, latest_backup

        monkeypatch.setenv("E13_BACKUP_DIR", str(tmp_path / "bk"))
        result = backup()
        copy = sqlite3.connect(result["path"])
        assert copy.execute("SELECT pseudonym FROM labelers").fetchall() == [("L01",)]
        assert copy.execute("PRAGMA user_version").fetchone()[0] > 0
        copy.close()
        second = backup()
        assert second["path"] != result["path"] and Path(result["path"]).exists()  # nothing is replaced
        assert latest_backup() == Path(second["path"])

    def test_due(self, owner, tmp_path, monkeypatch):
        from e13_labeler.backup import backup, due

        monkeypatch.setenv("E13_BACKUP_DIR", str(tmp_path / "bk"))
        monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "24")
        assert due()  # no backup yet
        path = Path(backup()["path"])
        assert not due()
        old = time.time() - 25 * 3600
        os.utime(path, (old, old))
        assert due()
        monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
        assert not due()

    def test_cli_and_api(self, owner, owner_client: TestClient, tmp_path, monkeypatch):
        from e13_labeler.__main__ import main
        from e13_labeler.db import get_db

        monkeypatch.setenv("E13_BACKUP_DIR", str(tmp_path / "bk"))
        assert main(["backup"]) == 0
        response = owner_client.post("/api/admin/backup")
        assert response.status_code == 200 and response.json()["sha256"]
        assert len(list((tmp_path / "bk").glob("e13-*.db"))) == 2
        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'backup'").fetchone()[0] == 2

    def test_api_owner_only(self, owner, fresh_client: TestClient):
        make_labeler("adm", clearance="internal", role="admin")
        login(fresh_client, "adm")
        assert fresh_client.post("/api/admin/backup").status_code == 403

    def test_server_backs_up_at_start(self, owner, tmp_path, monkeypatch):
        from e13_labeler.app import app

        monkeypatch.setenv("E13_BACKUP_DIR", str(tmp_path / "bk"))
        monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "24")
        with TestClient(app):
            for _ in range(50):
                if list((tmp_path / "bk").glob("e13-*.db")):
                    break
                time.sleep(0.05)
        assert len(list((tmp_path / "bk").glob("e13-*.db"))) == 1


class TestUpload:
    def test_import_from_admin_page(self, owner_client: TestClient):
        from e13_labeler.db import get_db

        content = FIXTURE.read_text(encoding="utf-8")
        response = owner_client.post("/api/admin/import", json={
            "filename": "synthetic_pool.jsonl", "content": content, "batch": "uploaded"})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["n_items"] > 0 and report["n_rejected"] == 0
        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0] == report["n_items"]
            row = conn.execute("SELECT detail_json, target FROM audit_log WHERE action = 'import'").fetchone()
        assert row["target"] == "upload:synthetic_pool.jsonl" and json.loads(row["detail_json"])["batch"] == "uploaded"

    def test_rejects_reported(self, owner_client: TestClient):
        response = owner_client.post("/api/admin/import", json={"filename": "bad.jsonl", "content": "{not json}\n"})
        assert response.json()["n_rejected"] == 1 and response.json()["errors"][0]["line"] == 1

    def test_labelers_cannot_import(self, public_client: TestClient):
        assert public_client.post("/api/admin/import", json={"filename": "x", "content": ""}).status_code == 403
