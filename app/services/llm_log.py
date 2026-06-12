"""Off-the-hot-path persistence of Ollama request/response exchanges.

Each LLM call records one row in the ``llm_logs`` table instead of dumping the
full prompt + completion to stdout. Writing that row from the calling thread
would be a problem: during stage-2 scoring the matcher holds an *uncommitted*
SQLite write transaction (filter-rejects are flushed but not committed until the
batch ends), and SQLite allows only one writer — a second connection writing a
log row would block on the write lock up to ``busy_timeout`` and stall scoring.

So records are handed to a single daemon writer thread via an in-memory queue.
The writer drains them with its own short-lived sessions, naturally slotting its
writes between the matcher's per-batch commits; the calling thread never blocks.
Logs are best-effort: a full queue drops records and a failed write is warned and
skipped — neither ever breaks an LLM call."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

# Bound the buffer so a stuck/slow writer can't grow memory without limit; past
# this we drop records (with a warning) rather than block the caller.
_QUEUE_MAXSIZE = 2000

_q: queue.Queue["LlmLogRecord"] | None = None
_worker: threading.Thread | None = None
_lock = threading.Lock()


@dataclass
class LlmLogRecord:
    """A flattened Ollama exchange; field names match ``models.LlmLog`` columns."""

    correlation_id: str
    model: str | None
    url: str
    temperature: float | None
    response_format: str
    prompt_chars: int
    request_messages: str  # JSON-encoded [{role, content}, …]
    status: str  # "ok" | "error"
    elapsed_ms: int | None
    done_reason: str | None
    prompt_tokens: int | None
    eval_tokens: int | None
    response_content: str | None
    error_detail: str | None


def _ensure_worker() -> queue.Queue:
    """Lazily start the daemon writer (and its queue) on first use."""
    global _q, _worker
    with _lock:
        if _q is None:
            _q = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="llm-log-writer", daemon=True)
            _worker.start()
        return _q


def enqueue(record: LlmLogRecord) -> None:
    """Queue one exchange for persistence. Non-blocking; drops on a full queue."""
    q = _ensure_worker()
    try:
        q.put_nowait(record)
    except queue.Full:
        log.warning("llm_log queue full (%d); dropping a record", _QUEUE_MAXSIZE)


def _drain() -> None:
    # Imported lazily so this module stays import-cheap and free of cycles.
    from ..db import session_scope
    from ..models import LlmLog

    assert _q is not None
    while True:
        record = _q.get()
        try:
            with session_scope() as db:
                db.add(LlmLog(**asdict(record)))
        except Exception:  # noqa: BLE001 — a log write must never crash the writer
            log.warning("failed to persist an llm_log record", exc_info=True)
        finally:
            _q.task_done()


def flush(timeout: float = 5.0) -> None:
    """Block until queued records are written (or ``timeout`` elapses). Intended
    for tests and graceful shutdown; a no-op when nothing has been enqueued."""
    q = _q
    if q is None:
        return
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
