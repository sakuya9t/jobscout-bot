"""Database engine and session management."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - exercised at runtime
        """WAL lets readers and one writer proceed concurrently; busy_timeout
        makes writers wait for a held lock instead of failing instantly. Without
        these, the daily scan's writes collide with dashboard/Telegram writes as
        'database is locked'."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        # SQLite ignores FK constraints unless this is set per-connection. ORM
        # cascades cover today's deletes, but this stops any raw-SQL delete from
        # silently orphaning child rows.
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def seed_presets(db: Session) -> None:
    """Ensure a global Company row exists for each built-in preset (idempotent),
    keyed by ``preset_key``. These are the shared, crawled-once companies."""
    from .company_presets import PRESETS
    from .models import Company

    have = {c.preset_key for c in db.scalars(select(Company).where(Company.preset_key.is_not(None)))}
    for p in PRESETS:
        if p.key in have:
            continue
        db.add(Company(
            preset_key=p.key, user_id=None, name=p.name, careers_url=p.careers_url,
            ats_type=p.ats_type, ats_token=p.ats_token, location_hint=p.location_hint,
        ))
    db.flush()


def _migrate_user_presets(db: Session) -> None:
    """One-time: a legacy per-user company that is really a preset becomes a
    subscription to the shared preset company, and the duplicate per-user row is
    deleted (its positions/match-results cascade; preset matches re-score next
    run). Idempotent — after the first pass no user company matches a preset."""
    from .company_presets import PRESETS
    from .models import Company, Subscription

    by_token = {(p.ats_type, p.ats_token): p.key for p in PRESETS if p.ats_token}
    by_name = {p.name.lower(): p.key for p in PRESETS}
    global_by_key = {
        c.preset_key: c for c in db.scalars(select(Company).where(Company.preset_key.is_not(None)))
    }
    legacy = db.scalars(
        select(Company).where(Company.user_id.is_not(None), Company.preset_key.is_(None))
    )
    for c in legacy:
        key = by_token.get((c.ats_type, c.ats_token)) or by_name.get((c.name or "").lower())
        shared = global_by_key.get(key) if key else None
        if shared is None:
            continue  # genuine custom company — leave as-is
        if not db.scalar(select(Subscription).where(
            Subscription.user_id == c.user_id, Subscription.company_id == shared.id
        )):
            db.add(Subscription(user_id=c.user_id, company_id=shared.id))
        db.delete(c)  # cascades its now-duplicate positions + match results
    db.flush()


def init_db() -> None:
    """Create all tables, seed preset companies, and run the one-time preset
    migration. Import models first so they register with the metadata."""
    from . import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)
    with session_scope() as db:
        seed_presets(db)
        _migrate_user_presets(db)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for use outside request handlers (scheduler, MCP, CLI)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
