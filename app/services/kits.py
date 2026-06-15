"""Generate a per-position "application kit": a summary of what the role is
looking for, the open application questions it (likely) asks with advice + a draft
answer, a tailored cover letter, and a revised resume.

This is the writing counterpart to the matcher's scoring: it runs the GOOD model a
few times for ONE posting, on demand (an explicit click), and persists the result
on an ``ApplicationKit`` row. Reads never call the LLM — the row is cached and only
re-generated when the user explicitly asks again. Designed to run headless inside
the background worker (``kit_worker``) via ``session_scope`` and to be called
directly in tests with an injected client (like ``matcher``)."""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from sqlalchemy.orm import Session

from ..models import ApplicationKit, Interest, MatchResult, Position, Resume, User
from . import llm
from .ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)


# ── Structured analysis contract ─────────────────────────────────────────────
class GeneratedQuestion(BaseModel):
    """One open application question the model surfaced for a posting."""

    question: str
    advice: str = ""
    suggested_answer: str = ""


class KitAnalysis(BaseModel):
    """The structured first call: what the role wants + its open questions. Source
    of truth for both the Ollama ``format`` schema and parsing (like
    ``schemas.MatchVerdict``)."""

    looking_for: list[str] = Field(default_factory=list)
    open_questions: list[GeneratedQuestion] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """The structured resume call: the rewritten resume as copy-paste-ready
    Markdown, plus a short note on what was optimized for this specific position."""

    resume_markdown: str = ""
    optimization_summary: str = ""


ANALYSIS_SCHEMA = KitAnalysis.model_json_schema()
RESUME_SCHEMA = TailoredResume.model_json_schema()

_RESUME_CHARS = 8000  # the rewrite needs a fuller resume than scoring's 6000
_DESC_CHARS = 6000

ANALYSIS_SYSTEM = (
    "You are an expert career coach and technical recruiter helping a candidate "
    "apply to ONE specific job. Read the candidate's resume and the job posting, "
    "then return JSON with two fields.\n"
    "`looking_for`: 4-8 concise bullet strings naming the most important things "
    "this employer wants — must-have skills, experience, responsibilities, and "
    "qualities.\n"
    "`open_questions`: the open-ended questions the candidate would likely have to "
    "answer when applying. Include any the posting explicitly asks for (a required "
    "cover letter, 'Why do you want to work at <company>?', 'Describe a time "
    "you…'), plus the 1-3 most likely ones for this kind of role/company when none "
    "are stated. For each give `question`, `advice` (2-3 sentences on how to "
    "approach it), and `suggested_answer` (a concrete first-person draft grounded "
    "in the resume, 3-6 sentences). Be specific and truthful — never invent "
    "experience the resume does not support."
)

COVER_LETTER_SYSTEM = (
    "You are an expert cover-letter writer. Write a tailored, specific, concise "
    "cover letter (3-4 short paragraphs) for the candidate applying to this exact "
    "role. Ground every claim in the candidate's real resume — do not fabricate "
    "experience. Make clear why they fit this role and this company. Return ONLY "
    "the cover letter text: no preamble, no markdown headings, and no bracketed "
    "placeholders unless the resume genuinely lacks the detail."
)

RESUME_SYSTEM = (
    "You are an expert resume writer. Produce a revised version of the candidate's "
    "resume tailored to this specific role. Keep it 100% truthful — never add "
    "experience, skills, titles, dates, or employers that are not in the original "
    "— but reorder and re-emphasize the most relevant experience and skills, and "
    "mirror the role's language wherever the resume genuinely supports it.\n"
    "Return JSON with two fields. `resume_markdown`: the full revised resume as "
    "polished, copy-paste-ready Markdown that still stays ATS-friendly. Use this "
    "visual structure exactly when the source resume supports the details:\n"
    "- The candidate's name as the only `#` heading.\n"
    "- A compact contact/value line directly under the name using plain text and "
    "pipes, e.g. `City | email | phone | LinkedIn/GitHub/portfolio`.\n"
    "- `---` after the contact line.\n"
    "- `## Professional Summary`: 3-4 crisp lines tailored to the job, not generic.\n"
    "- `## Core Skills`: grouped bullets such as `- Languages: ...`, "
    "`- Platforms: ...`, `- Practices: ...`; only include supported skills.\n"
    "- Optional `## Selected Impact` with 3-5 quantified or outcome-oriented bullets "
    "when the resume contains real accomplishments worth featuring.\n"
    "- `## Professional Experience`: within each entry, company name as a `###` "
    "heading, then a `**Role | Location | Dates**` line, followed by 3-6 bullets. "
    "Lead bullets with strong verbs, surface metrics when present, and tune wording "
    "to the job posting without fabricating.\n"
    "- `## Projects`, `## Education`, `## Certifications`, or `## Publications` only "
    "when present in the source resume.\n"
    "Never use Markdown tables, HTML, emojis, code fences, icons, fake placeholders, "
    "or columns. Keep it concise enough to print cleanly; prefer one to two pages. "
    "`optimization_summary`: "
    "3-4 sentences, addressed to the candidate, explaining what you changed and "
    "emphasized to fit this specific position."
)


def _requirements_block(interest: Interest | None) -> str:
    if interest is None:
        return "(none specified)"
    reqs = [f"- {k}: {v}" for k, v in {
        "Desired titles": interest.title_keywords,
        "Locations": interest.locations,
        "Seniority": interest.seniority,
        "Employment type": interest.employment_type,
        "Notes": interest.notes,
    }.items() if v]
    return "\n".join(reqs) or "(none specified)"


def _posting_block(pos: Position, company_name: str) -> str:
    return (
        f"Company: {company_name}\n"
        f"Title: {pos.title}\n"
        f"Location: {pos.location or 'n/a'}\n"
        f"Department: {pos.department or 'n/a'}\n"
        f"Employment type: {pos.employment_type or 'n/a'}\n"
        f"Description:\n{(pos.description or '(no description scraped)')[:_DESC_CHARS]}"
    )


def _analysis_prompt(resume: Resume, interest: Interest | None, pos: Position, company_name: str) -> str:
    return (
        "## CANDIDATE REQUIREMENTS\n" + _requirements_block(interest) + "\n\n"
        "## RESUME\n" + resume.content_text[:_RESUME_CHARS] + "\n\n"
        "## JOB POSTING\n" + _posting_block(pos, company_name) + "\n"
    )


def _writing_prompt(
    resume: Resume, pos: Position, company_name: str, looking_for: list[str]
) -> str:
    wants = "\n".join(f"- {w}" for w in looking_for) or "(see the posting)"
    return (
        "## WHAT THIS ROLE IS LOOKING FOR\n" + wants + "\n\n"
        "## CANDIDATE RESUME\n" + resume.content_text[:_RESUME_CHARS] + "\n\n"
        "## JOB POSTING\n" + _posting_block(pos, company_name) + "\n"
    )


def _best_match(db: Session, user: User, position: Position) -> MatchResult | None:
    """The stored match the detail page treats as authoritative for this position:
    a passing match (highest score) when one exists, else the highest-scoring row.
    Its interest supplies the requirement notes used to steer generation."""
    return db.scalar(
        select(MatchResult)
        .where(MatchResult.user_id == user.id, MatchResult.position_id == position.id)
        .order_by(
            MatchResult.passed_filter.desc(),
            MatchResult.match_score.desc(),
            MatchResult.win_probability.desc(),
        )
        .limit(1)
    )


def get_or_create(db: Session, user: User, position: Position) -> ApplicationKit:
    """Find this (user, position)'s kit row or create a fresh one (no LLM call)."""
    kit = db.scalar(
        select(ApplicationKit).where(
            ApplicationKit.user_id == user.id, ApplicationKit.position_id == position.id
        )
    )
    if kit is None:
        kit = ApplicationKit(user_id=user.id, position_id=position.id, status="generating")
        db.add(kit)
        db.flush()
    return kit


def mark_generating(db: Session, user: User, position: Position, resume: Resume | None) -> ApplicationKit:
    """Reset the kit to a clean 'generating' state and persist it, so a client
    polling immediately after a (re)generate request sees work in progress with the
    stale content cleared. The background worker then fills it in."""
    kit = get_or_create(db, user, position)
    kit.status = "generating"
    kit.resume_id = resume.id if resume else None
    kit.error_detail = None
    kit.looking_for = None
    kit.open_questions = None
    kit.cover_letter = None
    kit.revised_resume = None
    db.flush()
    return kit


def generate(
    db: Session,
    user: User,
    position: Position,
    *,
    client: OllamaClient | None = None,
) -> ApplicationKit:
    """Generate (or regenerate) the application kit for ``position`` and persist it.

    Runs three GOOD-model calls — structured analysis, cover letter, revised resume
    — committing after each so a client polling sees partial results and a late
    failure keeps what already succeeded. Sets ``status='ok'`` on success or
    ``status='error'`` (with ``error_detail``) on a terminal LLM failure."""
    kit = get_or_create(db, user, position)
    kit.status = "generating"
    kit.error_detail = None
    # Clear any prior content so a regenerate never shows a mix of old and new
    # (the POST path resets via mark_generating; this keeps a direct call correct).
    kit.looking_for = kit.open_questions = kit.cover_letter = None
    kit.revised_resume = kit.resume_optimization = None

    resume = db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id, Resume.is_active == True)  # noqa: E712
        .order_by(Resume.created_at.desc())
    )
    if resume is None:
        kit.status = "error"
        kit.error_detail = "No active resume uploaded — upload one before generating a kit."
        db.commit()
        return kit
    kit.resume_id = resume.id

    if client is None:
        client, _ = llm.clients_for_user(db, user)
    kit.model = client.model

    company_name = position.company.name if position.company else "the company"
    interest = match.interest if (match := _best_match(db, user, position)) else None

    try:
        # 1) Structured analysis: what the role wants + its open questions.
        raw = client.chat_json(ANALYSIS_SYSTEM, _analysis_prompt(resume, interest, position, company_name),
                               ANALYSIS_SCHEMA)
        analysis = _parse_analysis(raw)
        kit.looking_for = json.dumps(analysis.looking_for)
        kit.open_questions = json.dumps([q.model_dump() for q in analysis.open_questions])
        db.commit()

        looking_for = analysis.looking_for
        # 2) Cover letter (free text).
        kit.cover_letter = client.chat_text(
            COVER_LETTER_SYSTEM, _writing_prompt(resume, position, company_name, looking_for)
        ).strip()
        db.commit()

        # 3) Tailored resume (structured: Markdown + an optimization note).
        resume_raw = client.chat_json(
            RESUME_SYSTEM, _writing_prompt(resume, position, company_name, looking_for), RESUME_SCHEMA
        )
        tailored = _parse_resume(resume_raw)
        kit.revised_resume = tailored.resume_markdown.strip() or None
        kit.resume_optimization = tailored.optimization_summary.strip() or None

        kit.status = "ok"
        db.commit()
    except OllamaError as exc:
        # Covers OllamaBudgetError too — its message already names the quota issue.
        log.warning("kit generation failed for user %s position %s: %s", user.id, position.id, exc)
        kit.status = "error"
        kit.error_detail = str(exc)[:1000]
        db.commit()
    return kit


def _parse_analysis(raw: dict) -> KitAnalysis:
    """Validate the analysis payload; fall back to empty lists on drift rather than
    failing the whole kit (the cover letter + resume are still worth generating)."""
    try:
        return KitAnalysis.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError:
        log.warning("kit analysis returned an unexpected payload; continuing with empty analysis")
        return KitAnalysis()


def _parse_resume(raw: dict) -> TailoredResume:
    """Validate the tailored-resume payload; fall back to empty fields on drift."""
    try:
        return TailoredResume.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError:
        log.warning("kit resume returned an unexpected payload; leaving the resume empty")
        return TailoredResume()
