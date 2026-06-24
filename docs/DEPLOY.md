# Deploying on DigitalOcean App Platform

## Topology

One long-lived `jobscout serve` process runs everything in-process — web (FastAPI/
uvicorn), the scoring drain, the kit worker, and the daily scheduler. This is the app's
native shape: no external cron and no queue broker.

- **Production** is a control-panel App Platform app that auto-deploys `main` via DO's
  native GitHub integration. DO's Python buildpack installs from `requirements.txt`; the
  start command comes from the `Procfile` (`web: python -m app.cli serve …`). Tables
  self-create on boot (`init_db()` in the app lifespan).
- **Database** is DigitalOcean Managed Postgres attached to the app, so
  `JOBSCOUT_DATABASE_URL=${db.DATABASE_URL}` (the bindable var; attaching also auto-adds
  the app as a trusted source). See the README "Production database" section for the
  schema copy / cutover.
- **PR previews** are ephemeral per-PR apps on throwaway SQLite — see
  [preview-deploys.md](preview-deploys.md).
- **CI** ([ci.md](ci.md)) is the test gate; required checks block merges, so only tested
  code reaches the deploy branch.

## Production environment variables

Set under App Platform → Settings → Environment Variables (Production):

| Variable | Value / note |
|---|---|
| `JOBSCOUT_SECRET_KEY` | Long random string. Signs JWT sessions **and** is the Fernet key for the encrypted credential columns (Telegram token, LLM key) — rotating it strands those rows, so set once. |
| `JOBSCOUT_DATABASE_URL` | `${db.DATABASE_URL}` — bindable from the attached Managed Postgres. |
| `JOBSCOUT_COOKIE_SECURE` | `1` (served over HTTPS). |
| `JOBSCOUT_REQUIRE_INVITE` | `1` (recommended for a public deploy). |
| `JOBSCOUT_ADMIN_TOKEN` | Optional — long random string to enable `/api/admin/*`. |
| `CRON_SECRET` | Optional — bearer token gating the `GET /api/cron/run-daily` and `POST /api/cron/run-scoring` HTTP triggers (handy for an external scheduler; not needed, since the in-process scheduler/drain run by default). |

Defaults that matter: `JOBSCOUT_SCHEDULER_ENABLED=1` and
`JOBSCOUT_BACKGROUND_WORKERS_ENABLED=1` (both on) — the long-lived server runs the daily
scrape and drains the scoring queue in-process.

## How scoring and the daily scrape run

- The **daily scrape** fires from the in-process APScheduler
  (`services/scheduler.py`) at `JOBSCOUT_DAILY_RUN_HOUR`/`_MINUTE`, then enqueues users
  with a backlog.
- **Scoring drains in-process**: `evaluator.ensure_draining()` spawns a bounded worker
  pool (`JOBSCOUT_SCORING_MAX_CONCURRENCY`) the moment work is enqueued; the `scoring_jobs`
  queue (`SELECT … FOR UPDATE SKIP LOCKED`) makes claims atomic and caps concurrent DB
  connections.
- The `/api/cron/*` endpoints remain as optional authenticated manual triggers (and
  `jobscout run-scoring` as an out-of-process drain). There is no GitHub Actions cron.

## Caveats

- **Resume file storage is ephemeral.** Uploaded resumes land on the instance disk and
  are lost on redeploy; the app falls back to the DB-stored extracted text (which
  matching/scoring use), so this is cosmetic. Object storage (e.g. DO Spaces) is the
  durable fix if original-file download matters.
- **Single instance assumed.** The scheduler runs per instance, so `instance_count > 1`
  would double the daily scrape (the queue's `SKIP LOCKED` prevents double-*scoring*, not
  double-*scraping*). Keep one web instance, or split a dedicated scheduler/worker
  instance, before scaling out.
- **DB connections use SQLAlchemy `NullPool`** (one connection per active checkout, no
  in-process pool). Keep `JOBSCOUT_SCORING_MAX_CONCURRENCY` under the Managed Postgres
  connection limit, which is shared with web requests.

## Rate limiting & DoS

`app/ratelimit.py` enforces in-process per-IP limits (a global blanket plus stricter caps
on `/api/auth/login` and `/api/auth/register`). On a single-instance deploy that's a real
cap; behind multiple instances it's per-instance. App Platform provides baseline platform
DDoS protection — for a hardened public deploy, front the app with a CDN/WAF (e.g.
Cloudflare) as the edge shield and treat the in-app limiter as defense-in-depth.

## Backups

Managed Postgres has daily backups + PITR on paid plans. Keep a provider-portable safety
net regardless: a scheduled `pg_dump` → object storage (S3 / R2 / Spaces).
