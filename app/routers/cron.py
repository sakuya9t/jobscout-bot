"""Serverless cron endpoint — the daily scrape+score pipeline behind an HTTP call.

On a long-lived server the in-process scheduler (``app/services/scheduler.py``)
runs ``daily_job`` on a timer. On Vercel that scheduler is disabled
(``JOBSCOUT_SCHEDULER_ENABLED=0``); instead a Vercel Cron entry hits this endpoint
on a schedule. Vercel automatically sends ``Authorization: Bearer $CRON_SECRET``
on cron invocations when the ``CRON_SECRET`` env var is set, so we authenticate on
that exact value (read straight from the environment, bypassing the ``JOBSCOUT_``
settings prefix). The work is the same synchronous run as ``jobscout run-daily``
(``app/cli.py:cmd_run_daily``) — ``run_for_all_users`` scrapes and scores to
completion inline, so the scan finishes within the single request.
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Header, HTTPException, status

from ..logging_config import get_logger
from ..services import matcher, telegram_bot

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
    """Run the daily pipeline for all users, then push each user's Telegram report.

    Synchronous: ``run_for_all_users`` scrapes and drains the scoring backlog inline,
    so the scan is complete when this returns (bounded by the function's maxDuration —
    a large backlog resumes on the next run since scoring is idempotent/persisted)."""
    _require_cron(authorization)
    log.info("cron run-daily starting")
    summaries = matcher.run_for_all_users()
    new = sum(s.new_positions for s in summaries.values())
    scored = sum(s.scored for s in summaries.values())
    errors = [e for s in summaries.values() for e in s.errors]
    telegram_bot.send_daily_reports(
        {uid: s.errors for uid, s in summaries.items() if s.errors}
    )
    log.info(
        "cron run-daily done: %d users, %d new positions, %d scored, %d warnings",
        len(summaries), new, scored, len(errors),
    )
    return {
        "status": "ok",
        "users": len(summaries),
        "new_positions": new,
        "scored": scored,
        "warnings": errors,
    }
