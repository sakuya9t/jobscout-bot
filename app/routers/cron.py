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

from ..logging_config import get_logger
from ..services import matcher

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
    log.info(
        "cron run-daily done: %d users, %d new positions, %d warnings",
        len(summaries), new, len(errors),
    )
    return {
        "status": "ok",
        "users": len(summaries),
        "new_positions": new,
        "warnings": errors,
    }
