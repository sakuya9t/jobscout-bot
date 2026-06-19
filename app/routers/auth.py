"""Register / login / logout / me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import invites, ratelimit
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
from ..schemas import (
    Credentials,
    PasswordChange,
    RegisterCredentials,
    Token,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "access_token"


def _rl_login(request: Request) -> None:
    """Throttle login attempts per IP to blunt credential-stuffing. Reads the limit
    from settings at request time so it tracks config (and test overrides)."""
    ratelimit.enforce(
        request, scope="login", limit=settings.rate_limit_auth_per_minute, window_s=60
    )


def _rl_register(request: Request) -> None:
    """Throttle signups per IP — also caps invite-code guessing on this route."""
    ratelimit.enforce(
        request, scope="register", limit=settings.rate_limit_register_per_hour, window_s=3600
    )


def _rl_change_password(request: Request) -> None:
    """Throttle change-password per IP to blunt brute-forcing the current password
    (an attacker on a stolen session trying to take over the account). Its own scope,
    so it never eats into a legitimate user's login budget."""
    ratelimit.enforce(
        request, scope="change_password", limit=settings.rate_limit_auth_per_minute, window_s=60
    )


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure,
        # Match the JWT lifetime so the cookie doesn't outlive (or under-live) the
        # token it carries.
        max_age=settings.jwt_expire_minutes * 60, path="/",
    )


@router.post("/register", response_model=Token, dependencies=[Depends(_rl_register)])
def register(
    creds: RegisterCredentials, response: Response, db: Session = Depends(get_db)
) -> Token:
    # Reserve an invite use *inside* this transaction: if the user insert below fails
    # (e.g. duplicate email), the rollback also undoes the increment, so a failed
    # signup never burns a code. A generic 403 avoids an oracle for unknown vs
    # expired vs exhausted codes — invite-code guessing is also rate-limited above.
    if settings.require_invite and not invites.redeem(db, creds.invite_code):
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired invitation code")
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
        # the source of truth, so a check-then-insert TOCTOU can't 500 here. The
        # rollback also releases the invite use reserved above.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    db.refresh(user)
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return Token(access_token=token)


@router.post("/login", response_model=Token, dependencies=[Depends(_rl_login)])
def login(creds: Credentials, response: Response, db: Session = Depends(get_db)) -> Token:
    user = db.scalar(select(User).where(User.email == creds.email.lower()))
    if not user or not verify_password(creds.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return Token(access_token=token)


@router.post("/change-password", dependencies=[Depends(_rl_change_password)])
def change_password(
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Change the logged-in user's password. The current password must be re-supplied
    and verified (so a stolen-but-idle session can't silently rotate the password),
    and the new one must differ and meet the registration complexity rule. The session
    cookie/JWT stays valid — it carries no password, so nothing to re-issue."""
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    if verify_password(payload.new_password, user.hashed_password):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "New password must be different from the current one"
        )
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


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
