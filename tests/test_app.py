"""App-level tests: auth roundtrip, multi-tenant isolation, the matcher pipeline
(scoring, dedup-on-rerun, error-marker skip, descriptionless skip), reporter
thresholds, and the Ollama health states. No network: the LLM client is faked
and the scraper is monkeypatched."""
from __future__ import annotations

import json
from datetime import timedelta
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
        if "results" in schema.get("properties", {}):
            n = user.count("### Posting ")
            return {
                "results": [
                    {
                        "id": i + 1, "matches_requirements": True,
                        "match_score": self.score, "win_probability": 50,
                        "reasoning": "Solid fit.", "strengths": ["Python"], "gaps": [],
                    }
                    for i in range(n)
                ]
            }
        return {
            "matches_requirements": True, "match_score": self.score,
            "win_probability": 50, "reasoning": "Solid fit.",
            "strengths": ["Python"], "gaps": [],
        }


class FailClient:
    model = "fake-fail"

    def chat_json(self, *a, **k):
        raise OllamaError("Ollama returned 500: server error")


class BudgetClient:
    """Scoring stub that simulates Ollama quota/budget exhaustion (HTTP 402)."""
    model = "fake-budget"

    def chat_json(self, *a, **k):
        from app.services.ollama_client import OllamaBudgetError

        raise OllamaBudgetError("Ollama budget/quota exhausted (HTTP 402): no remaining credits")


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


# ── Schema reconcile (micro-migration) ───────────────────────────────────────
def test_reconcile_schema_adds_column_relaxes_notnull_and_keeps_data():
    """An older on-disk DB whose ``companies`` table predates ``preset_key`` and
    still has ``user_id NOT NULL`` is brought up to the current model: the column
    and its unique index are added, the NOT NULL is relaxed, existing rows survive,
    and a preset row (user_id NULL) can then be inserted."""
    import tempfile
    from sqlalchemy import create_engine, inspect, text
    from app.db import _reconcile_schema

    path = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    try:
        # Stand up an "old" companies table (no preset_key, user_id NOT NULL), then
        # let create_all add the remaining current tables around it.
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE companies ("
                " id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " name VARCHAR(255), careers_url VARCHAR(1024), ats_type VARCHAR(32),"
                " ats_token VARCHAR(255), location_hint VARCHAR(255),"
                " is_active BOOLEAN, created_at DATETIME, last_scraped_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO companies (id, user_id, name, ats_type)"
                " VALUES (1, 42, 'Acme', 'greenhouse')"
            ))
        models.Base.metadata.create_all(eng)  # creates the other tables; skips companies

        _reconcile_schema(eng)

        insp = inspect(eng)
        cols = {c["name"]: c for c in insp.get_columns("companies")}
        assert "preset_key" in cols                       # column added
        assert cols["user_id"]["nullable"] is True        # NOT NULL relaxed
        assert any(ix["name"] == "ix_companies_preset_key" for ix in insp.get_indexes("companies"))

        with eng.begin() as conn:
            # Existing row preserved through the rebuild...
            assert conn.execute(text("SELECT name FROM companies WHERE id=1")).scalar() == "Acme"
            # ...and a preset row (user_id NULL) now inserts, which the old schema forbade.
            conn.execute(text(
                "INSERT INTO companies (id, user_id, preset_key, name) VALUES (2, NULL, 'anthropic', 'Anthropic')"
            ))
            assert conn.execute(text("SELECT COUNT(*) FROM companies")).scalar() == 2

        # Idempotent: a second pass is a no-op (no error, same shape).
        _reconcile_schema(eng)
    finally:
        eng.dispose()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


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


def test_resume_preview_file_and_content():
    """The résumé title's in-page preview is served two ways: the original file
    (inline) and the extracted text fallback — both owner-scoped."""
    with TestClient(app) as c:
        ha = {"Authorization": f"Bearer {_register(c, 'pv@x.com').json()['access_token']}"}
        rid = c.post("/api/resumes", headers=ha,
                     files={"file": ("cv.txt", b"Senior Python engineer", "text/plain")}).json()["id"]

        # Original file, served inline (not as an attachment) with its content type.
        f = c.get(f"/api/resumes/{rid}/file", headers=ha)
        assert f.status_code == 200 and f.content == b"Senior Python engineer"
        assert f.headers["content-type"].startswith("text/plain")
        assert "inline" in f.headers.get("content-disposition", "")

        # Extracted-text fallback (for formats the browser can't render).
        content = c.get(f"/api/resumes/{rid}/content", headers=ha).json()
        assert content["filename"] == "cv.txt" and content["content_text"] == "Senior Python engineer"

        # Another user can't preview it.
        hb = {"Authorization": f"Bearer {_register(c, 'pv2@x.com').json()['access_token']}"}
        assert c.get(f"/api/resumes/{rid}/file", headers=hb).status_code == 404
        assert c.get(f"/api/resumes/{rid}/content", headers=hb).status_code == 404
    # Auth required.
    with TestClient(app) as c:
        assert c.get("/api/resumes/1/file").status_code == 401
        assert c.get("/api/resumes/1/content").status_code == 401


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


def test_failure_persists_marker_and_is_not_rebilled(monkeypatch):
    # score_max_attempts=1 → a failure is terminal immediately (no retry), so this
    # exercises the terminal-marker path; bounded-retry behaviour is covered in
    # tests/test_scoring_queue.py.
    monkeypatch.setattr(matcher.settings, "score_max_attempts", 1)
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=FailClient(), filter_client=FilterPass())
    assert res.scored == 0 and res.errors  # failure surfaced
    with session_scope() as db:
        markers = list(db.scalars(
            matcher.select(models.MatchResult).where(models.MatchResult.model == matcher.ERROR_MODEL)))
        assert len(markers) == 1 and markers[0].attempts == 1
    # A terminal marker (attempts == limit) settles the pair: re-run must not call
    # either model again (it's in the `already` set).
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=BoomClient(), filter_client=BoomClient())
    # --retry-failed clears markers so they re-score.
    with session_scope() as db:
        assert matcher.clear_failed_markers(db, user_id=uid) == 1


def test_budget_exhaustion_stops_run_and_auto_recovers():
    """An exhausted Ollama budget surfaces a clear warning, writes NO error-markers
    (unlike a generic failure), and lets the postings re-score on the next run once
    quota is back — without a manual --retry-failed."""
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid),
                                   client=BudgetClient(), filter_client=FilterPass())
    assert res.scored == 0
    assert any("budget" in e.lower() or "quota" in e.lower() for e in res.errors)
    # The half-processed batch was rolled back: no markers, no rows at all — so the
    # score-dedup `already` set won't skip these postings next run.
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.MatchResult))) == []
    # Quota restored: the same postings now score (proving they weren't skipped).
    with session_scope() as db:
        res2 = matcher.run_for_user(db, db.get(models.User, uid),
                                    client=GoodClient(), filter_client=FilterPass())
    assert res2.scored >= 1


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


def test_google_coverage_complete_vs_capped(monkeypatch):
    """Google paging reports completeness for removal reconciliation: 'complete' only
    when an empty page is reached within the cap (whole board seen); capping out at
    max_pages leaves it incomplete, so absence can't be read as removed."""
    import json
    import re
    from app.services import scraper

    base = "https://www.google.com/about/careers/applications/jobs/results/"

    def page_for(jid):
        gid = str(900000000000000 + jid)  # Google ids are long numeric strings (>=15 digits)
        rec = [gid, f"Role {jid}", f"{base}{gid}",
               [None, "<p>Build things</p>"], [None, ""], "projects/x", None, "Google", None,
               [["NYC, USA", ["NYC, USA"], "NYC", None, "NY", "US"]]]
        return "AF_initDataCallback({key:'ds:0', data:" + json.dumps([[rec]]) + ", sideChannel: {}});"

    # Reaches an empty page within the cap → complete.
    monkeypatch.setattr(scraper, "_fetch_text",
        lambda url: page_for(1) if "page=1" in url else "<html>no jobs</html>")
    out, complete = scraper._google_paged(base, max_pages=5)
    assert complete is True and len(out) == 1

    # Every page has a fresh role and we exhaust max_pages → not complete.
    monkeypatch.setattr(scraper, "_fetch_text",
        lambda url: page_for(100 + int(re.search(r"page=(\d+)", url).group(1))))
    out2, complete2 = scraper._google_paged(base, max_pages=3)
    assert complete2 is False and len(out2) == 3


def test_eightfold_description_extracted_from_jsonld(monkeypatch):
    """Eightfold's search API carries no description; we pull it from the job page's
    schema.org JobPosting JSON-LD (HTML stripped to text). Failures never raise."""
    from app.services import scraper

    page = ('<html><head><script type="application/ld+json">'
            '{"@type":"JobPosting","description":"<p>Build <b>great</b> systems</p>"}'
            '</script></head><body>x</body></html>')
    monkeypatch.setattr(scraper, "_fetch_text", lambda url: page)
    assert scraper._eightfold_description("https://j/careers/job/1") == "Build great systems"

    # JSON-LD as a list: take the entry that has a description.
    listed = ('<script type="application/ld+json">'
              '[{"@type":"WebSite"},{"@type":"JobPosting","description":"Role <i>info</i>"}]</script>')
    monkeypatch.setattr(scraper, "_fetch_text", lambda url: listed)
    assert scraper._eightfold_description("u") == "Role info"

    monkeypatch.setattr(scraper, "_fetch_text", lambda url: "<html>no jsonld</html>")
    assert scraper._eightfold_description("u") is None

    def boom(url):
        raise scraper.ScrapeError("dead host")
    monkeypatch.setattr(scraper, "_fetch_text", boom)
    assert scraper._eightfold_description("u") is None  # never raises


def test_fetch_eightfold_descriptions_caps_and_dedups(monkeypatch):
    from app.services import scraper

    seen: list[str] = []

    def fake_one(url):
        seen.append(url)
        return None if url.endswith("none") else f"desc {url}"

    monkeypatch.setattr(scraper, "_eightfold_description", fake_one)
    urls = ["u1", "u2", "u2", "u3none", "u4"]  # u2 repeated, u3none resolves to nothing
    out = scraper.fetch_eightfold_descriptions(urls, cap=3, max_workers=2)
    # cap applies over the de-duped order [u1, u2, u3none, u4] -> first 3 fetched.
    assert set(seen) == {"u1", "u2", "u3none"}
    assert out == {"u1": "desc u1", "u2": "desc u2"}  # u3none contributes nothing
    assert scraper.fetch_eightfold_descriptions(urls, cap=0) == {}  # disabled


def test_eightfold_falls_back_to_smartapply_on_pcsx_403(monkeypatch):
    """Eightfold tenants serve PCSX *or* SmartApply and 403 the other. PCSX is tried
    first; on a 403 the adapter switches to SmartApply (Netflix) and maps its differing
    field names (canonicalPositionUrl, t_create/t_update)."""
    import time
    from app.services import scraper

    recent = int(time.time()) - 3600  # within the age cutoff so paging continues
    calls: list[str] = []

    def fake_json(url: str) -> dict:
        calls.append(url)
        if "pcsx" in url:
            raise scraper._EightfoldFlavourUnavailable()
        if "start=0" in url:
            return {"positions": [{
                "id": 790, "name": "AI Engineer",
                "locations": ["USA - Remote"], "department": "Data",
                "work_location_option": "remote",
                "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790",
                "t_create": recent,
            }]}
        return {"positions": []}  # next page empty -> whole board seen

    monkeypatch.setattr(scraper, "_eightfold_json", fake_json)
    out, coverage = scraper._eightfold_paged(
        "https://explore.jobs.netflix.net/careers", "netflix.com", max_pages=3)

    assert any("pcsx" in u for u in calls) and any("apply/v2/jobs" in u for u in calls)
    assert coverage == "full"  # reached an empty page within the cap
    assert len(out) == 1
    p = out[0]
    assert p.external_id == "790" and p.title == "AI Engineer"
    assert p.location == "USA - Remote" and p.department == "Data"
    assert p.url == "https://explore.jobs.netflix.net/careers/job/790"
    assert p.posted_at is not None  # t_create parsed (epoch seconds)


def test_amazon_scrape_parses_json_and_pages_newest_first(monkeypatch):
    """amazon.jobs has no third-party ATS; the adapter pages /en/search.json by offset,
    keys on the stable id_icims, and folds the inline description + qualifications in."""
    from app.services import scraper

    def fake_json(url: str) -> dict:
        if "offset=0" in url:
            return {"jobs": [{
                "id_icims": "111", "id": "uuid-churns", "title": " SDE II ",
                "location": "Seattle, WA", "job_category": "Software Development",
                "job_schedule_type": "Full-Time", "job_path": "/en/jobs/111/sde-ii",
                "description": "<p>Build <b>things</b></p>",
                "basic_qualifications": "<p>CS degree</p>",
                "posted_date": "June 18, 2026",
            }]}
        return {"jobs": []}  # next page empty -> stop

    monkeypatch.setattr(scraper, "_fetch_json", fake_json)
    out = scraper.scrape_amazon(max_pages=5)

    assert len(out) == 1
    p = out[0]
    assert p.external_id == "111"  # id_icims, not the churning UUID
    assert p.title == "SDE II"
    assert p.url == "https://www.amazon.jobs/en/jobs/111/sde-ii"
    assert p.department == "Software Development"
    assert p.description and "things" in p.description and "CS degree" in p.description
    assert p.posted_at and p.posted_at.year == 2026 and p.posted_at.month == 6


def test_apple_scrape_uses_csrf_then_pages(monkeypatch):
    """Apple's search needs a CSRF token (fetched once) then pages the POST search,
    parsing the inline jobSummary as the description and building the details URL."""
    from app.services import scraper

    monkeypatch.setattr(scraper, "_apple_csrf_token", lambda client: "tok")

    def fake_page(client, token, page):
        assert token == "tok"
        if page == 0:
            return {"searchResults": [{
                "positionId": "200", "postingTitle": "Machine Learning Engineer",
                "transformedPostingTitle": "machine-learning-engineer",
                "locations": [{"name": "Cupertino"}, {"city": "Austin"}],
                "team": {"teamName": "Hardware"},
                "jobSummary": "Do <b>great</b> work",
                "postDateInGMT": "2026-06-18T04:51:53.395483689Z",
            }]}
        return {"searchResults": []}  # next page empty -> stop

    monkeypatch.setattr(scraper, "_apple_search_page", fake_page)
    out = scraper.scrape_apple(max_pages=3)

    assert len(out) == 1
    p = out[0]
    assert p.external_id == "200"
    assert p.title == "Machine Learning Engineer"
    assert p.url == "https://jobs.apple.com/en-us/details/200/machine-learning-engineer"
    assert p.location == "Cupertino; Austin"
    assert p.department == "Hardware"
    assert p.description == "Do great work"
    assert p.posted_at and p.posted_at.year == 2026  # nanosecond ISO parses on 3.11


def test_new_company_presets_registered_and_dispatchable():
    """The five new presets use ats_types the scraper actually dispatches on, and the
    account gating matches each board (Amazon's portal needs a sign-in; Apple's does
    not gate browsing)."""
    from app.company_presets import PRESETS_BY_KEY

    supported = {"greenhouse", "lever", "ashby", "eightfold", "google",
                 "amazon", "apple", "html", "auto"}
    expected = {
        "airbnb": "greenhouse", "databricks": "greenhouse",
        "netflix": "eightfold", "amazon": "amazon", "apple": "apple",
    }
    for key, ats in expected.items():
        p = PRESETS_BY_KEY[key]
        assert p.ats_type == ats and p.ats_type in supported
    assert PRESETS_BY_KEY["airbnb"].ats_token == "airbnb"
    assert PRESETS_BY_KEY["databricks"].ats_token == "databricks"
    assert PRESETS_BY_KEY["netflix"].ats_token == "netflix.com"
    assert PRESETS_BY_KEY["amazon"].requires_account is True
    assert PRESETS_BY_KEY["apple"].requires_account is False


def test_eightfold_upsert_enriches_new_and_backfills_existing(monkeypatch):
    """_upsert_positions fetches detail-page descriptions for eightfold: new rows are
    enriched, and a previously-stored description-less row is healed (backfill)."""
    from app.services import scraper
    from app.services.matcher import _upsert_positions

    with session_scope() as db:
        company = models.Company(name="NV", ats_type="eightfold", ats_token="nv.com",
                                 careers_url="https://jobs.nv.com/careers")
        db.add(company); db.flush()
        db.add(models.Position(company_id=company.id, external_id="old1", title="Old Role",
                               url="https://jobs.nv.com/careers/job/old1", description=None))
        db.flush()
        cid = company.id

    scraped = [
        scraper.ScrapedPosition(external_id="old1", title="Old Role",
                                url="https://jobs.nv.com/careers/job/old1"),  # existing, empty
        scraper.ScrapedPosition(external_id="new1", title="New Role",
                                url="https://jobs.nv.com/careers/job/new1"),  # brand new
    ]
    monkeypatch.setattr(scraper, "scrape_company", lambda company: scraped)
    captured = {}

    def fake_fetch(urls, cap, max_workers=None):
        captured["urls"] = list(urls)
        captured["cap"] = cap
        return {u: f"Full description for {u}" for u in urls}

    monkeypatch.setattr(scraper, "fetch_eightfold_descriptions", fake_fetch)

    with session_scope() as db:
        new_positions, errs = _upsert_positions(db, db.get(models.Company, cid))
        assert not errs
        assert [p.external_id for p in new_positions] == ["new1"]
        assert "Full description" in (new_positions[0].description or "")  # new row enriched
        healed = db.scalar(matcher.select(models.Position).where(
            models.Position.company_id == cid, models.Position.external_id == "old1"))
        assert "Full description" in (healed.description or "")  # existing row backfilled

    assert set(captured["urls"]) == {  # both empties submitted, newest-first order preserved
        "https://jobs.nv.com/careers/job/old1", "https://jobs.nv.com/careers/job/new1"}
    assert captured["cap"] == 150  # default per-crawl cap from settings


def test_migrate_db_copies_schema_and_all_rows(tmp_path):
    """`jobscout migrate-db` recreates the schema on the target and copies every
    table's rows (FK order preserved). Exercised SQLite->SQLite; the Postgres
    sequence-reset step is dialect-guarded and simply skipped here."""
    import argparse
    from sqlalchemy import create_engine, func, select
    from app import models
    from app.cli import cmd_migrate_db
    from app.db import engine as source_engine

    # Seed related rows across several tables (users/resumes/companies/positions/
    # interests) so the copy has FK chains to preserve.
    with session_scope() as db:
        _seed_user(db, email="m1@x.com")
        _seed_user(db, email="m2@x.com")

    target_url = "sqlite:///" + str(tmp_path / "target.db")
    rc = cmd_migrate_db(argparse.Namespace(target=target_url, drop=True, batch=1000))
    assert rc == 0

    target_engine = create_engine(target_url)
    md = models.Base.metadata
    try:
        with source_engine.connect() as s, target_engine.connect() as t:
            for table in md.sorted_tables:
                src_n = s.execute(select(func.count()).select_from(table)).scalar()
                tgt_n = t.execute(select(func.count()).select_from(table)).scalar()
                assert src_n == tgt_n, f"{table.name}: {src_n} != {tgt_n}"
            # Spot-check real content survived, not just counts.
            emails = set(t.execute(select(models.User.__table__.c.email)).scalars())
            assert {"m1@x.com", "m2@x.com"} <= emails
    finally:
        target_engine.dispose()

    # Same-URL guard and missing-target guard return non-zero without copying.
    assert cmd_migrate_db(argparse.Namespace(target=None, drop=False, batch=1000)) == 2


def test_backfill_descriptions_cli_heals_eightfold(monkeypatch):
    """`jobscout backfill-descriptions` fetches descriptions for stored eightfold
    postings that lack one (and only those), and leaves others/ATSes untouched."""
    import argparse
    from app.cli import cmd_backfill_descriptions
    from app.services import scraper

    with session_scope() as db:
        nv = models.Company(preset_key="nvidia", name="NVIDIA", ats_type="eightfold",
                            ats_token="nvidia.com", careers_url="https://jobs.nv.com/careers")
        gh = models.Company(name="GH", ats_type="greenhouse")  # must be ignored
        db.add_all([nv, gh]); db.flush()
        db.add(models.Position(company_id=nv.id, external_id="e1", title="A",
                               url="https://jobs.nv.com/careers/job/e1", description=None))
        db.add(models.Position(company_id=nv.id, external_id="e2", title="B",
                               url="https://jobs.nv.com/careers/job/e2", description=""))
        db.add(models.Position(company_id=nv.id, external_id="e3", title="C",
                               url="https://jobs.nv.com/careers/job/e3", description="already here"))
        db.add(models.Position(company_id=gh.id, external_id="g1", title="G",
                               url="https://gh/g1", description=None))  # non-eightfold: skip
        db.flush()
        nv_id, gh_id = nv.id, gh.id

    captured = {}

    def fake_fetch(urls, cap, max_workers=None):
        captured["urls"] = list(urls)
        return {u: f"desc::{u.rsplit('/', 1)[-1]}" for u in urls}

    monkeypatch.setattr(scraper, "fetch_eightfold_descriptions", fake_fetch)

    rc = cmd_backfill_descriptions(argparse.Namespace(company=None, limit=0))
    assert rc == 0
    # Only the two empty eightfold rows were fetched (not the filled one, not GH).
    assert set(captured["urls"]) == {
        "https://jobs.nv.com/careers/job/e1", "https://jobs.nv.com/careers/job/e2"}
    with session_scope() as db:
        by_ext = {p.external_id: p for p in db.scalars(
            matcher.select(models.Position).where(models.Position.company_id == nv_id))}
        assert by_ext["e1"].description == "desc::e1" and by_ext["e2"].description == "desc::e2"
        assert by_ext["e3"].description == "already here"  # untouched
        gh_pos = db.scalar(matcher.select(models.Position).where(
            models.Position.company_id == gh_id))
        assert gh_pos.description is None  # non-eightfold never fetched

    # --company filter matches by preset key; an unknown one is a clean no-op exit.
    assert cmd_backfill_descriptions(argparse.Namespace(company="nvidia", limit=0)) == 0
    assert cmd_backfill_descriptions(argparse.Namespace(company="nope", limit=0)) == 1


def test_non_eightfold_skips_description_fetch(monkeypatch):
    """A Greenhouse (or other) company never triggers the detail-page fetch — those
    ATSes return descriptions inline, so a description-less row stays as-is."""
    from app.services import scraper
    from app.services.matcher import _upsert_positions

    with session_scope() as db:
        company = models.Company(name="GH", ats_type="greenhouse", ats_token="gh")
        db.add(company); db.flush()
        cid = company.id
    monkeypatch.setattr(scraper, "scrape_company", lambda company: [
        scraper.ScrapedPosition(external_id="g1", title="Role", url="https://x/g1", description=None)])

    def fail(*a, **k):
        raise AssertionError("detail fetch must not run for non-eightfold ATS")

    monkeypatch.setattr(scraper, "fetch_eightfold_descriptions", fail)
    with session_scope() as db:
        new_positions, errs = _upsert_positions(db, db.get(models.Company, cid))
        assert not errs and new_positions[0].description is None


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


# ── Position availability / removal tracking ─────────────────────────────────
def _gh_result(*ext_ids, live=None):
    """A ScrapeResult for a Greenhouse-style (full-board) crawl: a described posting
    per id, with ``live`` defaulting to exactly those ids on the board."""
    from app.services import scraper

    positions = [
        scraper.ScrapedPosition(external_id=e, title=f"Backend Engineer {e}",
                                location="Remote", description="Build APIs")
        for e in ext_ids
    ]
    ids = set(ext_ids) if live is None else live
    return scraper.ScrapeResult(positions=positions, live_external_ids=ids, coverage="full")


def test_reconcile_marks_removed_then_clears_on_reappear(monkeypatch):
    """An authoritative crawl marks a stored posting removed once it leaves the
    board, and clears the flag if it later reappears."""
    from app.services import scraper
    from app.services.matcher import _upsert_positions

    with session_scope() as db:
        company = models.Company(name="GH", ats_type="greenhouse", ats_token="gh")
        db.add(company); db.flush()
        cid = company.id

    def crawl(company):
        return crawl.result
    monkeypatch.setattr(scraper, "scrape_company", crawl)

    crawl.result = _gh_result("a", "b")              # both live
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))

    crawl.result = _gh_result("a")                   # "b" gone from the board
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))
    with session_scope() as db:
        by_id = {p.external_id: p for p in db.scalars(
            matcher.select(models.Position).where(models.Position.company_id == cid))}
        assert by_id["a"].removed_at is None
        assert by_id["b"].removed_at is not None

    crawl.result = _gh_result("a", "b")              # "b" relisted
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))
    with session_scope() as db:
        b = db.scalar(matcher.select(models.Position).where(models.Position.external_id == "b"))
        assert b.removed_at is None


def test_reconcile_skips_partial_and_empty_scrapes(monkeypatch):
    """A partial scrape (coverage=None) and a suspect empty board both leave stored
    postings untouched — neither can prove a removal."""
    from app.services import scraper
    from app.services.matcher import _upsert_positions

    with session_scope() as db:
        company = models.Company(name="GH", ats_type="greenhouse", ats_token="gh")
        db.add(company); db.flush()
        cid = company.id
    monkeypatch.setattr(scraper, "scrape_company", lambda c: _gh_result("a", "b"))
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))

    # Partial coverage: only "a" on the (partial) board, coverage=None → no removals.
    monkeypatch.setattr(scraper, "scrape_company", lambda c: scraper.ScrapeResult(
        positions=[], live_external_ids={"a"}, coverage=None))
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))
    # Empty live set even under "full" coverage: treated as a flukey fetch, never
    # mass-closes the company.
    monkeypatch.setattr(scraper, "scrape_company", lambda c: scraper.ScrapeResult(
        positions=[], live_external_ids=set(), coverage="full"))
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))
    with session_scope() as db:
        removed = [p for p in db.scalars(
            matcher.select(models.Position).where(models.Position.company_id == cid))
            if p.removed_at is not None]
        assert removed == []


def test_reconcile_floor_scopes_removals_to_covered_window(monkeypatch):
    """With datetime (floor) coverage — a newest-first walk that reached the age
    cutoff — only absent postings newer than the floor are closed; the older tail,
    outside what the scrape actually covered, is left untouched."""
    from app.services import scraper
    from app.services.matcher import _upsert_positions

    now = matcher.utcnow()
    floor = now - timedelta(days=90)
    with session_scope() as db:
        company = models.Company(name="NV", ats_type="eightfold",
                                 careers_url="https://jobs.nvidia.com/careers")
        db.add(company); db.flush()
        cid = company.id
        for ext, age in [("live", 5), ("recent", 10), ("ancient", 200)]:
            db.add(models.Position(company_id=cid, external_id=ext, title=ext,
                                   description="d", posted_at=now - timedelta(days=age)))
    # Only "live" is still on the board; coverage is complete down to the floor.
    monkeypatch.setattr(scraper, "scrape_company", lambda c: scraper.ScrapeResult(
        positions=[], live_external_ids={"live"}, coverage=floor))
    with session_scope() as db:
        _upsert_positions(db, db.get(models.Company, cid))
    with session_scope() as db:
        by_id = {p.external_id: p for p in db.scalars(
            matcher.select(models.Position).where(models.Position.company_id == cid))}
        assert by_id["live"].removed_at is None        # on the board → stays
        assert by_id["recent"].removed_at is not None  # absent, within window → closed
        assert by_id["ancient"].removed_at is None     # absent, older than floor → kept


def test_removed_position_hidden_unless_applied(monkeypatch):
    """A removed posting drops out of the job list / detail / visibility gate — but
    one the user applied to stays, flagged removed, so its record survives."""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)  # custom greenhouse company + posting "1" + interest
    monkeypatch.setattr(matcher.scraper, "scrape_company",
                        lambda c: _gh_result("1", "2"))
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid),
                             client=GoodClient(), filter_client=FilterPass())
    with session_scope() as db:
        pid2 = db.scalar(matcher.select(models.Position.id)
                         .where(models.Position.external_id == "2"))

    # Close posting "2".
    with session_scope() as db:
        db.get(models.Position, pid2).removed_at = matcher.utcnow()
    with session_scope() as db:
        user = db.get(models.User, uid)
        items, _ = reporter.build_job_list(db, user, category="matching")
        assert pid2 not in {i["position_id"] for i in items}
        assert reporter.position_visible(db, user, pid2) is False
        assert reporter.build_position_detail(db, user, pid2) is None

    # Apply to it → it returns to the list, badged removed, and the detail opens.
    with session_scope() as db:
        db.add(models.Application(user_id=uid, position_id=pid2))
    with session_scope() as db:
        user = db.get(models.User, uid)
        items, _ = reporter.build_job_list(db, user, category="matching")
        row = next(i for i in items if i["position_id"] == pid2)
        assert row["removed"] is True
        assert reporter.position_visible(db, user, pid2) is True
        assert reporter.build_position_detail(db, user, pid2)["removed"] is True


def test_removed_positions_are_not_scored():
    """A closed posting is never (re)scored or counted as pending."""
    with session_scope() as db:
        uid = _seed_user(db)
        db.scalar(matcher.select(models.Position)).removed_at = matcher.utcnow()
    with session_scope() as db:
        assert matcher.count_pending(db, db.get(models.User, uid)) == 0
    # BoomClient fails loudly if either model is called → proves the pair was skipped.
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid),
                                   client=BoomClient(), filter_client=BoomClient())
        assert res.scored == 0
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.MatchResult))) == []


def test_apply_and_kit_rejected_on_removed_posting():
    """The write actions refuse a closed posting: re-marking applied and generating a
    kit both 409, even when the user already has an application/match on record."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'rm@x.com').json()['access_token']}"}
        with session_scope() as db:
            user = db.scalar(matcher.select(models.User).where(models.User.email == "rm@x.com"))
            company = models.Company(user_id=user.id, name="Acme",
                                     ats_type="greenhouse", ats_token="acme")
            db.add(company); db.flush()
            pos = models.Position(company_id=company.id, external_id="x", title="Backend",
                                  description="d", removed_at=matcher.utcnow())
            db.add(pos); db.flush()
            db.add(models.Resume(user_id=user.id, filename="r", content_text="py", is_active=True))
            db.add(models.MatchResult(user_id=user.id, position_id=pos.id,
                                      passed_filter=True, match_score=80, win_probability=50))
            db.add(models.Application(user_id=user.id, position_id=pos.id))  # applied before it closed
            pid = pos.id
        assert c.post(f"/api/applications/{pid}", headers=h).status_code == 409
        assert c.post(f"/api/positions/{pid}/kit", headers=h).status_code == 409


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


def test_scoring_batch_scores_multiple_survivors_in_one_call(monkeypatch):
    """When several postings survive the cheap filter, the expensive model gets
    one batched request instead of one request per posting."""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)
    fresh = [scraper.ScrapedPosition(external_id=f"b{i}", title=f"Backend Engineer {i}",
                                     location="Remote", description="Build APIs") for i in range(4)]
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: fresh)
    monkeypatch.setattr(matcher.settings, "score_filter_batch_size", 10)
    monkeypatch.setattr(matcher.settings, "score_batch_size", 10)
    good = GoodClient()
    flt = FilterPass()
    with session_scope() as db:
        res = matcher.run_for_user(db, db.get(models.User, uid), client=good, filter_client=flt)
    assert flt.calls == 1
    assert res.scored == 5
    assert good.calls == 1


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


def test_run_drains_whole_backlog_to_zero(monkeypatch):
    """There is no per-run cap: one run scores the entire backlog, and count_pending
    drops to 0. (Replaces the old 'capped, click Run again' behavior.)"""
    from app.services import scraper

    with session_scope() as db:
        uid = _seed_user(db)
    fresh = [scraper.ScrapedPosition(external_id=f"f{i}", title="Backend Engineer",
                                     location="Remote", description="Build APIs") for i in range(4)]
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: fresh)
    good = GoodClient()
    with session_scope() as db:
        user = db.get(models.User, uid)
        # Backlog before a run = (4 fresh + 1 seeded) described positions × 1 interest.
        res = matcher.run_for_user(db, user, client=good, filter_client=FilterPass())
        assert res.scored == 5
        assert not any("cap" in e.lower() for e in res.errors)  # no cap message anymore
        assert matcher.count_pending(db, user) == 0  # fully drained


def test_excluded_pairs_leave_the_backlog(monkeypatch):
    """A keyword-excluded pair gets an 'excluded' marker row (so the backlog
    converges to 0 and the drain terminates) instead of being silently dropped."""
    with session_scope() as db:
        uid = _seed_user(db)
        # Exclude anything mentioning "backend" — the seeded position's title.
        db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid)
                  ).exclude_keywords = "backend"
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: [])
    with session_scope() as db:
        user = db.get(models.User, uid)
        res = matcher.run_for_user(db, user, client=BoomClient(), filter_client=BoomClient())
        assert res.scored == 0  # excluded before any LLM call (BoomClient never raises)
        assert matcher.count_pending(db, user) == 0  # the exclude marker emptied the backlog
        markers = list(db.scalars(matcher.select(models.MatchResult).where(
            models.MatchResult.model == matcher.EXCLUDED_MODEL)))
        assert len(markers) == 1 and markers[0].passed_filter is False


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
            db,
            user,
            SimpleNamespace(
                new_positions=1,
                scored=1,
                filtered=0,
                errors=[
                    "Reached this run's scoring cap (50) — more postings remain "
                    "unscored. Click Run again to continue, or raise JOBSCOUT_SCORE_MAX_PER_RUN."
                ],
            ),
        )

        assert reporter.job_list_items(first)[0]["title"] == "Backend Engineer"
        latest_items = reporter.job_list_items(second)
        assert [item["title"] for item in latest_items] == ["Principal Backend", "Backend Engineer"]
        assert "candidate evaluation cap" in reporter.job_list_errors(second)[0]
        assert "remain unevaluated" in reporter.job_list_errors(second)[0]

        from app.routers.reports import get_job_list

        response = get_job_list(second.id, limit=1, offset=0, category="matching", user=user, db=db)
        assert response.total == 2
        assert len(response.items) == 1
        assert response.items[0].title == "Principal Backend"
        # offset paginates over the stored snapshot items.
        page2 = get_job_list(second.id, limit=1, offset=1, category="matching", user=user, db=db)
        assert page2.items[0].title == "Backend Engineer"


# ── Telegram ─────────────────────────────────────────────────────────────────
def test_telegram_link_binds_chat_and_burns_code(monkeypatch):
    """On-demand linking reads the bot's /start <code> once, binds the chat, and
    burns the one-time code so a leaked code can't re-bind the chat."""
    from app.auth import create_access_token
    from app.services import evaluator
    import app.services.telegram_bot as tg

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        u = models.User(email="t@x.com", hashed_password="h",
                        telegram_bot_token="bot-123", telegram_link_code="abc123")
        db.add(u)
        db.flush()
        uid = u.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        # Nothing DMed yet -> not linked, code intact.
        monkeypatch.setattr(tg, "find_start_chat", lambda token, code: None)
        r = c.post("/api/telegram-config/link", headers=h).json()
        assert r["ok"] is False and "abc123" in r["detail"]
        assert c.get("/api/telegram-config", headers=h).json()["linked"] is False

        # User DMs /start abc123 -> chat bound (only for the matching code), code burned.
        monkeypatch.setattr(tg, "find_start_chat",
                            lambda token, code: "555" if code == "abc123" else None)
        ok = c.post("/api/telegram-config/link", headers=h).json()
        assert ok["ok"] is True and "555" in ok["detail"]
        cfg = c.get("/api/telegram-config", headers=h).json()
        assert cfg["linked"] is True and cfg["chat_id"] == "555"
    with session_scope() as db:
        u = db.get(models.User, uid)
        assert u.telegram_chat_id == "555" and u.telegram_link_code is None


def test_telegram_config_saves_token_and_tests(monkeypatch):
    """Token is saved (never echoed), the link code is minted on demand, and Test
    validates the token then only sends once a chat is linked."""
    from app.auth import create_access_token
    from app.services import evaluator
    import app.services.telegram_bot as tg

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db, email="tg@x.com")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    sent = {"called": False}
    monkeypatch.setattr(tg, "get_bot_username", lambda token: (True, "scout_bot"))

    def _send(token, chat_id, text):
        sent["called"] = True
        return True

    monkeypatch.setattr(tg, "send_message", _send)
    with TestClient(app) as c:
        # Fresh user: no token, but a link code is minted so there's one to DM.
        first = c.get("/api/telegram-config", headers=h).json()
        assert first["has_token"] is False and first["linked"] is False and first["link_code"]

        # Test with no token reports it and sends nothing.
        none = c.post("/api/telegram-config/test", headers=h).json()
        assert none["ok"] is False and "bot token" in none["detail"].lower()
        assert sent["called"] is False

        # Save a token (never echoed back).
        saved = c.put("/api/telegram-config", headers=h, json={"bot_token": "bot-xyz"}).json()
        assert saved["has_token"] is True and "bot_token" not in saved

        # Valid token but no chat linked yet -> Test reports it, still no send.
        nochat = c.post("/api/telegram-config/test", headers=h).json()
        assert nochat["ok"] is False and "linked" in nochat["detail"] and sent["called"] is False

        # Link a chat, then a successful Test actually sends via the user's bot.
        monkeypatch.setattr(tg, "find_start_chat", lambda token, code: "777")
        c.post("/api/telegram-config/link", headers=h)
        ok = c.post("/api/telegram-config/test", headers=h).json()
        assert ok["ok"] is True and sent["called"] is True and "scout_bot" in ok["detail"]

        # Blank token on re-save keeps the stored one.
        c.put("/api/telegram-config", headers=h, json={})
    with session_scope() as db:
        assert db.get(models.User, uid).telegram_bot_token == "bot-xyz"
    assert c.get("/api/telegram-config").status_code == 401  # auth required


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


# ── Async evaluation drain ───────────────────────────────────────────────────
def test_count_pending_ignores_undescribed():
    """Description-less postings are never scored, so they're not in the backlog."""
    with session_scope() as db:
        uid = _seed_user(db, description=None)
    with session_scope() as db:
        assert matcher.count_pending(db, db.get(models.User, uid)) == 0


def test_evaluator_drains_backlog_and_records_snapshot(monkeypatch):
    """The background drain body scores the whole backlog to completion and records
    one snapshot. (Run synchronously here for determinism.)"""
    from app.services import evaluator

    with session_scope() as db:
        uid = _seed_user(db)  # 1 described position × 1 interest = backlog of 1
    monkeypatch.setattr(matcher.llm, "clients_for_user", lambda db, user: (GoodClient(), FilterPass()))

    evaluator._run(uid)

    with session_scope() as db:
        user = db.get(models.User, uid)
        assert matcher.count_pending(db, user) == 0
        snaps = list(db.scalars(matcher.select(models.JobListSnapshot)
                                .where(models.JobListSnapshot.user_id == uid)))
        assert len(snaps) == 1 and snaps[0].scored == 1


def test_run_endpoint_returns_immediately_with_pending(monkeypatch):
    """/api/run scrapes, hands scoring to the background worker, and returns the
    pending backlog without scoring inline (scored=0)."""
    from app.services import evaluator

    kicked: list[int] = []
    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: kicked.append(uid))
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'async@x.com').json()['access_token']}"}
        with session_scope() as db:
            user = db.scalar(matcher.select(models.User).where(models.User.email == "async@x.com"))
            db.add(models.Resume(user_id=user.id, filename="r.txt",
                                 content_text="Senior Python", is_active=True))
            comp = models.Company(user_id=user.id, name="Acme"); db.add(comp); db.flush()
            db.add(models.Position(company_id=comp.id, external_id="1",
                                   title="Backend Engineer", description="Build APIs"))
            db.add(models.Interest(user_id=user.id, label="be", title_keywords="backend",
                                   is_active=True))
        body = c.post("/api/run", headers=h).json()
        assert body["pending"] == 1 and body["scored"] == 0
        assert kicked  # background drain was kicked
        status = c.get("/api/evaluation/status", headers=h).json()
        assert status["pending"] == 1 and status["in_progress"] is False


# ── Job list: categories + pagination ────────────────────────────────────────
def _seed_match_mix(db, uid):
    """Add a mix of matching, filter-rejected, keyword-excluded, and error-marker
    rows so the category/pagination logic has something to slice."""
    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    company = db.scalar(matcher.select(models.Company).where(models.Company.user_id == uid))
    rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))

    def add(ext, title, *, passed, score, model):
        pos = models.Position(company_id=company.id, external_id=ext, title=title, description="d")
        db.add(pos); db.flush()
        db.add(models.MatchResult(
            user_id=uid, position_id=pos.id, resume_id=rid, interest_id=interest.id,
            passed_filter=passed, match_score=score, win_probability=score,
            reasoning="r", model=model))

    add("a", "Match A", passed=True, score=90, model="good")
    add("b", "Match B", passed=True, score=60, model="good")          # below min_score 70
    add("c", "Rejected", passed=False, score=0, model="deepseek-flash")  # filter reject
    add("d", "Excluded", passed=False, score=0, model=matcher.EXCLUDED_MODEL)
    add("e", "Errored", passed=False, score=0, model=matcher.ERROR_MODEL)  # transient failure
    db.flush()


def test_build_job_list_categories_and_pagination():
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
    with session_scope() as db:
        user = db.get(models.User, uid)

        matching, total_m = reporter.build_job_list(db, user, category="matching", limit=10)
        assert total_m == 2 and [m["title"] for m in matching] == ["Match A", "Match B"]
        assert all(not m["non_matching"] for m in matching)
        assert matching[1]["below_threshold"] is True  # B (60) < interest min_score (70)

        all_items, total_all = reporter.build_job_list(db, user, category="all", limit=50)
        titles = [m["title"] for m in all_items]
        assert total_all == 4 and "Errored" not in titles  # error markers excluded
        assert {"Rejected", "Excluded"} <= set(titles)
        assert titles[:2] == ["Match A", "Match B"]  # matches rank first
        non = [m for m in all_items if m["non_matching"]]
        assert len(non) == 2 and all(m["match_score"] == 0 for m in non)

        page1, t = reporter.build_job_list(db, user, category="all", limit=2, offset=0)
        page2, _ = reporter.build_job_list(db, user, category="all", limit=2, offset=2)
        assert t == 4 and [m["title"] for m in page1] == ["Match A", "Match B"]
        assert set(m["title"] for m in page1).isdisjoint(m["title"] for m in page2)


def test_job_list_endpoint_paginates_and_filters_category(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)  # no real background drain
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        m = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert m["total"] == 2 and len(m["items"]) == 2
        assert all(not it["non_matching"] for it in m["items"])

        a = c.get("/api/job-lists/latest?category=all&limit=2&offset=0", headers=h).json()
        assert a["total"] == 4 and len(a["items"]) == 2  # page size honored

        a_all = c.get("/api/job-lists/latest?category=all&limit=50", headers=h).json()
        assert any(it["non_matching"] for it in a_all["items"])  # non-matching visible in 'all'


def test_build_job_list_score_and_win_filters():
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)  # A: 90/90, B: 60/60 (win == score in the helper)
    with session_scope() as db:
        user = db.get(models.User, uid)
        # match score ≥ 75 keeps only A; B (60) drops.
        hi, hi_total = reporter.build_job_list(db, user, category="matching", min_score=75, limit=50)
        assert hi_total == 1 and [m["title"] for m in hi] == ["Match A"]
        # win rate ≥ 75 likewise keeps only A.
        _, win_total = reporter.build_job_list(db, user, category="matching", min_win=75)
        assert win_total == 1
        # the >0 default (1) keeps both real matches.
        _, lo_total = reporter.build_job_list(db, user, category="matching", min_score=1, min_win=1)
        assert lo_total == 2


def _seed_dated_matches(db, uid):
    """Matching positions across a range of listed dates. The two highest scorers
    are *old* (20 and 40 days), so a recent-only window must replace them with
    lower-scoring fresh roles. 'Undated' has no posted_at -> first_seen_at (now)
    is its effective date. These are extra rows on top of _seed_user's base
    position (which has no MatchResult and so never appears in the job list)."""
    from app.timeutil import utcnow

    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    company = db.scalar(matcher.select(models.Company).where(models.Company.user_id == uid))
    rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))
    now = utcnow()
    specs = [  # (ext, title, score, days_ago | None -> undated)
        ("old1", "OldHigh1", 99, 40), ("old2", "OldHigh2", 98, 20),
        ("r0", "Recent0", 85, 0), ("r2", "Recent2", 84, 2),
        ("r4", "Recent4", 83, 4), ("r6", "Recent6", 82, 6),
        ("undated", "Undated", 70, None),
    ]
    for ext, title, score, days in specs:
        posted = None if days is None else now - timedelta(days=days)
        pos = models.Position(company_id=company.id, external_id=ext, title=title,
                              description="d", posted_at=posted)
        db.add(pos); db.flush()
        db.add(models.MatchResult(user_id=uid, position_id=pos.id, resume_id=rid,
                                  interest_id=interest.id, passed_filter=True,
                                  match_score=score, win_probability=score,
                                  reasoning="r", model="good"))
    db.flush()


def test_build_job_list_post_date_filter():
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_dated_matches(db, uid)
    with session_scope() as db:
        user = db.get(models.User, uid)

        _, all_total = reporter.build_job_list(db, user, category="matching")
        assert all_total == 7  # the 7 seeded matches (base position has no match)

        # 24h window keeps only today's (Recent0) and the undated (first-seen now).
        wk, total = reporter.build_job_list(db, user, category="matching", posted_within_days=1)
        assert total == 2 and {m["title"] for m in wk} == {"Recent0", "Undated"}

        # 7-day window: the four Recent* roles + Undated; both old highs excluded.
        _, total7 = reporter.build_job_list(db, user, category="matching", posted_within_days=7)
        assert total7 == 5

        # 30-day window additionally lets the 20-day-old high back in; 40-day stays out.
        _, total30 = reporter.build_job_list(db, user, category="matching", posted_within_days=30)
        assert total30 == 6

        # Every row carries an effective listed date for the UI.
        page, _ = reporter.build_job_list(db, user, category="matching", limit=50)
        assert all(m["listed_at"] for m in page)


def test_top5_backfills_from_date_filtered_pool():
    """With a post-date window, the top-5 glance is drawn from the filtered pool —
    the real highest scorers (here the 20- and 40-day-old ones) that fall outside
    the window are replaced by fresh lower-scoring roles, not left as gaps."""
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_dated_matches(db, uid)
    with session_scope() as db:
        user = db.get(models.User, uid)
        items, total = reporter.build_job_list(
            db, user, category="matching", posted_within_days=7, limit=5
        )
        # 5 in-window matches, all returned, neither old high scorer among them.
        assert total == 5 and len(items) == 5
        assert {"OldHigh1", "OldHigh2"}.isdisjoint(m["title"] for m in items)


def _seed_two_company_matches(db, uid):
    """Add a second company (Globex, 2 matches) alongside _seed_user's Acme (1
    match) so the company filter has more than one company to slice. Returns
    (acme_id, globex_id)."""
    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))
    acme = db.scalar(matcher.select(models.Company).where(models.Company.user_id == uid))
    globex = models.Company(user_id=uid, name="Globex")
    db.add(globex); db.flush()

    def add(company, ext, title, score):
        pos = models.Position(company_id=company.id, external_id=ext, title=title, description="d")
        db.add(pos); db.flush()
        db.add(models.MatchResult(user_id=uid, position_id=pos.id, resume_id=rid,
                                  interest_id=interest.id, passed_filter=True, match_score=score,
                                  win_probability=score, reasoning="r", model="good"))

    add(acme, "a1", "Acme Backend", 90)
    add(globex, "g1", "Globex Backend", 85)
    add(globex, "g2", "Globex Frontend", 80)
    db.flush()
    return acme.id, globex.id


def test_build_job_list_filters_by_company():
    with session_scope() as db:
        uid = _seed_user(db)
        acme_id, globex_id = _seed_two_company_matches(db, uid)
    with session_scope() as db:
        user = db.get(models.User, uid)
        _, total = reporter.build_job_list(db, user, category="matching", limit=50)
        assert total == 3  # all matches across both companies

        gx, gx_total = reporter.build_job_list(
            db, user, category="matching", company_id=globex_id, limit=50)
        assert gx_total == 2 and {m["company"] for m in gx} == {"Globex"}

        ac, ac_total = reporter.build_job_list(
            db, user, category="matching", company_id=acme_id, limit=50)
        assert ac_total == 1 and ac[0]["company"] == "Acme"

        # An id with no matches for this user yields an empty (not leaked) page.
        _, none_total = reporter.build_job_list(
            db, user, category="matching", company_id=999999, limit=50)
        assert none_total == 0


def _seed_preset_subscription_match(db, uid, *, key="nvidia", name="NVIDIA"):
    """Add a global preset company, subscribe the user, and score one passing match
    against its shared position. Returns (company_id, position_id)."""
    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))
    company = models.Company(preset_key=key, user_id=None, name=name,
                             ats_type="eightfold", ats_token=f"{key}.com")
    db.add(company); db.flush()
    db.add(models.Subscription(user_id=uid, company_id=company.id))
    pos = models.Position(company_id=company.id, external_id="nv1", title="GPU Engineer",
                          description="CUDA")
    db.add(pos); db.flush()
    db.add(models.MatchResult(
        user_id=uid, position_id=pos.id, resume_id=rid, interest_id=interest.id,
        passed_filter=True, match_score=90, win_probability=85, reasoning="r", model="good"))
    db.flush()
    return company.id, pos.id


def test_unfollowing_preset_drops_its_jobs_from_lists():
    """Unfollowing a preset deletes only the Subscription; its shared positions and the
    user's MatchResults persist (other followers still use them). The job list, report,
    and detail/visibility gates must stop surfacing them — regression: NVIDIA jobs kept
    showing after unfollow."""
    with session_scope() as db:
        uid = _seed_user(db)  # also seeds a custom "Acme" company + a position
        interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
        rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))
        acme_pos = db.scalar(matcher.select(models.Position))  # the custom company's position
        acme_pid = acme_pos.id
        db.add(models.MatchResult(
            user_id=uid, position_id=acme_pid, resume_id=rid, interest_id=interest.id,
            passed_filter=True, match_score=88, win_probability=80, reasoning="r", model="good"))
        nv_cid, nv_pid = _seed_preset_subscription_match(db, uid)

    with session_scope() as db:  # while subscribed: both companies show
        user = db.get(models.User, uid)
        items, total = reporter.build_job_list(db, user, category="matching", limit=50)
        assert total == 2 and {m["company"] for m in items} == {"Acme", "NVIDIA"}
        assert reporter.position_visible(db, user, nv_pid) is True

    with session_scope() as db:  # unfollow NVIDIA (API path: delete the subscription)
        sub = db.scalar(matcher.select(models.Subscription).where(
            models.Subscription.user_id == uid, models.Subscription.company_id == nv_cid))
        db.delete(sub)

    with session_scope() as db:
        user = db.get(models.User, uid)
        items, total = reporter.build_job_list(db, user, category="matching", limit=50)
        assert total == 1 and {m["company"] for m in items} == {"Acme"}  # NVIDIA gone
        assert "NVIDIA" not in {m["company"] for m in reporter.build_report(db, user, min_results=10)}
        assert reporter.position_visible(db, user, nv_pid) is False
        assert reporter.build_position_detail(db, user, nv_pid) is None
        # The user's own custom-company match is untouched.
        assert reporter.position_visible(db, user, acme_pid) is True


def test_applied_job_survives_unfollow():
    """A job the user already applied to stays on the list after they unfollow its
    company (mirrors the removed-but-applied exception) so the application isn't lost."""
    with session_scope() as db:
        uid = _seed_user(db)
        nv_cid, nv_pid = _seed_preset_subscription_match(db, uid)
        db.add(models.Application(user_id=uid, position_id=nv_pid))
    with session_scope() as db:
        sub = db.scalar(matcher.select(models.Subscription).where(
            models.Subscription.user_id == uid, models.Subscription.company_id == nv_cid))
        db.delete(sub)
    with session_scope() as db:
        user = db.get(models.User, uid)
        items, total = reporter.build_job_list(db, user, category="matching", limit=50)
        assert total == 1 and items[0]["company"] == "NVIDIA" and items[0]["applied"] is True
        assert reporter.position_visible(db, user, nv_pid) is True


def test_incomplete_batch_warning_is_not_a_provider_failure():
    """An incomplete/invalid-batch warning (a content issue, retried per-pair) must NOT
    raise the red 'check your provider key' banner; real provider failures still do."""
    assert reporter.is_llm_failure(
        "Scoring failed for 9 posting(s): model returned an incomplete batch result") is False
    assert reporter.is_llm_failure(
        "Scoring failed: model returned an invalid batch result") is False
    assert reporter.llm_failed(
        ["Scoring failed for 2 posting(s): model returned an incomplete batch result"]) is False
    # Genuine provider/auth/quota failures still trip the banner.
    assert reporter.is_llm_failure("Scoring failed: Could not reach Ollama at https://x") is True
    assert reporter.is_llm_failure("Ollama budget/quota appears to be exhausted") is True


def test_job_list_endpoint_filters_by_company(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
        _, globex_id = _seed_two_company_matches(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        wide = c.get("/api/job-lists/latest?category=matching&limit=50", headers=h).json()
        assert wide["total"] == 3
        narrow = c.get(
            f"/api/job-lists/latest?category=matching&limit=50&company_id={globex_id}", headers=h
        ).json()
        assert narrow["total"] == 2 and all(it["company"] == "Globex" for it in narrow["items"])


def test_job_list_snapshot_filters_by_company():
    """The frozen-snapshot path filters by company name resolved from the id."""
    from app.routers.reports import get_job_list

    with session_scope() as db:
        uid = _seed_user(db)
        _, globex_id = _seed_two_company_matches(db, uid)
        user = db.get(models.User, uid)
        snap = reporter.record_job_list_snapshot(
            db, user, SimpleNamespace(new_positions=0, scored=3, filtered=0, errors=[])
        )
        full = get_job_list(snap.id, limit=50, user=user, db=db)
        assert full.total == 3
        only_gx = get_job_list(snap.id, limit=50, company_id=globex_id, user=user, db=db)
        assert only_gx.total == 2 and all(it.company == "Globex" for it in only_gx.items)
        # Unknown company id -> empty page, never another company's rows.
        missing = get_job_list(snap.id, limit=50, company_id=999999, user=user, db=db)
        assert missing.total == 0 and missing.items == []


def test_job_list_endpoint_accepts_post_date_filter(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_dated_matches(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        wide = c.get("/api/job-lists/latest?category=matching&limit=50", headers=h).json()
        narrow = c.get(
            "/api/job-lists/latest?category=matching&limit=50&posted_within_days=7", headers=h
        ).json()
        assert wide["total"] == 7 and narrow["total"] == 5
        assert all(it["listed_at"] for it in narrow["items"])


def test_filter_items_posted_within_keeps_undated():
    """Frozen-snapshot date filtering keeps items missing listed_at (older saved
    snapshots) and drops only those provably outside the window."""
    from app.timeutil import utcnow

    now = utcnow()
    items = [
        {"title": "fresh", "listed_at": (now - timedelta(days=1)).isoformat()},
        {"title": "stale", "listed_at": (now - timedelta(days=20)).isoformat()},
        {"title": "legacy"},  # no listed_at -> kept
    ]
    kept = {m["title"] for m in reporter.filter_items_posted_within(items, 7)}
    assert kept == {"fresh", "legacy"}
    # Window off -> everything passes through untouched.
    assert reporter.filter_items_posted_within(items, None) == items


# ── Application status ("Mark applied") ──────────────────────────────────────
def _first_matched_position_id(db, uid):
    return db.scalar(
        matcher.select(models.MatchResult.position_id)
        .where(models.MatchResult.user_id == uid)
        .limit(1)
    )


def _add_match(db, uid, position_id, score=80):
    """Give user ``uid`` a (passing) MatchResult on an existing position, so that
    position shows in their job list and is markable-applied."""
    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    rid = db.scalar(matcher.select(models.Resume.id).where(models.Resume.user_id == uid))
    db.add(models.MatchResult(
        user_id=uid, position_id=position_id, resume_id=rid, interest_id=interest.id,
        passed_filter=True, match_score=score, win_probability=score, reasoning="r", model="good"))
    db.flush()


def test_mark_and_unmark_applied(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
        pid = _first_matched_position_id(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        listed = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert all(it["applied"] is False for it in listed["items"])  # nothing applied yet

        r = c.post(f"/api/applications/{pid}", headers=h)
        assert r.status_code == 201 and r.json()["position_id"] == pid and r.json()["status"] == "applied"
        assert c.post(f"/api/applications/{pid}", headers=h).status_code == 201  # idempotent

        after = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert any(it["position_id"] == pid and it["applied"] for it in after["items"])
        assert [a["position_id"] for a in c.get("/api/applications", headers=h).json()] == [pid]

        assert c.delete(f"/api/applications/{pid}", headers=h).status_code == 204
        assert c.delete(f"/api/applications/{pid}", headers=h).status_code == 204  # idempotent
        cleared = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert all(not it["applied"] for it in cleared["items"])
        assert c.get("/api/applications", headers=h).json() == []


def test_mark_applied_rejects_position_not_in_job_list(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)  # no real background drain
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        # A position the user has no MatchResult for is invisible to them -> 404.
        assert c.post("/api/applications/999999", headers=h).status_code == 404
        assert c.get("/api/applications", headers=h).json() == []


def test_applied_status_is_per_user(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid_a = _seed_user(db, email="a@x.com")
        uid_b = _seed_user(db, email="b@x.com")
        # Two users legitimately share a position only via a preset they both follow
        # (custom companies are per-user). Subscribe both and match both on it.
        cid, pid = _seed_preset_subscription_match(db, uid_a)
        db.add(models.Subscription(user_id=uid_b, company_id=cid))
        _add_match(db, uid_b, pid)  # B is matched on the SAME shared position
    ha = {"Authorization": f"Bearer {create_access_token(uid_a)}"}
    hb = {"Authorization": f"Bearer {create_access_token(uid_b)}"}
    with TestClient(app) as c:
        assert c.post(f"/api/applications/{pid}", headers=ha).status_code == 201
        # B sees the same position but it's NOT applied for them, and B has no apps.
        mb = c.get("/api/job-lists/latest?category=matching&limit=50", headers=hb).json()
        assert any(it["position_id"] == pid and it["applied"] is False for it in mb["items"])
        assert c.get("/api/applications", headers=hb).json() == []


def test_applied_overlaid_on_frozen_snapshot():
    from app.routers.reports import get_job_list

    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        matcher.run_for_user(db, user, client=GoodClient(score=88), filter_client=FilterPass())
        snap = reporter.record_job_list_snapshot(
            db, user, SimpleNamespace(new_positions=0, scored=1, filtered=0, errors=[])
        )
        pid = reporter.job_list_items(snap)[0]["position_id"]

        before = get_job_list(snap.id, user=user, db=db)
        assert before.items and all(not it.applied for it in before.items)

        db.add(models.Application(user_id=uid, position_id=pid))
        db.commit()
        after = get_job_list(snap.id, user=user, db=db)
        assert any(it.position_id == pid and it.applied for it in after.items)


def test_applications_cascade_on_user_delete():
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
        db.add(models.Application(user_id=uid, position_id=_first_matched_position_id(db, uid)))
        db.commit()
        assert len(list(db.scalars(matcher.select(models.Application)))) == 1
        db.delete(db.get(models.User, uid))
        db.commit()
        assert list(db.scalars(matcher.select(models.Application))) == []


# ── LLM provider config (per-user) ───────────────────────────────────────────
def test_effective_llm_config_defaults_to_provider_with_no_key():
    from app.llm_providers import DEFAULT_PROVIDER_OBJ as p
    from app.services import llm

    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        eff = llm.effective_config(db, user)
        assert eff.provider == p.key and eff.base_url == p.base_url
        assert eff.main_model == p.default_main_model
        assert eff.light_model == p.default_light_model
        assert eff.api_key is None  # no per-user key, no global fallback anymore


def test_effective_llm_config_uses_user_overrides():
    from app.services import llm

    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.LlmConfig(user_id=uid, provider="ollama_cloud", api_key="sk-user",
                                main_model="big-model", light_model="small-model"))
        db.commit()
        eff = llm.effective_config(db, db.get(models.User, uid))
        assert eff.base_url == "https://ollama.com"  # from the provider registry
        assert eff.api_key == "sk-user"
        assert eff.main_model == "big-model" and eff.light_model == "small-model"


# ── Company application accounts (encrypted, per-user) ───────────────────────
def test_crypto_roundtrip_and_tamper_resistance():
    from app import crypto

    token = crypto.encrypt("hunter2")
    assert token and "hunter2" not in token          # ciphertext, not plaintext
    assert crypto.decrypt(token) == "hunter2"         # round-trips
    assert crypto.encrypt(None) is None and crypto.encrypt("") is None
    assert crypto.decrypt(None) is None
    assert crypto.decrypt("not-a-real-token") is None  # bad token -> None, never raises


def _subscribe_preset(client, headers, name):
    """Subscribe the user to a built-in preset by name; returns the company row."""
    presets = client.get("/api/companies/presets", headers=headers).json()
    p = next(x for x in presets if x["name"] == name)
    body = {k: p[k] for k in ("name", "careers_url", "ats_type", "ats_token")}
    body["preset_key"] = p["key"]
    return client.post("/api/companies", json=body, headers=headers).json()


def test_account_preset_attach_encrypts_and_tags():
    """A preset that needs an account: saving credentials encrypts them at rest,
    never echoes the password, and flips the watch-list tag to attached. Blank
    password on re-save keeps the stored one; Remove clears the account."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'acc@x.com').json()['access_token']}"}
        cid = _subscribe_preset(c, h, "Google")["id"]  # Google Careers requires an account

        row = next(x for x in c.get("/api/companies", headers=h).json() if x["id"] == cid)
        assert row["requires_account"] is True and row["account_attached"] is False

        det = c.get(f"/api/companies/{cid}/detail", headers=h).json()
        assert det["requires_account"] is True
        assert det["account_portal_url"] and "google" in det["account_portal_url"]
        assert det["account_username"] is None and det["account_has_password"] is False

        saved = c.put(f"/api/companies/{cid}/account", headers=h,
                      json={"username": "me@gmail.com", "password": "hunter2",
                            "portal_url": "https://accounts.google.com/"}).json()
        assert saved["account_attached"] is True
        assert saved["account_username"] == "me@gmail.com"
        assert saved["account_has_password"] is True
        assert "password" not in saved  # secret never returned

        with session_scope() as db:
            acct = db.scalar(matcher.select(models.CompanyAccount)
                             .where(models.CompanyAccount.company_id == cid))
            assert acct.username_enc and "me@gmail.com" not in acct.username_enc
            assert acct.password_enc and "hunter2" not in acct.password_enc

        row = next(x for x in c.get("/api/companies", headers=h).json() if x["id"] == cid)
        assert row["account_attached"] is True

        # Blank password keeps the saved one; the username change persists.
        c.put(f"/api/companies/{cid}/account", headers=h,
              json={"username": "me2@gmail.com", "password": ""})
        det2 = c.get(f"/api/companies/{cid}/detail", headers=h).json()
        assert det2["account_username"] == "me2@gmail.com" and det2["account_has_password"] is True

        assert c.delete(f"/api/companies/{cid}/account", headers=h).status_code == 204
        det3 = c.get(f"/api/companies/{cid}/detail", headers=h).json()
        assert det3["account_attached"] is False and det3["account_has_password"] is False


def test_account_rejected_for_non_account_and_custom_companies():
    """Companies you can apply to without an account (Greenhouse presets, and any
    custom company) expose no account section and refuse account saves."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'noa@x.com').json()['access_token']}"}

        cid = _subscribe_preset(c, h, "Anthropic")["id"]  # Greenhouse: no account needed
        assert c.get(f"/api/companies/{cid}/detail", headers=h).json()["requires_account"] is False
        assert c.put(f"/api/companies/{cid}/account", headers=h,
                     json={"username": "x", "password": "y"}).status_code == 400

        custom = c.post("/api/companies", json={"name": "Acme Custom"}, headers=h).json()
        cdet = c.get(f"/api/companies/{custom['id']}/detail", headers=h).json()
        assert cdet["requires_account"] is False and cdet["is_preset"] is False
        assert c.put(f"/api/companies/{custom['id']}/account", headers=h,
                     json={"username": "x"}).status_code == 400


def test_company_detail_scoped_to_user_watchlist():
    """Detail is visible only for companies on the user's list: a preset they don't
    follow and another user's custom company both 404; auth is required."""
    with TestClient(app) as c:
        ha = {"Authorization": f"Bearer {_register(c, 'da@x.com').json()['access_token']}"}
        hb = {"Authorization": f"Bearer {_register(c, 'db@x.com').json()['access_token']}"}

        # A seeded preset the user hasn't subscribed to is not visible.
        with session_scope() as db:
            gid = db.scalar(matcher.select(models.Company.id)
                            .where(models.Company.preset_key == "google"))
        assert c.get(f"/api/companies/{gid}/detail", headers=ha).status_code == 404

        # B can't see A's custom company; A can.
        custom = c.post("/api/companies", json={"name": "Acme"}, headers=ha).json()
        assert c.get(f"/api/companies/{custom['id']}/detail", headers=hb).status_code == 404
        assert c.get(f"/api/companies/{custom['id']}/detail", headers=ha).status_code == 200
    with TestClient(app) as c:
        assert c.get("/api/companies/1/detail").status_code == 401  # auth required


def test_company_accounts_cascade_on_user_delete():
    """Deleting a user removes their saved credentials (no orphaned ciphertext)."""
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'cas@x.com').json()['access_token']}"}
        cid = _subscribe_preset(c, h, "NVIDIA")["id"]  # NVIDIA (Workday) requires an account
        c.put(f"/api/companies/{cid}/account", headers=h,
              json={"username": "n@v.com", "password": "p"})
    with session_scope() as db:
        assert db.scalar(matcher.select(models.CompanyAccount)) is not None
        db.delete(db.scalar(matcher.select(models.User).where(models.User.email == "cas@x.com")))
        db.commit()
        assert list(db.scalars(matcher.select(models.CompanyAccount))) == []


def test_clients_built_from_user_config(monkeypatch):
    from app.services import llm

    captured = []

    class FakeClient:
        def __init__(self, base_url=None, api_key=None, model=None, timeout=None):
            captured.append((base_url, api_key, model))
            self.model = model

    monkeypatch.setattr(llm, "OllamaClient", FakeClient)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.LlmConfig(user_id=uid, provider="ollama_cloud", api_key="sk-x",
                                main_model="big", light_model="small"))
        db.commit()
        llm.clients_for_user(db, db.get(models.User, uid))
    assert ("https://ollama.com", "sk-x", "big") in captured     # scoring client
    assert ("https://ollama.com", "sk-x", "small") in captured   # relevance-filter client


def test_llm_config_endpoints_roundtrip(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)  # no real background drain
    with session_scope() as db:
        uid = _seed_user(db)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        cfg = c.get("/api/llm-config", headers=h).json()
        assert cfg["provider"] == "ollama_cloud"
        assert any(p["key"] == "ollama_cloud" and p["base_url"] == "https://ollama.com"
                   for p in cfg["providers"])

        r = c.put("/api/llm-config", headers=h, json={
            "provider": "ollama_cloud", "main_model": "big", "light_model": "small",
            "api_key": "sk-123"})
        assert r.status_code == 200
        got = r.json()
        assert got["main_model"] == "big" and got["light_model"] == "small"
        assert got["has_api_key"] is True and "api_key" not in got  # key never echoed back

        again = c.get("/api/llm-config", headers=h).json()
        assert again["main_model"] == "big" and again["base_url"] == "https://ollama.com"
        assert again["has_api_key"] is True
    assert c.get("/api/llm-config").status_code == 401  # auth required


def test_llm_config_keeps_key_when_blank_and_rejects_unknown_provider(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)  # no real background drain
    with session_scope() as db:
        uid = _seed_user(db)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        c.put("/api/llm-config", headers=h, json={
            "provider": "ollama_cloud", "main_model": "m", "light_model": "l", "api_key": "sk-keep"})
        # Re-saving without api_key keeps the stored one.
        c.put("/api/llm-config", headers=h, json={
            "provider": "ollama_cloud", "main_model": "m2", "light_model": "l2"})
        assert c.get("/api/llm-config", headers=h).json()["has_api_key"] is True
        # Unknown provider is rejected.
        assert c.put("/api/llm-config", headers=h, json={
            "provider": "openai", "main_model": "m", "light_model": "l"}).status_code == 400
    with session_scope() as db:
        cfg = db.scalar(matcher.select(models.LlmConfig).where(models.LlmConfig.user_id == uid))
        assert cfg.api_key == "sk-keep" and cfg.main_model == "m2"


def test_matcher_resolves_clients_per_user(monkeypatch):
    """run_for_user with no injected clients resolves them from the user's config."""
    called = {}

    def fake_clients(db, user):
        called["uid"] = user.id
        return GoodClient(score=91), FilterPass()

    monkeypatch.setattr(matcher.llm, "clients_for_user", fake_clients)
    with session_scope() as db:
        uid = _seed_user(db)
        res = matcher.run_for_user(db, db.get(models.User, uid))  # no clients injected
        assert called.get("uid") == uid and res.scored == 1


def test_llm_config_test_button(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator
    import app.routers.llm_config as rc
    from app.services.ollama_client import OllamaError

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)

    built: list[str] = []

    class FakeClient:
        def __init__(self, **k):
            self.model = k.get("model")
            built.append(self.model)

        def chat_text(self, *a, **k):
            if self.model == "bad":
                raise OllamaError("Ollama returned 401: invalid api key")
            return "OK"

    monkeypatch.setattr(rc, "OllamaClient", FakeClient)
    with session_scope() as db:
        uid = _seed_user(db)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        # Distinct models -> both probed, both pass.
        built.clear()
        ok = c.post("/api/llm-config/test", headers=h, json={
            "provider": "ollama_cloud", "main_model": "big", "light_model": "small",
            "api_key": "sk-1"}).json()
        assert ok["ok"] is True and built == ["big", "small"]
        assert {r["role"] for r in ok["results"]} == {"main", "light"}

        # Same model for both -> probed once.
        built.clear()
        same = c.post("/api/llm-config/test", headers=h, json={
            "provider": "ollama_cloud", "main_model": "big", "light_model": "big",
            "api_key": "sk-1"}).json()
        assert same["ok"] is True and built == ["big"] and len(same["results"]) == 1

        # One model fails -> overall not ok, the failing model named with its error.
        built.clear()
        bad = c.post("/api/llm-config/test", headers=h, json={
            "provider": "ollama_cloud", "main_model": "big", "light_model": "bad",
            "api_key": "sk-1"}).json()
        assert bad["ok"] is False and built == ["big", "bad"]
        light = next(r for r in bad["results"] if r["role"] == "light")
        assert light["ok"] is False and "401" in light["detail"]
        assert next(r for r in bad["results"] if r["role"] == "main")["ok"] is True

        # No key supplied and none saved -> reported, and no client is even built.
        built.clear()
        nokey = c.post("/api/llm-config/test", headers=h, json={
            "provider": "ollama_cloud", "main_model": "big", "light_model": "small"}).json()
        assert nokey["ok"] is False and "API key" in nokey["detail"] and built == []


def test_llm_failed_classifier():
    assert reporter.llm_failed(["Scoring failed: Ollama returned 401"]) is True
    assert reporter.llm_failed(["Filtering failed: read timeout"]) is True
    assert reporter.llm_failed(["Ollama budget/quota appears to be exhausted"]) is True
    assert reporter.llm_failed(["Acme: refusing to fetch private host"]) is False
    assert reporter.llm_failed([]) is False


def test_job_list_surfaces_llm_error(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
        reporter.record_job_list_snapshot(db, db.get(models.User, uid), SimpleNamespace(
            new_positions=0, scored=0, filtered=0,
            errors=["Scoring failed: Ollama returned 401: invalid api key"]))
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        assert c.get("/api/job-lists/latest", headers=h).json()["llm_error"] is True
    # A later clean snapshot clears the banner.
    with session_scope() as db:
        reporter.record_job_list_snapshot(db, db.get(models.User, uid), SimpleNamespace(
            new_positions=0, scored=1, filtered=0, errors=[]))
    with TestClient(app) as c:
        assert c.get("/api/job-lists/latest", headers=h).json()["llm_error"] is False


def test_score_filter_exempts_non_matching_in_all_category():
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
    with session_scope() as db:
        user = db.get(models.User, uid)
        items, total = reporter.build_job_list(db, user, category="all", min_score=75, limit=50)
        titles = [m["title"] for m in items]
        # Only A clears the score floor; the two non-matching rows are exempt.
        assert "Match A" in titles and "Match B" not in titles
        assert {"Rejected", "Excluded"} <= set(titles) and "Errored" not in titles
        assert total == 3


def test_job_list_endpoint_applies_score_win_filters(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
        _seed_match_mix(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        hi = c.get("/api/job-lists/latest?category=matching&min_score=75&limit=50", headers=h).json()
        assert hi["total"] == 1 and hi["items"][0]["title"] == "Match A"
        # Non-matching stays visible in 'all' even with a high score floor.
        allhi = c.get("/api/job-lists/latest?category=all&min_score=90&limit=50", headers=h).json()
        assert any(it["non_matching"] for it in allhi["items"])


# ── Shared preset catalog + crawling ─────────────────────────────────────────
def test_seed_presets_is_idempotent_and_global():
    from app.company_presets import PRESETS
    from app.db import seed_presets

    with session_scope() as db:
        seed_presets(db)
        seed_presets(db)  # second pass must not duplicate
    with session_scope() as db:
        rows = list(db.scalars(matcher.select(models.Company).where(models.Company.preset_key.is_not(None))))
        assert len(rows) == len(PRESETS)
        assert all(c.user_id is None and c.is_preset for c in rows)


def test_crawl_presets_populates_shared_positions(monkeypatch):
    from app.company_presets import PRESETS
    from app.db import seed_presets
    from app.services import crawler, scraper

    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda c: [
        scraper.ScrapedPosition(external_id=f"{c.preset_key}-1", title=f"{c.name} Eng", description="build")
    ])
    with session_scope() as db:
        seed_presets(db)
    summary = crawler.crawl_presets()
    assert summary["companies"] == len(PRESETS) and summary["new_positions"] == len(PRESETS)
    with session_scope() as db:
        positions = list(db.scalars(matcher.select(models.Position)))
        assert len(positions) == len(PRESETS)
        assert all(db.get(models.Company, p.company_id).is_preset for p in positions)


def test_user_scan_does_not_crawl_presets(monkeypatch):
    from app.db import seed_presets

    crawled: list[str] = []
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda c: crawled.append(c.name) or [])
    with session_scope() as db:
        uid = _seed_user(db)  # has a custom company "Acme"
        seed_presets(db)
        anthropic = db.scalar(matcher.select(models.Company).where(models.Company.preset_key == "anthropic"))
        db.add(models.Subscription(user_id=uid, company_id=anthropic.id))
    with session_scope() as db:
        matcher.scrape_only(db, db.get(models.User, uid))
    # The user's scan crawls only their custom company, never the subscribed preset.
    assert "Acme" in crawled and "Anthropic" not in crawled


def test_scrape_for_all_users_scrapes_but_does_not_score(monkeypatch):
    """The daily-cron entry point saves new positions for every user but never
    scores or snapshots — matching is deferred to an on-demand scan."""
    from app.services import crawler, scraper

    monkeypatch.setattr(crawler, "crawl_presets", lambda: None)  # isolate from preset crawl
    monkeypatch.setattr(
        matcher.scraper, "scrape_company",
        lambda company: [scraper.ScrapedPosition(
            external_id="new1", title="Backend Engineer", location="Remote",
            description="Build Python APIs")],
    )
    with session_scope() as db:
        uid = _seed_user(db)  # already has one seeded position + active resume/interest

    summaries = matcher.scrape_for_all_users()

    assert summaries[uid].new_positions == 1 and summaries[uid].scored == 0
    with session_scope() as db:
        # The freshly scraped posting is persisted (alongside the seeded one)...
        assert len(list(db.scalars(matcher.select(models.Position)))) == 2
        # ...but the cron wrote no MatchResults and saved no job-list snapshot.
        assert list(db.scalars(matcher.select(models.MatchResult))) == []
        assert list(db.scalars(matcher.select(models.JobListSnapshot))) == []


def test_two_subscribers_share_one_preset_position():
    from app.db import seed_presets

    with session_scope() as db:
        seed_presets(db)
        anthropic = db.scalar(matcher.select(models.Company).where(models.Company.preset_key == "anthropic"))
        aid = anthropic.id
        pos = models.Position(company_id=aid, external_id="x1", title="Backend Engineer", description="build")
        db.add(pos); db.flush()
        pid = pos.id
        uids = []
        for email in ("share1@x.com", "share2@x.com"):
            u = models.User(email=email, hashed_password="h"); db.add(u); db.flush()
            db.add(models.Resume(user_id=u.id, filename="r", content_text="python", is_active=True))
            db.add(models.Interest(user_id=u.id, label="be", title_keywords="backend", is_active=True))
            db.add(models.Subscription(user_id=u.id, company_id=aid))
            uids.append(u.id)

    with session_scope() as db:  # each subscriber's backlog is the one shared position
        for uid in uids:
            assert matcher.count_pending(db, db.get(models.User, uid)) == 1
    for uid in uids:
        with session_scope() as db:
            matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
    with session_scope() as db:  # two MatchResults against the SAME position row
        results = list(db.scalars(matcher.select(models.MatchResult).where(models.MatchResult.position_id == pid)))
        assert len(results) == 2 and {r.user_id for r in results} == set(uids)


def test_add_preset_subscribes_custom_creates(monkeypatch):
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {_register(c, 'cat@x.com').json()['access_token']}"}
        r = c.post("/api/companies", headers=h, json={
            "name": "Anthropic", "ats_type": "greenhouse", "ats_token": "anthropic", "preset_key": "anthropic"})
        assert r.status_code == 201 and r.json()["is_preset"] is True
        # Subscribing again is a 409, not a duplicate.
        assert c.post("/api/companies", headers=h, json={"name": "Anthropic", "preset_key": "anthropic"}).status_code == 409
        # A non-preset is created as a per-user custom company.
        r2 = c.post("/api/companies", headers=h, json={
            "name": "Acme", "careers_url": "https://acme.example/careers", "ats_type": "html"})
        assert r2.status_code == 201 and r2.json()["is_preset"] is False
        assert {x["name"] for x in c.get("/api/companies", headers=h).json()} >= {"Anthropic", "Acme"}
    with session_scope() as db:  # exactly one shared Anthropic row
        rows = list(db.scalars(matcher.select(models.Company).where(models.Company.preset_key == "anthropic")))
        assert len(rows) == 1


def test_admin_crawl_endpoint_token_gated(monkeypatch):
    from app.config import settings
    from app.services import crawler

    kicks: list[int] = []
    monkeypatch.setattr(crawler, "crawl_presets_async", lambda: kicks.append(1))
    with TestClient(app) as c:
        monkeypatch.setattr(settings, "admin_token", "")  # disabled
        assert c.post("/api/admin/crawl").status_code == 503
        monkeypatch.setattr(settings, "admin_token", "s3cret")
        assert c.post("/api/admin/crawl").status_code == 401  # missing token
        r = c.post("/api/admin/crawl", headers={"X-Admin-Token": "s3cret"})
        assert r.status_code == 202 and kicks


# ── Health ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,expected", [(200, "ok"), (401, "unauthorized"), (503, "unreachable")])
def test_health_states(monkeypatch, status, expected):
    import app.services.ollama_client as oc

    class Resp:
        status_code = status

    monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: Resp())
    assert oc.OllamaClient().health() == expected


# ── Ollama logging ───────────────────────────────────────────────────────────
def test_ollama_logs_request_and_response(monkeypatch):
    """Every Ollama call persists the outgoing prompt and the completion to the
    llm_logs table (correlated by a request id) — and never the API key."""
    import app.services.ollama_client as oc
    from app.services import llm_log

    class Resp:
        def json(self):
            return {"message": {"content": "hello world"}, "done_reason": "stop",
                    "prompt_eval_count": 12, "eval_count": 3}

    monkeypatch.setattr(oc, "_post", lambda url, body, headers, timeout: Resp())
    out = oc.OllamaClient(api_key="super-secret-key").chat_text("be terse", "hi there")
    llm_log.flush()

    assert out == "hello world"
    with session_scope() as db:
        rows = list(db.scalars(matcher.select(models.LlmLog)))
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "ok" and row.correlation_id
        assert "hi there" in row.request_messages and "be terse" in row.request_messages
        assert row.response_content == "hello world"
        assert row.prompt_tokens == 12 and row.eval_tokens == 3 and row.done_reason == "stop"
        # The API key lives only in the Authorization header, never in what we store.
        assert "super-secret-key" not in row.request_messages


@pytest.mark.parametrize("status,body,expected", [
    (402, "Payment Required", "budget"),       # 402 is always budget
    (429, "monthly quota exceeded", "budget"),  # 429 naming a cap = budget
    (429, "too many requests", "generic"),      # bare 429 = transient rate limit
    (403, "insufficient credits", "budget"),
    (500, "internal error", "generic"),
])
def test_ollama_budget_error_classification(monkeypatch, status, body, expected):
    """A budget/quota rejection raises OllamaBudgetError so the matcher can react;
    everything else stays a generic OllamaError."""
    import app.services.ollama_client as oc

    class Resp:
        status_code = status
        text = body

        def raise_for_status(self):
            raise oc.httpx.HTTPStatusError("err", request=None, response=self)

    # Bypass the retry/backoff wrapper: post raises the status error directly.
    monkeypatch.setattr(oc.httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(oc.OllamaError) as ei:
        oc.OllamaClient().chat_text("s", "u")
    is_budget = isinstance(ei.value, oc.OllamaBudgetError)
    assert is_budget == (expected == "budget")


def test_ollama_logs_failures(monkeypatch, caplog):
    """A transport/HTTP error is recorded as an error row (and still logged to
    stdout at WARNING with the ✗ marker) before being raised."""
    import app.services.ollama_client as oc
    from app.services import llm_log

    def boom(*a, **k):
        raise oc.httpx.ConnectError("connection refused")

    monkeypatch.setattr(oc, "_post", boom)
    with caplog.at_level("WARNING", logger="app.services.ollama_client"):
        with pytest.raises(oc.OllamaError):
            oc.OllamaClient().chat_text("s", "u")
    llm_log.flush()

    assert any("✗" in r.getMessage() for r in caplog.records)  # terse stdout warning
    with session_scope() as db:
        rows = list(db.scalars(matcher.select(models.LlmLog)))
        assert len(rows) == 1
        assert rows[0].status == "error" and "connection refused" in rows[0].error_detail
        assert rows[0].response_content is None


def test_ollama_logging_can_be_disabled(monkeypatch):
    import app.services.ollama_client as oc
    from app.services import llm_log

    class Resp:
        def json(self):
            return {"message": {"content": "quiet"}}

    monkeypatch.setattr(oc, "_post", lambda *a, **k: Resp())
    monkeypatch.setattr(oc.settings, "log_ollama", False)
    oc.OllamaClient().chat_text("s", "u")
    llm_log.flush()
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.LlmLog))) == []


# ── Application kit (per-position detail page) ───────────────────────────────
class KitClient:
    """Generation stub. chat_json branches on the schema: the analysis call returns
    looking_for/open_questions, the resume call returns Markdown + an optimization
    note. chat_text returns the cover letter."""
    model = "fake-kit"

    def __init__(self):
        self.json_calls = 0
        self.text_calls = 0

    def chat_json(self, system, user, schema, temperature=0.2):
        self.json_calls += 1
        if "resume_markdown" in schema.get("properties", {}):
            return {"resume_markdown": "# Jane Doe\n\n## Experience\n- Built Python APIs",
                    "optimization_summary": "Reordered to lead with Python and distributed systems."}
        return {
            "looking_for": ["Strong Python", "Distributed systems"],
            "open_questions": [
                {"question": "Why do you want to work here?",
                 "advice": "Tie your background to the mission.",
                 "suggested_answer": "I admire the team's work on…"},
            ],
        }

    def chat_text(self, system, user, temperature=0.4):
        self.text_calls += 1
        return "Dear hiring team, …"


def _seeded_position(db, uid):
    company = db.scalar(matcher.select(models.Company).where(models.Company.user_id == uid))
    return db.scalar(matcher.select(models.Position).where(models.Position.company_id == company.id))


def test_kit_generate_populates_all_fields():
    from app.services import kits

    with session_scope() as db:
        uid = _seed_user(db)
    client = KitClient()
    with session_scope() as db:
        user = db.get(models.User, uid)
        pos = _seeded_position(db, uid)
        kit = kits.generate(db, user, pos, client=client)
        assert kit.status == "ok"
    # Two structured calls (analysis + resume) + one free-text doc (cover letter).
    assert client.json_calls == 2 and client.text_calls == 1
    with session_scope() as db:
        kit = db.scalar(matcher.select(models.ApplicationKit))
        assert json.loads(kit.looking_for) == ["Strong Python", "Distributed systems"]
        oq = json.loads(kit.open_questions)
        assert oq[0]["question"] and oq[0]["advice"] and oq[0]["suggested_answer"]
        assert kit.cover_letter.startswith("Dear hiring team")
        # The tailored resume is copy-paste-ready Markdown + an optimization note.
        assert kit.revised_resume.startswith("# Jane Doe")
        assert "Reordered to lead with Python" in kit.resume_optimization
        assert kit.model == "fake-kit" and kit.resume_id is not None


def test_resume_prompt_requests_polished_markdown_structure():
    from app.services import kits

    assert "Professional Summary" in kits.RESUME_SYSTEM
    assert "Core Skills" in kits.RESUME_SYSTEM
    assert "Selected Impact" in kits.RESUME_SYSTEM
    assert "Never use Markdown tables" in kits.RESUME_SYSTEM


def test_kit_generate_error_keeps_partials():
    """A failure on a later call leaves the kit in 'error' but keeps what already
    completed (the analysis), so the page still shows partial results."""
    from app.services import kits
    from app.services.ollama_client import OllamaError

    class TextFails(KitClient):
        def chat_text(self, *a, **k):
            raise OllamaError("Ollama returned 500: boom")

    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        kit = kits.generate(db, db.get(models.User, uid), _seeded_position(db, uid), client=TextFails())
        assert kit.status == "error" and "boom" in kit.error_detail
    with session_scope() as db:
        kit = db.scalar(matcher.select(models.ApplicationKit))
        assert json.loads(kit.looking_for)  # analysis survived the later failure
        assert kit.cover_letter is None and kit.revised_resume is None


def test_kit_generate_without_resume_errors():
    from app.services import kits

    with session_scope() as db:
        uid = _seed_user(db)
        db.delete(db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid)))
    with session_scope() as db:
        kit = kits.generate(db, db.get(models.User, uid), _seeded_position(db, uid), client=KitClient())
        assert kit.status == "error" and "resume" in kit.error_detail.lower()


def test_kit_worker_run_generates_kit(monkeypatch):
    """The background worker body resolves the user's client and produces an 'ok'
    kit (mirrors the evaluator drain test). Run synchronously for determinism."""
    from app.services import kit_worker, kits

    with session_scope() as db:
        uid = _seed_user(db)
        pid = _seeded_position(db, uid).id
    monkeypatch.setattr(kits.llm, "clients_for_user", lambda db, user: (KitClient(), KitClient()))

    kit_worker._run(uid, pid)

    with session_scope() as db:
        kit = db.scalar(matcher.select(models.ApplicationKit))
        assert kit and kit.status == "ok" and kit.cover_letter and kit.revised_resume


def test_position_detail_endpoint_returns_best_match(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:  # produce a passing match so the position is visible
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(score=88), filter_client=FilterPass())
        pid = _first_matched_position_id(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        d = c.get(f"/api/positions/{pid}/detail", headers=h).json()
        assert d["position_id"] == pid and d["title"] == "Backend Engineer"
        assert d["match_score"] == 88 and d["non_matching"] is False
        assert d["strengths"] == ["Python"] and d["applied"] is False
        assert d["kit"] is None  # nothing generated yet


def test_position_detail_and_kit_require_visibility(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator, kit_worker

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    kicked = []
    monkeypatch.setattr(kit_worker, "ensure_generating", lambda u, p: kicked.append((u, p)))
    with session_scope() as db:
        uid = _seed_user(db)
        unseen_pid = _seeded_position(db, uid).id  # exists, but no MatchResult for the user
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        assert c.get(f"/api/positions/{unseen_pid}/detail", headers=h).status_code == 404
        assert c.get(f"/api/positions/{unseen_pid}/kit", headers=h).status_code == 404
        assert c.post(f"/api/positions/{unseen_pid}/kit", headers=h).status_code == 404
        assert c.get("/api/positions/999999/detail", headers=h).status_code == 404
    assert kicked == []  # an invisible position never reaches the worker


def test_generate_kit_kicks_worker_and_polls(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator, kit_worker

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    kicked = []
    monkeypatch.setattr(kit_worker, "ensure_generating", lambda u, p: kicked.append((u, p)))
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
        pid = _first_matched_position_id(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        assert c.get(f"/api/positions/{pid}/kit", headers=h).status_code == 404  # none yet
        r = c.post(f"/api/positions/{pid}/kit", headers=h)
        assert r.status_code == 202 and r.json()["status"] == "generating"
        assert kicked == [(uid, pid)]
        polled = c.get(f"/api/positions/{pid}/kit", headers=h).json()
        assert polled["status"] == "generating"
        # Re-posting (regenerate) re-arms the worker.
        c.post(f"/api/positions/{pid}/kit", headers=h)
        assert kicked == [(uid, pid), (uid, pid)]
    # The detail payload now carries the in-progress kit.
    with TestClient(app) as c:
        d = c.get(f"/api/positions/{pid}/detail", headers=h).json()
        assert d["kit"]["status"] == "generating"


def test_generate_kit_requires_resume(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator, kit_worker

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    monkeypatch.setattr(kit_worker, "ensure_generating", lambda u, p: None)
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
        pid = _first_matched_position_id(db, uid)
        # Deactivate (don't delete) the resume: the scored match — and thus the
        # position's visibility — survives, but there's no *active* resume to tailor.
        db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid)).is_active = False
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        assert c.post(f"/api/positions/{pid}/kit", headers=h).status_code == 400


def test_kit_cascades_on_resume_and_user_delete():
    with session_scope() as db:
        uid = _seed_user(db)
        pos = _seeded_position(db, uid)
        resume = db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid))
        db.add(models.ApplicationKit(user_id=uid, position_id=pos.id, resume_id=resume.id, status="ok"))
        db.flush()
    # Replacing/deleting the tailored resume drops the (now-stale) kit.
    with session_scope() as db:
        db.delete(db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid)))
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.ApplicationKit))) == []
        # Re-add a kit, then deleting the user cascades it too.
        pos = _seeded_position(db, uid)
        db.add(models.ApplicationKit(user_id=uid, position_id=pos.id, status="ok"))
    with session_scope() as db:
        db.delete(db.get(models.User, uid))
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.ApplicationKit))) == []


# ── Applicant profile (user-level autofill data) ─────────────────────────────
def test_profile_defaults_email_when_absent(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db, email="prof@x.com")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        p = c.get("/api/profile", headers=h).json()
        # No profile saved yet -> blank default with the account email pre-filled.
        assert p["email"] == "prof@x.com" and p["first_name"] is None
        assert p["education"] == [] and p["experience"] == []
    assert c.get("/api/profile").status_code == 401  # auth required


def test_profile_upsert_and_replace_children(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db, email="pu@x.com")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        body = {
            "first_name": " Jane ", "last_name": "Doe", "phone": "555-1234",
            "authorized_to_work": True, "requires_sponsorship": False,
            "remote_preference": "remote", "gender": "Female",
            "education": [{"school": "MIT", "degree": "BS", "field_of_study": "CS"}],
            "experience": [
                {"company": "Acme", "title": "Engineer", "is_current": True},
                {"company": "Globex", "title": "Intern"},
            ],
        }
        saved = c.put("/api/profile", headers=h, json=body).json()
        assert saved["first_name"] == "Jane"  # trimmed
        assert saved["authorized_to_work"] is True and saved["requires_sponsorship"] is False
        assert saved["gender"] == "Female"
        assert [e["school"] for e in saved["education"]] == ["MIT"]
        assert [x["company"] for x in saved["experience"]] == ["Acme", "Globex"]
        assert saved["experience"][0]["is_current"] is True

        # A second save with fewer rows REPLACES the children (no leftovers).
        replaced = c.put("/api/profile", headers=h, json={
            "first_name": "Jane",
            "education": [],
            "experience": [{"company": "NewCo", "title": "Staff"}],
        }).json()
        assert replaced["education"] == []
        assert [x["company"] for x in replaced["experience"]] == ["NewCo"]
        # GET reflects the latest state.
        got = c.get("/api/profile", headers=h).json()
        assert [x["company"] for x in got["experience"]] == ["NewCo"]


def test_profile_drops_blank_child_rows(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db, email="pb@x.com")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        saved = c.put("/api/profile", headers=h, json={
            "education": [{"school": "", "degree": ""}, {"school": "CMU"}],
            "experience": [{"is_current": True}],  # only a flag -> not a real entry
        }).json()
        assert [e["school"] for e in saved["education"]] == ["CMU"]
        assert saved["experience"] == []


def test_profile_is_per_user(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid_a = _seed_user(db, email="pa@x.com")
        uid_b = _seed_user(db, email="pbb@x.com")
    ha = {"Authorization": f"Bearer {create_access_token(uid_a)}"}
    hb = {"Authorization": f"Bearer {create_access_token(uid_b)}"}
    with TestClient(app) as c:
        c.put("/api/profile", headers=ha, json={"first_name": "AAA", "phone": "111"})
        # B never sees A's data; B's default email is B's own.
        pb = c.get("/api/profile", headers=hb).json()
        assert pb["first_name"] is None and pb["email"] == "pbb@x.com"


def test_profile_extract_normalizes_model_aliases():
    """Models often ignore the field names we ask for and emit their own (name,
    location, linkedin, work_experience, dates, role, highlights). The extractor
    must still map them onto the profile shape — the real-world failure that made
    import return an empty name and no experience."""
    from app.services import profile_extract

    class AliasClient:
        model = "fake-extract"

        def chat_text(self, system, user, temperature=0.4):
            # Different keys than we asked for, plus prose around the JSON.
            return (
                "Here is the extracted profile:\n"
                '{"name": "Jane Q Doe", "email": "jane@doe.dev", '
                '"location": "Boston, MA, USA", "linkedin": "https://linkedin.com/in/jane", '
                '"work_experience": [{"company": "Acme", "role": "Senior Engineer", '
                '"dates": "2021 - Present", "highlights": ["Built APIs", "Led a team"]}], '
                '"education": [{"school": "MIT", "degree": "BS", "field": "CS", "dates": "2016 - 2020"}]}'
            )

    with session_scope() as db:
        uid = _seed_user(db, email="ex@x.com")
    with session_scope() as db:
        draft = profile_extract.extract_from_resume(db, db.get(models.User, uid), client=AliasClient())

    assert draft["first_name"] == "Jane" and draft["last_name"] == "Q Doe"
    assert draft["email"] == "jane@doe.dev"
    assert (draft["city"], draft["state_region"], draft["country"]) == ("Boston", "MA", "USA")
    assert draft["linkedin_url"] == "https://linkedin.com/in/jane"
    exp = draft["experience"][0]
    assert exp["company"] == "Acme" and exp["title"] == "Senior Engineer"
    assert exp["start_date"] == "2021" and exp["is_current"] is True
    # Bullets become one "- " line each.
    assert exp["description"] == "- Built APIs\n- Led a team"
    edu = draft["education"][0]
    assert edu["school"] == "MIT" and edu["field_of_study"] == "CS"
    assert edu["start_date"] == "2016" and edu["end_date"] == "2020"


def test_format_description_bullets_and_ordered_lists():
    from app.services.profile_extract import _format_description as fmt

    # A list of bullets -> one "- " line each (existing markers stripped).
    assert fmt(["Built APIs", "Led a team"]) == "- Built APIs\n- Led a team"
    assert fmt(["• Built APIs", "- Led a team"]) == "- Built APIs\n- Led a team"
    # Inline glyphs and newlines both split into "- " lines.
    assert fmt("• Built APIs • Led a team") == "- Built APIs\n- Led a team"
    assert fmt("Built APIs\nLed a team") == "- Built APIs\n- Led a team"
    # Ordered list: each item on its own line, numbering preserved.
    assert fmt("1. Built APIs 2. Led a team") == "1. Built APIs\n2. Led a team"
    # Plain prose stays a single block — no spurious leading dash.
    prose = "Led a team that shipped the billing platform."
    assert fmt(prose) == prose
    # Description priority + empties.
    assert fmt(None, ["Did X"]) == "- Did X"
    assert fmt("", None, []) == ""


def test_profile_extract_requires_active_resume():
    from app.services import profile_extract

    with session_scope() as db:
        uid = _seed_user(db, email="nr@x.com")
        db.delete(db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid)))
    with session_scope() as db:
        with pytest.raises(profile_extract.NoResumeError):
            profile_extract.extract_from_resume(db, db.get(models.User, uid), client=object())


def test_profile_import_endpoint(monkeypatch):
    from app.auth import create_access_token
    from app.services import evaluator, profile_extract

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    monkeypatch.setattr(profile_extract, "extract_from_resume",
                        lambda db, user, **k: {"first_name": "Imported", "education": [], "experience": []})
    with session_scope() as db:
        uid = _seed_user(db, email="imp@x.com")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        out = c.post("/api/profile/import-from-resume", headers=h).json()
        assert out["first_name"] == "Imported"
        # Import does NOT persist — the saved profile is still empty.
        assert c.get("/api/profile", headers=h).json()["first_name"] is None


def test_profile_cascades_on_user_delete():
    with session_scope() as db:
        uid = _seed_user(db, email="pc@x.com")
        db.add(models.ApplicantProfile(user_id=uid, first_name="Jane"))
        db.add(models.ProfileEducation(user_id=uid, school="MIT"))
        db.add(models.ProfileExperience(user_id=uid, company="Acme"))
        db.flush()
    with session_scope() as db:
        db.delete(db.get(models.User, uid))
    with session_scope() as db:
        assert list(db.scalars(matcher.select(models.ApplicantProfile))) == []
        assert list(db.scalars(matcher.select(models.ProfileEducation))) == []
        assert list(db.scalars(matcher.select(models.ProfileExperience))) == []


def test_job_list_surfaces_kit_status(monkeypatch):
    """The job list overlays each position's application-kit status (live, like
    'applied') so the row can show a generating/ready icon."""
    from app.auth import create_access_token
    from app.services import evaluator

    monkeypatch.setattr(evaluator, "ensure_running", lambda uid: None)
    with session_scope() as db:
        uid = _seed_user(db)
    with session_scope() as db:
        matcher.run_for_user(db, db.get(models.User, uid), client=GoodClient(), filter_client=FilterPass())
        pid = _first_matched_position_id(db, uid)
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    with TestClient(app) as c:
        # No kit yet -> kit_status is null.
        before = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert all(it["kit_status"] is None for it in before["items"])
    with session_scope() as db:
        db.add(models.ApplicationKit(user_id=uid, position_id=pid, status="generating"))
    with TestClient(app) as c:
        gen = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert any(it["position_id"] == pid and it["kit_status"] == "generating" for it in gen["items"])
    with session_scope() as db:
        db.scalar(matcher.select(models.ApplicationKit)).status = "ok"
    with TestClient(app) as c:
        done = c.get("/api/job-lists/latest?category=matching&limit=10", headers=h).json()
        assert any(it["position_id"] == pid and it["kit_status"] == "ok" for it in done["items"])
