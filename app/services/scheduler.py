"""In-process daily scheduler. Scrapes every user's companies at the configured
hour and saves any new positions. It deliberately does NOT score — matching is
expensive per user, so it's deferred to an on-demand scan from the job-list view
(web ``/api/run``). Everything is optional and guarded so the app boots fine
without it."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import settings
from . import crawler, matcher

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def daily_job() -> None:
    log.info("daily scrape starting")
    summaries = matcher.scrape_for_all_users()
    total_new = sum(s.new_positions for s in summaries.values())
    log.info("daily scrape done: %d users, %d new positions", len(summaries), total_new)
    # The scrape is a once-a-day producer: hand any new backlog to the scoring queue and
    # wake its consumer. We never score here — the queue drains in its own worker pool,
    # in small per-user batches, separate from this cron (joined only through the queue).
    from . import evaluator  # lazy import keeps the scrape/scheduler free of evaluator at import time
    enqueued = evaluator.enqueue_pending_and_drain()
    log.info("daily scrape: enqueued %d user(s) for scoring", enqueued)


def start() -> None:
    """Start the daily scheduler if enabled. Idempotent."""
    global _scheduler

    if settings.scheduler_enabled and _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            daily_job,
            CronTrigger(hour=settings.daily_run_hour, minute=settings.daily_run_minute),
            id="daily_job",
            replace_existing=True,
        )
        _scheduler.start()
        log.info("scheduler started: daily at %02d:%02d",
                 settings.daily_run_hour, settings.daily_run_minute)
        # Populate the shared preset catalog now if it's empty/stale, so a freshly
        # started service has jobs without waiting for the daily cron hour.
        if crawler.presets_are_stale():
            log.info("preset catalog stale on startup — kicking a background crawl")
            crawler.crawl_presets_async()


def shutdown() -> None:
    global _scheduler
    crawler.shutdown()
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
