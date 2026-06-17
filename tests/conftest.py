"""Test config: point the app at a throwaway SQLite DB and a real secret BEFORE
any app module imports (which build the engine at import time), then give every
test a clean schema."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("JOBSCOUT_DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("JOBSCOUT_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
os.environ.setdefault("JOBSCOUT_SCHEDULER_ENABLED", "0")
# Keep the broad suite on open registration and no throttling; the invite/rate-limit
# tests (test_invite_ratelimit.py) flip these on explicitly per case.
os.environ.setdefault("JOBSCOUT_REQUIRE_INVITE", "0")
os.environ.setdefault("JOBSCOUT_RATE_LIMIT_ENABLED", "0")

import pytest  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import llm_log  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop + recreate all tables around each test for isolation."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    # The Ollama wire log is written by a background thread; drain it before the
    # next test so an in-flight record can't bleed into the next test's fresh DB.
    llm_log.flush()
    Base.metadata.drop_all(engine)
