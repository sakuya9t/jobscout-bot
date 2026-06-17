"""Symmetric encryption for secrets at rest.

Every credential-like field is Fernet-encrypted before it touches the database, so a
DB leak alone doesn't expose it: a user's saved application-portal credentials
(``CompanyAccount``), their Telegram bot token, and their LLM API key. The portal
credentials call :func:`encrypt`/:func:`decrypt` directly at the router; the Telegram
token and LLM key use the :class:`EncryptedString` SQLAlchemy column type, which does
it transparently on every read/write.

The Fernet key is derived from ``JOBSCOUT_SECRET_KEY`` (the same env secret that signs
sessions), so there's no extra key to manage. The corollary: the **same**
``JOBSCOUT_SECRET_KEY`` must be configured everywhere a deployment reads these — the web
app, the daily-scan job, and migrations — or decryption fails. Rotating the secret
invalidates existing ciphertext, which :func:`decrypt` reports as ``None`` rather than
crashing.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

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


class EncryptedString(TypeDecorator):
    """A text column whose value is Fernet-encrypted at rest (see module docstring).

    Encrypts on write and decrypts on read transparently, so call sites just use the
    plain string — no read site can forget to decrypt. A stored value that isn't valid
    ciphertext for the current key (a row written before this column was encrypted, or
    after a key rotation) is passed through unchanged on read, so switching an existing
    column to this type doesn't null out legacy rows; ``jobscout encrypt-secrets``
    converts them in place. Backed by ``Text`` because ciphertext is longer than the
    plaintext it replaces (a Telegram token outgrows the old ``VARCHAR(128)``)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        plaintext = decrypt(value)
        # decrypt() -> None means it isn't ciphertext for the current key: a legacy
        # plaintext row or a rotated key. Return it as-is rather than silently
        # nulling a value the app may still need.
        return plaintext if plaintext is not None else value
