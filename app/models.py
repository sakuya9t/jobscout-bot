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
    # that bot. ``telegram_bot_token`` is sensitive — treat the DB as secret, and
    # never echo it back over the API (see routers/telegram_config.py).
    telegram_bot_token: Mapped[str | None] = mapped_column(String(128))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_link_code: Mapped[str | None] = mapped_column(String(32), index=True)

    resumes: Mapped[list[Resume]] = relationship(back_populates="user", cascade="all, delete-orphan")
    # Custom (user-owned) companies only; preset companies are global (user_id NULL)
    # and followed via ``subscriptions``.
    companies: Mapped[list[Company]] = relationship(back_populates="user", cascade="all, delete-orphan")
    subscriptions: Mapped[list[Subscription]] = relationship(
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
    llm_config: Mapped[LlmConfig | None] = relationship(
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

    company: Mapped[Company] = relationship(back_populates="positions")
    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
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
    # User-supplied API key for the provider. Stored as-is (like the Telegram
    # token); treat the DB as sensitive. NULL falls back to the global settings key.
    api_key: Mapped[str | None] = mapped_column(String(512))
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
