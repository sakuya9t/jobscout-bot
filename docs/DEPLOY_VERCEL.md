# Deploying on Vercel

## Automated CI/CD: GitHub Actions → Supabase + Vercel

`.github/workflows/deploy.yml` ships the app on every push to `main` (and on demand
from the Actions tab → **Run workflow**). It runs three gated stages:

1. **test** — `pytest`.
2. **migrate** — `jobscout init-db` against the prod Supabase Postgres. This is
   `create_all` + preset seed: additive and idempotent, so it's safe on every deploy.
   It only creates tables and seeds the built-in company presets — **prod starts as a
   brand-new, empty database**; no local data is ever copied to it.
3. **deploy** — builds and deploys to Vercel with the Vercel CLI (`vercel pull`/
   `build`/`deploy --prebuilt --prod`).

### How the app runs on Vercel (serverless)

The app is normally a long-lived server, so three small adaptations let it run as
serverless functions:

- `api/index.py` re-exports the FastAPI `app`; `vercel.json` rewrites every path to it.
- The in-process scheduler and background worker threads are turned **off** on Vercel
  (`JOBSCOUT_SCHEDULER_ENABLED=0`, `JOBSCOUT_BACKGROUND_WORKERS=0`) since threads don't
  survive a function freeze.
- The daily scan runs instead via a **Vercel Cron** entry (in `vercel.json`) that hits
  `GET /api/cron/run-daily` once a day. That endpoint runs the same synchronous
  scrape+score as `jobscout run-daily`, authenticated by Vercel's `CRON_SECRET` bearer.

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
   | `SUPABASE_DB_URL` | the Supabase URL above (`?sslmode=require`) |
   | `VERCEL_TOKEN` | a Vercel access token |
   | `VERCEL_ORG_ID` | from step 3 |
   | `VERCEL_PROJECT_ID` | from step 3 |

   The prod secret key is **not** a GitHub secret — `init-db` doesn't use it, so it only
   lives in Vercel (next step).
5. **Set Vercel project env vars** (Project → Settings → Environment Variables, Production):
   | Variable | Value / note |
   | --- | --- |
   | `JOBSCOUT_DATABASE_URL` | same Supabase URL as `SUPABASE_DB_URL` |
   | `JOBSCOUT_SECRET_KEY` | the brand-new key from step 2 — unique to prod, never your local key |
   | `JOBSCOUT_DATA_DIR` | `/tmp/jobscout-data` — **required**: Vercel's FS is read-only except `/tmp`, and the app `mkdir`s this dir at import |
   | `CRON_SECRET` | long random string; Vercel signs cron calls with it and the endpoint checks it |
   | `JOBSCOUT_SCHEDULER_ENABLED` | `0` |
   | `JOBSCOUT_BACKGROUND_WORKERS` | `0` |
   | `JOBSCOUT_COOKIE_SECURE` | `1` (served over HTTPS) |
   | `JOBSCOUT_ADMIN_TOKEN` | optional — long random string to enable `/api/admin/*` |
   | `JOBSCOUT_REQUIRE_INVITE` | `1` (recommended for a public deploy) |

After that, **push to `main`** (or run the workflow manually) to deploy.

### Limitations of the minimal serverless deploy

- **Cron scan is time-bounded.** It runs inline within one invocation, capped by the
  function `maxDuration` (≤300s on Pro). A very large first-run backlog can time out;
  it picks up where it left off on the next run (scoring is idempotent and persisted).
- **Dashboard "Run scan now" scoring may not fully drain** on Vercel — its scrape
  enqueues to the background evaluator, which doesn't survive a function freeze. The
  daily cron (synchronous) is the reliable path. The crawl/scrape itself still runs.
- **Resume original-file preview isn't durable.** Uploaded files land in `/tmp`; the
  dashboard already falls back to the DB-stored extracted text, and matching/scoring
  use that text, so this is cosmetic. Supabase Storage is the real fix later.
- **Rate limiting is per-instance** on serverless — rely on the Vercel WAF (below).

For a deploy where the scheduler, workers, and rate limiter all work as-is, run the
long-lived server on a VM / container / Fly.io / Render instead (still pointed at
Supabase), and front it with a CDN/WAF.

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
