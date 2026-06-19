"""SQLAlchemy ORM models. Multi-user from the ground up: every user-owned
row carries a ``user_id`` and queries are always scoped to the current user."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .crypto import EncryptedString
from .timeutil import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Telegram is per-user: the user supplies their own bot token (created via
    # @BotFather) and links the chat reports go to by DMing ``/start <code>`` to
    # that bot. ``telegram_bot_token`` is a secret: encrypted at rest via
    # ``EncryptedString`` and never echoed back over the API (see routers/telegram_config.py).
    telegram_bot_token: Mapped[str | None] = mapped_column(EncryptedString)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_link_code: Mapped[str | None] = mapped_column(String(32), index=True)

    resumes: Mapped[list[Resume]] = relationship(back_populates="user", cascade="all, delete-orphan")
    # Custom (user-owned) companies only; preset companies are global (user_id NULL)
    # and followed via ``subscriptions``.
    companies: Mapped[list[Company]] = relationship(back_populates="user", cascade="all, delete-orphan")
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # Per-user credentials for the application portals of preset companies that
    # require an account to apply (see CompanyAccount). Encrypted at rest.
    company_accounts: Mapped[list[CompanyAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    interests: Mapped[list[Interest]] = relationship(back_populates="user", cascade="all, delete-orphan")
    matches: Mapped[list[MatchResult]] = relationship(back_populates="user", cascade="all, delete-orphan")
    job_list_snapshots: Mapped[list[JobListSnapshot]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    application_kits: Mapped[list[ApplicationKit]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    llm_config: Mapped[LlmConfig | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    # User-level applicant profile (the autofill data source for phase-2
    # auto-apply): one scalar record plus repeating education/experience rows.
    profile: Mapped[ApplicantProfile | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    profile_education: Mapped[list[ProfileEducation]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profile_experience: Mapped[list[ProfileExperience]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # Operational queue row driving the periodic background scoring drain (one per
    # user; see services/scoring_queue.py). Cascade-deleted with the user.
    scoring_job: Mapped[ScoringJob | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class JobListSnapshot(Base):
    """A saved dashboard job-list version captured after a scan finishes."""

    __tablename__ = "job_list_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    new_positions: Mapped[int] = mapped_column(Integer, default=0)
    scored: Mapped[int] = mapped_column(Integer, default=0)
    filtered: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text)
    items_json: Mapped[str] = mapped_column(Text, default="[]")

    user: Mapped[User] = relationship(back_populates="job_list_snapshots")


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="resumes")
    # Deleting a resume removes the matches scored against it (and its on-disk
    # file, handled in the router) so reports never reference a missing resume.
    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )
    # Application kits are tailored to a specific resume, so replacing/deleting the
    # resume drops them too (they'd be stale) — same lifecycle as ``matches``.
    application_kits: Mapped[list[ApplicationKit]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )


class Company(Base):
    """A company whose career page we crawl for postings.

    Two kinds share this table:
    - **Preset** (global): ``preset_key`` set, ``user_id`` NULL — one shared row per
      ``company_presets.PRESETS`` entry, crawled once by ``crawl_presets`` and
      matched against any user's resume. Users follow them via ``Subscription``.
    - **Custom** (per-user): ``user_id`` set, ``preset_key`` NULL — owned by one
      user and still crawled on that user's scan."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_company_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL for global preset companies; set for user-owned custom companies.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    # Stable preset slug (company_presets) for global rows; NULL for custom ones.
    preset_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    careers_url: Mapped[str | None] = mapped_column(String(1024))
    # ATS-first scraping: "greenhouse" | "lever" | "ashby" | "html" | "auto"
    ats_type: Mapped[str] = mapped_column(String(32), default="auto")
    # Board token / org slug for the ATS API (e.g. greenhouse board token).
    ats_token: Mapped[str | None] = mapped_column(String(255))
    location_hint: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User | None] = relationship(back_populates="companies")
    positions: Mapped[list[Position]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    # Users' saved application-portal accounts for this company (preset companies
    # only; cascades so a removed company never leaves orphaned credentials).
    accounts: Mapped[list[CompanyAccount]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    @property
    def is_preset(self) -> bool:
        return self.preset_key is not None


class Subscription(Base):
    """A user following a global (preset) company so its shared jobs are matched
    against their resume. Custom companies are owned via ``Company.user_id`` and
    need no subscription."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "company_id", name="uq_subscription_user_company"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="subscriptions")
    company: Mapped[Company] = relationship()


class CompanyAccount(Base):
    """A user's login for the application portal of a (preset) company that
    requires registering an account to apply — e.g. Google Careers or NVIDIA's
    Workday. One row per (user, company); only created for companies whose preset
    has ``requires_account`` set. The username and password are encrypted at rest
    (see ``app/crypto.py``) — unlike the Telegram token / LLM key, which are stored
    plaintext — because these are credentials to a third-party site. The portal URL
    and notes are non-secret and stored as-is. Feeds the phase-2 auto-apply."""

    __tablename__ = "company_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "company_id", name="uq_company_account_user_company"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    # Encrypted (Fernet) — never store/echo the plaintext. The username is the
    # identifier the user logs in with (often an email); presence of a username is
    # what "account attached" means in the UI.
    username_enc: Mapped[str | None] = mapped_column(Text)
    password_enc: Mapped[str | None] = mapped_column(Text)
    # Non-secret: where the user registers/signs in (defaults from the preset).
    portal_url: Mapped[str | None] = mapped_column(String(1024))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="company_accounts")
    company: Mapped[Company] = relationship(back_populates="accounts")


class Interest(Base):
    """A user's requirement profile: what kind of roles they want and where.
    Used both as a cheap pre-filter and to steer the LLM match decision."""

    __tablename__ = "interests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(255))  # e.g. "Senior Backend (EU remote)"
    # Comma-separated keyword lists kept as text for simplicity / portability.
    title_keywords: Mapped[str | None] = mapped_column(Text)  # "backend, platform, infra"
    locations: Mapped[str | None] = mapped_column(Text)  # "remote, berlin, eu"
    seniority: Mapped[str | None] = mapped_column(String(128))  # "senior, staff"
    employment_type: Mapped[str | None] = mapped_column(String(128))  # "full-time"
    exclude_keywords: Mapped[str | None] = mapped_column(Text)  # "manager, sales"
    notes: Mapped[str | None] = mapped_column(Text)  # free-text requirements for the LLM
    min_score: Mapped[int] = mapped_column(Integer, default=70)  # report threshold
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="interests")
    # Deleting an interest removes the matches scored against it (mirrors the
    # Resume semantics). Without this cascade, PRAGMA foreign_keys=ON turns
    # every interest delete into an IntegrityError once it has match rows.
    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="interest", cascade="all, delete-orphan"
    )


class Position(Base):
    """A scraped job posting belonging to a company. ``external_id`` + company
    is the natural dedup key so we only treat genuinely-new postings as new."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_position_company_extid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))  # ATS id or url hash
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512))
    department: Mapped[str | None] = mapped_column(String(255))
    employment_type: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(1024))
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Set when a crawl no longer finds this posting on the company's board (full-board
    # ATS only — Greenhouse/Lever/Ashby; partial sources can't conclude removal). NULL =
    # currently listed. Removed positions are hidden everywhere except an application the
    # user already made; a later reappearance on the board clears this back to NULL.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    company: Mapped[Company] = relationship(back_populates="positions")
    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )
    application_kits: Mapped[list[ApplicationKit]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )


class MatchResult(Base):
    """The LLM's evaluation of one position against one user's resume + interest.
    Persisted so reports are reproducible and we don't re-score unchanged rows."""

    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "position_id", "resume_id", "interest_id", name="uq_match_unique"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"))
    interest_id: Mapped[int | None] = mapped_column(ForeignKey("interests.id"))

    passed_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    match_score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100 resume<->role fit
    win_probability: Mapped[int] = mapped_column(Integer, default=0)  # 0-100 chance to land it
    reasoning: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[str | None] = mapped_column(Text)  # JSON-encoded list
    gaps: Mapped[str | None] = mapped_column(Text)  # JSON-encoded list
    model: Mapped[str | None] = mapped_column(String(128))
    # Retry counter for a failed (position, interest) pair: how many times scoring has
    # been tried and failed (only set on error-markers, model == matcher.ERROR_MODEL).
    # The pair is re-scored while attempts < settings.score_max_attempts, then the
    # marker is terminal. 0 for real results / filter-rejects.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="matches")
    position: Mapped[Position] = relationship(back_populates="matches")
    resume: Mapped[Resume | None] = relationship(back_populates="matches")
    interest: Mapped[Interest | None] = relationship(back_populates="matches")


class LlmConfig(Base):
    """A user's chosen LLM provider + credentials/models. One row per user (its
    absence means "use the deployment-wide defaults from settings"). ``provider``
    is a key into ``llm_providers.PROVIDERS`` (which supplies the base URL); the
    user brings their own ``api_key`` and picks the main (scoring) and light
    (relevance-filter) models."""

    __tablename__ = "llm_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="ollama_cloud")
    # User-supplied API key for the provider. A secret: encrypted at rest via
    # ``EncryptedString`` (like the Telegram token). NULL falls back to the global
    # settings key.
    api_key: Mapped[str | None] = mapped_column(EncryptedString)
    main_model: Mapped[str | None] = mapped_column(String(128))  # scoring ("good") model
    light_model: Mapped[str | None] = mapped_column(String(128))  # cheap relevance filter
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="llm_config")


class Application(Base):
    """A user's application status for one position. Today it's set manually from
    the dashboard ("Mark applied"); the phase 2/3 auto-apply will create and
    advance these same rows — hence the ``status``/``source`` fields rather than a
    bare boolean. One row per (user, position); its absence means "not applied"."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("user_id", "position_id", name="uq_application_user_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    # Room to grow for auto-apply: e.g. applied | pending | auto_applied | failed.
    status: Mapped[str] = mapped_column(String(32), default="applied")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | auto
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="applications")
    position: Mapped[Position] = relationship(back_populates="applications")


class LlmLog(Base):
    """One Ollama request/response exchange, persisted for auditing/debugging.

    Keeps the full prompt + completion out of stdout (where they were dumped per
    call) and in a queryable table instead. Rows are written off the hot path by a
    background writer (``services/llm_log.py``) so logging never blocks scoring or
    contends with the matcher's open write transaction. Not tied to a user/run:
    it's a low-level wire log, pruned by age/volume rather than cascaded."""

    __tablename__ = "llm_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(16), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(512))
    temperature: Mapped[float | None] = mapped_column(Float)
    response_format: Mapped[str | None] = mapped_column(String(16))  # "json-schema" | "text"
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    request_messages: Mapped[str | None] = mapped_column(Text)  # JSON: [{role, content}, …]
    status: Mapped[str] = mapped_column(String(16), default="ok")  # "ok" | "error"
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    done_reason: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    eval_tokens: Mapped[int | None] = mapped_column(Integer)
    response_content: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)


class InviteCode(Base):
    """A registration invitation. The plaintext code is shown once at mint time and
    never stored: we keep only ``code_hash`` = HMAC-SHA256(root_secret, code), so a DB
    leak yields irreversible hashes (no usable codes, and no way to forge new ones) and
    the root key never touches a row. See ``app/invites.py`` for the derivation and the
    atomic redeem that enforces ``max_uses``/``expires_at``. Not user-owned — a code is
    shared and consumed by whoever registers with it, so it carries no ``user_id``."""

    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    # Null = never expires. Stored as naive UTC like every other timestamp (timeutil).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApplicationKit(Base):
    """LLM-generated application materials for one (user, position): a summary of
    what the role is looking for, detected open application questions with advice +
    a draft answer, a cover letter, and a tailored resume.

    Generated on demand from the position detail page (an explicit "Generate"
    click), in the background (see ``services/kit_worker.py``), never during the
    bulk scan — which would multiply cost across every posting. The row is cached
    and shown as-is on every later page open; it is re-generated only when the user
    explicitly asks (a fresh POST), so reads never trigger LLM calls. One row per
    (user, position)."""

    __tablename__ = "application_kits"
    __table_args__ = (UniqueConstraint("user_id", "position_id", name="uq_kit_user_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    # Which resume the kit was tailored against (nullable so a kit survives even if
    # we couldn't resolve a resume; in practice generation requires an active one).
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"))

    # "generating" while the background worker runs, "ok" once complete, "error" on
    # a terminal LLM failure (with ``error_detail`` set). The detail page polls this.
    status: Mapped[str] = mapped_column(String(16), default="generating")
    looking_for: Mapped[str | None] = mapped_column(Text)  # JSON list[str]
    # JSON list[{question, advice, suggested_answer}] — detected open application
    # questions plus how to approach them and a draft answer.
    open_questions: Mapped[str | None] = mapped_column(Text)
    cover_letter: Mapped[str | None] = mapped_column(Text)
    # The tailored resume, as copy-paste-ready Markdown.
    revised_resume: Mapped[str | None] = mapped_column(Text)
    # A short (3-4 sentence) note on what was optimized for this position, shown
    # below the resume block (not part of the copyable Markdown).
    resume_optimization: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="application_kits")
    position: Mapped[Position] = relationship(back_populates="application_kits")
    resume: Mapped[Resume | None] = relationship(back_populates="application_kits")


class ApplicantProfile(Base):
    """The user-level information a job application asks for, kept once so it can
    autofill the bulk of an application form (à la the Simplify extension) and feed
    the phase-2 auto-apply. One row per user; repeating education/work history live
    in their own tables. Free-text/nullable throughout — applications vary wildly,
    so we store what the user gives and never force a shape."""

    __tablename__ = "applicant_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)

    # Identity + contact
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    preferred_name: Mapped[str | None] = mapped_column(String(128))
    pronouns: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    state_region: Mapped[str | None] = mapped_column(String(128))
    postal_code: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(128))

    # Links
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    github_url: Mapped[str | None] = mapped_column(String(512))
    portfolio_url: Mapped[str | None] = mapped_column(String(512))
    other_url: Mapped[str | None] = mapped_column(String(512))

    # Work authorization. The booleans are nullable on purpose: NULL = "not
    # answered" (distinct from an explicit no), which application forms care about.
    work_authorization: Mapped[str | None] = mapped_column(String(255))  # e.g. "US citizen", "H-1B"
    authorized_to_work: Mapped[bool | None] = mapped_column(Boolean)
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean)
    open_to_relocation: Mapped[bool | None] = mapped_column(Boolean)

    # Job preferences
    desired_salary: Mapped[str | None] = mapped_column(String(64))
    salary_currency: Mapped[str | None] = mapped_column(String(16))
    remote_preference: Mapped[str | None] = mapped_column(String(32))  # remote | hybrid | onsite | any
    preferred_locations: Mapped[str | None] = mapped_column(Text)
    earliest_start_date: Mapped[str | None] = mapped_column(String(64))
    notice_period: Mapped[str | None] = mapped_column(String(64))

    # Voluntary self-identification (EEO). Sensitive and optional — treat the DB as
    # secret (as with tokens/keys). NULL throughout when the user declines.
    gender: Mapped[str | None] = mapped_column(String(64))
    race_ethnicity: Mapped[str | None] = mapped_column(String(128))
    hispanic_latino: Mapped[str | None] = mapped_column(String(32))
    # Standard self-ID phrasings are long ("I identify as one or more of the
    # classifications of a protected veteran"), so these are wide.
    veteran_status: Mapped[str | None] = mapped_column(String(128))
    disability_status: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="profile")


class ProfileEducation(Base):
    """One education entry on a user's applicant profile. Dates are free text
    ('Jun 2020 – Present', '2019'), since that's how résumés state them and how
    application forms accept them. Replaced wholesale when the profile is saved."""

    __tablename__ = "profile_education"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    school: Mapped[str | None] = mapped_column(String(255))
    degree: Mapped[str | None] = mapped_column(String(255))
    field_of_study: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[str | None] = mapped_column(String(64))
    end_date: Mapped[str | None] = mapped_column(String(64))
    gpa: Mapped[str | None] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="profile_education")


class ProfileExperience(Base):
    """One work-experience entry on a user's applicant profile. Replaced wholesale
    when the profile is saved (mirrors ProfileEducation)."""

    __tablename__ = "profile_experience"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[str | None] = mapped_column(String(64))
    end_date: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="profile_experience")


class ScoringJob(Base):
    """A user's slot in the periodic background-scoring queue. One row per user
    (``uq_scoring_job_user``), so enqueue is an idempotent upsert and a user can
    never have two queued jobs. The row is the durable, cross-process claim record
    the GitHub Actions scoring cron drains (see services/scoring_queue.py): a worker
    flips ``pending`` -> ``running`` atomically via ``SELECT … FOR UPDATE SKIP
    LOCKED`` (Postgres), drains the user's whole backlog, then marks it ``done`` /
    re-arms ``pending`` if work remains / ``error`` on failure. The backlog itself
    lives in MatchResult gaps (``matcher.count_pending``); this table only schedules
    and serializes the draining so concurrent DB connections stay bounded."""

    __tablename__ = "scoring_jobs"
    __table_args__ = (UniqueConstraint("user_id", name="uq_scoring_job_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # pending (work to do) | running (claimed by a worker) | done | error.
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # Lower drains first; reserved for a future on-demand jump-ahead. Tie-broken by
    # enqueued_at so the queue is FIFO within a priority.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # When a worker claimed it (used to reclaim rows stuck running after a crash).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="scoring_job")


class ScoringEvent(Base):
    """One scoring-queue / worker lifecycle event, persisted for tracing instead of
    dumped to stdout (which buried the signal in per-batch noise). Every state change a
    ``ScoringJob`` goes through — enqueue, claim, finalize, drain summary, error, and the
    reconcile self-heal actions (reclaim/park/requeue) — plus worker-pool spawn/exit,
    writes a row here. That makes a flaky drain reconstructable after the fact: filter by
    ``user_id`` and read the ordered ``event``/``state_from``->``state_to`` trail to see
    exactly where scoring stopped and why.

    Written off the hot path by a background writer (``services/scoring_log.py``) so
    tracing never blocks a claim/drain or contends with the matcher's open write
    transaction — same design as ``LlmLog``. Not FK'd to users: it's a low-level audit
    log that should survive a user deletion and never fail a write on a race; prune by
    age/volume rather than cascade."""

    __tablename__ = "scoring_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # The user whose job this event concerns (None for pool-wide events like a worker
    # spawn/exit). Plain int, not a FK — see the class docstring.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # enqueue | claim | finalize | drain | error | done | reconcile | worker
    event: Mapped[str] = mapped_column(String(24), index=True)
    state_from: Mapped[str | None] = mapped_column(String(16))
    state_to: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int | None] = mapped_column(Integer)
    # The thread that emitted it (evaluator*/scoring-*/MainThread) — lets you follow one
    # worker's claim -> drain -> finalize trail through interleaved concurrent drains.
    worker: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
