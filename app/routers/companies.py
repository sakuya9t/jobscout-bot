"""CRUD for the per-user company watch-list."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..company_presets import PRESETS
from ..db import get_db
from ..models import Company, Subscription, User
from ..schemas import CompanyIn, CompanyOut, CompanyPresetOut, CompanyUpdate

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("/presets", response_model=list[CompanyPresetOut])
def list_presets(user: User = Depends(get_current_user)):
    """Built-in popular companies the dashboard offers as one-click form fills.
    Static data; auth-gated only so nothing is exposed before login."""
    return PRESETS


def _owned_custom(db: Session, user: User, company_id: int) -> Company:
    """Fetch a custom company owned by the user (presets aren't editable here)."""
    company = db.get(Company, company_id)
    if not company or company.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


def _detect_preset_key(payload: CompanyIn) -> str | None:
    """Resolve a preset for an add request: explicit ``preset_key`` wins, else match
    the payload to a known preset by ATS token or name (so the dashboard's preset
    fill subscribes to the shared company instead of creating a duplicate)."""
    if payload.preset_key:
        return payload.preset_key
    for p in PRESETS:
        if p.ats_token and (p.ats_type, p.ats_token) == (payload.ats_type, payload.ats_token):
            return p.key
        if p.name.lower() == (payload.name or "").lower():
            return p.key
    return None


@router.get("", response_model=list[CompanyOut])
def list_companies(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The user's watch-list: their own custom companies + the preset companies they
    subscribe to (merged, name-sorted)."""
    custom = db.scalars(select(Company).where(Company.user_id == user.id))
    subscribed = db.scalars(
        select(Company)
        .join(Subscription, Subscription.company_id == Company.id)
        .where(Subscription.user_id == user.id)
    )
    return sorted([*custom, *subscribed], key=lambda c: c.name.lower())


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def add_company(payload: CompanyIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    preset_key = _detect_preset_key(payload)
    if preset_key:
        # Subscribe to the shared global preset company (idempotent per user).
        company = db.scalar(select(Company).where(Company.preset_key == preset_key))
        if company is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown preset company")
        if db.scalar(select(Subscription).where(
            Subscription.user_id == user.id, Subscription.company_id == company.id
        )):
            raise HTTPException(status.HTTP_409_CONFLICT, "Company already on your list")
        db.add(Subscription(user_id=user.id, company_id=company.id))
        db.commit()
        return company

    # Custom company: created per-user as before.
    if db.scalar(select(Company).where(Company.user_id == user.id, Company.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Company already on your list")
    company = Company(user_id=user.id, **payload.model_dump(exclude={"preset_key"}))
    db.add(company)
    try:
        db.commit()
    except IntegrityError:
        # Lost a concurrent-insert race past the pre-check; the unique
        # (user_id, name) constraint is the source of truth (cf. register).
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Company already on your list")
    db.refresh(company)
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: int,
    payload: CompanyUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    company = _owned_custom(db, user, company_id)  # presets are global — not editable
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(company_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    if company.is_preset:
        # Unsubscribe from the shared preset; never delete the catalog row itself.
        sub = db.scalar(select(Subscription).where(
            Subscription.user_id == user.id, Subscription.company_id == company.id
        ))
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
        db.delete(sub)
        db.commit()
        return
    if company.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    db.delete(company)
    db.commit()
