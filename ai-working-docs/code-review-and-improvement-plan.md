# JobScout — Remaining Work

Last updated: 2026-06-09 (after the fourth review pass; everything previously
flagged and fixed lives in git history — do not re-flag it). Audience: AI
agents (Opus etc.) continuing work on this repo. Read `docs/DESIGN.md` +
`docs/SPEC.md` first. Companion doc:
`ai-working-docs/career-ops-research-summary.md` (what to port from
`santifer/career-ops`).

Verified state: `pytest -q` → 25 passed, 1 skipped (live-network). Four review
passes are done; no known correctness bugs remain. Everything below is
structural or accepted-risk work, in priority order.

## Tier 1 — structural (needs Alembic first)

- **P2-3: adopt Alembic.** `create_all` only today. Required *before* either
  schema change below. First real migration lands with P1-7.
- **P1-7: async runs.** `POST /api/run` (`app/routers/reports.py:17`) and MCP
  `run_daily_scan` block for the entire pipeline (minutes when the LLM is
  slow; proxies/browsers time out). Add a `runs` table (id, user_id, status,
  started_at, finished_at, summary JSON) + background thread/`BackgroundTasks`;
  `POST /api/run` returns the run id immediately, `GET /api/runs/{id}` polls.
  Keep a synchronous mode for the CLI. Natural seam for later queue adoption
  (arq/huey) — don't add a queue now.
  - **Fold in N-4 (concurrent-run guard):** there is no guard today —
    double-clicking "Run scan now" (or cron + manual overlap) runs two
    pipelines for the same user, which can insert the same
    `(company_id, external_id)` or `(position, interest)` pair →
    `IntegrityError` → 500 mid-run. The runs table should refuse (or queue)
    when a run is already in_progress.
- **P1-8: durable MCP tokens.** `jobscout token` mints the same 7-day JWT as
  browser sessions (`app/auth.py`), so MCP configs break a week later with an
  opaque "Unauthorized". Add an `api_tokens` table (random opaque token,
  hashed, user_id, label, created_at, revoked) checked by
  `authenticate_token` first, JWT fallback second; `jobscout token` mints the
  long-lived kind. Needs Alembic.

## Tier 2 — small / accepted-risk items

- **N-9b (accepted risk): SSRF DNS-rebinding TOCTOU.** `_validate_url`
  resolves DNS, then `httpx` resolves again at fetch time — a hostile resolver
  could return a public IP for the check and a private one for the fetch. A
  full fix pins the resolved IP via a custom transport. Documented as accepted
  at this deployment size; revisit if exposed to untrusted multi-tenant users.
- **P1-2 remainder: no login rate limiting** (`/api/auth/login` → offline
  brute force). `slowapi` is a one-file addition; only matters once deployed
  beyond personal use.

## Tier 3 — design improvements (not blocking)

- **Step 8: provider registry + more ATS adapters.** Port the
  `detect()`/`fetch()` provider-registry structure and the Workable /
  SmartRecruiters / Recruitee adapters from career-ops (public no-auth APIs;
  big coverage win, zero LLM cost). See the research summary.
- **N-14: position staleness/liveness.** Closed postings are never marked
  dead and stay in reports indefinitely; ATS adapters could diff current
  external_ids per scrape almost for free. Pairs well with the registry port
  (career-ops has liveness patterns).
- **P2-1:** positions duplicated per user — two users watching the same
  company scrape and store everything twice. Acceptable ≤10 users; then
  either dedupe scrape work per `(ats_type, token)` within one
  `run_for_all_users` pass (easy, no schema change) or split `Company` into a
  global board + per-user watch rows (needs Alembic).
- **P2-4:** reporter loads every passed match then filters in Python
  (`app/services/reporter.py`); push the per-interest threshold join into SQL
  when row counts matter.
- **P2-5:** resume truncation — hard cut at 6 000 chars
  (`app/services/matcher.py`) silently drops the tail of long resumes.
  Warn at upload time when extracted text exceeds the budget; later, LLM-
  summarize the overflow once.
- **P2-6:** settings singleton at import time makes env overrides in tests
  awkward; touch only if test friction grows.
- **P2-7:** in-process APScheduler + `uvicorn --workers N` would run the
  daily job N times. Docs assume one process — add a loud README note (the
  cron path already covers serious deployments).
- **Test depth:** matcher HTML-junk end-to-end, reporter `on_date` range
  across a simulated TZ, a `runs`-table state test once P1-7 lands.
- Then P2 features per `docs/PLAN.md` (artifacts/writer.py).

## Reuse map — don't hand-roll these

| Concern | Current | Use instead | Why |
|---|---|---|---|
| Migrations | none (`create_all`) | `alembic` | required before runs/api_tokens tables |
| Background runs | blocking request | stdlib thread + `runs` table now; `arq`/`huey` only if scale demands | P1-7; avoid queue infra at this size |
| Rate limiting | none | `slowapi` | only when deployed multi-user |
| More ATS coverage | 3 adapters + HTML | port Workable / SmartRecruiters / Recruitee from career-ops | public no-auth APIs; zero LLM cost |
| Scraper structure | if/elif dispatch | provider registry (`detect()`/`fetch()` protocol), ported from career-ops | makes adapter additions one-file changes |
| Big-board scraping (LinkedIn/Indeed) | n/a | `python-jobspy` — optional, different niche | only if "watch a company" expands to "search all boards" |
| JS-heavy pages | unimplemented `use_browser` flag | Playwright (already an optional extra) + career-ops liveness patterns | the flag and TODOs already point here |

## Recommended work order

Each step is independently shippable; verify after each.

1. **Alembic (P2-3)**, then **P1-7** runs table + background execution +
   in-progress guard (N-4), then **P1-8** api_tokens. One migration chain —
   this is the bulk of the remaining work.
2. **Provider registry + Workable/SmartRecruiters/Recruitee** (+ N-14
   liveness while in there).
3. P2-1/4/5/7, login rate-limiting, and `docs/PLAN.md` P2 features as demand
   appears.
