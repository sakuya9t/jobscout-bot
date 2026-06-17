"""The Telegram bot token and LLM API key are secrets and MUST be encrypted at rest.

These tests are a regression guard: if either column is ever reverted to a plain
``String``/``Text`` — or a new credential-named column is added as plaintext — the
suite fails. They also prove the round-trip works end to end (raw column holds
ciphertext; the ORM hands back plaintext) and that the one-time ``encrypt-secrets``
migration converts legacy plaintext rows.
"""
from __future__ import annotations

import argparse

from sqlalchemy import String, Text, select, text

from app.crypto import EncryptedString, decrypt
from app.db import SessionLocal, engine
from app.models import Base, LlmConfig, User

# (table, column) credential-named columns intentionally NOT EncryptedString. Each
# must be safe by other means — keep this list short and justified.
_PLAINTEXT_ALLOWED = {
    ("users", "hashed_password"),          # bcrypt hash: one-way, not reversible by design
    ("companies", "ats_token"),            # public ATS board slug (part of the careers URL), not a credential
    ("company_accounts", "password_enc"),  # Fernet-encrypted via crypto.encrypt() at the router; stored as Text
}
_SECRET_NAME_HINTS = ("token", "api_key", "password", "secret")
_STRINGY = (String, Text, EncryptedString)


def test_known_credential_columns_are_encrypted():
    """Direct guard: these two columns must use EncryptedString. Reverting either to a
    plain String/Text (storing the secret in plaintext) fails here."""
    assert isinstance(User.__table__.c.telegram_bot_token.type, EncryptedString), (
        "users.telegram_bot_token must be EncryptedString — never store it as plaintext"
    )
    assert isinstance(LlmConfig.__table__.c.api_key.type, EncryptedString), (
        "llm_configs.api_key must be EncryptedString — never store it as plaintext"
    )


def test_no_credential_column_is_plaintext():
    """Sweep every model: any string column whose name looks credential-like must be
    EncryptedString unless explicitly allowlisted above. Catches a future secret column
    added as plaintext, too."""
    offenders = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not isinstance(col.type, _STRINGY):
                continue  # integer token-counts etc. aren't secrets
            name = col.name.lower()
            if not any(hint in name for hint in _SECRET_NAME_HINTS):
                continue
            if (table.name, col.name) in _PLAINTEXT_ALLOWED:
                continue
            if not isinstance(col.type, EncryptedString):
                offenders.append(f"{table.name}.{col.name} ({col.type})")
    assert not offenders, (
        "Credential-like columns stored as plaintext — wrap them in EncryptedString or, "
        f"if genuinely safe, add to _PLAINTEXT_ALLOWED with a reason: {offenders}"
    )


def test_telegram_token_encrypted_at_rest():
    with SessionLocal() as db:
        u = User(email="tg@example.com", hashed_password="x", telegram_bot_token="123456:SECRET")
        db.add(u)
        db.commit()
        uid = u.id
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT telegram_bot_token FROM users WHERE id = :i"), {"i": uid}
        ).scalar()
    assert raw and raw != "123456:SECRET"  # stored value is ciphertext, not the token
    assert decrypt(raw) == "123456:SECRET"  # ...and decrypts back to it
    with SessionLocal() as db:  # ORM read is transparently decrypted
        assert db.get(User, uid).telegram_bot_token == "123456:SECRET"


def test_llm_api_key_encrypted_at_rest():
    with SessionLocal() as db:
        u = User(email="llm@example.com", hashed_password="x")
        db.add(u)
        db.flush()
        db.add(LlmConfig(user_id=u.id, api_key="sk-supersecret"))
        db.commit()
        uid = u.id
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT api_key FROM llm_configs WHERE user_id = :i"), {"i": uid}
        ).scalar()
    assert raw and raw != "sk-supersecret"
    assert decrypt(raw) == "sk-supersecret"
    with SessionLocal() as db:
        assert db.scalar(select(LlmConfig).where(LlmConfig.user_id == uid)).api_key == "sk-supersecret"


def test_legacy_plaintext_passes_through_on_read():
    """A row written before encryption (raw plaintext) still reads correctly, so
    switching the column type doesn't null out existing values."""
    with SessionLocal() as db:
        u = User(email="legacy@example.com", hashed_password="x")
        db.add(u)
        db.commit()
        uid = u.id
    with engine.begin() as conn:  # stamp plaintext straight into the column
        conn.execute(
            text("UPDATE users SET telegram_bot_token = 'legacy-plain' WHERE id = :i"), {"i": uid}
        )
    with SessionLocal() as db:
        assert db.get(User, uid).telegram_bot_token == "legacy-plain"


def test_encrypt_secrets_command_converts_plaintext():
    """`jobscout encrypt-secrets` re-encrypts legacy plaintext rows in place."""
    from app.cli import cmd_encrypt_secrets

    with SessionLocal() as db:
        u = User(email="mig@example.com", hashed_password="x")
        db.add(u)
        db.flush()
        db.add(LlmConfig(user_id=u.id, api_key="seed"))
        db.commit()
        uid = u.id
    with engine.begin() as conn:  # simulate pre-encryption plaintext in both columns
        conn.execute(text("UPDATE users SET telegram_bot_token = 'PLAIN-TOK' WHERE id = :i"), {"i": uid})
        conn.execute(text("UPDATE llm_configs SET api_key = 'PLAIN-KEY' WHERE user_id = :i"), {"i": uid})

    assert cmd_encrypt_secrets(argparse.Namespace()) == 0

    with engine.connect() as conn:
        raw_tok = conn.execute(text("SELECT telegram_bot_token FROM users WHERE id = :i"), {"i": uid}).scalar()
        raw_key = conn.execute(text("SELECT api_key FROM llm_configs WHERE user_id = :i"), {"i": uid}).scalar()
    assert raw_tok != "PLAIN-TOK" and decrypt(raw_tok) == "PLAIN-TOK"
    assert raw_key != "PLAIN-KEY" and decrypt(raw_key) == "PLAIN-KEY"
