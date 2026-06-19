"""Background scoring drains — both the on-demand (web) path and the periodic cron
share one queue (services/scoring_queue.py).

`/api/run` scrapes synchronously, then calls `ensure_running` to ENQUEUE the user and
kick the drain so the request returns immediately. Scoring no longer runs as a
detached per-request thread: instead a bounded pool of at most
`scoring_max_concurrency` worker threads claim users off the queue and drain them.
That bound is the database-connection throttle — a peak of users all hitting
*Run scan* enqueues cheap rows but can never push more than N drains (and thus N
Supabase connections) at once. The same `scoring_queue` is drained by the
`jobscout run-scoring` cron via `drain_queue` (one-shot, run-to-completion).

Concurrency model (web path):
- `ensure_running` -> `scoring_queue.enqueue` (durable) -> `ensure_draining`.
- `ensure_draining` keeps up to N daemon workers alive; each loops
  `scoring_queue.claim_one` (FOR UPDATE SKIP LOCKED) -> drain that user -> repeat,
  exiting when the queue is empty. The per-user `scoring_jobs` row is the claim, so
  two workers (or a web worker and the cron) never drain the same user.
- On serverless (`background_workers_enabled=0`) `ensure_draining` is a no-op:
  threads don't survive a function freeze, so `ensure_running` only enqueues and the
  GitHub Actions cron does the draining.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..models import User
from . import dispatch, matcher, reporter, scoring_log, scoring_queue

log = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
# Number of web drain workers currently alive (never exceeds scoring_max_concurrency).
# Guarded by `_guard`, which also serializes a worker's "queue looks empty, exit?"
# decision against `ensure_draining` so a job enqueued in that window isn't orphaned.
_active_workers = 0
_guard = threading.Lock()


def _max_workers() -> int:
    return max(1, settings.scoring_max_concurrency)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    # Lazily created so importing the module (e.g. in tests) doesn't spin threads.
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_max_workers(), thread_name_prefix="evaluator"
            )
        return _executor


def ensure_running(user_id: int) -> None:
    """Enqueue ``user_id`` for scoring and kick the drain. Always enqueues (durably,
    so the scheduled cron is a backstop even on serverless). On a long-lived server we
    drain in-process (`ensure_draining`); where threads don't survive (serverless) we
    instead fire the event-driven dispatch so a consumer drains now rather than at the
    next scheduled run (`dispatch.dispatch_scoring_run`, a no-op when unconfigured)."""
    with session_scope() as db:
        # force=True: this is a user-initiated re-run, so re-arm even a 'running' row —
        # recovers a job orphaned by a crashed/frozen worker, which would otherwise
        # block the click until the stale-reclaim sweep (scoring_stale_minutes).
        scoring_queue.enqueue(db, user_id, force=True)
    if settings.background_workers_enabled:
        ensure_draining()
    else:
        dispatch.dispatch_scoring_run()


def ensure_draining() -> None:
    """Ensure up to ``scoring_max_concurrency`` web workers are draining the queue.
    No-op on serverless (threads don't survive a freeze — the cron drains there)."""
    if not settings.background_workers_enabled:
        return
    global _active_workers
    # Nothing queued ⇒ don't spawn no-op workers (the producer commits its enqueue
    # before calling us, so a job in flight is already visible here).
    with session_scope() as db:
        if not scoring_queue.has_pending(db):
            return
    ex = _get_executor()  # built outside _guard to avoid re-entrant locking
    with _guard:
        spawn = _max_workers() - _active_workers
        _active_workers += spawn
        active = _active_workers
    if spawn > 0:
        scoring_log.record(
            "worker", state_to="spawn",
            detail=f"spawned {spawn} worker(s); active {active}/{_max_workers()}",
        )
    for _ in range(spawn):
        ex.submit(_web_worker)


def _web_worker() -> None:
    """Claim and drain users until the queue is empty. Before exiting on an empty
    queue, re-checks under `_guard` so a job enqueued just as we were leaving (while
    `ensure_draining` saw us still active) is picked up rather than stranded. The
    re-check and the slot-release happen under the same `_guard` hold so that window
    is closed. Always releases its slot on exit (incl. a DB error), or the pool would
    permanently under-count and eventually stop spawning workers."""
    global _active_workers

    def _release() -> None:  # clamp: a concurrent `shutdown` reset can't go negative
        global _active_workers
        _active_workers = max(0, _active_workers - 1)

    while True:
        try:
            with session_scope() as db:
                user_id = scoring_queue.claim_one(db)
        except Exception:
            log.exception("scoring worker: claim failed; releasing slot")
            with _guard:
                _release()
                active = _active_workers
            scoring_log.record(
                "worker", state_to="exit", detail=f"claim error; active now {active}"
            )
            return
        if user_id is None:
            with _guard:
                try:
                    with session_scope() as db:
                        user_id = scoring_queue.claim_one(db)
                except Exception:
                    log.exception("scoring worker: re-check claim failed")
                    user_id = None
                if user_id is None:
                    _release()
                    active = _active_workers
                    scoring_log.record(
                        "worker", state_to="exit",
                        detail=f"queue empty; active now {active}",
                    )
                    return
            # Claimed on the re-check — drain it OUTSIDE the guard (never hold the
            # lock across the LLM-heavy drain). `_drain_claimed_user` never raises.
        _drain_claimed_user(user_id)


def active_users() -> set[int]:
    """Users with a queued/running scoring job — powers the UI's `in_progress` flag."""
    with session_scope() as db:
        return scoring_queue.active_user_ids(db)


def _run(user_id: int) -> None:
    """Drain one specific user's backlog synchronously (no queue claim). Used by the
    startup resume helper's callers and tests; the queue paths use `_web_worker` /
    `drain_queue` instead."""
    _drain_claimed_user(user_id)


def resume_pending_on_startup() -> None:
    """On boot, (re)enqueue every user with a non-empty backlog — finishing any drain
    interrupted by a restart and reclaiming jobs stranded `running` by a crash — then
    kick the workers. Safe no-op when all caught up.

    `reclaim_all_running=True` because this is process start: no worker thread exists
    yet, so any `running` row is necessarily orphaned by the dead process — including
    one claimed seconds before the restart, which the stale-window guard would
    otherwise leave stuck `running` (never re-armed, never drained) until the window
    elapsed. That's the "Evaluating — N still to score" view that never progresses."""
    try:
        with session_scope() as db:
            scoring_queue.reconcile(db, reclaim_all_running=True)
        ensure_draining()
    except Exception:
        log.exception("evaluator startup resume failed")


def shutdown() -> None:
    """Stop accepting new drains and let in-flight ones finish (called on app
    shutdown)."""
    global _executor, _active_workers
    with _guard:
        ex = _executor
        _executor = None
        _active_workers = 0
    if ex is not None:
        ex.shutdown(wait=False)


# --- Periodic queue drain (the `jobscout run-scoring` cron) -------------------
# Same queue as the web path, but run one-shot to completion from a standalone
# process with its own bounded pool, then exit.

@dataclass
class DrainSummary:
    users: int = 0       # users whose drain completed (incl. no-op drains)
    scored: int = 0
    failed: int = 0      # users whose drain raised
    errors: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, res) -> None:
        with self._lock:
            self.users += 1
            self.scored += getattr(res, "scored", 0)
            # Keep a small, de-duplicated sample of warnings for the run log.
            for e in getattr(res, "errors", []):
                if e not in self.errors and len(self.errors) < 10:
                    self.errors.append(e)

    def add_failure(self) -> None:
        with self._lock:
            self.failed += 1


def drain_queue(
    *, max_workers: int | None = None, budget_seconds: int | None = None
) -> DrainSummary:
    """Drain the scoring queue with a bounded pool of ``max_workers`` threads, each
    claiming one user at a time (``scoring_queue.claim_one``) and scoring its whole
    backlog, until the queue is empty or ``budget_seconds`` elapses. ``max_workers``
    is THE DB-connection throttle: at most that many users drain — and hold a
    connection — at once. Returns aggregate counts for the cron summary.

    Callers (the `jobscout run-scoring` CLI) typically run ``scoring_queue.reconcile``
    first to populate the queue; this function only consumes it."""
    workers = max(1, max_workers if max_workers is not None else settings.scoring_max_concurrency)
    budget = settings.scoring_run_budget_seconds if budget_seconds is None else budget_seconds
    deadline = (time.monotonic() + budget) if budget and budget > 0 else None
    summary = DrainSummary()

    def worker() -> None:
        while deadline is None or time.monotonic() < deadline:
            with session_scope() as db:
                user_id = scoring_queue.claim_one(db)
            if user_id is None:
                return  # queue drained (or all remaining rows held by peers)
            # Pass the run deadline so one huge user's drain also stops at the budget
            # (it's only checked between users here) and re-arms instead of running
            # past the job timeout and being killed mid-drain.
            _drain_claimed_user(user_id, summary, deadline=deadline)

    threads = [
        threading.Thread(target=worker, name=f"scoring-{i}", daemon=True)
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info(
        "scoring drain done: %d user(s), %d scored, %d failed",
        summary.users, summary.scored, summary.failed,
    )
    return summary


def _drain_claimed_user(
    user_id: int, summary: DrainSummary | None = None, *, deadline: float | None = None
) -> None:
    """Score one already-claimed user's backlog in its own session, then settle its
    queue row. Never raises — a single user's failure is isolated to that user.
    ``deadline`` (a ``time.monotonic()`` value) caps the drain so a big backlog stops
    at the run budget and re-arms ``pending`` rather than overrunning the job timeout."""
    try:
        with session_scope() as db:
            user = db.get(User, user_id)
            if user is None:  # user deleted after being claimed
                scoring_queue.mark_done(db, user_id)
                return
            res = matcher.score_to_completion(db, user, deadline=deadline)
            if res.did_run:
                res.finalize_errors()
                reporter.record_job_list_snapshot(db, user, res)
            scoring_log.record(
                "drain", user_id=user_id,
                detail=(
                    f"did_run={res.did_run} scored={res.scored} filtered={res.filtered} "
                    f"time_exhausted={res.time_exhausted} "
                    f"budget_exhausted={res.budget_exhausted} errors={len(res.errors)}"
                ),
            )
            scoring_queue.finalize(db, user, res)
            if summary is not None:
                summary.add(res)
    except Exception as exc:  # never let one user kill a worker thread
        log.exception("scoring drain failed for user %s", user_id)
        if summary is not None:
            summary.add_failure()
        try:
            with session_scope() as db:
                scoring_queue.mark_error(db, user_id, str(exc))
        except Exception:
            log.exception("scoring queue: could not mark user %s errored", user_id)
