# Change log

Project-level infrastructure history, newest first. Concise by design — see git for detail.

## 2026-06 — Decouple deploy from reconcile; load-balance the preset crawl
- Startup scoring resume (`scoring_queue.reconcile`) now runs on a background thread
  instead of inline in the app lifespan, so a push no longer blocks readiness on a full
  queue reconcile — deploy and reconcile are independent.
- The shared preset crawl is spread across `JOBSCOUT_SCRAPE_PRESET_SPREAD_MINUTES`
  (default 30) with `_JITTER` (default 0.3) rather than hitting every board back-to-back;
  the burst stays bounded as the preset list grows.

## 2026-06 — Database: Supabase → DigitalOcean Managed Postgres
- Production DB moved to DigitalOcean Managed Postgres, attached to the App Platform app
  (`JOBSCOUT_DATABASE_URL=${db.DATABASE_URL}`). App is DB-agnostic (SQLAlchemy/psycopg2),
  so no code-path change. Removed the Supabase CLI scaffolding.

## 2026-06 — Removed the scoring dispatch trigger
- Deleted `dispatch_scoring_run()` and the `scoring_dispatch_*` settings. It was the
  serverless workaround to kick the drain over HTTP; the long-lived server drains
  in-process on enqueue, so it had become a permanent no-op.

## 2026-06 — Compute: Vercel → DigitalOcean App Platform
- Moved off Vercel serverless to one long-lived `jobscout serve` process (web +
  in-process scoring drain + kit worker + daily scheduler).
- Removed the Vercel entrypoint (`api/index.py`, `vercel.json`), the deploy workflow
  (`deploy.yml`), and the GitHub Actions crons (`daily-scan.yml`, `scoring.yml`) — the
  in-process scheduler + worker pool replace them.
- Added PR preview apps (`.do/app.yaml` + `preview.yml`) and the host-independent CI test
  gate (`ci.yml`).
