"""Read-only access to scraped positions (scoped to the user's companies)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Company, Position, User
from ..schemas import PositionOut

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
