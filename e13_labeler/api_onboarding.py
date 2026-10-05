"""
Onboarding endpoints: the guideline (FR-26) and the quiz (FR-27), which is also
the retraining quiz after an auto-pause (FR-30).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import guideline, quiz
from .auth import require_agreement
from .db import get_db

router = APIRouter()


def _call(fn):
    try:
        return fn()
    except quiz.QuizError as e:
        raise HTTPException(e.status, str(e))


@router.get("/api/onboarding", tags=["Onboarding"], summary="Where this account stands")
async def onboarding(labeler: dict = Depends(require_agreement)):
    """next: guideline | quiz | label | wait (paused for review)."""
    with get_db() as conn:
        return quiz.stage(conn, labeler)


@router.get("/api/guideline", tags=["Onboarding"], summary="The labelling guideline")
async def get_guideline(labeler: dict = Depends(require_agreement)):
    """Opening it is recorded: the quiz needs a view of the current version (FR-26)."""
    with get_db() as conn:
        guideline.record_view(conn, labeler["id"])
    return guideline.page()


@router.post("/api/quiz/start", tags=["Onboarding"], summary="Start (or resume) the quiz")
async def start_quiz(labeler: dict = Depends(require_agreement)):
    with get_db() as conn:
        return _call(lambda: quiz.start(conn, labeler))


@router.get("/api/quiz", tags=["Onboarding"], summary="The current quiz question, or the result")
async def get_quiz(labeler: dict = Depends(require_agreement)):
    with get_db() as conn:
        return _call(lambda: quiz.current(conn, labeler))


class QuizSpan(BaseModel):
    side: str
    role: str
    text: str
    option: Optional[str] = None
    pointer: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    reasons: list[str] = Field(default_factory=list)


class QuizAnswer(BaseModel):
    item_id: str
    answerable: bool = False
    reasons: list[str] = Field(default_factory=list)
    spans: list[QuizSpan] = Field(default_factory=list)


@router.post("/api/quiz/answer", tags=["Onboarding"], summary="Answer the current question")
async def answer_quiz(body: QuizAnswer, labeler: dict = Depends(require_agreement)):
    """Immediate feedback: the gold reasons, spans and explanation (FR-27). The last answer carries the result."""
    with get_db() as conn:
        return _call(lambda: quiz.answer(conn, labeler, body.item_id, body.answerable, body.reasons,
                                         [s.model_dump() for s in body.spans]))
