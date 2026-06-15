"""Manual application tracking: mark/unmark a job-list position as applied.

Phase 1 is the dashboard's "Mark applied" toggle. The same ``applications`` rows
are what the phase 2/3 auto-apply will create and advance, so this is the single
source of truth for "has this user applied to this position".

A user may only mark a position they can actually see — i.e. one already scored
for them (a MatchResult exists), which is exactly the set shown in their job
list. That ties the action to the visible list and avoids recording applications
against arbitrary/foreign position ids."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Application, MatchResult, User
from ..schemas import ApplicationOut

router = APIRouter(prefix="/api/applications", tags=["applications"])


def _require_visible(db: Session, user: User, position_id: int) -> None:
    """404 unless this position is in the user's job list (has been scored for
    them). Also serves as the position-exists check."""
    seen = db.scalar(
        select(MatchResult.id)
        .where(MatchResult.user_id == user.id, MatchResult.position_id == position_id)
        .limit(1)
    )
    if not seen:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not in your job list")


def _get(db: Session, user: User, position_id: int) -> Application | None:
    return db.scalar(
        select(Application).where(
            Application.user_id == user.id, Application.position_id == position_id
        )
    )


@router.get("", response_model=list[ApplicationOut])
def list_applications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(Application).where(Application.user_id == user.id)))


@router.post("/{position_id}", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
def mark_applied(
    position_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a position applied. Idempotent: re-marking returns the existing row."""
    _require_visible(db, user, position_id)
    application = _get(db, user, position_id)
    if application is None:
        application = Application(user_id=user.id, position_id=position_id)
        db.add(application)
        db.commit()
        db.refresh(application)
    return application


@router.delete("/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
def unmark_applied(
    position_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo a "mark applied". Idempotent: 204 whether or not a row existed."""
    application = _get(db, user, position_id)
    if application is not None:
        db.delete(application)
        db.commit()
