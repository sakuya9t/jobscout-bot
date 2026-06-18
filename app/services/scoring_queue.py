"""Postgres-backed claim queue for the periodic scoring drain.

The expensive per-user matching step is deferred off the daily scrape and run on
its own schedule by the `jobscout run-scoring` cron (see
.github/workflows/scoring.yml). This module is the queue that cron drains.

Why a DB queue instead of a broker: the durable backlog already exists as DB state
(``matcher.count_pending`` — the missing-MatchResult set), and the deploy has no
host for RabbitMQ/Redis. A ``scoring_jobs`` row per user, claimed with ``SELECT …
FOR UPDATE SKIP LOCKED``, gives an atomic, cross-process-safe claim with zero new
infra. The point of the queue is throttling: a bounded worker pool claims a few
users at a time, so concurrent Supabase connections stay constant in the number of
users (which otherwise exhausts the pooler — see app/db.py:NullPool).

Lifecycle of a row: ``pending`` (work to do) -> ``running`` (claimed) -> ``done``
(drained) / ``error`` (failed) / back to ``pending`` (re-armed because more work
appeared, or the worker couldn't start). ``reconcile`` (re)enqueues every user with
a non-empty backlog and reclaims rows stranded ``running`` by a crashed worker.

Dialect note: ``with_for_update(skip_locked=True)`` is honoured on Postgres and
silently ignored on SQLite (dev/tests). The claim therefore also does a conditional
``UPDATE … WHERE state='pending'`` and checks the row count, which makes the claim
atomic on SQLite too (single-writer, no SKIP LOCKED) — and is a harmless no-op race
guard on Postgres where the row is already locked."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..logging_config import get_logger
from ..models import ScoringJob, User
from ..timeutil import utcnow
from . import matcher

log = get_logger(__name__)


def enqueue(db: Session, user_id: int, *, priority: int = 0) -> ScoringJob:
    """Idempotently put a user on the queue. Inserts a ``pending`` row, or, if one
    already exists and isn't currently ``running``, resets it to ``pending`` and
    bumps ``enqueued_at`` (so a re-enqueue re-drains). A ``running`` row is left
    untouched — its worker owns it. Commits so the row is visible to other workers."""
    job = db.scalar(select(ScoringJob).where(ScoringJob.user_id == user_id))
    if job is None:
        job = ScoringJob(user_id=user_id, state="pending", priority=priority)
        db.add(job)
    elif job.state != "running":
        job.state = "pending"
        job.enqueued_at = utcnow()
        job.priority = priority
        job.last_error = None
        job.finished_at = None
    db.commit()
    return job


def reconcile(db: Session) -> int:
    """Prepare the queue for a drain: reclaim crashed ``running`` rows, then enqueue
    every user that still has a non-empty scoring backlog. Self-healing — the backlog
    is derived from ``matcher.count_pending``, so nothing relies on scattered enqueue
    calls staying in sync. Returns the number of users enqueued."""
    stale_before = utcnow() - timedelta(minutes=max(1, settings.scoring_stale_minutes))
    reclaimed = db.execute(
        update(ScoringJob)
        .where(ScoringJob.state == "running", ScoringJob.claimed_at < stale_before)
        .values(state="pending", claimed_at=None)
    ).rowcount or 0
    if reclaimed:
        log.warning("scoring queue: reclaimed %d stale running job(s)", reclaimed)
    db.commit()

    enqueued = 0
    for user_id in list(db.scalars(select(User.id))):
        user = db.get(User, user_id)
        if user is not None and matcher.count_pending(db, user) > 0:
            enqueue(db, user_id)
            enqueued += 1
    log.info("scoring queue: reconcile enqueued %d user(s)", enqueued)
    return enqueued


def claim_one(db: Session) -> int | None:
    """Atomically claim the next pending user and return its id (None when the queue
    is empty / all remaining rows are locked by peers). Marks the row ``running`` and
    commits before returning, so the claim is durable across the long drain that
    follows and the row lock is released immediately."""
    while True:
        job = db.scalars(
            select(ScoringJob)
            .where(ScoringJob.state == "pending")
            .order_by(ScoringJob.priority, ScoringJob.enqueued_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if job is None:
            db.rollback()
            return None
        # Conditional claim: on SQLite (no SKIP LOCKED) this is what makes the claim
        # atomic; on Postgres the row is already locked so rowcount is always 1.
        claimed = db.execute(
            update(ScoringJob)
            .where(ScoringJob.id == job.id, ScoringJob.state == "pending")
            .values(state="running", claimed_at=utcnow(), attempts=ScoringJob.attempts + 1)
        ).rowcount
        db.commit()
        if claimed:
            return job.user_id
        # Lost the race to a peer worker — try the next pending row.


def finalize(db: Session, user: User, res) -> None:
    """Settle a user's job after a drain, based on the RunResult ``res``:
    - the drain didn't run (another in-process drain holds the score lock): leave
      ``pending`` so it's retried;
    - budget/quota exhausted: mark ``done`` now — the next cron's reconcile re-enqueues
      it once quota is back (never hot-loop a dead quota inside one run);
    - otherwise re-arm ``pending`` if work appeared mid-drain, else ``done``."""
    if not getattr(res, "did_run", True):
        _set_state(db, user.id, "pending")
        return
    if getattr(res, "budget_exhausted", False):
        _set_state(db, user.id, "done", finished=True)
        return
    state = "pending" if matcher.count_pending(db, user) > 0 else "done"
    _set_state(db, user.id, state, finished=state == "done")


def mark_error(db: Session, user_id: int, message: str) -> None:
    """Mark a user's job ``error`` after the drain raised. ``reconcile`` re-enqueues
    it on the next run if the backlog is still non-empty, so this isn't terminal."""
    db.execute(
        update(ScoringJob)
        .where(ScoringJob.user_id == user_id)
        .values(state="error", last_error=(message or "")[:1000], finished_at=utcnow())
    )
    db.commit()


def mark_done(db: Session, user_id: int) -> None:
    """Mark a user's job ``done`` (e.g. the user vanished after being claimed)."""
    _set_state(db, user_id, "done", finished=True)


def _set_state(db: Session, user_id: int, state: str, *, finished: bool = False) -> None:
    values: dict = {"state": state}
    if finished:
        values["finished_at"] = utcnow()
    db.execute(update(ScoringJob).where(ScoringJob.user_id == user_id).values(**values))
    db.commit()


def counts_by_state(db: Session) -> dict[str, int]:
    """Snapshot of how many jobs sit in each state — for the CLI/cron summary."""
    from sqlalchemy import func

    rows = db.execute(
        select(ScoringJob.state, func.count()).group_by(ScoringJob.state)
    ).all()
    return {state: n for state, n in rows}


def has_pending(db: Session) -> bool:
    """Whether any job is waiting to be claimed — lets the web pool skip spawning
    workers when there's nothing to drain (and avoids a thundering herd of no-op
    workers on every boot)."""
    return db.scalar(
        select(ScoringJob.id).where(ScoringJob.state == "pending").limit(1)
    ) is not None


def active_user_ids(db: Session) -> set[int]:
    """Users with a job still queued or being scored (state pending/running). Powers
    the dashboard's ``in_progress`` flag — and, unlike the old in-memory set, it's
    correct across processes (a web worker and the cron see the same state)."""
    return set(
        db.scalars(
            select(ScoringJob.user_id).where(ScoringJob.state.in_(("pending", "running")))
        )
    )
