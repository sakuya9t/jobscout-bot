"""The "find a posting in my job list by its URL" lookup: the URL normalizer
(``services/urlmatch``) and the read-only ``GET /api/positions/lookup`` endpoint.
No network — the endpoint never scrapes or scores; we seed a Position + MatchResult
directly and assert visibility/isolation, applied-state, and URL-variant matching."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.db import session_scope
from app.main import app
from app.services.urlmatch import normalize_posting_url


# ── unit: normalize_posting_url ──────────────────────────────────────────────
def test_normalize_collapses_cosmetic_differences():
    base = "https://boards.greenhouse.io/acme/jobs/123"
    # http vs https, www, trailing slash, fragment, and tracking params all collapse.
    variants = [
        "http://boards.greenhouse.io/acme/jobs/123",
        "https://www.boards.greenhouse.io/acme/jobs/123/",
        "https://boards.greenhouse.io/acme/jobs/123#apply",
        "https://boards.greenhouse.io/acme/jobs/123?utm_source=newsletter&gh_src=x",
        "boards.greenhouse.io/acme/jobs/123",  # scheme-less paste
    ]
    key = normalize_posting_url(base)
    assert key is not None
    for v in variants:
        assert normalize_posting_url(v) == key, v


def test_normalize_keeps_identifying_query_and_distinct_paths():
    # A non-tracking query param is part of the identity and is kept.
    assert normalize_posting_url("https://x.io/jobs?id=1") != normalize_posting_url("https://x.io/jobs?id=2")
    # Different paths never collapse.
    assert normalize_posting_url("https://x.io/jobs/1") != normalize_posting_url("https://x.io/jobs/2")


def test_normalize_rejects_empty_and_non_http():
    for bad in (None, "", "   ", "ftp://example.com/x", "javascript:alert(1)", "notaurl"):
        assert normalize_posting_url(bad) is None


# ── endpoint helpers ─────────────────────────────────────────────────────────
def _auth(client, email):
    token = client.post("/api/auth/register", json={"email": email, "password": "secret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _seed_position(email, url, *, score=80, removed=False):
    """Give the user a followed company + a Position (visible via a MatchResult)."""
    with session_scope() as db:
        uid = db.scalar(select(models.User.id).where(models.User.email == email))
        company = models.Company(user_id=uid, name="Acme", ats_type="greenhouse", ats_token="acme")
        db.add(company); db.flush()
        pos = models.Position(
            company_id=company.id, external_id="1", title="Backend Engineer",
            location="Remote", description="d", url=url,
            removed_at=(models.utcnow() if removed else None),
        )
        db.add(pos); db.flush()
        db.add(models.MatchResult(user_id=uid, position_id=pos.id,
                                  passed_filter=True, match_score=score, win_probability=50))
        return pos.id


URL = "https://boards.greenhouse.io/acme/jobs/123"


# ── endpoint: GET /api/positions/lookup ──────────────────────────────────────
def test_lookup_matches_visible_position():
    with TestClient(app) as c:
        h = _auth(c, "a@x.com")
        pid = _seed_position("a@x.com", URL)
        body = c.get("/api/positions/lookup", params={"url": URL}, headers=h).json()
        assert body["matched"] is True
        assert body["position_id"] == pid
        assert body["match_score"] == 80
        assert body["applied"] is False


def test_lookup_matches_cosmetic_url_variant():
    with TestClient(app) as c:
        h = _auth(c, "b@x.com")
        pid = _seed_position("b@x.com", URL)
        variant = "http://www.boards.greenhouse.io/acme/jobs/123/?utm_source=foo#apply"
        body = c.get("/api/positions/lookup", params={"url": variant}, headers=h).json()
        assert body["matched"] is True and body["position_id"] == pid


def test_lookup_reflects_applied_state():
    with TestClient(app) as c:
        h = _auth(c, "d@x.com")
        pid = _seed_position("d@x.com", URL)
        assert c.post(f"/api/applications/{pid}", headers=h).status_code == 201
        body = c.get("/api/positions/lookup", params={"url": URL}, headers=h).json()
        assert body["matched"] is True and body["applied"] is True


def test_lookup_unknown_url_is_a_miss():
    with TestClient(app) as c:
        h = _auth(c, "e@x.com")
        _seed_position("e@x.com", URL)
        body = c.get("/api/positions/lookup",
                     params={"url": "https://jobs.lever.co/other/abc"}, headers=h).json()
        assert body["matched"] is False
        assert body["position_id"] is None


def test_lookup_is_isolated_per_user():
    """A position scored for one user is invisible to another (no MatchResult)."""
    with TestClient(app) as c:
        _auth(c, "owner@x.com")
        _seed_position("owner@x.com", URL)
        other = _auth(c, "stranger@x.com")
        body = c.get("/api/positions/lookup", params={"url": URL}, headers=other).json()
        assert body["matched"] is False


def test_lookup_removed_unapplied_position_is_a_miss():
    """The visibility gate hides a closed posting the user never applied to."""
    with TestClient(app) as c:
        h = _auth(c, "f@x.com")
        _seed_position("f@x.com", URL, removed=True)
        body = c.get("/api/positions/lookup", params={"url": URL}, headers=h).json()
        assert body["matched"] is False


def test_lookup_invalid_url_is_422():
    with TestClient(app) as c:
        h = _auth(c, "g@x.com")
        assert c.get("/api/positions/lookup", params={"url": "notaurl"}, headers=h).status_code == 422


def test_lookup_requires_auth():
    with TestClient(app) as c:
        assert c.get("/api/positions/lookup", params={"url": URL}).status_code == 401
