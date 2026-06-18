"""Invitation codes for gated registration.

The *plaintext* code is a random token shown once at mint time and never stored.
What lands in the DB is ``code_hash`` = HMAC-SHA256(``root_secret``, code), where
``root_secret`` derives from ``JOBSCOUT_SECRET_KEY`` (the same env secret that signs
sessions and keys ``app/crypto.py``) — so there is no extra key to manage. The key
never appears in a code or a row: a DB leak yields only irreversible hashes, which an
attacker can neither redeem (they can't recover the code) nor extend (they can't mint
new valid codes without the key). Expiry and use-count live as columns and are enforced
atomically at redeem time (see :func:`redeem`).
"""
from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import update
from sqlalchemy.orm import Session

from .config import settings
from .models import InviteCode
from .timeutil import utcnow

# The random part of a code is drawn from an unambiguous uppercase alphabet: no
# URL-safe-base64 punctuation (-, _) and no easy-to-confuse glyphs (0/O, 1/I/L), so
# codes read cleanly and transcribe without errors. 14 chars over this 31-symbol
# alphabet ≈ 69 bits of entropy — online guessing is hopeless even before the auth
# rate limit, yet it's short enough to type.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_CHARS = 14
_PREFIX = "JS-"


def _root_secret() -> str:
    """The HMAC key. Falls back to the app secret so there's no second key to set;
    set ``JOBSCOUT_INVITE_SECRET`` to rotate invites independently of sessions."""
    return settings.invite_secret or settings.secret_key


def hash_code(code: str) -> str:
    """HMAC-SHA256 of a (normalized) plaintext code, hex — the value stored/looked up."""
    normalized = code.strip()
    return hmac.new(_root_secret().encode("utf-8"), normalized.encode("utf-8"), sha256).hexdigest()


def generate() -> tuple[str, str]:
    """A fresh ``(plaintext, code_hash)`` pair. Only the hash is persisted."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_CHARS))
    plaintext = _PREFIX + body
    return plaintext, hash_code(plaintext)


def mint(
    db: Session,
    *,
    max_uses: int = 1,
    expires_in_days: int | None = None,
    note: str | None = None,
    count: int = 1,
) -> list[str]:
    """Create ``count`` invite codes and return their plaintext (shown once — they
    can't be recovered later). ``expires_in_days=None`` mints non-expiring codes."""
    expires_at = utcnow() + timedelta(days=expires_in_days) if expires_in_days else None
    codes: list[str] = []
    for _ in range(max(1, count)):
        plaintext, code_hash = generate()
        db.add(InviteCode(
            code_hash=code_hash, max_uses=max(1, max_uses), expires_at=expires_at, note=note,
        ))
        codes.append(plaintext)
    db.flush()
    return codes


def redeem(db: Session, code: str | None) -> bool:
    """Consume one use of ``code`` if it is valid, atomically. Returns True on success.

    The increment is a single conditional UPDATE so concurrent registrations can never
    push ``uses`` past ``max_uses`` (SQLite serializes writers; Postgres re-evaluates the
    predicate against the locked row under READ COMMITTED). The caller runs this inside
    the registration transaction, so if the subsequent user insert fails (e.g. duplicate
    email) the rollback also undoes this increment — a failed signup never burns a use."""
    if not code or not code.strip():
        return False
    result = db.execute(
        update(InviteCode)
        .where(
            InviteCode.code_hash == hash_code(code),
            InviteCode.revoked.is_(False),
            InviteCode.uses < InviteCode.max_uses,
            (InviteCode.expires_at.is_(None)) | (InviteCode.expires_at > utcnow()),
        )
        .values(uses=InviteCode.uses + 1)
    )
    return result.rowcount == 1


def revoke(db: Session, *, code_id: int | None = None, code: str | None = None) -> bool:
    """Revoke a code by id or by plaintext (hashed for lookup). Returns True if a row
    was revoked. Idempotent — re-revoking an already-revoked code still returns True."""
    stmt = update(InviteCode).values(revoked=True)
    if code_id is not None:
        stmt = stmt.where(InviteCode.id == code_id)
    elif code:
        stmt = stmt.where(InviteCode.code_hash == hash_code(code))
    else:
        return False
    return db.execute(stmt).rowcount >= 1


def is_expired(invite: InviteCode, now: datetime | None = None) -> bool:
    """True when ``invite`` has passed its expiry — for display in ``invite list``."""
    return invite.expires_at is not None and invite.expires_at <= (now or utcnow())
