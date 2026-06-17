"""Pre-fill a user's applicant profile from their uploaded résumé using the LLM.

This is the onboarding shortcut (à la Simplify): rather than typing everything,
the user clicks "Import from résumé" and the GOOD model extracts the structured
fields a résumé actually contains — name, contact, links, education, work history —
which the dashboard drops into the profile form for review before saving. Work
authorization, preferences, and EEO are deliberately NOT inferred (résumés don't
state them, and guessing would be wrong), so those stay blank for the user.

Robustness note: many models ignore Ollama's structured ``format`` grammar (see the
matcher / the ollama-structured-output memo) and emit their OWN field names
(``name`` instead of first/last, ``work_experience`` instead of ``experience``,
``dates`` instead of start/end). So we DON'T constrain with ``format``: we ask for a
specific JSON shape in the prompt, parse the object out of the reply, and then
normalize common key/shape aliases before validating. Accepts an injected client
for tests (which call ``chat_text``)."""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Resume, User
from . import llm
from .ollama_client import OllamaClient

log = logging.getLogger(__name__)

_RESUME_CHARS = 8000


class NoResumeError(RuntimeError):
    """Raised when the user has no active résumé to import from (router -> 400)."""


# ── The shape we normalize the model's reply into (strings default to "") ──────
class _ExtractedEducation(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""
    location: str = ""
    description: str = ""


class _ExtractedExperience(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False
    description: str = ""


class ExtractedProfile(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state_region: str = ""
    country: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    education: list[_ExtractedEducation] = Field(default_factory=list)
    experience: list[_ExtractedExperience] = Field(default_factory=list)


EXTRACT_SYSTEM = (
    "You extract a candidate's information from their résumé so it can autofill "
    "job-application forms. Reply with ONLY a single JSON object — no prose, no "
    "markdown, no code fences — using EXACTLY these keys:\n"
    "first_name, last_name, email, phone, city, state_region, country, "
    "linkedin_url, github_url, portfolio_url, education, experience.\n"
    "`education` is an array of objects {school, degree, field_of_study, "
    "start_date, end_date, gpa, location, description}.\n"
    "`experience` is an array of objects {company, title, location, start_date, "
    "end_date, is_current, description}.\n"
    "Rules:\n"
    "- Split the candidate's full name into first_name and last_name.\n"
    "- Split a single location like 'Menlo Park, CA' into city / state_region / country.\n"
    "- For each role, split a date range like '2020 – Present' into start_date and "
    "end_date, and set is_current=true when the role is ongoing (end is "
    "'Present'/'Current').\n"
    "- For EVERY role and education entry, fill `description` with the résumé's own "
    "wording, PRESERVING its format: if the résumé uses bullet points, return them "
    "as an ARRAY of bullet strings (one element per bullet); if the résumé describes "
    "it as a paragraph or a single sentence, return that text as a single STRING. Do "
    "NOT invent bullets from prose, do NOT split a paragraph, and do NOT merge "
    "bullets into a paragraph. Never drop a role's description, including the oldest "
    "roles.\n"
    "- Use ONLY facts present in the résumé; never invent. Leave a field empty only "
    "when the résumé truly says nothing about it. List the most recent items first."
)


def extract_from_resume(db: Session, user: User, *, client: OllamaClient | None = None) -> dict:
    """Extract a draft profile from the user's active résumé and return it as a
    dict (NOT persisted — the caller reviews/edits then saves). Raises
    ``NoResumeError`` when there's no active résumé; LLM errors propagate as
    ``OllamaError`` for the router to surface."""
    resume = db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id, Resume.is_active == True)  # noqa: E712
        .order_by(Resume.created_at.desc())
    )
    if resume is None:
        raise NoResumeError("Upload a résumé before importing a profile from it.")

    if client is None:
        client, _ = llm.clients_for_user(db, user)
    prompt = "## RÉSUMÉ\n" + resume.content_text[:_RESUME_CHARS] + "\n"
    # Free-form (not structured `format`): we steer the shape in the prompt and
    # parse/normalize ourselves, so models that ignore the grammar still work.
    reply = client.chat_text(EXTRACT_SYSTEM, prompt, temperature=0.1)
    return _normalize(_json_object(reply)).model_dump()


# ── Parsing + alias normalization ──────────────────────────────────────────────
def _json_object(text: str | None) -> dict:
    """Pull the first JSON object out of a model reply (tolerating prose/code
    fences). Returns {} when none parses."""
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.S)  # first '{' to last '}'
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _lc(d: object) -> dict:
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def _first(d: dict, *keys: str) -> str:
    """First non-empty value among ``keys`` (already lower-cased dict), as text."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return ""


# Real bullet glyphs we can safely split an inline list on (NOT "*"/"·", which
# appear in normal text). Leading "-"/"*"/glyph markers are stripped per line.
_INLINE_BULLETS = "•◦▪‣●○"
_LEADING_MARKER = re.compile(r"^\s*[-*•◦▪‣·∙・●○]\s+")
_ORDERED = re.compile(r"^\(?\d{1,2}[.)]\s+")  # "1. " / "2) " / "(3) " ordered-list item


def _strip_marker(s: str) -> str:
    return _LEADING_MARKER.sub("", s).strip()


def _format_description(*values: object) -> str:
    """Normalize a role/education description into clean lines: every bullet on its
    own line starting with "- ", and every ordered-list item on its own line
    (numbering preserved). Accepts a list of bullets or a string (with newline /
    glyph / inline-ordered separators). Plain prose with no list structure is left
    as a single block (no spurious leading dash)."""
    val: object = None
    for v in values:
        if (isinstance(v, list) and v) or (isinstance(v, str) and v.strip()):
            val = v
            break
    if val is None:
        return ""

    if isinstance(val, list):
        items = [str(x) for x in val]
    else:
        s = val.strip()
        structured = (
            "\n" in s
            or _ORDERED.search(s)
            or any(c in s for c in _INLINE_BULLETS)
            or _LEADING_MARKER.match(s)
        )
        if not structured:
            return s  # a single prose sentence/paragraph — leave it alone
        s = re.sub(rf"\s*[{re.escape(_INLINE_BULLETS)}]\s*", "\n", s)  # inline glyph -> line
        s = re.sub(r"(?<=\S)\s+(?=\d{1,2}[.)]\s)", "\n", s)  # inline "N." -> new line
        items = s.split("\n")

    lines: list[str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        lines.append(item if _ORDERED.match(item) else "- " + _strip_marker(item))
    return "\n".join(lines)


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _split_location(loc: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in (loc or "").split(",") if p.strip()]
    return (
        parts[0] if parts else "",
        parts[1] if len(parts) > 1 else "",
        parts[2] if len(parts) > 2 else "",
    )


_PRESENT = ("present", "current", "now", "ongoing", "to date", "till date", "present.")
# Range separators: dash variants, or the words to/through/until as whole words.
_DASH = re.compile(r"\s*(?:[–—−-]|\bto\b|\bthrough\b|\buntil\b)\s*", re.I)


def _is_present(s: str) -> bool:
    low = (s or "").lower()
    return any(p in low for p in _PRESENT)


def _split_dates(s: str) -> tuple[str, str, bool]:
    s = (s or "").strip()
    if not s:
        return "", "", False
    parts = _DASH.split(s, maxsplit=1)
    if len(parts) == 2:
        start, end = parts[0].strip(), parts[1].strip()
    else:
        start, end = s, ""
    return start, end, _is_present(end) or (not end and _is_present(start))


def _truthy(v: object) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1", "y")


def _norm_experience(raw: object) -> dict:
    d = _lc(raw)
    start = _first(d, "start_date", "start", "from", "start_year")
    end = _first(d, "end_date", "end", "to", "end_year")
    is_current = _truthy(d.get("is_current") or d.get("current"))
    if not start and not end:
        start, end, cur = _split_dates(_first(d, "dates", "date", "duration", "period"))
        is_current = is_current or cur
    else:
        is_current = is_current or _is_present(end)
    return {
        "company": _first(d, "company", "employer", "organization", "organisation", "name"),
        "title": _first(d, "title", "role", "position", "job_title"),
        "location": _first(d, "location", "city", "place"),
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "description": _format_description(
            d.get("description"), d.get("summary"), d.get("details"),
            d.get("responsibilities"), d.get("highlights"), d.get("bullets"),
        ),
    }


def _norm_education(raw: object) -> dict:
    d = _lc(raw)
    start = _first(d, "start_date", "start", "from", "start_year")
    end = _first(d, "end_date", "end", "to", "graduation", "graduation_date", "grad_date", "year", "end_year")
    if not start and not end:
        start, end, _ = _split_dates(_first(d, "dates", "date", "duration", "period"))
    return {
        "school": _first(d, "school", "institution", "university", "college", "name"),
        "degree": _first(d, "degree", "qualification"),
        "field_of_study": _first(d, "field_of_study", "field", "major", "study", "specialization", "specialisation"),
        "start_date": start,
        "end_date": end,
        "gpa": _first(d, "gpa", "grade"),
        "location": _first(d, "location", "city", "place"),
        "description": _format_description(d.get("description"), d.get("details"), d.get("notes")),
    }


def _as_list(*values: object) -> list:
    for v in values:
        if isinstance(v, list):
            return v
    return []


def _normalize(raw: dict) -> ExtractedProfile:
    """Map the model's reply (whatever keys it chose) onto ExtractedProfile,
    accepting the common aliases models emit instead of our exact field names."""
    d = _lc(raw)
    first = _first(d, "first_name", "firstname", "given_name")
    last = _first(d, "last_name", "lastname", "surname", "family_name")
    if not first and not last:
        first, last = _split_name(_first(d, "name", "full_name", "fullname", "candidate_name"))
    city = _first(d, "city")
    state = _first(d, "state_region", "state", "region", "province")
    country = _first(d, "country")
    if not (city or state or country):
        city, state, country = _split_location(_first(d, "location", "address"))

    exp_raw = _as_list(
        d.get("experience"), d.get("work_experience"), d.get("experiences"),
        d.get("work_history"), d.get("employment"), d.get("jobs"),
    )
    edu_raw = _as_list(
        d.get("education"), d.get("schools"), d.get("education_history"), d.get("academics"),
    )
    payload = {
        "first_name": first,
        "last_name": last,
        "email": _first(d, "email", "email_address", "e_mail"),
        "phone": _first(d, "phone", "phone_number", "mobile", "tel", "telephone"),
        "city": city,
        "state_region": state,
        "country": country,
        "linkedin_url": _first(d, "linkedin_url", "linkedin", "linkedin_profile"),
        "github_url": _first(d, "github_url", "github"),
        "portfolio_url": _first(d, "portfolio_url", "portfolio", "website", "personal_website", "url"),
        "education": [_norm_education(e) for e in edu_raw if isinstance(e, dict)],
        "experience": [_norm_experience(x) for x in exp_raw if isinstance(x, dict)],
    }
    try:
        return ExtractedProfile.model_validate(payload)
    except ValidationError:
        log.warning("résumé profile normalization failed validation")
        return ExtractedProfile()
