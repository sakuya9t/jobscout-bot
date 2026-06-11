"""Register / login / logout / me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import (
    create_access_token,
    get_current_user,
    hash_password,
    new_link_code,
    verify_password,
)
from ..config import settings
from ..db import get_db
from ..models import User
from ..schemas import Credentials, Token, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "access_token"


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure,
        # Match the JWT lifetime so the cookie doesn't outlive (or under-live) the
        # token it carries.
        max_age=settings.jwt_expire_minutes * 60, path="/",
    )


@router.post("/register", response_model=Token)
def register(creds: Credentials, response: Response, db: Session = Depends(get_db)) -> Token:
    user = User(
        # Normalize so "A@b.com" and "a@b.com" can't become two accounts
        # (EmailStr only lowercases the domain half).
        email=creds.email.lower(),
        hashed_password=hash_password(creds.password),
        telegram_link_code=new_link_code(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Lost the race (or a plain duplicate) — the unique email constraint is
        # the source of truth, so a check-then-insert TOCTOU can't 500 here.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    db.refresh(user)
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return Token(access_token=token)


@router.post("/login", response_model=Token)
def login(creds: Credentials, response: Response, db: Session = Depends(get_db)) -> Token:
    user = db.scalar(select(User).where(User.email == creds.email.lower()))
    if not user or not verify_password(creds.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return Token(access_token=token)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/telegram-code", response_model=UserOut)
def regenerate_telegram_code(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> User:
    """Mint a fresh one-time Telegram link code. Needed because the old code is
    burned on a successful /start link, so re-linking requires a new one."""
    user.telegram_link_code = new_link_code()
    db.commit()
    db.refresh(user)
    return user
