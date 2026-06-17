"""CRUD for the per-user company watch-list."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..company_presets import PRESETS, PRESETS_BY_KEY
from ..crypto import decrypt, encrypt
from ..db import get_db
from ..models import Company, CompanyAccount, Subscription, User
from ..schemas import (
    CompanyAccountIn,
    CompanyDetailOut,
    CompanyIn,
    CompanyOut,
    CompanyPresetOut,
    CompanyUpdate,
)

router = APIRouter(prefix="/api/companies", tags=["companies"])


def _requires_account(company: Company) -> bool:
    """Whether applying to this company needs a registered portal account — a
    property of its preset; always False for custom (non-preset) companies."""
    preset = PRESETS_BY_KEY.get(company.preset_key) if company.preset_key else None
    return bool(preset and preset.requires_account)


def _visible_company(db: Session, user: User, company_id: int) -> Company:
    """Fetch a company on the user's watch-list: a custom company they own, or a
    preset company they subscribe to. 404 for anything else (incl. presets they
    don't follow), so the detail page is scoped exactly to the user's list."""
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    if company.is_preset:
        followed = db.scalar(select(Subscription.id).where(
            Subscription.user_id == user.id, Subscription.company_id == company.id
        ))
        if not followed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    elif company.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


def _get_account(db: Session, user: User, company_id: int) -> CompanyAccount | None:
    return db.scalar(select(CompanyAccount).where(
        CompanyAccount.user_id == user.id, CompanyAccount.company_id == company_id
    ))


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
    subscribe to (merged, name-sorted). Each row carries whether it needs an
    application account and whether the user has attached one (drives the tags)."""
    custom = db.scalars(select(Company).where(Company.user_id == user.id))
    subscribed = db.scalars(
        select(Company)
        .join(Subscription, Subscription.company_id == Company.id)
        .where(Subscription.user_id == user.id)
    )
    companies = sorted([*custom, *subscribed], key=lambda c: c.name.lower())
    # Company ids the user has an account attached for (a username is saved).
    attached = set(db.scalars(select(CompanyAccount.company_id).where(
        CompanyAccount.user_id == user.id, CompanyAccount.username_enc.is_not(None)
    )))
    out: list[CompanyOut] = []
    for c in companies:
        row = CompanyOut.model_validate(c)
        row.requires_account = _requires_account(c)
        row.account_attached = c.id in attached
        out.append(row)
    return out


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


def _detail(db: Session, user: User, company: Company) -> CompanyDetailOut:
    """Build the detail payload: the company + this user's account state. The
    password is reported as a flag only; the username (an identifier) is decrypted
    so the form prefills. The portal URL falls back to the preset default."""
    out = CompanyDetailOut.model_validate(company)
    out.requires_account = _requires_account(company)
    preset = PRESETS_BY_KEY.get(company.preset_key) if company.preset_key else None
    account = _get_account(db, user, company.id)
    username = decrypt(account.username_enc) if account else None
    out.account_username = username
    out.account_attached = bool(username)
    out.account_has_password = bool(account and account.password_enc)
    out.account_notes = account.notes if account else None
    out.account_portal_url = (
        (account.portal_url if account and account.portal_url else None)
        or (preset.account_portal_url if preset else None)
    )
    return out


@router.get("/{company_id}/detail", response_model=CompanyDetailOut)
def company_detail(
    company_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The company watch-list detail page data, scoped to the user's list."""
    company = _visible_company(db, user, company_id)
    return _detail(db, user, company)


@router.put("/{company_id}/account", response_model=CompanyDetailOut)
def save_account(
    company_id: int,
    payload: CompanyAccountIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the user's application-portal account for a company. Only allowed for
    preset companies that require an account; the username/password are encrypted
    before storage. The password follows keep-blank semantics (blank keeps the
    saved one); the username is set as-typed (blank clears it)."""
    company = _visible_company(db, user, company_id)
    if not _requires_account(company):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This company doesn't require an application account.",
        )
    account = _get_account(db, user, company.id)
    if account is None:
        account = CompanyAccount(user_id=user.id, company_id=company.id)
        db.add(account)
    account.username_enc = encrypt(payload.username)  # blank -> None (clears it)
    if payload.password is not None:  # blank -> keep the saved password
        account.password_enc = encrypt(payload.password)
    account.portal_url = payload.portal_url
    account.notes = payload.notes
    db.commit()
    return _detail(db, user, company)


@router.delete("/{company_id}/account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    company_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Remove the user's saved account for a company. Idempotent (204 either way)."""
    company = _visible_company(db, user, company_id)
    account = _get_account(db, user, company.id)
    if account is not None:
        db.delete(account)
        db.commit()
