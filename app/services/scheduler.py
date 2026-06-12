"""In-process daily scheduler. Runs the full pipeline for all users at the
configured hour, then pushes Telegram reports. Also runs a background Telegram
long-poll loop for account linking. Everything is optional and guarded so the
app boots fine with no scheduler and no bot."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import settings
from . import crawler, matcher, telegram_bot

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_telegram_thread: threading.Thread | None = None
_telegram_stop = threading.Event()


def daily_job() -> None:
    log.info("daily job starting")
    summaries = matcher.run_for_all_users()
    total_new = sum(s.new_positions for s in summaries.values())
    total_scored = sum(s.scored for s in summaries.values())
    log.info("daily job done: %d users, %d new positions, %d scored",
             len(summaries), total_new, total_scored)
    error_by_user = {uid: s.errors for uid, s in summaries.items() if s.errors}
    telegram_bot.send_daily_reports(error_by_user)


def _telegram_loop() -> None:
    """Long-poll Telegram for /start linking. Any failure (bad token, network,
    non-JSON) backs off exponentially instead of hot-looping, and never lets the
    daemon thread die — it logs and retries until the process stops."""
    offset = None
    backoff = 1.0
    while not _telegram_stop.is_set():
        try:
            offset = telegram_bot.poll_updates(offset)
            backoff = 1.0  # success → reset
        except Exception as exc:  # noqa: BLE001 — keep the poller alive
            log.warning("telegram poll failed, backing off %.0fs: %s", backoff, exc)
            _telegram_stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)


def start() -> None:
    """Start scheduler + telegram polling if enabled. Idempotent."""
    global _scheduler, _telegram_thread

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

    if settings.telegram_bot_token and _telegram_thread is None:
        _telegram_stop.clear()
        _telegram_thread = threading.Thread(target=_telegram_loop, daemon=True, name="telegram")
        _telegram_thread.start()
        log.info("telegram poller started")


def shutdown() -> None:
    global _scheduler
    _telegram_stop.set()
    crawler.shutdown()
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
