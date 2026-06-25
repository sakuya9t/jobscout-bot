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
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import settings
from ..timeutil import to_naive_utc, utcnow

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


# Full-board ATS APIs whose single response is the company's *entire* live listing,
# so an id's absence reliably means "removed". Paginated sources (Google/Eightfold)
# report their coverage per-scrape instead (see ScrapeResult.coverage).
_AUTHORITATIVE_ATS = frozenset({"greenhouse", "lever", "ashby"})


@dataclass
class ScrapeResult:
    """One company's scrape: ``positions`` is the age-filtered set to upsert (same
    rows we've always stored); ``live_external_ids`` is the *unfiltered* set of ids
    the board currently lists, used to reconcile availability.

    ``coverage`` says how much of the board ``live_external_ids`` represents, which
    bounds what a crawl may infer is removed:
      * ``"full"``   – the complete board; any stored id that's absent is closed.
      * a ``datetime`` floor – a newest-first walk that reached the age cutoff, so the
        board is fully covered for postings listed on/after the floor; absent ids
        newer than it are closed, the older tail is left untouched.
      * ``None``     – partial/unknown coverage (capped paging, or the lossy HTML
        fallback); a crawl never infers removals from it."""

    positions: list[ScrapedPosition]
    live_external_ids: set[str] | None = None
    coverage: str | datetime | None = None


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
def _send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, httpx.Response]:
    """One SSRF-guarded request on ``client`` (retried on transient errors by the
    decorator). Validates the host on every redirect hop and caps body size. Returns
    ``(body, response)``: the body is drained, but the response's status/headers stay
    readable — Apple reads a CSRF token off a GET before POSTing its search. Pass a
    shared ``client`` to persist cookies across calls (Apple's CSRF cookie must ride
    along on the search POST)."""
    # Cap on any single response we'll buffer, to bound memory from a hostile or
    # misconfigured endpoint (we stream and abort past this).
    max_bytes = settings.scrape_max_response_mb * 1024 * 1024
    for _ in range(_MAX_REDIRECTS + 1):
        _validate_url(url)
        with client.stream(method, url, json=json_body, headers=headers) as resp:
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
            return b"".join(chunks), resp
    raise ScrapeError(f"too many redirects from {url}")


def _fetch_bytes(url: str) -> bytes:
    """SSRF-guarded GET with transient-error retries; wraps leftover transport
    errors as ``ScrapeError`` so callers only deal with one exception type."""
    try:
        with _client() as c:
            return _send(c, "GET", url)[0]
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


def _google_paged(careers_url: str | None, max_pages: int) -> tuple[list[ScrapedPosition], bool]:
    """Page Google careers results. Returns (positions, complete), where ``complete``
    is True only when we hit the true end of the board (an empty page) within the cap
    — i.e. we saw the whole listing, so absence reliably means removed. Capping out at
    ``max_pages`` (or a no-progress repeated page) leaves ``complete`` False: we can't
    tell an absent role from one on a page we never fetched."""
    base = careers_url or _GOOGLE_RESULTS_URL
    sep = "&" if urlparse(base).query else "?"
    out: list[ScrapedPosition] = []
    seen: set[str] = set()
    complete = False
    for page in range(1, max_pages + 1):
        records = _google_records(_fetch_text(f"{base}{sep}page={page}"))
        if not records:
            complete = True  # reached the end of the board (no more results)
            break
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
            break  # a full page of already-seen ids: ambiguous, don't claim completeness
    return out, complete


def scrape_google(careers_url: str | None, max_pages: int) -> list[ScrapedPosition]:
    return _google_paged(careers_url, max_pages)[0]


# ── Eightfold careers ─────────────────────────────────────────────────────────
# Eightfold-hosted boards (NVIDIA at jobs.nvidia.com, Netflix at
# explore.jobs.netflix.net) render their listing client-side but expose an
# unauthenticated JSON search API. Two API flavours exist — PCSX
# (``/api/pcsx/search``, e.g. NVIDIA) and SmartApply (``/api/apply/v2/jobs``, e.g.
# Netflix); a given tenant serves one and 403s the other, so we try PCSX first and
# fall back to SmartApply. Either way the board is paged 10 at a time, newest-first,
# so rather than pull a whole multi-thousand-role board we walk pages until we cross
# the age cutoff (or hit ``max_pages``). The ``domain`` query param is the org's
# registrable domain (``nvidia.com``/``netflix.com``), NOT the careers host — pass it
# via ``ats_token``; if omitted we derive it by stripping a leading careers subdomain.
_EIGHTFOLD_PAGE = 10
_CAREERS_SUBDOMAINS = {"jobs", "careers", "career", "job", "apply", "talent", "recruiting"}
# Pull JSON-LD <script> blocks out of a job detail page. Some boards HTML-entity-encode
# the ``+`` in the type attribute (Phenom emits ``application/ld&#x2B;json``), so accept
# the literal ``+`` and its hex/decimal entity forms.
_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld(?:\+|&#x2[Bb];|&#43;)json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def _eightfold_description(url: str) -> str | None:
    """Fetch one eightfold job detail page and extract its description from the
    schema.org JobPosting JSON-LD block (the PCSX search API returns none). Best
    effort: any fetch/parse failure yields None so one bad page never breaks the
    batch. The description is HTML, so it's stripped to plain text like the others."""
    try:
        html = _fetch_text(url)
    except ScrapeError:
        return None
    for block in _LD_JSON_RE.findall(html):
        try:
            data = _json.loads(block)
        except _json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("description"):
                stripped = _strip_html(obj["description"])
                if stripped:
                    return stripped
    return None


def fetch_eightfold_descriptions(
    urls: list[str], cap: int, max_workers: int | None = None
) -> dict[str, str]:
    """Concurrently fetch JSON-LD descriptions for up to ``cap`` eightfold job pages
    (in the order given — callers pass newest-first). Returns ``{url: description}``
    for the pages that resolved one. Concurrency is bounded (``max_workers``,
    default ``scrape_eightfold_desc_workers``) so a crawl fetches in small batches
    rather than firing hundreds of requests at once."""
    from concurrent.futures import ThreadPoolExecutor

    workers = max_workers or settings.scrape_eightfold_desc_workers
    targets = [u for u in dict.fromkeys(urls) if u][: max(0, cap)]
    if not targets or workers < 1:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, desc in zip(targets, pool.map(_eightfold_description, targets)):
            if desc:
                out[url] = desc
    return out


def _eightfold_domain(careers_url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    host = (urlparse(careers_url).hostname or "").lower()
    labels = host.split(".")
    if len(labels) >= 3 and labels[0] in _CAREERS_SUBDOMAINS:
        return ".".join(labels[1:])  # jobs.nvidia.com -> nvidia.com
    return host


def _eightfold_position(rec: dict, origin: str) -> ScrapedPosition:
    locs = rec.get("locations") or rec.get("standardizedLocations") or []
    # PCSX uses postedTs/creationTs; SmartApply uses t_update/t_create. Both are
    # epoch *seconds* (unlike Lever/Ashby millis), so we convert here, not via _parse_ts.
    posted = (
        rec.get("postedTs") or rec.get("creationTs")
        or rec.get("t_update") or rec.get("t_create")
    )
    posted_at = None
    if isinstance(posted, (int, float)):
        try:
            posted_at = to_naive_utc(datetime.fromtimestamp(posted, tz=timezone.utc))
        except (ValueError, OSError):
            posted_at = None
    # PCSX gives a relative positionUrl; SmartApply an absolute canonicalPositionUrl
    # (urljoin leaves an absolute URL untouched).
    rel = rec.get("positionUrl") or rec.get("canonicalPositionUrl") or ""
    return ScrapedPosition(
        external_id=str(rec.get("id")),
        title=(rec.get("name") or "Untitled")[:300],
        location="; ".join(loc for loc in locs if loc) or None,
        department=rec.get("department"),
        # onsite/remote/hybrid (PCSX workLocationOption / SmartApply work_location_option)
        employment_type=rec.get("workLocationOption") or rec.get("work_location_option"),
        url=_http_url(urljoin(origin, rel)) if rel else None,
        posted_at=posted_at,
    )


class _EightfoldFlavourUnavailable(Exception):
    """Internal: an Eightfold tenant 403'd this API flavour (it serves the other),
    so the adapter should try the alternative (PCSX <-> SmartApply)."""


def _eightfold_endpoint(origin: str, dom: str, flavour: str, start: int) -> str:
    path = "api/apply/v2/jobs" if flavour == "smartapply" else "api/pcsx/search"
    return (
        f"{origin}/{path}?domain={dom}"
        f"&query=&location=&start={start}&num={_EIGHTFOLD_PAGE}"
    )


def _eightfold_extract(data: dict, flavour: str) -> list:
    if flavour == "smartapply":
        return (data or {}).get("positions") or []  # SmartApply: top-level positions
    return ((data or {}).get("data") or {}).get("positions") or []  # PCSX: data.positions


def _eightfold_json(url: str) -> dict:
    """GET an Eightfold JSON endpoint, distinguishing a 403 (this tenant serves the
    *other* API flavour) from real failures so the adapter can switch flavours."""
    try:
        with _client() as c:
            body, _ = _send(c, "GET", url)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            raise _EightfoldFlavourUnavailable() from exc
        raise ScrapeError(f"fetch {url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ScrapeError(f"fetch {url}: {exc}") from exc
    try:
        return _json.loads(body)
    except _json.JSONDecodeError as exc:
        raise ScrapeError(f"invalid JSON from {url}: {exc}") from exc


def _eightfold_paged(
    careers_url: str, domain: str | None, max_pages: int
) -> tuple[list[ScrapedPosition], str | datetime | None]:
    """Walk the Eightfold board newest-first. Returns (positions, coverage):
      * ``"full"`` – we reached an empty page, so we saw the entire board.
      * the ``cutoff`` datetime – we stopped because a whole page predated the age
        cutoff, so the board is fully covered for postings on/after that cutoff.
      * ``None`` – we hit ``max_pages`` (or a no-progress page) first, so coverage is
        partial and a crawl can't conclude any role is removed.

    The first request picks the flavour: PCSX is tried first and, if the tenant 403s
    it, SmartApply is used instead (see ``_EightfoldFlavourUnavailable``)."""
    parsed = urlparse(careers_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    dom = _eightfold_domain(careers_url, domain)
    cutoff = (
        utcnow() - timedelta(days=settings.scrape_max_age_days)
        if settings.scrape_max_age_days > 0
        else None
    )
    out: list[ScrapedPosition] = []
    seen: set[str] = set()
    coverage: str | datetime | None = None
    flavour: str | None = None
    for page in range(max_pages):
        start = page * _EIGHTFOLD_PAGE
        if flavour is None:
            try:
                data = _eightfold_json(_eightfold_endpoint(origin, dom, "pcsx", start))
                flavour = "pcsx"
            except _EightfoldFlavourUnavailable:
                flavour = "smartapply"
                data = _eightfold_json(_eightfold_endpoint(origin, dom, flavour, start))
        else:
            data = _eightfold_json(_eightfold_endpoint(origin, dom, flavour, start))
        positions = _eightfold_extract(data, flavour)
        if not positions:
            coverage = "full"  # reached the end of the board: complete listing seen
            break
        added = 0
        any_within_cutoff = False
        for rec in positions:
            if not isinstance(rec, dict):
                continue
            pos = _eightfold_position(rec, origin)
            if not pos.external_id or pos.external_id in seen:
                continue
            seen.add(pos.external_id)
            out.append(pos)
            added += 1
            if cutoff is None or pos.posted_at is None or pos.posted_at >= cutoff:
                any_within_cutoff = True
        if added == 0:
            break  # a full page of already-seen ids: ambiguous, leave coverage partial
        # Results are newest-first; once a whole page predates the cutoff, every later
        # page does too, so stop. We've now seen every posting on/after the cutoff —
        # the board is fully covered down to it (the final _within_max_age trim is
        # authoritative; this just bounds how many pages we fetch).
        if cutoff is not None and not any_within_cutoff:
            coverage = cutoff
            break
    return out, coverage


def scrape_eightfold(
    careers_url: str, domain: str | None, max_pages: int
) -> list[ScrapedPosition]:
    return _eightfold_paged(careers_url, domain, max_pages)[0]


# ── Newest-first paging (capped custom boards) ────────────────────────────────
def _paged_newest_first(
    fetch, max_pages: int
) -> tuple[list[ScrapedPosition], str | datetime | None]:
    """Walk a newest-first paginated source. ``fetch(page)`` (0-based) returns that
    page's ``ScrapedPosition``s, or ``[]`` at the end. Returns (positions, coverage):
      * the ``cutoff`` datetime – a whole page predated the age cutoff, so the source
        is fully covered for postings on/after it (absent newer ids are removed).
      * ``None`` – ran out of data, or hit ``max_pages``.

    Unlike the ATS adapters this never reports ``"full"``: these boards cap how deep
    you can page (amazon.jobs ~10k, Apple paginates a slice), so an empty page is an
    ambiguous window edge, not a proven board end — a crawl must not infer removals
    from it."""
    cutoff = (
        utcnow() - timedelta(days=settings.scrape_max_age_days)
        if settings.scrape_max_age_days > 0
        else None
    )
    out: list[ScrapedPosition] = []
    seen: set[str] = set()
    coverage: str | datetime | None = None
    for page in range(max_pages):
        batch = fetch(page)
        if not batch:
            break
        added = 0
        any_within_cutoff = False
        for pos in batch:
            if not pos.external_id or pos.external_id in seen:
                continue
            seen.add(pos.external_id)
            out.append(pos)
            added += 1
            if cutoff is None or pos.posted_at is None or pos.posted_at >= cutoff:
                any_within_cutoff = True
        if added == 0:
            break  # a full page of already-seen ids: ambiguous, leave coverage partial
        if cutoff is not None and not any_within_cutoff:
            coverage = cutoff
            break
    return out, coverage


# ── Amazon (amazon.jobs, no third-party ATS) ──────────────────────────────────
# amazon.jobs is a custom board, but exposes an unauthenticated JSON search at
# /en/search.json: GET, paged by ``offset``, newest-first (``sort=recent``), with the
# description inline (no detail fetch needed). The API caps results at ~10k, so we
# page newest-first and stop at the age cutoff like the other paged sources.
_AMAZON_SEARCH = "https://www.amazon.jobs/en/search.json"
_AMAZON_PAGE = 100  # amazon.jobs honours result_limit up to 100


def _parse_amazon_date(value) -> datetime | None:
    """amazon.jobs renders posted_date as e.g. 'June 18, 2026' (day granularity)."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value).strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return to_naive_utc(parsed)


def _amazon_position(job: dict) -> ScrapedPosition:
    # id_icims is the stable requisition id; the UUID `id` can churn between scrapes.
    ext = str(job.get("id_icims") or job.get("id") or "")
    path = job.get("job_path") or ""
    desc = " ".join(
        part for part in (
            job.get("description"),
            job.get("basic_qualifications"),
            job.get("preferred_qualifications"),
        ) if part
    )
    return ScrapedPosition(
        external_id=ext,
        title=(job.get("title") or "Untitled").strip()[:300],
        location=job.get("normalized_location") or job.get("location"),
        department=job.get("job_category") or job.get("business_category"),
        employment_type=job.get("job_schedule_type"),
        url=_http_url(urljoin(_AMAZON_SEARCH, path)) if path else None,
        description=_strip_html(desc) or None,
        posted_at=_parse_amazon_date(job.get("posted_date")),
    )


def _amazon_paged(max_pages: int) -> tuple[list[ScrapedPosition], str | datetime | None]:
    def fetch(page: int) -> list[ScrapedPosition]:
        url = (
            f"{_AMAZON_SEARCH}?result_limit={_AMAZON_PAGE}"
            f"&offset={page * _AMAZON_PAGE}&sort=recent"
        )
        data = _fetch_json(url)
        jobs = (data.get("jobs") if isinstance(data, dict) else None) or []
        return [_amazon_position(j) for j in jobs if isinstance(j, dict)]

    return _paged_newest_first(fetch, max_pages)


def scrape_amazon(max_pages: int) -> list[ScrapedPosition]:
    return _amazon_paged(max_pages)[0]


# ── Apple (jobs.apple.com, no third-party ATS) ────────────────────────────────
# Apple's careers board is a JS SPA backed by a JSON search API that needs a CSRF
# token: GET /api/v1/csrfToken returns it in the ``x-apple-csrf-token`` header (and
# sets a matching cookie), then POST /api/v1/search returns 20 roles/page with the
# summary inline. The token's cookie must ride along on the POST, so both requests
# share one client. Paged newest-first like the other capped custom boards.
_APPLE_ORIGIN = "https://jobs.apple.com"
_APPLE_CSRF_URL = f"{_APPLE_ORIGIN}/api/v1/csrfToken"
_APPLE_SEARCH_URL = f"{_APPLE_ORIGIN}/api/v1/search"


def _apple_csrf_token(client: httpx.Client) -> str:
    try:
        _, resp = _send(client, "GET", _APPLE_CSRF_URL)
    except httpx.HTTPError as exc:
        raise ScrapeError(f"apple: CSRF fetch failed: {exc}") from exc
    token = resp.headers.get("x-apple-csrf-token")
    if not token:
        raise ScrapeError("apple: search API returned no CSRF token")
    return token


def _apple_search_page(client: httpx.Client, token: str, page: int) -> dict:
    body = {
        "query": "", "filters": {}, "page": page + 1, "locale": "en-us", "sort": "newest",
        # The API returns zero results unless a date-format block is present.
        "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
    }
    headers = {"X-Apple-CSRF-Token": token, "Accept": "application/json"}
    try:
        data, _ = _send(client, "POST", _APPLE_SEARCH_URL, json_body=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ScrapeError(f"apple: search request failed: {exc}") from exc
    try:
        parsed = _json.loads(data)
    except _json.JSONDecodeError as exc:
        raise ScrapeError(f"apple: invalid JSON from search: {exc}") from exc
    return (parsed or {}).get("res") or {}


def _apple_position(rec: dict) -> ScrapedPosition:
    pid = str(rec.get("positionId") or rec.get("id") or "")
    slug = rec.get("transformedPostingTitle") or ""
    locs = rec.get("locations") or []
    loc = "; ".join(
        l.get("name") or l.get("city")
        for l in locs
        if isinstance(l, dict) and (l.get("name") or l.get("city"))
    ) or None
    team = rec.get("team")
    return ScrapedPosition(
        external_id=pid,
        title=(rec.get("postingTitle") or "Untitled")[:300],
        location=loc,
        department=team.get("teamName") if isinstance(team, dict) else None,
        url=_http_url(f"{_APPLE_ORIGIN}/en-us/details/{pid}/{slug}") if pid else None,
        description=_strip_html(rec.get("jobSummary")),
        posted_at=_parse_ts(rec.get("postDateInGMT")),
    )


def _apple_paged(max_pages: int) -> tuple[list[ScrapedPosition], str | datetime | None]:
    with _client() as client:
        token = _apple_csrf_token(client)

        def fetch(page: int) -> list[ScrapedPosition]:
            res = _apple_search_page(client, token, page)
            return [
                _apple_position(r)
                for r in (res.get("searchResults") or [])
                if isinstance(r, dict)
            ]

        return _paged_newest_first(fetch, max_pages)


def scrape_apple(max_pages: int) -> list[ScrapedPosition]:
    return _apple_paged(max_pages)[0]


# ── XML sitemap of job pages (TLS-fingerprint-walled boards, e.g. Citadel) ────
# Some careers sites server-render nothing useful over plain HTTP and front their
# pages with Cloudflare, which blocks plain httpx by TLS/JA3 fingerprint (Citadel
# returns a "Just a moment…" challenge). But they still publish a standard <urlset>
# sitemap that lists every open-role detail page, and the detail pages carry a
# schema.org JobPosting JSON-LD block with the full description. So this adapter:
#   1. reads the sitemap (``ats_token`` = its URL) for the live set of role URLs, then
#   2. fetches each detail page with a *browser TLS profile* (curl_cffi, see
#      ``_fetch_impersonated``) to get past the fingerprint wall, and
#   3. pulls the real title/description/location/date out of its JSON-LD.
# The sitemap is the authoritative listing of which roles are open (coverage "full");
# a detail fetch that fails just leaves that one posting description-less (then the
# matcher skips it) rather than failing the whole crawl.
# (Single <urlset> only — a <sitemapindex> of sub-sitemaps isn't followed; no current
# preset needs it.)
_SITEMAP_URL_BLOCK = re.compile(r"<url\b[^>]*>(.*?)</url>", re.I | re.S)
_SITEMAP_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)
_SITEMAP_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.I | re.S)


def _slug_to_title(url: str) -> str:
    """Best-effort human title from a job URL's trailing slug (the fallback when a
    detail page yields no JSON-LD), e.g.
    ``…/details/global-quantitative-strategies-quantitative-researcher/`` →
    ``Global Quantitative Strategies Quantitative Researcher``."""
    slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1]
    title = slug.replace("-", " ").replace("_", " ").strip()
    return title.title() if title else "Untitled"


class _ImpersonatedTransient(Exception):
    """A retryable failure from an impersonated GET: a transport error, or a status
    Cloudflare sometimes returns under a burst (403/429/5xx) that clears on retry."""


@retry(
    retry=retry_if_exception(lambda e: isinstance(e, _ImpersonatedTransient)),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8),
    reraise=True,
)
def _impersonated_get(url: str):
    """One curl_cffi Chrome-impersonated GET (no redirect follow), retried on transient
    failures. impersonate sets the full Chrome header/TLS profile — don't override the
    User-Agent (a mismatch with the TLS fingerprint re-trips Cloudflare)."""
    from curl_cffi import requests as _creq

    try:
        resp = _creq.get(url, impersonate="chrome", timeout=30, allow_redirects=False)
    except Exception as exc:  # curl_cffi transport error
        raise _ImpersonatedTransient(str(exc)) from exc
    # 403/429 here is usually Cloudflare rate-limiting a burst, not a hard block (a
    # single request succeeds); retry it. 5xx is transient too.
    if resp.status_code in (403, 429) or resp.status_code >= 500:
        raise _ImpersonatedTransient(f"HTTP {resp.status_code}")
    return resp


def _fetch_impersonated(url: str) -> str:
    """GET a page that blocks plain httpx via TLS/JA3 fingerprinting, using a real
    browser TLS profile (curl_cffi Chrome impersonation). SSRF-guarded like ``_send``:
    every hop's host must resolve to a public IP, redirects are followed manually and
    re-validated, and the body is size-capped. Raises ``ScrapeError`` on any failure so
    callers handle one exception type. Used for Cloudflare-fingerprint-gated boards
    (e.g. Citadel) whose pages return "Just a moment…" under httpx."""
    max_bytes = settings.scrape_max_response_mb * 1024 * 1024
    for _ in range(_MAX_REDIRECTS + 1):
        _validate_url(url)  # scheme + public-host check on every hop
        try:
            resp = _impersonated_get(url)
        except Exception as exc:  # transient retries exhausted, or any other error
            raise ScrapeError(f"fetch {url}: {exc}") from exc
        if 300 <= resp.status_code < 400:
            loc = resp.headers.get("location")
            if loc:
                url = urljoin(url, loc)
                continue
        if resp.status_code >= 400:
            raise ScrapeError(f"fetch {url}: HTTP {resp.status_code}")
        body = resp.content or b""
        if len(body) > max_bytes:
            raise ScrapeError(f"response from {url} exceeds {max_bytes} bytes")
        return body.decode("utf-8", errors="replace")
    raise ScrapeError(f"too many redirects from {url}")


def _jsonld_jobposting(html: str) -> dict | None:
    """Return the first schema.org JobPosting object from a page's JSON-LD blocks."""
    for block in _LD_JSON_RE.findall(html):
        try:
            data = _json.loads(block)
        except _json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return None


def _jsonld_location(job: dict) -> str | None:
    """Flatten a JobPosting's ``jobLocation`` (one or many) to a readable string.

    Prefer each ``Place``'s own ``name`` ("City, ST") when present: some boards
    (Meta) carry a correct ``name`` but a broken/duplicated ``address`` breakdown
    (every role claims the same city). Otherwise fall back to joining the
    ``PostalAddress`` fields (Citadel). Only string-valued address fields are
    joined, so a nested ``addressCountry`` object (``{"@type": "Country", ...}``,
    again Meta) is skipped rather than stringified into the result.
    """
    raw = job.get("jobLocation")
    items = raw if isinstance(raw, list) else [raw]
    seen: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if isinstance(name, str) and name.strip():
            seg = name.strip()
        else:
            addr = it.get("address")
            if not isinstance(addr, dict):
                continue
            seg = ", ".join(
                addr[k]
                for k in ("addressLocality", "addressRegion", "addressCountry")
                if isinstance(addr.get(k), str) and addr[k]
            )
        if seg and seg not in seen:
            seen.append(seg)
    if not seen:
        return None
    # A role open in many offices (Meta lists dozens) can blow past the location
    # column (varchar(512)). Keep the offices that fit a budget and summarize the rest
    # as "+N more" — both readable and safely within the column, instead of a hard
    # mid-string truncation (or a batch-aborting overflow).
    kept: list[str] = []
    used = 0
    for seg in seen:
        extra = len(seg) + (2 if kept else 0)  # "; " separator
        if kept and used + extra > 460:  # leave headroom for the " +N more" tail
            break
        kept.append(seg)
        used += extra
    more = len(seen) - len(kept)
    return "; ".join(kept) + (f" +{more} more" if more else "")


def _sitemap_position(url: str, lastmod: str | None) -> ScrapedPosition:
    """Fetch one detail page and build a position from its JSON-LD JobPosting. Best
    effort: a failed fetch / missing JSON-LD falls back to the URL slug for the title
    and leaves the description empty (so the matcher skips it) rather than aborting."""
    title = _slug_to_title(url)[:300]
    description = location = employment = None
    posted_at = _parse_ts(lastmod)
    try:
        job = _jsonld_jobposting(_fetch_impersonated(url))
    except ScrapeError:
        job = None
    if job:
        title = (job.get("title") or title)[:300]
        description = _strip_html(job.get("description"))
        location = _jsonld_location(job)
        employment = job.get("employmentType")
        posted_at = _parse_ts(job.get("datePosted")) or posted_at
    return ScrapedPosition(
        external_id=_hash(url),
        title=title,
        location=location,
        employment_type=employment,
        url=url,
        description=description,
        posted_at=posted_at,
    )


def scrape_sitemap(sitemap_url: str, url_filter: str | None = None) -> list[ScrapedPosition]:
    from concurrent.futures import ThreadPoolExecutor

    # When the sitemap mixes job-detail pages with marketing/blog URLs, ``url_filter``
    # (a regex) keeps only the job-detail entries so we don't fetch — and surface as
    # description-less "jobs" — the non-job pages. None keeps every entry.
    keep = re.compile(url_filter) if url_filter else None
    xml = _fetch_impersonated(sitemap_url)
    entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for block in _SITEMAP_URL_BLOCK.findall(xml):
        m = _SITEMAP_LOC.search(block)
        if not m:
            continue
        url = _http_url(m.group(1).strip())
        if not url or url in seen:
            continue
        if keep and not keep.search(url):
            continue
        seen.add(url)
        lm = _SITEMAP_LASTMOD.search(block)
        entries.append((url, lm.group(1).strip() if lm else None))
    if not entries:
        return []
    # Fetch detail pages concurrently (bounded) — reuse the eightfold detail-fetch
    # worker cap so a big board doesn't fire hundreds of requests at once.
    workers = max(1, settings.scrape_eightfold_desc_workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda e: _sitemap_position(*e), entries))


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
def _within_max_age(
    positions: list[ScrapedPosition], max_age_days: int
) -> list[ScrapedPosition]:
    """Keep only postings dated within ``max_age_days`` (by ``posted_at``), to
    bound how much we store and score downstream.

    Postings with no ``posted_at`` are KEPT: Google careers and the generic HTML
    fallback don't expose a per-posting date, so dropping the undated ones would
    silently lose every posting from those sources. ``max_age_days`` <= 0 disables
    the filter."""
    if max_age_days <= 0:
        return positions
    cutoff = utcnow() - timedelta(days=max_age_days)
    return [p for p in positions if p.posted_at is None or p.posted_at >= cutoff]


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
    if "amazon.jobs" in host:
        return "amazon", None
    if "jobs.apple.com" in host:
        return "apple", None
    if "eightfold.ai" in host:
        # A *.eightfold.ai tenant URL; the org domain still must be supplied
        # explicitly via ats_token (it isn't recoverable from the host), so the
        # adapter will derive a best-effort default. Custom-domain Eightfold
        # boards (e.g. jobs.nvidia.com) can't be inferred and need ats_type set.
        return "eightfold", None
    return "html", None


def scrape_company(company) -> ScrapeResult:
    """Resolve the right adapter for a Company ORM object and run it. Returns a
    ``ScrapeResult``: the age-filtered postings to upsert, plus the live id set and
    its ``coverage`` (see ScrapeResult) used to reconcile availability."""
    ats_type = company.ats_type or "auto"
    token = company.ats_token

    if ats_type == "auto":
        ats_type, inferred = _infer_ats(company.careers_url)
        token = token or inferred

    if ats_type in _AUTHORITATIVE_ATS and not token:
        # Inference may still recover a token from the URL.
        _, token = _infer_ats(company.careers_url)
        if not token:
            raise ScrapeError(f"{company.name}: ats_type={ats_type} but no ats_token/url token")

    # ``coverage`` says how much of the board ``results`` represents (drives removal
    # reconciliation): "full" for whole-board sources, a datetime floor for a
    # newest-first walk that reached the age cutoff, None when only a partial slice
    # was seen (capped paging, or the lossy HTML fallback).
    coverage: str | datetime | None = None
    if ats_type == "greenhouse":
        results = scrape_greenhouse(token)
        coverage = "full"
    elif ats_type == "lever":
        results = scrape_lever(token)
        coverage = "full"
    elif ats_type == "ashby":
        results = scrape_ashby(token)
        coverage = "full"
    elif ats_type == "google":
        results, complete = _google_paged(company.careers_url, settings.scrape_google_max_pages)
        coverage = "full" if complete else None
    elif ats_type == "eightfold":
        if not company.careers_url:
            raise ScrapeError(f"{company.name}: eightfold scrape needs careers_url")
        results, coverage = _eightfold_paged(
            company.careers_url, token, settings.scrape_eightfold_max_pages
        )
    elif ats_type == "sitemap":
        if not token:
            raise ScrapeError(f"{company.name}: sitemap scrape needs ats_token (the sitemap URL)")
        results = scrape_sitemap(token, getattr(company, "job_url_filter", None))
        # The sitemap enumerates every open-role page, so it's a complete listing:
        # an id's absence means the role was removed (same as the full-board ATSs).
        coverage = "full"
    elif ats_type == "amazon":
        results, coverage = _amazon_paged(settings.scrape_amazon_max_pages)
    elif ats_type == "apple":
        results, coverage = _apple_paged(settings.scrape_apple_max_pages)
    elif ats_type == "html":
        if not company.careers_url:
            raise ScrapeError(f"{company.name}: html scrape needs careers_url")
        results = scrape_html(company.careers_url, settings.scrape_max_positions_per_company)
        # coverage stays None: the HTML fallback is lossy (JS-rendered pages, a hard
        # cap), so an absent role usually means "we couldn't parse it", not "removed".
    else:
        raise ScrapeError(f"{company.name}: unknown ats_type {ats_type!r}")

    # Capture the live id set before any age trim — availability reconciliation needs
    # the board as scraped, not the recent slice (else an old-but-live role looks
    # removed). Only meaningful when we have coverage to act on.
    live_external_ids = {p.external_id for p in results} if coverage is not None else None

    # No global cap: the ATS adapters return a company's full board (large boards
    # have 200+ postings, and external_id dedup means a dropped one is never seen
    # again). The per-company limit only bounds the noisy HTML fallback, which
    # applies it itself via the ``max_positions`` argument to ``scrape_html``.
    #
    # Drop stale postings last, so every adapter is filtered uniformly: only roles
    # posted/updated within scrape_max_age_days are pulled (undated sources kept).
    positions = _within_max_age(results, settings.scrape_max_age_days)
    return ScrapeResult(
        positions=positions, live_external_ids=live_external_ids, coverage=coverage
    )
