"""In-process daily scheduler. Runs the full pipeline for all users at the
configured hour, then pushes each user's Telegram report through that user's own
bot. Telegram is per-user (the bot token + linked chat live on the User row), so
there is no global bot or long-poll loop — linking is on-demand from the settings
page. Everything is optional and guarded so the app boots fine without it."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import settings
from . import crawler, matcher, telegram_bot

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def daily_job() -> None:
    log.info("daily job starting")
    summaries = matcher.run_for_all_users()
    total_new = sum(s.new_positions for s in summaries.values())
    total_scored = sum(s.scored for s in summaries.values())
    log.info("daily job done: %d users, %d new positions, %d scored",
             len(summaries), total_new, total_scored)
    error_by_user = {uid: s.errors for uid, s in summaries.items() if s.errors}
    telegram_bot.send_daily_reports(error_by_user)


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
