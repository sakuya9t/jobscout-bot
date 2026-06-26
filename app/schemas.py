"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── LLM structured-output contract ───────────────────────────────────────────
class MatchSubScore(BaseModel):
    """One aspect of a match score. Labels are intentionally human-readable because
    they are rendered directly in the job detail score breakdown."""

    label: str
    score: int = Field(ge=0, le=100)
    rationale: str = ""


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
    # Optional detailed rubric for the job-detail breakdown. Defaults empty so older
    # tests/models that only return the original top-level score remain valid.
    score_breakdown: list[MatchSubScore] = Field(default_factory=list)
    # Pay range read from the posting itself, when it explicitly states one (the LLM
    # reads the full description anyway, so this is ~free and catches prose the regex
    # can't). All optional + leniently coerced below so a malformed value degrades to
    # null instead of failing the whole score. Persisted onto the Position, not here.
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None  # ISO-ish code, e.g. "USD"
    salary_period: str | None = None    # "year" | "hour" | "month" | "week"

    @field_validator("salary_min", "salary_max", mode="before")
    @classmethod
    def _coerce_amount(cls, v):
        """Models sometimes emit "$120,000" / "120k" / "120000.0" instead of an int.
        Coerce best-effort; on anything unparseable return None (NEVER raise, or one
        stray salary token would invalidate the whole posting in the batch)."""
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v) if v > 0 else None
        if isinstance(v, str):
            s = v.strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
            mult = 1000 if s.endswith("k") else 1
            s = s[:-1].strip() if s.endswith("k") else s
            try:
                n = float(s) * mult
            except ValueError:
                return None
            return int(n) if n > 0 else None
        return None

    @field_validator("salary_currency", "salary_period", mode="before")
    @classmethod
    def _coerce_short_str(cls, v):
        if not isinstance(v, str):
            return None
        return v.strip()[:16] or None


# ── Auth ─────────────────────────────────────────────────────────────────────
def _require_password_complexity(v: str) -> str:
    """The new-password policy shared by registration and change-password: >= 8 chars
    with at least one letter and one digit, which rejects "12345678" / "password"
    without forcing character-class gymnastics on users. Login is deliberately NOT
    held to this (it validates against plain Credentials) so an existing account with
    an older/weaker password still gets a clean 401 on a wrong password, not a 422."""
    if len(v) < 8 or not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
        raise ValueError(
            "Password must be at least 8 characters and include a letter and a number"
        )
    return v


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class RegisterCredentials(Credentials):
    # Redeclared to drop the parent's min_length=6 so the complexity validator below
    # is the single source of truth for the registration password policy (and always
    # produces its own message). Login keeps the laxer Credentials rule on purpose.
    password: str
    # Required at registration only when JOBSCOUT_REQUIRE_INVITE is on (the route, not
    # the schema, enforces presence, so the field is optional here and ignored when
    # invites are disabled). Login uses plain Credentials and never carries a code.
    invite_code: str | None = None

    @field_validator("password")
    @classmethod
    def _password_complexity(cls, v: str) -> str:
        return _require_password_complexity(v)


class PasswordChange(BaseModel):
    """Change-password request for a logged-in user. ``current_password`` is verified
    in the route (not here); ``new_password`` must meet the same complexity rule as
    registration."""

    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _password_complexity(cls, v: str) -> str:
        return _require_password_complexity(v)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(_ORM):
    id: int
    email: str
    telegram_chat_id: str | None = None
    telegram_link_code: str | None = None


# ── LLM provider config ──────────────────────────────────────────────────────
class LlmProviderOut(BaseModel):
    """A selectable provider for the settings dropdown."""

    key: str
    label: str
    base_url: str


class LlmConfigOut(BaseModel):
    """The user's effective LLM config plus the providers to choose from. The API
    key is never returned — only whether one is set."""

    provider: str
    base_url: str
    main_model: str
    light_model: str
    has_api_key: bool
    providers: list[LlmProviderOut]


class LlmModelTest(BaseModel):
    """Result of probing one model during the settings-page "Test"."""

    role: str  # "main" | "light"
    model: str
    ok: bool
    detail: str


class LlmTestResult(BaseModel):
    """Outcome of the settings-page "Test" button — one entry per distinct model
    tested (main + light, deduped when they're the same)."""

    ok: bool  # every probed model succeeded
    detail: str  # one-line summary across the probed models
    results: list[LlmModelTest] = Field(default_factory=list)


class LlmConfigIn(BaseModel):
    provider: str
    main_model: str
    light_model: str
    # Optional: omit/leave blank to keep the saved key; a non-empty value replaces it.
    api_key: str | None = None

    @field_validator("provider", "main_model", "light_model")
    @classmethod
    def _required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


# ── Telegram config ──────────────────────────────────────────────────────────
class TelegramConfigOut(BaseModel):
    """The user's Telegram delivery state. The bot token is never returned — only
    whether one is set — and ``link_code`` is the one-time code to DM the bot."""

    has_token: bool
    linked: bool
    chat_id: str | None = None
    link_code: str | None = None


class TelegramConfigIn(BaseModel):
    # Optional: omit/leave blank to keep the saved token; a non-empty value replaces it.
    bot_token: str | None = None

    @field_validator("bot_token")
    @classmethod
    def _strip_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class TelegramActionResult(BaseModel):
    """Outcome of the settings-page Link/Test actions."""

    ok: bool
    detail: str


# ── Resume ───────────────────────────────────────────────────────────────────
class ResumeOut(_ORM):
    id: int
    filename: str
    is_active: bool
    created_at: datetime


class ResumeContentOut(_ORM):
    """The résumé's extracted plain text, for the in-page preview fallback when the
    browser can't render the original file (e.g. .docx)."""

    filename: str
    content_text: str


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
    # Whether this (preset) company requires a registered application account, and
    # whether the current user has attached one. Both overlaid by the router (not
    # ORM columns) to drive the watch-list tag. Always False for custom companies.
    requires_account: bool = False
    account_attached: bool = False


class CompanyDetailOut(CompanyOut):
    """The company watch-list detail page payload: the company plus the account
    state for the current user. The saved password is never returned (only whether
    one is set); the username is an identifier and is returned so the form prefills."""

    # Where the user registers/signs in to apply (preset default or user override).
    account_portal_url: str | None = None
    account_username: str | None = None
    account_has_password: bool = False
    account_notes: str | None = None


class CompanyAccountIn(BaseModel):
    """Save the current user's application-portal account for a company. The
    username is set as-typed (blank clears it, which also clears "attached"); the
    password follows the keep-blank convention (a non-empty value replaces it, blank
    keeps the stored one) since it's never prefilled into the form."""

    username: str | None = None
    password: str | None = None
    portal_url: str | None = None
    notes: str | None = None

    @field_validator("username", "portal_url", "notes")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None

    @field_validator("password")
    @classmethod
    def _strip_password(cls, v: str | None) -> str | None:
        # Preserve interior spaces but trim edges; blank -> None (= keep existing).
        if v is None:
            return None
        return v.strip() or None


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
    # True once the posting left the company's board. Such a row only appears at all
    # because the user applied to it; the UI badges it "Closed" and locks its actions.
    removed: bool = False
    # Effective "listed" date (ISO, naive UTC): the ATS post date when known, else
    # when our crawler first saw it. Drives the dashboard's post-date filter/label.
    listed_at: str | None = None
    # Preformatted pay range parsed from the posting (e.g. "$120,000–$150,000/yr"),
    # or null when the posting states no pay. Shown as a chip on the job-list row.
    salary_display: str | None = None
    # Whether the current user has marked this position applied (the "Mark applied"
    # toggle). Overlaid live at render time, not stored in saved snapshots.
    applied: bool = False
    # Application-kit status for this position, overlaid live like ``applied``:
    # None (no kit requested) | "generating" | "ok" | "error". Drives the job-list
    # row's kit-status icon.
    kit_status: str | None = None


class ApplicationOut(_ORM):
    position_id: int
    status: str
    applied_at: datetime


# ── Application kit (per-position detail page) ───────────────────────────────
class OpenQuestionOut(BaseModel):
    """One open application question the LLM detected for a posting, with how to
    approach it and a draft answer grounded in the candidate's resume."""

    question: str
    advice: str = ""
    suggested_answer: str = ""


class ApplicationKitOut(BaseModel):
    """The generated (or in-progress) application kit for one (user, position).
    ``status`` is "generating" | "ok" | "error"; the content fields are populated
    as the background worker completes, so a polling client sees partial results."""

    status: str
    looking_for: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestionOut] = Field(default_factory=list)
    cover_letter: str | None = None
    # The tailored resume as copy-paste-ready Markdown, plus a short note on what
    # was optimized for this position (shown below the resume, not part of the copy).
    revised_resume: str | None = None
    resume_optimization: str | None = None
    model: str | None = None
    error_detail: str | None = None
    updated_at: datetime | None = None


class PositionDetailOut(BaseModel):
    """Everything the position detail page shows: the posting, the best stored
    match (score/win/strengths/gaps), live applied status, and the cached kit (or
    null when none has been generated yet)."""

    position_id: int
    company: str
    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    url: str | None = None
    description: str | None = None
    listed_at: str | None = None
    match_score: int | None = None
    win_probability: int | None = None
    reasoning: str | None = None
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    score_breakdown: list[MatchSubScore] = Field(default_factory=list)
    # True when the best stored match for this position did not pass the relevance
    # filter (score fields aren't meaningful) — the page shows a "not a match" pill.
    non_matching: bool = False
    # True once the posting left the company's board (the page shows a "No longer
    # listed" banner and locks apply/kit actions). Only reachable for applied postings.
    removed: bool = False
    applied: bool = False
    # Pay range parsed from the posting (pay-transparency disclosures), when present.
    # ``salary_display`` is the preformatted one-liner; the raw fields are for clients
    # that want to filter/sort. All null when the posting states no pay.
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_display: str | None = None
    kit: ApplicationKitOut | None = None


class RescoreStatusOut(BaseModel):
    """Status of a single-position "Re-evaluate" kicked from the detail page.
    ``in_progress`` is true while the background re-score runs; ``error`` carries a
    one-line reason from the last completed run (null on success). The page polls this
    until ``in_progress`` is false, then reloads the detail to show the new score."""

    in_progress: bool = False
    error: str | None = None


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
    # True when this run's warnings include an LLM-communication failure (bad
    # key/model, unreachable provider, exhausted quota) — drives a dashboard banner.
    llm_error: bool = False
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


# ── Applicant profile (user-level autofill data) ─────────────────────────────
class _BlankStrToNone(BaseModel):
    """Coerce blank/whitespace-only string fields to None (and trim the rest) so the
    profile stores clean nulls instead of empty strings from an untouched form."""

    @model_validator(mode="after")
    def _coerce_blanks(self):
        for name, value in self.__dict__.items():
            if isinstance(value, str):
                setattr(self, name, value.strip() or None)
        return self


class ProfileEducationIn(_BlankStrToNone):
    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    gpa: str | None = None
    location: str | None = None
    description: str | None = None


class ProfileEducationOut(ProfileEducationIn):
    id: int | None = None


class ProfileExperienceIn(_BlankStrToNone):
    company: str | None = None
    title: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    description: str | None = None


class ProfileExperienceOut(ProfileExperienceIn):
    id: int | None = None


class _ProfileScalars(BaseModel):
    """The flat profile fields shared by the In (request) and Out (response) models.
    Every field is optional — an application profile is filled incrementally."""

    # Identity + contact
    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state_region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    # Links
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    other_url: str | None = None
    # Work authorization (booleans tri-state: None = not answered)
    work_authorization: str | None = None
    authorized_to_work: bool | None = None
    requires_sponsorship: bool | None = None
    open_to_relocation: bool | None = None
    # Job preferences
    desired_salary: str | None = None
    salary_currency: str | None = None
    remote_preference: str | None = None
    preferred_locations: str | None = None
    earliest_start_date: str | None = None
    notice_period: str | None = None
    # Voluntary self-identification (EEO)
    gender: str | None = None
    race_ethnicity: str | None = None
    hispanic_latino: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None


class ApplicantProfileIn(_ProfileScalars, _BlankStrToNone):
    education: list[ProfileEducationIn] = Field(default_factory=list)
    experience: list[ProfileExperienceIn] = Field(default_factory=list)


class ApplicantProfileOut(_ProfileScalars):
    education: list[ProfileEducationOut] = Field(default_factory=list)
    experience: list[ProfileExperienceOut] = Field(default_factory=list)
