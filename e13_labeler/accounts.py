"""
Invites, password resets and labeler management (requirements FR-51, FR-52,
FR-53, FR-60).

Who may do what:
- the owner manages everyone but themselves; an admin manages labelers only;
- only the owner invites admins, invites internal clearance, or changes a
  clearance (FR-52);
- invites and reset links are single use, expire after 7 days and are stored
  hashed (NFR-5).

Every change here writes an audit row.
"""

import secrets
import sqlite3
from datetime import timedelta
from statistics import median
from typing import Optional

from . import contributor
from .auth import CLEARANCES, create_labeler, hash_password, hash_token, iso, revoke_sessions, utcnow
from .db import audit

INVITE_DAYS = 7
INVITE_ROLES = ("labeler", "admin")
MIN_PASSWORD = 10


class AccountError(ValueError):
    """``status`` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _labeler(conn: sqlite3.Connection, pseudonym: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM labelers WHERE pseudonym = ? AND kind = 'human'", (pseudonym,)).fetchone()
    if row is None:
        raise AccountError(f"no labeler {pseudonym}", 404)
    return row


def check_can_manage(actor: dict, target: sqlite3.Row) -> None:
    if target["id"] == actor["id"]:
        raise AccountError("you can't manage your own account here", 403)
    if target["role"] == "owner":
        raise AccountError("the owner account can't be managed", 403)
    if actor["role"] == "admin" and target["role"] != "labeler":
        raise AccountError("admins manage labelers only", 403)
    if actor["role"] not in ("owner", "admin"):
        raise AccountError("requires role owner or admin", 403)


# ============================================================================
# Invites and resets
# ============================================================================

def _new_token(conn, *, purpose: str, role: Optional[str], clearance: Optional[str], created_by: int,
               labeler_id: Optional[int] = None) -> dict:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = iso(now + timedelta(days=INVITE_DAYS))
    conn.execute(
        """INSERT INTO invites (token_hash, purpose, role, clearance, labeler_id, expires_at, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (hash_token(token), purpose, role or "", clearance or "", labeler_id, expires, created_by, iso(now)))
    return {"token": token, "expires_at": expires}


def create_invite(conn: sqlite3.Connection, actor: dict, role: str = "labeler", clearance: str = "public") -> dict:
    """FR-51: a single-use link bound to a role and clearance. The token is shown once."""
    if role not in INVITE_ROLES:
        raise AccountError(f"role must be one of {', '.join(INVITE_ROLES)}")
    if clearance not in CLEARANCES:
        raise AccountError(f"clearance must be one of {', '.join(CLEARANCES)}")
    if actor["role"] != "owner" and (role != "labeler" or clearance != "public"):
        raise AccountError("only the owner invites admins or internal clearance", 403)
    out = _new_token(conn, purpose="invite", role=role, clearance=clearance, created_by=actor["id"])
    audit(conn, actor["id"], "invite_create", None, {"role": role, "clearance": clearance,
                                                     "expires_at": out["expires_at"]})
    return {**out, "role": role, "clearance": clearance, "path": f"/?invite={out['token']}"}


def list_invites(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT i.purpose, i.role, i.clearance, i.expires_at, i.created_at, i.used_at,
                  c.pseudonym AS created_by, u.pseudonym AS used_by, t.pseudonym AS for_labeler,
                  substr(i.token_hash, 1, 12) AS id
           FROM invites i LEFT JOIN labelers c ON c.id = i.created_by LEFT JOIN labelers u ON u.id = i.used_by
           LEFT JOIN labelers t ON t.id = i.labeler_id ORDER BY i.created_at DESC""").fetchall()
    now = iso(utcnow())
    return [{**dict(r), "state": "used" if r["used_by"] or r["used_at"] else
             ("expired" if r["expires_at"] <= now else "open")} for r in rows]


def revoke_invite(conn: sqlite3.Connection, actor: dict, invite_id: str) -> None:
    """Expire an open invite now. ``invite_id`` is the hash prefix from list_invites."""
    if len(invite_id) < 12:
        raise AccountError("invite id too short")
    cur = conn.execute("UPDATE invites SET expires_at = ? WHERE token_hash LIKE ? AND used_at IS NULL",
                       (iso(utcnow()), invite_id + "%"))
    if not cur.rowcount:
        raise AccountError("no open invite with that id", 404)
    audit(conn, actor["id"], "invite_revoke", invite_id)


def _open_token(conn, token: str, purpose: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM invites WHERE token_hash = ? AND purpose = ?",
                       (hash_token(token or ""), purpose)).fetchone()
    if row is None or row["used_at"] or row["used_by"] or row["expires_at"] <= iso(utcnow()):
        raise AccountError("invalid or expired link", 403)
    return row


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"use at least {MIN_PASSWORD} characters")


def register(conn: sqlite3.Connection, token: str, login_name: str, password: str) -> dict:
    """FR-51: the only way to create a non-owner account. Without a valid invite: 403."""
    invite = _open_token(conn, token, "invite")
    login_name = (login_name or "").strip()
    if not 3 <= len(login_name) <= 64:
        raise AccountError("login name must be 3 to 64 characters")
    _check_password(password)
    if conn.execute("SELECT 1 FROM labelers WHERE login_name = ?", (login_name,)).fetchone():
        raise AccountError("that login name is taken", 409)
    labeler = create_labeler(conn, login_name, password, role=invite["role"], clearance=invite["clearance"],
                             status="onboarding")
    conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ?",
                 (labeler["id"], iso(utcnow()), invite["token_hash"]))
    audit(conn, labeler["id"], "register", labeler["pseudonym"],
          {"role": invite["role"], "clearance": invite["clearance"], "invited_by": invite["created_by"]})
    return labeler


def create_reset(conn: sqlite3.Connection, actor: dict, pseudonym: str) -> dict:
    """FR-52: a single-use password-reset link for one labeler."""
    target = _labeler(conn, pseudonym)
    check_can_manage(actor, target)
    out = _new_token(conn, purpose="reset", role=None, clearance=None, created_by=actor["id"],
                     labeler_id=target["id"])
    audit(conn, actor["id"], "password_reset_link", pseudonym)
    return {**out, "pseudonym": pseudonym, "path": f"/?reset={out['token']}"}


def reset_password(conn: sqlite3.Connection, token: str, password: str) -> dict:
    link = _open_token(conn, token, "reset")
    _check_password(password)
    target = conn.execute("SELECT * FROM labelers WHERE id = ?", (link["labeler_id"],)).fetchone()
    if target is None or target["status"] == "revoked":
        raise AccountError("invalid or expired link", 403)
    conn.execute("UPDATE labelers SET password_hash = ? WHERE id = ?", (hash_password(password), target["id"]))
    conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ?",
                 (target["id"], iso(utcnow()), link["token_hash"]))
    revoke_sessions(conn, target["id"])
    audit(conn, target["id"], "password_reset", target["pseudonym"])
    return {"pseudonym": target["pseudonym"], "login_name": target["login_name"]}


# ============================================================================
# Management (FR-52)
# ============================================================================

def has_passed_quiz(conn: sqlite3.Connection, labeler_id: int) -> bool:
    return conn.execute("SELECT 1 FROM quiz_attempts WHERE labeler_id = ? AND passed = 1",
                        (labeler_id,)).fetchone() is not None


def set_status(conn: sqlite3.Connection, actor: Optional[dict], pseudonym: str, action: str, *,
               reason: Optional[str] = None, skip_quiz: bool = False) -> dict:
    """
    pause | resume | revoke. Revoking ends every session at once and is final
    (invite the person again). Resuming returns a labeler to ``active`` if they
    have passed a quiz, otherwise to ``onboarding``; the owner may skip the quiz.
    ``actor`` None is the system (FR-30 auto-pause).
    """
    target = _labeler(conn, pseudonym)
    if actor is not None:
        check_can_manage(actor, target)
    if target["status"] == "revoked":
        raise AccountError("this account is revoked; send a new invite instead", 409)
    if action == "pause":
        status, pause_reason = "paused", reason or "manual"
    elif action == "resume":
        if skip_quiz and (actor is None or actor["role"] != "owner"):
            raise AccountError("only the owner can skip the quiz", 403)
        status = "active" if skip_quiz or has_passed_quiz(conn, target["id"]) else "onboarding"
        pause_reason = None
    elif action == "revoke":
        status, pause_reason = "revoked", target["pause_reason"]
        revoke_sessions(conn, target["id"])
    else:
        raise AccountError("action must be pause, resume or revoke")
    conn.execute("UPDATE labelers SET status = ?, pause_reason = ? WHERE id = ?", (status, pause_reason, target["id"]))
    if status != "active":
        conn.execute("DELETE FROM locks WHERE labeler_id = ?", (target["id"],))
    audit(conn, actor["id"] if actor else None, f"labeler_{action}", pseudonym,
          {"from": target["status"], "to": status, "reason": pause_reason, "skip_quiz": skip_quiz or None})
    return {"pseudonym": pseudonym, "status": status, "pause_reason": pause_reason}


def set_clearance(conn: sqlite3.Connection, actor: dict, pseudonym: str, clearance: str) -> dict:
    """Owner only (FR-52). Locks on items the new clearance can't see are dropped."""
    if actor["role"] != "owner":
        raise AccountError("only the owner changes clearance", 403)
    if clearance not in CLEARANCES:
        raise AccountError(f"clearance must be one of {', '.join(CLEARANCES)}")
    target = _labeler(conn, pseudonym)
    check_can_manage(actor, target)
    conn.execute("UPDATE labelers SET clearance = ? WHERE id = ?", (clearance, target["id"]))
    conn.execute("DELETE FROM locks WHERE labeler_id = ?", (target["id"],))
    audit(conn, actor["id"], "clearance_change", pseudonym, {"from": target["clearance"], "to": clearance})
    return {"pseudonym": pseudonym, "clearance": clearance}


def accept_agreement(conn: sqlite3.Connection, labeler: dict, version: str) -> dict:
    """FR-60: stored with the version accepted."""
    if version != contributor.VERSION:
        raise AccountError(f"the current agreement is version {contributor.VERSION}", 409)
    now = iso(utcnow())
    conn.execute("UPDATE labelers SET agreement_version = ?, agreement_at = ? WHERE id = ?",
                 (version, now, labeler["id"]))
    audit(conn, labeler["id"], "agreement_accept", labeler["pseudonym"], {"version": version})
    return {"agreement_version": version, "agreement_at": now}


def labeler_stats(conn: sqlite3.Connection, labeler_id: int) -> dict:
    """FR-52 / FR-35: counts and timing for one labeler (gold accuracy is added by quality.py)."""
    rows = conn.execute(
        """SELECT a.skipped_code, a.active_ms, a.is_gold_probe, a.created_at FROM annotations a
           WHERE a.labeler_id = ? AND a.version = 1""", (labeler_id,)).fetchall()
    today = utcnow().strftime("%Y-%m-%d")
    labelled = [r for r in rows if r["skipped_code"] is None]
    times = [r["active_ms"] for r in labelled if r["active_ms"] is not None]
    return {
        "n_labelled": len(labelled),
        "n_skipped": len(rows) - len(labelled),
        "n_today": sum(1 for r in labelled if r["created_at"].startswith(today)),
        "n_gold_probes": sum(1 for r in labelled if r["is_gold_probe"]),
        "median_active_ms": median(times) if times else None,
        "n_edits": conn.execute("SELECT COUNT(*) FROM annotations WHERE labeler_id = ? AND version > 1",
                                (labeler_id,)).fetchone()[0],
    }
