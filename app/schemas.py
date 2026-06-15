"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── LLM structured-output contract ───────────────────────────────────────────
class MatchVerdict(BaseModel):
    """The LLM's scoring of one (resume, role) pair. This is the single source of
    truth for both the Ollama ``format`` (``model_json_schema()``) and parsing
    (``model_validate_json``), so a drifting/incomplete model response becomes a
    real validation error instead of being silently coerced to zeros."""

    matches_requirements: bool
    match_score: int = Field(ge=0, le=100)  # resume <-> role fit
    win_probability: int = Field(ge=0, le=100)  # realistic chance of an offer
    reasoning: str
    # Supplementary lists: some capable models honor the schema's core fields but
    # omit these arrays, so default them empty rather than failing the whole score.
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


# ── Auth ─────────────────────────────────────────────────────────────────────
class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(_ORM):
    id: int
    email: str
    telegram_chat_id: str | None = None
    telegram_link_code: str | None = None


# ── Resume ───────────────────────────────────────────────────────────────────
class ResumeOut(_ORM):
    id: int
    filename: str
    is_active: bool
    created_at: datetime


# ── Company ──────────────────────────────────────────────────────────────────
class CompanyPresetOut(BaseModel):
    """A built-in popular-company option the dashboard offers as a one-click fill."""

    key: str
    name: str
    careers_url: str
    ats_type: str
    ats_token: str | None = None
    location_hint: str | None = None


class CompanyIn(BaseModel):
    name: str
    careers_url: str | None = None
    ats_type: str = "auto"
    ats_token: str | None = None
    location_hint: str | None = None
    # When set (or when the payload matches a preset), "adding" subscribes the user
    # to the shared global preset company instead of creating a per-user duplicate.
    preset_key: str | None = None


class CompanyUpdate(BaseModel):
    name: str | None = None
    careers_url: str | None = None
    ats_type: str | None = None
    ats_token: str | None = None
    location_hint: str | None = None
    is_active: bool | None = None


class CompanyOut(_ORM):
    id: int
    name: str
    careers_url: str | None
    ats_type: str
    ats_token: str | None
    location_hint: str | None
    is_active: bool
    last_scraped_at: datetime | None
    # True for a global preset company the user is subscribed to (vs their own
    # custom company). Deleting it unsubscribes rather than removing the catalog row.
    is_preset: bool = False


# ── Interest ─────────────────────────────────────────────────────────────────
class InterestIn(BaseModel):
    label: str
    title_keywords: str | None = None
    locations: str | None = None
    seniority: str | None = None
    employment_type: str | None = None
    exclude_keywords: str | None = None
    notes: str | None = None
    min_score: int = 70


class InterestUpdate(InterestIn):
    label: str | None = None
    min_score: int | None = None
    is_active: bool | None = None


class InterestOut(_ORM):
    id: int
    label: str
    title_keywords: str | None
    locations: str | None
    seniority: str | None
    employment_type: str | None
    exclude_keywords: str | None
    notes: str | None
    min_score: int
    is_active: bool


# ── Position / Match / Report ────────────────────────────────────────────────
class PositionOut(_ORM):
    id: int
    company_id: int
    title: str
    location: str | None
    department: str | None
    url: str | None
    first_seen_at: datetime


class MatchOut(BaseModel):
    position_id: int
    company: str
    title: str
    location: str | None
    url: str | None
    match_score: int
    win_probability: int
    reasoning: str | None
    strengths: list[str]
    gaps: list[str]
    # True when this row is below the user's score threshold but shown anyway to
    # keep the dashboard non-empty (the daily Telegram report omits these).
    below_threshold: bool = False
    # True for a non-matching job (filter-rejected or keyword-excluded) shown only
    # in the dashboard's "all jobs" view — score fields are not meaningful for it.
    non_matching: bool = False
    # Effective "listed" date (ISO, naive UTC): the ATS post date when known, else
    # when our crawler first saw it. Drives the dashboard's post-date filter/label.
    listed_at: str | None = None
    # Whether the current user has marked this position applied (the "Mark applied"
    # toggle). Overlaid live at render time, not stored in saved snapshots.
    applied: bool = False


class ApplicationOut(_ORM):
    position_id: int
    status: str
    applied_at: datetime


class JobListRunOut(BaseModel):
    id: int
    created_at: datetime
    new_positions: int
    scored: int
    filtered: int
    total: int
    has_errors: bool = False


class JobListOut(BaseModel):
    id: int | None = None
    created_at: datetime | None = None
    new_positions: int = 0
    scored: int = 0
    filtered: int = 0
    errors: list[str] = Field(default_factory=list)
    total: int = 0
    # Positions still queued for background evaluation (0 = fully evaluated).
    pending: int = 0
    items: list[MatchOut] = Field(default_factory=list)


class EvaluationStatus(BaseModel):
    """Lightweight backlog poll for the dashboard's 'XX positions unevaluated'."""

    pending: int = 0
    in_progress: bool = False


class RunSummary(BaseModel):
    new_positions: int
    scored: int
    top_matches: list[MatchOut]
    errors: list[str] = []
    # Positions handed to the background evaluator and not yet scored.
    pending: int = 0
