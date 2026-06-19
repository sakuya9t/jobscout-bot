"""Off-the-hot-path persistence of scoring-queue / worker lifecycle events.

Every state change a ``ScoringJob`` goes through — enqueue, claim, finalize, drain
summary, error, and the reconcile self-heal actions — plus worker-pool spawn/exit
records one row in the ``scoring_events`` table. That keeps the trace OUT of stdout
(where it was buried under per-batch scoring noise) and in a queryable table you can
filter by user to reconstruct exactly where a flaky drain stopped and why.

Why a background writer rather than writing inline: a claim/finalize is on the path
that decides whether scoring keeps going, and during a drain the matcher holds an
*uncommitted* SQLite write transaction (one writer only) — a second connection
writing an event row would block on the write lock and stall scoring. So records are
handed to a single daemon writer thread via an in-memory queue, drained with its own
short-lived sessions that naturally slot between the matcher's commits; the caller
never blocks. Tracing is best-effort: a full queue drops records and a failed write
is warned and skipped — neither ever breaks a claim or a drain. Mirrors
``services/llm_log.py``."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass

from ..config import settings

log = logging.getLogger(__name__)

# Bound the buffer so a stuck/slow writer can't grow memory without limit; past this
# we drop records (with a warning) rather than block the caller.
_QUEUE_MAXSIZE = 5000

_q: "queue.Queue[ScoringEventRecord] | None" = None
_worker: threading.Thread | None = None
_lock = threading.Lock()


@dataclass
class ScoringEventRecord:
    """A flattened lifecycle event; field names match ``models.ScoringEvent`` columns."""

    event: str
    user_id: int | None
    state_from: str | None
    state_to: str | None
    attempts: int | None
    worker: str | None
    detail: str | None


def _ensure_worker() -> "queue.Queue[ScoringEventRecord]":
    """Lazily start the daemon writer (and its queue) on first use."""
    global _q, _worker
    with _lock:
        if _q is None:
            _q = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_drain, name="scoring-log-writer", daemon=True
            )
            _worker.start()
        return _q


def record(
    event: str,
    *,
    user_id: int | None = None,
    state_from: str | None = None,
    state_to: str | None = None,
    attempts: int | None = None,
    detail: str | None = None,
) -> None:
    """Queue one queue/worker event for persistence. Non-blocking; drops on a full
    queue. No-op when ``log_scoring_events`` is off. The emitting thread name is
    captured automatically so each worker's trail is attributable."""
    if not settings.log_scoring_events:
        return
    rec = ScoringEventRecord(
        event=event,
        user_id=user_id,
        state_from=state_from,
        state_to=state_to,
        attempts=attempts,
        worker=threading.current_thread().name,
        detail=(detail or None) and detail[:1000],
    )
    q = _ensure_worker()
    try:
        q.put_nowait(rec)
    except queue.Full:
        log.warning("scoring_events queue full (%d); dropping a record", _QUEUE_MAXSIZE)


def _drain() -> None:
    # Imported lazily so this module stays import-cheap and free of cycles.
    from ..db import session_scope
    from ..models import ScoringEvent

    assert _q is not None
    while True:
        rec = _q.get()
        try:
            with session_scope() as db:
                db.add(ScoringEvent(**asdict(rec)))
        except Exception:  # noqa: BLE001 — a trace write must never crash the writer
            log.warning("failed to persist a scoring_event record", exc_info=True)
        finally:
            _q.task_done()


def flush(timeout: float = 5.0) -> None:
    """Block until queued records are written (or ``timeout`` elapses). Intended for
    tests and graceful shutdown; a no-op when nothing has been enqueued."""
    q = _q
    if q is None:
        return
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
