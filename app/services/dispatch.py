"""Event-driven kick for the scoring drain (the pub/sub "publish" side).

The scoring backlog lives in the ``scoring_jobs`` queue (services/scoring_queue.py).
A long-lived server drains it in-process the moment work is enqueued
(``evaluator.ensure_draining``), but a serverless deploy (Vercel) can't — its threads
die on a function freeze. Instead of waiting for the next scheduled cron, this module
fires a one-shot HTTP trigger so a consumer drains *now*:

  producer enqueues -> dispatch_scoring_run() -> POST -> consumer drains the queue

Transport is plain HTTP so it works the same everywhere and is testable locally:
  * ``JOBSCOUT_SCORING_DISPATCH_URL`` points at any consumer of ``jobscout run-scoring``
    (e.g. this app's own ``POST /api/cron/run-scoring`` with token = ``CRON_SECRET``, or
    historically a GitHub ``repository_dispatch`` endpoint + ``..._TOKEN``).
  * Unset (default): no-op. This is the case on DigitalOcean — the in-process worker
    pool drains the moment work is enqueued, so no HTTP kick is needed. (The original
    production consumer, the ``scoring.yml`` GitHub Actions workflow, has been retired
    with the move off serverless; this module is now an optional local/dev convenience.)

Fire-and-forget: a failed kick is logged, never raised — the scheduled run remains the
backstop, so a flaky trigger can't break the producer or leave work unscored.
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..logging_config import get_logger

log = get_logger(__name__)


def dispatch_scoring_run() -> bool:
    """Fire the scoring-drain trigger. Returns True if a request was sent (and
    accepted), False when no URL is configured or the request failed. Never raises."""
    url = settings.scoring_dispatch_url
    if not url:
        return False  # not configured — rely on in-process drain / scheduled cron
    headers = {
        "Accept": "application/vnd.github+json",      # harmless for a non-GitHub target
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.scoring_dispatch_token:
        headers["Authorization"] = f"Bearer {settings.scoring_dispatch_token}"
    try:
        resp = httpx.post(
            url,
            json={"event_type": settings.scoring_dispatch_event},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("scoring dispatch fired -> %s (HTTP %s)", url, resp.status_code)
        return True
    except httpx.HTTPError as exc:
        # The scheduled cron is the backstop; a missed kick only adds latency.
        log.warning("scoring dispatch failed (%s): %s", url, exc)
        return False
