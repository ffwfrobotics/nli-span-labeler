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
from .db import audit, get_db, init_db
from .tiers import visible_tiers

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
    {"name": "Annotation", "description": "Flagging and (soon) labelling items."},
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
        """INSERT INTO locks (item_id, labeler_id, until) VALUES (?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET labeler_id = excluded.labeler_id, until = excluded.until
           WHERE locks.labeler_id = excluded.labeler_id OR locks.until <= ?""",
        (item_id, labeler_id, until, iso(now)),
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
