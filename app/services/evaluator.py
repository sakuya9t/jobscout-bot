"""Background evaluation drainer.

`/api/run` scrapes synchronously, then hands scoring to this worker so the request
returns immediately. The worker drains a user's entire scoring backlog (see
`matcher.score_to_completion`) to completion off the request path, records one
`JobListSnapshot` per completed drain, and re-arms itself if more work appeared
while it ran. The dashboard shows the remaining `matcher.count_pending` count.

Concurrency model:
- One drain per user at a time. `ensure_running` dedups via `_active`; the
  matcher's per-user score lock is the cross-process backstop (daily run vs web).
- `_active` doubles as the `in_progress` signal the UI polls.
- Re-arm only when work remains AND the budget wasn't exhausted — never hot-loop
  against a dead Ollama quota.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..models import User
from . import matcher, reporter

log = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_active: set[int] = set()
_guard = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    # Lazily created so importing the module (e.g. in tests) doesn't spin threads.
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, settings.eval_max_workers),
                thread_name_prefix="evaluator",
            )
        return _executor


def ensure_running(user_id: int) -> None:
    """Schedule a background drain for ``user_id`` unless one is already queued or
    running (the active drain re-queries each pass, so it absorbs new work)."""
    with _guard:
        if user_id in _active:
            return
        _active.add(user_id)
    _get_executor().submit(_run, user_id)


def active_users() -> set[int]:
    """Users with a queued/running drain — powers the UI's `in_progress` flag."""
    with _guard:
        return set(_active)


def _run(user_id: int) -> None:
    budget_exhausted = False
    try:
        with session_scope() as db:
            user = db.get(User, user_id)
            if user is None:
                return
            res = matcher.score_to_completion(db, user)
            if not res.did_run:
                # Another drain (e.g. the daily run) holds the score lock; it will
                # finish the work and record its own snapshot. Don't double up.
                return
            res.finalize_errors()
            reporter.record_job_list_snapshot(db, user, res)
            budget_exhausted = res.budget_exhausted
    except Exception:  # never let a drain kill the worker thread
        log.exception("evaluation drain failed for user %s", user_id)
    finally:
        with _guard:
            _active.discard(user_id)
    # Re-arm for work that arrived mid-drain (a concurrent /api/run scraped more),
    # but never against an exhausted budget — that would hot-loop on a dead quota.
    if budget_exhausted:
        return
    try:
        with session_scope() as db:
            user = db.get(User, user_id)
            if user is not None and matcher.count_pending(db, user) > 0:
                ensure_running(user_id)
    except Exception:
        log.exception("evaluation re-arm check failed for user %s", user_id)


def resume_pending_on_startup() -> None:
    """Re-kick drains for any user with a non-empty backlog, so an evaluation that
    was interrupted by a process restart finishes. Safe no-op when all caught up."""
    try:
        with session_scope() as db:
            user_ids = list(db.scalars(matcher.select(User.id)))
        for uid in user_ids:
            with session_scope() as db:
                user = db.get(User, uid)
                if user is not None and matcher.count_pending(db, user) > 0:
                    ensure_running(uid)
    except Exception:
        log.exception("evaluator startup resume failed")


def shutdown() -> None:
    """Stop accepting new drains and let in-flight ones finish (called on app
    shutdown)."""
    global _executor
    with _guard:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=False)
