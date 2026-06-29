"""Password hashing + JWT session tokens, and FastAPI auth dependencies."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

# bcrypt hashes only the first 72 bytes; longer passwords must be truncated to
# the same byte boundary on both hash and verify or verification silently fails.
_BCRYPT_MAX_BYTES = 72


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def new_link_code() -> str:
    """Short code a user pastes to /start in the Telegram bot."""
    return secrets.token_hex(4)


def new_temp_password() -> str:
    """A single-use temporary password for the forgot-password flow. URL-safe so it's
    easy to copy out of a Telegram message; ~12 chars of entropy is plenty for a
    credential that lives at most a few minutes and is invalidated on first use."""
    return secrets.token_urlsafe(9)


def _extract_token(authorization: str | None, access_token: str | None) -> str | None:
    """Accept either a Bearer header (API/MCP clients) or a cookie (browser)."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return access_token


def _resolve_user(
    authorization: str | None, access_token: str | None, db: Session
) -> User:
    token = _extract_token(authorization, access_token)
    user_id = decode_token(token) if token else None
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


def get_current_user(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_user(authorization, access_token, db)
    # A user who logged in with a temporary password (forgot-password flow) is held at
    # the door until they pick a real one: every protected route depends on this, so the
    # whole app is gated by the one check. The bypass routes (/me, /set-new-password,
    # /logout) use get_user_for_password_change instead so the user can read their state
    # and clear the flag. The 403 detail is a stable code the SPA/login JS keys off to
    # redirect to the set-new-password screen.
    if user.must_change_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "password_change_required")
    return user


def get_user_for_password_change(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Like get_current_user but WITHOUT the must-change-password gate — for the handful
    of routes that must stay reachable while the flag is set (so it doesn't deadlock the
    very endpoint that clears it)."""
    return _resolve_user(authorization, access_token, db)


def get_optional_user(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """Like get_current_user but returns None instead of raising — for pages
    that render differently when logged out."""
    token = _extract_token(authorization, access_token)
    user_id = decode_token(token) if token else None
    return db.get(User, user_id) if user_id else None


def authenticate_token(token: str, db: Session) -> User | None:
    """Resolve a raw bearer token to a user — used by the MCP server."""
    user_id = decode_token(token)
    return db.get(User, user_id) if user_id else None
