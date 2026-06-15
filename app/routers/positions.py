"""Read-only access to scraped positions (scoped to the user's companies), plus
the per-position detail page API: a position's detail payload and its on-demand,
cached "application kit" (role summary + open-question advice + cover letter +
tailored resume), generated in the background by ``services/kit_worker``."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import ApplicationKit, Company, Position, Resume, User
from ..schemas import (
    ApplicationKitOut,
    OpenQuestionOut,
    PositionDetailOut,
    PositionOut,
)
from ..services import kit_worker, kits, reporter

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
def list_positions(
    company_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    company_ids = [c for c in db.scalars(select(Company.id).where(Company.user_id == user.id))]
    if company_id is not None:
        company_ids = [c for c in company_ids if c == company_id]
    if not company_ids:
        return []
    return list(
        db.scalars(
            select(Position)
            .where(Position.company_id.in_(company_ids))
            .order_by(Position.first_seen_at.desc())
        )
    )


def _require_visible_position(db: Session, user: User, position_id: int) -> Position:
    """404 unless this position is in the user's job list (scored for them). Returns
    the Position so callers don't re-fetch it."""
    if not reporter.position_visible(db, user, position_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not in your job list")
    position = db.get(Position, position_id)
    if position is None:  # defensive: visibility implies it exists
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    return position


def _kit_out(kit: ApplicationKit | None) -> ApplicationKitOut | None:
    """Serialize a stored kit row (JSON text columns -> typed lists) for the API."""
    if kit is None:
        return None
    return ApplicationKitOut(
        status=kit.status,
        looking_for=_loads_list(kit.looking_for),
        open_questions=[OpenQuestionOut(**q) for q in _loads_dicts(kit.open_questions)],
        cover_letter=kit.cover_letter,
        revised_resume=kit.revised_resume,
        resume_optimization=kit.resume_optimization,
        model=kit.model,
        error_detail=kit.error_detail,
        updated_at=kit.updated_at,
    )


def _loads_list(value: str | None) -> list[str]:
    try:
        data = json.loads(value) if value else []
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _loads_dicts(value: str | None) -> list[dict]:
    try:
        data = json.loads(value) if value else []
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _get_kit(db: Session, user: User, position_id: int) -> ApplicationKit | None:
    return db.scalar(
        select(ApplicationKit).where(
            ApplicationKit.user_id == user.id, ApplicationKit.position_id == position_id
        )
    )


@router.get("/{position_id}/detail", response_model=PositionDetailOut)
def position_detail(
    position_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The detail page payload: posting fields + the user's best stored match
    (score/win/strengths/gaps) + live applied status + the cached kit (or null).
    Read-only — never triggers generation."""
    _require_visible_position(db, user, position_id)
    detail = reporter.build_position_detail(db, user, position_id)
    if detail is None:  # consistent with the visibility gate
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not in your job list")
    return PositionDetailOut(**detail, kit=_kit_out(_get_kit(db, user, position_id)))


@router.get("/{position_id}/kit", response_model=ApplicationKitOut)
def get_kit(
    position_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current kit for polling. 404 until one has been requested. Read-only."""
    _require_visible_position(db, user, position_id)
    kit = _get_kit(db, user, position_id)
    if kit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No application kit generated yet")
    return _kit_out(kit)


@router.post("/{position_id}/kit", response_model=ApplicationKitOut, status_code=status.HTTP_202_ACCEPTED)
def generate_kit(
    position_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate (or regenerate) the application kit. Marks the row 'generating',
    hands the LLM work to the background worker, and returns immediately; the page
    polls ``GET .../kit``. Re-posting while one is in flight is a no-op restart."""
    position = _require_visible_position(db, user, position_id)
    resume = db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id, Resume.is_active == True)  # noqa: E712
        .order_by(Resume.created_at.desc())
    )
    if resume is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Upload a resume before generating an application kit.",
        )
    kit = kits.mark_generating(db, user, position, resume)
    db.commit()
    kit_worker.ensure_generating(user.id, position_id)
    return _kit_out(kit)
