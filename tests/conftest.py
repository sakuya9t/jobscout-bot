"""Test config: point the app at a throwaway SQLite DB and a real secret BEFORE
any app module imports (which build the engine at import time), then give every
test a clean schema."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("JOBSCOUT_DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("JOBSCOUT_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
os.environ.setdefault("JOBSCOUT_SCHEDULER_ENABLED", "0")
os.environ.setdefault("JOBSCOUT_TELEGRAM_BOT_TOKEN", "")

import pytest  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop + recreate all tables around each test for isolation."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
