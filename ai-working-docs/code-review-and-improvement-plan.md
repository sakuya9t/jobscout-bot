# JobScout — Code Review & Improvement Plan

Date: 2026-06-09. Reviewed: full `app/` tree, docs, tests, installed venv.
Audience: AI agents (Opus etc.) continuing work on this repo. Read
`docs/DESIGN.md` + `docs/SPEC.md` first; this doc tells you what is *wrong or
weak* in the current implementation and the highest-leverage order to fix it.
Companion doc: `ai-working-docs/career-ops-research-summary.md` (what to port
from `santifer/career-ops`).

Verified state: `pytest -q` → 7/7 pass; `import app.main` OK; **but the app is
broken at runtime — see P0-1.** Tests pass because nothing exercises hashing or
the pipeline.

---

## 1. Verdict in one paragraph

The architecture is sound and matches the docs: clean service layer, ATS-first
scraping, per-user scoping everywhere, LLM cost control via dedup + pre-filter,
and the same services reused by HTTP/MCP/CLI/scheduler. The defects are
concentrated in (a) a hard runtime breakage in auth, (b) silent failure modes
that make the product *look* like it works while producing nothing, (c)
SSRF/network-trust issues inherent to accepting user URLs in a multi-user web
service, and (d) SQLite/timezone/concurrency details that will bite the first
real deployment. None require redesign; all are fixable in place.

## 2. P0 — broken right now (fix before anything else)

### P0-1. passlib 1.7.4 is incompatible with bcrypt 5.x → register/login 500s
Confirmed by execution in this venv:
`pwd_context.hash()` raises (passlib reads `bcrypt.__about__`, removed in
bcrypt>=4.1; then its self-test trips bcrypt 5's 72-byte ValueError).
Every call to `hash_password`/`verify_password` in `app/auth.py:16-24` fails →
**no user can register or log in.**

Fix (recommended): drop passlib entirely, use `bcrypt` directly (~12 lines):
```python
import bcrypt
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode()[:72], bcrypt.gensalt()).decode()
def verify_password(p: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode()[:72], hashed.encode())
    except ValueError:
        return False
```
Remove `passlib[bcrypt]` from `pyproject.toml`, add `bcrypt>=4.1`. (Alternative:
`pwdlib[bcrypt]` — the maintained passlib successor — if algorithm agility is
wanted. Do NOT pin `bcrypt<4.1`; passlib is unmaintained since 2020.)
Add a regression test: register → login roundtrip via `TestClient`.

### P0-2. LLM scoring failures are invisible to the user
`_score_position` (`app/services/matcher.py:166-169`) catches `OllamaError`,
logs a warning, returns `None` — **nothing is appended to `RunResult.errors`.**
With a wrong/missing Ollama API key, a run reports
`new_positions: N, scored: 0, errors: []` and the dashboard shows "no matches"
with zero indication anything failed. Compounding it, `OllamaClient.health()`
(`app/services/ollama_client.py:99-105`) treats any status `<500` as healthy, so
a 401 (bad key) reports "ollama: reachable" in `/health` and `jobscout health`.

Fix:
- Append a summarized error per failure to `res.errors` (dedupe identical
  messages; cap at ~5 with a "+N more" tail so one outage doesn't produce 200
  lines).
- `health()` should distinguish `ok / unauthorized / unreachable` (Cloud: an
  authed `GET /api/tags` returns 401 with a bad key — surface that).

## 3. P1 — security (multi-user web service accepting user URLs)

### P1-1. SSRF via user-supplied URLs
`scrape_html` (`app/services/scraper.py:143`) fetches any `careers_url` a user
enters — including `http://169.254.169.254/...`, `http://localhost:8000/...`,
internal hosts. All clients use `follow_redirects=True` (`scraper.py:38-43`),
so even the ATS adapters (fixed hosts) can be redirected to internal addresses.
`career-ops` solved this; port its patterns (see research summary §"Safer
Scraper Behavior"):
- Allow only `http(s)` schemes; resolve DNS and reject private/loopback/
  link-local/metadata ranges (`ipaddress` stdlib) before fetching.
- For the three ATS adapters: pin to their known API hosts and set
  `follow_redirects=False` (Greenhouse/Lever/Ashby APIs don't redirect).
- Cap response size (e.g. 5 MB) and keep the 30 s timeout.
- Re-validate on redirect if redirects remain enabled for the HTML path.

### P1-2. Production-credential foot-guns
- `secret_key` defaults to `"dev-insecure-change-me"` (`app/config.py:19`) and
  nothing stops production use → anyone can forge JWTs for any user id. On
  startup, refuse (or log CRITICAL) when the default secret is used and the
  server binds beyond localhost.
- Session cookie lacks `secure=True` (`app/routers/auth.py:24-27`); make it
  configurable, default on when not localhost. (`samesite=lax` + `httponly`
  already mitigate CSRF/XSS reasonably for this app.)
- No rate limiting on `/api/auth/login` → offline brute force. `slowapi` is a
  one-file addition if/when this is deployed beyond personal use.

### P1-3. Registration race returns 500
`app/routers/auth.py:31-44` does check-then-insert; concurrent duplicate emails
hit the unique constraint → unhandled `IntegrityError` → 500. Catch it → 409.

## 4. P1 — correctness / reliability

### P1-4. Timezone mixing breaks the daily Telegram report
- Model columns use naive `func.now()` → SQLite stores **UTC** naive.
- `send_daily_reports` (`app/services/telegram_bot.py:96`) filters
  `created_at.date() == date.today()` → **local** date. Any server timezone
  offset from UTC drops or duplicates matches near the boundary (e.g. a scan at
  8 AM UTC+8 scores rows stamped the previous UTC day → report is empty).
- `Company.last_scraped_at` is set tz-aware (`matcher.py:150`) while the column
  is naive — SQLAlchemy/SQLite stores inconsistent representations.

Fix: standardize on UTC-aware datetimes everywhere (`DateTime(timezone=True)`
or store naive-UTC consistently); filter "today" as a UTC range
`[start, start+1day)`, not `.date()` equality in Python.

### P1-5. Telegram poller: tight loop, fragile thread
`poll_updates` (`app/services/telegram_bot.py:55-84`):
- A 401/404 (bad token) returns an HTTP response *instantly* (no long-poll
  wait) and is not an `httpx.HTTPError` → the `while` loop in
  `scheduler._telegram_loop` spins at full speed hammering Telegram.
- `resp.json()` can raise (non-JSON body) — uncaught → the daemon thread dies
  silently for the rest of the process lifetime.
- The API envelope's `"ok"` field is never checked.
Fix: wrap the loop body in broad try/except with exponential backoff (1→60 s),
check `ok`, and `raise_for_status()` before `.json()`.

### P1-6. SQLite concurrency: long write transactions
`run_for_user` holds one session/transaction across the *entire*
scrape-everything + score-everything run (minutes when LLM calls are slow).
The scheduler thread, Telegram link writes, and HTTP requests write
concurrently → "database is locked" after the 5 s default busy timeout.
Fix:
- Enable WAL + busy_timeout in `app/db.py` via an `event.listens_for(engine,
  "connect")` pragma hook (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=15000`).
- Commit incrementally in the pipeline: per company after upsert, per position
  (or small batch) after scoring — this also preserves partial progress when a
  run dies mid-way (today a crash at position 39/40 loses everything *and*
  re-bills the LLM next run, defeating the idempotency goal).

### P1-7. `POST /api/run` blocks for minutes
`app/routers/reports.py:17` runs the whole pipeline inside the request (sync
def → threadpool thread + DB session held throughout; proxies/browsers time
out; user gets no progress). Same issue for the MCP `run_daily_scan` tool
(client timeouts). Fix: introduce a `runs` table (id, user_id, status,
started_at, finished_at, summary JSON) + run in a background thread/
`BackgroundTasks`; `POST /api/run` returns the run id immediately,
`GET /api/runs/{id}` polls. Keep a synchronous mode for CLI. This is also the
natural seam for later queue adoption (arq/huey) — don't add a queue now.

### P1-8. MCP tokens silently expire after 7 days
`jobscout token` mints the same 7-day JWT used for browser sessions
(`app/auth.py:27-30`). An MCP config (`JOBSCOUT_MCP_TOKEN=...`) breaks a week
later with an opaque "Unauthorized". Add an `api_tokens` table (random opaque
token, hashed, user_id, label, created_at, revoked) checked by
`authenticate_token` first, JWT fallback second. `jobscout token` should mint
the long-lived kind.

### P1-9. HTML fallback produces junk positions
`scrape_html` (`app/services/scraper.py:143-176`):
- `mailto:jobs@acme.com` matches `_JOB_HINT` ("job") → fake Position created.
  Filter schemes: only http(s)/relative hrefs.
- Scheme-relative `//host/path` hrefs become `base + "//host/path"` → broken
  URL and broken dedup hash. Use `urllib.parse.urljoin(careers_url, href)`.
- Nav links ("Careers", "Jobs at Acme") become "positions" with no description;
  the LLM then scores `(no description scraped)` → meaningless scores billed
  per user per interest. Gate: skip LLM scoring (or hard-flag low confidence in
  the report) when `description is None`; surface "N undescribed links found at
  <company> — configure its ATS" as a run warning instead.

### P1-10. No retry/backoff; permanent failures re-billed daily
- Scraper and Ollama calls are single-shot; one transient 502 fails the
  company/position for the day.
- A *permanently* failing position (e.g. model rejects, content too long) is
  retried (and billed) every run forever, because failure leaves no
  MatchResult row.
Fix: add `tenacity` retries (2–3 attempts, exponential, jitter) around HTTP
calls; on terminal scoring failure persist a MatchResult with
`passed_filter=False, model="error"`-style marker (or a `score_failed` flag)
so the `already` set skips it; add a CLI flag to re-try failed ones.

### P1-11. Schema/spec mismatches worth deciding now (pre-Alembic)
- SPEC says `MatchResult` unique on `(user_id, position_id, resume_id)`;
  model uses `(…, interest_id)` too (`app/models.py:141-145`). The matcher
  keys its dedup on `(position_id, interest_id)` per resume — the model is
  right, the SPEC is stale. **Update SPEC.md.**
- `resume_id`/`interest_id` are nullable inside that unique constraint; in
  SQLite NULLs never collide, so rows with NULLs aren't deduped. Either make
  them non-nullable (nothing currently writes NULLs) or accept and document.
- Uploading a new resume invalidates the `already` set → full re-score of all
  positions × interests (one-time LLM cost spike). Intentional, but
  undocumented — add to DESIGN.md and consider only re-scoring positions above
  some prior score.
- `Float` imported unused in `app/models.py:11`.

## 5. P2 — design improvements (not blocking)

1. **Positions duplicated per user.** `Company` is per-user, so two users
   watching Anthropic scrape the same board twice and store every posting
   twice. Acceptable ≤10 users; before that grows, either dedupe scrape work
   per `(ats_type, token)` within one `run_for_all_users` pass (easy, no
   schema change) or split `Company` into global `CompanyBoard` + per-user
   watch rows (schema change — needs Alembic first).
2. **LLM schema drift.** `MATCH_SCHEMA` (`matcher.py:24-42`) is a hand-written
   dict, and responses go through `data.get(...)` with silent defaults — a
   model that returns `match_score: "85"` or omits a field is silently coerced
   to 0. Define a Pydantic `MatchVerdict` model; send
   `MatchVerdict.model_json_schema()` as the Ollama `format` and parse with
   `model_validate_json` → validation errors become real errors. One source of
   truth, reuses a dep you already have.
3. **No migrations.** `create_all` only. Adopt Alembic *before* any of the
   schema changes above (api_tokens, runs, liveness fields, P2 artifacts).
4. **Reporter scalability.** `build_report` loads every passed match then
   filters in Python (`reporter.py:31-67`). Fine at hundreds of rows; push the
   per-interest threshold join into SQL when it matters. Also `on_date`
   filtering belongs in SQL (and must be a UTC range — see P1-4).
5. **Resume truncation.** Hard cut at 6 000 chars (`matcher.py:59`) silently
   drops the tail (often skills/most-recent role on 3+ page resumes). At
   minimum warn at upload time when extracted text exceeds the budget; later,
   do a one-time LLM summarization of the overflow.
6. **Settings singleton at import time** (`app/config.py:63`) makes env
   overrides in tests awkward — tests must monkeypatch attributes instead of
   setting env. Low priority; touch only if test friction grows.
7. **Scheduler vs multi-worker.** In-process APScheduler + `uvicorn --workers
   N` would run the daily job N times. Current docs assume one process — add a
   loud comment/README note; the cron path already covers serious deployments.
8. **Pre-filter location semantics.** Adopt career-ops' allow/block/
   always-allow semantics; notably: a position with *no* location data
   currently fails a location-filtered interest (`matcher.py:88-93`) — missing
   data should pass through to the LLM, not be silently dropped (remote roles
   frequently have empty location fields).

## 6. Reuse map — don't hand-roll these

| Concern | Current | Use instead | Why |
|---|---|---|---|
| Password hashing | passlib (broken) | `bcrypt` direct (or `pwdlib`) | P0-1; passlib unmaintained |
| HTTP retries | none | `tenacity` | tiny dep, fixes P1-10 |
| LLM response schema | hand dict + `.get()` | Pydantic model (`model_json_schema()` / `model_validate_json`) | already a dep; kills drift |
| Ollama API | hand-rolled `httpx` client | keep it (it's thin & correct) — or official `ollama` pkg if features grow | hand-rolled is fine; don't add `litellm` unless multi-provider becomes a goal |
| Telegram | hand long-poll | keep minimal **with backoff fix** now; `python-telegram-bot` if commands/buttons get added | PTB handles rate limits, retries, webhooks |
| Migrations | none | `alembic` | required before any schema change |
| Rate limiting | none | `slowapi` | only when deployed multi-user |
| Background runs | blocking request | stdlib thread + `runs` table now; `arq`/`huey` only if scale demands | avoid queue infra at this size |
| More ATS coverage | 3 adapters + HTML | port Workable / SmartRecruiters / Recruitee from career-ops (public no-auth APIs) | big coverage win, zero LLM cost; see research summary |
| Scraper structure | if/elif dispatch | provider registry (`detect()`/`fetch()` protocol), ported from career-ops | makes adapter additions one-file changes |
| Big-board scraping (LinkedIn/Indeed) | n/a | `python-jobspy` — optional, different niche | only if "watch a company" expands to "search all boards" |
| JS-heavy pages | unimplemented `use_browser` flag | Playwright (already an optional extra) + career-ops liveness patterns | the flag and TODOs already point here |
| SSRF guard | none | stdlib `ipaddress` + DNS resolve check (no good maintained lib; `advocate` is stale) | P1-1 |

## 7. Recommended work order

Each step is independently shippable; verify after each.

1. **Unbreak auth (P0-1).** Swap passlib→bcrypt, update pyproject, add
   register/login TestClient test. Verify: `pytest`, manual register on
   `jobscout serve`.
2. **Surface failures (P0-2 + P1-5).** Errors into `RunResult.errors`;
   health() distinguishes 401; Telegram backoff + ok-check.
   Verify: run with a bogus Ollama key → run summary shows errors; health says
   unauthorized.
3. **SSRF + scraper hardening (P1-1, P1-9).** URL/IP validation, pinned ATS
   hosts, no-redirect for APIs, urljoin + scheme filter, response-size cap.
   Verify: unit tests with crafted hrefs (`mailto:`, `//host`, private IPs).
4. **SQLite + timezone (P1-4, P1-6).** WAL+busy_timeout pragmas, UTC
   normalization, range-based "today", incremental commits in matcher.
   Verify: concurrent run + dashboard click test; report-date test across
   simulated TZ.
5. **Async runs + durable tokens (P1-7, P1-8).** `runs` table + background
   execution + `GET /api/runs/{id}`; `api_tokens` table; `jobscout token`
   mints long-lived. (Adopt **Alembic** in this step — first real migration.)
6. **Retries + failure persistence (P1-10).** tenacity on HTTP; terminal
   scoring failures persisted so they're not re-billed.
7. **Pydantic LLM contract (P2-2)** — quick, do alongside 6.
8. **Provider registry + Workable/SmartRecruiters/Recruitee** (career-ops
   port, research summary §Provider Registry / §Additional ATS Providers).
9. **Test depth.** Matcher pipeline with a fake OllamaClient (happy path,
   LLM error, prefilter, dedup-on-rerun), reporter thresholds, auth flows,
   one router-level ownership test (user A can't read user B's data — the
   single most important multi-tenancy invariant, currently untested).
10. Then P2 features per `docs/PLAN.md` (artifacts/writer.py) and the
    career-ops liveness/legitimacy ideas.

## 7b. IMPLEMENTATION STATUS (updated 2026-06-09, by Opus)

Fixes below are committed and covered by tests (`pytest -q` → 17 passed, 1
skipped/live-network). Verified by execution, not just code-reading.

**Done**
- **P0-1** auth: dropped passlib, direct `bcrypt` in `app/auth.py` (72-byte
  truncation handled). `pyproject` swapped `passlib[bcrypt]`→`bcrypt>=4.1`.
  Test: register→login→dup(409)→bad-pw(401).
- **P0-2** failures surfaced: `RunResult.add_error/finalize_errors` (dedup + cap
  5 + "+N more"); `_score_position` reports errors; `OllamaClient.health()` now
  returns `ok|unauthorized|unreachable`; `/health` + `jobscout health` updated.
- **P1-5** Telegram poller: `poll_updates` `raise_for_status`+`ok`-check;
  `scheduler._telegram_loop` exponential backoff 1→60 s, never dies.
- **P1-1/P1-9** scraper: SSRF guard (`_validate_url` + `_host_is_public` blocks
  private/loopback/link-local/metadata + non-http), manual redirect w/ per-hop
  validation, streamed 5 MB cap, `follow_redirects=False`; all adapters route
  through `_fetch_json`/`_fetch_text`. HTML fallback filters mailto:/js/#,
  uses `urljoin`. Matcher skips descriptionless postings (no LLM bill) + warns.
- **P1-2/P1-3** security: `secret_is_default` → CRITICAL log on boot + `serve`
  refuses non-localhost bind; `cookie_secure` setting (Secure flag);
  registration race now catches `IntegrityError`→409.
- **P1-4/P1-6** time+SQLite: `app/timeutil.utcnow/to_naive_utc`, all timestamps
  naive-UTC (`default=utcnow`), reporter `on_date` is a UTC range in SQL,
  Telegram uses UTC date; WAL+`busy_timeout=15000` pragma on connect;
  matcher commits per-company and per-position (lock released between LLM calls,
  partial progress survives a crash).
- **P1-10** resilience: `tenacity` retries (3×, expo+jitter) on transient
  (timeout/429/5xx) for scraper `_fetch_bytes` and Ollama `_post`; terminal
  scoring failures persist a `model="error"` marker (skipped on re-run);
  `clear_failed_markers` + `run-daily --retry-failed`.
- **P2-2** LLM contract: `schemas.MatchVerdict` is the single source for the
  Ollama `format` (`model_json_schema()`) and parsing (`model_validate`);
  invalid/incomplete responses → ValidationError → marker, not silent zeros.
- **P2-8** prefilter: postings with no location no longer dropped by the
  location gate (defer to LLM).
- **P1-11** cleanups: removed unused `Float`/`Resume` imports; SPEC unique key
  corrected to include `interest_id` + error-marker note; DESIGN documents the
  resume-reupload re-score.
- **Tests**: new `tests/conftest.py` (throwaway DB, fresh schema per test) +
  `tests/test_app.py` — auth roundtrip, **tenant isolation** (B can't see/mutate
  A's data), scoring+dedup, marker-skip, descriptionless-skip, reporter
  thresholds, health states. Scraper SSRF + HTML-junk tests in `test_smoke.py`.

**Deferred (not yet done — recommended next batch, unchanged priority)**
- **P1-7** async runs (`runs` table + background exec + `GET /api/runs/{id}`):
  `POST /api/run` and MCP `run_daily_scan` still block for the full pipeline.
  Needs Alembic first. Biggest remaining UX risk for large watch-lists.
- **P1-8** durable MCP tokens (`api_tokens` table): `jobscout token` still mints
  a 7-day JWT, so MCP configs break after a week. Needs Alembic first.
- **P2-1** positions duplicated per user; **P2-3** Alembic (prereq for the
  above); **P2-4** reporter SQL threshold push-down; **P2-5** resume-truncation
  upload warning; **P2-6** settings-singleton test friction; **P2-7**
  multi-worker scheduler note.
- **Step 8** provider registry + Workable/SmartRecruiters/Recruitee adapters
  (see `career-ops-research-summary.md`); **Step 10** P2 features.
- **P1-2 remainder**: no login rate-limiting (`slowapi`) — only matters once
  deployed multi-user.

## 8. What is good — do not churn

- Service-layer separation; HTTP/MCP/CLI/scheduler all reuse `matcher`/
  `reporter` exactly as DESIGN intends. Keep this seam.
- Per-user scoping is consistently applied in every router and MCP tool
  (`_owned()` helpers, token-resolved user). Keep the pattern; just add the
  test in step 9.
- ATS-API-first strategy and `(company_id, external_id)` dedup — correct and
  cheap. The pre-filter-before-LLM gate is the right cost model.
- Dashboard JS escapes all server-sourced strings (`esc()` in
  `app/templates/dashboard.html:85`), Telegram output HTML-escapes scraped/LLM
  text (`reporter.py:99-104`). XSS posture is fine for what it is.
- Resume upload: size cap, filename sanitization, write-before-commit
  ordering, cascade cleanup. Solid.
- The hand-rolled Ollama client is small, correct (structured `format`,
  bearer-optional), and not worth replacing.
