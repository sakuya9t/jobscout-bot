"""Career-page scraping. ATS-API-first (Greenhouse / Lever / Ashby), with a
generic HTML fallback. Every adapter returns a list of ``ScrapedPosition`` and
all network/parse failures raise ``ScrapeError`` so the matcher can record them
per-company without aborting the run."""
from __future__ import annotations

import hashlib
import ipaddress
import json as _json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import settings
from ..timeutil import to_naive_utc

# Redirects are not auto-followed; each hop is SSRF-validated up to this many.
_MAX_REDIRECTS = 3


class ScrapeError(RuntimeError):
    pass


@dataclass
class ScrapedPosition:
    external_id: str
    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    url: str | None = None
    description: str | None = None
    posted_at: datetime | None = None


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _client() -> httpx.Client:
    # follow_redirects=False: we follow manually so every hop is SSRF-validated.
    return httpx.Client(
        headers={"User-Agent": settings.scrape_user_agent},
        timeout=30,
        follow_redirects=False,
    )


def _host_is_public(host: str) -> bool:
    """Resolve ``host`` and require every address to be a global/public IP.
    Blocks SSRF to loopback, private, link-local (incl. cloud metadata
    169.254.169.254), reserved, multicast, and unspecified ranges."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # ``is_global`` is the single source of truth for "publicly routable":
        # it already excludes private, loopback, link-local (incl. the
        # 169.254.169.254 metadata endpoint), reserved, multicast, unspecified,
        # AND ranges the explicit flags miss — notably 100.64.0.0/10 (CGNAT).
        if not addr.is_global:
            return False
    return True


def _validate_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts that resolve to non-public IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ScrapeError(f"refusing non-http(s) URL: {url!r}")
    host = parsed.hostname
    if not host or not _host_is_public(host):
        raise ScrapeError(f"refusing to fetch private/unresolvable host: {host!r}")


def _is_transient(exc: BaseException) -> bool:
    """Retry only on errors that might clear on their own: connect/read failures
    and 429/5xx. A 404 or an SSRF rejection (ScrapeError) is permanent."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8),
    reraise=True,
)
def _fetch_bytes_once(url: str) -> bytes:
    """One SSRF-guarded GET attempt (retried on transient errors by the
    decorator). Validates the host on every redirect hop and caps body size."""
    # Cap on any single response we'll buffer, to bound memory from a hostile or
    # misconfigured endpoint (we stream and abort past this).
    max_bytes = settings.scrape_max_response_mb * 1024 * 1024
    with _client() as c:
        for _ in range(_MAX_REDIRECTS + 1):
            _validate_url(url)
            with c.stream("GET", url) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if loc:
                        url = urljoin(url, loc)
                        continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ScrapeError(
                            f"response from {url} exceeds {max_bytes} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ScrapeError(f"too many redirects from {url}")


def _fetch_bytes(url: str) -> bytes:
    """SSRF-guarded GET with transient-error retries; wraps leftover transport
    errors as ``ScrapeError`` so callers only deal with one exception type."""
    try:
        return _fetch_bytes_once(url)
    except httpx.HTTPError as exc:
        raise ScrapeError(f"fetch {url}: {exc}") from exc


def _fetch_json(url: str) -> dict | list:
    try:
        return _json.loads(_fetch_bytes(url))
    except _json.JSONDecodeError as exc:
        raise ScrapeError(f"invalid JSON from {url}: {exc}") from exc


def _fetch_text(url: str) -> str:
    return _fetch_bytes(url).decode("utf-8", errors="replace")


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _http_url(value) -> str | None:
    """Keep only http(s) URLs from third-party ATS JSON. The posting URL is
    rendered as a link in the dashboard and Telegram, so a hostile/buggy board
    must not be able to smuggle a javascript:/data: scheme into an href."""
    if not value:
        return None
    url = str(value).strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _parse_ts(value) -> datetime | None:
    """Parse an ATS timestamp to naive UTC (the app-wide storage convention)."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            # Lever/Ashby use epoch millis.
            parsed = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None
    return to_naive_utc(parsed)


# ── ATS adapters ─────────────────────────────────────────────────────────────
def scrape_greenhouse(token: str) -> list[ScrapedPosition]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _fetch_json(url)
    out = []
    for job in data.get("jobs", []):
        out.append(
            ScrapedPosition(
                external_id=str(job.get("id")),
                title=job.get("title", "Untitled"),
                location=(job.get("location") or {}).get("name"),
                department=(job.get("departments") or [{}])[0].get("name"),
                url=_http_url(job.get("absolute_url")),
                description=_strip_html(job.get("content")),
                posted_at=_parse_ts(job.get("updated_at") or job.get("first_published")),
            )
        )
    return out


def scrape_lever(token: str) -> list[ScrapedPosition]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = _fetch_json(url)
    out = []
    for job in data:
        cats = job.get("categories") or {}
        out.append(
            ScrapedPosition(
                external_id=str(job.get("id")),
                title=job.get("text", "Untitled"),
                location=cats.get("location"),
                department=cats.get("team") or cats.get("department"),
                employment_type=cats.get("commitment"),
                url=_http_url(job.get("hostedUrl")),
                description=_strip_html(job.get("descriptionPlain") or job.get("description")),
                posted_at=_parse_ts(job.get("createdAt")),
            )
        )
    return out


def scrape_ashby(token: str) -> list[ScrapedPosition]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    data = _fetch_json(url)
    out = []
    for job in data.get("jobs", []):
        out.append(
            ScrapedPosition(
                external_id=str(job.get("id")),
                title=job.get("title", "Untitled"),
                location=job.get("location"),
                department=job.get("department") or job.get("team"),
                employment_type=job.get("employmentType"),
                url=_http_url(job.get("jobUrl")),
                description=_strip_html(job.get("descriptionHtml")),
                posted_at=_parse_ts(job.get("publishedAt")),
            )
        )
    return out


# ── Google careers (no ATS API) ──────────────────────────────────────────────
# Google's careers site is a JS SPA, but it *server-side-renders* each results
# page's jobs into the HTML as an ``AF_initDataCallback({... data:[...]})`` blob,
# and ``?page=N`` paginates over plain HTTP. So we parse that embedded JSON rather
# than running a headless browser. The record layout is positional and could shift
# if Google reworks the page, so every field access is defensive and best-effort.
_GOOGLE_RESULTS_URL = "https://www.google.com/about/careers/applications/jobs/results/"
_AF_INIT = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_AF_DATA = re.compile(r"data:(\[.*\])\s*,\s*sideChannel", re.S)


def _looks_like_google_job(rec) -> bool:
    # A job record starts with a long numeric id and a title string.
    return (
        isinstance(rec, list)
        and len(rec) >= 3
        and isinstance(rec[0], str)
        and rec[0].isdigit()
        and len(rec[0]) >= 15
        and isinstance(rec[1], str)
    )


def _find_google_jobs(node) -> list | None:
    """Depth-first search for the list of job records inside a parsed data blob."""
    if isinstance(node, list):
        if node and all(_looks_like_google_job(r) for r in node):
            return node
        for el in node:
            found = _find_google_jobs(el)
            if found:
                return found
    return None


def _google_records(html: str) -> list:
    """Pull the job-record list out of a results page's embedded JSON."""
    for blob in _AF_INIT.findall(html):
        m = _AF_DATA.search(blob)
        if not m:
            continue
        try:
            data = _json.loads(m.group(1))
        except _json.JSONDecodeError:
            continue
        found = _find_google_jobs(data)
        if found:
            return found
    return []


def _google_location(rec) -> str | None:
    """Locations live in a ``[["City, ST, USA", [...], ...], ...]`` sub-array;
    find it by shape so we don't depend on its exact index."""
    for field in rec[3:]:
        if (
            isinstance(field, list)
            and field
            and isinstance(field[0], list)
            and field[0]
            and isinstance(field[0][0], str)
        ):
            return field[0][0]
    return None


def _google_position(rec) -> ScrapedPosition:
    job_id = rec[0]
    # Description = "about the job" + "minimum qualifications", each a [None, html]
    # pair; concatenate whatever HTML strings the record carries.
    desc_parts = [
        f[1]
        for f in rec[2:]
        if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str) and "<" in f[1]
    ]
    return ScrapedPosition(
        external_id=job_id,
        title=(rec[1] or "Untitled")[:300],
        location=_google_location(rec),
        url=_http_url(rec[2] if len(rec) > 2 else None) or f"{_GOOGLE_RESULTS_URL}{job_id}",
        description=_strip_html(" ".join(desc_parts)) if desc_parts else None,
    )


def scrape_google(careers_url: str | None, max_pages: int) -> list[ScrapedPosition]:
    base = careers_url or _GOOGLE_RESULTS_URL
    sep = "&" if urlparse(base).query else "?"
    out: list[ScrapedPosition] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        records = _google_records(_fetch_text(f"{base}{sep}page={page}"))
        if not records:
            break  # past the last page (or layout changed)
        added = 0
        for rec in records:
            try:
                pos = _google_position(rec)
            except (IndexError, TypeError):
                continue  # skip a malformed record rather than aborting the run
            if pos.external_id in seen:
                continue
            seen.add(pos.external_id)
            out.append(pos)
            added += 1
        if added == 0:
            break  # a full page of already-seen ids: no progress, stop paging
    return out


# ── Generic HTML fallback ────────────────────────────────────────────────────
_JOB_HINT = re.compile(r"(job|career|position|opening|vacanc|role|gh_jid|/jobs/)", re.I)


def scrape_html(careers_url: str, max_positions: int) -> list[ScrapedPosition]:
    """Best-effort: pull anchors that look like individual job links. This is a
    fallback for sites without a known ATS; it won't have descriptions.

    TODO(browser): JS-rendered pages return no useful anchors here. A headless
    Playwright path gated on ``settings.use_browser`` would fix that; not yet
    implemented (see config.py)."""
    from bs4 import BeautifulSoup

    html = _fetch_text(careers_url)

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[ScrapedPosition] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 3:
            continue
        # Skip non-navigational schemes (mailto:/tel:/javascript:/#fragments) so
        # e.g. "mailto:jobs@acme.com" isn't mistaken for a posting by _JOB_HINT.
        scheme = urlparse(href).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            continue
        if href.startswith("#"):
            continue
        if not (_JOB_HINT.search(href) or _JOB_HINT.search(text)):
            continue
        # urljoin correctly resolves relative, absolute, and scheme-relative
        # ("//host/path") hrefs against the careers page.
        full = urljoin(careers_url, href)
        if not full.startswith(("http://", "https://")) or full in seen:
            continue
        seen.add(full)
        out.append(ScrapedPosition(external_id=_hash(full), title=text[:300], url=full))
        if len(out) >= max_positions:
            break
    return out


# ── Dispatch ─────────────────────────────────────────────────────────────────
def _infer_ats(careers_url: str | None) -> tuple[str, str | None]:
    """Return (ats_type, token) guessed from the careers URL host/path."""
    if not careers_url:
        return "html", None
    host = urlparse(careers_url).netloc.lower()
    path = urlparse(careers_url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if "greenhouse.io" in host and parts:
        # parts[0] is the board token; a pasted *job* URL
        # (boards.greenhouse.io/<token>/jobs/<id>) must still resolve to <token>,
        # not the trailing job id.
        return "greenhouse", parts[0]
    if "lever.co" in host and parts:
        return "lever", parts[0]
    if "ashbyhq.com" in host and parts:
        return "ashby", parts[0]
    if "google.com" in host and "careers" in path:
        return "google", None
    return "html", None


def scrape_company(company) -> list[ScrapedPosition]:
    """Resolve the right adapter for a Company ORM object and run it.
    Truncates to the configured per-company cap."""
    ats_type = company.ats_type or "auto"
    token = company.ats_token

    if ats_type == "auto":
        ats_type, inferred = _infer_ats(company.careers_url)
        token = token or inferred

    if ats_type in {"greenhouse", "lever", "ashby"} and not token:
        # Inference may still recover a token from the URL.
        _, token = _infer_ats(company.careers_url)
        if not token:
            raise ScrapeError(f"{company.name}: ats_type={ats_type} but no ats_token/url token")

    if ats_type == "greenhouse":
        results = scrape_greenhouse(token)
    elif ats_type == "lever":
        results = scrape_lever(token)
    elif ats_type == "ashby":
        results = scrape_ashby(token)
    elif ats_type == "google":
        results = scrape_google(company.careers_url, settings.scrape_google_max_pages)
    elif ats_type == "html":
        if not company.careers_url:
            raise ScrapeError(f"{company.name}: html scrape needs careers_url")
        results = scrape_html(company.careers_url, settings.scrape_max_positions_per_company)
    else:
        raise ScrapeError(f"{company.name}: unknown ats_type {ats_type!r}")

    # No global cap: the ATS adapters return a company's full board (large boards
    # have 200+ postings, and external_id dedup means a dropped one is never seen
    # again). The per-company limit only bounds the noisy HTML fallback, which
    # applies it itself via the ``max_positions`` argument to ``scrape_html``.
    return results
