# Change log

Project-level infrastructure history, newest first. Concise by design — see git for detail.

## 2026-06 — Stabilize match scoring (deterministic + derived headline)
- The same posting could swing 30+ points between identical scoring calls. Two fixes:
  (1) the scoring call now runs at `temperature=0` with a fixed `JOBSCOUT_SCORE_SEED`
  (default 11) so a single sample is reproducible — cover-letter/résumé generation is
  deliberately left stochastic; (2) the headline `match_score` is now derived in code as
  a fixed weighted average of the five rubric sub-scores (vertical 0.35 / skills 0.25 /
  seniority 0.20 / location 0.10 / preferences 0.10) instead of trusting the model's
  volatile free-form number — see `matcher._derive_match_score`.
- The model's own number is kept only as a fallback when the breakdown maps fewer than 4
  of the 5 aspects, and a `matches_requirements=False` verdict caps the headline at 40.

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
