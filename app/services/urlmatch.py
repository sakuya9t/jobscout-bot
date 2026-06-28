"""Normalize a job-posting URL into a comparison key.

Used by the "find this posting in my job list" lookup: a URL a user pastes rarely
matches byte-for-byte what we scraped (http vs https, ``www.``, a trailing slash, a
``#fragment``, ``?utm_*`` tracking params), so we compare normalized keys instead of
raw strings. We deliberately do *not* try to bridge fundamentally different URL forms
for the same job (a company-site ``?gh_jid=`` embed vs the canonical
``boards.greenhouse.io/.../jobs/{id}`` we store) — that needs ATS-specific id
extraction and is out of scope here."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

# Query params that never identify a posting — dropped before comparing. (Compared
# case-insensitively against the param name.)
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "source", "src", "ref", "referrer", "mc_cid", "mc_eid",
})


def normalize_posting_url(url: str | None) -> str | None:
    """Return a stable comparison key for ``url``, or ``None`` for empty / non-http(s)
    input. Two URLs that point at the same posting (differing only in scheme, ``www.``,
    trailing slash, fragment, or tracking params) yield the same key."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    # Tolerate a pasted bare URL ("boards.greenhouse.io/foo/jobs/1"): no scheme, so the
    # whole thing parses as a path. Retry with an https:// prefix when it looks host-ish.
    if not parts.scheme and not parts.netloc and "." in raw.split("/", 1)[0]:
        parts = urlsplit(f"https://{raw}")
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = urlencode(sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ))
    key = f"{host}{path}"
    if query:
        key += f"?{query}"
    return key
