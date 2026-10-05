"""
Online backups (requirements NFR-6).

A consistent copy of the live database (SQLite's online backup API, the
``.backup`` equivalent), written to ``outputs/e13_labeler/backups/`` and
checked with ``PRAGMA integrity_check``. Backups are never deleted (owner
policy: keep all artifacts).

The server makes one at start-up when the newest backup is older than
``BACKUP_INTERVAL_HOURS`` (default 24), then checks hourly, so a server left
running backs up nightly. ``BACKUP_INTERVAL_HOURS=0`` turns this off;
``python -m e13_labeler backup`` makes one at any time.
"""

import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("e13.backup")


def backup_dir() -> Path:
    return Path(os.environ.get("E13_BACKUP_DIR", config.OUTPUTS_DIR / "backups"))


def interval_hours() -> float:
    return float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))


def backup(dest_dir: Optional[Path] = None) -> dict:
    """Copy the live database; returns the path, size and sha256 of the copy."""
    dest_dir = Path(dest_dir or backup_dir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = dest_dir / f"e13-{stamp}.db"
    src = sqlite3.connect(config.db_path(), timeout=30)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        check = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if check != "ok":
        raise RuntimeError(f"backup {dest} failed its integrity check: {check}")
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    return {"path": str(dest), "bytes": dest.stat().st_size, "sha256": digest}


def latest_backup(dest_dir: Optional[Path] = None) -> Optional[Path]:
    files = sorted(Path(dest_dir or backup_dir()).glob("e13-*.db"))
    return files[-1] if files else None


def due(now: Optional[float] = None) -> bool:
    hours = interval_hours()
    if hours <= 0 or not config.db_path().exists():
        return False
    last = latest_backup()
    if last is None:
        return True
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    return now - last.stat().st_mtime >= hours * 3600


async def backup_loop(check_every: float = 3600) -> None:
    """Background task for the server's lifespan."""
    while True:
        try:
            if due():
                result = await asyncio.to_thread(backup)
                log.warning("backup written: %s (%d bytes)", result["path"], result["bytes"])
        except Exception:  # never take the server down over a backup; log and retry next time
            log.exception("backup failed")
        await asyncio.sleep(check_every)
