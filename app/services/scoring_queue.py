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
from . import matcher, scoring_log

log = get_logger(__name__)


def enqueue(
    db: Session, user_id: int, *, priority: int = 0, force: bool = False,
    reset_attempts: bool = False,
) -> ScoringJob:
    """Idempotently put a user on the queue. Inserts a ``pending`` row, or, if one
    already exists and isn't currently ``running``, resets it to ``pending`` and bumps
    ``enqueued_at`` (so a re-enqueue re-drains). Commits so the row is visible to peers.

    A ``running`` row is normally left untouched — its worker owns it — so automatic
    re-enqueues (``reconcile``) don't disturb an in-flight drain. ``force`` overrides
    that: a *user-initiated* re-run (``evaluator.ensure_running`` from the dashboard's
    "Recalculate") re-arms even a ``running`` row and resets ``attempts``, recovering a
    row orphaned by a crashed/frozen worker so the click always (re)starts scoring. The
    per-user score lock + idempotent upserts keep this safe if a drain is in fact live."""
    job = db.scalar(select(ScoringJob).where(ScoringJob.user_id == user_id))
    if job is None:
        job = ScoringJob(user_id=user_id, state="pending", priority=priority)
        db.add(job)
        scoring_log.record("enqueue", user_id=user_id, state_to="pending", detail="new row")
    elif force or job.state != "running":
        prior = job.state
        job.state = "pending"
        job.enqueued_at = utcnow()
        job.priority = priority
        job.last_error = None
        job.finished_at = None
        if force or reset_attempts:
            job.attempts = 0  # fresh round: clear the consecutive-failure counter
        scoring_log.record(
            "enqueue", user_id=user_id, state_from=prior, state_to="pending",
            attempts=job.attempts, detail=f"re-armed (force={force})",
        )
    else:
        # A live worker owns the running row — an automatic re-enqueue leaves it be.
        # Traced because this is why a (non-forced) enqueue can look like a no-op.
        scoring_log.record(
            "enqueue", user_id=user_id, state_from="running", state_to="running",
            attempts=job.attempts, detail="skipped: running, not forced",
        )
    db.commit()
    return job


def reconcile(db: Session, *, reclaim_all_running: bool = False) -> int:
    """Prepare the queue for a drain AND self-heal stuck work, then enqueue every user
    with a backlog. Run before every drain (cron/dispatch/startup), so nothing sits
    failed forever. Returns the number of users enqueued. Three passes:

    1. *Per-posting auto-resolve*: clear terminal error-markers older than the TTL so
       those pairs re-enter the backlog and get a fresh round (score_marker_retry_after_hours).
    2. *Orphan reclaim/park*: a ``running`` row whose worker died goes back to ``pending``
       to retry — unless it has used up its attempts (``scoring_job_max_attempts``), in
       which case it's parked as ``error`` (kicked out of the active queue) so it stops
       hot-looping. Mid-run (cron/dispatch) a ``running`` row may belong to a LIVE
       in-process worker, so only rows stuck past the stale window (``scoring_stale_minutes``)
       are treated as orphans. At process startup that's not so — no worker thread has
       been spawned yet, so EVERY ``running`` row is orphaned by the dead process;
       ``reclaim_all_running`` drops the stale-window guard then. Without it, a job
       claimed seconds before a restart sits ``running`` (not stale, and pass 3 skips
       ``running`` rows) with no worker draining it until the stale window finally
       elapses — i.e. scoring silently stalls after every restart.
    3. *Cooldown requeue*: a parked job (``error`` with attempts at the cap) is requeued
       with a fresh budget once it's been parked longer than the cooldown — the
       auto-resolve so a parked job never stays out forever."""
    now = utcnow()
    cap = max(1, settings.scoring_job_max_attempts)
    stale_before = now - timedelta(minutes=max(1, settings.scoring_stale_minutes))
    cooldown_before = now - timedelta(minutes=max(1, settings.scoring_job_retry_cooldown_minutes))

    # 1) Auto-resolve per-posting failures: expire terminal markers so the pairs retry.
    if settings.score_marker_retry_after_hours > 0:
        expired = matcher.clear_failed_markers(
            db, older_than=now - timedelta(hours=settings.score_marker_retry_after_hours)
        )
        if expired:
            log.info("scoring queue: cleared %d expired failed-marker(s)", expired)
            scoring_log.record(
                "reconcile", detail=f"cleared {expired} expired failed-marker(s)"
            )

    # 2) Reclaim orphaned 'running' rows: retry under the cap, else park (kick out).
    # At startup every running row is orphaned (no worker spawned yet); mid-run only
    # rows stuck past the stale window are — a fresher one may be a live worker's.
    orphaned = select(ScoringJob).where(ScoringJob.state == "running")
    if not reclaim_all_running:
        orphaned = orphaned.where(ScoringJob.claimed_at < stale_before)
    reclaimed = parked = 0
    for job in db.scalars(orphaned):
        if job.attempts >= cap:
            job.state = "error"
            job.finished_at = now
            job.last_error = f"orphaned: claimed {job.attempts}x without finishing"
            parked += 1
            scoring_log.record(
                "reconcile", user_id=job.user_id, state_from="running", state_to="error",
                attempts=job.attempts, detail="parked: orphaned, hit attempt cap",
            )
        else:
            job.state = "pending"
            job.claimed_at = None
            reclaimed += 1
            scoring_log.record(
                "reconcile", user_id=job.user_id, state_from="running", state_to="pending",
                attempts=job.attempts, detail="reclaimed orphaned running row",
            )
    db.commit()
    if reclaimed or parked:
        log.warning(
            "scoring queue: reclaimed %d, parked %d orphaned running job(s)", reclaimed, parked
        )

    # 3) Enqueue users with a backlog; requeue parked jobs only after their cooldown.
    enqueued = 0
    for user_id in list(db.scalars(select(User.id))):
        user = db.get(User, user_id)
        if user is None or matcher.count_pending(db, user) <= 0:
            continue
        job = db.scalar(select(ScoringJob).where(ScoringJob.user_id == user_id))
        if job is not None and job.state == "running":
            continue  # a live worker owns it (or it'll be reclaimed next sweep)
        if job is not None and job.state == "error" and job.attempts >= cap:
            # Parked: leave it out of the queue until the cooldown elapses, then requeue
            # with a fresh retry budget (auto-resolve).
            if job.finished_at is not None and job.finished_at > cooldown_before:
                continue
            scoring_log.record(
                "reconcile", user_id=user_id, state_from="error", state_to="pending",
                detail="requeued parked job after cooldown",
            )
            enqueue(db, user_id, reset_attempts=True)
            enqueued += 1
            continue
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
            # job.attempts is re-read post-commit (expire_on_commit) -> the bumped value.
            scoring_log.record(
                "claim", user_id=job.user_id, state_from="pending", state_to="running",
                attempts=job.attempts,
            )
            return job.user_id
        # Lost the race to a peer worker — try the next pending row.


def finalize(db: Session, user: User, res) -> None:
    """Settle a user's job after a drain, based on the RunResult ``res``:
    - the drain didn't run (another in-process drain holds the score lock): leave
      ``pending`` so it's retried;
    - budget/quota exhausted: mark ``done`` now — the next cron's reconcile re-enqueues
      it once quota is back (never hot-loop a dead quota inside one run);
    - otherwise re-arm ``pending`` if work appeared mid-drain, else ``done``.

    Every path is a *clean* end of the worker's lifecycle (completion, progress, or a
    deferral), so it resets ``attempts`` — the consecutive-failure counter only grows
    on orphaned claims and ``mark_error`` (see ``reconcile``)."""
    if not getattr(res, "did_run", True):
        # Another in-process drain holds the per-user score lock; leave pending so the
        # worker (or a peer) re-claims and continues. Traced because a string of these
        # is the signature of two workers fighting over one user.
        scoring_log.record(
            "finalize", user_id=user.id, state_to="pending",
            detail="did_run=False (score lock held by peer)",
        )
        _set_state(db, user.id, "pending", reset_attempts=True)
        return
    if getattr(res, "budget_exhausted", False):
        scoring_log.record(
            "finalize", user_id=user.id, state_to="done",
            detail="budget_exhausted (re-enqueued when quota returns)",
        )
        _set_state(db, user.id, "done", finished=True, reset_attempts=True)
        return
    pending = matcher.count_pending(db, user)
    state = "pending" if pending > 0 else "done"
    # pending>0 -> 'pending' means the worker keeps draining (big backlog / time budget);
    # ==0 -> 'done' is a clean finish. This event tells you which, and why a row re-arms.
    scoring_log.record(
        "finalize", user_id=user.id, state_to=state,
        detail=(
            f"scored={getattr(res, 'scored', 0)} pending={pending} "
            f"time_exhausted={getattr(res, 'time_exhausted', False)}"
        ),
    )
    _set_state(db, user.id, state, finished=state == "done", reset_attempts=True)


def mark_error(db: Session, user_id: int, message: str) -> None:
    """Mark a user's job ``error`` after the drain raised. Leaves ``attempts`` as-is
    (this failed claim counts toward the cap); ``reconcile`` retries while under the cap,
    then parks it and requeues after a cooldown — so it's never terminal forever."""
    db.execute(
        update(ScoringJob)
        .where(ScoringJob.user_id == user_id)
        .values(state="error", last_error=(message or "")[:1000], finished_at=utcnow())
    )
    db.commit()
    # A stop point: the row won't be retried until reconcile runs (cron/dispatch/startup)
    # — on a long-lived dev server with no cron that's the next restart or forced re-run,
    # which is exactly how a drain "goes flaky". The full traceback is already on stdout
    # (evaluator logs the exception); here we record the queue transition for the trail.
    attempts = db.scalar(select(ScoringJob.attempts).where(ScoringJob.user_id == user_id))
    scoring_log.record(
        "error", user_id=user_id, state_to="error", attempts=attempts,
        detail=(message or "")[:500],
    )


def mark_done(db: Session, user_id: int) -> None:
    """Mark a user's job ``done`` (e.g. the user vanished after being claimed)."""
    scoring_log.record("done", user_id=user_id, state_to="done", detail="marked done")
    _set_state(db, user_id, "done", finished=True, reset_attempts=True)


def _set_state(
    db: Session, user_id: int, state: str, *, finished: bool = False,
    reset_attempts: bool = False,
) -> None:
    values: dict = {"state": state}
    if finished:
        values["finished_at"] = utcnow()
    if reset_attempts:
        values["attempts"] = 0
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
