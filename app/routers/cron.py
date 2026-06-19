"""Optional HTTP trigger for the daily scrape.

This is the same scrape-only run as ``jobscout run-daily`` (``app/cli.py``) behind
an authenticated HTTP call. The daily scan runs in a GitHub Actions cron
(``.github/workflows/daily-scan.yml``) in production; this endpoint remains handy
for a manual kick or for a long-lived-server deploy. It only scrapes and saves new
positions — scoring is deferred to an on-demand scan from the job-list view (web
``/api/run``), so this stays cheap and fast even with many users.

Auth: send ``Authorization: Bearer <CRON_SECRET>`` (the value of the ``CRON_SECRET``
env var, read straight from the environment, bypassing the ``JOBSCOUT_`` prefix).
This is also the header Vercel Cron would send automatically if you ever schedule
it there.
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Header, HTTPException, status

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..services import dispatch, evaluator, matcher, scoring_queue

router = APIRouter(prefix="/api/cron", tags=["cron"])

log = get_logger(__name__)


def _require_cron(authorization: str | None) -> None:
    """Authenticate a Vercel Cron call. ``CRON_SECRET`` unset => 503 (never an open
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
    # Publish the freshly-scraped work: enqueue every user with a backlog, then kick a
    # scoring drain. Scrape and scoring stay separate jobs — this only marks work ready
    # and fires the trigger; the consumer does the expensive matching.
    with session_scope() as db:
        enqueued = scoring_queue.reconcile(db)
    if enqueued:
        dispatch.dispatch_scoring_run()
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
    rows), then run the bounded worker pool to the per-run budget. This is the consumer
    the dispatch trigger calls — in production the trigger goes to GitHub Actions
    (``jobscout run-scoring``) because a big drain exceeds a serverless time limit, but
    this endpoint makes the loop runnable/testable locally: point
    ``JOBSCOUT_SCORING_DISPATCH_URL`` at it (token = ``CRON_SECRET``). If the budget
    leaves work behind, it re-fires the trigger so the queue keeps draining."""
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
    if more:
        dispatch.dispatch_scoring_run()  # budget left work behind — continue the drain
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
