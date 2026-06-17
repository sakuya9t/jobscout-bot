"""Vercel Python entrypoint.

Vercel's @vercel/python runtime serves the ASGI callable named ``app`` from a file
under ``api/``; ``vercel.json`` rewrites every path here. We just re-export the real
FastAPI app. Note: Vercel's adapter does not run ASGI lifespan, so ``init_db()``
isn't called at boot here — that's intentional. CI runs ``jobscout init-db`` against
Supabase on deploy, and the scheduler/background workers are disabled on Vercel
(JOBSCOUT_SCHEDULER_ENABLED=0, JOBSCOUT_BACKGROUND_WORKERS=0); the daily scan runs via
the Vercel Cron entry that hits /api/cron/run-daily.
"""
from app.main import app  # noqa: F401
