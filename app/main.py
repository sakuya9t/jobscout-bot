"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from .config import settings
from .db import init_db, session_scope
from .logging_config import configure_logging, get_logger
from .routers import auth, companies, interests, pages, positions, reports, resumes
from .services import evaluator, scheduler
from .services.ollama_client import get_client

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.secret_is_default:
        log.critical(
            "JOBSCOUT_SECRET_KEY is the built-in default — JWTs can be forged. "
            "Set a long random JOBSCOUT_SECRET_KEY before exposing this server."
        )
    init_db()
    scheduler.start()
    # Resume any evaluation backlog left unfinished by a prior process.
    evaluator.resume_pending_on_startup()
    yield
    scheduler.shutdown()
    evaluator.shutdown()


app = FastAPI(title="JobScout", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(resumes.router)
app.include_router(companies.router)
app.include_router(interests.router)
app.include_router(positions.router)
app.include_router(reports.router)
app.include_router(pages.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    db_ok = True
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    ollama = get_client().health()
    return {"status": "ok", "db": db_ok, "ollama": ollama, "ollama_ok": ollama == "ok"}
