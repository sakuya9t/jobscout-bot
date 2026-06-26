"""Background runner for the per-position "Re-evaluate" action on the detail page.

``POST /api/positions/{id}/rescore`` kicks a single-position re-score and returns
immediately; the detail page then polls ``GET /api/positions/{id}/rescore`` until it
reports the work is no longer in progress, then reloads the detail to show the
refreshed score, "How you line up", and the Winning/Risks columns. Mirrors
``services/kit_worker`` — keyed on a ``(user_id, position_id)`` pair (one in-flight
re-score per posting) — but runs ``matcher.rescore_position`` instead of kit
generation.

Unlike the kit, a re-score has no persisted in-progress status (it overwrites the
MatchResult atomically and only on success), so the "is it running?" signal and the
last error live in memory here. A re-score interrupted by a restart simply doesn't
finish — there's nothing half-written to recover — so there's no startup resume; the
user just clicks again."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..db import session_scope
from ..logging_config import get_logger
from ..models import Position, User
from . import matcher

log = get_logger(__name__)

_executor: ThreadPoolExecutor | None = None
_active: set[tuple[int, int]] = set()
# Last failure message per (user, position), surfaced once to the polling page and
# cleared when a new re-score starts. Absent ⇒ the last run succeeded (or none yet).
_errors: dict[tuple[int, int], str] = {}
_guard = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    # Lazily created so importing the module (e.g. in tests) doesn't spin threads.
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, settings.eval_max_workers),
                thread_name_prefix="rescore",
            )
        return _executor


def ensure_rescoring(user_id: int, position_id: int) -> None:
    """Schedule a background re-score for ``(user_id, position_id)`` unless one is
    already queued or running for that pair. Clears any prior error so the polling
    page doesn't show a stale failure for the new run."""
    key = (user_id, position_id)
    with _guard:
        if key in _active:
            return
        _active.add(key)
        _errors.pop(key, None)
    _get_executor().submit(_run, user_id, position_id)


def is_rescoring(user_id: int, position_id: int) -> bool:
    """Whether a re-score is queued/running for this pair (UI liveness signal)."""
    with _guard:
        return (user_id, position_id) in _active


def last_error(user_id: int, position_id: int) -> str | None:
    """The most recent re-score failure for this pair, or None when the last run
    succeeded / none has run. Reported alongside ``is_rescoring`` so the page can show
    why a re-evaluation didn't take."""
    with _guard:
        return _errors.get((user_id, position_id))


def _run(user_id: int, position_id: int) -> None:
    key = (user_id, position_id)
    try:
        with session_scope() as db:
            user = db.get(User, user_id)
            position = db.get(Position, position_id)
            if user is None or position is None:
                return
            res = matcher.rescore_position(db, user, position)
            if res.errors:
                with _guard:
                    _errors[key] = res.errors[0]
    except Exception:  # never let a re-score kill the worker thread
        log.exception("re-score failed for user %s position %s", user_id, position_id)
        with _guard:
            _errors[key] = "Re-evaluation failed unexpectedly. Please try again."
    finally:
        with _guard:
            _active.discard(key)


def shutdown() -> None:
    """Stop accepting new re-scores and let in-flight ones finish (app shutdown)."""
    global _executor
    with _guard:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=False)
