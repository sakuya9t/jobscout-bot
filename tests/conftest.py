"""Test config: point the app at a throwaway SQLite DB and a real secret BEFORE
any app module imports (which build the engine at import time), then give every
test a clean schema."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("JOBSCOUT_DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("JOBSCOUT_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
os.environ.setdefault("JOBSCOUT_SCHEDULER_ENABLED", "0")
# Keep scoring deterministic: don't spawn the background drain pool (its persistent
# workers would outlive a test and hit the next test's freshly-dropped tables). Tests
# exercise draining explicitly via evaluator._run / evaluator.drain_queue instead.
os.environ.setdefault("JOBSCOUT_BACKGROUND_WORKERS_ENABLED", "0")
# Keep the broad suite on open registration and no throttling; the invite/rate-limit
# tests (test_invite_ratelimit.py) flip these on explicitly per case.
os.environ.setdefault("JOBSCOUT_REQUIRE_INVITE", "0")
os.environ.setdefault("JOBSCOUT_RATE_LIMIT_ENABLED", "0")
# Crawl the preset catalog back-to-back in tests — the production crawl spreads
# companies over scrape_preset_spread_minutes, which would make the crawl tests sleep.
os.environ.setdefault("JOBSCOUT_SCRAPE_PRESET_SPREAD_MINUTES", "0")

import pytest  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import llm_log, scoring_log  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop + recreate all tables around each test for isolation."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    # The Ollama wire log and the scoring-queue trace are written by background
    # threads; drain them before the next test so an in-flight record can't bleed
    # into the next test's freshly-dropped tables.
    llm_log.flush()
    scoring_log.flush()
    Base.metadata.drop_all(engine)
