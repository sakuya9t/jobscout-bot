"""Optional HTTP trigger for the daily scrape+score pipeline.

This is the same synchronous run as ``jobscout run-daily`` (``app/cli.py``) behind
an authenticated HTTP call. NOTE: it is **not** the scheduled production path. On
Vercel's serverless functions a full scan can't complete within the (Hobby) 60s
limit — a single Ollama request alone can take up to JOBSCOUT_OLLAMA_TIMEOUT — so
the daily scan runs in a GitHub Actions cron (``.github/workflows/daily-scan.yml``,
no time limit) instead. This endpoint remains handy for a manual kick or for a
long-lived-server deploy where the request can run to completion.

Auth: send ``Authorization: Bearer <CRON_SECRET>`` (the value of the ``CRON_SECRET``
env var, read straight from the environment, bypassing the ``JOBSCOUT_`` prefix).
This is also the header Vercel Cron would send automatically if you ever schedule
it there on a plan whose function time limit fits.
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
