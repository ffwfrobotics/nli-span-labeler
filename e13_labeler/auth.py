"""
Accounts, sessions and request identity.

Descends from the authentication helpers of the old ``app.py``, with the
upgrades from requirements NFR-5:
- passwords are hashed with argon2id (was salted SHA-256);
- session tokens are stored hashed (were stored in plaintext);
- forwarded-for headers are trusted only from TRUSTED_PROXIES (were trusted
  from anyone, which let clients dodge the login rate limit).
"""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request

from . import config
from .db import get_db

_hasher = PasswordHasher()  # argon2id with the library's current defaults

ROLES = ("owner", "admin", "labeler", "model")
CLEARANCES = ("public", "internal")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Passwords and tokens
# ============================================================================

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    """Session and invite tokens are high-entropy, so a plain SHA-256 is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


# ============================================================================
# Labelers
# ============================================================================

def next_pseudonym(conn: sqlite3.Connection) -> str:
    """Pseudonyms are L01, L02, ...: the human-facing id used everywhere (NFR-4)."""
    n = conn.execute("SELECT COUNT(*) FROM labelers WHERE kind = 'human'").fetchone()[0]
    while True:
        n += 1
        pseudonym = f"L{n:02d}"
        if not conn.execute("SELECT 1 FROM labelers WHERE pseudonym = ?", (pseudonym,)).fetchone():
            return pseudonym


def create_labeler(
    conn: sqlite3.Connection,
    login_name: str,
    password: str,
    role: str = "labeler",
    clearance: str = "public",
    status: str = "onboarding",
) -> dict:
    if role not in ROLES or role == "model":
        raise ValueError(f"invalid human role: {role}")
    if clearance not in CLEARANCES:
        raise ValueError(f"invalid clearance: {clearance}")
    pseudonym = next_pseudonym(conn)
    cur = conn.execute(
        """INSERT INTO labelers (pseudonym, kind, role, clearance, status, login_name, password_hash)
           VALUES (?, 'human', ?, ?, ?, ?, ?)""",
        (pseudonym, role, clearance, status, login_name, hash_password(password)),
    )
    return {"id": cur.lastrowid, "pseudonym": pseudonym, "role": role, "clearance": clearance}


def get_owner(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM labelers WHERE role = 'owner' AND status != 'revoked' ORDER BY id LIMIT 1"
    ).fetchone()


def labeler_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "pseudonym": row["pseudonym"],
        "login_name": row["login_name"],
        "role": row["role"],
        "clearance": row["clearance"],
        "status": row["status"],
    }


# ============================================================================
# Sessions
# ============================================================================

def create_session(labeler_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=config.SESSION_EXPIRY_DAYS)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, labeler_id, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), labeler_id, iso(expires_at)),
        )
        conn.execute("UPDATE labelers SET last_seen = ? WHERE id = ?", (iso(utcnow()), labeler_id))
    return token


def get_labeler_from_session(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute(
            """SELECT l.* FROM sessions s JOIN labelers l ON s.labeler_id = l.id
               WHERE s.token_hash = ? AND s.expires_at > ? AND l.status != 'revoked'""",
            (hash_token(token), iso(utcnow())),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE labelers SET last_seen = ? WHERE id = ?", (iso(utcnow()), row["id"]))
        return labeler_dict(row)


def delete_session(token: str):
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def revoke_sessions(conn: sqlite3.Connection, labeler_id: int):
    """End every session of a labeler immediately (FR-52)."""
    conn.execute("DELETE FROM sessions WHERE labeler_id = ?", (labeler_id,))


# ============================================================================
# Request identity
# ============================================================================

def client_ip(request: Request) -> str:
    """
    The client address, honouring X-Forwarded-For / X-Real-IP only when the
    direct peer is a configured trusted proxy.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in config.TRUSTED_PROXIES:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return peer


def is_loopback(request: Request) -> bool:
    return client_ip(request) in config.LOOPBACK_HOSTS


async def get_current_labeler(request: Request) -> dict:
    """Dependency: the labeler behind this request, or 401."""
    if config.single_user():
        # FR-54: one owner, auto-login. Non-loopback requests never get this far
        # (see the single-user middleware in app.py).
        with get_db() as conn:
            owner = get_owner(conn)
        if not owner:
            raise HTTPException(503, "SINGLE_USER=1 but no owner exists. Run: python -m e13_labeler create-owner")
        return labeler_dict(owner)

    labeler = get_labeler_from_session(request.cookies.get(config.SESSION_COOKIE))
    if not labeler:
        raise HTTPException(401, "Not authenticated. Please log in.")
    return labeler


def require_role(*roles: str):
    """Dependency factory: 403 unless the labeler has one of ``roles``."""

    async def checker(labeler: dict = Depends(get_current_labeler)) -> dict:
        if labeler["role"] not in roles:
            raise HTTPException(403, f"Requires role: {' or '.join(roles)}")
        return labeler

    return checker


require_admin = require_role("owner", "admin")
require_owner = require_role("owner")
