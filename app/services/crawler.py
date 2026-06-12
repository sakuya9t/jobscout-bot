"""Shared preset crawling.

Preset companies are global (one row per ``company_presets.PRESETS`` entry) and
crawled ONCE per run — by the daily scheduler, a startup stale-check, or the admin
endpoint — independent of any user scan. Their postings land in shared ``Position``
rows that every subscriber's resume is then matched against, so we never fetch or
store the same company's jobs once per user.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from ..db import session_scope
from ..logging_config import get_logger
from ..models import Company
from ..timeutil import utcnow

log = get_logger(__name__)

# Only one crawl runs at a time across daily cron / startup / admin endpoint.
_crawl_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_guard = threading.Lock()


def _active_preset_ids(db) -> list[int]:
    return [
        c.id for c in db.scalars(
            select(Company).where(Company.preset_key.is_not(None), Company.is_active == True)  # noqa: E712
        )
    ]


def crawl_presets() -> dict:
    """Crawl every active global preset company once into shared positions.
    Serialized by ``_crawl_lock`` so overlapping triggers don't double-fetch; a
    concurrent call is a no-op. Returns a small summary dict."""
    from .matcher import _upsert_positions  # reuse the per-(company, external_id) dedup upsert

    summary = {"skipped": False, "companies": 0, "new_positions": 0, "errors": []}
    if not _crawl_lock.acquire(blocking=False):
        log.info("preset crawl already running — skipping this trigger")
        summary["skipped"] = True
        return summary
    try:
        with session_scope() as db:
            preset_ids = _active_preset_ids(db)
        # Per-company session + commit: release the write lock between companies and
        # never let one company's failure discard another's positions.
        for company_id in preset_ids:
            with session_scope() as db:
                company = db.get(Company, company_id)
                if company is None:
                    continue
                new_positions, errs = _upsert_positions(db, company)
                summary["companies"] += 1
                summary["new_positions"] += len(new_positions)
                summary["errors"].extend(errs)
        log.info(
            "preset crawl done: %d companies, %d new positions, %d error(s)",
            summary["companies"], summary["new_positions"], len(summary["errors"]),
        )
    finally:
        _crawl_lock.release()
    return summary


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crawler")
        return _executor


def crawl_presets_async() -> None:
    """Kick a preset crawl on a background thread (admin endpoint, startup check) so
    the caller returns immediately."""
    _get_executor().submit(_run_safe)


def _run_safe() -> None:
    try:
        crawl_presets()
    except Exception:  # never let a crawl kill the worker thread
        log.exception("background preset crawl failed")


def presets_are_stale(max_age_hours: int = 20) -> bool:
    """True if any active preset has never been crawled or was last crawled longer
    ago than ``max_age_hours`` — used to decide whether to crawl on startup."""
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    with session_scope() as db:
        rows = list(db.scalars(
            select(Company.last_scraped_at).where(
                Company.preset_key.is_not(None), Company.is_active == True  # noqa: E712
            )
        ))
    return any(ts is None or ts < cutoff for ts in rows) if rows else False


def shutdown() -> None:
    """Stop the background crawler executor (called on app shutdown)."""
    global _executor
    with _guard:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=False)
