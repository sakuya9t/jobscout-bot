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
    strengths: list[str]
    gaps: list[str]


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
class CompanyIn(BaseModel):
    name: str
    careers_url: str | None = None
    ats_type: str = "auto"
    ats_token: str | None = None
    location_hint: str | None = None


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


class RunSummary(BaseModel):
    new_positions: int
    scored: int
    top_matches: list[MatchOut]
    errors: list[str] = []
