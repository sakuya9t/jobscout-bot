# DigitalOcean App Platform migration plan

Goal: move the app off Vercel onto **DigitalOcean App Platform**, where the single
server process runs web + background scoring + kit generation + the daily scheduler
in-process — the shape the app was built for — and the GitHub Actions cron + dispatch
plumbing that only existed to fake a persistent process on Vercel can be deleted.

## Decision

- **No RabbitMQ.** The durable queue already exists as DB state (`scoring_jobs`,
  claimed with `SELECT … FOR UPDATE SKIP LOCKED`). A broker adds a stateful service to
  run/monitor for zero benefit at this scale.
- **The app is server-native.** Defaults are `background_workers_enabled=True` +
  `scheduler_enabled=True`: in-process drain, in-process kit worker, in-process daily
  scheduler. Vercel was the awkward fit — the GitHub Actions cron + `repository_dispatch`
  + dispatch tokens exist *only* to simulate a long-lived process.
- **Target = DigitalOcean App Platform**, one `jobscout serve` with background workers +
  scheduler on. It matches the native design and lets us **delete** `scoring.yml`,
  `daily-scan.yml`, the dispatch mechanism, and the dispatch env/secret.
- **Keep Supabase** as the database (DB + `scoring_jobs` queue) — this migration is
  compute-only; the DB is not moving.

## Component inventory

| Component | Today | Kind of work | Needs a long-lived host? |
|---|---|---|---|
| Web app (dashboard, API, auth) | Vercel | request/response | No — but runs fine in-process |
| **Scoring drain** (`run-scoring`) | GitHub Actions + dispatch | long batch, LLM | **Yes** → in-process on DO |
| **Kit generation** (`kit_worker`) | in-process on Vercel (unreliable) | medium batch, LLM | **Yes** → reliable on DO |
| Daily scrape (`run-daily`) | GitHub Actions (`daily-scan.yml`) | bounded batch | → in-process scheduler on DO |
| In-process scheduler (`scheduler.py`) | disabled on Vercel | cron | Enabled once the server runs |
| Telegram daily reports (`send_daily_reports`) | **unwired (no caller)** | push | wherever the daily job runs |
| Queue state (`scoring_jobs`) | Supabase | durable state | keep (no broker) |

## Why no RabbitMQ

The only thing a broker would buy here is push-vs-poll latency (~seconds). At this scale
(N users, bounded LLM batches, low frequency) that's not worth a second stateful service.
Escalation ladder if we ever outgrow it:

1. In-process handoff (have it) →
2. Postgres claim queue (have it: `scoring_jobs` + SKIP LOCKED) →
3. Postgres `LISTEN/NOTIFY` (push, still no broker) →
4. A broker (RabbitMQ/Redis) — only at real multi-node, high-throughput scale.

We're at level 2 and it's sufficient.

## Why DigitalOcean App Platform

- **Shape:** the single-process native app, with App Platform providing managed HTTPS,
  git-push deploys, an env UI, logs, and auto-restart. Start command: `jobscout serve`
  bound to `$PORT`. Optionally split into a `web` service + a `worker` service from the
  same repo (different run commands) later for isolation.
- **Simplest end-to-end** — architecture *and* ops — and DigitalOcean is a large,
  established public company, so low vanish risk. The `scoring_jobs` queue stays (it
  throttles DB concurrency) but the *trigger* plumbing vanishes: `ensure_running` kicks
  the in-process drain directly.
- **Cost:** ~$5/mo Basic per service — includes managed HTTPS, deploys, and logs.
- **Caveat:** single region, no edge CDN (fine for an authed dashboard); we leave Vercel.

## What gets deleted / what's kept

Delete or disable once the DO server is live:

- ✅ `.github/workflows/scoring.yml` — scoring drain (now in-process) — *removed earlier*
- ✅ `.github/workflows/daily-scan.yml` — daily scrape (now the in-process scheduler)
- ✅ `.github/workflows/deploy.yml` + `vercel.json` + `api/index.py` — the Vercel deploy
  pipeline and serverless entrypoint. Schema now self-applies at boot (`init_db()` in the
  app lifespan; Vercel's adapter skipped lifespan, which is why a separate `migrate`
  stage existed). `encrypt-secrets` was a one-time migration (prod already encrypted;
  new writes are encrypted at write time) — the CLI command stays if ever needed.
- ⏳ Dispatch env on the host: leave `JOBSCOUT_SCORING_DISPATCH_URL/_TOKEN` **unset** *(env, external)*
- ⏳ GitHub secret `GH_DISPATCH_TOKEN` *(GitHub settings, external)*
- ⏳ `services/dispatch.py` is inert (unset URL = no-op). **Not yet removed** — still
  wired into `cron.py`/`cli.py`/`evaluator.py` with ~6 dedicated tests, and it's still
  handy for the local drain loop. Remove as a focused follow-up if desired.

Keep:

- Supabase (DB + `scoring_jobs` queue)
- The **test** CI gate (`ci.yml`) — host-independent; DigitalOcean's GitHub auto-deploy
  is the deploy step (no in-repo deploy workflow)

## Migration steps

**Phase 0 — prep (no prod impact)**
1. Create a DigitalOcean App from the GitHub repo (auto-deploys on push).
2. Set env: `JOBSCOUT_DATABASE_URL` (Supabase pooler URL), `JOBSCOUT_SECRET_KEY`
   (same value Vercel uses), any LLM defaults. Leave dispatch vars unset.
3. Keep defaults `background_workers_enabled=1`, `scheduler_enabled=1`.
4. Start command: `jobscout serve` bound to `$PORT`. Confirm `/` + health.

**Phase 1 — parallel run (de-risk)**
5. Deploy to DigitalOcean while Vercel + GitHub Actions stay live.
   *Safe to run both:* the queue uses `FOR UPDATE SKIP LOCKED` + idempotent upserts, so
   the App Platform in-process drain and the GHA drain can't double-score.
6. On the DigitalOcean app URL: upload a resume, click **Recalculate**, watch it score
   in-process (App Platform runtime logs, or `jobscout queue-log`).

**Phase 2 — cut over** *(done, except the external steps noted)*
7. ✅ Vercel app stopped; DigitalOcean serves traffic. *(Repoint DNS to the DO
   domain/subdomain if not already — external.)*
8. ✅ `scoring.yml` + `daily-scan.yml` deleted; `deploy.yml` (Vercel pipeline) deleted —
   DigitalOcean's GitHub auto-deploy is the deploy step.
9. ⏳ Remove `GH_DISPATCH_TOKEN` (GitHub repo secret) and leave the dispatch env vars
   unset on the host — **external**, do this in GitHub/DO settings.

**Phase 3 — cleanup** *(done in-repo, except the optional dispatch removal)*
10. ✅ Vercel artifacts removed (`vercel.json`, `api/index.py`, local `.vercel/`).
    ✅ README + docs topology updated (`docs/DEPLOY_VERCEL.md` marked deprecated,
    pointing here). ⏳ `dispatch.py` removal deferred (see *What gets deleted*).

**Rollback:** re-add the deleted workflows + the Vercel entrypoint (`vercel.json` +
`api/index.py`) from git history and re-link Vercel. The DB queue is untouched, so no
work is lost.

## Data durability & backups

- Data lives in Supabase Postgres, **not** on the App Platform host — so the compute
  provider dying is not data loss. Choose compute for uptime; choose the DB + backups for
  durability.
- Supabase is fine on **Pro** (PITR); the **free** tier pauses after ~1 week idle and has
  limited backups. If durability matters, stay on Pro.
- **Non-negotiable regardless:** a scheduled `pg_dump` → object storage (S3 / R2 / B2).
  Postgres is portable, so with your own dumps no vendor can lose or hold your data — this
  matters more than which compute provider you pick.

## Open items to fix during the move (host-independent)

- **Telegram reports appear unwired:** `send_daily_reports` has no caller. Whatever runs
  the daily job (in-process `scheduler.daily_job`) should call it, else reports never go
  out. The in-process scheduler is the natural home once the server runs.
- **Kit generation reliability:** `kit_worker` submits to a thread pool with no
  queue/dispatch fallback → best-effort on Vercel. The long-lived DO server fixes it for
  free via `background_workers_enabled=1` + `resume_pending_on_startup`.
