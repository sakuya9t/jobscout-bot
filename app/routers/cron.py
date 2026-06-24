"""Optional HTTP trigger for the daily scrape.

This is the same scrape-only run as ``jobscout run-daily`` (``app/cli.py``) behind
an authenticated HTTP call. In production the daily scan runs via the in-process
scheduler (``services/scheduler.py``); this endpoint remains handy for a manual kick
or an external scheduler. It only scrapes and saves new positions — scoring is
deferred to an on-demand scan from the job-list view (web ``/api/run``), so this stays
cheap and fast even with many users.

Auth: send ``Authorization: Bearer <CRON_SECRET>`` (the value of the ``CRON_SECRET``
env var, read straight from the environment, bypassing the ``JOBSCOUT_`` prefix).
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Header, HTTPException, status

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..services import evaluator, matcher, scoring_queue

router = APIRouter(prefix="/api/cron", tags=["cron"])

log = get_logger(__name__)


def _require_cron(authorization: str | None) -> None:
    """Authenticate a cron HTTP call. ``CRON_SECRET`` unset => 503 (never an open
    trigger); a missing or mismatched bearer token => 401 (constant-time compare)."""
    expected = os.environ.get("CRON_SECRET")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Cron endpoint is disabled. Set the CRON_SECRET env var to enable it.",
        )
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer ") :].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid cron token.")


@router.get("/run-daily")
def run_daily(authorization: str | None = Header(default=None)) -> dict:
    """Scrape every user's companies and save new positions. Scoring is deliberately
    NOT done here — matching is deferred to an on-demand scan from the job-list view
    (web ``/api/run``), so the daily cron stays cheap and fast with many users."""
    _require_cron(authorization)
    log.info("cron run-daily (scrape-only) starting")
    summaries = matcher.scrape_for_all_users()
    new = sum(s.new_positions for s in summaries.values())
    errors = [e for s in summaries.values() for e in s.errors]
    # Publish the freshly-scraped work: enqueue every user with a backlog. Scrape and
    # scoring stay separate jobs — this only marks work ready; the long-lived server's
    # in-process workers (or the `jobscout run-scoring` drain) do the expensive matching.
    with session_scope() as db:
        enqueued = scoring_queue.reconcile(db)
    log.info(
        "cron run-daily done: %d users, %d new positions, %d warnings, %d enqueued",
        len(summaries), new, len(errors), enqueued,
    )
    return {
        "status": "ok",
        "users": len(summaries),
        "new_positions": new,
        "enqueued": enqueued,
        "warnings": errors,
    }


@router.post("/run-scoring")
def run_scoring(authorization: str | None = Header(default=None)) -> dict:
    """Drain the scoring queue: reconcile (enqueue users with a backlog, reclaim stale
    rows), then run the bounded worker pool to the per-run budget. An authenticated
    out-of-process drain equivalent to ``jobscout run-scoring`` — handy for an external
    scheduler. If the budget leaves work pending, ``more_pending`` is true so the next
    run continues the drain."""
    _require_cron(authorization)
    log.info("cron run-scoring starting")
    with session_scope() as db:
        enqueued = scoring_queue.reconcile(db)
    summary = evaluator.drain_queue(
        max_workers=settings.scoring_max_concurrency,
        budget_seconds=settings.scoring_run_budget_seconds,
    )
    with session_scope() as db:
        states = scoring_queue.counts_by_state(db)
        more = scoring_queue.has_pending(db)
    log.info(
        "cron run-scoring done: enqueued %d, drained %d user(s), scored %d, more=%s",
        enqueued, summary.users, summary.scored, more,
    )
    return {
        "status": "ok",
        "enqueued": enqueued,
        "users": summary.users,
        "scored": summary.scored,
        "failed": summary.failed,
        "queue": states,
        "more_pending": more,
    }
