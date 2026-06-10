"""Smoke tests. Run with: pytest -q   (after `pip install -e '.[dev]'`).

`test_live_greenhouse` hits a real public Greenhouse board and is skipped when
offline / when JOBSCOUT_SKIP_NET=1."""
from __future__ import annotations

import os
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


def test_prefilter_excludes_and_titles():
    from app.models import Interest, Position
    from app.services.matcher import _passes_prefilter

    interest = Interest(
        label="be", title_keywords="backend, platform",
        locations="remote, berlin", exclude_keywords="manager",
    )
    assert _passes_prefilter(Position(title="Senior Backend Engineer", location="Remote (EU)"), interest)
    assert not _passes_prefilter(Position(title="Engineering Manager", location="Remote"), interest)
    assert not _passes_prefilter(Position(title="Backend Engineer", location="New York"), interest)
    assert not _passes_prefilter(Position(title="Sales Lead", location="Remote"), interest)
    # No location data → don't drop on the location gate; defer to the LLM.
    assert _passes_prefilter(Position(title="Backend Engineer", location=None), interest)


def test_infer_ats():
    from app.services.scraper import _infer_ats

    assert _infer_ats("https://boards.greenhouse.io/anthropic") == ("greenhouse", "anthropic")
    assert _infer_ats("https://jobs.lever.co/openai") == ("lever", "openai")
    assert _infer_ats("https://jobs.ashbyhq.com/openai") == ("ashby", "openai")
    assert _infer_ats("https://example.com/careers") == ("html", None)
    assert _infer_ats(None) == ("html", None)


def test_greenhouse_parse_offline(monkeypatch):
    """Parse a canned Greenhouse payload without network."""
    import json

    import app.services.scraper as scraper

    payload = {
        "jobs": [
            {"id": 123, "title": "Backend Engineer",
             "location": {"name": "Remote"}, "departments": [{"name": "Eng"}],
             "absolute_url": "https://x/123", "content": "<p>Build <b>things</b></p>",
             "updated_at": "2026-01-02T00:00:00Z"}
        ]
    }

    # Stub the SSRF-guarded byte fetch so no DNS/network is touched.
    monkeypatch.setattr(scraper, "_fetch_bytes", lambda url: json.dumps(payload).encode())
    out = scraper.scrape_greenhouse("acme")
    assert len(out) == 1
    p = out[0]
    assert p.external_id == "123" and p.title == "Backend Engineer"
    assert p.location == "Remote" and p.url == "https://x/123"
    assert p.description == "Build things"  # html stripped


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
    monkeypatch.setattr(scraper, "scrape_greenhouse", lambda token: captured.setdefault("t", token) or [])
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
