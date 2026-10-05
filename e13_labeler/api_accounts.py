"""
Account endpoints for M2: invites and registration (FR-51), labeler management
and password resets (FR-52), and the contributor agreement (FR-60).
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import accounts, config, contributor
from .auth import create_session, get_current_labeler, labeler_dict, require_admin
from .db import get_db
from .ratelimit import limiter

router = APIRouter()


def set_session_cookies(response: Response, token: str) -> None:
    """Session cookie (HttpOnly) plus the CSRF token JS must echo (readable, NFR-5)."""
    from .auth import CSRF_COOKIE, csrf_token

    max_age = config.SESSION_EXPIRY_DAYS * 24 * 60 * 60
    response.set_cookie(config.SESSION_COOKIE, token, httponly=True, secure=config.cookie_secure(),
                        samesite="strict", max_age=max_age)
    response.set_cookie(CSRF_COOKIE, csrf_token(token), httponly=False, secure=config.cookie_secure(),
                        samesite="strict", max_age=max_age)


def _call(fn):
    try:
        return fn()
    except accounts.AccountError as e:
        raise HTTPException(e.status, str(e))


# ============================================================================
# Registration and resets (no session needed)
# ============================================================================

class RegisterIn(BaseModel):
    token: str = ""  # missing answers 403 like a bad one (FR-51), not 422
    login_name: str
    password: str


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/api/auth/register", tags=["Authentication"], summary="Register with an invite")
@limiter.limit(config.RATE_LIMIT_AUTH)
async def register(request: Request, body: RegisterIn, response: Response):
    """FR-51: accounts are created by invite only. No valid, unused, unexpired token: 403."""
    if config.single_user():
        raise HTTPException(403, "SINGLE_USER mode has no other accounts")
    with get_db() as conn:
        labeler = _call(lambda: accounts.register(conn, body.token, body.login_name, body.password))
    set_session_cookies(response, create_session(labeler["id"]))
    return {"status": "registered", "pseudonym": labeler["pseudonym"]}


@router.post("/api/auth/reset", tags=["Authentication"], summary="Set a new password with a reset link")
@limiter.limit(config.RATE_LIMIT_AUTH)
async def reset(request: Request, body: ResetIn):
    with get_db() as conn:
        return {"status": "password_set", **_call(lambda: accounts.reset_password(conn, body.token, body.password))}


# ============================================================================
# Contributor agreement (FR-60)
# ============================================================================

@router.get("/api/agreement", tags=["Authentication"], summary="Contributor agreement")
async def get_agreement(labeler: dict = Depends(get_current_labeler)):
    return {"version": contributor.VERSION, "text": contributor.TEXT,
            "accepted_version": labeler.get("agreement_version")}


@router.post("/api/agreement", tags=["Authentication"], summary="Accept the contributor agreement")
async def accept_agreement(version: str = Body(..., embed=True), labeler: dict = Depends(get_current_labeler)):
    with get_db() as conn:
        return _call(lambda: accounts.accept_agreement(conn, labeler, version))


# ============================================================================
# Admin: invites and labelers (FR-51, FR-52)
# ============================================================================

class InviteIn(BaseModel):
    role: str = Field("labeler", description="labeler or admin (admin: owner only)")
    clearance: str = Field("public", description="public or internal (internal: owner only)")


@router.post("/api/admin/invites", tags=["Admin"], summary="Create an invite link")
async def create_invite(body: InviteIn, admin: dict = Depends(require_admin)):
    """Single use, expires in 7 days. The token is returned once and stored hashed."""
    with get_db() as conn:
        return _call(lambda: accounts.create_invite(conn, admin, body.role, body.clearance))


@router.get("/api/admin/invites", tags=["Admin"], summary="List invites and reset links")
async def list_invites(admin: dict = Depends(require_admin)):
    with get_db() as conn:
        return {"invites": accounts.list_invites(conn)}


@router.post("/api/admin/invites/{invite_id}/revoke", tags=["Admin"], summary="Expire an open invite")
async def revoke_invite(invite_id: str, admin: dict = Depends(require_admin)):
    with get_db() as conn:
        _call(lambda: accounts.revoke_invite(conn, admin, invite_id))
    return {"status": "revoked", "id": invite_id}


class StatusIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)
    skip_quiz: bool = Field(False, description="resume: owner only, activate without a passed quiz")


@router.post("/api/admin/labelers/{pseudonym}/clearance", tags=["Admin"], summary="Change clearance (owner)")
async def labeler_clearance(pseudonym: str, clearance: str = Body(..., embed=True),
                            admin: dict = Depends(require_admin)):
    with get_db() as conn:
        return _call(lambda: accounts.set_clearance(conn, admin, pseudonym, clearance))


@router.post("/api/admin/labelers/{pseudonym}/{action}", tags=["Admin"], summary="Pause, resume or revoke")
async def labeler_action(pseudonym: str, action: str, body: StatusIn = StatusIn(),
                         admin: dict = Depends(require_admin)):
    """Revoking ends every session at once (FR-52)."""
    if action == "reset":
        with get_db() as conn:
            return _call(lambda: accounts.create_reset(conn, admin, pseudonym))
    if action not in ("pause", "resume", "revoke"):
        raise HTTPException(404, "unknown action")
    with get_db() as conn:
        return _call(lambda: accounts.set_status(conn, admin, pseudonym, action, reason=body.reason,
                                                 skip_quiz=body.skip_quiz))


@router.get("/api/admin/labelers/{pseudonym}/stats", tags=["Admin"], summary="Per-labeler stats")
async def labeler_stats(pseudonym: str, admin: dict = Depends(require_admin)):
    from . import quality

    with get_db() as conn:
        row = _call(lambda: accounts._labeler(conn, pseudonym))
        return {**labeler_dict(row), **accounts.labeler_stats(conn, row["id"]),
                "gold": quality.gold_accuracy(conn, row["id"])}
