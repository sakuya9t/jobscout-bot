"""App-level tests: auth roundtrip, multi-tenant isolation, the matcher pipeline
(scoring, dedup-on-rerun, error-marker skip, descriptionless skip), reporter
thresholds, and the Ollama health states. No network: the LLM client is faked
and the scraper is monkeypatched."""
from __future__ import annotations

import json
from types import SimpleNamespace

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
    """Fails the test if either model is called at all (proves a pair was skipped)."""
    model = "fake-boom"

    def chat_json(self, *a, **k):
        raise AssertionError("LLM should not have been called")

    def chat_text(self, *a, **k):
        raise AssertionError("LLM should not have been called")


class FilterPass:
    """Cheap-filter stub (stage 1, free-text YES/NO): every posting matches."""
    model = "fake-filter"

    def __init__(self):
        self.calls = 0

    def chat_text(self, system, user, temperature=0.4):
        self.calls += 1
        return "YES — plausible"


class FilterReject:
    """Cheap-filter stub (stage 1): nothing matches, so scoring never runs."""
    model = "fake-filter"

    def __init__(self):
        self.calls = 0

    def chat_text(self, system, user, temperature=0.4):
        self.calls += 1
        return "NO — not a fit"


class FilterFirstOnly:
    """Batched cheap-filter stub: returns a JSON array marking only posting #1 a
    match, to prove the matcher batches one call and honors per-posting verdicts."""
    model = "fake-filter"

    def __init__(self):
        self.calls = 0

    def chat_text(self, system, user, temperature=0.4):
        self.calls += 1
        n = user.count("| loc:")  # one per posting block in the batch prompt
        return json.dumps([{"id": i + 1, "match": i == 0} for i in range(n)])


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


def test_email_normalized_case_insensitive():
    """A@b.com and a@b.com are the same account; login is case-insensitive."""
    with TestClient(app) as c:
        assert _register(c, "Mixed@Case.com").status_code == 200
        assert _register(c, "mixed@case.com").status_code == 409  # duplicate
        assert c.post("/api/auth/login",
                      json={"email": "MIXED@CASE.com", "password": "secret123"}).status_code == 200


def test_regenerate_telegram_code():
    with TestClient(app) as c:
        tok = _register(c, "rg@x.com").json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        code1 = c.get("/api/auth/me", headers=h).json()["telegram_link_code"]
        code2 = c.post("/api/auth/telegram-code", headers=h).json()["telegram_link_code"]
        assert code1 and code2 and code2 != code1


def test_company_presets_listed_and_addable():
    """The preset registry is exposed and a preset can be added as a real company
    (carrying its ATS type/token through), so the dashboard's quick-add works."""
    from app.company_presets import PRESETS

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'p@x.com').json()['access_token']}"}

        presets = c.get("/api/companies/presets", headers=h).json()
        assert {p["name"] for p in presets} >= {"Anthropic", "OpenAI", "Google"}
        assert len(presets) == len(PRESETS)

        anthropic = next(p for p in presets if p["name"] == "Anthropic")
        assert anthropic["ats_type"] == "greenhouse" and anthropic["ats_token"] == "anthropic"

        # The preset payload is a valid company create body.
        body = {k: anthropic[k] for k in ("name", "careers_url", "ats_type", "ats_token")}
        company = c.post("/api/companies", json=body, headers=h).json()
        assert company["ats_type"] == "greenhouse" and company["ats_token"] == "anthropic"

    # Unauthenticated callers get nothing (auth-gated like its siblings).
    with TestClient(app) as c:
        assert c.get("/api/companies/presets").status_code == 401


def test_resume_is_account_level_and_overwritten():
    """One resume per account: a second upload replaces the first (new row id),
    so the listing only ever holds the latest resume."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'rs@x.com').json()['access_token']}"}

        r1 = c.post("/api/resumes", headers=h,
                    files={"file": ("first.txt", b"Senior Python engineer", "text/plain")})
        assert r1.status_code == 201
        rid1 = r1.json()["id"]

        r2 = c.post("/api/resumes", headers=h,
                    files={"file": ("second.txt", b"Staff Go engineer", "text/plain")})
        assert r2.status_code == 201 and r2.json()["id"] != rid1

        listing = c.get("/api/resumes", headers=h).json()
        assert len(listing) == 1
        assert listing[0]["id"] == r2.json()["id"] and listing[0]["filename"] == "second.txt"


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
        res = matcher.run_for_user(db, db.get(models.User, uid), client=good, filter_client=FilterPass())
    assert res.scored == 1 and not res.errors and good.calls == 1
    # Re-run is free: already-scored pair is skipped, neither model called again.
    with session_scope() as db:
        res2 = matcher.run_for_user(
            db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient()
        )
    assert res2.scored == 0


def test_failure_persists_marker_and_is_not_rebilled():
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=FailClient(), filter_client=FilterPass())
    assert res.scored == 0 and res.errors  # failure surfaced
    with session_scope() as db:
        markers = list(db.scalars(
            matcher.select(models.MatchResult).where(models.MatchResult.model == matcher.ERROR_MODEL)))
        assert len(markers) == 1
    # Re-run must not call either model again (marker in the `already` set).
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient())
    # --retry-failed clears markers so they re-score.
    with session_scope() as db:
        assert matcher.clear_failed_markers(db, user_id=uid) == 1


def test_descriptionless_position_skipped_with_warning(monkeypatch):
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db, description=None)
    # Surface a brand-new descriptionless posting this run; the warning is scoped
    # to newly-seen ones so it doesn't repeat forever for already-known postings.
    monkeypatch.setattr(
        matcher.scraper, "scrape_company",
        lambda company: [scraper.ScrapedPosition(external_id="999", title="Careers", description=None)],
    )
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient())
    assert res.scored == 0  # never billed either model
    assert any("no scraped description" in e for e in res.errors)
    # Re-run: the same posting is no longer new, so it must not warn again.
    with session_scope() as db:
        res2 = matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient())
    assert not any("no scraped description" in e for e in res2.errors)


def test_delete_interest_with_matches_cascades():
    """With PRAGMA foreign_keys=ON, deleting an interest must cascade to its
    match rows instead of raising an FK IntegrityError (a 500 on the route)."""
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
    with session_scope() as db:
        assert db.scalar(matcher.select(models.MatchResult)) is not None
        db.delete(db.scalar(matcher.select(models.Interest)))  # must not raise
    with session_scope() as db:
        assert db.scalar(matcher.select(models.MatchResult)) is None


def test_google_scrape_parses_ssr_json_and_paginates(monkeypatch):
    """Google has no ATS API; we parse the jobs embedded in its results-page HTML
    (AF_initDataCallback) and page via ?page=N until a page yields nothing."""
    import json
    from app.services import scraper

    rec = [
        "123456789012345",
        "Staff Software Engineer",
        "https://www.google.com/about/careers/applications/jobs/results/123456789012345",
        [None, "<ul><li>Build distributed systems</li></ul>"],
        [None, "<h3>Minimum qualifications:</h3><ul><li>BS degree</li></ul>"],
        "projects/x", None, "Google", None,
        [["New York, NY, USA", ["New York, NY, USA"], "New York", None, "NY", "US"]],
    ]
    page1 = "AF_initDataCallback({key:'ds:0', data:" + json.dumps([[rec]]) + ", sideChannel: {}});"
    pages: dict[str, str] = {}

    def fake_fetch(url: str) -> str:
        return page1 if "page=1" in url else "<html>no jobs here</html>"

    monkeypatch.setattr(scraper, "_fetch_text", fake_fetch)
    out = scraper.scrape_google("https://www.google.com/about/careers/applications/jobs/results/", max_pages=5)

    assert len(out) == 1  # page 2 has no records -> paging stops
    p = out[0]
    assert p.external_id == "123456789012345"
    assert p.title == "Staff Software Engineer"
    assert p.location == "New York, NY, USA"
    assert p.description and "distributed systems" in p.description and "BS degree" in p.description


def test_duplicate_external_ids_in_one_scrape_deduped(monkeypatch):
    """A board repeating an external_id within one scrape must not violate the
    (company_id, external_id) unique constraint and abort the run."""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)
    dupe = scraper.ScrapedPosition(external_id="dup", title="Backend Engineer",
                                   location="Remote", description="Build APIs")
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: [dupe, dupe])
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
    assert res.new_positions == 1 and not res.errors
    with session_scope() as db:
        rows = list(db.scalars(matcher.select(models.Position)
                               .where(models.Position.external_id == "dup")))
        assert len(rows) == 1


def test_excluded_by_keyword_emits_warning():
    """A posting dropped by an explicit exclude keyword never reaches either model,
    and a resulting '0 scored' run explains why instead of staying silent."""
    with session_scope() as db:
        uid = _seed_user(db)
        interest = db.scalar(matcher.select(models.Interest))
        interest.exclude_keywords = "backend"  # the seeded posting is "Backend Engineer"
    with session_scope() as db:
        res = matcher.run_for_user(
            db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient()
        )
    assert res.scored == 0
    assert any("exclude keywords" in e for e in res.errors)


def test_cheap_filter_gates_expensive_scoring():
    """Stage 1: the cheap filter rejecting a posting means the scoring model is
    never called, a passed_filter=False row is persisted (so the report omits it),
    and a re-run skips the pair entirely."""
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        res = matcher.run_for_user(
            db, db.get(models.User, uid), client=BoomClient(), filter_client=FilterReject()
        )
    assert res.scored == 0 and res.filtered == 1
    assert any("relevance filter" in e for e in res.errors)
    with session_scope() as db:
        rows = list(db.scalars(matcher.select(models.MatchResult)))
        assert len(rows) == 1 and rows[0].passed_filter is False
    # Re-run: rejection is recorded, so neither model runs again.
    with session_scope() as db:
        res2 = matcher.run_for_user(
            db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient()
        )
    assert res2.scored == 0 and res2.filtered == 0


def test_filter_batch_one_call_mixed_verdicts(monkeypatch):
    """The cheap filter runs ONCE per batch and the matcher honors per-posting
    verdicts: only postings it marks a match get the (expensive) scoring call."""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)  # 1 seeded posting + interest, no excludes
    fresh = [scraper.ScrapedPosition(external_id=f"f{i}", title=f"Backend Engineer {i}",
                                     location="Remote", description="Build APIs") for i in range(3)]
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: fresh)
    monkeypatch.setattr(matcher.settings, "score_filter_batch_size", 10)  # all 4 in one batch
    flt = FilterFirstOnly()
    good = GoodClient()
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=good, filter_client=flt)
    # 4 candidates screened in a single call; only #1 matches → 1 scored, 3 filtered.
    assert flt.calls == 1
    assert res.scored == 1 and res.filtered == 3 and good.calls == 1


def test_resume_reupload_same_content_is_noop():
    """Re-uploading identical resume content reuses the existing resume (same id),
    so scores cached against it aren't recomputed; new content replaces it."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'rc@x.com').json()['access_token']}"}
        id1 = c.post("/api/resumes", headers=h,
                     files={"file": ("cv.txt", b"Senior Python engineer", "text/plain")}).json()["id"]
        # Same content (even with a different filename) → same row, no new id.
        same = c.post("/api/resumes", headers=h,
                      files={"file": ("other.txt", b"Senior Python engineer", "text/plain")})
        assert same.json()["id"] == id1
        # Different content → replaced with a fresh row.
        changed = c.post("/api/resumes", headers=h,
                         files={"file": ("cv.txt", b"Staff Go engineer", "text/plain")})
        assert changed.json()["id"] != id1
        assert len(c.get("/api/resumes", headers=h).json()) == 1


def test_scoring_capped_per_run(monkeypatch):
    """The per-run cap bounds how many postings get evaluated; the rest wait for
    the next run, and the run says it was truncated."""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)
    fresh = [scraper.ScrapedPosition(external_id=f"f{i}", title="Backend Engineer",
                                     location="Remote", description="Build APIs") for i in range(4)]
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: fresh)
    monkeypatch.setattr(matcher.settings, "score_max_per_run", 2)
    good = GoodClient()
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=good, filter_client=FilterPass())
    assert res.scored == 2 and good.calls == 2
    assert any("cap" in e.lower() for e in res.errors)


def test_report_min_results_backfills_below_threshold():
    """The dashboard report (min_results) shows below-threshold matches, tagged,
    when too few clear the bar — while the default (Telegram) stays threshold-only."""
    with session_scope() as db:
        uid = _seed_user(db, min_score=90)  # threshold above the score
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(score=80), filter_client=FilterPass())
    with session_scope() as db:
        user = db.get(models.User, uid)
        assert reporter.build_report(db, user) == []  # threshold-only: 80 < 90
        backfilled = reporter.build_report(db, user, min_results=5)
        assert len(backfilled) == 1 and backfilled[0]["below_threshold"] is True


def test_editing_interest_clears_its_matches():
    """Editing matching criteria drops the interest's matches (so the next run
    re-evaluates); editing a non-scoring field (label) keeps them."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'ie@x.com').json()['access_token']}"}
        iid = c.post("/api/interests", json={"label": "BE", "title_keywords": "backend"},
                     headers=h).json()["id"]
        with session_scope() as db:
            user = db.scalar(matcher.select(models.User).where(models.User.email == "ie@x.com"))
            company = models.Company(user_id=user.id, name="Acme")
            db.add(company)
            db.flush()
            pos = models.Position(company_id=company.id, external_id="1",
                                  title="Backend Engineer", description="x")
            db.add(pos)
            db.flush()
            db.add(models.MatchResult(user_id=user.id, position_id=pos.id, interest_id=iid,
                                      match_score=80, passed_filter=True, model="m"))

        c.patch(f"/api/interests/{iid}", json={"label": "BE renamed"}, headers=h)
        with session_scope() as db:
            assert db.scalar(matcher.select(models.MatchResult)) is not None  # label-only: kept

        c.patch(f"/api/interests/{iid}", json={"title_keywords": "platform"}, headers=h)
        with session_scope() as db:
            assert db.scalar(matcher.select(models.MatchResult)) is None  # criteria change: cleared


def test_reporter_threshold_filters_low_scores():
    with session_scope() as db:
        uid = _seed_user(db, min_score=90)  # interest threshold above the score
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(score=88), filter_client=FilterPass())
    with session_scope() as db:
        user = db.get(models.User, uid)
        # 88 < interest min_score 90 → excluded by default thresholds.
        assert reporter.build_report(db, user) == []
        # Explicit override below the score → included.
        assert len(reporter.build_report(db, user, min_score=80)) == 1


def test_job_list_snapshots_keep_previous_ranked_versions():
    with session_scope() as db:
        uid = _seed_user(db, min_score=90)
        user = db.get(models.User, uid)
        matcher.run_for_user(db, user, client=GoodClient(score=80), filter_client=FilterPass())
        first = reporter.record_job_list_snapshot(
            db, user, SimpleNamespace(new_positions=0, scored=1, filtered=0, errors=[])
        )
        first_items = reporter.job_list_items(first)
        assert len(first_items) == 1
        assert first_items[0]["title"] == "Backend Engineer"
        assert first_items[0]["below_threshold"] is True

        company = db.scalar(matcher.select(models.Company).where(models.Company.user_id == uid))
        db.add(models.Position(company_id=company.id, external_id="2", title="Principal Backend",
                               location="Remote", description="Build Python platforms"))
        db.flush()
        matcher.run_for_user(db, user, client=GoodClient(score=97), filter_client=FilterPass())
        second = reporter.record_job_list_snapshot(
            db, user, SimpleNamespace(new_positions=1, scored=1, filtered=0, errors=["cap reached"])
        )

        assert reporter.job_list_items(first)[0]["title"] == "Backend Engineer"
        latest_items = reporter.job_list_items(second)
        assert [item["title"] for item in latest_items] == ["Principal Backend", "Backend Engineer"]
        assert reporter.job_list_errors(second) == ["cap reached"]

        from app.routers.reports import _job_list_out

        response = _job_list_out(second, limit=1)
        assert response.total == 2
        assert len(response.items) == 1
        assert response.items[0].title == "Principal Backend"


# ── Telegram ─────────────────────────────────────────────────────────────────
def test_telegram_link_code_is_one_time():
    """A link code is burned on use so a leaked code can't re-bind the chat."""
    import app.services.telegram_bot as tg

    with session_scope() as db:
        u = models.User(email="t@x.com", hashed_password="h", telegram_link_code="abc123")
        db.add(u)
        db.flush()
        uid = u.id
    assert "Linked" in tg._link_account("abc123", "555")
    with session_scope() as db:
        u = db.get(models.User, uid)
        assert u.telegram_chat_id == "555" and u.telegram_link_code is None
    # Replaying the now-dead code is rejected.
    assert "isn't valid" in tg._link_account("abc123", "999")


def test_telegram_report_warnings_and_chunking():
    from app.services.reporter import report_to_telegram
    from app.services.telegram_bot import _split_for_telegram

    msg = report_to_telegram([], errors=["Ollama returned 401: bad key"])
    assert "Run warnings" in msg and "401" in msg
    chunks = _split_for_telegram("line\n" * 5000)
    assert len(chunks) >= 2 and all(len(ch) <= 4096 for ch in chunks)
    # The "+N more" tail (6th entry from RunResult) must not be sliced off.
    errors = [f"err {i}" for i in range(5)] + ["… and 3 more error(s)"]
    assert "and 3 more" in report_to_telegram([], errors=errors)


# ── Health ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,expected", [(200, "ok"), (401, "unauthorized"), (503, "unreachable")])
def test_health_states(monkeypatch, status, expected):
    import app.services.ollama_client as oc

    class Resp:
        status_code = status

    monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: Resp())
    assert oc.OllamaClient().health() == expected
