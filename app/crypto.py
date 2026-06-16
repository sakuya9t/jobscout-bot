"""Symmetric encryption for secrets at rest (e.g. a user's saved application-
portal credentials in ``CompanyAccount``).

Unlike the Telegram bot token / LLM API key — stored as-is with the DB treated as
secret — third-party login credentials are encrypted before they touch the
database, so a DB leak alone doesn't expose them. The Fernet key is derived from
``JOBSCOUT_SECRET_KEY`` (the same env secret that signs sessions), so there's no
extra key to manage; rotating the secret invalidates existing ciphertext, which
``decrypt`` reports as ``None`` rather than crashing.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """A Fernet built from a key deterministically derived from the app secret.
    SHA-256 yields the 32 bytes Fernet needs; urlsafe-b64 makes it a valid key."""
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a string to a urlsafe token, or pass ``None``/empty through as
    ``None`` (so an absent secret stays an absent column, not a ciphertext blob)."""
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """Decrypt a token produced by :func:`encrypt`. Returns ``None`` for a missing
    value or one that can't be decrypted with the current key (e.g. the secret was
    rotated, or the column holds legacy plaintext) — never raises."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
