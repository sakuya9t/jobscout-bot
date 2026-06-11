"""The daily pipeline: scrape companies -> upsert positions -> cheap pre-filter
-> LLM filter+score against the user's resume -> persist MatchResults.

Designed to run headless (scheduler / CLI / MCP) using ``session_scope`` as well
as inside a request with an injected session."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Company, Interest, MatchResult, Position, Resume, User
from ..schemas import MatchVerdict
from ..timeutil import utcnow
from . import scraper
from .ollama_client import OllamaClient, OllamaError, get_client

log = logging.getLogger(__name__)

# Structured-output schema the scoring model must fill (Ollama `format`). Derived
# from the Pydantic model so the schema and the parser never drift apart.
MATCH_SCHEMA = MatchVerdict.model_json_schema()

# Stage 1: a CHEAP model decides relevance (semantic, not substring) so we only
# spend the expensive scoring model on postings that actually fit the interest.
# Batched + free-text JSON, NOT Ollama's structured `format`: cheap models ignore
# the `format` grammar but happily emit a JSON array when asked to in the prompt.
FILTER_SYSTEM_PROMPT = (
    "You are a fast recruiting screener. Given a candidate's role requirements and a "
    "NUMBERED list of job postings, decide for EACH posting whether it is a plausible "
    "match for what they're looking for — the right kind of role, seniority, field, "
    "and location. Judge meaning, not exact keywords (e.g. 'backend' ≈ 'server-side', "
    "'Bay Area' ≈ 'San Francisco'). Be inclusive at the margin: when unsure, use "
    "match=true (the detailed scoring step is stricter). Use match=false only for "
    "clearly wrong roles (wrong discipline/seniority, an excluded keyword, or an "
    "impossible location).\n"
    "Respond with ONLY a JSON array — one object per posting, using its number as "
    '"id": [{"id": 1, "match": true}, {"id": 2, "match": false}]. No prose, no code fences.'
)


def _parse_yes_no(text: str) -> bool:
    """First yes/no token wins; default to YES (inclusive) when unclear, matching
    the prompt's 'when unsure, let it through' instruction."""
    match = re.search(r"\b(yes|no)\b", (text or "").lower())
    return match.group(1) == "yes" if match else True

# Stage 2: the GOOD model does the expensive resume<->role scoring.
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
_FILTER_DESC_CHARS = 800  # the cheap relevance filter only needs a short blurb per posting

# Marker stored on the `model` column of a MatchResult when scoring terminally
# failed, so the pair is skipped on re-runs rather than re-billed. Cleared by
# `clear_failed_markers` (CLI: run-daily --retry-failed).
ERROR_MODEL = "error"


_MAX_REPORTED_ERRORS = 5


@dataclass
class RunResult:
    new_positions: int = 0
    scored: int = 0  # postings the good model fully scored (passed the cheap filter)
    filtered: int = 0  # postings the cheap model judged not a match
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
    """Cheap *negative-only* gate: drop a posting only when it hits an explicit
    exclude keyword. Positive relevance (title/location/seniority fit) is NOT
    decided here by substring matching — that's the cheap LLM filter's job, which
    judges meaning instead of exact text. So title/location act as hints to the
    models, never as a hard keyword gate that can silently exclude everything."""
    excludes = _csv(interest.exclude_keywords)
    if not excludes:
        return True
    haystack = f"{pos.title} {pos.location or ''} {pos.department or ''}".lower()
    return not any(x in haystack for x in excludes)


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


def _build_filter_batch_prompt(interest: Interest, positions: list[Position]) -> str:
    """Stage-1 prompt: interest requirements vs a NUMBERED batch of postings (no
    resume — relevance doesn't need it, and a short per-posting blurb keeps the
    cheap batch call small)."""
    reqs = [f"- {k}: {v}" for k, v in {
        "Desired titles": interest.title_keywords,
        "Locations": interest.locations,
        "Seniority": interest.seniority,
        "Employment type": interest.employment_type,
        "Exclusions": interest.exclude_keywords,
        "Notes": interest.notes,
    }.items() if v]
    blocks = [
        f"{i}. {pos.title} | loc: {pos.location or 'n/a'} | dept: {pos.department or 'n/a'}\n"
        f"   {(pos.description or '')[:_FILTER_DESC_CHARS]}"
        for i, pos in enumerate(positions, 1)
    ]
    return (
        "## CANDIDATE REQUIREMENTS\n" + ("\n".join(reqs) or "(none specified)") + "\n\n"
        "## JOB POSTINGS (decide a match for each, by its number)\n" + "\n".join(blocks) + "\n"
    )


def _extract_json_array(text: str | None) -> list | None:
    if not text:
        return None
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _parse_filter_batch(text: str, positions: list[Position]) -> dict[int, tuple[bool, str]]:
    """Map the cheap model's batch reply to ``{pos_id: (matches, reason)}``. Robust
    to format drift: prefer a JSON array keyed by 1-based ``id``, fall back to
    ``n: yes/no`` lines, then to a single global YES/NO, and finally fail OPEN
    (keep) for anything still missing so the stricter scoring step still gets a look."""
    verdicts: dict[int, tuple[bool, str]] = {}
    for item in _extract_json_array(text) or []:
        if not isinstance(item, dict):
            continue
        idx = item.get("id", item.get("i", item.get("index")))
        match = item.get("match", item.get("matches"))
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if 1 <= idx <= len(positions) and isinstance(match, bool):
            verdicts[positions[idx - 1].id] = (match, str(item.get("reason") or "")[:500])
    if len(verdicts) < len(positions):  # line fallback "n: yes/no"
        for m in re.finditer(r"(\d+)\s*[:.)\-]\s*(yes|no)\b", text.lower()):
            idx = int(m.group(1))
            if 1 <= idx <= len(positions):
                verdicts.setdefault(positions[idx - 1].id, (m.group(2) == "yes", ""))
    if not verdicts:  # no per-id signal — treat a lone YES/NO as global
        keep = _parse_yes_no(text)
        return {pos.id: (keep, (text or "").strip()[:500]) for pos in positions}
    for pos in positions:  # fail open on anything the model skipped
        verdicts.setdefault(pos.id, (True, ""))
    return verdicts


def _filter_batch(
    client: OllamaClient, interest: Interest, positions: list[Position]
) -> tuple[dict[int, tuple[bool, str]], str | None]:
    """Stage 1 (cheap model), batched: one call screens ``positions``. Returns
    ``({pos_id: (matches, reason)}, None)`` or ``({}, message)`` on a terminal error."""
    if not positions:
        return {}, None
    try:
        text = client.chat_text(FILTER_SYSTEM_PROMPT, _build_filter_batch_prompt(interest, positions))
    except OllamaError as exc:
        log.warning("batch filter failed (%d postings): %s", len(positions), exc)
        return {}, f"Filtering failed: {exc}"
    return _parse_filter_batch(text, positions), None


def _persist_error_marker(
    db: Session, user: User, resume: Resume, interest: Interest, pos: Position, message: str
) -> None:
    """Mark a (position, interest) pair as terminally failed so it isn't re-billed
    until ``--retry-failed`` clears it."""
    db.add(MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        reasoning=message[:1000], model=ERROR_MODEL,
    ))
    db.flush()


def _persist_filter_reject(
    db: Session, user: User, resume: Resume, interest: Interest, pos: Position,
    reason: str, filter_model: str,
) -> None:
    """Record a cheap-filter 'not a match' so it ranks out of the report and the
    pair lands in the ``already`` set (not re-screened next run)."""
    db.add(MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        reasoning=(reason or "Screened out as not a match.")[:1000],
        strengths=json.dumps([]), gaps=json.dumps([]), model=filter_model,
    ))
    db.flush()


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
        # Guard against a board repeating an id within one scrape — a second
        # row would violate uq_position_company_extid and abort the whole run.
        existing[sp.external_id] = pos
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


def run_for_user(
    db: Session,
    user: User,
    client: OllamaClient | None = None,
    filter_client: OllamaClient | None = None,
) -> RunResult:
    """Scrape all of a user's companies and score newly-seen / unscored positions
    against their active resume. Idempotent: positions already scored for the
    active resume are skipped, so re-running the same day costs nothing extra.

    Two-stage scoring: a cheap model triages relevance, then the good (scoring)
    model — ``client`` if injected, else ``get_client()`` — only scores the
    survivors. A per-run cap bounds how many postings are evaluated."""
    score_client = client or get_client()
    filter_client = filter_client or OllamaClient(model=settings.ollama_filter_model)
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

    # Newest first so the per-run cap spends its budget on the freshest postings.
    all_positions.sort(key=lambda p: p.first_seen_at or utcnow(), reverse=True)
    # Postings with no scraped description come from the generic HTML fallback (nav
    # links etc.). Scoring them just bills the LLM to read "(no description scraped)"
    # and yields meaningless numbers, so skip them and (for newly-seen ones) nudge
    # the user to configure a real ATS instead.
    described = [p for p in all_positions if (p.description or "").strip()]
    undescribed = sum(
        1 for p in all_positions if p.id in new_ids and not (p.description or "").strip()
    )

    # Per-run budget on how many postings we evaluate; bounds the cheap-filter and
    # scoring work alike. 0 = unlimited.
    budget = settings.score_max_per_run
    batch_size = max(1, settings.score_filter_batch_size)
    evaluated = 0
    truncated = False
    excluded = 0  # pairs dropped by an explicit exclude keyword (before any LLM call)
    filter_model = settings.ollama_filter_model

    # Evaluate each interest against the postings it hasn't been scored for. The
    # cheap relevance filter is BATCHED (one call screens up to batch_size postings);
    # only survivors get the expensive per-posting scoring.
    for interest in interests:
        if truncated:
            break
        candidates: list[Position] = []
        for pos in described:
            if (pos.id, interest.id) in already:
                continue
            if not _passes_prefilter(pos, interest):  # explicit exclude keyword
                excluded += 1
                continue
            candidates.append(pos)

        idx = 0
        while idx < len(candidates):
            if budget and evaluated >= budget:
                truncated = True
                break
            room = min(batch_size, budget - evaluated) if budget else batch_size
            batch = candidates[idx : idx + room]
            idx += len(batch)
            evaluated += len(batch)

            # Stage 1 — batched cheap relevance filter.
            verdicts, ferr = _filter_batch(filter_client, interest, batch)
            if ferr is not None:
                res.add_error(ferr)
                for pos in batch:  # marker so the batch isn't re-billed every run
                    _persist_error_marker(db, user, resume, interest, pos, ferr)
                db.commit()
                continue
            for pos in batch:
                matches, reason = verdicts[pos.id]
                if not matches:
                    _persist_filter_reject(db, user, resume, interest, pos, reason, filter_model)
                    res.filtered += 1
                    continue
                # Stage 2 — expensive resume<->role scoring for survivors only.
                match_id, serr = _score_position(score_client, db, user, resume, interest, pos)
                if match_id is not None:
                    res.scored += 1
                    res.match_ids.append(match_id)
                if serr is not None:
                    res.add_error(serr)
            # Commit per batch so the write lock is released between calls and
            # partial progress (incl. markers) survives a crash mid-run.
            db.commit()

    if undescribed:
        res.add_error(
            f"{undescribed} posting(s) had no scraped description and were skipped — "
            "set the company's ATS (greenhouse/lever/ashby) for full job text."
        )
    if truncated:
        res.add_error(
            f"Reached this run's scoring cap ({budget}) — more postings remain "
            "unscored. Click Run again to continue, or raise JOBSCOUT_SCORE_MAX_PER_RUN."
        )
    # Explain a "0 scored" run instead of leaving it silent (cf. the pre-filter was
    # silent by design). Order matters: the relevance filter is the usual culprit.
    if res.scored == 0 and not truncated:
        if res.filtered:
            res.add_error(
                f"The relevance filter judged none of the {res.filtered} evaluated "
                "posting(s) a match for your interests — broaden an interest "
                "(titles/locations/notes) and run again."
            )
        elif excluded:
            res.add_error(
                f"{excluded} posting(s) were dropped by your interest's exclude "
                "keywords before scoring. Loosen the exclude list if that's too broad."
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
                from . import reporter

                user = db.get(User, uid)
                if retry_failed:
                    clear_failed_markers(db, user_id=uid)
                summaries[uid] = run_for_user(db, user)
                reporter.record_job_list_snapshot(db, user, summaries[uid])
        except Exception as exc:  # isolate per-user failures
            log.exception("daily run failed for user %s", uid)
            r = RunResult()
            r.errors.append(f"run failed: {exc}")
            summaries[uid] = r
    return summaries
