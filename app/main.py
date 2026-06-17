"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import ratelimit
from .config import settings
from .db import init_db, session_scope
from .logging_config import configure_logging, get_logger
from .routers import (
    admin,
    applications,
    auth,
    companies,
    interests,
    llm_config,
    pages,
    positions,
    profile,
    reports,
    resumes,
    telegram_config,
)
from .services import evaluator, kit_worker, scheduler
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
    # Finish any application kit left mid-generation by a prior process.
    kit_worker.resume_pending_on_startup()
    yield
    scheduler.shutdown()
    evaluator.shutdown()
    kit_worker.shutdown()


app = FastAPI(title="JobScout", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Per-IP blanket limit on every request as a coarse DoS brake. The auth routes
    carry their own stricter per-route limits on top of this. ``/health`` is exempt so
    uptime checks aren't throttled. No-op when JOBSCOUT_RATE_LIMIT_ENABLED is off."""
    if request.url.path != "/health":
        allowed, retry_after = ratelimit.check_global(request)
        if not allowed:
            return JSONResponse(
                {"detail": "Too many requests. Please slow down and try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


app.include_router(auth.router)
app.include_router(resumes.router)
app.include_router(companies.router)
app.include_router(interests.router)
app.include_router(positions.router)
app.include_router(reports.router)
app.include_router(applications.router)
app.include_router(profile.router)
app.include_router(llm_config.router)
app.include_router(telegram_config.router)
app.include_router(admin.router)
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
