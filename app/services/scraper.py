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

# Cap on any single response we'll buffer, to bound memory from a hostile or
# misconfigured endpoint (we stream and abort past this).
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
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
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
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
                    if total > _MAX_RESPONSE_BYTES:
                        raise ScrapeError(
                            f"response from {url} exceeds {_MAX_RESPONSE_BYTES} bytes"
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
                url=job.get("absolute_url"),
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
                url=job.get("hostedUrl"),
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
                url=job.get("jobUrl"),
                description=_strip_html(job.get("descriptionHtml")),
                posted_at=_parse_ts(job.get("publishedAt")),
            )
        )
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
        return "greenhouse", parts[-1]
    if "lever.co" in host and parts:
        return "lever", parts[0]
    if "ashbyhq.com" in host and parts:
        return "ashby", parts[0]
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
    elif ats_type == "html":
        if not company.careers_url:
            raise ScrapeError(f"{company.name}: html scrape needs careers_url")
        results = scrape_html(company.careers_url, settings.scrape_max_positions_per_company)
    else:
        raise ScrapeError(f"{company.name}: unknown ats_type {ats_type!r}")

    return results[: settings.scrape_max_positions_per_company]
