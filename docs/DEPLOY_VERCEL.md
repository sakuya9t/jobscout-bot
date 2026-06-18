# Deploying on Vercel

## Automated CI/CD: GitHub Actions → Supabase + Vercel

`.github/workflows/deploy.yml` ships the app on every push to `main` (and on demand
from the Actions tab → **Run workflow**). It runs three gated stages:

1. **test** — `pytest`.
2. **migrate** — `jobscout init-db` then `jobscout encrypt-secrets` against the prod
   Supabase Postgres. `init-db` (`create_all` + preset seed) is additive and idempotent;
   it only creates tables and seeds the built-in company presets — **prod starts as a
   brand-new, empty database**, no local data is ever copied. `encrypt-secrets` then
   Fernet-encrypts the credential columns (`users.telegram_bot_token`,
   `llm_configs.api_key`) at rest; it's idempotent (a no-op once everything's encrypted)
   and needs `JOBSCOUT_SECRET_KEY`.
3. **deploy** — `vercel deploy --prod`. Vercel builds **remotely** (its image has
   `uv` + the Python toolchain), so there's no `vercel build`/`--prebuilt` step and the
   CI runner needs no Python build tools.

A second workflow, `.github/workflows/daily-scan.yml`, runs the daily scan on a cron
schedule (see below).

### How the app runs on Vercel (serverless)

The app is normally a long-lived server, so a few small adaptations let it run as
serverless functions:

- `api/index.py` re-exports the FastAPI `app`; `vercel.json` declares an
  `@vercel/python` build and routes every path to it.
- The in-process scheduler and background worker threads are turned **off** on Vercel
  (`JOBSCOUT_SCHEDULER_ENABLED=0`, `JOBSCOUT_BACKGROUND_WORKERS_ENABLED=0`) since threads
  don't survive a function freeze. (Note the `_ENABLED` suffix — a misspelled var is
  silently ignored, leaving workers **on**, which strands scoring-queue rows in
  `running` when the function freezes.)
- The **daily scan runs in GitHub Actions**, not on Vercel. A full scrape+score can't
  finish within a Vercel function's time limit (60s on Hobby; a single Ollama request
  alone can take up to `JOBSCOUT_OLLAMA_TIMEOUT`), so `.github/workflows/daily-scan.yml`
  runs `jobscout run-daily` on a runner with no time cap — the whole pipeline, including
  per-user Telegram reports, completes there. The `GET /api/cron/run-daily` endpoint
  still exists as an optional manual trigger (bearer-authed with `CRON_SECRET`), but it
  is not the scheduled path.

### First-time bootstrap (once)

Prod is a fresh, empty deployment with its **own** secret key — nothing from your
local environment is carried over.

1. **Create the Supabase project** and grab its connection string — use the
   *Session pooler* / direct connection (port 5432) with `?sslmode=require`. Leave the
   database empty; CI's `init-db` creates the schema + presets on the first deploy.
2. **Generate a brand-new prod secret key** (do **not** reuse your local one):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```
   This becomes `JOBSCOUT_SECRET_KEY` in Vercel below. Since prod starts empty, it has
   no encrypted rows or sessions tied to any other key — a unique key is exactly right.
3. **Link the Vercel project**: `vercel link` in the repo (creates `.vercel/`), or create
   it in the dashboard. Note its **Org ID** and **Project ID**.
4. **Set GitHub repo secrets** (Settings → Secrets and variables → Actions):
   | Secret | Value |
   | --- | --- |
   | `SUPABASE_DB_URL` | the Supabase URL above (`?sslmode=require`) — used by `init-db`, `encrypt-secrets`, **and** the daily scan |
   | `JOBSCOUT_SECRET_KEY` | the prod key from step 2 — **required** and **must be byte-for-byte identical to the value in Vercel**. It's the Fernet key for the encrypted credential columns, so `encrypt-secrets` and the daily scan can't decrypt what the web app wrote unless it matches |
   | `VERCEL_TOKEN` | a Vercel access token |
   | `VERCEL_ORG_ID` | from step 3 |
   | `VERCEL_PROJECT_ID` | from step 3 |
   | `GH_DISPATCH_TOKEN` | optional — a fine-grained PAT / GitHub App token with `actions: write`, used by the daily-scan and scoring workflows to fire `repository_dispatch` so scoring continues without waiting for the schedule (see "Continuous scoring"). Omit to run schedule-only |
5. **Set Vercel project env vars** (Project → Settings → Environment Variables, Production):
   | Variable | Value / note |
   | --- | --- |
   | `JOBSCOUT_DATABASE_URL` | same Supabase URL as `SUPABASE_DB_URL` |
   | `JOBSCOUT_SECRET_KEY` | the brand-new key from step 2 — unique to prod, never your local key, and **identical to the GitHub secret of the same name** (it's the at-rest encryption key for the Telegram-token / LLM-key columns) |
   | `JOBSCOUT_DATA_DIR` | `/tmp/jobscout-data` — **required**: Vercel's FS is read-only except `/tmp`, and the app `mkdir`s this dir at import |
   | `CRON_SECRET` | optional — long random string gating the `GET /api/cron/run-daily` and `POST /api/cron/run-scoring` HTTP triggers. Not required for the GitHub Actions crons; set it for the HTTP triggers / local testing |
   | `JOBSCOUT_SCHEDULER_ENABLED` | `0` |
   | `JOBSCOUT_BACKGROUND_WORKERS_ENABLED` | `0` — threads don't survive a function freeze; scoring is enqueued and drained by the `run-scoring` GitHub Actions workflow instead |
   | `JOBSCOUT_SCORING_DISPATCH_URL` | optional but recommended — `https://api.github.com/repos/<owner>/<repo>/dispatches`. When the dashboard enqueues a scan, the app fires this so the Scoring workflow drains **now** instead of waiting for the schedule (see "Continuous scoring" below). Omit to rely on the schedule only |
   | `JOBSCOUT_SCORING_DISPATCH_TOKEN` | required iff the URL is set — a fine-grained PAT / GitHub App token with `actions: write` (the default `GITHUB_TOKEN` can't trigger a workflow) |
   | `JOBSCOUT_COOKIE_SECURE` | `1` (served over HTTPS) |
   | `JOBSCOUT_ADMIN_TOKEN` | optional — long random string to enable `/api/admin/*` |
   | `JOBSCOUT_REQUIRE_INVITE` | `1` (recommended for a public deploy) |

After that, **push to `main`** (or run the workflow manually) to deploy.

### Limitations of the minimal serverless deploy

- **Scrape and scoring both run in GitHub Actions, not on Vercel.** Vercel's function
  time limit (60s on Hobby) can't fit them, so they live in two crons with no time cap:
  `.github/workflows/daily-scan.yml` (`jobscout run-daily`, scrape only, `0 8 * * *`) and
  `.github/workflows/scoring.yml` (`jobscout run-scoring`, drains the matching backlog via
  the bounded queue, every 4h) — both **UTC**; change the schedules there, or trigger an
  ad-hoc run from the Actions tab → **Run workflow**.
- **Dashboard "Run scan now" scoring is queued, not inline,** on Vercel — its scrape runs
  synchronously, then it **enqueues** the user to the durable scoring queue (background
  threads don't survive a function freeze) and fires the dispatch trigger (below). The
  `run-scoring` GitHub Actions workflow drains the queue reliably; the dashboard shows
  matches from the DB as they're scored. So a manual refresh's results appear once the
  scoring run picks it up rather than within the request.
- **Resume original-file preview isn't durable.** Uploaded files land in `/tmp`; the
  dashboard already falls back to the DB-stored extracted text, and matching/scoring
  use that text, so this is cosmetic. Supabase Storage is the real fix later.
- **Rate limiting is per-instance** on serverless — rely on the Vercel WAF (below).
- **Database connections go through Supabase's pooler.** The app uses SQLAlchemy
  `NullPool` on Postgres (no in-process connection pool), so serverless instances and
  the daily-scan / run-scoring jobs don't hoard idle connections and exhaust the pooler's
  per-client cap. The scoring cron additionally bounds how many users score at once
  (`JOBSCOUT_SCORING_MAX_CONCURRENCY`), so a backlog spike can't blow the connection cap.
  Use the **session pooler** URL (port `5432`) as documented; if you later run
  enough concurrency to still hit "max clients reached in session mode", switch the
  connection string to the **transaction pooler** (port `6543`), which multiplexes and
  raises the ceiling — change it in both `SUPABASE_DB_URL` and `JOBSCOUT_DATABASE_URL`.

For a deploy where the scheduler, workers, and rate limiter all work as-is, run the
long-lived server on a VM / container / Fly.io / Render instead (still pointed at
Supabase), and front it with a CDN/WAF.

### Continuous scoring (event-driven drain)

Without extra config the queue only drains on the scoring schedule (every 4h), so new
work can sit idle for hours. To make it drain **as soon as work appears and keep going
until the backlog is empty**, the app fires a lightweight trigger instead of waiting:

```
producer enqueues  ──►  app fires repository_dispatch ("score")  ──►  Scoring workflow drains
        ▲                                                                      │
        └──────────────  re-fires while the per-run budget leaves a backlog  ◄─┘
```

- **Producers** that publish work: the dashboard "Run scan now" (`/api/run`), and the
  daily scan (`run-daily` reconciles + fires after scraping).
- **Consumer**: the `scoring.yml` workflow listens on `repository_dispatch: [score]` and
  runs `jobscout run-scoring`, which drains the queue within
  `JOBSCOUT_SCORING_RUN_BUDGET_SECONDS`. A single huge backlog now **stops cleanly at the
  budget and re-arms** (per-user time budget) instead of being killed by the job timeout
  and stranding a row in `running`; if work remains it re-fires the trigger, so runs go
  back-to-back until empty, then idle.
- **Setup**: set `JOBSCOUT_SCORING_DISPATCH_URL` +
  `JOBSCOUT_SCORING_DISPATCH_TOKEN` on Vercel, and the `GH_DISPATCH_TOKEN` GitHub secret
  (so the workflows can re-fire). Leave them unset to fall back to schedule-only.

**Run/test it locally** — no Supabase/GitHub needed. The trigger is plain HTTP, so point
it at the app's own consumer endpoint:

```bash
export CRON_SECRET=dev-secret
export JOBSCOUT_BACKGROUND_WORKERS_ENABLED=0          # simulate serverless (no in-process drain)
export JOBSCOUT_SCORING_DISPATCH_URL=http://localhost:8000/api/cron/run-scoring
export JOBSCOUT_SCORING_DISPATCH_TOKEN=$CRON_SECRET
jobscout serve                                        # in one shell
# enqueue work (dashboard "Run scan now", or run-daily), then watch it drain:
curl -X POST -H "Authorization: Bearer $CRON_SECRET" http://localhost:8000/api/cron/run-scoring
```

On a long-lived server (default `JOBSCOUT_BACKGROUND_WORKERS_ENABLED=1`) you don't need any
of this — the in-process evaluator drains the queue the moment work is enqueued, and the
dispatch trigger is a no-op.

---

# DoS protection & rate limiting

## Does Vercel already protect against DoS? — Yes, partly, and some of it is free.

**Free on every plan (no setup):**
- **Automatic DDoS mitigation** at the network and application layers (L3/L4 and L7).
  Vercel absorbs and mitigates volumetric attacks at the platform edge before they
  reach your function.
- The **Vercel WAF** (Web Application Firewall) is available on all plans: custom rules,
  IP/CIDR/geo blocking, and **Attack Challenge Mode** (a one-toggle JS/proof-of-work
  challenge that sheds bot/DoS traffic during an incident).
- **Firewall-mitigated traffic is not billed** — requests denied, challenged, or
  rate-limited by the WAF don't incur CDN-request or fast-data-transfer charges, so
  turning these on doesn't make an attack more expensive for you.

**Paid add-on features (the real per-endpoint shield):**
- **WAF rate-limiting rules** — set request limits per path/identifier (e.g. cap
  `/api/auth/login`) directly at the edge.
- **Managed rulesets** — predefined protections like the OWASP Top 10.

These are configured in the Vercel dashboard (Project → Firewall), not in `vercel.json`,
and propagate globally in well under a second. They're the recommended **front line** for
DoS because they act at the edge, before any of your code runs.

> Pricing and exact plan gating change over time — confirm against
> <https://vercel.com/docs/vercel-firewall/vercel-waf/usage-and-pricing> before relying on it.

## Why this app also ships its own rate limiter

`app/ratelimit.py` enforces in-process per-IP limits (a global blanket plus stricter
caps on `/api/auth/login` and `/api/auth/register`). That's the right tool for the
**current single-process uvicorn / VM deployment** and for defense-in-depth, and it
works with no external service.

**Important caveat for Vercel:** the in-memory limiter's counters live inside one process.
On a multi-instance or serverless deploy, each instance has its own counters, so the
effective limit is roughly `(your limit) × (number of live instances)` — it slows abuse
but is not a hard cap. On Vercel, treat the **WAF rate-limit rules as the enforcement
layer** and the in-app limiter as a backstop.

## Architectural notes before moving this app to Vercel serverless

This app was built as a long-lived server, and a few pieces don't fit the serverless model:

- **In-process scheduler** (`apscheduler`, `app/services/scheduler.py`) won't run on
  serverless. Replace it with a **Vercel Cron** entry that calls an endpoint which runs
  `jobscout run-daily`'s work.
- **Background worker threads** (`evaluator`, `kit_worker`) don't survive past a single
  request/invocation. They'd need a queue or to run synchronously within a request.
- **SQLite** has no durable local filesystem on serverless — already addressed: point
  `JOBSCOUT_DATABASE_URL` at **Supabase Postgres** (see `jobscout migrate-db`).
- Because counters aren't shared across instances, **rate limiting falls to the WAF** as
  noted above.

If you instead deploy the long-lived server (a VM, container, Fly.io, Render, etc.), the
in-app scheduler, workers, and rate limiter all work as-is — and you can still front it
with a CDN/WAF for edge DoS protection.

## Sources
- Vercel WAF usage & pricing — <https://vercel.com/docs/vercel-firewall/vercel-waf/usage-and-pricing>
- DDoS mitigation — <https://vercel.com/docs/vercel-firewall/ddos-mitigation>
- WAF upgrade (persistent actions, rate limiting, API control) — <https://vercel.com/blog/vercel-waf-upgrade-brings-persistent-actions-rate-limiting-and-api-control>
- Firewall-mitigated traffic is free — <https://vercel.com/changelog/web-application-firewall-mitigated-traffic-is-free-on-vercel>
