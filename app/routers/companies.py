"""CRUD for the per-user company watch-list."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Company, User
from ..schemas import CompanyIn, CompanyOut, CompanyUpdate

router = APIRouter(prefix="/api/companies", tags=["companies"])


def _owned(db: Session, user: User, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if not company or company.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


@router.get("", response_model=list[CompanyOut])
def list_companies(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(Company).where(Company.user_id == user.id).order_by(Company.name)))


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def add_company(payload: CompanyIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if db.scalar(select(Company).where(Company.user_id == user.id, Company.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Company already on your list")
    company = Company(user_id=user.id, **payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: int,
    payload: CompanyUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    company = _owned(db, user, company_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(company_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(_owned(db, user, company_id))
    db.commit()
