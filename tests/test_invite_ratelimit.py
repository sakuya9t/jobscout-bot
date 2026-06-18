"""Invite-gated registration + rate limiting.

These features are off by default in the suite (see conftest), so each test flips the
relevant setting on explicitly and, for rate limiting, resets the process-global limiter
so state can't bleed between tests."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import invites, ratelimit
from app.config import settings
from app.db import session_scope
from app.main import app
from app.models import InviteCode
from app.timeutil import utcnow


def _mint(**kw) -> str:
    """Mint one code through a real session and return its plaintext."""
    with session_scope() as db:
        return invites.mint(db, count=1, **kw)[0]


def _register(client, email, *, code=None, password="secret123"):
    body = {"email": email, "password": password}
    if code is not None:
        body["invite_code"] = code
    return client.post("/api/auth/register", json=body)


# ── invite codes ─────────────────────────────────────────────────────────────
def test_generated_codes_are_clean_uppercase_alphanumeric():
    """Codes read cleanly: a `JS-` prefix then only unambiguous uppercase letters
    and digits — no base64 punctuation (-, _, +, /) or look-alike glyphs."""
    import re

    code = _mint(max_uses=1)
    assert code.startswith("JS-")
    body = code[len("JS-"):]
    assert re.fullmatch(r"[A-Z0-9]+", body)
    assert not (set(body) & set("01OIL")), "should drop look-alike characters"


def test_only_the_hmac_is_stored_never_the_plaintext():
    code = _mint(max_uses=1)
    with session_scope() as db:
        stored = db.scalar(select(InviteCode.code_hash))
    assert stored != code                      # not the plaintext
    assert stored == invites.hash_code(code)   # is its HMAC
    assert len(stored) == 64                   # sha256 hex


def test_register_requires_a_valid_code(monkeypatch):
    monkeypatch.setattr(settings, "require_invite", True)
    with TestClient(app) as c:
        assert _register(c, "no-code@x.com").status_code == 403
        assert _register(c, "bad-code@x.com", code="JS-nonsense").status_code == 403
        code = _mint(max_uses=1)
        assert _register(c, "ok@x.com", code=code).status_code == 200
        # Single-use code is now exhausted.
        assert _register(c, "second@x.com", code=code).status_code == 403


def test_failed_signup_does_not_burn_a_use(monkeypatch):
    """A duplicate-email 409 must roll back the reserved use, so the code's remaining
    capacity is intact for the next real signup."""
    monkeypatch.setattr(settings, "require_invite", True)
    code = _mint(max_uses=2)
    with TestClient(app) as c:
        assert _register(c, "dup@x.com", code=code).status_code == 200       # use 1
        assert _register(c, "dup@x.com", code=code).status_code == 409       # no use burned
        # If the 409 had consumed a use, max_uses=2 would now be exhausted and this 403s.
        assert _register(c, "fresh@x.com", code=code).status_code == 200     # use 2


def test_redeem_respects_max_uses_expiry_and_revoke():
    with session_scope() as db:
        code = invites.mint(db, max_uses=2)[0]
        assert invites.redeem(db, code) is True
        assert invites.redeem(db, code) is True
        assert invites.redeem(db, code) is False          # max_uses reached
        assert invites.redeem(db, None) is False          # missing code

        expired = invites.mint(db, max_uses=1, expires_in_days=1)[0]
        db.execute(
            InviteCode.__table__.update()
            .where(InviteCode.code_hash == invites.hash_code(expired))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        assert invites.redeem(db, expired) is False       # past expiry

        revoked = invites.mint(db, max_uses=1)[0]
        invites.revoke(db, code=revoked)
        assert invites.redeem(db, revoked) is False       # revoked


def test_invite_ignored_when_not_required(monkeypatch):
    monkeypatch.setattr(settings, "require_invite", False)
    with TestClient(app) as c:
        assert _register(c, "open@x.com").status_code == 200  # no code needed


# ── rate limiting ────────────────────────────────────────────────────────────
@pytest.fixture
def reset_limiter():
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


def test_login_is_rate_limited(monkeypatch, reset_limiter):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    with TestClient(app) as c:
        codes = [
            c.post("/api/auth/login", json={"email": "x@y.com", "password": "wrong1"}).status_code
            for _ in range(5)
        ]
    assert codes[:3] == [401, 401, 401]           # invalid creds, allowed
    assert codes[3] == 429 and codes[4] == 429     # then throttled
