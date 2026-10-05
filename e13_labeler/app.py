#!/usr/bin/env python3
"""
E13 labeler: a web app for collecting blind, multi-labeler abstain-reason labels
with evidence spans (docs/e13/E13_LABELING_APP_REQUIREMENTS.md).

This module descends from the NLI span labeler's ``app.py`` (tag ``legacy-final``,
commit 72c9bbb). What survives from it: the FastAPI app and middleware stack
(rate limiting, CORS lockdown, request logging), cookie sessions, item locks and
the flag endpoint. Everything premise/hypothesis-specific is gone.

Usage:
    python -m e13_labeler create-owner     # once, at install
    python -m e13_labeler serve            # or ./run.sh
    # then open http://localhost:8000

Configuration is via environment variables; see ``e13_labeler/config.py``.
"""

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import __version__, config
from .auth import (
    client_ip,
    create_session,
    delete_session,
    get_current_labeler,
    get_labeler_from_session,
    is_loopback,
    iso,
    labeler_dict,
    require_admin,
    utcnow,
    verify_password,
)
from .batches import set_status
from .db import audit, get_db, init_db
from .labelling import (
    PolicyViolation,
    Span,
    Submission,
    SubmissionError,
    blind_question,
    reasons_json,
    validate_submission,
)
from .reasons import SKIP_CODES
from .tiers import tiers_within, visible_tiers

# Initialize rate limiter with the proxy-aware key function
limiter = Limiter(
    key_func=client_ip,
    enabled=config.RATE_LIMIT_ENABLED,
    default_limits=[config.RATE_LIMIT_DEFAULT],
)

# ============================================================================
# API Documentation
# ============================================================================

API_DESCRIPTION = """
# E13 labeler API

Collects blind, independent human labels of **abstain reasons** for (state, question)
items, with the evidence spans that triggered them, and measures per-reason
agreement with Krippendorff's α.

## Authentication

Endpoints need a session cookie from `/api/auth/login`. Accounts are created by the
owner (`python -m e13_labeler create-owner`, invites later). With `SINGLE_USER=1` the
owner is logged in automatically, and only loopback requests are served.

## Item locking

An item handed out by `/api/next` is locked to its labeler for `LOCK_TIMEOUT_MINUTES`
(default 20). Locks are released on submit or skip, or via `/api/lock/release/{item_id}`.

## Visibility

Every item carries a `permissions` tier (`libre`, `restricted`, `jev`, `jev+restricted`).
`public` labelers see `libre` items only; any other item answers 404, even by direct id.
"""

TAGS_METADATA = [
    {"name": "Authentication", "description": "Login, logout and session status."},
    {"name": "Admin", "description": "Owner/admin endpoints for labeler management."},
    {"name": "Annotation", "description": "Getting, labelling, skipping and flagging items."},
    {"name": "Locking", "description": "Item locks for concurrent labelling."},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="E13 labeler API",
    description=API_DESCRIPTION,
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=TAGS_METADATA,
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


# ============================================================================
# Middleware
# ============================================================================

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=config.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def single_user_guard(request: Request, call_next):
    """FR-54: in SINGLE_USER mode, refuse every request that isn't from loopback."""
    if config.single_user() and not is_loopback(request):
        return JSONResponse({"detail": "SINGLE_USER mode serves loopback only"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log all requests with timing, IP and labeler pseudonym."""
    start_time = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000

    who = "anon"
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        try:
            labeler = get_labeler_from_session(token)
            if labeler:
                who = labeler["pseudonym"]
        except Exception:
            pass  # Don't break the request on a logging failure

    # Format: ISO timestamp | IP | METHOD /path | status | duration | labeler
    log_line = (
        f"{iso(utcnow())} | {client_ip(request)} | {request.method} {request.url.path} | "
        f"{response.status_code} | {duration_ms:.0f}ms | {who}"
    )
    if response.status_code >= 500:
        config.request_logger.error(log_line)
    elif response.status_code >= 400:
        config.request_logger.warning(log_line)
    else:
        config.request_logger.info(log_line)
    return response


# ============================================================================
# Pydantic Models
# ============================================================================

class LabelerLogin(BaseModel):
    login_name: str = Field(..., description="Login name")
    password: str = Field(..., description="Password")


class LabelerResponse(BaseModel):
    id: int
    pseudonym: str = Field(..., description="Pseudonymous id used in exports, e.g. L03")
    login_name: Optional[str] = None
    role: str
    clearance: str
    status: str
    single_user: bool = False


class LockStatusResponse(BaseModel):
    item_id: str
    locked: bool
    locked_by: Optional[str] = Field(None, description="Pseudonym of the lock owner")
    locked_until: Optional[str] = None
    expires_in_seconds: Optional[int] = None
    is_own_lock: bool = False


# ============================================================================
# Item visibility and locks
# ============================================================================

def fetch_visible_item(conn, item_id: str, labeler: dict):
    """
    The item row if the labeler's clearance allows it, else 404 (FR-50, FR-57).
    A hidden item and a missing one look the same.
    """
    tiers = visible_tiers(labeler["clearance"])
    row = conn.execute(
        f"SELECT * FROM items WHERE item_id = ? AND permissions IN ({','.join('?' * len(tiers))})",
        (item_id, *tiers),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Item not found: {item_id}")
    return row


def acquire_lock(conn, item_id: str, labeler_id: int) -> Optional[str]:
    """
    Lock an item for a labeler, or extend their own lock. Returns the expiry,
    or None if someone else holds a live lock. A single upsert, so two labelers
    racing for the same item can't both win.
    """
    now = utcnow()
    until = iso(now + timedelta(minutes=config.LOCK_TIMEOUT_MINUTES))
    cur = conn.execute(
        """INSERT INTO locks (item_id, labeler_id, until, served_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               served_at = CASE WHEN locks.labeler_id = excluded.labeler_id AND locks.until > ?
                                THEN locks.served_at ELSE excluded.served_at END,
               labeler_id = excluded.labeler_id, until = excluded.until
           WHERE locks.labeler_id = excluded.labeler_id OR locks.until <= ?""",
        (item_id, labeler_id, until, iso(now), iso(now), iso(now)),
    )
    return until if cur.rowcount else None


def release_lock(conn, item_id: str, labeler_id: int) -> bool:
    cur = conn.execute("DELETE FROM locks WHERE item_id = ? AND labeler_id = ?", (item_id, labeler_id))
    return cur.rowcount > 0


def get_lock_status(conn, item_id: str) -> Optional[dict]:
    row = conn.execute(
        """SELECT k.labeler_id, k.until, l.pseudonym FROM locks k
           JOIN labelers l ON k.labeler_id = l.id WHERE k.item_id = ? AND k.until > ?""",
        (item_id, iso(utcnow())),
    ).fetchone()
    if not row:
        return None
    return {
        "labeler_id": row["labeler_id"],
        "pseudonym": row["pseudonym"],
        "until": row["until"],
        "expires_in_seconds": max(0, int((_parse(row["until"]) - utcnow()).total_seconds())),
    }


def cleanup_expired_locks(conn) -> int:
    return conn.execute("DELETE FROM locks WHERE until <= ?", (iso(utcnow()),)).rowcount


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ============================================================================
# API Endpoints - Root
# ============================================================================

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    """Serve the labelling interface."""
    return FileResponse(config.STATIC_DIR / "index.html")


# ============================================================================
# API Endpoints - Authentication
# ============================================================================

@app.post("/api/auth/login", tags=["Authentication"], summary="Log in")
@limiter.limit(config.RATE_LIMIT_AUTH)
async def login(request: Request, credentials: LabelerLogin, response: Response):
    """Authenticate with login name and password. Sets an HttpOnly, SameSite=Strict session cookie."""
    if config.single_user():
        raise HTTPException(400, "Login is not used in SINGLE_USER mode")

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM labelers WHERE login_name = ? AND kind = 'human'", (credentials.login_name,)
        ).fetchone()
        if not row or not verify_password(credentials.password, row["password_hash"]):
            raise HTTPException(401, "Invalid login name or password")
        if row["status"] == "revoked":
            raise HTTPException(403, "This account has been revoked")
        audit(conn, row["id"], "login", row["pseudonym"], {"ip": client_ip(request)})

    token = create_session(row["id"])
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="strict",
        max_age=config.SESSION_EXPIRY_DAYS * 24 * 60 * 60,
    )
    return {"status": "logged_in", "labeler": labeler_dict(row)}


@app.post("/api/auth/logout", tags=["Authentication"], summary="Log out")
async def logout(request: Request, response: Response):
    """Invalidate the current session and clear the session cookie."""
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        delete_session(token)
    response.delete_cookie(config.SESSION_COOKIE)
    return {"status": "logged_out"}


@app.get("/api/me", tags=["Authentication"], summary="Current labeler", response_model=LabelerResponse)
async def get_me(labeler: dict = Depends(get_current_labeler)):
    return LabelerResponse(**labeler, single_user=config.single_user())


@app.get("/api/auth/status", tags=["Authentication"], summary="Auth mode")
async def auth_status():
    """Whether the server runs in SINGLE_USER mode. Self-registration is always off (FR-51)."""
    return {"single_user": config.single_user(), "registration_enabled": False, "version": __version__}


# ============================================================================
# API Endpoints - Admin
# ============================================================================

@app.get("/api/admin/labelers", tags=["Admin"], summary="List labelers")
async def list_labelers(admin: dict = Depends(require_admin)):
    """All accounts, human and model, by pseudonym. Contact details are never included."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM labelers ORDER BY id").fetchall()
        return {
            "labelers": [
                {**labeler_dict(r), "kind": r["kind"], "created_at": r["created_at"], "last_seen": r["last_seen"]}
                for r in rows
            ]
        }


# ============================================================================
# API Endpoints - Locking
# ============================================================================

@app.get(
    "/api/lock/status/{item_id:path}",
    tags=["Locking"],
    summary="Get lock status",
    response_model=LockStatusResponse,
)
async def get_item_lock_status(item_id: str, labeler: dict = Depends(get_current_labeler)):
    with get_db() as conn:
        fetch_visible_item(conn, item_id, labeler)
        status = get_lock_status(conn, item_id)
    if not status:
        return LockStatusResponse(item_id=item_id, locked=False)
    return LockStatusResponse(
        item_id=item_id,
        locked=True,
        locked_by=status["pseudonym"],
        locked_until=status["until"],
        expires_in_seconds=status["expires_in_seconds"],
        is_own_lock=status["labeler_id"] == labeler["id"],
    )


@app.post("/api/lock/release/{item_id:path}", tags=["Locking"], summary="Release lock")
async def release_item_lock(item_id: str, labeler: dict = Depends(get_current_labeler)):
    with get_db() as conn:
        fetch_visible_item(conn, item_id, labeler)
        if release_lock(conn, item_id, labeler["id"]):
            return {"status": "released", "item_id": item_id}
        status = get_lock_status(conn, item_id)
    if status:
        raise HTTPException(403, f"Item is locked by {status['pseudonym']}")
    return {"status": "not_locked", "item_id": item_id}


@app.post("/api/lock/extend/{item_id:path}", tags=["Locking"], summary="Extend lock")
async def extend_item_lock(item_id: str, labeler: dict = Depends(get_current_labeler)):
    with get_db() as conn:
        fetch_visible_item(conn, item_id, labeler)
        status = get_lock_status(conn, item_id)
        if not status:
            raise HTTPException(404, "No lock exists for this item")
        if status["labeler_id"] != labeler["id"]:
            raise HTTPException(403, f"Item is locked by {status['pseudonym']}")
        until = acquire_lock(conn, item_id, labeler["id"])
    return {
        "status": "extended",
        "item_id": item_id,
        "lock_until": until,
        "expires_in_seconds": config.LOCK_TIMEOUT_MINUTES * 60,
    }


@app.get("/api/lock/mine", tags=["Locking"], summary="List my locks")
async def list_my_locks(labeler: dict = Depends(get_current_labeler)):
    with get_db() as conn:
        cleanup_expired_locks(conn)
        rows = conn.execute(
            "SELECT item_id, until FROM locks WHERE labeler_id = ? ORDER BY until DESC", (labeler["id"],)
        ).fetchall()
    return {"locks": [{"item_id": r["item_id"], "locked_until": r["until"]} for r in rows], "count": len(rows)}


# ============================================================================
# API Endpoints - Flags
# ============================================================================

FLAG_KINDS = ("bad_item", "guideline_unclear", "other")


@app.post("/api/flag", tags=["Annotation"], summary="Flag an item for review")
async def flag_item(
    item_id: str = Body(..., embed=True),
    kind: str = Body(..., embed=True, description="bad_item, guideline_unclear or other"),
    note: Optional[str] = Body(None, embed=True, max_length=2000),
    labeler: dict = Depends(get_current_labeler),
):
    """FR-44: labelers flag an item from the labelling screen; flags go to an admin list."""
    if kind not in FLAG_KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(FLAG_KINDS)}")
    with get_db() as conn:
        fetch_visible_item(conn, item_id, labeler)
        if conn.execute(
            "SELECT 1 FROM flags WHERE item_id = ? AND labeler_id = ? AND kind = ?",
            (item_id, labeler["id"], kind),
        ).fetchone():
            raise HTTPException(400, "You have already flagged this item for this reason")
        cur = conn.execute(
            "INSERT INTO flags (item_id, labeler_id, kind, note) VALUES (?, ?, ?, ?)",
            (item_id, labeler["id"], kind, note),
        )
    return {"status": "flagged", "flag_id": cur.lastrowid, "item_id": item_id}


@app.get("/api/admin/flags", tags=["Admin"], summary="List flags")
async def list_flags(status: Optional[str] = "pending", admin: dict = Depends(require_admin)):
    with get_db() as conn:
        query = """SELECT f.id, f.item_id, f.kind, f.note, f.status, f.created_at, l.pseudonym
                   FROM flags f JOIN labelers l ON f.labeler_id = l.id"""
        params = []
        if status:
            query += " WHERE f.status = ?"
            params.append(status)
        rows = conn.execute(query + " ORDER BY f.created_at DESC", params).fetchall()
    return {"flags": [dict(r) for r in rows], "count": len(rows)}


# ============================================================================
# API Endpoints - Labelling (requirements §4.2, §5.3)
# ============================================================================

class SpanIn(BaseModel):
    side: str = Field(..., description="state or option")
    role: str = Field(..., description="support, refute, unsupported or framing")
    text: str = Field(..., description="The selected text; must equal the slice")
    option: Optional[str] = Field(None, description="Option the span is about: choice key, score level, true/false")
    pointer: Optional[str] = Field(None, description="RFC 6901 pointer, for JSON states")
    start: Optional[int] = None
    end: Optional[int] = None
    reasons: list[str] = Field(default_factory=list, description="Checked reasons this span triggered")


class AnnotationIn(BaseModel):
    item_id: str
    answerable: bool = False
    reasons: list[str] = Field(default_factory=list, description="The checked reasons")
    note: Optional[str] = None
    spans: list[SpanIn] = Field(default_factory=list)
    policy_override: bool = Field(False, description="Shift+Enter: save despite unmet span policy")
    active_ms: Optional[int] = Field(None, description="Client-measured active time (FR-22)")


class SkipIn(BaseModel):
    item_id: str
    code: str = Field(..., description="cannot_judge, broken_item, offensive, too_long or other")
    note: Optional[str] = Field(None, max_length=2000)


def _open_batches(conn):
    return conn.execute("SELECT * FROM batches WHERE status = 'open' ORDER BY priority DESC, id").fetchall()


def _batch_tiers(batch, labeler: dict) -> tuple:
    """FR-57: the labeler's clearance, narrowed by the batch's tier ceiling."""
    within = tiers_within(batch["tier_ceiling"])
    return tuple(t for t in visible_tiers(labeler["clearance"]) if t in within)


def _human_label_count_sql() -> str:
    # Labels that count toward the overlap target: human, not skipped, not gold probes.
    return """(SELECT COUNT(DISTINCT a.labeler_id) FROM annotations a JOIN labelers h ON a.labeler_id = h.id
               WHERE a.item_id = i.item_id AND a.batch_id = b.id AND a.skipped_code IS NULL
                 AND a.is_gold_probe = 0 AND h.kind = 'human')"""


def pick_next(conn, labeler: dict):
    """
    FR-32: an item in an open batch, within the labeler's clearance and the batch
    ceiling, never labelled or skipped by this labeler (in any batch), not locked
    by someone else, and below its overlap target. Items that already have labels
    from others come first (complete pairs early), then batch priority, then random.
    A labeler who still holds a lock gets that item back.
    """
    now = iso(utcnow())
    held = conn.execute(
        """SELECT i.*, b.id AS batch_id FROM locks k JOIN items i ON i.item_id = k.item_id
           JOIN batch_items bi ON bi.item_id = i.item_id JOIN batches b ON b.id = bi.batch_id
           WHERE k.labeler_id = ? AND k.until > ? AND b.status = 'open'
             AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?)
           ORDER BY k.served_at LIMIT 1""",
        (labeler["id"], now, labeler["id"]),
    ).fetchone()
    if held and held["permissions"] in visible_tiers(labeler["clearance"]):
        return held, conn.execute("SELECT * FROM batches WHERE id = ?", (held["batch_id"],)).fetchone()

    best = None
    for batch in _open_batches(conn):
        allowed = _batch_tiers(batch, labeler)
        if not allowed:
            continue
        row = conn.execute(
            f"""SELECT i.*, {_human_label_count_sql()} AS n_labels
                FROM batch_items bi JOIN batches b ON b.id = bi.batch_id JOIN items i ON i.item_id = bi.item_id
                WHERE b.id = ? AND i.permissions IN ({','.join('?' * len(allowed))})
                  AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?)
                  AND NOT EXISTS (SELECT 1 FROM locks k WHERE k.item_id = i.item_id
                                  AND k.labeler_id != ? AND k.until > ?)
                  AND n_labels < b.overlap_target
                ORDER BY n_labels > 0 DESC, RANDOM() LIMIT 1""",
            (batch["id"], *allowed, labeler["id"], labeler["id"], now),
        ).fetchone()
        if row is None:
            continue
        key = (row["n_labels"] > 0, batch["priority"])
        if best is None or key > best[0]:
            best = (key, row, batch)
    return (best[1], best[2]) if best else (None, None)


def blind_payload(conn, item, batch, labeler: dict, lock_until: str) -> dict:
    """§5.3: exactly what a labeler may see. No source, gold, e13, model answers or others' labels."""
    reason_set = json.loads(batch["reason_set_json"])
    done = conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE labeler_id = ? AND batch_id = ? AND skipped_code IS NULL",
        (labeler["id"], batch["id"]),
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id = ?", (batch["id"],)).fetchone()[0]
    complete = conn.execute(
        f"""SELECT COUNT(*) FROM batch_items bi JOIN items i ON i.item_id = bi.item_id
            JOIN batches b ON b.id = bi.batch_id
            WHERE b.id = ? AND {_human_label_count_sql()} >= b.overlap_target""",
        (batch["id"],),
    ).fetchone()[0]
    return {
        "item_id": item["item_id"],
        "lock_until": lock_until,
        "state": item["state"],
        "state_format": item["state_format"],
        "question": blind_question(json.loads(item["question_json"])),
        "reason_set": reason_set,
        "task_type": batch["task_type"],
        "span_policy": {r: p for r, p in json.loads(batch["span_policy_json"]).items() if r in reason_set},
        "require_note": bool(batch["require_note"]),
        "progress": {"batch": batch["name"], "done_by_me": done,
                     "batch_pct": round(complete / total, 4) if total else 0.0},
    }


@app.get("/api/next", tags=["Annotation"], summary="Next item to label")
async def next_item(labeler: dict = Depends(get_current_labeler)):
    """Serve and lock the next item (FR-32), as the blind payload of §5.3 (FR-12)."""
    if labeler["status"] in ("paused", "revoked"):
        raise HTTPException(403, f"Account is {labeler['status']}")
    with get_db() as conn:
        item, batch = pick_next(conn, labeler)
        if item is None:
            raise HTTPException(404, "No items to label right now")
        until = acquire_lock(conn, item["item_id"], labeler["id"])
        if until is None:  # lost a race for the lock; the client just asks again
            raise HTTPException(409, "Item was just taken; request the next one")
        return blind_payload(conn, item, batch, labeler, until)


def _assignment(conn, item_id: str, labeler: dict):
    """The item and its open batch for a submission, enforcing visibility, lock and once-only."""
    item = fetch_visible_item(conn, item_id, labeler)
    lock = get_lock_status(conn, item_id)
    if lock and lock["labeler_id"] != labeler["id"]:
        raise HTTPException(409, f"Item is locked by {lock['pseudonym']}")
    if conn.execute("SELECT 1 FROM annotations WHERE item_id = ? AND labeler_id = ?",
                    (item_id, labeler["id"])).fetchone():
        raise HTTPException(409, "You have already labelled or skipped this item")
    for batch in _open_batches(conn):
        if item["permissions"] in _batch_tiers(batch, labeler) and conn.execute(
            "SELECT 1 FROM batch_items WHERE batch_id = ? AND item_id = ?", (batch["id"], item_id)
        ).fetchone():
            return item, batch
    raise HTTPException(404, f"Item {item_id} is not in an open batch")


def _wall_ms(conn, item_id: str, labeler_id: int) -> Optional[int]:
    row = conn.execute("SELECT served_at FROM locks WHERE item_id = ? AND labeler_id = ?",
                       (item_id, labeler_id)).fetchone()
    if not row or not row["served_at"]:
        return None
    return int((utcnow() - _parse(row["served_at"])).total_seconds() * 1000)


@app.post("/api/annotations", tags=["Annotation"], summary="Submit labels for an item")
async def submit_annotation(body: AnnotationIn, labeler: dict = Depends(get_current_labeler)):
    """
    Validates FR-13 to FR-19 and stores one annotation version with its spans.
    Unmet span policy answers 422 with ``policy: true``; resubmit with
    ``policy_override`` to save anyway (recorded).
    """
    submission = Submission(
        answerable=body.answerable, reasons=body.reasons, note=body.note,
        spans=[Span(**s.model_dump()) for s in body.spans],
        policy_override=body.policy_override, active_ms=body.active_ms,
    )
    with get_db() as conn:
        item, batch = _assignment(conn, body.item_id, labeler)
        reason_set = json.loads(batch["reason_set_json"])
        try:
            validate_submission(
                submission, state=item["state"], state_format=item["state_format"],
                question=json.loads(item["question_json"]), reason_set=reason_set,
                span_policy=json.loads(batch["span_policy_json"]), require_note=bool(batch["require_note"]),
            )
        except PolicyViolation as e:
            raise HTTPException(422, {"policy": True, "problems": e.problems})
        except SubmissionError as e:
            raise HTTPException(422, {"policy": False, "problems": e.problems})

        cur = conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, answerable, reasons_json, note,
                                        policy_override, active_ms, wall_ms, guideline_version, app_version)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["item_id"], batch["id"], labeler["id"], int(submission.answerable),
             json.dumps(reasons_json(submission.reasons, reason_set)), submission.note,
             int(submission.policy_override), submission.active_ms,
             _wall_ms(conn, item["item_id"], labeler["id"]), batch["guideline_version"], config.app_version()),
        )
        annotation_id = cur.lastrowid
        for s in submission.spans:
            conn.execute(
                """INSERT INTO spans (annotation_id, side, option, pointer, start, "end", text, role, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (annotation_id, s.side, s.option, s.pointer, s.start, s.end, s.text, s.role, json.dumps(s.reasons)),
            )
        release_lock(conn, item["item_id"], labeler["id"])
    return {"status": "saved", "annotation_id": annotation_id, "version": 1,
            "policy_override": submission.policy_override}


@app.post("/api/skip", tags=["Annotation"], summary="Skip an item")
async def skip_item(body: SkipIn, labeler: dict = Depends(get_current_labeler)):
    """FR-20: skip with a reason code. The item is never served to this labeler again."""
    if body.code not in SKIP_CODES:
        raise HTTPException(422, f"code must be one of {', '.join(SKIP_CODES)}")
    with get_db() as conn:
        item, batch = _assignment(conn, body.item_id, labeler)
        conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, skipped_code, note,
                                        wall_ms, guideline_version, app_version)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (item["item_id"], batch["id"], labeler["id"], body.code, body.note,
             _wall_ms(conn, item["item_id"], labeler["id"]), batch["guideline_version"], config.app_version()),
        )
        release_lock(conn, item["item_id"], labeler["id"])
    return {"status": "skipped", "item_id": body.item_id}


# ============================================================================
# API Endpoints - Batches
# ============================================================================

@app.get("/api/admin/batches", tags=["Admin"], summary="List batches")
async def list_batches(admin: dict = Depends(require_admin)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT b.*, (SELECT COUNT(*) FROM batch_items WHERE batch_id = b.id) AS n_items
               FROM batches b ORDER BY b.id"""
        ).fetchall()
    return {"batches": [
        {"name": r["name"], "status": r["status"], "task_type": r["task_type"], "n_items": r["n_items"],
         "overlap_target": r["overlap_target"], "tier_ceiling": r["tier_ceiling"], "priority": r["priority"],
         "reason_set": json.loads(r["reason_set_json"]), "require_note": bool(r["require_note"])}
        for r in rows
    ]}


@app.post("/api/admin/batches/{name}/status", tags=["Admin"], summary="Open or close a batch")
async def set_batch_status(name: str, status: str = Body(..., embed=True), admin: dict = Depends(require_admin)):
    with get_db() as conn:
        try:
            set_status(conn, name, status, admin["id"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    return {"name": name, "status": status}
