"""CRUD for a user's interest/requirement profiles."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..logging_config import get_logger
from ..models import Interest, MatchResult, User
from ..schemas import InterestIn, InterestOut, InterestUpdate
from ..services import evaluator

router = APIRouter(prefix="/api/interests", tags=["interests"])

log = get_logger(__name__)

# Fields that change *what the LLM matches against*; editing any of them must
# invalidate this interest's existing matches so the next run re-evaluates.
_SCORING_FIELDS = {"title_keywords", "locations", "seniority", "employment_type", "exclude_keywords", "notes"}


def _owned(db: Session, user: User, interest_id: int) -> Interest:
    interest = db.get(Interest, interest_id)
    if not interest or interest.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interest not found")
    return interest


@router.get("", response_model=list[InterestOut])
def list_interests(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(Interest).where(Interest.user_id == user.id)))


@router.post("", response_model=InterestOut, status_code=status.HTTP_201_CREATED)
def add_interest(payload: InterestIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    interest = Interest(user_id=user.id, **payload.model_dump())
    db.add(interest)
    db.commit()
    db.refresh(interest)
    return interest


@router.patch("/{interest_id}", response_model=InterestOut)
def update_interest(
    interest_id: int,
    payload: InterestUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    interest = _owned(db, user, interest_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(interest, field, value)
    criteria_changed = bool(_SCORING_FIELDS & changes.keys())
    if criteria_changed:
        # Matching criteria changed: drop this interest's matches (incl. cheap-filter
        # rejections) so every posting is re-screened against the new criteria instead
        # of being skipped as "already scored".
        db.execute(
            delete(MatchResult).where(
                MatchResult.user_id == user.id, MatchResult.interest_id == interest_id
            )
        )
    db.commit()
    db.refresh(interest)
    if criteria_changed:
        # Kick the re-evaluation now (don't wait for the next manual scan/cron): the
        # just-deleted pairs are back in the backlog, so a drain re-screens every
        # posting against the new filters — newly-eligible roles gain a score and
        # newly-ineligible ones flip to "not a match". Best-effort: the scheduled
        # scan is the backstop if this kick can't run (e.g. serverless), so never let
        # it fail the save.
        try:
            evaluator.ensure_running(user.id)
        except Exception:
            log.exception("interest update: failed to kick re-evaluation for user %s", user.id)
    return interest


@router.delete("/{interest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_interest(interest_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(_owned(db, user, interest_id))
    db.commit()
