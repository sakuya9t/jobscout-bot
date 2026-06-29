"""FastAPI application entrypoint."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
    cron,
    interests,
    llm_config,
    pages,
    positions,
    profile,
    reports,
    resumes,
    telegram_config,
)
from .services import evaluator, kit_worker, rescore_worker, scheduler
from .services.ollama_client import get_client

configure_logging()
log = get_logger(__name__)


def _resume_backlogs_on_startup() -> None:
    """Resume work interrupted by a restart, OFF the boot critical path (see lifespan).

    The scoring resume runs ``scoring_queue.reconcile`` — a sweep over every user's
    backlog that self-heals the queue. Running it inline in lifespan made every deploy
    block on a full reconcile, coupling deploy time and health to the queue's size;
    that's why it now runs on a background thread instead. Each helper already guards
    its own exceptions; the wrappers here are belt-and-suspenders so a failure can't
    kill the thread silently."""
    try:
        evaluator.resume_pending_on_startup()
    except Exception:
        log.exception("startup scoring resume failed")
    try:
        kit_worker.resume_pending_on_startup()
    except Exception:
        log.exception("startup kit resume failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.secret_is_default:
        log.critical(
            "JOBSCOUT_SECRET_KEY is the built-in default — JWTs can be forged. "
            "Set a long random JOBSCOUT_SECRET_KEY before exposing this server."
        )
    init_db()
    scheduler.start()
    # On the long-lived server (DigitalOcean App Platform) the bounded worker pool drains
    # scoring backlogs in-process, off the request path — this is the default
    # (JOBSCOUT_BACKGROUND_WORKERS_ENABLED=1). It can be gated off for a serverless host
    # whose threads don't survive a function freeze; there scoring is only enqueued
    # durably and an out-of-process drain (CLI/cron) consumes the queue instead.
    if settings.background_workers_enabled:
        # Resume interrupted backlogs (scoring reconcile + mid-generation kits) on a
        # background thread so the app reaches "ready" immediately. Deploy and reconcile
        # are now independent: a push no longer waits on a full queue reconcile to boot.
        threading.Thread(
            target=_resume_backlogs_on_startup, name="startup-resume", daemon=True
        ).start()
    yield
    scheduler.shutdown()
    evaluator.shutdown()
    kit_worker.shutdown()
    rescore_worker.shutdown()


app = FastAPI(title="JobScout", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Per-IP blanket limit on every request as a coarse DoS brake. The auth routes
    carry their own stricter per-route limits on top of this. ``/health`` is exempt so
    uptime checks aren't throttled, and ``/static/*`` is exempt so a single SPA page
    load (which fans out into many hashed asset requests) can't trip the limit. No-op
    when JOBSCOUT_RATE_LIMIT_ENABLED is off."""
    path = request.url.path
    if path != "/health" and not path.startswith("/static/"):
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
app.include_router(cron.router)
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


# ── Vue SPA (incremental migration) ──────────────────────────────────────────
# The built SPA lives in app/static (committed; Vite uses base="/static/"). It's
# mounted and routed only when a build is present, so tests/dev without a build are
# unaffected. Registered AFTER every router and pages.py, the /app catch-all serves
# index.html for client-side (history-mode) routes; scoping it to /app/* means it
# can't shadow /api/*, /health, or the still-server-rendered pages (/, /login,
# /positions/{id}, /companies/{id}). Broaden this at the final cutover.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if (_STATIC_DIR / "index.html").exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{rest:path}", include_in_schema=False)
    def spa_index(rest: str = "") -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")
