"""The daily pipeline: scrape companies -> upsert positions -> cheap pre-filter
-> LLM filter+score against the user's resume -> persist MatchResults.

Designed to run headless (scheduler / CLI / MCP) using ``session_scope`` as well
as inside a request with an injected session."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Company, Interest, MatchResult, Position, Resume, Subscription, User
from ..schemas import MatchVerdict
from ..timeutil import utcnow
from . import llm, scraper
from .ollama_client import OllamaBudgetError, OllamaClient, OllamaError

log = logging.getLogger(__name__)

class BatchMatchVerdict(MatchVerdict):
    """One scoring verdict inside a batched response. ``id`` is the 1-based
    posting number from the prompt so we can persist each result to the right row."""

    id: int = Field(ge=1)


class MatchBatchResponse(BaseModel):
    results: list[BatchMatchVerdict]


MATCH_BATCH_SCHEMA = MatchBatchResponse.model_json_schema()

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
_FILTER_DESC_CHARS = 800  # the cheap relevance filter only needs a short blurb per posting
_SCORE_BATCH_DESC_CHARS = 1500  # batch scoring keeps one resume + N postings in context

# Marker stored on the `model` column of a MatchResult when scoring terminally
# failed, so the pair is skipped on re-runs rather than re-billed. Cleared by
# `clear_failed_markers` so a later scan re-scores the pair.
ERROR_MODEL = "error"
# Marker for a pair dropped by an explicit exclude keyword before any LLM call.
# Persisting it (instead of just counting) means the pair leaves the evaluation
# backlog, so the "positions unevaluated" count converges to zero and the
# background drain terminates.
EXCLUDED_MODEL = "excluded"


_MAX_REPORTED_ERRORS = 5
# Two per-user locks with distinct jobs (see plan): the SCRAPE lock (held briefly)
# stops two concurrent scrapes racing on uq_position_company_extid; the SCORE lock
# (held for a whole drain) ensures only one evaluator works a user at a time. They
# are separate so /api/run can scrape while a background drain is scoring.
_SCRAPE_LOCKS: dict[int, threading.Lock] = {}
_SCORE_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(registry: dict[int, threading.Lock], user_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = registry.get(user_id)
        if lock is None:
            lock = threading.Lock()
            registry[user_id] = lock
        return lock


@dataclass
class RunResult:
    new_positions: int = 0
    scored: int = 0  # postings the good model fully scored (passed the cheap filter)
    filtered: int = 0  # postings the cheap model judged not a match
    # Set when scoring stopped because the Ollama budget/quota was exhausted. The
    # background drainer checks this to avoid re-arming against a dead quota.
    budget_exhausted: bool = False
    # Set when scoring stopped because the per-run wall-clock budget elapsed (a big
    # backlog that didn't fit one run). Unlike budget_exhausted this re-arms normally:
    # progress is committed per batch and finalize leaves the queue row ``pending`` so
    # the next run continues. Prevents the drain being killed mid-run and stranded.
    time_exhausted: bool = False
    # False when score_to_completion couldn't acquire the score lock (another drain
    # is already working this user), so the caller can skip recording a snapshot.
    did_run: bool = True
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


def _build_score_batch_prompt(resume: Resume, interest: Interest, positions: list[Position]) -> str:
    """Stage-2 prompt: score multiple postings in one model call. The resume and
    requirements are repeated once, then each posting is numbered so the JSON
    response can be mapped back to DB rows."""
    reqs = [f"- {k}: {v}" for k, v in {
        "Desired titles": interest.title_keywords,
        "Locations": interest.locations,
        "Seniority": interest.seniority,
        "Employment type": interest.employment_type,
        "Exclusions": interest.exclude_keywords,
        "Notes": interest.notes,
    }.items() if v]
    blocks = [
        f"### Posting {i}\n"
        f"Company position id: {pos.external_id}\n"
        f"Title: {pos.title}\n"
        f"Location: {pos.location or 'n/a'}\n"
        f"Department: {pos.department or 'n/a'}\n"
        f"Employment type: {pos.employment_type or 'n/a'}\n"
        f"Description:\n{(pos.description or '(no description scraped)')[:_SCORE_BATCH_DESC_CHARS]}\n"
        for i, pos in enumerate(positions, 1)
    ]
    return (
        "Score every numbered posting independently against the same resume and "
        "candidate requirements. Return JSON with a top-level `results` array. "
        "Each result must include the posting's 1-based `id` and the same verdict "
        "fields requested by the schema. Do not omit postings.\n\n"
        "## CANDIDATE REQUIREMENTS\n" + ("\n".join(reqs) or "(none specified)") + "\n\n"
        "## RESUME\n" + resume.content_text[:_RESUME_CHARS] + "\n\n"
        "## JOB POSTINGS\n" + "\n".join(blocks) + "\n"
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
    except OllamaBudgetError:
        raise  # recoverable: let the run abort cleanly without poisoning postings
    except OllamaError as exc:
        log.warning("batch filter failed (%d postings): %s", len(positions), exc)
        return {}, f"Filtering failed: {exc}"
    return _parse_filter_batch(text, positions), None


def _flush_match(db: Session, result: MatchResult) -> bool:
    """Flush one new MatchResult inside a SAVEPOINT, tolerating the cross-process
    race where another drain (the periodic scoring cron vs. an on-demand web scan)
    already scored this (user, position, resume, interest) pair. The ``uq_match_unique``
    violation is swallowed and ``False`` returned; the SAVEPOINT rolls back only this
    row, so the rest of the batch (and its LLM spend) is kept rather than re-billed.
    Returns True when the row was inserted."""
    try:
        with db.begin_nested():
            db.add(result)
            db.flush()
        return True
    except IntegrityError:
        log.debug("match row already scored by a concurrent drain — skipping the pair")
        return False


def _persist_error_marker(
    db: Session, user: User, resume: Resume, interest: Interest, pos: Position, message: str
) -> None:
    """Mark a (position, interest) pair as terminally failed so it isn't re-billed
    until ``clear_failed_markers`` clears it."""
    _flush_match(db, MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        reasoning=message[:1000], model=ERROR_MODEL,
    ))


def _persist_filter_reject(
    db: Session, user: User, resume: Resume, interest: Interest, pos: Position,
    reason: str, filter_model: str,
) -> None:
    """Record a cheap-filter 'not a match' so it ranks out of the report and the
    pair lands in the ``already`` set (not re-screened next run)."""
    _flush_match(db, MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        reasoning=(reason or "Screened out as not a match.")[:1000],
        strengths=json.dumps([]), gaps=json.dumps([]), model=filter_model,
    ))


def _persist_exclude_reject(
    db: Session, user: User, resume: Resume, interest: Interest, pos: Position
) -> None:
    """Record a keyword-exclusion 'not a match' so the pair leaves the backlog and
    ranks out of the report (passed_filter=False), without spending an LLM call."""
    _flush_match(db, MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        reasoning="Dropped by an interest exclude keyword.",
        strengths=json.dumps([]), gaps=json.dumps([]), model=EXCLUDED_MODEL,
    ))


def _persist_score_result(
    db: Session,
    user: User,
    resume: Resume,
    interest: Interest,
    pos: Position,
    verdict: MatchVerdict,
    model: str,
) -> int | None:
    """Persist one scored verdict; returns the new row id, or None if a concurrent
    drain already scored this pair (see ``_flush_match``)."""
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
        model=model,
    )
    if not _flush_match(db, result):
        return None
    return result.id


def _upsert_positions(db: Session, company: Company) -> tuple[list[Position], list[str]]:
    """Scrape one company and upsert. Returns (new_positions, errors)."""
    errors: list[str] = []
    try:
        result = scraper.scrape_company(company)
    except scraper.ScrapeError as exc:
        return [], [str(exc)]
    except Exception as exc:  # defensive: never let one company kill the run
        return [], [f"{company.name}: unexpected scrape error: {exc}"]

    # Normalize: production returns a ScrapeResult; tests that monkeypatch
    # scrape_company may still return a bare list (no availability signal).
    if isinstance(result, scraper.ScrapeResult):
        scraped, live_ids, coverage = result.positions, result.live_external_ids, result.coverage
    else:
        scraped, live_ids, coverage = result, None, None

    new_positions: list[Position] = []
    # Positions still missing a description that this ATS only exposes on the job
    # detail page (eightfold): (position, detail_url), newest-first. Covers both
    # freshly-inserted rows and previously-stored description-less ones (backfill).
    needs_desc: list[tuple[Position, str]] = []
    detail_desc = company.ats_type == "eightfold"  # only ATS that needs a detail fetch
    existing = {
        p.external_id: p
        for p in db.scalars(select(Position).where(Position.company_id == company.id))
    }
    for sp in scraped:
        known = existing.get(sp.external_id)
        if known is not None:
            # Heal a stored row that has no description once the detail fetch can
            # supply one (e.g. the whole eightfold board predates this fix).
            if detail_desc and sp.url and not (known.description or "").strip():
                needs_desc.append((known, sp.url))
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
        if detail_desc and sp.url and not (sp.description or "").strip():
            needs_desc.append((pos, sp.url))

    if detail_desc and needs_desc and settings.scrape_eightfold_max_descriptions > 0:
        descs = scraper.fetch_eightfold_descriptions(
            [url for _, url in needs_desc], settings.scrape_eightfold_max_descriptions
        )
        for pos, url in needs_desc:
            text = descs.get(url)
            if text:
                pos.description = text

    _reconcile_removals(db, company, live_ids, coverage, existing)

    company.last_scraped_at = utcnow()
    db.flush()  # assign ids
    return new_positions, errors


def _reconcile_removals(
    db: Session,
    company: Company,
    live_ids: set[str] | None,
    coverage: str | datetime | None,
    existing: dict[str, Position],
) -> None:
    """Mark stored positions removed when they drop off the company's board, and clear
    the flag for any that reappear. ``coverage`` (from the scrape) bounds what may be
    inferred removed:

      * ``"full"`` – the whole board; any stored id that's absent is closed.
      * a ``datetime`` floor – the board is fully covered only for postings listed
        on/after it (a newest-first walk that reached the age cutoff), so a stored
        posting absent *and* newer than the floor is closed, while the older tail —
        outside what the scrape covered — is left untouched.
      * ``None`` – partial/unknown coverage; no removals are inferred.

    An empty ``live_ids`` is treated as a suspect fetch (a flukey-empty response would
    otherwise close the whole company) and skipped — a genuine reappearance heals the
    flag on the next non-empty crawl. ``existing`` is the (already-loaded) map of this
    company's stored positions, reused so we don't re-query."""
    if coverage is None or not live_ids:  # nothing authoritative to act on
        return
    floor = coverage if isinstance(coverage, datetime) else None  # None ⇒ "full"
    now = utcnow()
    for ext_id, pos in existing.items():
        if ext_id in live_ids:
            if pos.removed_at is not None:
                pos.removed_at = None  # relisted → available again
            continue
        if pos.removed_at is not None:
            continue  # absent and already marked removed
        # Absent from the board: only close it if it falls within what we covered.
        if floor is not None and (pos.posted_at is None or pos.posted_at < floor):
            continue  # older than the covered window (or undated) → can't conclude
        pos.removed_at = now


def _score_batch(
    client: OllamaClient,
    db: Session,
    user: User,
    resume: Resume,
    interest: Interest,
    positions: list[Position],
) -> tuple[list[int], str | None]:
    """Call the scoring model once for a batch of postings and persist one
    MatchResult per posting. This is the main request-count reducer: after the
    cheap relevance filter, survivors are scored N-at-a-time instead of one call
    per posting."""
    if not positions:
        return [], None

    prompt = _build_score_batch_prompt(resume, interest, positions)
    try:
        data = client.chat_json(SYSTEM_PROMPT, prompt, MATCH_BATCH_SCHEMA)
    except OllamaBudgetError:
        # Recoverable — don't write error-markers, or these postings would be
        # skipped until clear_failed_markers runs even after the user tops up.
        # Propagate so the run loop stops and reports it once.
        raise
    except OllamaError as exc:
        log.warning("batch scoring failed for %d postings: %s", len(positions), exc)
        message = f"Scoring failed: {exc}"
        for pos in positions:
            _persist_error_marker(db, user, resume, interest, pos, message)
        return [], f"Scoring failed: {exc}"

    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        message = "Scoring failed: model returned an invalid batch result"
        log.warning("batch scoring returned invalid top-level payload for %d postings", len(positions))
        for pos in positions:
            _persist_error_marker(db, user, resume, interest, pos, message)
        return [], message

    verdicts: dict[int, BatchMatchVerdict] = {}
    invalid = 0
    for raw in raw_results:
        try:
            verdict = BatchMatchVerdict.model_validate(raw)
        except ValidationError:
            invalid += 1
            continue
        if verdict.id > len(positions) or verdict.id in verdicts:
            invalid += 1
            continue
        verdicts[verdict.id] = verdict

    match_ids: list[int] = []
    missing = 0
    marker_message = "Scoring failed: model omitted or invalidated this posting in the batch"
    for idx, pos in enumerate(positions, 1):
        verdict = verdicts.get(idx)
        if verdict is None:
            missing += 1
            _persist_error_marker(db, user, resume, interest, pos, marker_message)
            continue
        match_id = _persist_score_result(db, user, resume, interest, pos, verdict, client.model)
        if match_id is not None:  # None ⇒ a concurrent drain already scored this pair
            match_ids.append(match_id)

    if invalid or missing:
        return (
            match_ids,
            f"Scoring failed for {invalid + missing} posting(s): model returned an incomplete batch result",
        )
    return match_ids, None


def clear_failed_markers(db: Session, user_id: int | None = None) -> int:
    """Delete error-marker MatchResults so failed (position, interest) pairs are
    re-scored on the next run. Returns the number cleared."""
    stmt = delete(MatchResult).where(MatchResult.model == ERROR_MODEL)
    if user_id is not None:
        stmt = stmt.where(MatchResult.user_id == user_id)
    return db.execute(stmt).rowcount or 0


def _custom_companies(db: Session, user: User) -> list[Company]:
    """The user's own custom companies (preset rows have a NULL user_id, so a
    ``user_id == me`` filter already excludes them). These are the only companies a
    user *scan* crawls; presets are crawled separately by crawl_presets."""
    return list(
        db.scalars(select(Company).where(Company.user_id == user.id, Company.is_active == True))  # noqa: E712
    )


def _user_companies(db: Session, user: User) -> list[Company]:
    """All active companies whose jobs are matched for this user: their own custom
    companies plus the global preset companies they subscribe to."""
    by_id = {c.id: c for c in _custom_companies(db, user)}
    subscribed = db.scalars(
        select(Company)
        .join(Subscription, Subscription.company_id == Company.id)
        .where(
            Subscription.user_id == user.id,
            Subscription.is_active == True,  # noqa: E712
            Company.is_active == True,  # noqa: E712
        )
    )
    by_id.update({c.id: c for c in subscribed})
    return list(by_id.values())


def _active_inputs(db: Session, user: User) -> tuple[Resume | None, list[Interest], list[Company]]:
    """Load the user's active resume, interests, and matched companies (custom +
    subscribed presets) — the shared inputs scoring needs."""
    resume = db.scalar(
        select(Resume).where(Resume.user_id == user.id, Resume.is_active == True)  # noqa: E712
        .order_by(Resume.created_at.desc())
    )
    interests = list(
        db.scalars(select(Interest).where(Interest.user_id == user.id, Interest.is_active == True))  # noqa: E712
    )
    return resume, interests, _user_companies(db, user)


def _described(positions: list[Position]) -> list[Position]:
    """Postings with a real scraped description, newest first. Description-less
    postings (generic HTML-fallback nav links) are never scored — the LLM would
    only read "(no description scraped)" — so they're excluded from the backlog."""
    described = [p for p in positions if (p.description or "").strip()]
    described.sort(key=lambda p: p.first_seen_at or utcnow(), reverse=True)
    return described


def scrape_only(db: Session, user: User, res: RunResult | None = None) -> RunResult:
    """Scrape + upsert the user's CUSTOM companies, recording new_positions and any
    per-company scrape errors on ``res``. Preset companies are global and crawled
    once by ``crawler.crawl_presets`` (daily / on-demand), never on a user scan.
    Serialized per user by the scrape lock so two concurrent scrapes can't race on
    uq_position_company_extid. Does NOT score — that's ``score_to_completion``'s."""
    res = res or RunResult()
    companies = _custom_companies(db, user)
    undescribed = 0
    # Commit per company so we never hold a write lock across the whole scrape
    # (SQLite) and a later failure can't discard already-scraped postings.
    with _lock_for(_SCRAPE_LOCKS, user.id):
        for company in companies:
            new_positions, errs = _upsert_positions(db, company)
            for e in errs:
                res.add_error(e)
            res.new_positions += len(new_positions)
            undescribed += sum(1 for p in new_positions if not (p.description or "").strip())
            db.commit()
    if undescribed:
        res.add_error(
            f"{undescribed} posting(s) had no scraped description and were skipped — "
            "set the company's ATS (greenhouse/lever/ashby) for full job text."
        )
    return res


def count_pending(db: Session, user: User) -> int:
    """Size of the evaluation backlog: (described position × active interest) pairs
    of the user's active companies that still lack a MatchResult for the active
    resume. 0 when there's no active resume/interests/companies (nothing to score).
    This is the number the dashboard shows as "positions still being evaluated"."""
    resume, interests, companies = _active_inputs(db, user)
    if not resume or not interests or not companies:
        return 0
    company_ids = [c.id for c in companies]
    described_ids = {
        p.id for p in db.scalars(
            select(Position).where(
                Position.company_id.in_(company_ids), Position.removed_at.is_(None)
            )
        )
        if (p.description or "").strip()
    }
    if not described_ids:
        return 0
    interest_ids = [i.id for i in interests]
    filled = sum(
        1
        for pid, iid in db.execute(
            select(MatchResult.position_id, MatchResult.interest_id).where(
                MatchResult.user_id == user.id,
                MatchResult.resume_id == resume.id,
                MatchResult.interest_id.in_(interest_ids),
            )
        )
        if pid in described_ids
    )
    return len(described_ids) * len(interest_ids) - filled


def _past_deadline(deadline: float | None) -> bool:
    """True once the run's wall-clock budget (a ``time.monotonic()`` value) is spent."""
    return deadline is not None and time.monotonic() >= deadline


def score_to_completion(
    db: Session,
    user: User,
    res: RunResult | None = None,
    *,
    client: OllamaClient | None = None,
    filter_client: OllamaClient | None = None,
    deadline: float | None = None,
) -> RunResult:
    """Score the user's current evaluation backlog. Two-stage batched scoring: a cheap
    model triages relevance, then the good model scores survivors. Guarded by the
    non-blocking score lock so only one drain runs per user; if another already holds
    it, returns immediately with ``res.did_run = False``.

    Drains to completion by default. ``deadline`` (a ``time.monotonic()`` value) caps
    the run: when it's reached the drain stops between batches and sets
    ``res.time_exhausted`` — committed progress is kept and the backlog finishes on the
    next run. This is what lets the periodic cron split one huge backlog across runs
    instead of being killed mid-drain (see services/evaluator.drain_queue). Also stops
    and sets ``res.budget_exhausted`` if the Ollama quota runs out (no markers written,
    so it re-scores once quota returns)."""
    res = res or RunResult()
    lock = _lock_for(_SCORE_LOCKS, user.id)
    if not lock.acquire(blocking=False):
        log.debug("score_to_completion: a drain is already running for user %s — skipping", user.id)
        res.did_run = False
        return res
    try:
        _score_locked(db, user, res, client=client, filter_client=filter_client, deadline=deadline)
    finally:
        lock.release()
    return res


def _score_locked(
    db: Session,
    user: User,
    res: RunResult,
    *,
    client: OllamaClient | None = None,
    filter_client: OllamaClient | None = None,
    deadline: float | None = None,
) -> None:
    # Build the scoring + relevance-filter clients from this user's effective LLM
    # config (their provider/key/models, else the global defaults), unless a caller
    # injected clients (tests, or a future override).
    if client is None or filter_client is None:
        default_score, default_filter = llm.clients_for_user(db, user)
    score_client = client or default_score
    filter_client = filter_client or default_filter

    resume, interests, companies = _active_inputs(db, user)
    if not resume:
        res.add_error("No active resume uploaded — cannot score.")
    if not interests:
        res.add_error("No active interests configured — nothing to match against.")
    if not resume or not interests or not companies:
        return

    company_ids = [c.id for c in companies]
    described = _described(
        list(db.scalars(
            select(Position).where(
                Position.company_id.in_(company_ids), Position.removed_at.is_(None)
            )
        ))
    )
    already = {
        (m.position_id, m.interest_id)
        for m in db.scalars(
            select(MatchResult).where(
                MatchResult.user_id == user.id, MatchResult.resume_id == resume.id
            )
        )
    }

    batch_size = max(1, settings.score_filter_batch_size)
    score_batch_size = max(1, settings.score_batch_size)
    filter_model = filter_client.model  # the model name we tag filter-rejects with
    excluded = 0  # pairs dropped by an explicit exclude keyword (no LLM call)

    # Evaluate every active interest against the postings it hasn't been scored for.
    # Both LLM stages are batched. Every processed pair gets *some* MatchResult row
    # (score / filter-reject / exclude / error-marker), so the backlog strictly
    # shrinks and the drain terminates.
    try:
        for interest in interests:
            if _past_deadline(deadline):
                res.time_exhausted = True
                return
            candidates: list[Position] = []
            for pos in described:
                if (pos.id, interest.id) in already:
                    continue
                if not _passes_prefilter(pos, interest):  # explicit exclude keyword
                    _persist_exclude_reject(db, user, resume, interest, pos)
                    excluded += 1
                    continue
                candidates.append(pos)
            db.commit()  # persist exclude markers (so they leave the backlog)

            idx = 0
            while idx < len(candidates):
                # Stop cleanly at the run budget — the previous batch is already
                # committed, so the remaining backlog finishes on the next run.
                if _past_deadline(deadline):
                    res.time_exhausted = True
                    return
                batch = candidates[idx : idx + batch_size]
                idx += len(batch)

                # Stage 1 — batched cheap relevance filter.
                verdicts, ferr = _filter_batch(filter_client, interest, batch)
                if ferr is not None:
                    res.add_error(ferr)
                    for pos in batch:  # marker so the batch isn't re-billed every run
                        _persist_error_marker(db, user, resume, interest, pos, ferr)
                    db.commit()
                    continue
                survivors: list[Position] = []
                for pos in batch:
                    matches, reason = verdicts[pos.id]
                    if not matches:
                        _persist_filter_reject(db, user, resume, interest, pos, reason, filter_model)
                        res.filtered += 1
                        continue
                    survivors.append(pos)

                # Stage 2 — expensive resume<->role scoring for survivors only.
                for start in range(0, len(survivors), score_batch_size):
                    score_batch = survivors[start : start + score_batch_size]
                    match_ids, serr = _score_batch(score_client, db, user, resume, interest, score_batch)
                    res.scored += len(match_ids)
                    res.match_ids.extend(match_ids)
                    if serr is not None:
                        res.add_error(serr)
                # Commit per batch so the write lock is released between calls and
                # partial progress (incl. markers) survives a crash mid-drain.
                db.commit()
    except OllamaBudgetError as exc:
        # Quota ran out. Discard the half-processed batch (no markers written, so the
        # remaining postings re-score automatically once quota is back), then surface
        # one clear message. Earlier committed batches + exclude markers are kept.
        res.budget_exhausted = True
        db.rollback()
        res.add_error(
            "Ollama budget/quota appears to be exhausted, so scoring stopped early. "
            "No results were lost — the remaining postings will be scored "
            "automatically on the next run once your Ollama account has quota again. "
            f"(Ollama said: {exc})"
        )

    # Explain a "0 scored" outcome instead of leaving it silent (but not when we
    # stopped early on a budget — there's simply more to do next run).
    if res.scored == 0 and not res.budget_exhausted and not res.time_exhausted:
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


def run_for_user(
    db: Session,
    user: User,
    client: OllamaClient | None = None,
    filter_client: OllamaClient | None = None,
) -> RunResult:
    """Synchronous full run: scrape, then drain the entire scoring backlog to
    completion. Used by the daily scheduler, CLI, MCP, and tests. (The web path
    scrapes here too but defers scoring to the background evaluator.)"""
    res = RunResult()
    scrape_only(db, user, res)
    score_to_completion(db, user, res, client=client, filter_client=filter_client)
    res.finalize_errors()
    return res


def scrape_for_all_users() -> dict[int, RunResult]:
    """Daily-cron entry point. Crawl the shared preset catalog ONCE, then scrape each
    user's custom companies and persist any new positions — committed independently
    per user. Deliberately does NOT score: matching is expensive per user, so it's
    deferred to an on-demand scan from the job-list view (web ``/api/run`` -> the
    background evaluator in ``services/evaluator``). New positions simply accumulate
    until the user runs a scan, which scores the whole backlog."""
    from . import crawler

    crawler.crawl_presets()  # shared, once — not per user

    summaries: dict[int, RunResult] = {}
    with session_scope() as db:
        user_ids = list(db.scalars(select(User.id)))
    for uid in user_ids:
        try:
            with session_scope() as db:
                user = db.get(User, uid)
                res = scrape_only(db, user)
                res.finalize_errors()
                summaries[uid] = res
        except Exception as exc:  # isolate per-user failures
            log.exception("daily scrape failed for user %s", uid)
            r = RunResult()
            r.errors.append(f"scrape failed: {exc}")
            summaries[uid] = r
    return summaries
