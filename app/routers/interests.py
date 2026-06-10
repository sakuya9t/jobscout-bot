"""CRUD for a user's interest/requirement profiles."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Interest, User
from ..schemas import InterestIn, InterestOut, InterestUpdate

router = APIRouter(prefix="/api/interests", tags=["interests"])


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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(interest, field, value)
    db.commit()
    db.refresh(interest)
    return interest


@router.delete("/{interest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_interest(interest_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(_owned(db, user, interest_id))
    db.commit()
