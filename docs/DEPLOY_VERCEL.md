# Deploying on Vercel: DoS protection & rate limiting

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
