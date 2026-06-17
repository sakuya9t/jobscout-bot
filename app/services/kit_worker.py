"""Background runner for per-position application-kit generation.

``POST /api/positions/{id}/kit`` marks the kit "generating" and hands the work to
this worker so the request returns immediately; the detail page then polls
``GET /api/positions/{id}/kit`` until the status leaves "generating". Mirrors
``services/evaluator.py`` but is keyed on a ``(user_id, position_id)`` pair (one
in-flight generation per position) rather than per user."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..models import ApplicationKit, Position, User
from . import kits

log = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_active: set[tuple[int, int]] = set()
_guard = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    # Lazily created so importing the module (e.g. in tests) doesn't spin threads.
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, settings.eval_max_workers),
                thread_name_prefix="kit-gen",
            )
        return _executor


def ensure_generating(user_id: int, position_id: int) -> None:
    """Schedule a background kit generation for ``(user_id, position_id)`` unless one
    is already queued or running for that pair."""
    key = (user_id, position_id)
    with _guard:
        if key in _active:
            return
        _active.add(key)
    _get_executor().submit(_run, user_id, position_id)


def is_generating(user_id: int, position_id: int) -> bool:
    """Whether a generation is queued/running for this pair (UI liveness signal)."""
    with _guard:
        return (user_id, position_id) in _active


def _run(user_id: int, position_id: int) -> None:
    try:
        with session_scope() as db:
            user = db.get(User, user_id)
            position = db.get(Position, position_id)
            if user is None or position is None:
                return
            kits.generate(db, user, position)
    except Exception:  # never let a generation kill the worker thread
        log.exception("kit generation failed for user %s position %s", user_id, position_id)
        _mark_failed(user_id, position_id)
    finally:
        with _guard:
            _active.discard((user_id, position_id))


def _mark_failed(user_id: int, position_id: int) -> None:
    """Best-effort: flip a stuck 'generating' row to 'error' after an unexpected
    crash, so the page stops polling forever."""
    try:
        with session_scope() as db:
            kit = db.scalar(
                select(ApplicationKit).where(
                    ApplicationKit.user_id == user_id,
                    ApplicationKit.position_id == position_id,
                )
            )
            if kit is not None and kit.status == "generating":
                kit.status = "error"
                kit.error_detail = "Generation failed unexpectedly. Please try again."
    except Exception:
        log.exception("kit failure-mark failed for user %s position %s", user_id, position_id)


def resume_pending_on_startup() -> None:
    """Re-kick any kit left mid-generation by a prior process so it finishes (or
    fails cleanly) instead of polling forever. Safe no-op when none are stuck."""
    try:
        with session_scope() as db:
            stuck = list(
                db.execute(
                    select(ApplicationKit.user_id, ApplicationKit.position_id).where(
                        ApplicationKit.status == "generating"
                    )
                )
            )
        for user_id, position_id in stuck:
            ensure_generating(user_id, position_id)
    except Exception:
        log.exception("kit worker startup resume failed")


def shutdown() -> None:
    """Stop accepting new generations and let in-flight ones finish (app shutdown)."""
    global _executor
    with _guard:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=False)
