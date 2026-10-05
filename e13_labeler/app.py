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

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import __version__, config
from .auth import (
    CSRF_COOKIE,
    client_ip,
    create_session,
    csrf_ok,
    csrf_token,
    needs_agreement,
    delete_session,
    get_current_labeler,
    get_labeler_from_session,
    is_loopback,
    iso,
    labeler_dict,
    require_active,
    require_admin,
    require_owner,
    utcnow,
    verify_password,
)
from . import batches as batches_mod
from .batches import set_status
from .db import audit, get_db, init_db
from .labelling import (
    PolicyViolation,
    Span,
    Submission,
    SubmissionError,
    blind_question,
    item_asof,
    reasons_json,
    validate_submission,
)
from .reasons import CANDIDATES, DEFINITIONS, HARD_SPAN_RULES, REASONS, SKIP_CODES
from .importer import is_jev
from .quality import check_auto_pause
from .tiers import tiers_within, visible_to
from .api_accounts import router as accounts_router, set_session_cookies
from .api_onboarding import router as onboarding_router

from .ratelimit import limiter

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
    {"name": "Onboarding", "description": "Guideline, quiz and the retraining quiz."},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from .backup import backup_loop, interval_hours

    init_db()
    task = asyncio.create_task(backup_loop()) if interval_hours() > 0 else None  # NFR-6
    yield
    if task:
        task.cancel()


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

app.include_router(accounts_router)
app.include_router(onboarding_router)
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
async def host_check(request: Request, call_next):
    """Refuse unexpected Host headers (DNS rebinding); see config.allowed_hosts()."""
    allowed = config.allowed_hosts()
    if "*" not in allowed:
        host = request.headers.get("host", "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if host not in allowed and name not in allowed:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def single_user_guard(request: Request, call_next):
    """FR-54: in SINGLE_USER mode, refuse every request that isn't from loopback."""
    if config.single_user() and not is_loopback(request):
        return JSONResponse({"detail": "SINGLE_USER mode serves loopback only"}, status_code=403)
    return await call_next(request)


CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register", "/api/auth/reset"}


@app.middleware("http")
async def csrf_check(request: Request, call_next):
    """
    NFR-5: mutating API requests must echo the CSRF token (cookie e13_csrf) in
    X-CSRF-Token. Login, registration and reset carry no session to abuse.
    """
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/")
            and request.url.path not in CSRF_EXEMPT and not csrf_ok(request)):
        return JSONResponse({"detail": "CSRF token missing or invalid; reload the page"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log all requests with timing, IP and the pseudonym the auth dependency resolved."""
    start_time = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000

    labeler = getattr(request.state, "labeler", None)
    who = labeler["pseudonym"] if labeler else "anon"

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
    pause_reason: Optional[str] = None
    agreement_version: Optional[str] = None
    needs_agreement: bool = False
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

def is_admin(labeler: dict) -> bool:
    return labeler["role"] in ("owner", "admin")


def _locked_message(lock: dict, labeler: dict) -> str:
    """Labelers don't learn who else is working on what (blindness); admins do."""
    return f"Item is locked by {lock['pseudonym']}" if is_admin(labeler) else "Item is locked by another labeler"


def fetch_visible_item(conn, item_id: str, labeler: dict):
    """
    The item row if the labeler's clearance allows it, else 404 (FR-50, FR-57).
    A hidden item and a missing one look the same.
    """
    allowed = visible_to(labeler["clearance"])
    row = conn.execute(
        f"SELECT * FROM items WHERE item_id = ? AND visibility IN ({','.join('?' * len(allowed))})",
        (item_id, *allowed),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Item not found: {item_id}")
    return row


def acquire_lock(conn, item_id: str, labeler_id: int, batch_id: Optional[int] = None,
                 gold_probe: bool = False) -> Optional[str]:
    """
    Lock an item for a labeler, or extend their own lock. Returns the expiry,
    or None if someone else holds a live lock. A single upsert, so two labelers
    racing for the same item can't both win. ``batch_id`` records which batch
    served it, so the submit lands there; ``gold_probe`` marks a hidden gold
    item (FR-28). Extending a live lock keeps both.
    """
    now = utcnow()
    until = iso(now + timedelta(minutes=config.LOCK_TIMEOUT_MINUTES))
    cur = conn.execute(
        """INSERT INTO locks (item_id, labeler_id, until, served_at, batch_id, gold_probe) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               served_at = CASE WHEN locks.labeler_id = excluded.labeler_id AND locks.until > ?
                                THEN locks.served_at ELSE excluded.served_at END,
               asof = CASE WHEN locks.labeler_id = excluded.labeler_id AND locks.until > ?
                           THEN locks.asof ELSE NULL END,
               gold_probe = CASE WHEN locks.labeler_id = excluded.labeler_id AND locks.until > ?
                                 THEN MAX(locks.gold_probe, excluded.gold_probe) ELSE excluded.gold_probe END,
               batch_id = COALESCE(excluded.batch_id, locks.batch_id),
               labeler_id = excluded.labeler_id, until = excluded.until
           WHERE locks.labeler_id = excluded.labeler_id OR locks.until <= ?""",
        (item_id, labeler_id, until, iso(now), batch_id, int(gold_probe), iso(now), iso(now), iso(now), iso(now)),
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

    set_session_cookies(response, create_session(row["id"]))
    return {"status": "logged_in", "labeler": labeler_dict(row)}


@app.post("/api/auth/logout", tags=["Authentication"], summary="Log out")
async def logout(request: Request, response: Response):
    """Invalidate the current session and clear the session cookie."""
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        delete_session(token)
    response.delete_cookie(config.SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"status": "logged_out"}


@app.get("/api/me", tags=["Authentication"], summary="Current labeler", response_model=LabelerResponse)
async def get_me(request: Request, response: Response, labeler: dict = Depends(get_current_labeler)):
    # (Re)issue the CSRF cookie: SINGLE_USER mode has no login to set it
    response.set_cookie(CSRF_COOKIE, csrf_token(request.cookies.get(config.SESSION_COOKIE)), httponly=False,
                        secure=config.cookie_secure(), samesite="strict")
    return LabelerResponse(**labeler, needs_agreement=needs_agreement(labeler), single_user=config.single_user())


@app.get("/api/auth/status", tags=["Authentication"], summary="Auth mode")
async def auth_status():
    """Whether the server runs in SINGLE_USER mode. Registration needs an invite (FR-51)."""
    return {"single_user": config.single_user(), "registration_enabled": False, "invite_only": True,
            "version": __version__}


@app.get("/api/reasons", tags=["Annotation"], summary="Reason definitions")
async def reason_definitions():
    """The label set and the short definitions shown to labelers (requirements §1.4)."""
    return {"reasons": [{"key": r, "definition": DEFINITIONS[r], "candidate": r in CANDIDATES} for r in REASONS],
            "answerable": "No abstain reason applies: the state settles the question and an option fits."}


# ============================================================================
# API Endpoints - Admin
# ============================================================================

@app.get("/api/admin/labelers", tags=["Admin"], summary="List labelers")
async def list_labelers(admin: dict = Depends(require_admin)):
    """All accounts, human and model, by pseudonym, with stats (FR-52). Contact details are never included."""
    from .accounts import labeler_stats
    from .quality import gold_accuracy

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM labelers ORDER BY id").fetchall()
        out = []
        for r in rows:
            entry = {**labeler_dict(r), "kind": r["kind"], "created_at": r["created_at"], "last_seen": r["last_seen"]}
            if r["kind"] == "human":
                gold = gold_accuracy(conn, r["id"])
                entry.update(labeler_stats(conn, r["id"]), gold_accuracy=gold["accuracy"],
                             gold_rolling=gold["rolling"], gold_below_threshold=gold["below_threshold"])
            out.append(entry)
        return {"labelers": out}


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
        locked_by=status["pseudonym"] if is_admin(labeler) else None,
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
        raise HTTPException(403, _locked_message(status, labeler))
    return {"status": "not_locked", "item_id": item_id}


@app.post("/api/lock/extend/{item_id:path}", tags=["Locking"], summary="Extend lock")
async def extend_item_lock(item_id: str, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        fetch_visible_item(conn, item_id, labeler)
        status = get_lock_status(conn, item_id)
        if not status:
            raise HTTPException(404, "No lock exists for this item")
        if status["labeler_id"] != labeler["id"]:
            raise HTTPException(403, _locked_message(status, labeler))
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
    labeler: dict = Depends(require_active),
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
    """Flags on items within the admin's clearance (FR-57)."""
    allowed = visible_to(admin["clearance"])
    with get_db() as conn:
        query = f"""SELECT f.id, f.item_id, f.kind, f.note, f.status, f.created_at, f.resolution, f.resolved_at,
                           l.pseudonym, r.pseudonym AS resolved_by
                    FROM flags f JOIN labelers l ON f.labeler_id = l.id LEFT JOIN labelers r ON r.id = f.resolved_by
                    JOIN items i ON i.item_id = f.item_id
                    WHERE i.visibility IN ({','.join('?' * len(allowed))})"""
        params = list(allowed)
        if status:
            query += " AND f.status = ?"
            params.append(status)
        rows = conn.execute(query + " ORDER BY f.created_at DESC", params).fetchall()
    return {"flags": [dict(r) for r in rows], "count": len(rows)}


FLAG_RESOLUTIONS = ("resolved", "dismissed")


@app.post("/api/admin/flags/{flag_id}/resolve", tags=["Admin"], summary="Resolve or dismiss a flag")
async def resolve_flag(flag_id: int, status: str = Body("resolved", embed=True),
                       resolution: Optional[str] = Body(None, embed=True, max_length=2000),
                       admin: dict = Depends(require_admin)):
    """FR-44: the flag keeps its history (who, when, what was done); nothing is deleted."""
    if status not in FLAG_RESOLUTIONS:
        raise HTTPException(422, f"status must be one of {', '.join(FLAG_RESOLUTIONS)}")
    with get_db() as conn:
        flag = conn.execute("SELECT * FROM flags WHERE id = ?", (flag_id,)).fetchone()
        if flag is None:
            raise HTTPException(404, "No such flag")
        fetch_visible_item(conn, flag["item_id"], admin)
        conn.execute("""UPDATE flags SET status = ?, resolution = ?, resolved_by = ?, resolved_at = ?
                        WHERE id = ?""", (status, resolution, admin["id"], iso(utcnow()), flag_id))
        audit(conn, admin["id"], f"flag_{status}", flag["item_id"], {"flag": flag_id, "resolution": resolution})
    return {"id": flag_id, "status": status}


@app.get("/api/admin/progress", tags=["Admin"], summary="Progress and ETA")
async def admin_progress(admin: dict = Depends(require_admin)):
    """FR-35: per batch, items at 0/1/2/3+ labels, % complete and ETA; per labeler, today/total/median time."""
    from .progress import progress

    with get_db() as conn:
        return progress(conn)


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


def _batch_filter(batch, labeler: dict) -> tuple[str, list]:
    """
    SQL (over items ``i``) for what this labeler may get from this batch (FR-57):
    - the item's visibility within the labeler's clearance;
    - its release tier within the batch's tier_ceiling, which can only narrow;
    - if the batch shows a Jev teacher's answer (FR-34), items carrying that
      answer are internal-only (owner decision, 2026-10-05).
    """
    allowed = visible_to(labeler["clearance"])
    within = tiers_within(batch["tier_ceiling"])
    sql = (f"i.visibility IN ({','.join('?' * len(allowed))}) "
           f"AND i.permissions IN ({','.join('?' * len(within))})")
    params = [*allowed, *within]
    shown = batch["show_model_answer"]
    if is_jev(shown) and "restricted" not in allowed:
        sql += """ AND json_extract(i.model_answers_json, '$."' || ? || '"') IS NULL"""
        params.append(shown)
    return sql, params


def _human_label_count_sql() -> str:
    # Labels that count toward the overlap target: human, not skipped, not gold probes.
    return """(SELECT COUNT(DISTINCT a.labeler_id) FROM annotations a JOIN labelers h ON a.labeler_id = h.id
               WHERE a.item_id = i.item_id AND a.batch_id = b.id AND a.skipped_code IS NULL
                 AND a.is_gold_probe = 0 AND h.kind = 'human')"""


def _eligible_sql(batch, labeler: dict) -> tuple[str, list]:
    """
    SQL over ``i`` (item), ``bi`` (batch item), ``b`` (batch) for items this
    labeler may label in this batch, apart from locks.

    Ordinary batch (FR-32): never labelled or skipped by this labeler in any
    batch, and below the item's overlap target (its reliability-subset target,
    else the batch's overlap_target).

    Re-label batch: the one deliberate exception to "never twice". Only items this
    labeler labelled (not skipped) in the source batch at least relabel_after_days
    ago, and not yet in this batch.
    """
    sql, params = _batch_filter(batch, labeler)
    if batch["relabel_of"] is None:
        sql += f"""
            AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?)
            AND {_human_label_count_sql()} < COALESCE(bi.target, b.overlap_target)"""
        params.append(labeler["id"])
    else:
        sql += """
            AND EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?
                        AND a.batch_id = b.relabel_of AND a.skipped_code IS NULL
                        AND a.created_at <= datetime('now', ?))
            AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?
                            AND a.batch_id = b.id)"""
        params += [labeler["id"], f"-{int(batch['relabel_after_days'])} days", labeler["id"]]
    return sql, params


def _eligible(conn, item_id: str, batch, labeler: dict) -> bool:
    sql, params = _eligible_sql(batch, labeler)
    return conn.execute(
        f"""SELECT 1 FROM batch_items bi JOIN batches b ON b.id = bi.batch_id JOIN items i ON i.item_id = bi.item_id
            WHERE b.id = ? AND i.item_id = ? AND {sql}""",
        (batch["id"], item_id, *params),
    ).fetchone() is not None


def gold_rates() -> tuple[float, float, int]:
    """FR-28: (gold_rate_new, gold_rate, warm-up items)."""
    import os

    return (float(os.environ.get("E13_GOLD_RATE_NEW", "0.20")), float(os.environ.get("E13_GOLD_RATE", "0.05")),
            int(os.environ.get("E13_GOLD_WARMUP", "50")))


def should_probe(rng, n_done: int) -> bool:
    """Serve hidden gold now? 20% for a labeler's first 50 items, 5% after (FR-28)."""
    rate_new, rate, warmup = gold_rates()
    return rng.random() < (rate_new if n_done < warmup else rate)


def _probe_candidate(conn, batch, labeler: dict):
    """
    A gold item this labeler has never labelled, skipped or seen in a quiz, that
    the batch's filters allow and nobody else has locked. Owners write gold, so
    they get no probes.
    """
    if labeler["role"] == "owner":
        return None
    sql, params = _batch_filter(batch, labeler)
    return conn.execute(
        f"""SELECT i.* FROM gold g JOIN items i ON i.item_id = g.item_id
            WHERE g.retired = 0 AND {sql}
              AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.item_id = i.item_id AND a.labeler_id = ?)
              AND NOT EXISTS (SELECT 1 FROM quiz_answers q JOIN quiz_attempts t ON t.id = q.attempt_id
                              WHERE q.item_id = i.item_id AND t.labeler_id = ?)
              AND NOT EXISTS (SELECT 1 FROM locks k WHERE k.item_id = i.item_id AND k.until > ?)
            ORDER BY RANDOM() LIMIT 1""",
        (*params, labeler["id"], labeler["id"], iso(utcnow())),
    ).fetchone()


def pick_next(conn, labeler: dict, rng=None):
    """
    FR-32: an eligible item (see _eligible_sql) in an open batch that nobody else
    has locked. Items that already have labels from others come first (complete
    pairs early, so α accrues), then batch priority, then random. A labeler who
    still holds a lock on an eligible item gets that item back.

    FR-28: now and then, a hidden gold item instead, served through the batch the
    ordinary pick came from, so it looks like any other item. Returns
    (item, batch, is_probe).
    """
    import random

    now = iso(utcnow())
    batches = _open_batches(conn)
    for held in conn.execute(
        "SELECT item_id, batch_id, gold_probe FROM locks WHERE labeler_id = ? AND until > ? ORDER BY served_at",
        (labeler["id"], now),
    ).fetchall():
        for batch in batches:
            if held["gold_probe"] and held["batch_id"] == batch["id"]:
                return conn.execute("SELECT * FROM items WHERE item_id = ?", (held["item_id"],)).fetchone(), batch, True
            if held["batch_id"] in (None, batch["id"]) and _eligible(conn, held["item_id"], batch, labeler):
                return conn.execute("SELECT * FROM items WHERE item_id = ?", (held["item_id"],)).fetchone(), batch, False

    best = None
    for batch in batches:
        sql, params = _eligible_sql(batch, labeler)
        row = conn.execute(
            f"""SELECT i.*, {_human_label_count_sql()} AS n_labels
                FROM batch_items bi JOIN batches b ON b.id = bi.batch_id JOIN items i ON i.item_id = bi.item_id
                WHERE b.id = ? AND {sql}
                  AND NOT EXISTS (SELECT 1 FROM locks k WHERE k.item_id = i.item_id
                                  AND k.labeler_id != ? AND k.until > ?)
                ORDER BY n_labels > 0 DESC, RANDOM() LIMIT 1""",
            (batch["id"], *params, labeler["id"], now),
        ).fetchone()
        if row is None:
            continue
        key = (row["n_labels"] > 0, batch["priority"])
        if best is None or key > best[0]:
            best = (key, row, batch)
    if best is None:
        return None, None, False
    _, item, batch = best
    if batch["relabel_of"] is None:  # re-label passes measure consistency; no probes there
        n_done = conn.execute("SELECT COUNT(*) FROM annotations WHERE labeler_id = ? AND version = 1",
                              (labeler["id"],)).fetchone()[0]
        if should_probe(rng or random, n_done):
            probe = _probe_candidate(conn, batch, labeler)
            if probe is not None:
                return probe, batch, True
    return item, batch, False


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
            WHERE b.id = ? AND {_human_label_count_sql()} >= COALESCE(bi.target, b.overlap_target)""",
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
        "span_policy": {r: ("required" if r in HARD_SPAN_RULES else p)
                        for r, p in json.loads(batch["span_policy_json"]).items() if r in reason_set},
        "require_note": bool(batch["require_note"]),
        "asof": item_asof(item),
        # Batch names can describe the design (e.g. "stale-candidates"); labelers get a neutral one
        "progress": {"batch": batch["name"] if is_admin(labeler) else f"batch {batch['id']}", "done_by_me": done,
                     "batch_pct": round(complete / total, 4) if total else 0.0},
    }


@app.get("/api/next", tags=["Annotation"], summary="Next item to label")
async def next_item(labeler: dict = Depends(require_active)):
    """Serve and lock the next item (FR-32), as the blind payload of §5.3 (FR-12)."""
    with get_db() as conn:
        item, batch, probe = pick_next(conn, labeler)
        if item is None:
            raise HTTPException(404, "No items to label right now")
        until = acquire_lock(conn, item["item_id"], labeler["id"], batch["id"], gold_probe=probe)
        if until is None:  # lost a race for the lock; the client just asks again
            raise HTTPException(409, "Item was just taken; request the next one")
        payload = blind_payload(conn, item, batch, labeler, until)
        # Keep the as-of date shown, so the annotation records exactly what the labeler saw
        conn.execute("UPDATE locks SET asof = COALESCE(asof, ?) WHERE item_id = ? AND labeler_id = ?",
                     (payload["asof"], item["item_id"], labeler["id"]))
        held = conn.execute("SELECT asof FROM locks WHERE item_id = ? AND labeler_id = ?",
                            (item["item_id"], labeler["id"])).fetchone()
        payload["asof"] = held["asof"]
        return payload


def _assignment(conn, item_id: str, labeler: dict):
    """
    The item, the open batch a submission belongs to, and whether it is a hidden
    gold probe, enforcing visibility, lock and eligibility.
    """
    item = fetch_visible_item(conn, item_id, labeler)
    lock = get_lock_status(conn, item_id)
    if lock and lock["labeler_id"] != labeler["id"]:
        raise HTTPException(409, _locked_message(lock, labeler))
    served = conn.execute("SELECT batch_id, gold_probe FROM locks WHERE item_id = ? AND labeler_id = ? AND until > ?",
                          (item_id, labeler["id"], iso(utcnow()))).fetchone()
    batches = _open_batches(conn)
    if served and served["gold_probe"]:
        for batch in batches:
            if batch["id"] == served["batch_id"] and not conn.execute(
                    "SELECT 1 FROM annotations WHERE item_id = ? AND labeler_id = ?",
                    (item_id, labeler["id"])).fetchone():
                return item, batch, True
    if served and served["batch_id"] is not None:
        batches = sorted(batches, key=lambda b: b["id"] != served["batch_id"])  # the serving batch first
    for batch in batches:
        if _eligible(conn, item_id, batch, labeler):
            return item, batch, False
    if conn.execute("SELECT 1 FROM annotations WHERE item_id = ? AND labeler_id = ?",
                    (item_id, labeler["id"])).fetchone():
        raise HTTPException(409, "You have already labelled or skipped this item")
    raise HTTPException(404, f"Item {item_id} is not in an open batch for you")


def guideline_in_force(batch) -> str:
    """FR-26: the batch can pin a guideline version; otherwise the current one."""
    from . import guideline

    return batch["guideline_version"] or guideline.version()


def _served_asof(conn, item, labeler_id: int) -> str:
    """The as-of date the labeler was shown (from the lock), else the item's own."""
    row = conn.execute("SELECT asof FROM locks WHERE item_id = ? AND labeler_id = ?",
                       (item["item_id"], labeler_id)).fetchone()
    return row["asof"] if row and row["asof"] else item_asof(item)


def _wall_ms(conn, item_id: str, labeler_id: int) -> Optional[int]:
    row = conn.execute("SELECT served_at FROM locks WHERE item_id = ? AND labeler_id = ?",
                       (item_id, labeler_id)).fetchone()
    if not row or not row["served_at"]:
        return None
    return int((utcnow() - _parse(row["served_at"])).total_seconds() * 1000)


@app.post("/api/annotations", tags=["Annotation"], summary="Submit labels for an item")
async def submit_annotation(body: AnnotationIn, labeler: dict = Depends(require_active)):
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
        item, batch, probe = _assignment(conn, body.item_id, labeler)
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
                                        policy_override, is_gold_probe, active_ms, wall_ms, guideline_version,
                                        app_version, asof)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["item_id"], batch["id"], labeler["id"], int(submission.answerable),
             json.dumps(reasons_json(submission.reasons, reason_set)), submission.note,
             int(submission.policy_override), int(probe), submission.active_ms,
             _wall_ms(conn, item["item_id"], labeler["id"]), guideline_in_force(batch), config.app_version(),
             _served_asof(conn, item, labeler["id"])),
        )
        annotation_id = cur.lastrowid
        for s in submission.spans:
            conn.execute(
                """INSERT INTO spans (annotation_id, side, option, pointer, start, "end", text, role, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (annotation_id, s.side, s.option, s.pointer, s.start, s.end, s.text, s.role, json.dumps(s.reasons)),
            )
        release_lock(conn, item["item_id"], labeler["id"])
        if probe:  # FR-30; no feedback, so the response looks like any other save
            check_auto_pause(conn, labeler)
    return {"status": "saved", "annotation_id": annotation_id, "version": 1,
            "policy_override": submission.policy_override}


# ============================================================================
# API Endpoints - History and edits (FR-21)
# ============================================================================

EDIT_WINDOW = 20


def _recent_submissions(conn, labeler: dict) -> list:
    """
    The labeler's last 20 submissions (first versions, skips excluded), newest
    first, with their latest version. Items their clearance no longer covers, or
    that their batch's filters now hide, are left out (FR-50; review §3.5).
    """
    firsts = conn.execute(
        """SELECT a.item_id, a.batch_id, a.id FROM annotations a
           WHERE a.labeler_id = ? AND a.version = 1 AND a.skipped_code IS NULL AND a.batch_id IS NOT NULL
           ORDER BY a.id DESC LIMIT ?""", (labeler["id"], EDIT_WINDOW)).fetchall()
    out = []
    for f in firsts:
        batch = conn.execute("SELECT * FROM batches WHERE id = ?", (f["batch_id"],)).fetchone()
        sql, params = _batch_filter(batch, labeler)
        if not conn.execute(f"SELECT 1 FROM items i WHERE i.item_id = ? AND {sql}", (f["item_id"], *params)).fetchone():
            continue
        latest = conn.execute(
            """SELECT * FROM annotations WHERE item_id = ? AND labeler_id = ? AND batch_id = ?
               ORDER BY version DESC LIMIT 1""", (f["item_id"], labeler["id"], f["batch_id"])).fetchone()
        out.append((latest, batch))
    return out


def _edit_target(conn, labeler: dict, annotation_id: int):
    """The latest version of one of this labeler's recent submissions, and its batch; else 404/409."""
    for latest, batch in _recent_submissions(conn, labeler):
        if conn.execute("SELECT 1 FROM annotations WHERE id = ? AND item_id = ? AND labeler_id = ? AND batch_id = ?",
                        (annotation_id, latest["item_id"], labeler["id"], batch["id"])).fetchone():
            if batch["status"] == "closed":
                raise HTTPException(409, "This batch is closed; its labels can't be edited")
            return latest, batch
    raise HTTPException(404, "Not one of your last 20 submissions")


def _stored_spans(conn, annotation_id: int) -> list:
    return [{"side": s["side"], "role": s["role"], "text": s["text"], "option": s["option"], "pointer": s["pointer"],
             "start": s["start"], "end": s["end"], "reasons": json.loads(s["reasons_json"])}
            for s in conn.execute("SELECT * FROM spans WHERE annotation_id = ? ORDER BY id", (annotation_id,))]


@app.get("/api/history", tags=["Annotation"], summary="My last 20 submissions")
async def history(labeler: dict = Depends(require_active)):
    """FR-21: what can still be edited. Gold probes are listed like everything else (blindness)."""
    with get_db() as conn:
        rows = _recent_submissions(conn, labeler)
        return {"submissions": [{
            "annotation_id": a["id"], "item_id": a["item_id"], "batch": f"batch {b['id']}", "version": a["version"],
            "answerable": bool(a["answerable"]), "reasons": [r for r, v in json.loads(a["reasons_json"]).items() if v],
            "created_at": a["created_at"], "editable": b["status"] != "closed",
        } for a, b in rows]}


@app.get("/api/history/{annotation_id}", tags=["Annotation"], summary="One past submission, for editing")
async def history_item(annotation_id: int, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        latest, batch = _edit_target(conn, labeler, annotation_id)
        item = fetch_visible_item(conn, latest["item_id"], labeler)
        payload = blind_payload(conn, item, batch, labeler, lock_until=None)
        payload["asof"] = latest["asof"] or payload["asof"]  # what they saw the first time
        payload["edit"] = {"annotation_id": latest["id"], "version": latest["version"],
                           "answerable": bool(latest["answerable"]),
                           "reasons": [r for r, v in json.loads(latest["reasons_json"]).items() if v],
                           "note": latest["note"], "spans": _stored_spans(conn, latest["id"])}
        return payload


@app.put("/api/annotations/{annotation_id}", tags=["Annotation"], summary="Edit one of my last 20 submissions")
async def edit_annotation(annotation_id: int, body: AnnotationIn, labeler: dict = Depends(require_active)):
    """
    FR-21: every edit is a new version; the old one is kept (NFR-6). α and the
    exports use the latest version. Allowed until the batch closes.
    """
    submission = Submission(
        answerable=body.answerable, reasons=body.reasons, note=body.note,
        spans=[Span(**s.model_dump()) for s in body.spans],
        policy_override=body.policy_override, active_ms=body.active_ms,
    )
    with get_db() as conn:
        latest, batch = _edit_target(conn, labeler, annotation_id)
        if body.item_id != latest["item_id"]:
            raise HTTPException(422, "item_id doesn't match the annotation")
        item = fetch_visible_item(conn, latest["item_id"], labeler)
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
        version = latest["version"] + 1
        cur = conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, answerable, reasons_json, note,
                                        policy_override, is_gold_probe, active_ms, guideline_version, app_version,
                                        asof, position_in_state_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["item_id"], batch["id"], labeler["id"], version, int(submission.answerable),
             json.dumps(reasons_json(submission.reasons, reason_set)), submission.note,
             int(submission.policy_override), latest["is_gold_probe"], submission.active_ms,
             guideline_in_force(batch), config.app_version(), latest["asof"], latest["position_in_state_run"]),
        )
        for s in submission.spans:
            conn.execute(
                """INSERT INTO spans (annotation_id, side, option, pointer, start, "end", text, role, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cur.lastrowid, s.side, s.option, s.pointer, s.start, s.end, s.text, s.role, json.dumps(s.reasons)),
            )
        if latest["is_gold_probe"]:
            check_auto_pause(conn, labeler)
    return {"status": "saved", "annotation_id": cur.lastrowid, "version": version,
            "policy_override": submission.policy_override}


@app.post("/api/skip", tags=["Annotation"], summary="Skip an item")
async def skip_item(body: SkipIn, labeler: dict = Depends(require_active)):
    """FR-20: skip with a reason code. The item is never served to this labeler again."""
    if body.code not in SKIP_CODES:
        raise HTTPException(422, f"code must be one of {', '.join(SKIP_CODES)}")
    with get_db() as conn:
        item, batch, probe = _assignment(conn, body.item_id, labeler)
        conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, skipped_code, note, is_gold_probe,
                                        wall_ms, guideline_version, app_version, asof)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (item["item_id"], batch["id"], labeler["id"], body.code, body.note, int(probe),
             _wall_ms(conn, item["item_id"], labeler["id"]), guideline_in_force(batch), config.app_version(),
             _served_asof(conn, item, labeler["id"])),
        )
        release_lock(conn, item["item_id"], labeler["id"])
    return {"status": "skipped", "item_id": body.item_id}


# ============================================================================
# API Endpoints - Batches
# ============================================================================

class BatchConfig(BaseModel):
    overlap_target: Optional[int] = Field(None, ge=1, description="Labelers per item (1 to n; 3 is ideal)")
    reliability_fraction: Optional[float] = Field(None, ge=0, le=1, description="Share of items given more labelers")
    reliability_overlap: Optional[int] = Field(None, ge=2, description="Labelers per item in the reliability subset")
    priority: Optional[int] = None
    tier_ceiling: Optional[str] = None
    require_note: Optional[bool] = None
    relabel_after_days: Optional[int] = Field(None, ge=0, description="Re-label batches: minimum gap in days")


class RelabelIn(BaseModel):
    name: str = Field(..., description="Name of the new re-label batch")
    fraction: Optional[float] = Field(None, ge=0, le=1, description="Sample of the source's items (default: its "
                                                                    "reliability subset, else all)")
    after_days: int = Field(7, ge=0)


@app.get("/api/admin/batches", tags=["Admin"], summary="List batches")
async def list_batches(admin: dict = Depends(require_admin)):
    with get_db() as conn:
        return {"batches": [batches_mod.describe(conn, b)
                            for b in conn.execute("SELECT * FROM batches ORDER BY id").fetchall()]}


@app.post("/api/admin/batches/{name}/config", tags=["Admin"], summary="Configure a batch")
async def configure_batch(name: str, body: BatchConfig, admin: dict = Depends(require_admin)):
    """Overlap, reliability subset, priority, tier ceiling, note rule (FR-31). Re-samples the subset."""
    with get_db() as conn:
        try:
            return batches_mod.configure(conn, name, admin["id"], **body.model_dump())
        except ValueError as e:
            raise HTTPException(422, str(e))


@app.post("/api/admin/batches/{name}/relabel", tags=["Admin"], summary="Create a re-label batch")
async def relabel_batch(name: str, body: RelabelIn, admin: dict = Depends(require_admin)):
    """Intra-rater pass: labelers get their own items from batch ``name`` again, blind, after a gap."""
    with get_db() as conn:
        try:
            return batches_mod.create_relabel(conn, name, body.name, fraction=body.fraction,
                                              after_days=body.after_days, actor_id=admin["id"])
        except ValueError as e:
            raise HTTPException(422, str(e))


@app.post("/api/admin/batches/{name}/status", tags=["Admin"], summary="Open or close a batch")
async def set_batch_status(name: str, status: str = Body(..., embed=True), admin: dict = Depends(require_admin)):
    with get_db() as conn:
        try:
            warnings = set_status(conn, name, status, admin["id"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    return {"name": name, "status": status, "warnings": warnings}


# ============================================================================
# API Endpoints - Agreement, gold, export (M1)
# ============================================================================

def _admin_filters(admin: dict, batch: Optional[list] = None, **kwargs):
    from .records import Filters

    # FR-57: an admin with public clearance sees and exports libre items only
    return Filters(batches=batch or (), clearance=admin["clearance"], **kwargs)


@app.get("/api/admin/agreement", tags=["Admin"], summary="Agreement report")
async def agreement_report(batch: Optional[list[str]] = Query(None), n_boot: int = Query(1000, ge=0, le=5000),
                           admin: dict = Depends(require_admin)):
    """Per-reason α with CI, prevalence and n; candidates vs established; intra-rater; human/model (FR-37/38/41)."""
    from .analysis import report
    from .quality import gold_accuracy
    from .records import agreement_inputs

    with get_db() as conn:
        data = agreement_inputs(conn, _admin_filters(admin, batch))
        # Live account state for the labeler panel: rolling gold accuracy and pauses (FR-30)
        status = {r["pseudonym"]: {"status": r["status"], "pause_reason": r["pause_reason"],
                                   **{k: v for k, v in gold_accuracy(conn, r["id"]).items()
                                      if k in ("rolling", "rolling_n", "below_threshold")}}
                  for r in conn.execute("SELECT * FROM labelers WHERE kind = 'human'")}
    out = report(data, n_boot=n_boot)
    out["labelers"]["status"] = status
    return out


class GoldIn(BaseModel):
    answerable: bool = False
    reasons: list[str] = Field(default_factory=list)
    spans: list[SpanIn] = Field(default_factory=list)
    alternatives: dict[str, list[str]] = Field(default_factory=dict,
                                               description="reason -> acceptable alternative reasons")
    explanation: Optional[str] = Field(None, max_length=4000)


class PromoteIn(BaseModel):
    labeler: str = Field(..., description="Pseudonym whose latest annotation becomes gold, e.g. L01")
    explanation: Optional[str] = Field(None, max_length=4000)


def _gold_call(fn):
    try:
        return fn()
    except SubmissionError as e:
        raise HTTPException(422, {"problems": e.problems})
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/admin/gold", tags=["Admin"], summary="List gold")
async def list_gold_items(include_retired: bool = False, admin: dict = Depends(require_admin)):
    from .gold import list_gold

    with get_db() as conn:
        items = list_gold(conn, include_retired, visible_to(admin["clearance"]))
    return {"gold": items, "count": len(items)}


@app.put("/api/admin/gold/{item_id:path}", tags=["Admin"], summary="Create or edit gold")
async def put_gold(item_id: str, body: GoldIn, admin: dict = Depends(require_admin)):
    """FR-29: gold reasons, spans, acceptable alternatives and an explanation. Spans are validated like labels."""
    from .gold import save_gold

    with get_db() as conn:
        fetch_visible_item(conn, item_id, admin)
        return _gold_call(lambda: save_gold(
            conn, item_id, answerable=body.answerable, reasons=body.reasons,
            spans=[s.model_dump() for s in body.spans], alternatives=body.alternatives,
            explanation=body.explanation, actor_id=admin["id"]))


@app.post("/api/admin/gold/{item_id:path}/promote", tags=["Admin"], summary="Promote an annotation to gold")
async def promote_gold(item_id: str, body: PromoteIn, admin: dict = Depends(require_admin)):
    from .gold import promote

    with get_db() as conn:
        fetch_visible_item(conn, item_id, admin)
        return _gold_call(lambda: promote(conn, item_id, body.labeler, explanation=body.explanation,
                                          actor_id=admin["id"]))


@app.post("/api/admin/gold/{item_id:path}/retire", tags=["Admin"], summary="Retire gold")
async def retire_gold_item(item_id: str, admin: dict = Depends(require_admin)):
    """Nothing is hard-deleted (NFR-6); a retired gold item can be saved again."""
    from .gold import retire_gold

    with get_db() as conn:
        fetch_visible_item(conn, item_id, admin)
        return _gold_call(lambda: retire_gold(conn, item_id, admin["id"]))


# ============================================================================
# API Endpoints - Adjudication (FR-43)
# ============================================================================

async def require_internal_admin(admin: dict = Depends(require_admin)) -> dict:
    """FR-43: adjudicators are internal admins."""
    if admin["clearance"] != "internal":
        raise HTTPException(403, "Adjudication needs internal clearance")
    return admin


class AdjudicationIn(BaseModel):
    answerable: bool = False
    reasons: list[str] = Field(default_factory=list)
    spans: list[SpanIn] = Field(default_factory=list)
    note: Optional[str] = Field(None, max_length=2000)
    promote: bool = Field(False, description="Also save the result as gold (FR-29)")
    explanation: Optional[str] = Field(None, max_length=4000, description="Gold explanation, when promoting")
    alternatives: dict[str, list[str]] = Field(default_factory=dict)


@app.get("/api/admin/adjudication", tags=["Admin"], summary="Adjudication queue")
async def adjudication_queue(batch: Optional[list[str]] = Query(None), include_done: bool = False,
                             admin: dict = Depends(require_internal_admin)):
    """Items whose labelers disagree on any reason or on answerable, most disagreements first."""
    from .adjudication import queue

    with get_db() as conn:
        items = queue(conn, _admin_filters(admin, batch), include_done)
    return {"items": items, "count": len(items)}


@app.get("/api/admin/adjudication/{item_id:path}", tags=["Admin"], summary="Labels side by side")
async def adjudication_detail(item_id: str, admin: dict = Depends(require_internal_admin)):
    """All labels of the item, anonymised as L-a, L-b, ..., with any earlier adjudication and gold."""
    from .adjudication import detail

    with get_db() as conn:
        fetch_visible_item(conn, item_id, admin)
        try:
            return detail(conn, item_id, _admin_filters(admin))
        except ValueError as e:
            raise HTTPException(404, str(e))


@app.post("/api/admin/adjudication/{item_id:path}", tags=["Admin"], summary="Save an adjudication")
async def adjudication_save(item_id: str, body: AdjudicationIn, admin: dict = Depends(require_internal_admin)):
    """Stored apart from the raw labels, as a new version; α never changes. Optionally promoted to gold."""
    from .adjudication import save

    with get_db() as conn:
        fetch_visible_item(conn, item_id, admin)
        return _gold_call(lambda: save(
            conn, item_id, admin, answerable=body.answerable, reasons=body.reasons,
            spans=[s.model_dump() for s in body.spans], note=body.note, promote=body.promote,
            explanation=body.explanation, alternatives=body.alternatives, filters=_admin_filters(admin)))


class ImportIn(BaseModel):
    filename: str = Field(..., max_length=200, description="Shown in the import log")
    content: str = Field(..., max_length=64 * 1024 * 1024, description="The JSONL text (pool format, §5.2)")
    batch: Optional[str] = Field(None, max_length=100, description="Add the items to this batch (created if new)")
    replace: bool = False


@app.post("/api/admin/import", tags=["Admin"], summary="Import pool JSONL")
async def admin_import(body: ImportIn, admin: dict = Depends(require_admin)):
    """
    FR-11 from the browser: the same importer as the CLI. Lowering a tier
    (``--allow-lower-tier``) stays CLI-only, for the owner.
    """
    import hashlib

    from .importer import import_rows

    data = body.content.encode("utf-8")
    with get_db() as conn:
        report = import_rows(conn, body.content.splitlines(), f"upload:{body.filename}",
                             hashlib.sha256(data).hexdigest(), batch=body.batch or None, replace=body.replace,
                             actor_id=admin["id"], actor=admin["pseudonym"])
    out = report.as_dict()
    out["errors"] = [{"line": n, "error": e} for n, e in out["errors"][:200]]
    return out


@app.post("/api/admin/backup", tags=["Admin"], summary="Back up the database now")
async def admin_backup(admin: dict = Depends(require_owner)):
    """NFR-6: an online backup to outputs/e13_labeler/backups/ (the server also does this nightly)."""
    from .backup import backup

    result = backup()
    with get_db() as conn:
        audit(conn, admin["id"], "backup", result["path"], {"sha256": result["sha256"]})
    return result


class ExportIn(BaseModel):
    kinds: list[str] = Field(default_factory=lambda: ["annotations", "training", "agreement", "items"])
    batches: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    since: Optional[str] = None
    until: Optional[str] = None
    include_models: bool = True
    include_gold: bool = False
    text_included: bool = True


@app.post("/api/admin/export", tags=["Admin"], summary="Write an export run")
async def run_export(body: ExportIn, admin: dict = Depends(require_admin)):
    """FR-45..49: writes outputs/e13_labeler/exports/<timestamp>/ on the server and returns its manifest."""
    from .exports import write_export

    with get_db() as conn:
        try:
            return write_export(conn, kinds=body.kinds, text_included=body.text_included, actor_id=admin["id"],
                                filters=_admin_filters(admin, body.batches, permissions=body.permissions,
                                                       since=body.since, until=body.until,
                                                       include_models=body.include_models,
                                                       include_gold=body.include_gold))
        except ValueError as e:
            raise HTTPException(422, str(e))
