"""App-level tests: auth roundtrip, multi-tenant isolation, the matcher pipeline
(scoring, dedup-on-rerun, error-marker skip, descriptionless skip), reporter
thresholds, and the Ollama health states. No network: the LLM client is faked
and the scraper is monkeypatched."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import session_scope
from app import models
from app.services import matcher, reporter
from app.services.ollama_client import OllamaError


# ── Fakes ────────────────────────────────────────────────────────────────────
class GoodClient:
    """LLM stub returning a fixed valid verdict."""
    model = "fake-good"

    def __init__(self, score: int = 88):
        self.score = score
        self.calls = 0

    def chat_json(self, system, user, schema, temperature=0.2):
        self.calls += 1
        return {
            "matches_requirements": True, "match_score": self.score,
            "win_probability": 50, "reasoning": "Solid fit.",
            "strengths": ["Python"], "gaps": [],
        }


class FailClient:
    model = "fake-fail"

    def chat_json(self, *a, **k):
        raise OllamaError("Ollama returned 500: server error")


class BoomClient:
    """Fails the test if the LLM is called at all (proves a pair was skipped)."""
    model = "fake-boom"

    def chat_json(self, *a, **k):
        raise AssertionError("LLM should not have been called")


def _seed_user(db, *, email="u@x.com", description="Build Python APIs", min_score=70):
    user = models.User(email=email, hashed_password="h")
    db.add(user)
    db.flush()
    db.add(models.Resume(user_id=user.id, filename="r.txt",
                         content_text="Senior Python engineer", is_active=True))
    company = models.Company(user_id=user.id, name="Acme", ats_type="greenhouse", ats_token="acme")
    db.add(company)
    db.flush()
    db.add(models.Position(company_id=company.id, external_id="1", title="Backend Engineer",
                          location="Remote", description=description))
    db.add(models.Interest(user_id=user.id, label="be", title_keywords="backend",
                          locations="remote", min_score=min_score, is_active=True))
    db.flush()
    return user.id


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Never hit a real career page during pipeline tests."""
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: [])


# ── Auth ─────────────────────────────────────────────────────────────────────
def _register(client, email, password="secret123"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def test_register_login_roundtrip():
    with TestClient(app) as c:
        r = _register(c, "a@b.com")
        assert r.status_code == 200 and r.json()["access_token"]
        assert _register(c, "a@b.com").status_code == 409  # duplicate
        assert c.post("/api/auth/login",
                      json={"email": "a@b.com", "password": "secret123"}).status_code == 200
        # Correct length, wrong password → 401 (not a 422 validation error).
        assert c.post("/api/auth/login",
                      json={"email": "a@b.com", "password": "wrongpass"}).status_code == 401


def test_tenant_isolation():
    """User B must never see or mutate user A's data."""
    with TestClient(app) as c:
        tok_a = _register(c, "a@x.com").json()["access_token"]
        tok_b = _register(c, "b@x.com").json()["access_token"]
        ha = {"Authorization": f"Bearer {tok_a}"}
        hb = {"Authorization": f"Bearer {tok_b}"}

        cid = c.post("/api/companies", json={"name": "Acme"}, headers=ha).json()["id"]

        # B can't list, read, patch, or delete A's company.
        assert c.get("/api/companies", headers=hb).json() == []
        assert c.patch(f"/api/companies/{cid}", json={"name": "Hax"}, headers=hb).status_code == 404
        assert c.delete(f"/api/companies/{cid}", headers=hb).status_code == 404
        # B's positions view for A's company id is empty (scoped to B's companies).
        assert c.get(f"/api/positions?company_id={cid}", headers=hb).json() == []
        # A still sees it.
        assert len(c.get("/api/companies", headers=ha).json()) == 1


# ── Matcher pipeline ─────────────────────────────────────────────────────────
def test_scoring_and_dedup_on_rerun():
    with session_scope() as db:
        uid = _seed_user(db)
    good = GoodClient()
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=good)
    assert res.scored == 1 and not res.errors and good.calls == 1
    # Re-run is free: already-scored pair is skipped, LLM never called again.
    with session_scope() as db:
        res2 = matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient())
    assert res2.scored == 0


def test_failure_persists_marker_and_is_not_rebilled():
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=FailClient())
    assert res.scored == 0 and res.errors  # failure surfaced
    with session_scope() as db:
        markers = list(db.scalars(
            matcher.select(models.MatchResult).where(models.MatchResult.model == matcher.ERROR_MODEL)))
        assert len(markers) == 1
    # Re-run must not call the LLM again (marker in the `already` set).
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient())
    # --retry-failed clears markers so they re-score.
    with session_scope() as db:
        assert matcher.clear_failed_markers(db, user_id=uid) == 1


def test_descriptionless_position_skipped_with_warning():
    with session_scope() as db:
        uid = _seed_user(db, description=None)
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient())
    assert res.scored == 0  # never billed the LLM
    assert any("no scraped description" in e for e in res.errors)


def test_reporter_threshold_filters_low_scores():
    with session_scope() as db:
        uid = _seed_user(db, min_score=90)  # interest threshold above the score
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(score=88))
    with session_scope() as db:
        user = db.get(models.User, uid)
        # 88 < interest min_score 90 → excluded by default thresholds.
        assert reporter.build_report(db, user) == []
        # Explicit override below the score → included.
        assert len(reporter.build_report(db, user, min_score=80)) == 1


# ── Health ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,expected", [(200, "ok"), (401, "unauthorized"), (503, "unreachable")])
def test_health_states(monkeypatch, status, expected):
    import app.services.ollama_client as oc

    class Resp:
        status_code = status

    monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: Resp())
    assert oc.OllamaClient().health() == expected
