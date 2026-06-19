"""Smoke tests. Run with: pytest -q   (after `pip install -e '.[dev]'`).

`test_live_greenhouse` hits a real public Greenhouse board and is skipped when
offline / when JOBSCOUT_SKIP_NET=1."""
from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest


def test_resume_parser_txt():
    from app.services.resume_parser import extract_text

    text = extract_text("cv.txt", b"Jane Doe\nSenior  Backend Engineer\n\n\n\nPython, Go")
    assert "Senior Backend Engineer" in text  # whitespace collapsed
    assert "\n\n\n" not in text


def test_resume_parser_rejects_unknown():
    from app.services.resume_parser import extract_text

    with pytest.raises(ValueError):
        extract_text("cv.rtf", b"whatever")


def test_resume_parser_rejects_corrupt_pdf():
    """A malformed PDF must surface as ValueError (→ HTTP 422), not a raw
    PdfReadError bubbling up as a 500."""
    from app.services.resume_parser import extract_text

    with pytest.raises(ValueError):
        extract_text("cv.pdf", b"definitely not a real pdf")


def test_prefilter_is_excludes_only():
    """The cheap pre-filter is negative-only: it drops a posting solely on an
    explicit exclude keyword. Title/location no longer gate (the LLM judges those),
    so a non-excluded posting always passes regardless of title/location match."""
    from app.models import Interest, Position
    from app.services.matcher import _passes_prefilter

    interest = Interest(
        label="be", title_keywords="backend, platform",
        locations="remote, berlin", exclude_keywords="manager",
    )
    assert _passes_prefilter(Position(title="Senior Backend Engineer", location="Remote (EU)"), interest)
    assert not _passes_prefilter(Position(title="Engineering Manager", location="Remote"), interest)  # excluded
    # Title/location no longer gate — these reach the LLM instead of being dropped.
    assert _passes_prefilter(Position(title="Backend Engineer", location="New York"), interest)
    assert _passes_prefilter(Position(title="Sales Lead", location="Remote"), interest)
    assert _passes_prefilter(Position(title="Backend Engineer", location=None), interest)
    # With no exclude keywords, everything passes.
    assert _passes_prefilter(Position(title="Anything"), Interest(label="x"))


def test_parse_filter_batch():
    """The batched cheap-filter reply is parsed robustly: JSON array first, then
    'n: yes/no' lines, then a lone global YES/NO, and fail-OPEN (keep) otherwise."""
    from types import SimpleNamespace

    from app.services.matcher import _parse_filter_batch

    pos = [SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=30)]

    v = _parse_filter_batch('[{"id":1,"match":true},{"id":2,"match":false},{"id":3,"match":true}]', pos)
    assert v[10][0] is True and v[20][0] is False and v[30][0] is True
    # The per-posting `reason` is captured — it's what the "not a match" pill explains.
    v = _parse_filter_batch('[{"id":1,"match":false,"reason":"Senior role; you want entry-level"}]', pos)
    assert v[10] == (False, "Senior role; you want entry-level")
    # Wrapped in prose / code fences still parses.
    assert _parse_filter_batch('sure:\n```json\n[{"id":1,"match":false}]\n```', pos)[10][0] is False
    # Line fallback.
    v = _parse_filter_batch("1: yes\n2: no\n3: yes", pos)
    assert v[10][0] is True and v[20][0] is False and v[30][0] is True
    # A lone global YES/NO applies to all.
    assert all(m is False for m, _ in _parse_filter_batch("NO", pos).values())
    # Unparseable → fail open (keep everything for the stricter scoring step).
    assert all(m is True for m, _ in _parse_filter_batch("???", pos).values())
    # Partial JSON → named id honored, the rest fail open.
    v = _parse_filter_batch('[{"id":2,"match":false}]', pos)
    assert v[20][0] is False and v[10][0] is True and v[30][0] is True


def test_infer_ats():
    from app.services.scraper import _infer_ats

    assert _infer_ats("https://boards.greenhouse.io/anthropic") == ("greenhouse", "anthropic")
    # A pasted *job* URL must still resolve to the board token, not the job id.
    assert _infer_ats("https://boards.greenhouse.io/anthropic/jobs/123") == ("greenhouse", "anthropic")
    assert _infer_ats("https://jobs.lever.co/openai") == ("lever", "openai")
    assert _infer_ats("https://jobs.ashbyhq.com/openai") == ("ashby", "openai")
    # Google careers has no ATS token; it routes to the dedicated "google" adapter.
    assert _infer_ats("https://www.google.com/about/careers/applications/jobs/results/") == ("google", None)
    # A *.eightfold.ai tenant routes to the eightfold adapter (domain via ats_token).
    assert _infer_ats("https://nvidia.eightfold.ai/careers") == ("eightfold", None)
    # A custom-domain Eightfold board can't be inferred — needs explicit ats_type.
    assert _infer_ats("https://jobs.nvidia.com/careers") == ("html", None)
    assert _infer_ats("https://example.com/careers") == ("html", None)
    assert _infer_ats(None) == ("html", None)


def test_scrape_sitemap_pulls_real_descriptions(monkeypatch):
    import app.services.scraper as scraper

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://www.citadel.com/careers/details/commodities-analyst/</loc>
        <lastmod>2026-06-18T23:14:20+00:00</lastmod>
      </url>
      <url>
        <loc>https://www.citadel.com/careers/details/quant-researcher/</loc>
      </url>
      <url><loc>https://www.citadel.com/careers/details/commodities-analyst/</loc></url>
      <url><loc>mailto:jobs@citadel.com</loc></url>
    </urlset>"""
    # The first detail page has a JSON-LD JobPosting; the second has none (fallback).
    detail = """<html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org/","@type":"JobPosting",
       "title":"Commodities Analyst",
       "description":"<p>Analyse <b>commodities</b> markets.</p>",
       "datePosted":"2026-06-01","employmentType":"full_time",
       "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
         "addressLocality":"New York","addressRegion":"NY","addressCountry":"US"}}}
      </script></head><body></body></html>"""

    def fake_fetch(url):
        if url.endswith(".xml"):
            return xml
        if "commodities-analyst" in url:
            return detail
        return "<html><body>no structured data</body></html>"

    monkeypatch.setattr(scraper, "_fetch_impersonated", fake_fetch)

    out = scraper.scrape_sitemap("https://www.citadel.com/career-sitemap.xml")
    # The duplicate <loc> is deduped and the non-http <loc> dropped.
    assert len(out) == 2
    first = out[0]
    assert first.url == "https://www.citadel.com/careers/details/commodities-analyst/"
    # Real fields come from the JSON-LD (title/description/location/date), not the slug.
    assert first.title == "Commodities Analyst"
    assert first.description == "Analyse commodities markets."  # HTML stripped
    assert first.location == "New York, NY, US"
    assert first.employment_type == "full_time"
    assert first.posted_at is not None and first.posted_at.year == 2026
    # No JSON-LD on the second page: fall back to the slug title, no description (the
    # matcher will skip it) — never a crash.
    assert out[1].title == "Quant Researcher" and out[1].description is None
    assert all(p.external_id for p in out)


def test_eightfold_domain_derivation():
    from app.services.scraper import _eightfold_domain

    # Explicit ats_token always wins.
    assert _eightfold_domain("https://jobs.nvidia.com/careers", "nvidia.com") == "nvidia.com"
    # Otherwise strip a leading careers subdomain to get the registrable domain.
    assert _eightfold_domain("https://jobs.nvidia.com/careers", None) == "nvidia.com"
    assert _eightfold_domain("https://careers.acme.co.uk/x", None) == "acme.co.uk"
    # An already-registrable host is left alone.
    assert _eightfold_domain("https://acme.com/careers", None) == "acme.com"


def _eightfold_page(ids_and_days):
    """Build a PCSX search payload: each (id, days_ago) becomes one position."""
    from app.timeutil import utcnow

    now = utcnow()
    return {
        "status": 200,
        "data": {
            "count": 999,
            "positions": [
                {
                    "id": jid,
                    "name": f"Role {jid}",
                    "locations": ["Remote, US"],
                    "department": "Eng",
                    "workLocationOption": "remote",
                    "postedTs": int((now - timedelta(days=days)).timestamp()),
                    "positionUrl": f"/careers/job/{jid}",
                }
                for jid, days in ids_and_days
            ],
        },
    }


def test_eightfold_parses_and_builds_absolute_urls(monkeypatch):
    import app.services.scraper as scraper

    monkeypatch.setattr(
        scraper, "_eightfold_json",
        lambda url: _eightfold_page([(111, 1)]) if "start=0" in url else _eightfold_page([]),
    )
    out = scraper.scrape_eightfold("https://jobs.nvidia.com/careers", "nvidia.com", 60)
    assert len(out) == 1
    p = out[0]
    assert p.external_id == "111" and p.title == "Role 111"
    assert p.location == "Remote, US" and p.department == "Eng"
    # positionUrl is relative; it must resolve against the careers host.
    assert p.url == "https://jobs.nvidia.com/careers/job/111"
    assert p.posted_at is not None


def test_eightfold_paginates_and_stops_past_age_cutoff(monkeypatch):
    import app.services.scraper as scraper
    from app.config import settings

    monkeypatch.setattr(settings, "scrape_max_age_days", 30)
    # Page 0: recent. Page 1: all older than the 30-day cutoff -> stop, don't page 2.
    pages = {
        "start=0": _eightfold_page([(1, 2), (2, 5)]),
        "start=10": _eightfold_page([(3, 90), (4, 120)]),
        "start=20": _eightfold_page([(5, 1)]),  # must never be fetched
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        for key, payload in pages.items():
            if key in url:
                return payload
        return _eightfold_page([])

    monkeypatch.setattr(scraper, "_eightfold_json", fake_fetch)
    out = scraper.scrape_eightfold("https://jobs.nvidia.com/careers", "nvidia.com", 60)
    ids = {p.external_id for p in out}
    # Both pages are pulled (the stale page is trimmed later by _within_max_age),
    # but paging stops before start=20.
    assert ids == {"1", "2", "3", "4"}
    assert not any("start=20" in u for u in fetched)


def test_eightfold_coverage_signals(monkeypatch):
    """The Eightfold walk reports coverage for removal reconciliation: 'full' when it
    reaches the end of the board, a datetime floor when it stops at the age cutoff,
    and None when it caps out at max_pages first."""
    import app.services.scraper as scraper
    from datetime import datetime
    from app.config import settings

    monkeypatch.setattr(settings, "scrape_max_age_days", 30)

    # Reaches an empty page → whole board seen → "full".
    monkeypatch.setattr(scraper, "_eightfold_json",
        lambda url: _eightfold_page([(111, 1)]) if "start=0" in url else _eightfold_page([]))
    _, cov_full = scraper._eightfold_paged("https://jobs.nvidia.com/careers", "nvidia.com", 60)
    assert cov_full == "full"

    # A whole page predates the 30-day cutoff → covered down to the cutoff → datetime.
    pages = {"start=0": _eightfold_page([(1, 2)]), "start=10": _eightfold_page([(2, 90)])}
    monkeypatch.setattr(scraper, "_eightfold_json",
        lambda url: next((p for k, p in pages.items() if k in url), _eightfold_page([])))
    _, cov_floor = scraper._eightfold_paged("https://jobs.nvidia.com/careers", "nvidia.com", 60)
    assert isinstance(cov_floor, datetime)

    # Hits max_pages before the end or the cutoff → partial → None.
    monkeypatch.setattr(scraper, "_eightfold_json", lambda url: _eightfold_page([(1, 2)]))
    _, cov_partial = scraper._eightfold_paged("https://jobs.nvidia.com/careers", "nvidia.com", 1)
    assert cov_partial is None


@pytest.mark.skipif(os.environ.get("JOBSCOUT_SKIP_NET") == "1", reason="network disabled")
def test_live_eightfold():
    from app.services.scraper import scrape_eightfold

    try:
        jobs = scrape_eightfold("https://jobs.nvidia.com/careers", "nvidia.com", 2)
    except Exception as exc:  # network / board changes shouldn't hard-fail CI
        pytest.skip(f"network unavailable: {exc}")
    assert isinstance(jobs, list)
    if jobs:
        assert jobs[0].title and jobs[0].external_id and jobs[0].url


def test_scrape_company_does_not_cap_ats_results(monkeypatch):
    """ATS adapters return the full board; only the noisy HTML fallback is capped."""
    import app.services.scraper as scraper

    big = [scraper.ScrapedPosition(external_id=str(i), title=f"Role {i}") for i in range(60)]
    monkeypatch.setattr(scraper, "scrape_greenhouse", lambda token: list(big))
    company = SimpleNamespace(
        name="Acme", ats_type="greenhouse", ats_token="acme", careers_url=None
    )
    out = scraper.scrape_company(company)
    assert len(out.positions) == 60  # default per-company cap is 40 — must NOT truncate ATS results
    assert len(out.live_external_ids) == 60  # full board id set for availability reconcile


def test_scrape_company_filters_stale_postings(monkeypatch):
    """Only postings dated within scrape_max_age_days are pulled; undated postings
    (Google/HTML carry no date) are always kept."""
    import app.services.scraper as scraper
    from datetime import timedelta

    from app.timeutil import utcnow

    recent = scraper.ScrapedPosition(external_id="new", title="Fresh",
                                     posted_at=utcnow() - timedelta(days=5))
    stale = scraper.ScrapedPosition(external_id="old", title="Stale",
                                    posted_at=utcnow() - timedelta(days=90))
    undated = scraper.ScrapedPosition(external_id="undated", title="No date")  # posted_at=None
    monkeypatch.setattr(scraper, "scrape_greenhouse", lambda token: [recent, stale, undated])
    monkeypatch.setattr(scraper.settings, "scrape_max_age_days", 30)

    company = SimpleNamespace(name="Acme", ats_type="greenhouse", ats_token="acme", careers_url=None)
    result = scraper.scrape_company(company)
    kept = {p.external_id for p in result.positions}
    assert kept == {"new", "undated"}  # stale dropped; recent + undated kept
    # The live id set is the *full* board (pre-age-filter), so a still-live stale
    # posting is never mistaken for removed during reconcile.
    assert result.live_external_ids == {"new", "old", "undated"}


def test_within_max_age_zero_disables_filter():
    """max_age_days=0 keeps everything, however old."""
    import app.services.scraper as scraper
    from datetime import timedelta

    from app.timeutil import utcnow

    ancient = scraper.ScrapedPosition(external_id="x", title="Ancient",
                                      posted_at=utcnow() - timedelta(days=900))
    assert scraper._within_max_age([ancient], 0) == [ancient]


def test_greenhouse_parse_offline(monkeypatch):
    """Parse a canned Greenhouse payload without network."""
    import json

    import app.services.scraper as scraper

    payload = {
        "jobs": [
            {"id": 123, "title": "Backend Engineer",
             "location": {"name": "Remote"}, "departments": [{"name": "Eng"}],
             "absolute_url": "https://x/123", "content": "<p>Build <b>things</b></p>",
             "updated_at": "2026-01-02T00:00:00Z"},
            # A hostile/buggy board must not smuggle a javascript: scheme into
            # the stored url (it ends up inside an href in dashboard/Telegram).
            {"id": 124, "title": "Evil Role", "absolute_url": "javascript:alert(1)"},
        ]
    }

    # Stub the SSRF-guarded byte fetch so no DNS/network is touched.
    monkeypatch.setattr(scraper, "_fetch_bytes", lambda url: json.dumps(payload).encode())
    out = scraper.scrape_greenhouse("acme")
    assert len(out) == 2
    p = out[0]
    assert p.external_id == "123" and p.title == "Backend Engineer"
    assert p.location == "Remote" and p.url == "https://x/123"
    assert p.description == "Build things"  # html stripped
    assert out[1].url is None  # non-http(s) scheme dropped


def test_ssrf_blocks_private_and_nonhttp(monkeypatch):
    """_validate_url rejects private/loopback hosts and non-http(s) schemes."""
    import app.services.scraper as scraper

    with pytest.raises(scraper.ScrapeError):
        scraper._validate_url("file:///etc/passwd")
    with pytest.raises(scraper.ScrapeError):
        scraper._validate_url("ftp://example.com/x")
    # Force resolution to a loopback address.
    monkeypatch.setattr(scraper.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(scraper.ScrapeError):
        scraper._validate_url("http://internal.local/admin")
    # Link-local cloud-metadata address is blocked too.
    monkeypatch.setattr(scraper.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))])
    with pytest.raises(scraper.ScrapeError):
        scraper._validate_url("http://metadata/latest")


def test_html_scrape_filters_junk(monkeypatch):
    """mailto:/#/javascript: links are ignored; relative + scheme-relative job
    links are resolved with urljoin; descriptions are absent (fallback)."""
    import app.services.scraper as scraper

    html = """
      <a href="mailto:jobs@acme.com">Email jobs</a>
      <a href="#section">Jobs anchor</a>
      <a href="javascript:void(0)">Apply job</a>
      <a href="/jobs/123">Senior Backend Engineer</a>
      <a href="//cdn.acme.com/careers/9">Platform Role</a>
      <a href="https://acme.com/about">About us</a>
    """
    monkeypatch.setattr(scraper, "_fetch_text", lambda url: html)
    out = scraper.scrape_html("https://acme.com/careers", 40)
    urls = {p.url for p in out}
    assert "https://acme.com/jobs/123" in urls
    assert "https://cdn.acme.com/careers/9" in urls
    assert not any(u.startswith("mailto:") for u in urls)
    assert not any("javascript" in u for u in urls)
    assert all(p.description is None for p in out)


def test_dispatch_uses_inferred_token(monkeypatch):
    import app.services.scraper as scraper

    captured = {}
    monkeypatch.setattr(scraper, "scrape_greenhouse", lambda token: captured.update(t=token) or [])
    company = SimpleNamespace(
        name="Acme", ats_type="auto", ats_token=None,
        careers_url="https://boards.greenhouse.io/acme",
    )
    scraper.scrape_company(company)
    assert captured["t"] == "acme"


@pytest.mark.skipif(os.environ.get("JOBSCOUT_SKIP_NET") == "1", reason="network disabled")
def test_live_greenhouse():
    from app.services.scraper import scrape_greenhouse

    try:
        jobs = scrape_greenhouse("anthropic")  # public board
    except Exception as exc:  # network / board changes shouldn't hard-fail CI
        pytest.skip(f"network unavailable: {exc}")
    assert isinstance(jobs, list)
    if jobs:
        assert jobs[0].title and jobs[0].external_id
