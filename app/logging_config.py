"""Central logging configuration.

Call ``configure_logging()`` once per process at startup (the FastAPI lifespan /
``app.main`` import, and the CLI entrypoint). Everywhere else, get a logger with
``get_logger(__name__)`` instead of calling ``logging.basicConfig`` — that keeps
handler/level/format setup in one place and makes it configurable from settings
(``JOBSCOUT_LOG_LEVEL``, ``JOBSCOUT_LOG_FILE``)."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings

# Include the thread name so the scoring worker pool is traceable: each web/cron
# drain worker logs under its own thread (evaluator*/scoring-*), which lets you follow
# one worker's claim -> drain -> finalize lifecycle (and spot a worker that exits while
# work is still pending) in interleaved output.
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"

_configured = False


def configure_logging(force: bool = False) -> None:
    """Install root handlers/level from settings. Idempotent: a second call is a
    no-op unless ``force`` is set (used by tests that change settings)."""
    global _configured
    if _configured and not force:
        return

    level = getattr(logging, settings.log_level.upper(), None)
    if not isinstance(level, int):
        level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
        )

    # force=True replaces any handlers a prior basicConfig/library left behind, so
    # our format/level/handlers win regardless of import order.
    logging.basicConfig(level=level, format=_LOG_FORMAT, handlers=handlers, force=True)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring logging lazily if an entrypoint hasn't
    already, so a logger is never left without handlers."""
    if not _configured:
        configure_logging()
    return logging.getLogger(name)
