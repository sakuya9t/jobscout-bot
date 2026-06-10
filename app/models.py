"""SQLAlchemy ORM models. Multi-user from the ground up: every user-owned
row carries a ``user_id`` and queries are always scoped to the current user."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
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

    # Telegram linking: code is shown on the dashboard; user DMs the bot to bind chat_id.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_link_code: Mapped[str | None] = mapped_column(String(32), index=True)

    resumes: Mapped[list[Resume]] = relationship(back_populates="user", cascade="all, delete-orphan")
    companies: Mapped[list[Company]] = relationship(back_populates="user", cascade="all, delete-orphan")
    interests: Mapped[list[Interest]] = relationship(back_populates="user", cascade="all, delete-orphan")
    matches: Mapped[list[MatchResult]] = relationship(back_populates="user", cascade="all, delete-orphan")


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
    """A company whose career page we scan for new postings (per user)."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_company_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
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

    user: Mapped[User] = relationship(back_populates="companies")
    positions: Mapped[list[Position]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


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
