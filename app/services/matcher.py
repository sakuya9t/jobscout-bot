"""The daily pipeline: scrape companies -> upsert positions -> cheap pre-filter
-> LLM filter+score against the user's resume -> persist MatchResults.

Designed to run headless (scheduler / CLI / MCP) using ``session_scope`` as well
as inside a request with an injected session."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Company, Interest, MatchResult, Position, Resume, User
from ..schemas import MatchVerdict
from ..timeutil import utcnow
from . import scraper
from .ollama_client import OllamaClient, OllamaError, get_client

log = logging.getLogger(__name__)

# Structured-output schema the model must fill (Ollama `format`). Derived from
# the Pydantic model so the schema and the parser never drift apart.
MATCH_SCHEMA = MatchVerdict.model_json_schema()

SYSTEM_PROMPT = (
    "You are a meticulous technical recruiter. Given a candidate's resume, their "
    "role requirements, and a job posting, decide whether the posting genuinely "
    "matches the requirements, then score the fit. Be strict and realistic — do "
    "not inflate scores.\n"
    "Rubric for match_score (0-100): must-have skills overlap, seniority fit, "
    "domain/industry overlap, location & work-eligibility fit, and the candidate's "
    "stated requirement notes.\n"
    "win_probability (0-100) is the candidate's realistic chance of receiving an "
    "offer given typical competition, factoring seniority gaps and missing skills.\n"
    "Set matches_requirements=false if the role violates a hard requirement "
    "(wrong location, excluded keyword, wrong seniority). Keep reasoning to 2-4 "
    "sentences addressed to the candidate."
)

_RESUME_CHARS = 6000
_DESC_CHARS = 4000

# Marker stored on the `model` column of a MatchResult when scoring terminally
# failed, so the pair is skipped on re-runs rather than re-billed. Cleared by
# `clear_failed_markers` (CLI: run-daily --retry-failed).
ERROR_MODEL = "error"


_MAX_REPORTED_ERRORS = 5


@dataclass
class RunResult:
    new_positions: int = 0
    scored: int = 0
    errors: list[str] = field(default_factory=list)
    match_ids: list[int] = field(default_factory=list)
    # Internal: total error count + de-dup set so one outage doesn't emit 200
    # identical lines. ``errors`` holds at most _MAX_REPORTED_ERRORS unique
    # messages; ``error_count`` is the true total for the "+N more" tail.
    error_count: int = 0
    _seen_errors: set[str] = field(default_factory=set, repr=False)

    def add_error(self, message: str) -> None:
        self.error_count += 1
        if message in self._seen_errors:
            return
        self._seen_errors.add(message)
        if len(self.errors) < _MAX_REPORTED_ERRORS:
            self.errors.append(message)

    def finalize_errors(self) -> None:
        """Append a '+N more' tail if errors were truncated. Idempotent-ish:
        call once at the end of a run."""
        hidden = self.error_count - len(self.errors)
        if hidden > 0:
            self.errors.append(f"… and {hidden} more error(s)")


def _csv(value: str | None) -> list[str]:
    return [t.strip().lower() for t in (value or "").split(",") if t.strip()]


def _passes_prefilter(pos: Position, interest: Interest) -> bool:
    """Cheap substring gate to avoid spending LLM calls on obvious non-matches.
    Conservative: only *excludes*, never invents a match. The LLM is authoritative."""
    haystack = f"{pos.title} {pos.location or ''} {pos.department or ''}".lower()

    excludes = _csv(interest.exclude_keywords)
    if any(x in haystack for x in excludes):
        return False

    titles = _csv(interest.title_keywords)
    if titles and not any(t in haystack for t in titles):
        return False

    locations = _csv(interest.locations)
    # Only gate on location when the posting actually has location data. Many
    # (often remote) postings ship an empty location; dropping those here would
    # hide real matches, so defer the call to the LLM instead.
    if locations and (pos.location or "").strip():
        loc = pos.location.lower()
        # "remote" anywhere in the title/location counts as matching a remote pref.
        if not any(l in loc or l in haystack for l in locations):
            return False
    return True


def _build_user_prompt(resume: Resume, interest: Interest, pos: Position) -> str:
    reqs = [f"- {k}: {v}" for k, v in {
        "Desired titles": interest.title_keywords,
        "Locations": interest.locations,
        "Seniority": interest.seniority,
        "Employment type": interest.employment_type,
        "Exclusions": interest.exclude_keywords,
        "Notes": interest.notes,
    }.items() if v]
    return (
        "## CANDIDATE REQUIREMENTS\n" + ("\n".join(reqs) or "(none specified)") + "\n\n"
        "## RESUME\n" + resume.content_text[:_RESUME_CHARS] + "\n\n"
        "## JOB POSTING\n"
        f"Company position id: {pos.external_id}\n"
        f"Title: {pos.title}\n"
        f"Location: {pos.location or 'n/a'}\n"
        f"Department: {pos.department or 'n/a'}\n"
        f"Employment type: {pos.employment_type or 'n/a'}\n"
        f"Description:\n{(pos.description or '(no description scraped)')[:_DESC_CHARS]}\n"
    )


def _upsert_positions(db: Session, company: Company) -> tuple[list[Position], list[str]]:
    """Scrape one company and upsert. Returns (new_positions, errors)."""
    errors: list[str] = []
    try:
        scraped = scraper.scrape_company(company)
    except scraper.ScrapeError as exc:
        return [], [str(exc)]
    except Exception as exc:  # defensive: never let one company kill the run
        return [], [f"{company.name}: unexpected scrape error: {exc}"]

    new_positions: list[Position] = []
    existing = {
        p.external_id: p
        for p in db.scalars(select(Position).where(Position.company_id == company.id))
    }
    for sp in scraped:
        if sp.external_id in existing:
            continue
        pos = Position(
            company_id=company.id,
            external_id=sp.external_id,
            title=sp.title,
            location=sp.location,
            department=sp.department,
            employment_type=sp.employment_type,
            url=sp.url,
            description=sp.description,
            posted_at=sp.posted_at,
        )
        db.add(pos)
        new_positions.append(pos)
    company.last_scraped_at = utcnow()
    db.flush()  # assign ids
    return new_positions, errors


def _score_position(
    client: OllamaClient,
    db: Session,
    user: User,
    resume: Resume,
    interest: Interest,
    pos: Position,
) -> tuple[int | None, str | None]:
    """Call the LLM for one (position, resume) pair and persist a MatchResult.
    Returns ``(match_id, error)``: the id on success, or ``(None, message)`` so
    the caller can surface the failure in the run summary instead of swallowing
    it. The error message is intentionally not position-specific so identical
    outages (e.g. a bad API key) de-dup into a single reported line.

    On terminal failure (the HTTP layer already retried transient errors) a
    marker row is persisted with ``model=ERROR_MODEL`` so this (position,
    interest) pair lands in the ``already`` set and isn't re-billed every run.
    Clear markers with ``jobscout run-daily --retry-failed`` to retry them."""
    try:
        data = client.chat_json(SYSTEM_PROMPT, _build_user_prompt(resume, interest, pos), MATCH_SCHEMA)
        verdict = MatchVerdict.model_validate(data)
    except (OllamaError, ValidationError) as exc:
        log.warning("scoring failed for position %s: %s", pos.id, exc)
        # Keep the verbose error in the marker row for debugging, but report a
        # stable summary so identical failures de-dup in the run summary:
        # transport/auth errors share text; per-position validation noise doesn't.
        summary = str(exc) if isinstance(exc, OllamaError) else "model returned an invalid result"
        marker = MatchResult(
            user_id=user.id, position_id=pos.id, resume_id=resume.id,
            interest_id=interest.id, passed_filter=False, match_score=0,
            win_probability=0, reasoning=f"Scoring failed: {exc}"[:1000],
            model=ERROR_MODEL,
        )
        db.add(marker)
        db.flush()
        return None, f"Scoring failed: {summary}"

    result = MatchResult(
        user_id=user.id,
        position_id=pos.id,
        resume_id=resume.id,
        interest_id=interest.id,
        passed_filter=verdict.matches_requirements,
        match_score=verdict.match_score,
        win_probability=verdict.win_probability,
        reasoning=verdict.reasoning,
        strengths=json.dumps(verdict.strengths),
        gaps=json.dumps(verdict.gaps),
        model=client.model,
    )
    db.add(result)
    db.flush()
    return result.id, None


def clear_failed_markers(db: Session, user_id: int | None = None) -> int:
    """Delete error-marker MatchResults so failed (position, interest) pairs are
    re-scored on the next run. Returns the number cleared."""
    stmt = delete(MatchResult).where(MatchResult.model == ERROR_MODEL)
    if user_id is not None:
        stmt = stmt.where(MatchResult.user_id == user_id)
    return db.execute(stmt).rowcount or 0


def run_for_user(db: Session, user: User, client: OllamaClient | None = None) -> RunResult:
    """Scrape all of a user's companies and score newly-seen / unscored positions
    against their active resume. Idempotent: positions already scored for the
    active resume are skipped, so re-running the same day costs nothing extra."""
    client = client or get_client()
    res = RunResult()

    resume = db.scalar(
        select(Resume).where(Resume.user_id == user.id, Resume.is_active == True)  # noqa: E712
        .order_by(Resume.created_at.desc())
    )
    interests = list(
        db.scalars(select(Interest).where(Interest.user_id == user.id, Interest.is_active == True))  # noqa: E712
    )
    companies = list(
        db.scalars(select(Company).where(Company.user_id == user.id, Company.is_active == True))  # noqa: E712
    )
    if not resume:
        res.add_error("No active resume uploaded — cannot score.")
    if not interests:
        res.add_error("No active interests configured — nothing to match against.")

    # 1) Scrape + upsert. Track which positions are brand-new this run.
    #    Commit per company so we never hold a write lock across the whole run
    #    (SQLite) and so a later failure can't discard already-scraped postings.
    new_ids: set[int] = set()
    for company in companies:
        new_positions, errs = _upsert_positions(db, company)
        for e in errs:
            res.add_error(e)
        res.new_positions += len(new_positions)
        new_ids.update(p.id for p in new_positions)
        db.commit()

    if not resume or not interests:
        res.finalize_errors()
        return res

    # 2) Candidate set = every position of the user's companies that lacks a
    #    MatchResult for the active resume (covers new + any never-scored).
    company_ids = [c.id for c in companies]
    if not company_ids:
        res.finalize_errors()
        return res
    all_positions = list(
        db.scalars(select(Position).where(Position.company_id.in_(company_ids)))
    )
    already = {
        (m.position_id, m.interest_id)
        for m in db.scalars(
            select(MatchResult).where(
                MatchResult.user_id == user.id, MatchResult.resume_id == resume.id
            )
        )
    }

    undescribed = 0
    for pos in all_positions:
        # Postings with no scraped description come from the generic HTML
        # fallback (nav links etc.). Scoring them just bills the LLM to read
        # "(no description scraped)" and yields meaningless numbers, so skip and
        # nudge the user to configure a real ATS instead.
        if not (pos.description or "").strip():
            undescribed += 1
            continue
        # Score the position independently against EVERY interest whose pre-filter
        # it passes, so each interest gets its own ranking. Skipping already-scored
        # (position, interest) pairs keeps re-runs free, while a newly added or
        # edited interest still re-evaluates previously-seen positions.
        wrote_this_pos = False
        for interest in interests:
            if (pos.id, interest.id) in already:
                continue
            if not _passes_prefilter(pos, interest):
                continue
            match_id, error = _score_position(client, db, user, resume, interest, pos)
            if match_id is not None:
                res.scored += 1
                res.match_ids.append(match_id)
            if error is not None:
                res.add_error(error)
            # Either a real result or an error-marker row was written.
            wrote_this_pos = True
        # Commit per position so the write lock is released between LLM calls and
        # partial progress (incl. error markers) survives a crash mid-run.
        if wrote_this_pos:
            db.commit()

    if undescribed:
        res.add_error(
            f"{undescribed} posting(s) had no scraped description and were skipped — "
            "set the company's ATS (greenhouse/lever/ashby) for full job text."
        )
    res.finalize_errors()
    return res


def run_for_all_users(retry_failed: bool = False) -> dict[int, RunResult]:
    """Entry point for the scheduler/CLI. Each user committed independently.
    ``retry_failed`` clears prior error-markers first so failed pairs re-score."""
    summaries: dict[int, RunResult] = {}
    with session_scope() as db:
        user_ids = list(db.scalars(select(User.id)))
    for uid in user_ids:
        try:
            with session_scope() as db:
                user = db.get(User, uid)
                if retry_failed:
                    clear_failed_markers(db, user_id=uid)
                summaries[uid] = run_for_user(db, user)
        except Exception as exc:  # isolate per-user failures
            log.exception("daily run failed for user %s", uid)
            r = RunResult()
            r.errors.append(f"run failed: {exc}")
            summaries[uid] = r
    return summaries
